"""Database access: plain CRUD over schema.sql. No ORM, no business rules, no agent logic.

This is the data layer. The agent's tools (tools.py) are a thin cover over these functions.
Every function takes an open connection from `session()`, so a caller controls the
transaction and the role. Permissions are NOT re-implemented here: the database decides
(row-level security by property, least privilege per role, insert-only audit_log).

    with session("agent", property_id=pid) as conn:
        case = get_case_file(conn, case_id)
        update_case_file(conn, case_id, ladder_step=1)

Conventions
- create_x(conn, **fields) -> the new row (dict)
- get_x(conn, id)          -> row or None   (None also when row-level security hides it)
- list_xs(conn, **filters) -> list of rows
- update_x(conn, id, **fields) -> updated row or None
- delete_x(conn, id)       -> True if a row was deleted
Unknown field names raise ValueError, so a typo or a hostile key never reaches SQL.
"""
from __future__ import annotations

import datetime as dt
import uuid
from contextlib import contextmanager
from typing import Any, Iterator

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from config import DATABASE_URLS

Row = dict[str, Any]
Conn = psycopg.Connection


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

@contextmanager
def session(role: str = "app", property_id: uuid.UUID | str | None = None) -> Iterator[Conn]:
    """Open a transaction as `role` ('app', 'agent' or 'owner').

    Commits on a clean exit, rolls back on an exception. `property_id` scopes the whole
    transaction through row-level security; without it the app and agent roles see no rows.
    Do not call conn.commit() inside the block: the property setting lasts one transaction.
    """
    if role not in DATABASE_URLS:
        raise ValueError(f"unknown role: {role}")
    with psycopg.connect(DATABASE_URLS[role], row_factory=dict_row) as conn:
        if property_id is not None:
            conn.execute("SELECT set_config('app.property_id', %s, true)", (str(property_id),))
        yield conn


# ---------------------------------------------------------------------------
# Generic helpers (identifiers come from the fixed specs below, values are always bound)
# ---------------------------------------------------------------------------

# table -> (columns that may be set on create, columns that may be changed afterwards)
SPECS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "owner":         (frozenset({"name", "contact", "role"}), frozenset({"name", "contact", "role"})),
    "property":      (frozenset({"name", "region", "owner_id"}), frozenset({"name", "region", "owner_id"})),
    "resident":      (frozenset({"property_id", "display_name", "phone", "is_demo_user"}),
                      frozenset({"display_name", "phone", "is_demo_user"})),
    "fee_charge":    (frozenset({"resident_id", "cycle", "amount", "due_date", "status"}),
                      frozenset({"amount", "due_date", "status"})),
    "case_file":     (frozenset({"fee_charge_id", "ladder_step", "status", "promise_date", "next_action_at",
                                 "messages_sent", "stop_reason"}),
                      frozenset({"ladder_step", "status", "promise_date", "next_action_at", "messages_sent",
                                 "stop_reason"})),
    "message_draft": (frozenset({"case_id", "body", "ladder_step", "status", "idempotency_key", "send_after",
                                 "approved_by"}),
                      frozenset({"body", "status", "send_after", "approved_by"})),
    "chat_message":  (frozenset({"resident_id", "direction", "body", "created_at"}), frozenset({"body"})),
    "agent_run":     (frozenset({"case_id", "status", "step_count", "checkpoint", "started_at"}),
                      frozenset({"status", "step_count", "checkpoint"})),
    "audit_log":     (frozenset({"actor", "action", "target", "details", "run_id"}), frozenset()),  # insert only
}

_JSON_COLS = {"checkpoint", "details"}
_UUID_TABLES = set(SPECS) - {"audit_log"}   # audit_log.id is a bigint


def _val(col: str, v: Any) -> Any:
    return Jsonb(v) if col in _JSON_COLS and v is not None else v


def _check(table: str, fields: dict[str, Any], allowed: frozenset[str], what: str) -> None:
    bad = set(fields) - allowed
    if bad:
        raise ValueError(f"{table}: cannot {what} {sorted(bad)}")


