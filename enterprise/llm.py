"""The scripted "model": harness.ScriptedLLM plus virtual latency and a seeded failure rate.

A department's model is a small function `policy(system, messages) -> JSON action` (see depts/).
SimLLM wraps it so that every call costs virtual seconds and can fail with LLMUnavailable, the same
error the real OpenRouter adapter raises. It also counts calls: that count is what the task budget spends.
"""
from __future__ import annotations

import json
import random
import re
from typing import Callable

from harness import LLMUnavailable, Message, ScriptedLLM


class SimLLM:
    def __init__(self, policy: Callable[[str, list[Message]], str], *, latency: float, fail_rate: float,
                 rng: random.Random) -> None:
        self._inner = ScriptedLLM(policy)
        self.latency, self.fail_rate, self.rng = latency, fail_rate, rng
        self.calls = 0
        self.elapsed = 0.0

    def complete(self, system: str, messages: list[Message]) -> str:
        self.calls += 1
        self.elapsed += self.latency * (0.8 + 0.4 * self.rng.random())     # +-20%, seeded
        if self.rng.random() < self.fail_rate:
            raise LLMUnavailable("simulated model outage")
        return self._inner.complete(system, messages)


# -- helpers for writing scripted policies -------------------------------------------------

def act(tool: str, reason: str = "scripted", **args) -> str:
    return json.dumps({"tool": tool, "args": args, "reason": reason})


def submit(result: dict, reason: str = "done") -> str:
    """The terminal action. The result is a JSON string because harness.validate_args does not allow
    free-form objects (an object schema must list its properties)."""
    return act("submit", reason, result=json.dumps(result, sort_keys=True))


def task_of(messages: list[Message]) -> dict:
    """The first user turn is the task, as JSON."""
    return json.loads(messages[0]["content"])


_OBS = re.compile(r"<(tool_result|refused|error)>(.*)</\1>", re.S)


def observations(messages: list[Message]) -> list[dict]:
    """What the harness fed back so far, oldest first: {'kind', 'tool', 'ok', 'result'} per observation."""
    out = []
    for m in messages[1:]:
        if m["role"] != "user" or not (hit := _OBS.search(m["content"])):
            continue
        kind, body = hit.groups()
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            data = body
        out.append({"kind": kind, **(data if isinstance(data, dict) else {"result": data})})
    return out
