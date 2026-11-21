"""Chaos: kill the coordinator, kill a department, add latency, duplicate messages. Who notices, and what survives."""
import pytest

from enterprise.settings import Chaos, Settings
from helpers import MODES, by_case, run

COORD = {"central": "orchestrator", "hierarchical": "top", "choreography": "bus"}


def stalled_all(res, n):
    return res["outcomes"] == {"completed": 0, "escalated_failure": 0, "stalled": n}


# -- central ---------------------------------------------------------------------------------------

def test_central_killing_the_orchestrator_stalls_every_case_and_the_monitor_sees_it_within_the_timeout():
    sim, res = run("central", "rules", cases=10, chaos=Chaos(kill=("orchestrator",)), sim_only=True)
    s = sim.settings
    assert stalled_all(res, 10) and s.watchdog_on()                        # the monitor is on by default for central
    assert res["noticed"]["by"] == {"watchdog": 10}
    for r in sim.registry.all():
        waited = r["noticed_at"] - r["started"]
        assert s.watchdog_timeout < waited <= s.watchdog_timeout + s.watchdog_interval, waited   # within the timeout, not before


def test_central_without_the_monitor_a_dead_orchestrator_is_silent():
    res = run("central", "rules", cases=10, chaos=Chaos(kill=("orchestrator",)), watchdog=False)
    assert stalled_all(res, 10) and res["noticed"]["by"] == {"nobody": 10}


def test_central_killing_finance_is_noticed_by_the_orchestrator_which_retries_then_escalates():
    sim, res = run("central", "rules", cases=10, chaos=Chaos(kill=("finance",)), watchdog=False, sim_only=True)
    s = sim.settings
    assert res["outcomes"] == {"completed": 0, "escalated_failure": 10, "stalled": 0}
    assert res["noticed"]["by"] == {"orchestrator": 10}                    # no monitor needed
    assert res["faults"]["retries"] == 10 * s.max_retries                  # each case retried Finance, then gave up
    for r in sim.registry.all():
        assert r["status"] == "escalated_failure" and "vote.finance" in r["noticed_what"]
        assert r["noticed_at"] - r["started"] < (s.max_retries + 1) * s.step_timeout + 5     # noticed after the last retry, not before
    assert len(sim.w.store.scan("orch", "incident")) == 10                 # and a person has something to act on
    assert res["effects"] == {"waivers": 0, "notices": 0}                  # nothing was done without Finance's say-so


# -- hierarchical -------------------------------------------------------------------------------------

def test_hierarchical_killing_the_top_stalls_everything_but_a_dead_finance_is_caught_by_its_domain_orchestrator():
    top = run("hierarchical", "rules", cases=10, chaos=Chaos(kill=("top",)))
    assert stalled_all(top, 10) and top["noticed"]["by"] == {"watchdog": 10}
    fin = run("hierarchical", "rules", cases=10, chaos=Chaos(kill=("finance",)), watchdog=False)
    assert fin["outcomes"]["escalated_failure"] == 10 and fin["noticed"]["by"] == {"risk": 10}   # the domain orchestrator saw it first


def test_hierarchical_a_dead_domain_orchestrator_is_caught_by_the_top_after_its_own_timeout():
    res = run("hierarchical", "rules", cases=6, chaos=Chaos(kill=("risk",)), watchdog=False)
    assert res["outcomes"]["escalated_failure"] == 6 and res["noticed"]["by"] == {"top": 6}


# -- choreography -----------------------------------------------------------------------------------------

def test_choreography_killing_finance_stalls_silently_until_the_watchdog_is_turned_on():
    silent = run("choreography", "rules", cases=10, chaos=Chaos(kill=("finance",)))
    assert stalled_all(silent, 10) and silent["noticed"]["by"] == {"nobody": 10}
    assert silent["faults"]["retries"] == 0 and silent["faults"]["escalations"] == 0      # nothing even tried
    sim, loud = run("choreography", "rules", cases=10, chaos=Chaos(kill=("finance",)), watchdog=True, sim_only=True)
    assert stalled_all(loud, 10) and loud["noticed"]["by"] == {"watchdog": 10}
    assert all(r["noticed_at"] - r["started"] <= sim.settings.watchdog_timeout + sim.settings.watchdog_interval
               for r in sim.registry.all())


def test_choreography_the_dead_departments_backlog_is_visible_as_consumer_lag():
    sim, _ = run("choreography", "rules", cases=10, chaos=Chaos(kill=("finance",)), sim_only=True)
    log = sim.net.log
    assert log.lag("finance") > 0 and log.lag("ops") == log.lag("legal") == 0       # the number a real alert would watch


def test_the_bus_is_a_single_point_of_failure_for_every_mode_and_only_an_out_of_band_watchdog_sees_it():
    for mode in MODES:
        res = run(mode, "rules", cases=6, chaos=Chaos(kill=("bus",)), watchdog=True)
        assert stalled_all(res, 6) and res["faults"]["lost_bus_down"] > 0 and res["noticed"]["by"] == {"watchdog": 6}, mode


