"""Department-scoped data: ONE table, `ent_record(dept, kind, key, payload)`, and one rule:
a department role only sees and writes rows stamped with its own department (row-level security).

MemoryStore emulates that rule so tests need no database. PgStore (pgstore.py) runs the real
Postgres policies from schema.sql. Same three methods either way: put, get, scan.

Roles: collections, ops, finance, legal, orch (the control plane), and `owner`, which bypasses
the rule (like a table owner in Postgres). With rls=False EVERY role behaves like the owner:
that is the "everyone shares one database account" mistake, used to show what RLS is worth.

Leak bookkeeping: whenever a non-owner role is handed a row from another department, that is
recorded in `leaks`. It is how the attack tests know data crossed a line.
"""
from __future__ import annotations

import copy
from typing import Any

ROLES = ("collections", "ops", "finance", "legal", "orch")


class RowLevelDenied(PermissionError):
    pass


class MemoryStore:
    def __init__(self, rls: bool = True) -> None:
        self.rls = rls
        self._rows: dict[tuple[str, str, str], Any] = {}
        self.audit_rows: list[dict] = []        # insert-only: there is no update or delete method
        self.leaks: list[dict] = []

    # -- the rule ------------------------------------------------------------
    def _sees(self, role: str, dept: str) -> bool:
        return role == "owner" or not self.rls or role == dept

    # -- the three methods ---------------------------------------------------
    def put(self, role: str, kind: str, key: str, payload: dict, dept: str | None = None) -> None:
        dept = dept or role
        if not self._sees(role, dept):
            raise RowLevelDenied(f"row violates row-level security: role {role} cannot write dept {dept}")
        self._rows[(dept, kind, key)] = copy.deepcopy(payload)

    def get(self, role: str, kind: str, key: str, dept: str | None = None) -> dict | None:
        dept = dept if dept is not None else (None if role == "owner" else role)
        if dept is None:                                   # owner, any department: look through them all
            rows = [r for r in self.scan(role, kind) if r["key"] == key]
            return rows[0]["payload"] if rows else None
        if not self._sees(role, dept) or (dept, kind, key) not in self._rows:
            return None                                    # row-level security: a hidden row simply is not there
        if role != "owner" and dept != role:
            self.leaks.append({"layer": "rls", "role": role, "kind": kind, "key": key, "row_dept": dept})
        return copy.deepcopy(self._rows[(dept, kind, key)])

    def scan(self, role: str, kind: str, dept: str | None = None) -> list[dict]:
        """Rows of `kind` this role may see. `dept` narrows to one department (default: your own)."""
        want = dept if dept is not None else (None if role == "owner" else role)
        out = []
        for (d, k, key), payload in self._rows.items():
            if k != kind or (want is not None and d != want) or not self._sees(role, d):
                continue
            out.append({"dept": d, "kind": k, "key": key, "payload": copy.deepcopy(payload)})
            if role != "owner" and d != role:
                self.leaks.append({"layer": "rls", "role": role, "kind": k, "key": key, "row_dept": d})
        return out

    def audit(self, actor: str, action: str, target: str, details: dict | None = None) -> None:
        self.audit_rows.append({"id": len(self.audit_rows) + 1, "actor": actor, "action": action,
                                "target": target, "details": details or {}})
