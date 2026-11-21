"""Real queues: Celery workers (one per department), real Redis, real Postgres. Skipped unless all are reachable."""
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))


@pytest.fixture(scope="module")
def services():
    admin = os.environ.get("ENT_PG_ADMIN_DSN")
    if not admin or not os.environ.get("ENT_PG_DSN"):
        pytest.skip("set ENT_PG_ADMIN_DSN and ENT_PG_DSN to run the live (Celery + Redis + Postgres) tests")
    try:
        import redis
        import config
        redis.Redis.from_url(config.REDIS_URL, socket_connect_timeout=0.5).ping()
        from enterprise.pgstore import apply_schema
        apply_schema(admin)
    except Exception as exc:
        pytest.skip(f"redis or postgres not available: {exc}")


def live(*args):
    p = subprocess.run([sys.executable, "-m", "enterprise.live", "--spawn-workers", *args], cwd=ROOT, capture_output=True,
                       text=True, timeout=150, env={**os.environ, "PYTHONPATH": ROOT})
    assert p.returncode == 0, p.stderr[-800:]
    return p.stdout


def test_cases_complete_through_real_celery_queues_one_per_department(services):
    out = live("--cases", "4")
    assert out.count("-> completed") == 4 and "effects: 2 waivers" in out
    assert "Traceback" not in out


def test_a_missing_department_worker_means_timeout_retry_then_escalation_and_no_effects(services):
    out = live("--cases", "2", "--kill", "finance", "--timeout", "2", "--retries", "1")
    assert out.count("-> escalated_failure") == 2 and "finance did not answer" in out and "retry 1/1 (same key)" in out
    assert "effects: 0 waivers, 0 notices" in out
