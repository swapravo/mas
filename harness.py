"""A small agent harness, written from scratch (no agent framework).

It knows nothing about collections, fees or the schema. It provides:
  - a tool registry: any Python function can become a tool, described by a JSON schema
  - argument validation before every call
  - guards: hooks that can deny a call (caps, allow-lists, quiet hours, scope checks)
  - the loop: ask the model for ONE action -> validate -> guard -> run -> feed back the result
  - budgets and stop conditions: max steps, terminal tools, repeated-call detection
  - checkpoints after every step, so a crashed run resumes where it left off
  - an event stream for audit and tracing

The model talks in JSON, one action per turn (same shape as prompts.py):
    {"tool": "<name>", "args": {...}, "reason": "<one sentence>"}
Standard library only. Model providers plug in through the LLM protocol (OpenRouter free models here).
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from config import FREE_MODELS, OPENROUTER_KEY_ENV, load_dotenv

# ---------------------------------------------------------------------------
# Model interface
# ---------------------------------------------------------------------------

Message = dict[str, str]  # {"role": "user" | "assistant", "content": "..."}


class LLM(Protocol):
    def complete(self, system: str, messages: list[Message]) -> str: ...


class ScriptedLLM:
    """Replays canned replies. For tests, offline demos and the no-key fallback."""

    def __init__(self, replies: list[str] | Callable[[str, list[Message]], str]):
        self._replies, self._i = replies, 0

    def complete(self, system: str, messages: list[Message]) -> str:
        if callable(self._replies):
            return self._replies(system, messages)
        if self._i >= len(self._replies):
            raise RuntimeError("ScriptedLLM ran out of replies")
        out = self._replies[self._i]
        self._i += 1
        return out


class LLMUnavailable(RuntimeError):
    pass


class OpenRouterLLM:
    """Calls free models on OpenRouter (OpenAI-compatible chat API). Key: OPENROUTER_LLM_KEY in .env.

    Tries each model in `models` until one answers; retries a model once on 429/5xx.
    `post` is injectable so tests need no network.
    """
    URL = "https://openrouter.ai/api/v1/chat/completions"

    def __init__(self, models: list[str] | None = None, *, temperature: float = 0.2,
                 max_tokens: int = 1024, timeout: float = 60, post: Callable[[dict], dict] | None = None):
        load_dotenv()
        self.models = [m for m in (models or FREE_MODELS)]
        if any(not m.endswith(":free") for m in self.models):
            raise ValueError("only free models (':free') are allowed")
        self.temperature, self.max_tokens, self.timeout = temperature, max_tokens, timeout
        self._post = post or self._http_post
        self.last_model: str | None = None

    def _http_post(self, body: dict) -> dict:
        key = os.environ.get(OPENROUTER_KEY_ENV)
        if not key:
            raise LLMUnavailable(f"{OPENROUTER_KEY_ENV} is not set (put it in .env)")
        req = urllib.request.Request(self.URL, json.dumps(body).encode(), {
            "Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            raise _Retryable(e.code) if e.code in (408, 429) or e.code >= 500 else LLMUnavailable(f"HTTP {e.code}")
        except (urllib.error.URLError, TimeoutError) as e:
            raise _Retryable(0) from e

    def complete(self, system: str, messages: list[Message]) -> str:
        errors = []
        for model in self.models:
            for attempt in range(2):
                try:
                    out = self._post({"model": model, "temperature": self.temperature, "max_tokens": self.max_tokens,
                                      "messages": [{"role": "system", "content": system}, *messages]})
                    text = (out.get("choices") or [{}])[0].get("message", {}).get("content")
                    if text:
                        self.last_model = model
                        return text
                    errors.append(f"{model}: empty reply")
                    break
                except _Retryable as e:
                    errors.append(f"{model}: retryable {e.args[0]}")
                    time.sleep(0 if attempt else 1)
                except LLMUnavailable as e:
                    errors.append(f"{model}: {e}")
                    break
        raise LLMUnavailable("all free models failed: " + "; ".join(errors))


class _Retryable(Exception):
    pass


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

class ToolError(Exception):
    """Raise inside a tool for an expected failure. The message goes back to the model."""


@dataclass
class Tool:
    name: str
    description: str
    fn: Callable[..., Any]
    parameters: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {}})
    terminal: bool = False          # calling it ends the run (e.g. stop, close_case)
    tags: frozenset[str] = frozenset()  # free-form labels guards can use, e.g. {"read"}, {"draft"}

    def signature(self) -> str:
        props = self.parameters.get("properties", {})
        req = set(self.parameters.get("required", []))
        args = ", ".join(f"{k}{'' if k in req else '?'}: {v.get('type', 'any')}" for k, v in props.items())
        return f"{self.name}({args}): {self.description}"


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def add(self, tool: Tool) -> Tool:
        if tool.name in self._tools:
            raise ValueError(f"tool already registered: {tool.name}")
        self._tools[tool.name] = tool
        return tool

    def tool(self, name: str | None = None, *, description: str = "", parameters: dict | None = None,
             terminal: bool = False, tags: set[str] | frozenset[str] = frozenset()):
        """Decorator: @registry.tool(description=..., parameters={...})"""
        def wrap(fn: Callable[..., Any]) -> Callable[..., Any]:
            self.add(Tool(name or fn.__name__, description or (fn.__doc__ or "").strip().split("\n")[0],
                          fn, parameters or {"type": "object", "properties": {}}, terminal, frozenset(tags)))
            return fn
        return wrap

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools)

    def catalogue(self) -> str:
        """Human-readable tool list generated from the registry, appended to the system prompt."""
        return "\n".join(f"- {t.signature()}" for t in self._tools.values())


_TYPES = {"string": str, "integer": int, "number": (int, float), "boolean": bool,
          "object": dict, "array": list, "null": type(None)}


def validate_args(schema: dict[str, Any], args: Any, path: str = "args") -> list[str]:
    """Minimal JSON-schema check: type, required, enum, properties, items, no extra keys."""
    errs: list[str] = []
    t = schema.get("type")
    if t:
        py = _TYPES[t]
        ok = isinstance(args, py) and not (t in ("integer", "number") and isinstance(args, bool))
        if not ok:
            return [f"{path}: expected {t}, got {type(args).__name__}"]
    if "enum" in schema and args not in schema["enum"]:
        errs.append(f"{path}: must be one of {schema['enum']}")
    if t == "object":
        props = schema.get("properties", {})
        for k in schema.get("required", []):
            if k not in args:
                errs.append(f"{path}.{k}: required")
        for k, v in args.items():
            if k not in props:
                errs.append(f"{path}.{k}: unknown argument")
            else:
                errs += validate_args(props[k], v, f"{path}.{k}")
    if t == "array" and "items" in schema:
        for i, v in enumerate(args):
            errs += validate_args(schema["items"], v, f"{path}[{i}]")
    return errs


# ---------------------------------------------------------------------------
# Guards: policy checks that run before a tool is called
# ---------------------------------------------------------------------------

@dataclass
class Call:
    tool: Tool
    args: dict[str, Any]
    reason: str
    step: int


Guard = Callable[[Call, "RunState"], str | None]  # return a refusal message, or None to allow


def allow_only(*names: str) -> Guard:
    """Deny any tool not in the list (a per-run allow-list on top of the registry)."""
    allowed = set(names)
    return lambda c, s: None if c.tool.name in allowed else f"tool '{c.tool.name}' is not permitted here"


def max_calls(tag: str, limit: int) -> Guard:
    """Cap how many tagged calls (e.g. 'draft') a run may make."""
    return lambda c, s: (None if tag not in c.tool.tags or
                         sum(1 for e in s.events if e["type"] == "tool_result" and tag in e["tags"]) < limit
                         else f"cap reached: at most {limit} '{tag}' calls per run")


# ---------------------------------------------------------------------------
# Run state, result, checkpoints
# ---------------------------------------------------------------------------

@dataclass
class RunState:
    run_id: str
    messages: list[Message] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    step: int = 0

    def to_json(self) -> dict[str, Any]:
        return {"run_id": self.run_id, "messages": self.messages, "events": self.events, "step": self.step}

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "RunState":
        return cls(d["run_id"], d["messages"], d["events"], d["step"])


@dataclass
class RunResult:
    status: str                       # "done" | "max_steps" | "error"
    final: dict[str, Any] | None      # the terminal call: {"tool", "args", "result"}
    state: RunState


Checkpoint = Callable[[RunState], None]   # e.g. write state.to_json() to agent_run.checkpoint
EventSink = Callable[[dict[str, Any]], None]  # e.g. insert into audit_log


# ---------------------------------------------------------------------------
# Parsing the model's reply
# ---------------------------------------------------------------------------

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.S)


def parse_action(text: str) -> tuple[dict[str, Any] | None, str | None]:
    """Extract {"tool","args","reason"} from a reply. Tolerates code fences and surrounding prose."""
    s = _FENCE.sub("", text.strip())
    try:
        obj = json.loads(s)
    except json.JSONDecodeError:
        a, b = s.find("{"), s.rfind("}")
        if a < 0 or b <= a:
            return None, "reply was not JSON. Return only the JSON object."
        try:
            obj = json.loads(s[a:b + 1])
        except json.JSONDecodeError as e:
            return None, f"invalid JSON: {e.msg}. Return only the JSON object."
    if not isinstance(obj, dict) or not isinstance(obj.get("tool"), str):
        return None, 'reply must be {"tool": "<name>", "args": {...}, "reason": "..."}'
    if not isinstance(obj.get("args", {}), dict):
        return None, "args must be an object"
    return {"tool": obj["tool"], "args": obj.get("args", {}), "reason": str(obj.get("reason", ""))}, None


# ---------------------------------------------------------------------------
# The agent
# ---------------------------------------------------------------------------

class Agent:
    def __init__(self, llm: LLM, tools: ToolRegistry, system_prompt: str, *,
                 guards: list[Guard] | None = None,
                 max_steps: int = 8,
                 max_bad_replies: int = 2,
                 redact: Callable[[str], str] = lambda s: s,
                 checkpoint: Checkpoint | None = None,
                 on_event: EventSink | None = None,
                 include_catalogue: bool = True):
        self.llm, self.tools, self.guards = llm, tools, guards or []
        self.max_steps, self.max_bad = max_steps, max_bad_replies
        self.redact, self.checkpoint, self.on_event = redact, checkpoint, on_event
        self.system = system_prompt + (f"\n\n# Registered tools (authoritative)\n{tools.catalogue()}"
                                       if include_catalogue else "")

    # -- public -------------------------------------------------------------
    def run(self, task: str, run_id: str = "run") -> RunResult:
        """Start a fresh run. `task` is the first user turn (e.g. prompts.build_turn(...))."""
        st = RunState(run_id, [{"role": "user", "content": task}])
        return self._loop(st)

    def resume(self, saved: dict[str, Any]) -> RunResult:
        """Continue from a checkpoint (RunState.to_json()). Completed steps are not repeated."""
        return self._loop(RunState.from_json(saved))

    # -- internals ----------------------------------------------------------
    def _emit(self, st: RunState, type_: str, **data: Any) -> None:
        ev = {"type": type_, "step": st.step, **data}
        st.events.append(ev)
        if self.on_event:
            self.on_event({**ev, "run_id": st.run_id})

    def _feed(self, st: RunState, role: str, content: str) -> None:
        st.messages.append({"role": role, "content": content})

    def _observe(self, st: RunState, label: str, payload: Any) -> None:
        text = payload if isinstance(payload, str) else json.dumps(payload, default=str)
        self._feed(st, "user", f"<{label}>{self.redact(text)}</{label}>")

    def _loop(self, st: RunState) -> RunResult:
        bad = 0
        seen: set[str] = {e["sig"] for e in st.events if e["type"] == "tool_result"}
        while st.step < self.max_steps:
            st.step += 1
            reply = self.llm.complete(self.system, st.messages)
            self._feed(st, "assistant", reply)
            action, err = parse_action(reply)
            if err:
                bad += 1
                self._emit(st, "bad_reply", error=err)
                if bad > self.max_bad:
                    return self._finish(st, "error", None)
                self._observe(st, "error", err)
                self._save(st)
                continue

            tool = self.tools.get(action["tool"])
            if tool is None:
                self._emit(st, "refused", tool=action["tool"], why="unknown tool")
                self._observe(st, "error", f"unknown tool '{action['tool']}'. Available: {self.tools.names()}")
                self._save(st)
                continue

            problems = validate_args(tool.parameters, action["args"])
            if problems:
                self._emit(st, "refused", tool=tool.name, why="; ".join(problems))
                self._observe(st, "error", "invalid arguments: " + "; ".join(problems))
                self._save(st)
                continue

            call = Call(tool, action["args"], action["reason"], st.step)
            sig = f"{tool.name}:{json.dumps(call.args, sort_keys=True, default=str)}"
            denial = next((d for g in self.guards if (d := g(call, st))), None)
            if denial is None and sig in seen and not tool.terminal:
                denial = "you already made this exact call. Choose a different action or stop."
            if denial:
                self._emit(st, "refused", tool=tool.name, args=call.args, why=denial, reason=call.reason)
                self._observe(st, "refused", denial)
                self._save(st)
                continue

            try:
                result = tool.fn(**call.args)
                ok = True
            except ToolError as e:
                result, ok = f"tool error: {e}", False
            except Exception as e:  # a bug in a tool must not crash the run
                result, ok = f"tool failed: {type(e).__name__}", False
            seen.add(sig)
            self._emit(st, "tool_result", tool=tool.name, args=call.args, reason=call.reason,
                       ok=ok, tags=sorted(tool.tags), sig=sig,
                       result=result if isinstance(result, (str, int, float, bool, type(None), list, dict)) else str(result))
            if tool.terminal and ok:
                return self._finish(st, "done", {"tool": tool.name, "args": call.args, "result": result})
            self._observe(st, "tool_result", {"tool": tool.name, "ok": ok, "result": result})
            self._save(st)

        return self._finish(st, "max_steps", None)

    def _save(self, st: RunState) -> None:
        if self.checkpoint:
            self.checkpoint(st)

    def _finish(self, st: RunState, status: str, final: dict[str, Any] | None) -> RunResult:
        self._emit(st, "finished", status=status)
        self._save(st)
        return RunResult(status, final, st)
