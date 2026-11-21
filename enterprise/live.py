"""A live demo of the central pattern on REAL queues: Celery workers (one per department), Redis, Postgres.

    python -m enterprise.live --cases 3 --spawn-workers
    python -m enterprise.live --cases 3 --spawn-workers --kill finance      # no Finance worker: watch the timeout, retry, escalate

This is deliberately small: a blocking central orchestrator (intake, three votes in parallel, decide, act, reply) with the
same arbitration, the same envelope and the same departments as the simulator. The simulator is where the three modes,
chaos and approvals live (it is deterministic); this shows the same handlers behind a real broker, with real clock time.

Needs: Redis on REDIS_URL, Postgres with enterprise/schema.sql applied and ENT_PG_DSN set.
"""
from __future__ import annotations

import argparse
import gc
import json
import os
import subprocess
import sys
import time

from celery.exceptions import TimeoutError as CeleryTimeout

from enterprise.arbitration import decide
from enterprise.celery_app import QUEUES, TASKS, app
from enterprise.envelope import DEPT_NAMES, Envelope
from enterprise.settings import Policy
from enterprise.workload import Case, make_cases, seed_store

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def spawn_workers(skip: str | None) -> list[subprocess.Popen]:
    procs = []
    for d in DEPT_NAMES:
        if d == skip:
            continue
        procs.append(subprocess.Popen(
            [sys.executable, "-m", "celery", "-A", "enterprise.celery_app", "worker", "-Q", QUEUES[d], "-n", f"{d}@%h",
             "--pool", "solo", "--loglevel", "WARNING"], cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env={**os.environ, "PYTHONPATH": ROOT}))
    deadline = time.time() + 30
    want = len(procs)
    while time.time() < deadline:                                   # wait until every worker answers a ping
        if len(app.control.ping(timeout=1.0) or []) >= want:
            return procs
    stop(procs)
    raise SystemExit("workers did not start in 30s")


def stop(procs: list[subprocess.Popen]) -> None:
    for p in procs:
        p.terminate()
    for p in procs:
        p.wait(timeout=10)


def wait(result, timeout: float) -> dict:
    """Poll for a result instead of result.get(): get() subscribes to Redis pub/sub, and a subscription left behind by a
    timed-out wait makes Celery print a traceback at interpreter exit."""
    end = time.time() + timeout
    while time.time() < end:
        if result.ready():
            return result.get()
        time.sleep(0.02)
    raise CeleryTimeout(f"no result in {timeout}s")


