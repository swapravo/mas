"""Orchestrators: one small engine, four roles.

An orchestrator runs a PLAN: a list of stages. A stage sends one or more requests (in parallel) and waits for all
the results, or it is the word "decide" (arbitrate the votes, in code). State lives in the database (role `orch`),
not in memory, so what an orchestrator knows survives a restart.

  Central      one plan:  intake -> 3 votes -> decide -> act -> reply
  Top          the same plan, but each stage is handed to a DOMAIN orchestrator instead of a department
  Customer     domain orchestrator for Collections + Ops
  Risk         domain orchestrator for Finance + Legal

Rules enforced here, in code:
  timeout   a step that does not answer within `step_timeout` is retried (same idempotency key)...
  retries   ...up to `max_retries` times, then the orchestrator ESCALATES (a person is told) and stops the case.
            Finance and Legal own non-negotiable domains, so we never carry on without them
            (orchestrator_prompt.md: "retry, then escalate if the missing agent owns a non-negotiable domain").
  hops/deadline/budget  checked on every handoff (envelope.parse) and in every department (depts/base.py).
"""
from __future__ import annotations

from typing import Callable

from enterprise.arbitration import decide
from enterprise.envelope import DEPT_NAMES, Envelope
from enterprise.sim import Result

SERVICE = 0.01                    # an orchestrator is code, not a model: it is fast
DEPTS3 = ("ops", "finance", "legal")


def ids(t: dict) -> dict:
    return {"case_id": t["case"]["case_id"], "resident_id": t["case"]["resident_id"]}


def outcome_payload(t: dict) -> dict:
    d = t["decision"]
    return {**ids(t), "outcome": d["outcome"], "amount": d["fee"], "approved_by": d.get("approved_by", "")}


