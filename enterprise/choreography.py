"""Choreography: no orchestrator. Departments publish events and react to the ones they care about.

    api        -> case.opened
    collections   reacts to case.opened  : read the dispute            -> case.briefed   (carries the resident's text)
    ops/finance/legal react to case.briefed : cast a vote              -> vote.cast
    collections   reacts to vote.cast    : collect votes; at 3, decide -> case.decided   (or a person decides it)
    finance       reacts to case.decided : waive, if the outcome is waive  -> action.done
    legal         reacts to case.decided : serve a notice, if refuse        -> action.done
    collections   reacts to action.done  : draft the reply, close          -> case.closed

Nobody is in charge, but SOMEBODY has to hold the join (who has voted?) and run the arbitration. Here it is Collections,
the owner of the case. That is the lesson: choreography moves the orchestrator's job into one of the services; it does not
remove it. Nobody times anything out either, so when Finance dies, the case just waits (see watchdog.py).

Events carry the brief, including the resident's text, so every department sees it. That is faster (no asking back) and
means an injected instruction reaches every agent. In central mode only Collections ever reads the text.
"""
from __future__ import annotations

from dataclasses import replace

from enterprise.arbitration import decide
from enterprise.envelope import Envelope
from enterprise.sim import Result

SUBS = {
    "collections": {"case.opened", "vote.cast", "action.done"},
    "ops": {"case.briefed"},
    "finance": {"case.briefed", "case.decided"},
    "legal": {"case.briefed", "case.decided"},
}
SERVICE = 0.01


class Choreography:
    def __init__(self, sw) -> None:
        self.sw, self.w = sw, sw.w

    def handler(self, dept: str):
        return lambda env: self.react(dept, env)

    # -- helpers ----------------------------------------------------------------
    def event(self, env: Envelope, frm: str, step: str, payload: dict) -> Envelope:
        return env.forward(frm=frm, to="*", step=step, kind="event", payload=payload, data_class="internal")

    def work(self, dept: str, env: Envelope, step: str, payload: dict):
        """Run one step of a department's agent for an event. The event's hop count rides along."""
        return self.w.depts[dept].handle(replace(env, to_dept=dept, step=step, kind="request", payload=payload,
                                                 approval_required=step == "reply"))

    # -- reactions ---------------------------------------------------------------
    def react(self, dept: str, env: Envelope) -> Result:
        p, t = env.payload, env.step
        ids = {"case_id": p["case_id"], "resident_id": p["resident_id"]}
        if dept == "collections":
            if t == "case.opened":
                r = self.work(dept, env, "intake", ids)
                if not r.ok:
                    return Result(r.service, [], ok=False)
                brief = {**ids, "fee_id": r.payload["fee_id"]}
                if self.w.settings.events_carry_text:             # off = data minimisation on the bus
                    brief["text"] = r.payload["text"]
                return Result(r.service, [self.event(env, dept, "case.briefed", brief)])
            if t == "vote.cast":
                return self.join(env, ids)
            if t == "action.done":
                outcome = p["outcome"]
                if p["step"] == "act.waive" and not p["applied"]:          # Finance's own code refused the waiver
                    outcome = "refuse"
                    self.w.stats["policy_blocked"] += 1
                    self.sw.registry.update(p["case_id"], outcome="refuse", policy_blocked=True)
                r = self.work(dept, env, "reply", {**ids, "outcome": outcome, "amount": p["amount"]})
                if not r.ok:
                    return Result(r.service, [], ok=False)
                self.sw.complete(p["case_id"], {**self.sw.decisions[p["case_id"]], "outcome": outcome})
                return Result(r.service, [self.event(env, dept, "case.closed", {**ids, "outcome": outcome})])
        elif t == "case.briefed":                                              # ops / finance / legal vote
            r = self.work(dept, env, f"vote.{dept}", p)                          # p carries the resident's text
            if not r.ok or "error" in r.payload:
                return Result(r.service, [], ok=False)
            return Result(r.service, [self.event(env, dept, "vote.cast", {**ids, "dept": dept, **r.payload})])
        elif t == "case.decided":
            wants = {"finance": ("waive", "act.waive"), "legal": ("refuse", "act.refuse")}[dept]
            if p["outcome"] != wants[0]:
                return Result(0.0, [])
            r = self.work(dept, env, wants[1], {**ids, "amount": p["amount"], "approved_by": p.get("approved_by", "")})
            if not r.ok:
                return Result(r.service, [], ok=False)
            applied = r.payload.get("applied", r.payload.get("served"))
            return Result(r.service, [self.event(env, dept, "action.done", {**ids, "step": wants[1], "applied": bool(applied),
                                                                            "outcome": p["outcome"], "amount": p["amount"]})])
        return Result(0.0, [])

    def join(self, env: Envelope, ids: dict) -> Result:
        """Collections keeps the tally of votes (in its own rows) and decides when all three are in."""
        s, store, p = self.w.settings, self.w.store, env.payload
        row = store.get("collections", "tally", ids["case_id"]) or {"votes": {}, "decided": False}
        if p["dept"] in row["votes"] or row["decided"]:
            return Result(SERVICE, [])                                          # duplicate vote, or already decided
        row["votes"][p["dept"]] = {"dept": p["dept"], "vote": p["vote"], "reason": p["reason"], "amount": p.get("amount"),
                                   "t": self.sw.sim.now, "seq": self.sw.next_seq()}
        if len(row["votes"]) < 3:
            store.put("collections", "tally", ids["case_id"], row)
            return Result(SERVICE, [])
        votes = list(row["votes"].values())
        fee = next((v["amount"] for v in votes if v["dept"] == "finance"), 0)
        dec = decide(s.strategy, votes, fee=fee, limit=s.policy.fee_limit, case=ids, now=self.sw.sim.now, decide_by=s.decide_by)
        row["decided"] = True
        store.put("collections", "tally", ids["case_id"], row)
        decision = {**dec.to_dict(), "fee": fee}
        self.sw.decisions[ids["case_id"]] = decision
        self.sw.registry.update(ids["case_id"], outcome=dec.outcome, decided_by=dec.decided_by, conflict=dec.conflict)
        if dec.outcome == "escalate":
            self.sw.escalate(self, {"case": ids, "decision": decision, "env": env.to_wire()}, dec)
            return Result(SERVICE, [])
        return Result(SERVICE, [self.event(env, "collections", "case.decided",
                                           {**ids, "outcome": dec.outcome, "amount": fee, "approved_by": ""})])
