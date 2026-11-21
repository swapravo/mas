"""Simulation: builds one run (world + clock + network + the chosen mode + the workload), runs it, summarises it.

    sim = Simulation(Settings(mode="central", strategy="rules", cases=40))
    result = sim.run()           # a dict: see metrics.summarize
"""
from __future__ import annotations

from enterprise.approvals import ApproverPool, Item
from enterprise.arbitration import ESCALATION_PATH
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
        self.approvals = ApproverPool(self.sim, s)
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
        """The decision is applied and the reply is drafted. The draft still needs a person (draft, then approve)."""
        self.registry.finish(case_id, "completed", outcome=decision["outcome"], decided_by=decision["decided_by"],
                             conflict=decision["conflict"], reply_status="pending_approval")
        conflict, large = decision["conflict"], decision["fee"] > self.settings.policy.fee_limit
        self.approvals.submit(Item("reply", case_id, "high" if conflict else "low", (decision["outcome"], conflict, large),
                                   lambda who: self._reply_approved(case_id, who)))

    def _reply_approved(self, case_id: str, who: str) -> None:
        draft = self.w.store.get("owner", "draft", case_id, dept="collections")
        if draft:
            self.w.store.put("owner", "draft", case_id, {**draft, "status": "approved", "approved_by": who}, dept="collections")
        self.registry.annotate(case_id, reply_status="approved", reply_approved_at=self.sim.now, reply_approved_by=who)

    def case_done(self, task: dict) -> None:
        self.complete(task["case"]["case_id"], task["decision"])

    def escalate(self, who, task: dict, dec) -> bool:
        """A conflict the rules cannot settle. Store the decision card, show it to a person, and wait.

        Returns False (the orchestrator pauses the case). When the person answers, the decision goes back the way
        the votes came: as a message to the orchestrator, or as an event on the stream.
        """
        card, case_id = dec.card, task["case"]["case_id"]
        self.w.store.put("orch", "decision_card", case_id, card)
        self.cards.append(card)
        self.registry.update(case_id, status="awaiting_human")
        self.approvals.submit(Item("card", case_id, "high", ("card", card["recommendation"]["option"]),
                                   lambda approver: self._card_decided(who, task, card, approver)))
        return False

    def _card_decided(self, who, task: dict, card: dict, approver: str) -> None:
        """The person accepts the recommendation (a simulated person always does: that is what a rushed one does)."""
        case_id, choice = card["case"]["case_id"], card["recommendation"]["option"]
        if self.sim.now > card["decide_by"]:
            self.w.stats["cards_late"] += 1
        result = {"outcome": choice, "decided_by": "human", "approved_by": f"human:{ESCALATION_PATH[0]}",
                  "why": "a person accepted the recommendation on the decision card"}
        self.registry.update(case_id, status="open", outcome=choice, decided_by="human")
        if self.settings.mode == "choreography":
            env = Envelope(**task["env"])
            self.decisions[case_id] = {**task["decision"], **result}
            self.net.send(env.forward(frm="approvals", to="*", step="case.decided", kind="event", payload={
                "case_id": case_id, "resident_id": card["case"]["resident_id"], "outcome": choice,
                "amount": card["case"]["fee_rupees"], "approved_by": result["approved_by"]}))
        else:
            trigger = Envelope(**task["trigger"])
            self.net.send(trigger.forward(frm="approvals", to=who.name, step="human.decision", kind="result",
                                          payload={"ok": True, "result": result}))

    # -- run ---------------------------------------------------------------------------
    def run(self) -> dict:
        ch = self.w.chaos
        for c in self.cases:
            self.sim.at(c.arrival, self.start_case, c)
        self.sim.run(self.settings.horizon)
        return summarize(self)