class Orchestrator:
    name = ""
    plans: dict[str, list] = {}
    step_plan: dict[str, str] = {}          # which plan a returning result belongs to
    parent: str | None = None               # domain orchestrators answer their parent; others finish the case

    def __init__(self, sw) -> None:
        self.sw, self.w = sw, sw.w
        self.out: list[Envelope] = []

    # -- state (database) ----------------------------------------------------
    def _key(self, task_id: str, plan: str) -> str:
        return f"{task_id}:{self.name}:{plan}"

    def load(self, key: str) -> dict | None:
        return self.w.store.get("orch", "task", key)

    def save(self, task: dict) -> None:
        self.w.store.put("orch", "task", task["key"], task)

    def dead(self) -> bool:
        return self.sw.net.dead(self.name)

    # -- consumer entry point ------------------------------------------------
    def handle(self, env: Envelope) -> Result:
        self.out = []
        (self.on_request if env.kind == "request" else self.on_result)(env)
        return Result(SERVICE, self.out)

    def on_request(self, env: Envelope) -> None:
        if env.step not in self.plans:
            self.w.store.audit("system", "unknown_request", self.name, {"step": env.step})
            return
        key = self._key(env.task_id, env.step)
        if self.load(key):                                    # a retry or a duplicate of work we already hold
            self.w.stats["dup_requests_ignored"] += 1
            return
        task = {"key": key, "plan": env.step, "trigger": env.to_wire(), "case": dict(env.payload), "stage": 0,
                "steps": {}, "results": {}, "status": "running", "decision": None}
        self.advance(task, env)

    def on_result(self, env: Envelope) -> None:
        plan = self.step_plan.get(env.step)
        task = self.load(self._key(env.task_id, plan)) if plan else None
        if task is None or task["status"] not in ("running", "awaiting_human"):
            return
        if env.step == "human.decision":                      # a person answered a decision card
            task["decision"].update(env.payload["result"])
            task.update(status="running", stage=task["stage"] + 1)
            return self.advance(task, env)
        st = task["steps"].get(env.step)
        if st is None or st["state"] == "done":
            self.w.stats["dup_results_ignored"] += 1
            return
        res = env.payload.get("result") or {}
        if not env.payload.get("ok") or "error" in res:
            return self.retry_or_fail(task, env.step, env.payload.get("error") or res.get("error", "error"), env)
        st["state"] = "done"
        if env.from_dept in ("customer", "risk"):             # a domain orchestrator hands back its steps' results
            task["results"].update(res)
        else:
            task["results"][env.step] = {**res, "_t": self.sw.sim.now, "_seq": self.sw.next_seq()}
        self.after_record(task, env.step)
        self.advance(task, env)

    def after_record(self, task: dict, step: str) -> None:
        pass

    # -- the plan ---------------------------------------------------------------
    def advance(self, task: dict, env: Envelope) -> None:
        stages = self.plans[task["plan"]]
        while True:
            if task["stage"] >= len(stages):
                return self.finish(task, env)
            stage = stages[task["stage"]]
            if stage == "decide":
                if not self.decide(task):
                    task["status"] = "awaiting_human"
                    return self.save(task)
                task["stage"] += 1
                continue
            steps = stage(task)
            for step, to, payload in steps:
                if step not in task["steps"]:
                    self.dispatch(task, env, step, to, payload)
            if all(task["steps"][s]["state"] == "done" for s, _, _ in steps):
                task["stage"] += 1
                continue
            return self.save(task)

    def dispatch(self, task: dict, env: Envelope, step: str, to: str, payload: dict) -> None:
        out = env.forward(frm=self.name, to=to, step=step, payload=payload, data_class="internal",
                          approval_required=(step.split(".")[0] == "reply"))
        task["steps"][step] = {"state": "sent", "attempts": 1, "wire": out.to_wire()}
        self.out.append(out)
        self.w.stats["routing"] += 1                          # a coordinator chose where this goes
        self.sw.sim.at(self._timeout_for(to), self.on_timeout, task["key"], step, 1)

    def _timeout_for(self, to: str) -> float:
        s = self.w.settings
        return s.step_timeout if to in DEPT_NAMES else s.step_timeout * (s.max_retries + 1) * 2 + 10

    # -- failure handling: retry, then escalate ----------------------------------
    def on_timeout(self, key: str, step: str, attempt: int) -> None:
        if self.dead():                                       # timers die with the process
            return
        task = self.load(key)
        if task is None or task["status"] != "running":       # the case already failed or finished: stale timer
            return
        st = task["steps"].get(step)
        if st and st["state"] == "sent" and st["attempts"] == attempt:
            self.retry_or_fail(task, step, f"no answer within {self._timeout_for(st['wire']['to_dept']):.0f}s", None)

    def retry_or_fail(self, task: dict, step: str, why: str, env: Envelope | None) -> None:
        st, s = task["steps"][step], self.w.settings
        to = st["wire"]["to_dept"]
        if to in DEPT_NAMES and st["attempts"] <= s.max_retries and "budget" not in why:
            st["attempts"] += 1
            self.w.stats["retries"] += 1
            self.sw.net.send(Envelope(**st["wire"]))          # same envelope, same idempotency key
            self.sw.sim.at(self._timeout_for(to), self.on_timeout, task["key"], step, st["attempts"])
            return self.save(task)
        self.fail(task, step, why, env)

    def fail(self, task: dict, step: str, why: str, env: Envelope | None) -> None:
        case_id = task["case"]["case_id"]
        task["status"] = "failed"
        self.save(task)
        self.sw.registry.notice(case_id, self.name, f"{step}: {why}")
        self.w.stats["escalations"] += 1
        if self.parent:                                       # tell the parent; it escalates
            base = env or Envelope(**task["trigger"])
            self.sw.net.send(base.forward(frm=self.name, to=Envelope(**task["trigger"]).from_dept, step=task["plan"],
                                          kind="result", payload={"ok": False, "error": f"{step}: {why}"}))
        else:
            self.sw.registry.finish(case_id, "escalated_failure", reason=f"{step}: {why}")
            self.w.store.put("orch", "incident", case_id, {"case_id": case_id, "step": step, "why": why, "by": self.name})

    # -- finishing ------------------------------------------------------------------
    def finish(self, task: dict, env: Envelope) -> None:
        task["status"] = "done"
        self.save(task)
        if self.parent:
            self.out.append(env.forward(frm=self.name, to=Envelope(**task["trigger"]).from_dept, step=task["plan"], kind="result",
                                        payload={"ok": True, "result": {k: v for k, v in task["results"].items()}}))
        else:
            self.sw.case_done(task)

    # -- deciding (central and top only) ------------------------------------------
    def votes(self, task: dict) -> list[dict]:
        return [{"dept": s.split(".")[1], "t": r["_t"], "seq": r["_seq"], **r}
                for s, r in task["results"].items() if s.startswith("vote.") and "vote" in r]

    def decide(self, task: dict) -> bool:
        s, votes = self.w.settings, self.votes(task)
        fee = next((v["amount"] for v in votes if v["dept"] == "finance"), 0)
        dec = decide(s.strategy, votes, fee=fee, limit=s.policy.fee_limit, case=ids(task), now=self.sw.sim.now,
                     decide_by=s.decide_by)
        task["decision"] = {**dec.to_dict(), "fee": fee}
        self.sw.registry.update(ids(task)["case_id"], outcome=dec.outcome, decided_by=dec.decided_by, conflict=dec.conflict)
        if dec.outcome == "escalate":
            return self.sw.escalate(self, task, dec)          # False: wait for a person
        return True


