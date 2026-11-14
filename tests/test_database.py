"""CRUD tests against a real Postgres with schema.sql applied. Skipped if the database is unreachable."""
import datetime as dt
import os
import sys
import uuid

import psycopg
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import database as db  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _reachable():
    try:
        with db.session("owner") as c:
            c.execute("SELECT 1")
    except Exception as exc:
        pytest.skip(f"database not available: {exc}")


@pytest.fixture
def world():
    """Two properties with one resident each (made as owner, removed afterwards)."""
    made = []
    with db.session("owner") as c:
        for tag in ("a", "b"):
            o = db.create_owner(c, name=f"T-owner-{tag}", contact="xxxx", role="property_manager")
            p = db.create_property(c, name=f"T-prop-{tag}", region="N", owner_id=o["id"])
            r = db.create_resident(c, property_id=p["id"], display_name=f"T-res-{tag}", phone="xxxx")
            made.append((o, p, r))
    yield made
    with db.session("owner") as c:
        for o, p, r in made:
            for t, col, v in [("agent_run", "case_id IN (SELECT id FROM case_file WHERE fee_charge_id IN (SELECT id FROM fee_charge WHERE resident_id = %s))", r["id"]),
                              ("message_draft", "case_id IN (SELECT id FROM case_file WHERE fee_charge_id IN (SELECT id FROM fee_charge WHERE resident_id = %s))", r["id"]),
                              ("case_file", "fee_charge_id IN (SELECT id FROM fee_charge WHERE resident_id = %s)", r["id"]),
                              ("chat_message", "resident_id = %s", r["id"]), ("fee_charge", "resident_id = %s", r["id"]),
                              ("resident", "id = %s", r["id"]), ("property", "id = %s", p["id"]), ("owner", "id = %s", o["id"])]:
                c.execute(f"DELETE FROM {t} WHERE {col}", (v,))


def test_full_lifecycle_through_every_table(world):
    (o, p, r), _ = world
    with db.session("app", p["id"]) as c:
        fee = db.create_fee_charge(c, resident_id=r["id"], cycle=dt.date(2026, 11, 1), amount=850000,
                                   due_date=dt.date(2026, 11, 5), status="overdue")
        assert db.get_fee_charge(c, fee["id"])["amount"] == 850000
        assert [f["id"] for f in db.list_fee_charges(c, status="overdue", due_before=dt.date(2026, 11, 10))] == [fee["id"]]
        assert db.list_fee_charges(c, status="paid") == []
        assert db.update_fee_charge(c, fee["id"], status="paid")["status"] == "paid"

        case = db.create_case_file(c, fee_charge_id=fee["id"])
        assert case["ladder_step"] == 0 and case["status"] == "open"
        case = db.update_case_file(c, case["id"], ladder_step=1, promise_date=dt.date(2026, 11, 13), status="promised")
        assert case["ladder_step"] == 1 and db.list_case_files(c, status="promised")[0]["id"] == case["id"]

        d = db.create_message_draft(c, case_id=case["id"], body="hi", ladder_step=1, idempotency_key="k-" + uuid.uuid4().hex)
        assert db.get_message_draft_by_key(c, d["idempotency_key"])["id"] == d["id"]
        assert db.update_message_draft(c, d["id"], status="approved")["status"] == "approved"
        assert db.list_message_drafts(c, status="approved")[0]["id"] == d["id"]

        m = db.create_chat_message(c, resident_id=r["id"], direction="in", body="I'll pay Friday")
        assert db.list_chat_messages(c, resident_id=r["id"], direction="in")[0]["id"] == m["id"]

        run = db.create_agent_run(c, case_id=case["id"], status="running", checkpoint={"step": 1, "x": [1, 2]})
        assert db.update_agent_run(c, run["id"], step_count=2, checkpoint={"step": 2})["checkpoint"] == {"step": 2}

        a = db.create_audit_log(c, actor="agent", action="draft_reminder", target=str(case["id"]), details={"k": 1}, run_id=run["id"])
        assert db.get_audit_log(c, a["id"])["details"] == {"k": 1}
        assert db.list_audit_logs(c, run_id=run["id"], limit=1)[0]["id"] == a["id"]

        assert db.delete_chat_message(c, m["id"]) and db.get_chat_message(c, m["id"]) is None
        assert db.delete_agent_run(c, run["id"]) and db.delete_message_draft(c, d["id"])
        assert db.delete_case_file(c, case["id"]) and db.delete_fee_charge(c, fee["id"])
        assert db.get_owner(c, o["id"])["id"] == o["id"]
        assert db.list_residents(c)[0]["id"] == r["id"] and db.get_property(c, p["id"]) and db.list_properties(c)
        assert db.update_resident(c, r["id"], is_demo_user=True)["is_demo_user"]
        assert db.list_owners(c, role="property_manager")
        assert db.update_owner(c, o["id"], name="T-renamed")["name"] == "T-renamed"
        assert db.update_property(c, p["id"], region="S")["region"] == "S"


def test_rls_hides_other_property(world):
    (_, pa, ra), (_, pb, rb) = world
    with db.session("app", pa["id"]) as c:
        assert db.get_resident(c, rb["id"]) is None and db.get_property(c, pb["id"]) is None
        assert [x["id"] for x in db.list_residents(c)] == [ra["id"]]
    with db.session("app") as c:   # no property set: deny by default
        assert db.list_residents(c) == []


def test_unknown_and_immutable_fields_rejected(world):
    (_, p, r), _ = world
    with db.session("app", p["id"]) as c:
        with pytest.raises(ValueError):
            db.create_resident(c, property_id=p["id"], display_name="x", phone="x", **{"id": uuid.uuid4()})
        with pytest.raises(ValueError):
            db.update_resident(c, r["id"], property_id=uuid.uuid4())   # cannot move a resident between properties
        with pytest.raises(ValueError):
            db.update_resident(c, r["id"], **{"display_name = 'x'; DROP TABLE resident; --": 1})


def test_duplicate_idempotency_key_and_fee_cycle(world):
    (_, p, r), _ = world
    with pytest.raises(psycopg.errors.UniqueViolation):
        with db.session("app", p["id"]) as c:
            f = db.create_fee_charge(c, resident_id=r["id"], cycle=dt.date(2026, 12, 1), amount=1, due_date=dt.date(2026, 12, 5), status="due")
            db.create_fee_charge(c, resident_id=r["id"], cycle=dt.date(2026, 12, 1), amount=1, due_date=dt.date(2026, 12, 5), status="due")
    with db.session("app", p["id"]) as c:       # the failed transaction rolled back entirely
        assert db.list_fee_charges(c, resident_id=r["id"]) == []


def test_agent_role_limits_are_enforced_by_the_database(world):
    (_, p, r), _ = world
    with db.session("app", p["id"]) as c:
        fee = db.create_fee_charge(c, resident_id=r["id"], cycle=dt.date(2026, 11, 1), amount=100, due_date=dt.date(2026, 11, 5), status="overdue")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with db.session("agent", p["id"]) as c:
            db.update_fee_charge(c, fee["id"], amount=1)
    with db.session("agent", p["id"]) as c:
        case = None
        assert db.get_fee_charge(c, fee["id"])["amount"] == 100     # can read
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with db.session("agent", p["id"]) as c:
            db.create_case_file(c, fee_charge_id=fee["id"])          # cannot create cases, only update them


def test_audit_log_has_no_update_or_delete_functions():
    assert not hasattr(db, "update_audit_log") and not hasattr(db, "delete_audit_log")
