"""Tool tests: real Postgres (schema.sql applied), scripted model. Skipped without a database."""
import datetime as dt
import json
import os
import sys
import uuid
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import database as db  # noqa: E402
from harness import Agent, ScriptedLLM  # noqa: E402
from tools import build_registry, next_send_time  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")
NOON = dt.datetime(2026, 11, 5, 12, 0, tzinfo=IST)       # Thursday
NIGHT = dt.datetime(2026, 11, 5, 23, 30, tzinfo=IST)


@pytest.fixture(scope="module", autouse=True)
def _reachable():
    try:
        with db.session("owner") as c:
            c.execute("SELECT 1")
    except Exception as exc:
        pytest.skip(f"database not available: {exc}")


@pytest.fixture
def w():
    """One property, one resident, an overdue fee and an open case (plus a second property for isolation)."""
    ids = {}
    with db.session("owner") as c:
        for tag in ("a", "b"):
            o = db.create_owner(c, name="T-owner", contact="x", role="property_manager")
            p = db.create_property(c, name="T-prop", region="N", owner_id=o["id"])
            r = db.create_resident(c, property_id=p["id"], display_name="Priya Sharma", phone="x")
            f = db.create_fee_charge(c, resident_id=r["id"], cycle=dt.date(2026, 11, 1), amount=850000,
                                     due_date=dt.date(2026, 11, 1), status="overdue")
            k = db.create_case_file(c, fee_charge_id=f["id"])
            ids[tag] = dict(o=o["id"], p=p["id"], r=r["id"], f=f["id"], c=k["id"])
    yield ids
    with db.session("owner") as c:
        for v in ids.values():
            for sql in ("DELETE FROM message_draft WHERE case_id = %(c)s", "DELETE FROM case_file WHERE id = %(c)s",
                        "DELETE FROM chat_message WHERE resident_id = %(r)s", "DELETE FROM fee_charge WHERE id = %(f)s",
                        "DELETE FROM resident WHERE id = %(r)s", "DELETE FROM property WHERE id = %(p)s",
                        "DELETE FROM owner WHERE id = %(o)s"):
                c.execute(sql, v)


def tools(w, when=NOON, tag="a"):
    return build_registry(w[tag]["p"], clock=lambda: when)


def call(reg, name, **args):
    return reg.get(name).fn(**args)


def err(reg, name, **args):
    from harness import ToolError
    with pytest.raises(ToolError) as e:
        call(reg, name, **args)
    return str(e.value)


def case_row(w, tag="a"):
    with db.session("owner") as c:
        return db.get_case_file(c, w[tag]["c"])


def inbound(w, text, tag="a"):
    with db.session("owner") as c:
        db.create_chat_message(c, resident_id=w[tag]["r"], direction="in", body=text)


# ---- read tools --------------------------------------------------------------

def test_read_tools(w):
    t, cid = tools(w), str(w["a"]["c"])
    out = call(t, "list_overdue")
    assert out["count"] == 1 and out["fees"][0]["case_id"] == cid and out["fees"][0]["days_overdue"] == 4
    assert out["fees"][0]["amount_rupees"] == 8500 and out["week_ends"] == "2026-11-08"
    assert call(t, "list_overdue", week="2026-10-05")["count"] == 0       # fee is due after that week ends
    assert call(t, "get_fee", id=str(w["a"]["f"]))["amount_rupees"] == 8500
    case = call(t, "get_case", id=cid)
    assert case["ladder_step"] == 0 and case["fee"]["status"] == "overdue" and case["drafts"] == []


def test_list_overdue_only_sees_own_property(w):
    ids = {x["fee_id"] for x in call(tools(w), "list_overdue")["fees"]}
    assert str(w["a"]["f"]) in ids and str(w["b"]["f"]) not in ids


def test_chat_history_is_redacted(w):
    inbound(w, "I am Priya Sharma, call me on 98765 43210 or priya@mail.com, id 1234 5678 9012")
    out = call(tools(w), "get_chat_history", case=str(w["a"]["c"]), n=5)
    text = out[-1]["text"]
    assert "Priya" not in text and "98765" not in text and "@" not in text and "1234" not in text
    assert "[name]" in text and "[phone]" in text and "[email]" in text and "[id]" in text


def test_bad_ids_are_plain_errors(w):
    t = tools(w)
    assert "exactly as given" in err(t, "get_case", id="c14")
    assert err(t, "get_case", id=str(uuid.uuid4())) == "case not found"
    assert err(t, "get_case", id=str(w["b"]["c"])) == "case not found"     # other property: hidden by RLS


# ---- draft_reminder ----------------------------------------------------------

OK = "Hi, a quick reminder that your fee of Rs 8,500 was due on 1 Nov. Could you pay today?"


def test_draft_reminder_happy_path_and_idempotent(w):
    t, cid = tools(w), str(w["a"]["c"])
    r = call(t, "draft_reminder", case_id=cid, ladder_step=1, body=OK)
    assert r["result"] == "drafted" and r["held_for_quiet_hours"] is False
    again = call(t, "draft_reminder", case_id=cid, ladder_step=1, body=OK)
    assert again["result"] == "already_drafted" and again["draft_id"] == r["draft_id"]


def test_ladder_order_enforced(w):
    t, cid = tools(w), str(w["a"]["c"])
    assert "next step is 1" in err(t, "draft_reminder", case_id=cid, ladder_step=2, body=OK)
    call(t, "draft_reminder", case_id=cid, ladder_step=1, body=OK)
    # the case is still at step 0 until the draft is approved and sent, so step 2 stays refused
    assert "next step is 1" in err(t, "draft_reminder", case_id=cid, ladder_step=2, body=OK)