def _insert(conn: Conn, table: str, fields: dict[str, Any]) -> Row:
    _check(table, fields, SPECS[table][0], "set")
    if not fields:
        q = sql.SQL("INSERT INTO {} DEFAULT VALUES RETURNING *").format(sql.Identifier(table))
        return conn.execute(q).fetchone()
    q = sql.SQL("INSERT INTO {} ({}) VALUES ({}) RETURNING *").format(
        sql.Identifier(table),
        sql.SQL(", ").join(map(sql.Identifier, fields)),
        sql.SQL(", ").join(sql.Placeholder() * len(fields)))
    return conn.execute(q, [_val(c, v) for c, v in fields.items()]).fetchone()


def _get(conn: Conn, table: str, id_: Any) -> Row | None:
    return conn.execute(sql.SQL("SELECT * FROM {} WHERE id = %s").format(sql.Identifier(table)), (id_,)).fetchone()


def _find(conn: Conn, table: str, where: dict[str, Any], *, order: str = "id", desc: bool = False,
          limit: int | None = None, extra: list[tuple[str, str, Any]] | None = None) -> list[Row]:
    """SELECT with equality filters (None values are ignored) plus optional (column, operator, value) filters."""
    conds, params = [], []
    for c, v in where.items():
        if v is not None:
            conds.append(sql.SQL("{} = %s").format(sql.Identifier(c)))
            params.append(v)
    for c, op, v in extra or []:
        if v is not None:
            assert op in ("<", "<=", ">", ">=")
            conds.append(sql.SQL("{} {} %s").format(sql.Identifier(c), sql.SQL(op)))
            params.append(v)
    q = sql.SQL("SELECT * FROM {}").format(sql.Identifier(table))
    if conds:
        q += sql.SQL(" WHERE ") + sql.SQL(" AND ").join(conds)
    q += sql.SQL(" ORDER BY {} {}").format(sql.Identifier(order), sql.SQL("DESC" if desc else "ASC"))
    if limit is not None:
        q += sql.SQL(" LIMIT %s")
        params.append(int(limit))
    return conn.execute(q, params).fetchall()


def _update(conn: Conn, table: str, id_: Any, fields: dict[str, Any]) -> Row | None:
    _check(table, fields, SPECS[table][1], "change")
    if not fields:
        return _get(conn, table, id_)
    q = sql.SQL("UPDATE {} SET {} WHERE id = %s RETURNING *").format(
        sql.Identifier(table),
        sql.SQL(", ").join(sql.SQL("{} = %s").format(sql.Identifier(c)) for c in fields))
    return conn.execute(q, [*(_val(c, v) for c, v in fields.items()), id_]).fetchone()


def _delete(conn: Conn, table: str, id_: Any) -> bool:
    return conn.execute(sql.SQL("DELETE FROM {} WHERE id = %s").format(sql.Identifier(table)), (id_,)).rowcount > 0


# ---------------------------------------------------------------------------
# owner
# ---------------------------------------------------------------------------

def create_owner(conn: Conn, **fields: Any) -> Row:
    """name, contact (masked), role: property_manager | regional_head | leadership."""
    return _insert(conn, "owner", fields)

def get_owner(conn: Conn, id: uuid.UUID) -> Row | None:
    return _get(conn, "owner", id)

def list_owners(conn: Conn, *, role: str | None = None, limit: int | None = None) -> list[Row]:
    return _find(conn, "owner", {"role": role}, limit=limit)

def update_owner(conn: Conn, id: uuid.UUID, **fields: Any) -> Row | None:
    return _update(conn, "owner", id, fields)

def delete_owner(conn: Conn, id: uuid.UUID) -> bool:
    return _delete(conn, "owner", id)


# ---------------------------------------------------------------------------
# property
# ---------------------------------------------------------------------------

def create_property(conn: Conn, **fields: Any) -> Row:
    """name, region, owner_id."""
    return _insert(conn, "property", fields)

