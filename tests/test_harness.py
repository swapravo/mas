"""Harness tests. No database, no network, no model: a scripted LLM stands in."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from harness import Agent, ScriptedLLM, ToolError, ToolRegistry, allow_only, max_calls, validate_args  # noqa: E402


def act(tool, **args):
    return json.dumps({"tool": tool, "args": args, "reason": "r"})


def make():
    reg, log = ToolRegistry(), []

    @reg.tool(description="Add two numbers", parameters={
        "type": "object", "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
        "required": ["a", "b"]}, tags={"read"})
    def add(a, b):
        return a + b

    @reg.tool(description="Always fails", tags={"read"})
    def flaky():
        raise ToolError("try later")

    @reg.tool(description="Propose a note", parameters={
        "type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}, tags={"draft"})
    def note(text):
        log.append(text)
        return "ok"

    @reg.tool(description="End the run", parameters={
        "type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]}, terminal=True)
    def stop(reason):
        return reason

    return reg, log


def test_tools_are_generic_and_run_ends_on_terminal():
    reg, _ = make()
    r = Agent(ScriptedLLM([act("add", a=2, b=3), act("stop", reason="done")]), reg, "sys").run("go")
    assert r.status == "done" and r.final["tool"] == "stop"
    assert [e["result"] for e in r.state.events if e["type"] == "tool_result"][0] == 5


def test_catalogue_comes_from_registry():
    reg, _ = make()
    a = Agent(ScriptedLLM([]), reg, "sys")
    assert "add(a: integer, b: integer)" in a.system and "stop(reason: string)" in a.system


def test_unknown_tool_and_bad_args_are_fed_back_not_run():
    reg, _ = make()
    llm = ScriptedLLM([act("rm_rf"), act("add", a="x", b=1), act("add", a=1, b=1, c=9), act("stop", reason="x")])
    r = Agent(llm, reg, "sys").run("go")
    assert [e["why"] for e in r.state.events if e["type"] == "refused"][0] == "unknown tool"
    assert sum(e["type"] == "refused" for e in r.state.events) == 3
    assert r.status == "done"


def test_tool_error_is_reported_and_run_continues():
    reg, _ = make()
    r = Agent(ScriptedLLM([act("flaky"), act("stop", reason="x")]), reg, "sys").run("go")
    res = [e for e in r.state.events if e["type"] == "tool_result"][0]
    assert res["ok"] is False and "try later" in res["result"] and r.status == "done"


def test_guards_deny_and_cap():
    reg, log = make()
    llm = ScriptedLLM([act("note", text="1"), act("note", text="2"), act("stop", reason="x")])
    r = Agent(llm, reg, "sys", guards=[max_calls("draft", 1)]).run("go")
    assert log == ["1"] and any("cap reached" in e["why"] for e in r.state.events if e["type"] == "refused")
    r2 = Agent(ScriptedLLM([act("note", text="z"), act("stop", reason="x")]), reg, "sys",
               guards=[allow_only("stop")]).run("go")
    assert log == ["1"] and r2.status == "done"


def test_repeat_call_refused():
    reg, _ = make()
    r = Agent(ScriptedLLM([act("add", a=1, b=1), act("add", a=1, b=1), act("stop", reason="x")]), reg, "sys").run("go")
    assert any("already made" in e["why"] for e in r.state.events if e["type"] == "refused")


def test_step_budget_stops_loop():
    reg, _ = make()
    llm = ScriptedLLM(lambda s, m: act("add", a=len(m), b=1))
    r = Agent(llm, reg, "sys", max_steps=4).run("go")
    assert r.status == "max_steps" and r.state.step == 4


def test_garbage_replies_give_up():
    reg, _ = make()
    r = Agent(ScriptedLLM(["hello"] * 5), reg, "sys", max_bad_replies=2).run("go")
    assert r.status == "error"


def test_fenced_and_chatty_json_parses():
    reg, _ = make()
    r = Agent(ScriptedLLM(["Sure!\n```json\n" + act("stop", reason="x") + "\n```"]), reg, "sys").run("go")
    assert r.status == "done"


def test_resume_does_not_repeat_completed_steps():
    reg, log = make()
    saved = []
    crash = Agent(ScriptedLLM([act("note", text="once")]), reg, "sys", max_steps=1,
                  checkpoint=lambda s: saved.append(json.loads(json.dumps(s.to_json()))))
    assert crash.run("go", run_id="r1").status == "max_steps"
    # a "restart": new process, same checkpoint. The model re-proposes the same call, then stops.
    again = Agent(ScriptedLLM([act("note", text="once"), act("stop", reason="x")]), reg, "sys")
    r = again.resume(saved[-1])
    assert log == ["once"] and r.status == "done"


def test_redaction_applies_to_what_the_model_sees():
    reg, _ = make()
    seen = []
    def llm(system, messages):
        seen.append(messages[-1]["content"])
        return act("add", a=1, b=2) if len(seen) == 1 else act("stop", reason="x")
    Agent(ScriptedLLM(llm), reg, "sys", redact=lambda s: s.replace("3", "[n]")).run("go")
    assert "[n]" in seen[1] and "3" not in seen[1]


def test_events_stream_to_sink():
    reg, _ = make()
    got = []
    Agent(ScriptedLLM([act("stop", reason="x")]), reg, "sys", on_event=got.append).run("go", run_id="abc")
    assert got[-1]["type"] == "finished" and got[-1]["run_id"] == "abc"


def test_validate_args_nested():
    s = {"type": "object", "properties": {"xs": {"type": "array", "items": {"type": "integer"}},
                                          "k": {"enum": ["a", "b"]}}, "required": ["xs"]}
    assert validate_args(s, {"xs": [1, 2], "k": "a"}) == []
    assert validate_args(s, {"xs": [1, "2"]}) and validate_args(s, {"xs": [], "k": "z"}) and validate_args(s, {})
    assert validate_args({"type": "integer"}, True)  # bool is not an integer


# ---- OpenRouter adapter (network injected) ---------------------------------
import pytest  # noqa: E402
from harness import LLMUnavailable, OpenRouterLLM, _Retryable  # noqa: E402


def ok(text):
    return {"choices": [{"message": {"content": text}}]}


def test_openrouter_falls_through_models(monkeypatch):
    monkeypatch.setattr("harness.time.sleep", lambda s: None)
    calls = []
    def post(body):
        calls.append(body["model"])
        if body["model"] == "a:free":
            raise _Retryable(429)
        if body["model"] == "b:free":
            raise LLMUnavailable("HTTP 404")
        return ok("hello")
    llm = OpenRouterLLM(["a:free", "b:free", "c:free"], post=post)
    assert llm.complete("sys", [{"role": "user", "content": "hi"}]) == "hello"
    assert calls == ["a:free", "a:free", "b:free", "c:free"] and llm.last_model == "c:free"


def test_openrouter_all_fail_raises(monkeypatch):
    monkeypatch.setattr("harness.time.sleep", lambda s: None)
    llm = OpenRouterLLM(["a:free"], post=lambda b: {"choices": []})
    with pytest.raises(LLMUnavailable):
        llm.complete("s", [])


def test_only_free_models_allowed():
    with pytest.raises(ValueError):
        OpenRouterLLM(["openai/gpt-4o"])
