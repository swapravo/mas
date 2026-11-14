"""Celery pipeline tests: the 09:00 to 20:59 window, scanning, locking, releasing held drafts.

Needs Postgres (schema.sql applied) and Redis (docker compose up). Tasks run eagerly, in process,
with the agent itself replaced by a fake, so no model is called.
"""
import datetime as dt
import os
import sys
import uuid
from zoneinfo import ZoneInfo

import pytest
import redis

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import config  # noqa: E402
import database as db  # noqa: E402
import service  # noqa: E402
import tasks  # noqa: E402
from celery_app import app  # noqa: E402
from tools import in_window  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")
def at(h, m=0, day=14): return dt.datetime(2026, 11, day, h, m, tzinfo=IST)


# ---- the window (no services needed) -----------------------------------------

@pytest.mark.parametrize("h,m,ok", [(8, 59, False), (9, 0, True), (12, 0, True), (20, 59, True), (21, 0, False), (23, 59, False), (0, 0, False)])
def test_window_edges(h, m, ok):
    assert in_window(at(h, m)) is ok


def test_in_window_uses_india_time_whatever_the_input_zone():
    utc = dt.datetime(2026, 11, 14, 3, 29, tzinfo=dt.timezone.utc)      # 08:59 IST
    assert in_window(utc) is False and in_window(utc + dt.timedelta(minutes=1)) is True


def test_beat_only_schedules_inside_the_window():
    for name, entry in app.conf.beat_schedule.items():
        assert entry["schedule"].hour == set(range(9, 21)), name       # 9 to 20 inclusive: last firing 20:xx
    assert app.conf.timezone == "Asia/Kolkata" and app.conf.enable_utc is False


@pytest.mark.parametrize("h", [3, 8, 21, 23])
def test_tasks_refuse_outside_the_window(monkeypatch, h):
    monkeypatch.setattr(tasks, "_now", lambda: at(h, 30))
    monkeypatch.setattr(service, "run_case", lambda *a, **k: pytest.fail("agent must not run"))
    assert tasks.scan_and_enqueue.run()["status"] == "skipped"
    assert tasks.run_case.run(str(uuid.uuid4()), str(uuid.uuid4()))["status"] == "skipped"
    assert tasks.release_held_drafts.run()["status"] == "skipped"


# ---- with Postgres and Redis ---------------------------------------------------

@pytest.fixture(scope="module", autouse=True)
def _services():
    try:
        with db.session("owner") as c:
            c.execute("SELECT 1")
        redis.Redis.from_url(config.REDIS_URL).ping()
    except Exception as exc:
        pytest.skip(f"database or redis not available: {exc}")
    app.conf.task_always_eager = True
    app.conf.task_eager_propagates = True
    yield
    app.conf.task_always_eager = False


@pytest.fixture
def world():
    ids = {}
    with db.session("owner") as c:
        for tag in ("a", "b"):
            o = db.create_owner(c, name="T", contact="x", role="property_manager")
            p = db.create_property(c, name="T", region="N", owner_id=o["id"])
            r = db.create_resident(c, property_id=p["id"], display_name="T-res", phone="x")
            f = db.create_fee_charge(c, resident_id=r["id"], cycle=dt.date(2026, 11, 1), amount=100, due_date=dt.date(2026, 11, 1), status="overdue")
            k = db.create_case_file(c, fee_charge_id=f["id"], next_action_at=at(8, 0))
            ids[tag] = dict(o=o["id"], p=p["id"], r=r["id"], f=f["id"], c=k["id"])
    yield ids
    with db.session("owner") as c:
        for v in ids.values():
            for q in ("DELETE FROM message_draft WHERE case_id=%(c)s", "DELETE FROM agent_run WHERE case_id=%(c)s",
                      "DELETE FROM case_file WHERE id=%(c)s", "DELETE FROM chat_message WHERE resident_id=%(r)s",
                      "DELETE FROM fee_charge WHERE id=%(f)s", "DELETE FROM resident WHERE id=%(r)s",
                      "DELETE FROM property WHERE id=%(p)s", "DELETE FROM owner WHERE id=%(o)s"):
                c.execute(q, v)


