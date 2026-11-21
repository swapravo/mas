"""A tiny discrete-event simulator, and the message network built on it.

Time is VIRTUAL. `Sim.at(delay, fn)` schedules a call; `Sim.run(until)` plays events in time order
(ties broken by insertion order), so a run is exactly repeatable and takes milliseconds.

Net is the broker. Every consumer (a department or an orchestrator) has:
  - an inbox (a queue; names match the Celery queues: dept.<name>)
  - `concurrency` worker slots
  - a handler that does the work and says how long it took and what to send next
A consumer that is "killed" simply stops taking from its inbox; messages pile up. That is exactly
what a dead worker looks like from the outside.

Events (choreography) go through an EventLog (a Redis-Streams-shaped log) instead of one inbox:
every consumer group reads every event and ignores the types it does not subscribe to.
"""
from __future__ import annotations

import heapq
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

from enterprise.envelope import Envelope, EnvelopeError, parse
from enterprise.eventlog import MemoryEventLog


class Sim:
    def __init__(self) -> None:
        self.now = 0.0
        self._heap: list[tuple[float, int, Callable, tuple]] = []
        self._seq = 0

    def at(self, delay: float, fn: Callable, *args: Any) -> None:
        heapq.heappush(self._heap, (self.now + max(delay, 0.0), self._seq, fn, args))
        self._seq += 1

    def run(self, until: float) -> None:
        while self._heap and self._heap[0][0] <= until:
            t, _, fn, args = heapq.heappop(self._heap)
            self.now = t
            fn(*args)


@dataclass
class Result:
    """What a handler returns: how long the work took, what to send next, and whether it worked."""
    service: float
    out: list[Envelope] = field(default_factory=list)
    ok: bool = True


@dataclass
class Consumer:
    handler: Callable[[Envelope], Result]
    concurrency: int
    inbox: deque = field(default_factory=deque)       # items: (envelope, ack token or None, delivery attempt)
    busy: int = 0


class Net:
    def __init__(self, sim: Sim, world, known: Callable[[str], dict | None], log=None) -> None:
        self.sim, self.w, self.known = sim, world, known
        self.log = log or MemoryEventLog()
        self.c: dict[str, Consumer] = {}
        self.subs: dict[str, set[str]] = {}          # consumer group -> event types it reacts to
        self.sent = 0                                 # messages put on the wire (requests, results, events)
        self.rejections: list[dict] = []

    # -- setup ---------------------------------------------------------------
    def add(self, name: str, handler: Callable[[Envelope], Result], concurrency: int = 1,
            subscribes: set[str] | None = None) -> None:
        self.c[name] = Consumer(handler, concurrency)
        if subscribes is not None:
            self.subs[name] = subscribes
            self.log.create_group(name)

    # -- chaos ---------------------------------------------------------------
    def dead(self, name: str) -> bool:
        ch = self.w.chaos
        return name in ch.kill and self.sim.now >= ch.kill_at

    # -- sending ---------------------------------------------------------------
    def send(self, env: Envelope) -> None:
        stats, ch = self.w.stats, self.w.chaos
        self.sent += 1
        stats["messages"] += 1
        if self.dead("bus"):
            stats["lost_bus_down"] += 1
            return
        copies = 2 if ch.duplicate_every and self.sent % ch.duplicate_every == 0 else 1
        stats["dup_injected"] += copies - 1
        wire = env.to_wire()
        for _ in range(copies):
            if env.kind == "event":
                self.log.publish(wire)
                self.sim.at(self.w.settings.net_latency + ch.net_latency, self._poll_all)
            else:
                self.sim.at(self.w.settings.net_latency + ch.net_latency, self._arrive, wire, env.to_dept, None, 1)

    # -- receiving -------------------------------------------------------------
    def _poll_all(self) -> None:
        for group in self.subs:
            self._poll(group)

    def _poll(self, group: str) -> None:
        if self.dead("bus") or self.dead(group):
            return                                     # the entries stay in the stream: that is "lag"
        for entry_id, wire in self.log.read_group(group):
            self._arrive(wire, group, (group, entry_id), 1)

    def _arrive(self, wire: dict, dest: str, token, attempt: int) -> None:
        try:
            env = parse(wire, now=self.sim.now, recipient=dest, max_hops=self.w.settings.max_hops, known=self.known)
        except EnvelopeError as e:                     # a bad handoff never reaches the consumer
            self.w.stats["rejected"] += 1
            self.rejections.append({"t": round(self.sim.now, 2), "to": dest, "code": e.code, "errors": e.errors,
                                    "task": wire.get("task_id") if isinstance(wire, dict) else None})
            self.w.store.audit("system", "handoff_rejected", dest, {"code": e.code, "errors": e.errors})
            if token:
                self.log.ack(*token)
            return
        if env.kind == "event" and env.step not in self.subs.get(dest, set()):
            self.log.ack(*token)                        # not for this group
            return
        self.c[dest].inbox.append((env, token, attempt))
        self._pump(dest)

    def _pump(self, name: str) -> None:
        c = self.c[name]
        while c.inbox and c.busy < c.concurrency and not self.dead(name):
            env, token, attempt = c.inbox.popleft()
            c.busy += 1
            res = c.handler(env)
            self.sim.at(res.service, self._finish, name, env, token, attempt, res)

    def _finish(self, name: str, env: Envelope, token, attempt: int, res: Result) -> None:
        c = self.c[name]
        c.busy -= 1
        if self.dead(name):
            return                                      # it died mid-job: no result, no ack
        for out in res.out:
            self.send(out)
        if token:
            if res.ok:
                self.log.ack(*token)
            elif attempt <= self.w.settings.max_retries:     # not acked: the stream delivers it again later
                self.w.stats["redeliveries"] += 1
                self.sim.at(self.w.settings.redeliver_after, self._arrive, env.to_wire(), name, token, attempt + 1)
            else:
                self.w.stats["dead_letters"] += 1
                self.log.ack(*token)
        self._pump(name)
