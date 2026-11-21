"""A tiny key-value store for idempotency keys and per-task budget counters.

Two backends, one shape (this is the Redis shape: SET NX, GET, INCRBY):
  MemoryKV - offline, deterministic
  RedisKV  - real Redis (or fakeredis in tests)
"""
from __future__ import annotations


class MemoryKV:
    def __init__(self) -> None:
        self._d: dict[str, str] = {}

    def setnx(self, key: str, value: str) -> bool:
        """Set only if absent. True means we are the first (we own this step)."""
        if key in self._d:
            return False
        self._d[key] = value
        return True

    def get(self, key: str) -> str | None:
        return self._d.get(key)

    def set(self, key: str, value: str) -> None:
        self._d[key] = value

    def delete(self, key: str) -> None:
        self._d.pop(key, None)

    def incr(self, key: str, n: int = 1) -> int:
        self._d[key] = str(int(self._d.get(key, "0")) + n)
        return int(self._d[key])


class RedisKV:
    def __init__(self, client, prefix: str = "ent:") -> None:
        self.r, self.p = client, prefix

    def setnx(self, key: str, value: str) -> bool:
        return bool(self.r.set(self.p + key, value, nx=True))

    def get(self, key: str) -> str | None:
        v = self.r.get(self.p + key)
        return v.decode() if isinstance(v, bytes) else v

    def set(self, key: str, value: str) -> None:
        self.r.set(self.p + key, value)

    def delete(self, key: str) -> None:
        self.r.delete(self.p + key)

    def incr(self, key: str, n: int = 1) -> int:
        return int(self.r.incrby(self.p + key, n))

    def clear(self) -> None:
        """Forget every key this class wrote (its prefix), so a run starts clean."""
        for k in self.r.scan_iter(match=self.p + "*"):
            self.r.delete(k)