@pytest.fixture
def fake_agent(monkeypatch):
    calls = []
    def fake(case_id, property_id, now, clock_fn=None):
        calls.append((case_id, property_id))
        return {"status": "done", "model": "fake", "steps": [{"type": "tool_result", "tool": "wait"}]}
    monkeypatch.setattr(service, "run_case", fake)
    return calls


def test_scan_enqueues_only_due_cases_across_properties(monkeypatch, world, fake_agent):
    monkeypatch.setattr(tasks, "_now", lambda: at(10, 0))
    with db.session("owner") as c:      # make property b's case not due yet
        db.update_case_file(c, world["b"]["c"], next_action_at=at(15, 0))
    out = tasks.scan_and_enqueue.run()
    mine = [x for x in fake_agent if x[0] in (world["a"]["c"], world["b"]["c"])]
    assert out["status"] == "ok" and mine == [(world["a"]["c"], world["a"]["p"])]


def test_scan_skips_paid_and_closed_cases(monkeypatch, world, fake_agent):
    monkeypatch.setattr(tasks, "_now", lambda: at(10, 0))
    with db.session("owner") as c:
        db.update_fee_charge(c, world["a"]["f"], status="paid")
        db.update_case_file(c, world["b"]["c"], status="closed")
    tasks.scan_and_enqueue.run()
    assert not [x for x in fake_agent if x[0] in (world["a"]["c"], world["b"]["c"])]


def test_run_case_task_holds_a_redis_lock(monkeypatch, world, fake_agent):
    monkeypatch.setattr(tasks, "_now", lambda: at(10, 0))
    cid = str(world["a"]["c"])
    held = redis.Redis.from_url(config.REDIS_URL).lock(f"case-lock:{cid}", timeout=30, blocking=False)
    assert held.acquire()
    try:
        assert tasks.run_case.run(cid, str(world["a"]["p"]))["reason"] == "already running"
        assert fake_agent == []
    finally:
        held.release()
    assert tasks.run_case.run(cid, str(world["a"]["p"]))["status"] == "done"
    assert tasks.run_case.run(cid, str(world["a"]["p"]))["status"] == "done"     # lock was released after the run


def test_worker_role_is_read_only_but_sees_every_property(world):
    with db.session("worker") as c:
        seen = {r["case_id"] for r in db.list_cases_to_work(c, now=at(23, 0), limit=500)}
        assert {world["a"]["c"], world["b"]["c"]} <= seen
        import psycopg
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("UPDATE case_file SET status = 'closed'")
    with pytest.raises(Exception):
        with db.session("worker") as c:
            c.execute("SELECT * FROM chat_message")      # no access to message text


def test_held_drafts_are_released_only_inside_the_window(monkeypatch, world):
    cid = world["a"]["c"]
    with db.session("owner") as c:
        d = db.create_message_draft(c, case_id=cid, body="Hi, a quick reminder about your fee.", ladder_step=1,
                                    idempotency_key="k-" + uuid.uuid4().hex, send_after=at(9, 0, day=15), status="held")
    monkeypatch.setattr(tasks, "_now", lambda: at(9, 0, day=15) - dt.timedelta(minutes=1))     # 08:59
    assert tasks.release_held_drafts.run()["status"] == "skipped"
    monkeypatch.setattr(tasks, "_now", lambda: at(23, 0, day=15))
    assert tasks.release_held_drafts.run()["status"] == "skipped"
    with db.session("owner") as c:
        assert db.get_message_draft(c, d["id"])["status"] == "held"
    monkeypatch.setattr(tasks, "_now", lambda: at(9, 5, day=15))
    out = tasks.release_held_drafts.run()
    assert out["status"] == "ok" and out["sent"] >= 1
    with db.session("owner") as c:
        assert db.get_message_draft(c, d["id"])["status"] == "sent"
        case = db.get_case_file(c, cid)
        assert case["ladder_step"] == 1 and case["messages_sent"] == 1
        assert case["next_action_at"].astimezone(IST) == at(9, 5, day=15) + dt.timedelta(days=3)   # step 2 due in 3 days
        assert [m["body"] for m in db.list_chat_messages(c, resident_id=world["a"]["r"])] == ["Hi, a quick reminder about your fee."]
    assert tasks.release_held_drafts.run()["sent"] == 0       # nothing sent twice