# -- the four roles ---------------------------------------------------------------------

def _act(t: dict, to_waive: tuple[str, str], to_refuse: tuple[str, str]) -> list:
    p = outcome_payload(t)
    step, dept = to_waive if p["outcome"] == "waive" else to_refuse
    return [(step, dept, p)]


class _Decider(Orchestrator):
    """Shared by Central and Top: a waiver that Finance's own code refuses (above its limit, nobody approved)
    becomes a refusal. This is what saves last-writer-wins from itself."""

    def after_record(self, task: dict, step: str) -> None:
        r = task["results"].get(step, {})
        if step == "act.waive" and r.get("applied") is False:
            task["decision"].update(outcome="refuse", blocked_by_policy=True)
            self.w.stats["policy_blocked"] += 1
            self.sw.registry.update(task["case"]["case_id"], outcome="refuse", policy_blocked=True)


class Central(_Decider):
    name = "orchestrator"
    plans = {"start": [
        lambda t: [("intake", "collections", ids(t))],
        lambda t: [(f"vote.{d}", d, ids(t)) for d in DEPTS3],
        "decide",
        lambda t: _act(t, ("act.waive", "finance"), ("act.refuse", "legal")),
        lambda t: [("reply", "collections", outcome_payload(t))],
    ]}
    step_plan = {s: "start" for s in ("intake", "vote.ops", "vote.finance", "vote.legal", "act.waive", "act.refuse",
                                      "reply", "human.decision")}


class Top(_Decider):
    name = "top"
    plans = {"start": [
        lambda t: [("customer.gather", "customer", ids(t))],
        lambda t: [("risk.gather", "risk", ids(t))],
        "decide",
        lambda t: [("risk.act", "risk", outcome_payload(t))],
        lambda t: [("customer.reply", "customer", outcome_payload(t))],
    ]}
    step_plan = {s: "start" for s in ("customer.gather", "risk.gather", "risk.act", "customer.reply", "human.decision")}

    def after_record(self, task: dict, step: str) -> None:
        if step == "risk.act":                                # act.* results arrive flattened inside it
            super().after_record(task, "act.waive")


class Customer(Orchestrator):
    name, parent = "customer", "top"
    plans = {
        "customer.gather": [lambda t: [("intake", "collections", t["case"])], lambda t: [("vote.ops", "ops", t["case"])]],
        "customer.reply": [lambda t: [("reply", "collections", t["case"])]],
    }
    step_plan = {"intake": "customer.gather", "vote.ops": "customer.gather", "reply": "customer.reply"}


class Risk(Orchestrator):
    name, parent = "risk", "top"
    plans = {
        "risk.gather": [lambda t: [(f"vote.{d}", d, t["case"]) for d in ("finance", "legal")]],
        "risk.act": [lambda t: [("act.waive", "finance", t["case"])] if t["case"]["outcome"] == "waive"
                     else [("act.refuse", "legal", t["case"])]],
    }
    step_plan = {"vote.finance": "risk.gather", "vote.legal": "risk.gather", "act.waive": "risk.act", "act.refuse": "risk.act"}
