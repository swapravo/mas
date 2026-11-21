"""Simulation: builds one run (world + clock + network + the chosen mode + the workload), runs it, summarises it.

    sim = Simulation(Settings(mode="central", strategy="rules", cases=40))
    result = sim.run()           # a dict: see metrics.summarize
"""
from __future__ import annotations

from enterprise.choreography import SUBS, Choreography
from enterprise.envelope import DEPT_NAMES, Envelope
from enterprise.metrics import summarize
from enterprise.orchestrators import Central, Customer, Risk, Top
from enterprise.registry import Registry
from enterprise.settings import Chaos, Defences, Settings
from enterprise.sim import Net, Result, Sim
from enterprise.workload import make_cases, seed_store
from enterprise.world import World


class Simulation:
    def __init__(self, settings: Settings | None = None, chaos: Chaos | None = None, defences: Defences | None = None,
                 store=None, kv=None, log=None, cases=None) -> None:
        self.settings = s = settings or Settings()
        self.w = World(s, chaos, defences, store, kv)
        self.sim = Sim()
        self.w.clock = lambda: self.sim.now
        self.registry = Registry(self.w.store, lambda: self.sim.now)
        self.net = Net(self.sim, self.w, self.registry.known, log)
        self.cases = cases if cases is not None else make_cases(s.cases, s.seed, attack_every=s.attack_every, gap=s.arrival_gap)
        seed_store(self.w.store, self.cases)
        self.decisions: dict[str, dict] = {}      # case_id -> the decision taken (choreography keeps it here)
        self.cards: list[dict] = []
        self.approvals = None                      # the approver pool (approvals.py)
        self._seq = 0
        self.orchestrators = []
        self._build_mode()

    def next_seq(self) -> int:
        self._seq += 1
        return self._seq

    # -- wiring -------------------------------------------------------------------
    def _build_mode(self) -> None:
        s = self.settings
        if s.mode == "central":
            self.orchestrators = [Central(self)]
        elif s.mode == "hierarchical":
            self.orchestrators = [Top(self), Customer(self), Risk(self)]
        elif s.mode == "choreography":
            self.choreo = Choreography(self)
        else:
            raise ValueError(f"unknown mode {s.mode!r}")
        for o in self.orchestrators:
            self.net.add(o.name, o.handle, concurrency=1)
        for d in DEPT_NAMES:
            if s.mode == "choreography":
                self.net.add(d, self.choreo.handler(d), s.concurrency, subscribes=SUBS[d])
            else:
                self.net.add(d, self._dept_handler(d), s.concurrency)

    def _dept_handler(self, dept: str):
        def handle(env: Envelope) -> Result:
            r = self.w.depts[dept].handle(env)
            out = env.forward(frm=dept, to=env.from_dept, step=env.step, kind="result",
                              payload={"ok": r.ok, "result": r.payload, "error": r.error, "calls": r.calls})
            return Result(r.service, [out], ok=r.ok)
        return handle

    # -- the driver: disputes arrive ------------------------------------------------
    def start_case(self, c) -> None:
        s, now = self.settings, self.sim.now
        deadline = now + s.case_deadline
        self.registry.start(c, deadline)
        entry = {"central": "orchestrator", "hierarchical": "top", "choreography": "*"}[s.mode]
        self.net.send(Envelope(
            task_id=c.case_id, goal=c.goal, from_dept="api", to_dept=entry, on_behalf_of=c.principal, data_class="internal",
            deadline=deadline, schema_version=2, budget=s.budget, hop_count=0, approval_required=False,
            step="case.opened" if s.mode == "choreography" else "start",
            kind="event" if s.mode == "choreography" else "request",
            payload={"case_id": c.case_id, "resident_id": c.resident_id}))

    # -- finishing a case (called by whoever finishes it) -----------------------------
    def complete(self, case_id: str, decision: dict) -> None:
        self.registry.finish(case_id, "completed", outcome=decision["outcome"], decided_by=decision["decided_by"],
                             conflict=decision["conflict"])

    def case_done(self, task: dict) -> None:
        self.complete(task["case"]["case_id"], task["decision"])

    def escalate(self, who, task: dict, dec) -> bool:
        raise NotImplementedError("decision cards arrive with the approval queue")

    # -- run ---------------------------------------------------------------------------
    def run(self) -> dict:
        ch = self.w.chaos
        for c in self.cases:
            self.sim.at(c.arrival, self.start_case, c)
        self.sim.run(self.settings.horizon)
        return summarize(self)
