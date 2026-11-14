"""The Collections agent's tools: a thin, rule-checking cover over database.py.

Each tool validates its input, applies the business rules, then calls CRUD functions.
It never writes SQL. Tools run as the least-privileged 'agent' database role, scoped to
one property by row-level security, so even a buggy tool cannot see or change anything
outside that property or edit a fee.

    registry = build_registry(property_id)
    agent = Agent(llm, registry, SYSTEM_PROMPT, ...)

Read tools:      list_overdue, get_case, get_fee, get_chat_history
Command tools:   draft_reminder, draft_reply, log_promise, escalate_to_owner, close_case, stop, wait
                 (all create drafts or notes; each ends the run: one outcome per case per run)
Do not exist:    record_payment, change_amount, send_message, run_sql
"""
from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, Callable
from zoneinfo import ZoneInfo

import config
import database as db
from harness import ToolError, ToolRegistry
from redaction import contains_pii, redact

Clock = Callable[[], dt.datetime]
TZ = ZoneInfo(config.TIMEZONE)
OPEN_FEE = ("due", "overdue")
ACTIVE_CASE = ("open", "promised")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _id(value: str, what: str = "id") -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except ValueError:
        raise ToolError(f"{what} must be an id exactly as given in the case data, got '{value}'")


def _date(value: str, what: str = "date") -> dt.date:
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        raise ToolError(f"{what} must be YYYY-MM-DD, got '{value}'")


def _clean(row: dict[str, Any] | None) -> dict[str, Any] | None:
    """Make a row JSON-friendly."""
    if row is None:
        return None
    return {k: (str(v) if isinstance(v, (uuid.UUID, dt.date, dt.datetime)) else v) for k, v in row.items()}


def _days_overdue(due: dt.date, today: dt.date) -> int:
    return max((today - due).days, 0)


def _fee_view(fee: dict, today: dt.date) -> dict[str, Any]:
    return {"fee_id": str(fee["id"]), "amount_rupees": fee["amount"] / 100, "due_date": str(fee["due_date"]),
            "status": fee["status"], "days_overdue": _days_overdue(fee["due_date"], today)}


def next_send_time(now: dt.datetime) -> dt.datetime:
    """Now if inside allowed hours, otherwise the next 09:00 local time."""
    local = now.astimezone(TZ)
    start, end = config.QUIET_HOURS[1], config.QUIET_HOURS[0]
    if start <= local.hour < end:
        return now
    day = local.date() if local.hour < start else local.date() + dt.timedelta(days=1)
    return dt.datetime.combine(day, dt.time(start), tzinfo=TZ)


def _check_body(body: str, names: list[str]) -> str:
    body = body.strip()
    if not body:
        raise ToolError("body is empty")
    if len(body) > config.MAX_MESSAGE_CHARS:
        raise ToolError(f"body is too long ({len(body)} characters, max {config.MAX_MESSAGE_CHARS})")
    if contains_pii(body, names):
        raise ToolError("body contains a name, phone number or ID. Rewrite it using 'you' and 'your fee'")
    return body


def _day_bounds(now: dt.datetime) -> tuple[dt.datetime, dt.datetime]:
    local = now.astimezone(TZ)
    start = dt.datetime.combine(local.date(), dt.time(0), tzinfo=TZ)
    return start, start + dt.timedelta(days=1)


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------

