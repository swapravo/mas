"""Demo service layer: seeds one fake case, runs the agent, and handles approve/send.

This is the "core system" side for local testing. It owns the simulated clock so quiet hours,
promise dates and the daily cap can be tried without waiting. Single demo case, fake data only.
"""
from __future__ import annotations

import datetime as dt
import json
import threading
import uuid
from typing import Any

import database as db
from config import DAILY_CAP
from harness import Agent, LLMUnavailable, OpenRouterLLM
from prompts import SYSTEM_PROMPT, build_turn
from redaction import redact
from tools import TZ, build_registry

RESIDENT_NAME = "Asha Verma"          # fake; redacted before any model call
FEE_PAISE = 600000                    # Rs 6,000
DAYS_OVERDUE = 4

_lock = threading.Lock()
_state: dict[str, Any] = {"ids": None, "offset": dt.timedelta(0)}


# ---------------------------------------------------------------------------
# clock (simulated)
# ---------------------------------------------------------------------------

def clock() -> dt.datetime:
    return dt.datetime.now(TZ) + _state["offset"]


def set_clock(*, advance_hours: float | None = None, to: dt.datetime | None = None) -> None:
    if to is not None:
        _state["offset"] = to.astimezone(TZ) - dt.datetime.now(TZ)
    elif advance_hours is not None:
        _state["offset"] += dt.timedelta(hours=advance_hours)
    release_due_drafts()


# ---------------------------------------------------------------------------
# seed / reset
# ---------------------------------------------------------------------------

def _load_ids() -> dict[str, uuid.UUID] | None:
    with db.session("owner") as c:
        res = db.list_residents(c, is_demo_user=True, limit=1)
        if not res:
            return None
        r = res[0]
        p = db.get_property(c, r["property_id"])
        f = db.list_fee_charges(c, resident_id=r["id"], limit=1)[0]
        k = db.list_case_files(c, fee_charge_id=f["id"])[0]
        return {"owner": p["owner_id"], "property": p["id"], "resident": r["id"], "fee": f["id"], "case": k["id"]}


def ids() -> dict[str, uuid.UUID]:
    if _state["ids"] is None:
        _state["ids"] = _load_ids() or reset()
    return _state["ids"]


def reset() -> dict[str, uuid.UUID]:
    """Remove old demo data (the audit log is insert-only and stays) and create a fresh overdue case."""
    _state["offset"] = dt.timedelta(0)
    now = clock()
    with db.session("owner") as c:
        for r in db.list_residents(c, is_demo_user=True):
            for f in db.list_fee_charges(c, resident_id=r["id"]):
                for k in db.list_case_files(c, fee_charge_id=f["id"]):
                    for x in db.list_agent_runs(c, case_id=k["id"]):
                        db.delete_agent_run(c, x["id"])
                    for d in db.list_message_drafts(c, case_id=k["id"]):
                        db.delete_message_draft(c, d["id"])
                    db.delete_case_file(c, k["id"])
                db.delete_fee_charge(c, f["id"])
            for m in db.list_chat_messages(c, resident_id=r["id"]):
                db.delete_chat_message(c, m["id"])
            p = db.get_property(c, r["property_id"])
            db.delete_resident(c, r["id"])
            db.delete_property(c, p["id"])
            db.delete_owner(c, p["owner_id"])
        o = db.create_owner(c, name="Demo Owner", contact="xxxx-0000", role="property_manager")
        p = db.create_property(c, name="Demo Property", region="North", owner_id=o["id"])
        r = db.create_resident(c, property_id=p["id"], display_name=RESIDENT_NAME, phone="xxxxx-xx000", is_demo_user=True)
        due = now.date() - dt.timedelta(days=DAYS_OVERDUE)
        f = db.create_fee_charge(c, resident_id=r["id"], cycle=due.replace(day=1), amount=FEE_PAISE, due_date=due, status="overdue")
        k = db.create_case_file(c, fee_charge_id=f["id"], next_action_at=now)
    _state["ids"] = {"owner": o["id"], "property": p["id"], "resident": r["id"], "fee": f["id"], "case": k["id"]}
    return _state["ids"]


# ---------------------------------------------------------------------------
# audit
# ---------------------------------------------------------------------------

def _audit(c, actor: str, action: str, target: Any, details: dict | None = None, run_id: Any = None) -> None:
    db.create_audit_log(c, actor=actor, action=action, target=str(target), details=json.loads(json.dumps(details or {}, default=str)),
                        run_id=run_id)


# ---------------------------------------------------------------------------
# state for the UI
# ---------------------------------------------------------------------------

def _row(r: dict | None) -> dict | None:
    if r is None:
        return None
    return {k: (str(v) if isinstance(v, (uuid.UUID, dt.date, dt.datetime)) else v) for k, v in r.items()}


def get_state() -> dict[str, Any]:
    i = ids()
    release_due_drafts()
    with db.session("app", i["property"]) as c:
        case = db.get_case_file(c, i["case"])
        fee = db.get_fee_charge(c, i["fee"])
        drafts = db.list_message_drafts(c, case_id=i["case"])
        chat = db.list_chat_messages(c, resident_id=i["resident"])
        audit = [a for a in db.list_audit_logs(c, limit=300) if a["target"] == str(i["case"])][:60]
    now = clock()
    return {
        "now": now.isoformat(), "daily_cap": DAILY_CAP, "resident_name": RESIDENT_NAME,
        "case": _row(case), "fee": {**_row(fee), "amount_rupees": fee["amount"] / 100,
                                    "days_overdue": max((now.date() - fee["due_date"]).days, 0)},
        "drafts": [_row(d) for d in drafts], "chat": [_row(m) for m in chat], "audit": [_row(a) for a in audit],
    }


