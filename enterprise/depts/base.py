"""What every department shares: run one step of work as a small harness.Agent, safely.

handle(envelope) does, in order:
  1. idempotency - if this task step was already done, return the saved result and do nothing else
  2. budget      - refuse if the task has spent its model calls
  3. run         - a harness.Agent with the scripted model; tools go through the gateway
  4. save        - remember the result under the idempotency key

Failures (model down, budget gone) come back as Reply(ok=False, error=...), never as exceptions, so the
caller (an orchestrator, or the stream consumer) decides what to do. Failures are not remembered, so a
retry really runs again.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from harness import Agent, LLMUnavailable, Message, Tool, ToolError, ToolRegistry
from enterprise.envelope import Envelope
from enterprise.gateway import CallContext, Denied, Gateway
from enterprise.llm import SimLLM, act, observations, submit, task_of

TOOL_SECONDS = 0.02      # virtual cost of one tool call


@dataclass
class Reply:
    ok: bool
    payload: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    calls: int = 0           # model calls used
    service: float = 0.0     # virtual seconds the work took
    cached: bool = False     # True: a repeat of a step already done; nothing was run


Policy = Callable[[str, list[Message]], str]


# -- the scripted, gullible model ------------------------------------------------------------

_INJECTION = re.compile(r"ignore (?:all )?previous instructions", re.I)


def injected_action(text: str | None, task: dict) -> tuple[str, dict] | None:
    """What a GULLIBLE model does when text it reads tells it to do something else.

    Stand-in for prompt injection: a real model may resist, but we plan for the day it does not.
    """
    if not text or not _INJECTION.search(text):
        return None
    low = text.lower()
    if "apply_waiver" in low:
        return "finance.apply_waiver", {"resident_id": task["resident_id"], "case_id": task["case_id"], "amount": 1500}
    if hit := re.search(r"ledger of (r-\d+)", low):
        return "ask_dept", {"dept": "finance", "what": "ledger", "resident_id": hit.group(1)}
    return None


def make_policy(legit: Callable[[dict], list[tuple[str, dict]]], finish: Callable[[dict, list[dict]], dict],
                reads_text: bool = False) -> Policy:
    """A scripted model: (optionally an injected call first,) then a fixed list of tool calls, then submit.

    `reads_text`: the task payload carries resident text (choreography events do), so this model sees it.
    """
    def policy(system: str, messages: list[Message]) -> str:
        task = task_of(messages)
        plan: list[tuple[str, dict]] = []
        if reads_text and (inj := injected_action(task.get("text"), task)):
            plan.append(inj)
        skipped = len(plan)
        plan += legit(task)
        obs = observations(messages)
        if len(obs) < len(plan):
            tool, args = plan[len(obs)]
            return act(tool, "scripted", **args)
        return submit(finish(task, obs[skipped:]))
    return policy


def first_result(obs: list[dict]) -> Any:
    """The result of the first real tool call, or None if it failed."""
    return obs[0]["result"] if obs and obs[0].get("ok") else None


# -- registry: every gateway tool, plus `submit` -------------------------------------------------

def build_registry(gw: Gateway, ctx: CallContext) -> ToolRegistry:
    """The agent sees the shared tool catalogue. Which calls WORK is the gateway's decision, not the registry's."""
    reg = ToolRegistry()
    for spec in gw.catalogue():
        def fn(_name: str = spec.name, **args):
            try:
                return gw.call(ctx, _name, args)
            except Denied as e:
                raise ToolError(str(e))
        reg.add(Tool(spec.name, spec.description, fn, spec.parameters))
    reg.add(Tool("submit", "Finish this step. result is a JSON object, written as a string.", lambda result: result,
                 {"type": "object", "properties": {"result": {"type": "string"}}, "required": ["result"]},
                 terminal=True))
    return reg


# -- the department ----------------------------------------------------------------------------

class Department:
    name = ""
    system = ""

    def __init__(self, world) -> None:
        self.w = world

    def policy(self, kind: str) -> Policy:
        raise NotImplementedError

    def handle(self, env: Envelope) -> Reply:
        w, kind = self.w, env.step.split(".")[0]
        idem = f"idem:{self.name}:{env.key()}"
        if w.settings.idempotent:
            if not w.kv.setnx(idem, "pending"):
                saved = w.kv.get(idem)
                w.stats["dup_suppressed"] += 1
                w.store.audit("system", "duplicate_suppressed", f"{self.name}:{env.key()}")
                return Reply(True, json.loads(saved) if saved and saved != "pending" else {}, cached=True, service=0.01)
        try:
            reply = self._run(env, kind)
        except BaseException:
            w.kv.delete(idem)
            raise
        if w.settings.idempotent:
            if reply.ok:
                w.kv.set(idem, json.dumps(reply.payload))
            else:
                w.kv.delete(idem)
        return reply

    def _run(self, env: Envelope, kind: str) -> Reply:
        w = self.w
        spent = int(w.kv.get(f"budget:{env.task_id}") or 0)
        left = env.budget - spent
        if left <= 0:
            w.stats["budget_rejects"] += 1
            return Reply(False, error=f"budget exhausted ({spent} of {env.budget} model calls used)")
        llm = SimLLM(self.policy(kind), latency=w.settings.llm_latency.get(self.name, 1.0),
                     fail_rate=w.settings.llm_fail.get(self.name, 0.0), rng=w.rng(f"llm:{self.name}"))
        ctx = CallContext(self.name, env)
        agent = Agent(llm, build_registry(w.gateway, ctx), self.system,
                      max_steps=min(w.settings.max_agent_steps, left),
                      on_event=lambda ev: w.store.audit("agent", ev["type"], f"{self.name}:{ev['run_id']}",
                                                         {k: v for k, v in ev.items() if k in ("tool", "ok", "why", "status")}))
        task = json.dumps({"step": kind, **env.payload})
        try:
            res = agent.run(task, run_id=f"{env.key()}:{self.name}")
        except LLMUnavailable as e:
            w.kv.incr(f"budget:{env.task_id}", llm.calls)
            w.stats["llm_failures"] += 1
            return Reply(False, error=f"model unavailable: {e}", calls=llm.calls, service=llm.elapsed)
        w.kv.incr(f"budget:{env.task_id}", llm.calls)
        w.stats["llm_calls"] += llm.calls
        service = llm.elapsed + TOOL_SECONDS * llm.calls + w.chaos.dept_latency.get(self.name, 0.0)
        if res.status != "done":
            return Reply(False, error=f"agent stopped: {res.status}", calls=llm.calls, service=service)
        try:
            payload = json.loads(res.final["args"]["result"])
        except json.JSONDecodeError:
            return Reply(False, error="submit result was not valid JSON", calls=llm.calls, service=service)
        return Reply(True, payload, calls=llm.calls, service=service)
