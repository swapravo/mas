"""The tool gateway: the only door between an agent and the world.

Every tool call from every agent comes here. Three checks, each can be switched off (Defences):

  1. gateway  - is this tool on the caller's allow-list?            (stops: prompt injection)
  2. obo      - does the call touch only the person it is made FOR?   (stops: confused deputy)
  3. rls      - not here: the database enforces it (store.py).        (stops: direct cross-department read)

A tool runs with the role of the department that OWNS it, like a service calling its own database. So
if the gateway wrongly lets a Collections agent call a Finance tool, the tool still works: that is why
the allow-list has to be right. Every call, allowed or refused, is audited.

When a check is OFF and the call would have been refused, we let it run and record a `leak`. That is how
the attack tests count what got through.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from enterprise.envelope import Envelope
from enterprise.settings import Defences


class Denied(Exception):
    def __init__(self, layer: str, reason: str) -> None:
        super().__init__(f"denied by {layer}: {reason}")
        self.layer, self.reason = layer, reason


@dataclass
class CallContext:
    """Who is calling, and for whom. Built from the incoming envelope, so on_behalf_of travels with the work."""
    role: str                 # the department whose agent is calling
    env: Envelope             # the handoff being worked on
    @property
    def on_behalf_of(self) -> str:
        return self.env.on_behalf_of


@dataclass
class ToolSpec:
    name: str                              # "finance.apply_waiver"
    owner: str                             # department whose database role runs it
    fn: Callable[..., Any]                 # fn(db, ctx, **args)
    description: str
    parameters: dict[str, Any]
    scope_arg: str | None = None           # which argument names the resident this call touches


class Bound:
    """The store, bound to one role: what a tool gets as `db`."""
    def __init__(self, store, role: str) -> None:
        self.store, self.role = store, role

    def put(self, kind, key, payload):
        self.store.put(self.role, kind, key, payload)

    def get(self, kind, key):
        return self.store.get(self.role, kind, key)

    def scan(self, kind):
        return self.store.scan(self.role, kind)


class Gateway:
    def __init__(self, store, defences: Defences) -> None:
        self.store, self.defences = store, defences
        self._tools: dict[str, ToolSpec] = {}
        self.allow: dict[str, set[str]] = {}
        self.leaks: list[dict] = []        # a check was off and the call went through
        self.blocked: list[dict] = []      # a check was on and refused the call

    def register(self, spec: ToolSpec, allowed_roles: tuple[str, ...]) -> None:
        self._tools[spec.name] = spec
        for r in allowed_roles:
            self.allow.setdefault(r, set()).add(spec.name)

    def catalogue(self) -> list[ToolSpec]:
        return list(self._tools.values())

    def call(self, ctx: CallContext, name: str, args: dict[str, Any]) -> Any:
        spec = self._tools.get(name)
        if spec is None:
            raise Denied("gateway", f"no such tool {name}")
        who = {"role": ctx.role, "on_behalf_of": ctx.on_behalf_of, "tool": name, "args": args, "task": ctx.env.task_id}
        violations: list[tuple[str, str]] = []
        if name not in self.allow.get(ctx.role, set()):
            violations.append(("gateway", f"{ctx.role} is not allowed to call {name}"))
        kind, _, who_id = ctx.on_behalf_of.partition(":")
        if spec.scope_arg and kind == "resident" and args.get(spec.scope_arg) != who_id:
            violations.append(("obo", f"call is for {ctx.on_behalf_of} but touches {args.get(spec.scope_arg)}"))

        for layer, reason in violations:
            if getattr(self.defences, layer):
                self.blocked.append({**who, "layer": layer, "reason": reason})
                self.store.audit("agent", "tool_refused", name, {**who, "layer": layer, "reason": reason})
                raise Denied(layer, reason)

        result = spec.fn(Bound(self.store, spec.owner), ctx, **args)
        for layer, reason in violations:       # only reached if that layer was off
            self.leaks.append({**who, "layer": layer, "reason": reason})
        self.store.audit("agent", "tool_call", name, {**who, "leak": [v[0] for v in violations]})
        return result