# ---------------------------------------------------------------------------
# resident chat, approve, send
# ---------------------------------------------------------------------------

def resident_says(body: str) -> None:
    i = ids()
    with db.session("app", i["property"]) as c:
        m = db.create_chat_message(c, resident_id=i["resident"], direction="in", body=body.strip(), created_at=clock())
        _audit(c, "person", "resident_message", i["case"], {"chat_message_id": m["id"]})


def _send(c, draft: dict, now: dt.datetime) -> None:
    i = ids()
    case = db.get_case_file(c, draft["case_id"])
    db.update_message_draft(c, draft["id"], status="sent")
    db.create_chat_message(c, resident_id=i["resident"], direction="out", body=draft["body"], created_at=now)
    db.update_case_file(c, case["id"], messages_sent=case["messages_sent"] + 1,
                        ladder_step=max(case["ladder_step"], draft["ladder_step"]))
    _audit(c, "system", "message_sent", case["id"], {"draft_id": draft["id"], "ladder_step": draft["ladder_step"]})


def release_due_drafts() -> None:
    """Send approved drafts whose time has come (e.g. held overnight until 09:00)."""
    if _state["ids"] is None:
        return
    i = _state["ids"]
    now = clock()
    with db.session("app", i["property"]) as c:
        for d in db.list_message_drafts(c, case_id=i["case"], status="held", send_after_before=now):
            _send(c, d, now)


def approve(draft_id: uuid.UUID) -> str:
    i = ids()
    now = clock()
    with db.session("app", i["property"]) as c:
        d = db.get_message_draft(c, draft_id)
        if not d or d["status"] != "pending":
            raise ValueError("draft is not waiting for approval")
        db.update_message_draft(c, d["id"], status="approved", approved_by=i["owner"])
        _audit(c, "person", "approve_draft", i["case"], {"draft_id": d["id"]})
        d = db.get_message_draft(c, d["id"])
        if d["send_after"] <= now:
            _send(c, d, now)
            return "sent"
        db.update_message_draft(c, d["id"], status="held")
        _audit(c, "system", "held_for_quiet_hours", i["case"], {"draft_id": d["id"], "send_after": d["send_after"]})
        return "held"


def reject(draft_id: uuid.UUID) -> None:
    i = ids()
    with db.session("app", i["property"]) as c:
        d = db.get_message_draft(c, draft_id)
        if not d or d["status"] != "pending":
            raise ValueError("draft is not waiting for approval")
        db.update_message_draft(c, d["id"], status="rejected")
        _audit(c, "person", "reject_draft", i["case"], {"draft_id": d["id"]})


# ---------------------------------------------------------------------------
# run the agent
# ---------------------------------------------------------------------------

_llm = None


def run_agent() -> dict[str, Any]:
    global _llm
    i = ids()
    release_due_drafts()
    now = clock()
    if not _lock.acquire(blocking=False):
        raise RuntimeError("the agent is already running")
    try:
        reg = build_registry(i["property"], clock=clock)
        with db.session("agent", i["property"]) as c:
            case = db.get_case_file(c, i["case"])
            last = db.list_agent_runs(c, case_id=i["case"], limit=1)
            inbound = db.list_chat_messages(c, resident_id=i["resident"], direction="in",
                                            since=last[0]["started_at"] if last else None)
        if case["status"] in ("closed", "stopped") or (case["status"] == "escalated" and not inbound):
            return {"status": "skipped", "reason": f"case is {case['status']} and there is nothing new from the resident",
                    "steps": [], "replies": [], "model": None}
        with db.session("agent", i["property"]) as c:
            run = db.create_agent_run(c, case_id=i["case"], status="running", started_at=now)

        snap = reg.get("get_case").fn(id=str(i["case"]))
        snap.pop("drafts")
        turn = build_turn(snap, now.date(), [redact(m["body"], [RESIDENT_NAME]) for m in inbound])

        def checkpoint(st) -> None:
            with db.session("agent", i["property"]) as c:
                db.update_agent_run(c, run["id"], step_count=st.step, checkpoint=st.to_json())

        def on_event(ev: dict) -> None:
            if ev["type"] == "finished":
                return
            with db.session("agent", i["property"]) as c:
                _audit(c, "agent", ev["type"] if ev["type"] != "tool_result" else ev["tool"], i["case"],
                       {k: v for k, v in ev.items() if k not in ("type", "run_id", "sig")}, run_id=run["id"])

        _llm = _llm or OpenRouterLLM(max_tokens=500)
        try:
            res = Agent(_llm, reg, SYSTEM_PROMPT, max_steps=5, checkpoint=checkpoint, on_event=on_event
                        ).run(turn, run_id=str(run["id"]))
        except LLMUnavailable as e:
            with db.session("agent", i["property"]) as c:
                db.update_agent_run(c, run["id"], status="error")
            raise
        with db.session("agent", i["property"]) as c:
            db.update_agent_run(c, run["id"], status=res.status)
        steps = [{"step": e["step"], "type": e["type"], "tool": e.get("tool"), "args": e.get("args"),
                  "ok": e.get("ok"), "result": e.get("result"), "why": e.get("why") or e.get("error"),
                  "reason": e.get("reason")} for e in res.state.events if e["type"] != "finished"]
        return {"status": res.status, "model": _llm.last_model, "steps": steps,
                "replies": [m["content"] for m in res.state.messages if m["role"] == "assistant"],
                "new_messages": [redact(m["body"], [RESIDENT_NAME]) for m in inbound]}
    finally:
        _lock.release()
