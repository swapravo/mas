"""The case registry: one row per case, written by whoever starts and finishes it.

It lives in the database (control-plane role `orch`), OUTSIDE the message bus, on purpose: a watchdog that read
the bus could not see a dead bus. States: open, awaiting_human, completed, escalated_failure. A case that
is still `open` at the end of the run is stalled. `noticed_by` is the FIRST thing that noticed a failure.
"""
from __future__ import annotations

KIND = "case_registry"
TERMINAL = ("completed", "escalated_failure")


class Registry:
    def __init__(self, store, clock) -> None:
        self.store, self.clock = store, clock
        self._known: dict[str, dict] = {}

    def start(self, case, deadline: float) -> None:
        self._known[case.case_id] = {"goal": case.goal, "on_behalf_of": case.principal}
        self.store.put("orch", KIND, case.case_id, {
            "case_id": case.case_id, "status": "open", "started": self.clock(), "deadline": deadline,
            "noticed_by": None, "noticed_at": None, "noticed_what": None, "finished": None})

    def known(self, task_id: str) -> dict | None:
        """What the envelope validator needs to check goal and person (kept in memory, mirrors the rows)."""
        return self._known.get(task_id)

    def get(self, case_id: str) -> dict | None:
        return self.store.get("orch", KIND, case_id)

    def update(self, case_id: str, **fields) -> None:
        row = self.get(case_id)
        if row and row["status"] not in TERMINAL:
            self.store.put("orch", KIND, case_id, {**row, **fields})

    def annotate(self, case_id: str, **fields) -> None:
        """Add facts to a case at any time, even after it is finished (e.g. the reply was approved later)."""
        row = self.get(case_id)
        if row:
            self.store.put("orch", KIND, case_id, {**row, **fields})

    def finish(self, case_id: str, status: str, **fields) -> None:
        row = self.get(case_id)
        if row and row["status"] not in TERMINAL:
            self.store.put("orch", KIND, case_id, {**row, "status": status, "finished": self.clock(), **fields})

    def notice(self, case_id: str, who: str, what: str) -> None:
        """Record who saw the failure first. Later noticers do not overwrite it."""
        row = self.get(case_id)
        if row and row["noticed_by"] is None:
            self.store.put("orch", KIND, case_id, {**row, "noticed_by": who, "noticed_at": self.clock(), "noticed_what": what})

    def all(self) -> list[dict]:
        return [r["payload"] for r in self.store.scan("orch", KIND)]