def get_property(conn: Conn, id: uuid.UUID) -> Row | None:
    return _get(conn, "property", id)

def list_properties(conn: Conn, *, region: str | None = None, owner_id: uuid.UUID | None = None,
                    limit: int | None = None) -> list[Row]:
    return _find(conn, "property", {"region": region, "owner_id": owner_id}, limit=limit)

def update_property(conn: Conn, id: uuid.UUID, **fields: Any) -> Row | None:
    return _update(conn, "property", id, fields)

def delete_property(conn: Conn, id: uuid.UUID) -> bool:
    return _delete(conn, "property", id)


# ---------------------------------------------------------------------------
# resident
# ---------------------------------------------------------------------------

def create_resident(conn: Conn, **fields: Any) -> Row:
    """property_id, display_name, phone (masked), is_demo_user."""
    return _insert(conn, "resident", fields)

def get_resident(conn: Conn, id: uuid.UUID) -> Row | None:
    return _get(conn, "resident", id)

def list_residents(conn: Conn, *, property_id: uuid.UUID | None = None, is_demo_user: bool | None = None,
                   limit: int | None = None) -> list[Row]:
    return _find(conn, "resident", {"property_id": property_id, "is_demo_user": is_demo_user}, limit=limit)

def update_resident(conn: Conn, id: uuid.UUID, **fields: Any) -> Row | None:
    return _update(conn, "resident", id, fields)

def delete_resident(conn: Conn, id: uuid.UUID) -> bool:
    return _delete(conn, "resident", id)


# ---------------------------------------------------------------------------
# fee_charge
# ---------------------------------------------------------------------------

def create_fee_charge(conn: Conn, **fields: Any) -> Row:
    """resident_id, cycle, amount (paise), due_date, status. One per resident per cycle."""
    return _insert(conn, "fee_charge", fields)

def get_fee_charge(conn: Conn, id: uuid.UUID) -> Row | None:
    return _get(conn, "fee_charge", id)

def list_fee_charges(conn: Conn, *, resident_id: uuid.UUID | None = None, status: str | None = None,
                     due_before: dt.date | None = None, due_after: dt.date | None = None,
                     limit: int | None = None) -> list[Row]:
    return _find(conn, "fee_charge", {"resident_id": resident_id, "status": status}, order="due_date", limit=limit,
                 extra=[("due_date", "<", due_before), ("due_date", ">", due_after)])

def update_fee_charge(conn: Conn, id: uuid.UUID, **fields: Any) -> Row | None:
    return _update(conn, "fee_charge", id, fields)

def delete_fee_charge(conn: Conn, id: uuid.UUID) -> bool:
    return _delete(conn, "fee_charge", id)


# ---------------------------------------------------------------------------
# case_file
# ---------------------------------------------------------------------------

def create_case_file(conn: Conn, **fields: Any) -> Row:
    """fee_charge_id (unique: one case per fee), plus optional ladder_step, status, promise_date, ..."""
    return _insert(conn, "case_file", fields)

def get_case_file(conn: Conn, id: uuid.UUID) -> Row | None:
    return _get(conn, "case_file", id)

def list_case_files(conn: Conn, *, fee_charge_id: uuid.UUID | None = None, status: str | None = None,
                    ladder_step: int | None = None, next_action_before: dt.datetime | None = None,
                    limit: int | None = None) -> list[Row]:
    return _find(conn, "case_file", {"fee_charge_id": fee_charge_id, "status": status, "ladder_step": ladder_step},
                 order="next_action_at", limit=limit, extra=[("next_action_at", "<=", next_action_before)])

def update_case_file(conn: Conn, id: uuid.UUID, **fields: Any) -> Row | None:
    return _update(conn, "case_file", id, fields)

def delete_case_file(conn: Conn, id: uuid.UUID) -> bool:
    return _delete(conn, "case_file", id)


# ---------------------------------------------------------------------------
# message_draft
# ---------------------------------------------------------------------------

