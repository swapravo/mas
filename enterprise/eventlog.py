"""The event stream used by choreography: a Redis-Streams-shaped log.

  publish(wire)        -> entry id          (XADD)
  create_group(g)                            (XGROUP CREATE)
  read_group(g)        -> [(id, wire)]       entries this group has not been given yet (XREADGROUP >)
  ack(g, id)                                 (XACK)
  lag(g)               -> int                entries the group has not read: the number to alert on

MemoryEventLog is for offline runs. RedisEventLog is the same thing on real Redis (or fakeredis in tests).
The simulator supplies the time; the log only supplies the order.
"""
from __future__ import annotations

import json


class MemoryEventLog:
    def __init__(self) -> None:
        self.entries: list[tuple[str, dict]] = []
        self.cursor: dict[str, int] = {}
        self.acked: dict[str, set[str]] = {}

    def create_group(self, group: str) -> None:
        self.cursor.setdefault(group, 0)
        self.acked.setdefault(group, set())

    def publish(self, wire: dict) -> str:
        entry_id = f"{len(self.entries) + 1}-0"
        self.entries.append((entry_id, json.loads(json.dumps(wire))))
        return entry_id

    def read_group(self, group: str) -> list[tuple[str, dict]]:
        out = self.entries[self.cursor[group]:]
        self.cursor[group] = len(self.entries)
        return out

    def ack(self, group: str, entry_id: str) -> None:
        self.acked[group].add(entry_id)

    def lag(self, group: str) -> int:
        return len(self.entries) - self.cursor[group]

    def pending(self, group: str) -> int:
        return self.cursor[group] - len(self.acked[group])


class RedisEventLog:
    def __init__(self, client, stream: str = "ent:events", reset: bool = True) -> None:
        self.r, self.stream = client, stream
        if reset:
            self.r.delete(stream)

    def create_group(self, group: str) -> None:
        try:
            self.r.xgroup_create(self.stream, group, id="0", mkstream=True)
        except Exception as e:                           # group already exists
            if "BUSYGROUP" not in str(e):
                raise

    def publish(self, wire: dict) -> str:
        entry_id = self.r.xadd(self.stream, {"data": json.dumps(wire)})
        return entry_id.decode() if isinstance(entry_id, bytes) else entry_id

    def read_group(self, group: str) -> list[tuple[str, dict]]:
        resp = self.r.xreadgroup(group, f"{group}-1", {self.stream: ">"}, count=10_000)
        out = []
        for _, entries in resp or []:
            for entry_id, fields in entries:
                raw = fields.get(b"data", fields.get("data"))
                out.append((entry_id.decode() if isinstance(entry_id, bytes) else entry_id,
                            json.loads(raw.decode() if isinstance(raw, bytes) else raw)))
        return out

    def ack(self, group: str, entry_id: str) -> None:
        self.r.xack(self.stream, group, entry_id)

    def lag(self, group: str) -> int:
        for g in self.r.xinfo_groups(self.stream):
            name = g.get("name")
            if (name.decode() if isinstance(name, bytes) else name) == group:
                return int(g.get("lag") or 0)
        return 0

    def pending(self, group: str) -> int:
        return int(self.r.xpending(self.stream, group)["pending"])