def build_registry(property_id: uuid.UUID | str, clock: Clock | None = None) -> ToolRegistry:
    """Build the tool set for one property. `clock` is injectable so tests control time."""
    now_fn: Clock = clock or (lambda: dt.datetime.now(TZ))
    reg = ToolRegistry()

    def session():
        return db.session("agent", property_id)

    def load_case(c, case_id: str, allowed: tuple[str, ...] | None = None):
        case = db.get_case_file(c, _id(case_id, "case_id"))
        if not case:
            raise ToolError("case not found")
        if allowed and case["status"] not in allowed:
            raise ToolError(f"case is already {case['status']}"
                            + (f" ({case['stop_reason']})" if case["stop_reason"] else ""))
        return case

    def cap_check(c, resident_id, now) -> None:
        start, end = _day_bounds(now)
        sent = db.count_resident_drafts_between(c, resident_id, start, end)
        if sent >= config.DAILY_CAP:
            raise ToolError(f"daily cap reached: {config.DAILY_CAP} messages per resident per day")

    # ----- read tools -------------------------------------------------------

    @reg.tool(description="Unpaid fees due on or before the end of a week, with case ids. week is any date in the "
                          "target week (YYYY-MM-DD); omit for this week.",
              parameters={"type": "object", "properties": {"week": {"type": "string"}}}, tags={"read"})
    def list_overdue(week: str | None = None):
        today = now_fn().date()
        anchor = _date(week, "week") if week else today
        week_end = anchor + dt.timedelta(days=6 - anchor.weekday())      # the Sunday of that week
        with session() as c:
            rows = db.list_open_fees_with_cases(c, due_on_or_before=week_end, limit=50)
        return {"week_ends": str(week_end), "count": len(rows), "fees": [
            {"fee_id": str(r["fee_id"]), "case_id": str(r["case_id"]) if r["case_id"] else None,
             "amount_rupees": r["amount"] / 100, "due_date": str(r["due_date"]),
             "days_overdue": _days_overdue(r["due_date"], today), "ladder_step": r["ladder_step"],
             "case_status": r["case_status"], "promise_date": str(r["promise_date"]) if r["promise_date"] else None}
            for r in rows]}

    @reg.tool(description="One case: ladder step, status, promise, the fee, and the history of drafts.",
              parameters={"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}, tags={"read"})
    def get_case(id: str):
        with session() as c:
            case = load_case(c, id)
            fee = db.get_fee_charge(c, case["fee_charge_id"])
            drafts = db.list_message_drafts(c, case_id=case["id"])
        return {**_clean({k: case[k] for k in ("ladder_step", "status", "promise_date", "next_action_at",
                                               "messages_sent", "stop_reason")}),
                "case_id": str(case["id"]), "fee": _fee_view(fee, now_fn().date()),
                "drafts": [{"ladder_step": d["ladder_step"], "status": d["status"], "body": d["body"]} for d in drafts]}

    @reg.tool(description="One fee by fee id: amount in rupees, due date, status, days overdue.",
              parameters={"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}, tags={"read"})
    def get_fee(id: str):
        with session() as c:
            fee = db.get_fee_charge(c, _id(id, "id"))
        if not fee:
            raise ToolError("fee not found")
        return _fee_view(fee, now_fn().date())

    @reg.tool(description="The most recent chat messages for a case's resident, oldest first, with personal data removed.",
              parameters={"type": "object", "properties": {"case": {"type": "string"}, "n": {"type": "integer"}},
                          "required": ["case"]}, tags={"read"})
    def get_chat_history(case: str, n: int = 5):
        n = max(1, min(n, config.HISTORY_MAX))
        with session() as c:
            load_case(c, case)
            res = db.get_case_resident(c, _id(case))
            msgs = db.list_chat_messages(c, resident_id=res["id"], limit=n, newest_first=True)
        names = [res["display_name"]]
        return [{"direction": m["direction"], "at": str(m["created_at"]), "text": redact(m["body"], names)}
                for m in reversed(msgs)]

    # ----- command tools ----------------------------------------------------

    @reg.tool(description="Propose a reminder for the next ladder step (1 or 2). Creates a draft for a person to approve. "
                          "Written in plain words, under 50 words, no names or numbers.",
              parameters={"type": "object", "properties": {
                  "case_id": {"type": "string"}, "ladder_step": {"type": "integer", "enum": [1, 2]},
                  "body": {"type": "string"}}, "required": ["case_id", "ladder_step", "body"]},
              terminal=True, tags={"draft"})
    def draft_reminder(case_id: str, ladder_step: int, body: str):
        now = now_fn()
        with session() as c:
            case = load_case(c, case_id, ACTIVE_CASE)
            fee = db.get_fee_charge(c, case["fee_charge_id"])
            res = db.get_case_resident(c, case["id"])
            if fee["status"] not in OPEN_FEE:
                raise ToolError(f"fee is {fee['status']}. Use close_case instead")
            if ladder_step != case["ladder_step"] + 1:
                raise ToolError(f"the next step is {case['ladder_step'] + 1}, not {ladder_step}"
                                + (". Use escalate_to_owner" if case["ladder_step"] >= 2 else ""))
            if case["promise_date"] and case["promise_date"] >= now.astimezone(TZ).date():
                raise ToolError(f"resident promised to pay by {case['promise_date']}. Use wait")
            body = _check_body(body, [res["display_name"]])
            key = f"{case['id']}-s{ladder_step}"
            if (old := db.get_message_draft_by_key(c, key)):
                return {"result": "already_drafted", "draft_id": str(old["id"]), "draft_status": old["status"]}
            if db.list_open_drafts(c, case["id"]):
                raise ToolError("a draft for this case is already waiting. Use wait")
            cap_check(c, res["id"], now)
            send_after = next_send_time(now)
            d = db.create_message_draft(c, case_id=case["id"], body=body, ladder_step=ladder_step,
                                        idempotency_key=key, send_after=send_after)
        return {"result": "drafted", "draft_id": str(d["id"]), "status": "pending approval",
                "send_after": str(send_after), "held_for_quiet_hours": send_after > now}

    @reg.tool(description="Propose a short reply to the resident's latest message when no ladder step is needed "
                          "(greeting, thanks, a question). Does not move the ladder. Needs an inbound message.",
              parameters={"type": "object", "properties": {"case_id": {"type": "string"}, "body": {"type": "string"}},
                          "required": ["case_id", "body"]}, terminal=True, tags={"draft"})
    def draft_reply(case_id: str, body: str):
        now = now_fn()
        with session() as c:
            case = load_case(c, case_id, ACTIVE_CASE + ("escalated",))
            res = db.get_case_resident(c, case["id"])
            body = _check_body(body, [res["display_name"]])
            inbound = db.list_chat_messages(c, resident_id=res["id"], direction="in", limit=1, newest_first=True)
            if not inbound:
                raise ToolError("there is no message from the resident to reply to")
            key = f"{case['id']}-reply-{inbound[0]['id']}"
            if (old := db.get_message_draft_by_key(c, key)):
                return {"result": "already_drafted", "draft_id": str(old["id"]), "draft_status": old["status"]}
            cap_check(c, res["id"], now)
            send_after = next_send_time(now)
            d = db.create_message_draft(c, case_id=case["id"], body=body, ladder_step=case["ladder_step"],
                                        idempotency_key=key, send_after=send_after)
        return {"result": "drafted", "draft_id": str(d["id"]), "status": "pending approval",
                "send_after": str(send_after)}

    @reg.tool(description="Record the date the resident promised to pay (YYYY-MM-DD, from the calendar in the case data).",
              parameters={"type": "object", "properties": {"case_id": {"type": "string"}, "promise_date": {"type": "string"}},
                          "required": ["case_id", "promise_date"]}, terminal=True, tags={"write"})
    def log_promise(case_id: str, promise_date: str):
        today = now_fn().astimezone(TZ).date()
        when = _date(promise_date, "promise_date")
        if when < today:
            raise ToolError(f"{when} is in the past (today is {today})")
        if (when - today).days > config.MAX_PROMISE_DAYS:
            raise ToolError(f"{when} is more than {config.MAX_PROMISE_DAYS} days away. Ask for an earlier date")
        recheck = dt.datetime.combine(when + dt.timedelta(days=1), dt.time(config.QUIET_HOURS[1]), tzinfo=TZ)
        with session() as c:
            case = load_case(c, case_id, ACTIVE_CASE)
            db.update_case_file(c, case["id"], promise_date=when, status="promised", next_action_at=recheck)
        return {"result": "promise_logged", "promise_date": str(when), "recheck_at": str(recheck)}

    @reg.tool(description="Ask the property owner to step in (hardship, dispute, suspicious message, or the end of the ladder).",
              parameters={"type": "object", "properties": {"case_id": {"type": "string"}, "reason": {"type": "string"}},
                          "required": ["case_id", "reason"]}, terminal=True, tags={"write"})
    def escalate_to_owner(case_id: str, reason: str):
        with session() as c:
            case = load_case(c, case_id)
            if case["status"] == "escalated":
                return {"result": "already_escalated", "reason": case["stop_reason"]}
            if case["status"] in ("closed", "stopped"):
                raise ToolError(f"case is already {case['status']}")
            fields = {"status": "escalated", "stop_reason": reason.strip()[:200]}
            if case["ladder_step"] == 2:
                fields["ladder_step"] = 3
            db.update_case_file(c, case["id"], **fields)
        return {"result": "escalated", "reason": fields["stop_reason"]}

    @reg.tool(description="Close a case. Only allowed when the fee is paid or waived.",
              parameters={"type": "object", "properties": {"case_id": {"type": "string"}, "reason": {"type": "string"}},
                          "required": ["case_id", "reason"]}, terminal=True, tags={"write"})
    def close_case(case_id: str, reason: str):
        with session() as c:
            case = load_case(c, case_id)
            fee = db.get_fee_charge(c, case["fee_charge_id"])
            if fee["status"] not in ("paid", "waived"):
                raise ToolError(f"fee is {fee['status']}. A case closes only when the fee is paid or waived")
            db.update_case_file(c, case["id"], status="closed", stop_reason=reason.strip()[:200])
        return {"result": "closed"}

    @reg.tool(description="End work on this case with a plain reason (e.g. 'ladder complete', 'missing data').",
              parameters={"type": "object", "properties": {"case_id": {"type": "string"}, "reason": {"type": "string"}},
                          "required": ["case_id", "reason"]}, terminal=True, tags={"write"})
    def stop(case_id: str, reason: str):
        with session() as c:
            case = load_case(c, case_id)
            if case["status"] in ("closed", "stopped"):
                return {"result": f"already_{case['status']}"}
            db.update_case_file(c, case["id"], status="stopped", stop_reason=reason.strip()[:200])
        return {"result": "stopped"}

    @reg.tool(description="Nothing to do yet. Look at this case again on a later date (YYYY-MM-DD).",
              parameters={"type": "object", "properties": {"case_id": {"type": "string"}, "until": {"type": "string"}},
                          "required": ["case_id", "until"]}, terminal=True, tags={"write"})
    def wait(case_id: str, until: str):
        now = now_fn()
        day = _date(until, "until")
        when = dt.datetime.combine(day, dt.time(config.QUIET_HOURS[1]), tzinfo=TZ)
        if when <= now:
            raise ToolError(f"{day} is not in the future")
        if (day - now.astimezone(TZ).date()).days > config.MAX_PROMISE_DAYS:
            raise ToolError(f"{day} is too far away")
        with session() as c:
            case = load_case(c, case_id, ACTIVE_CASE)
            db.update_case_file(c, case["id"], next_action_at=when)
        return {"result": "waiting", "until": str(when)}

    return reg