def create_message_draft(conn: Conn, **fields: Any) -> Row:
    """case_id, body, ladder_step, idempotency_key (unique), plus optional status, send_after.

    A repeated idempotency_key raises psycopg.errors.UniqueViolation: a retry cannot create a second draft.
    """
    return _insert(conn, "message_draft", fields)

def get_message_draft(conn: Conn, id: uuid.UUID) -> Row | None:
    return _get(conn, "message_draft", id)

def get_message_draft_by_key(conn: Conn, idempotency_key: str) -> Row | None:
    rows = _find(conn, "message_draft", {"idempotency_key": idempotency_key}, limit=1)
    return rows[0] if rows else None

def list_message_drafts(conn: Conn, *, case_id: uuid.UUID | None = None, status: str | None = None,
                        send_after_before: dt.datetime | None = None, limit: int | None = None) -> list[Row]:
    return _find(conn, "message_draft", {"case_id": case_id, "status": status}, order="send_after", limit=limit,
                 extra=[("send_after", "<=", send_after_before)])

def update_message_draft(conn: Conn, id: uuid.UUID, **fields: Any) -> Row | None:
    return _update(conn, "message_draft", id, fields)

def delete_message_draft(conn: Conn, id: uuid.UUID) -> bool:
    return _delete(conn, "message_draft", id)


# ---------------------------------------------------------------------------
# chat_message
# ---------------------------------------------------------------------------

def create_chat_message(conn: Conn, **fields: Any) -> Row:
    """resident_id, direction ('in' | 'out'), body. Inbound text is untrusted data."""
    return _insert(conn, "chat_message", fields)

def get_chat_message(conn: Conn, id: uuid.UUID) -> Row | None:
    return _get(conn, "chat_message", id)

def list_chat_messages(conn: Conn, *, resident_id: uuid.UUID | None = None, direction: str | None = None,
                       since: dt.datetime | None = None, limit: int | None = None,
                       newest_first: bool = False) -> list[Row]:
    return _find(conn, "chat_message", {"resident_id": resident_id, "direction": direction}, order="created_at",
                 desc=newest_first, limit=limit, extra=[("created_at", ">", since)])

def update_chat_message(conn: Conn, id: uuid.UUID, **fields: Any) -> Row | None:
    return _update(conn, "chat_message", id, fields)

def delete_chat_message(conn: Conn, id: uuid.UUID) -> bool:
    return _delete(conn, "chat_message", id)


# ---------------------------------------------------------------------------
# agent_run
# ---------------------------------------------------------------------------

def create_agent_run(conn: Conn, **fields: Any) -> Row:
    """case_id, status, plus optional step_count, checkpoint (dict)."""
    return _insert(conn, "agent_run", fields)

def get_agent_run(conn: Conn, id: uuid.UUID) -> Row | None:
    return _get(conn, "agent_run", id)

def list_agent_runs(conn: Conn, *, case_id: uuid.UUID | None = None, status: str | None = None,
                    limit: int | None = None, newest_first: bool = True) -> list[Row]:
    return _find(conn, "agent_run", {"case_id": case_id, "status": status}, order="started_at",
                 desc=newest_first, limit=limit)

def update_agent_run(conn: Conn, id: uuid.UUID, **fields: Any) -> Row | None:
    return _update(conn, "agent_run", id, fields)

def delete_agent_run(conn: Conn, id: uuid.UUID) -> bool:
    return _delete(conn, "agent_run", id)


# ---------------------------------------------------------------------------
# audit_log: create and read only. There is no update or delete, on purpose:
# the table is insert-only in the database itself (see schema.sql).
# ---------------------------------------------------------------------------

def create_audit_log(conn: Conn, **fields: Any) -> Row:
    """actor ('agent' | 'person' | 'system'), action, target, details (dict), run_id."""
    return _insert(conn, "audit_log", fields)

def get_audit_log(conn: Conn, id: int) -> Row | None:
    return _get(conn, "audit_log", id)