def test_body_rules(w):
    t, cid = tools(w), str(w["a"]["c"])
    assert "name, phone number or ID" in err(t, "draft_reminder", case_id=cid, ladder_step=1, body="Hi Priya, please pay")
    assert "name, phone number or ID" in err(t, "draft_reminder", case_id=cid, ladder_step=1, body="Call 9876543210")
    assert "too long" in err(t, "draft_reminder", case_id=cid, ladder_step=1, body="x" * 500)
    assert err(t, "draft_reminder", case_id=cid, ladder_step=1, body="  ") == "body is empty"


def test_quiet_hours_hold_until_9am(w):
    r = call(tools(w, NIGHT), "draft_reminder", case_id=str(w["a"]["c"]), ladder_step=1, body=OK)
    assert r["held_for_quiet_hours"] is True and r["send_after"].startswith("2026-11-06 09:00")
    assert next_send_time(dt.datetime(2026, 11, 6, 3, 0, tzinfo=IST)).hour == 9
    assert next_send_time(NOON) == NOON


def test_daily_cap(w):
    t, cid = tools(w), str(w["a"]["c"])
    call(t, "draft_reminder", case_id=cid, ladder_step=1, body=OK)
    inbound(w, "hello")
    call(t, "draft_reply", case_id=cid, body="Hello! How can I help?")
    inbound(w, "thanks")
    assert "daily cap" in err(t, "draft_reply", case_id=cid, body="You're welcome!")


def test_no_reminder_during_promise_or_when_paid(w):
    t, cid = tools(w), str(w["a"]["c"])
    call(t, "log_promise", case_id=cid, promise_date="2026-11-06")
    assert "promised to pay by 2026-11-06" in err(t, "draft_reminder", case_id=cid, ladder_step=1, body=OK)
    with db.session("owner") as c:
        db.update_fee_charge(c, w["a"]["f"], status="paid")
    assert "Use close_case" in err(t, "draft_reminder", case_id=cid, ladder_step=1, body=OK)


# ---- draft_reply -------------------------------------------------------------

def test_draft_reply_needs_inbound_and_does_not_move_ladder(w):
    t, cid = tools(w), str(w["a"]["c"])
    assert "no message" in err(t, "draft_reply", case_id=cid, body="Hello!")
    inbound(w, "hey who is this?")
    r = call(t, "draft_reply", case_id=cid, body="Hello! I'm the assistant for your housing operator.")
    assert r["result"] == "drafted" and call(t, "draft_reply", case_id=cid, body="Hello again")["result"] == "already_drafted"
    assert case_row(w)["ladder_step"] == 0


# ---- other command tools -------------------------------------------------------

def test_log_promise(w):
    t, cid = tools(w), str(w["a"]["c"])
    assert "in the past" in err(t, "log_promise", case_id=cid, promise_date="2026-11-01")
    assert "YYYY-MM-DD" in err(t, "log_promise", case_id=cid, promise_date="Friday")
    assert "days away" in err(t, "log_promise", case_id=cid, promise_date="2027-06-01")
    call(t, "log_promise", case_id=cid, promise_date="2026-11-06")
    c = case_row(w)
    assert c["status"] == "promised" and str(c["promise_date"]) == "2026-11-06"
    assert c["next_action_at"].astimezone(IST) == dt.datetime(2026, 11, 7, 9, 0, tzinfo=IST)


def test_escalate_sets_ladder_3_only_from_step_2(w):
    t, cid = tools(w), str(w["a"]["c"])
    with db.session("owner") as c:
        db.update_case_file(c, w["a"]["c"], ladder_step=2)
    call(t, "escalate_to_owner", case_id=cid, reason="end of ladder")
    c = case_row(w)
    assert c["status"] == "escalated" and c["ladder_step"] == 3 and c["stop_reason"] == "end of ladder"
    assert call(t, "escalate_to_owner", case_id=cid, reason="again")["result"] == "already_escalated"
    assert "escalated" in err(t, "log_promise", case_id=cid, promise_date="2026-11-06")


def test_close_only_when_paid(w):
    t, cid = tools(w), str(w["a"]["c"])
    assert "only when the fee is paid" in err(t, "close_case", case_id=cid, reason="x")
    with db.session("owner") as c:
        db.update_fee_charge(c, w["a"]["f"], status="paid")
    call(t, "close_case", case_id=cid, reason="bank match found")
    assert case_row(w)["status"] == "closed"


def test_stop_and_wait(w):
    t, cid = tools(w), str(w["a"]["c"])
    assert "not in the future" in err(t, "wait", case_id=cid, until="2026-11-05")
    call(t, "wait", case_id=cid, until="2026-11-09")
    assert case_row(w)["next_action_at"].astimezone(IST).date() == dt.date(2026, 11, 9)
    call(t, "stop", case_id=cid, reason="ladder complete")
    assert case_row(w)["status"] == "stopped" and call(t, "stop", case_id=cid, reason="x")["result"] == "already_stopped"


def test_forbidden_tools_do_not_exist(w):
    names = set(tools(w).names())
    assert not names & {"record_payment", "change_amount", "send_message", "run_sql"}


# ---- through the harness ------------------------------------------------------

def test_agent_loop_recovers_from_a_refused_call(w):
    cid = str(w["a"]["c"])
    llm = ScriptedLLM([
        json.dumps({"tool": "draft_reminder", "args": {"case_id": cid, "ladder_step": 2, "body": OK}, "reason": "skip"}),
        json.dumps({"tool": "draft_reminder", "args": {"case_id": cid, "ladder_step": 1, "body": OK}, "reason": "ok"})])
    r = Agent(llm, tools(w), "sys").run("go")
    assert r.status == "done" and r.final["result"]["result"] == "drafted"
    assert any(e["type"] == "tool_result" and not e["ok"] for e in r.state.events)
