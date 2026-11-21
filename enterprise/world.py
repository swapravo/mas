"""The world: store, kv, gateway and the four departments, wired together.

This file is the core (no clock, no queues yet). sim.py and the orchestrators build on it.
"""
from __future__ import annotations

import random
from collections import Counter

from enterprise.depts import collections_dept, finance, legal, ops
from enterprise.envelope import Envelope, EnvelopeError, parse
from enterprise.gateway import Gateway, ToolSpec
from enterprise.kv import MemoryKV
from enterprise.settings import Chaos, Defences, Settings
from enterprise.store import MemoryStore
from harness import ToolError


class World:
    def __init__(self, settings: Settings | None = None, chaos: Chaos | None = None,
                 defences: Defences | None = None, store=None, kv=None) -> None:
        self.settings = settings or Settings()
        self.chaos = chaos or Chaos()
        self.defences = defences or Defences()
        self.store = store or MemoryStore(rls=self.defences.rls)
        self.kv = kv or MemoryKV()
        self.stats: Counter = Counter()
        self._rngs: dict[str, random.Random] = {}
        self.clock = lambda: 0.0                 # sim.py replaces this with the virtual clock
        self.gateway = Gateway(self.store, self.defences)
        collections_dept.register(self.gateway)
        ops.register(self.gateway)
        finance.register(self.gateway, self.settings.policy.fee_limit)
        legal.register(self.gateway)
        self._register_ask()
        self.depts = {d.name: d for d in (collections_dept.Collections(self), ops.Ops(self),
                                          finance.Finance(self), legal.Legal(self))}

    def rng(self, name: str) -> random.Random:
        """One seeded random stream per component (created once), so adding a component never shifts another's numbers."""
        if name not in self._rngs:
            self._rngs[name] = random.Random(f"{self.settings.seed}:{name}")
        return self._rngs[name]

    # -- agent-to-agent calls --------------------------------------------------------
    def _register_ask(self) -> None:
        def ask_dept(db, ctx, dept: str, what: str, resident_id: str):
            """Ask another department's agent a question. The person we act for travels with the call."""
            env = ctx.env.forward(frm=ctx.role, to=dept, step=f"ask.{what}.{resident_id}",
                                  payload={"what": what, "resident_id": resident_id})
            try:
                reply = self.ask(env)
            except EnvelopeError as e:
                raise ToolError(str(e))
            if not reply.ok:
                raise ToolError(f"{dept} could not answer: {reply.error}")
            return reply.payload
        self.gateway.register(ToolSpec(
            "ask_dept", "orch", ask_dept, "Ask another department's agent for something about a resident.",
            {"type": "object", "properties": {"dept": {"type": "string", "enum": ["ops", "finance", "legal"]},
                                              "what": {"type": "string"}, "resident_id": {"type": "string"}},
             "required": ["dept", "what", "resident_id"]}), ("collections", "ops", "finance", "legal"))

    def ask(self, env: Envelope):
        """Deliver a direct agent-to-agent handoff. It is validated like any other (hops, deadline, clearance...)."""
        env = parse(env.to_wire(), now=self.clock(), recipient=env.to_dept, max_hops=self.settings.max_hops)
        return self.depts[env.to_dept].handle(env)
