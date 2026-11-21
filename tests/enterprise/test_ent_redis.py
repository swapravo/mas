"""Redis: the event stream and the idempotency keys on a real Redis API.

Offline these run on fakeredis (same API, in-process). If a real Redis answers on REDIS_URL they run on it too.
The point: results must not change when the in-memory stand-ins are swapped for Redis.
"""
import pytest

fakeredis = pytest.importorskip("fakeredis")
redis = pytest.importorskip("redis")

from enterprise.eventlog import MemoryEventLog, RedisEventLog   # noqa: E402
from enterprise.kv import MemoryKV, RedisKV                      # noqa: E402
from enterprise.runner import Simulation                         # noqa: E402
from enterprise.settings import Chaos, Settings                  # noqa: E402


def real_redis():
    import config
    try:
        c = redis.Redis.from_url(config.REDIS_URL, socket_connect_timeout=0.5)
        c.ping()
        return c
    except Exception:
        return None


@pytest.fixture(params=["fakeredis", "real"])
def client(request):
    if request.param == "fakeredis":
        return fakeredis.FakeRedis()
    c = real_redis()
    if c is None:
        pytest.skip("no Redis on REDIS_URL")
    return c


def test_event_log_has_the_stream_semantics_we_rely_on(client):
    for log in (MemoryEventLog(), RedisEventLog(client, stream="ent:test:semantics")):
        log.create_group("a"), log.create_group("b")
        ids = [log.publish({"n": i}) for i in range(3)]
        assert log.lag("a") == 3
        got = log.read_group("a")
        assert [w["n"] for _, w in got] == [0, 1, 2] and log.lag("a") == 0 and log.lag("b") == 3     # groups are independent
        assert log.read_group("a") == []                                                              # each entry is given once
        log.ack("a", ids[0])
        assert log.pending("a") == 2                                                                  # delivered but not acked
    client.delete("ent:test:semantics")


def test_kv_keeps_the_set_if_absent_and_counter_semantics(client):
    for kv in (MemoryKV(), RedisKV(client, prefix="ent:test:kv:")):
        assert kv.setnx("k", "1") is True and kv.setnx("k", "2") is False and kv.get("k") == "1"
        assert kv.incr("n", 3) == 3 and kv.incr("n") == 4
        kv.delete("k")
        assert kv.get("k") is None and kv.setnx("k", "3") is True
    RedisKV(client, prefix="ent:test:kv:").clear()
    assert client.keys("ent:test:kv:*") == []


@pytest.mark.parametrize("mode", ["central", "hierarchical", "choreography"])
def test_a_run_on_redis_gives_exactly_the_same_results_as_a_run_in_memory(client, mode):
    s = lambda: Settings(mode=mode, strategy="rules", cases=16, attack_every=8)      # noqa: E731
    chaos = lambda: Chaos(duplicate_every=2)                                          # noqa: E731
    kv = RedisKV(client, prefix=f"ent:test:{mode}:")
    kv.clear()
    log = RedisEventLog(client, stream=f"ent:test:{mode}:events")
    on_redis = Simulation(s(), chaos(), kv=kv, log=log).run()
    assert on_redis == Simulation(s(), chaos()).run()
    kv.clear()
    client.delete(f"ent:test:{mode}:events")


def test_on_redis_the_dead_department_shows_up_as_consumer_lag(client):
    log = RedisEventLog(client, stream="ent:test:lag")
    sim = Simulation(Settings(mode="choreography", cases=6), Chaos(kill=("finance",)), log=log)
    sim.run()
    assert log.lag("finance") > 0 and log.lag("ops") == 0 and log.lag("legal") == 0
    client.delete("ent:test:lag")