def list_audit_logs(conn: Conn, *, run_id: uuid.UUID | None = None, actor: str | None = None,
                    since: dt.datetime | None = None, limit: int | None = None,
                    newest_first: bool = True) -> list[Row]:
    return _find(conn, "audit_log", {"run_id": run_id, "actor": actor}, order="id", desc=newest_first, limit=limit,
                 extra=[("ts", ">", since)])


# ---------------------------------------------------------------------------
# Joined and counting queries (read-only helpers the tools need)
# ---------------------------------------------------------------------------

def list_open_fees_with_cases(conn: Conn, *, due_on_or_before: dt.date, limit: int | None = None) -> list[Row]:
    """Unpaid fees (due or overdue) with their case, if one exists. Oldest due date first."""
    q = """SELECT f.id AS fee_id, f.amount, f.due_date, f.status AS fee_status,
                   c.id AS case_id, c.ladder_step, c.status AS case_status, c.promise_date
            FROM fee_charge f LEFT JOIN case_file c ON c.fee_charge_id = f.id
            WHERE f.status IN ('due', 'overdue') AND f.due_date <= %s
            ORDER BY f.due_date, f.id"""
    params: list[Any] = [due_on_or_before]
    if limit is not None:
        q += " LIMIT %s"
        params.append(int(limit))
    return conn.execute(q, params).fetchall()


def get_case_resident(conn: Conn, case_id: uuid.UUID) -> Row | None:
    """The resident a case is about (case -> fee -> resident)."""
    return conn.execute(
        """SELECT r.* FROM case_file c JOIN fee_charge f ON f.id = c.fee_charge_id
           JOIN resident r ON r.id = f.resident_id WHERE c.id = %s""", (case_id,)).fetchone()


def list_open_drafts(conn: Conn, case_id: uuid.UUID) -> list[Row]:
    """Drafts that are still in flight: pending, approved or held (not yet sent or rejected)."""
    return conn.execute(
        "SELECT * FROM message_draft WHERE case_id = %s AND status IN ('pending', 'approved', 'held') ORDER BY send_after",
        (case_id,)).fetchall()


def count_resident_drafts_between(conn: Conn, resident_id: uuid.UUID, start: dt.datetime, end: dt.datetime) -> int:
    """Drafts scheduled in [start, end) for any of a resident's cases, excluding rejected ones."""
    return conn.execute(
        """SELECT count(*) AS n FROM message_draft d JOIN case_file c ON c.id = d.case_id
           JOIN fee_charge f ON f.id = c.fee_charge_id
           WHERE f.resident_id = %s AND d.status <> 'rejected' AND d.send_after >= %s AND d.send_after < %s""",
        (resident_id, start, end)).fetchone()["n"]


# ---------------------------------------------------------------------------
# Scheduler queries (run as the read-only 'worker' role, across all properties)
# ---------------------------------------------------------------------------

def list_cases_to_work(conn: Conn, *, now: dt.datetime, limit: int = 50) -> list[Row]:
    """Active cases on unpaid fees whose next_action_at has come. Returns case_id and property_id only."""
    return conn.execute(
        """SELECT c.id AS case_id, r.property_id FROM case_file c
           JOIN fee_charge f ON f.id = c.fee_charge_id JOIN resident r ON r.id = f.resident_id
           WHERE c.status IN ('open', 'promised') AND f.status IN ('due', 'overdue') AND c.next_action_at <= %s
           ORDER BY c.next_action_at, c.id LIMIT %s""", (now, int(limit))).fetchall()


def list_due_held_drafts(conn: Conn, *, now: dt.datetime, limit: int = 100) -> list[Row]:
    """Approved drafts held overnight whose send time has come. Returns draft_id and property_id only."""
    return conn.execute(
        """SELECT d.id AS draft_id, r.property_id FROM message_draft d
           JOIN case_file c ON c.id = d.case_id JOIN fee_charge f ON f.id = c.fee_charge_id
           JOIN resident r ON r.id = f.resident_id
           WHERE d.status = 'held' AND d.send_after <= %s ORDER BY d.send_after LIMIT %s""", (now, int(limit))).fetchall()
