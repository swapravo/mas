"""PgStore: the same three methods as MemoryStore, on real Postgres with the policies from schema.sql.

Connection strings come from one DSN plus a role name:
    ENT_PG_DSN="host=localhost port=5432 dbname=mas_enterprise"       (role and password are added per department)
With rls=False every role connects as ent_owner (the table owner bypasses RLS): "everyone shares one account".
"""
from __future__ import annotations

import json
import os

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from enterprise.store import RowLevelDenied

SCHEMA = os.path.join(os.path.dirname(__file__), "schema.sql")


def default_dsn() -> str:
    return os.environ.get("ENT_PG_DSN", "host=localhost port=5432 dbname=mas_enterprise")


def apply_schema(admin_dsn: str) -> None:
    """(Re)create the ent_* objects. Needs a role that can create roles."""
    with psycopg.connect(admin_dsn, autocommit=True) as c:
        c.execute(open(SCHEMA).read())


class PgStore:
    def __init__(self, dsn: str | None = None, rls: bool = True) -> None:
        self.dsn, self.rls = dsn or default_dsn(), rls
        self._conns: dict[str, psycopg.Connection] = {}
        self.leaks: list[dict] = []

    def _conn(self, role: str) -> psycopg.Connection:
        role = role if (self.rls and role != "owner") else "owner"
        if role not in self._conns:
            self._conns[role] = psycopg.connect(f"{self.dsn} user=ent_{role} password=ent_{role}_pw",
                                                autocommit=True, row_factory=dict_row)
        return self._conns[role]

    def close(self) -> None:
        for c in self._conns.values():
            c.close()
        self._conns.clear()

    def reset(self) -> None:
        """Empty the data table between tests (the audit log is insert-only and stays)."""
        self._conn("owner").execute("TRUNCATE ent_record")

    # -- the three methods -------------------------------------------------------
    def put(self, role: str, kind: str, key: str, payload: dict, dept: str | None = None) -> None:
        try:
            self._conn(role).execute(
                "INSERT INTO ent_record (dept, kind, key, payload) VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (dept, kind, key) DO UPDATE SET payload = EXCLUDED.payload",
                (dept or role, kind, key, Jsonb(payload)))
        except psycopg.errors.InsufficientPrivilege as e:
            raise RowLevelDenied(str(e).splitlines()[0]) from e

    def get(self, role: str, kind: str, key: str, dept: str | None = None) -> dict | None:
        rows = [r for r in self.scan(role, kind, dept) if r["key"] == key]
        return rows[0]["payload"] if rows else None

    def scan(self, role: str, kind: str, dept: str | None = None) -> list[dict]:
        want = dept if dept is not None else (None if role == "owner" else role)
        sql, args = "SELECT dept, kind, key, payload FROM ent_record WHERE kind = %s", [kind]
        if want is not None:
            sql, args = sql + " AND dept = %s", [*args, want]
        rows = self._conn(role).execute(sql + " ORDER BY dept, key", args).fetchall()
        for r in rows:
            if role != "owner" and r["dept"] != role:
                self.leaks.append({"layer": "rls", "role": role, "kind": r["kind"], "key": r["key"], "row_dept": r["dept"]})
        return rows

    def audit(self, actor: str, action: str, target: str, details: dict | None = None) -> None:
        self._conn("orch").execute("INSERT INTO ent_audit (actor, action, target, details) VALUES (%s, %s, %s, %s)",
                                   (actor, action, target, Jsonb(details or {}, dumps=lambda o: json.dumps(o, default=str))))

    @property
    def audit_rows(self) -> list[dict]:
        return self._conn("owner").execute("SELECT id, actor, action, target, details FROM ent_audit ORDER BY id").fetchall()