def test_a_kill_part_way_through_lets_the_early_cases_finish():
    res = run("central", "priority", cases=20, chaos=Chaos(kill=("orchestrator",), kill_at=30.0), watchdog=True)
    assert 0 < res["outcomes"]["completed"] < 20 and res["outcomes"]["stalled"] > 0


def test_killing_something_that_does_not_exist_is_an_error():
    with pytest.raises(ValueError, match="cannot kill"):
        run("central", chaos=Chaos(kill=("finanse",)))


# -- latency ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("mode", MODES)
def test_extra_network_latency_slows_every_mode_by_its_hop_count(mode):
    base, slow = run(mode), run(mode, chaos=Chaos(net_latency=2.0))
    assert slow["completion_seconds"]["p50"] > base["completion_seconds"]["p50"] + 4.0
    assert slow["outcomes"]["completed"] == 16


def test_more_hops_means_more_exposure_to_latency():
    cost = {m: run(m, chaos=Chaos(net_latency=2.0))["completion_seconds"]["p50"] - run(m)["completion_seconds"]["p50"] for m in MODES}
    assert cost["choreography"] < cost["central"] < cost["hierarchical"]


def test_a_slow_department_makes_the_orchestrator_retry_and_idempotency_keeps_the_effects_single():
    # Finance takes ~26s per job and the orchestrator waits 15s: it retries while the first answer is still on its way.
    res = run("central", "priority", cases=8, chaos=Chaos(dept_latency={"finance": 25.0}), step_timeout=15.0, arrival_gap=40.0)
    assert res["faults"]["retries"] > 0 and res["outcomes"]["completed"] == 8
    calm = run("central", "priority", cases=8, arrival_gap=40.0)
    assert res["duplicates"]["cases_with_double_waiver"] == 0 and res["effects"] == calm["effects"]


# -- duplicates --------------------------------------------------------------------------------------------

@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("strategy", ["priority", "rules"])
@pytest.mark.parametrize("every", [1, 2, 3])
def test_a_duplicate_message_never_causes_a_second_waiver_or_notice(mode, strategy, every):
    base_sim, base = run(mode, strategy, cases=24, sim_only=True)
    sim, dup = run(mode, strategy, cases=24, chaos=Chaos(duplicate_every=every), sim_only=True)
    assert dup["duplicates"]["injected"] > 0
    assert dup["duplicates"]["cases_with_double_waiver"] == 0 and dup["duplicates"]["cases_with_double_notice"] == 0
    assert dup["effects"] == base["effects"]                                  # exactly the same waivers and notices
    assert dup["outcomes"] == base["outcomes"]
    assert {c: v["outcome"] for c, v in by_case(dup).items()} == {c: v["outcome"] for c, v in by_case(base).items()}
    for c in sim.cases:                                                        # the ledger moved once, not twice
        waived = sim.w.store.get("owner", "ledger", c.resident_id, dept="finance")["waived_total"]
        assert waived in (0, c.fee)


@pytest.mark.parametrize("mode", MODES)
def test_even_with_duplicates_the_reply_is_drafted_only_after_the_waiver_or_notice_step(mode):
    """A duplicated human decision must not push a case a stage too far. Outcomes alone would not show it:
    the reply would just be drafted early. So check the order of the tool calls."""
    sim, _ = run(mode, "rules", cases=24, chaos=Chaos(duplicate_every=1), sim_only=True)
    calls = [(r["details"]["task"], r["target"]) for r in sim.w.store.audit_rows if r["action"] == "tool_call"]
    for c in sim.cases:
        mine = [t for task, t in calls if task == c.case_id]
        acted = [i for i, t in enumerate(mine) if t in ("finance.apply_waiver", "legal.serve_notice")]
        reply = mine.index("collections.draft_reply")
        assert all(i < reply for i in acted), (c.case_id, mine)


def test_the_same_duplicates_do_double_waive_when_idempotency_is_switched_off():
    """The control: proves the test above is not passing by accident."""
    res = run("central", "priority", cases=24, chaos=Chaos(duplicate_every=1), idempotent=False)
    assert res["duplicates"]["cases_with_double_waiver"] > 0


def test_duplicates_are_visible_as_suppressed_work_not_hidden():
    res = run("central", "priority", cases=12, chaos=Chaos(duplicate_every=1))
    assert res["duplicates"]["suppressed"] > 0 and res["messages"] > run("central", "priority", cases=12)["messages"]


# -- the whole thing is repeatable ----------------------------------------------------------------------------

def test_a_chaos_run_is_as_repeatable_as_a_calm_one():
    a = run("hierarchical", "rules", cases=12, chaos=Chaos(kill=("finance",), duplicate_every=2, net_latency=0.5))
    b = run("hierarchical", "rules", cases=12, chaos=Chaos(kill=("finance",), duplicate_every=2, net_latency=0.5))
    assert a == b
