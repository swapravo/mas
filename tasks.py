"""Celery tasks: find cases that are due, run the agent on each, release overnight drafts.

    beat -> scan_and_enqueue -> run_case (one per case) -> agent -> drafts for a person to approve
    beat -> release_held_drafts -> sends approved drafts that were held for quiet hours

Rules
- Nothing runs outside 09:00 to 20:59 India time. Beat does not schedule outside it, tasks refuse
  to start outside it, and queued tasks expire before the next scan.
- One agent run per case at a time (Redis lock). Tools are idempotent, so a retry cannot double-send.
- The scanner uses a read-only database role; each run uses the agent role scoped to one property.
"""
from __future__ import annotations

import datetime as dt
import uuid

import redis

import config
import database as db
import service
from celery_app import app
from harness import LLMUnavailable
from tools import TZ, in_window

OUTSIDE = {"status": "skipped", "reason": "outside 09:00 to 20:59"}
_redis = None


def _now() -> dt.datetime:
    """The real clock. Tests replace this."""
    return dt.datetime.now(TZ)


def _client() -> redis.Redis:
    global _redis
    _redis = _redis or redis.Redis.from_url(config.REDIS_URL)
    return _redis


@app.task(name="scan_and_enqueue")
def scan_and_enqueue() -> dict:
    """Find active cases that are due and queue one run_case task for each."""
    now = _now()
    if not in_window(now):
        return OUTSIDE
    with db.session("worker") as c:
        rows = db.list_cases_to_work(c, now=now, limit=config.MAX_CASES_PER_SCAN)
    for r in rows:
        run_case.apply_async(args=[str(r["case_id"]), str(r["property_id"])],
                             expires=config.SCAN_EVERY_MINUTES * 60 - 60)
    return {"status": "ok", "enqueued": len(rows)}


@app.task(bind=True, name="run_case", autoretry_for=(LLMUnavailable,), retry_backoff=30,
          retry_backoff_max=600, retry_jitter=True, max_retries=3)
def run_case(self, case_id: str, property_id: str) -> dict:
    """Run the agent once for one case. Retries if no free model answers."""
    now = _now()
    if not in_window(now):
        return OUTSIDE
    lock = _client().lock(f"case-lock:{case_id}", timeout=config.CASE_LOCK_SECONDS, blocking=False)
    if not lock.acquire():
        return {"status": "skipped", "reason": "already running"}
    try:
        res = service.run_case(uuid.UUID(case_id), uuid.UUID(property_id), now)
    finally:
        try:
            lock.release()
        except redis.exceptions.LockError:       # the run outlived the lock timeout
            pass
    return {"status": res["status"], "model": res.get("model"),
            "tools": [s["tool"] for s in res["steps"] if s["type"] == "tool_result"]}


@app.task(name="release_held_drafts")
def release_held_drafts() -> dict:
    """Send approved drafts that were held overnight, once their time has come."""
    now = _now()
    if not in_window(now):
        return OUTSIDE
    with db.session("worker") as c:
        rows = db.list_due_held_drafts(c, now=now)
    sent = sum(service.send_draft(r["draft_id"], r["property_id"], now) == "sent" for r in rows)
    return {"status": "ok", "due": len(rows), "sent": sent}
