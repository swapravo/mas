"""Celery for the Enterprise layer: ONE QUEUE PER DEPARTMENT (dept.collections, dept.ops, dept.finance, dept.legal).

Same settings as ../celery_app.py (acks_late so a crashed worker's task goes back on the queue, prefetch 1 because model
calls are slow). The task body is the same Department.handle the simulator runs; here it runs in a real worker process
against real Postgres (RLS) and real Redis (idempotency keys).

    celery -A enterprise.celery_app worker -Q dept.finance -n finance@%h --pool solo --loglevel INFO     (one per department)
    python -m enterprise.live --cases 3              # the live demo; or add --spawn-workers to start them for you

Stop one worker and its queue just fills up (redis-cli LLEN dept.finance): that is what "a dead department" looks like.
"""
from __future__ import annotations

import time

import redis
from celery import Celery

import config
from enterprise.envelope import DEPT_NAMES, parse
from enterprise.kv import RedisKV
from enterprise.settings import Settings

QUEUES = {d: f"dept.{d}" for d in DEPT_NAMES}

app = Celery("mas_enterprise", broker=config.REDIS_URL, backend=config.REDIS_URL)
app.conf.update(
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    broker_connection_retry_on_startup=True,
    result_expires=3600,
    task_routes={f"enterprise.dept.{d}": {"queue": QUEUES[d]} for d in DEPT_NAMES},
)

_world = None


def world():
    """Built once per worker process: real Postgres store, real Redis keys, the wall clock."""
    global _world
    if _world is None:
        from enterprise.pgstore import PgStore
        from enterprise.world import World
        _world = World(Settings(), store=PgStore(), kv=RedisKV(redis.Redis.from_url(config.REDIS_URL)))
        _world.clock = time.time
    return _world


def _make_task(dept: str):
    @app.task(name=f"enterprise.dept.{dept}")
    def handle(wire: dict) -> dict:
        """Validate the envelope, run the department's agent for it, return the reply."""
        w = world()
        env = parse(wire, now=w.clock(), recipient=dept, max_hops=w.settings.max_hops)
        r = w.depts[dept].handle(env)
        return {"ok": r.ok, "result": r.payload, "error": r.error, "calls": r.calls, "cached": r.cached}
    return handle


TASKS = {d: _make_task(d) for d in DEPT_NAMES}
