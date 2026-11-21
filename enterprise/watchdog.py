"""The watchdog: something OUTSIDE the workflow that notices when cases stop moving.

It reads the case registry (a database table written when a case starts and when it finishes), not the message bus,
so it still works when the bus, the orchestrator or a department is dead. Every `watchdog_interval` seconds it looks for
cases that have been open longer than `watchdog_timeout` and records itself as the one who noticed.

Why it is a flag: in orchestrated modes the orchestrator notices a dead DEPARTMENT by itself (it times out and retries),
but nobody notices a dead orchestrator. In choreography nobody owns the whole case, so nobody notices anything. Without
this, a stalled case is silent.
"""
from __future__ import annotations


class Watchdog:
    def __init__(self, sw) -> None:
        self.sw, self.s = sw, sw.settings

    def start(self) -> None:
        self.sw.sim.at(self.s.watchdog_interval, self.check)

    def check(self) -> None:
        now = self.sw.sim.now
        for row in self.sw.registry.all():
            if row["status"] == "open" and row["noticed_by"] is None and now - row["started"] > self.s.watchdog_timeout:
                self.sw.registry.notice(row["case_id"], "watchdog", f"open for {now - row['started']:.0f}s with no progress")
                self.sw.w.stats["watchdog_flags"] += 1
        self.sw.sim.at(self.s.watchdog_interval, self.check)