class Live:
    def __init__(self, timeout: float, retries: int, strategy: str) -> None:
        self.timeout, self.retries, self.strategy, self.policy = timeout, retries, strategy, Policy()
        self.log: list[str] = []

    def envelope(self, c: Case, dept: str, step: str, payload: dict, hop: int, approval=False) -> dict:
        return Envelope(task_id=c.case_id, goal=c.goal, from_dept="orchestrator", to_dept=dept, on_behalf_of=c.principal,
                        data_class="internal", deadline=time.time() + 600, schema_version=2, budget=40, hop_count=hop,
                        approval_required=approval, step=step, payload=payload).to_wire()

    def ask(self, pending: dict) -> dict:
        """Send, wait, and on a timeout resend the SAME envelope (same idempotency key) up to `retries` times."""
        results, sent = {}, {}
        for key, (dept, wire) in pending.items():
            sent[key] = (dept, wire, [TASKS[dept].apply_async(args=[wire], queue=QUEUES[dept])])
        for key, (dept, wire, asyncs) in sent.items():
            for attempt in range(self.retries + 1):
                try:
                    results[key] = wait(asyncs[-1], self.timeout)
                    break
                except CeleryTimeout:
                    if attempt < self.retries:
                        self.log.append(f"    {dept} did not answer in {self.timeout:.0f}s: retry {attempt + 1}/{self.retries} (same key)")
                        asyncs.append(TASKS[dept].apply_async(args=[wire], queue=QUEUES[dept]))
            else:
                results[key] = {"ok": False, "error": f"{dept} did not answer after {self.retries} retries"}
        sent.clear()                     # drop the AsyncResults now, not at interpreter exit (their __del__ talks to Redis)
        return results

    def case(self, c: Case) -> dict:
        ids = {"case_id": c.case_id, "resident_id": c.resident_id}
        r = self.ask({"intake": ("collections", self.envelope(c, "collections", "intake", ids, 1))})["intake"]
        if not r["ok"]:
            return {"case": c.case_id, "status": "escalated_failure", "why": r["error"]}
        votes = self.ask({f"vote.{d}": (d, self.envelope(c, d, f"vote.{d}", ids, 3)) for d in ("ops", "finance", "legal")})
        failed = [k for k, v in votes.items() if not v["ok"] or "error" in v["result"]]
        if failed:
            return {"case": c.case_id, "status": "escalated_failure", "why": f"{failed[0]}: {votes[failed[0]].get('error') or votes[failed[0]]['result']}"}
        vs = [{"dept": k.split(".")[1], "t": 0.0, "seq": i, **v["result"]} for i, (k, v) in enumerate(votes.items())]
        fee = next(v["amount"] for v in vs if v["dept"] == "finance")
        dec = decide(self.strategy, vs, fee=fee, limit=self.policy.fee_limit, case=ids, now=time.time(), decide_by=600)
        outcome, approved, by = dec.outcome, "", dec.decided_by
        if outcome == "escalate":                                      # a person would decide; here they accept the advice
            outcome, approved, by = dec.card["recommendation"]["option"], "human:finance_manager", "human"
            self.log.append("    decision card: " + json.dumps(dec.card))
        step, dept = ("act.waive", "finance") if outcome == "waive" else ("act.refuse", "legal")
        act = self.ask({step: (dept, self.envelope(c, dept, step, {**ids, "amount": fee, "approved_by": approved}, 5))})[step]
        rep = self.ask({"reply": ("collections", self.envelope(c, "collections", "reply", {**ids, "outcome": outcome, "amount": fee}, 7, True))})["reply"]
        return {"case": c.case_id, "status": "completed" if act["ok"] and rep["ok"] else "escalated_failure", "outcome": outcome,
                "decided_by": by, "votes": {v["dept"]: v["vote"] for v in vs}}


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="python -m enterprise.live", description=__doc__.split("\n")[0])
    ap.add_argument("--cases", type=int, default=3)
    ap.add_argument("--strategy", default="rules", choices=("last", "priority", "rules"))
    ap.add_argument("--kill", choices=DEPT_NAMES, help="do not start this department's worker")
    ap.add_argument("--spawn-workers", action="store_true", help="start one Celery worker per department for the demo")
    ap.add_argument("--timeout", type=float, default=8.0, help="seconds to wait for a department before retrying")
    ap.add_argument("--retries", type=int, default=2)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args(argv)

    from enterprise.celery_app import world
    w = world()
    cases = make_cases(args.cases, args.seed)
    w.store.reset()
    seed_store(w.store, cases)
    w.kv.clear()
    procs = spawn_workers(args.kill) if args.spawn_workers else []
    live = Live(args.timeout, args.retries, args.strategy)
    try:
        print(f"live: {args.cases} cases, strategy={args.strategy}, workers: {[d for d in DEPT_NAMES if d != args.kill]}")
        for c in cases:
            t0 = time.time()
            res = live.case(c)
            w.store.put("orch", "live_task", c.case_id, res)           # state in the database
            print(f"  {c.case_id} fee Rs {c.fee:>5} repair {str(c.repair_days):>4} notice {c.notice_live!s:5} -> "
                  f"{res['status']} {res.get('outcome', '')} {res.get('why', '')} ({time.time() - t0:.1f}s)")
            for line in live.log:
                print(line)
            live.log.clear()
        waivers, letters = w.store.scan("owner", "waiver_applied"), w.store.scan("owner", "notice_letter")
        print(f"  effects: {len(waivers)} waivers, {len(letters)} notices; audit rows in Postgres: {len(w.store.audit_rows)}")
    finally:
        stop(procs)
        gc.collect()
        app.close()                    # stop the result-backend thread cleanly (a timed-out .get() leaves it subscribed)


if __name__ == "__main__":
    main()
