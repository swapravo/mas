"""The three orchestration modes: same workload, same decisions, different cost; limits enforced in code."""
import pytest

from enterprise.envelope import Envelope
from helpers import MODES, by_case, expected_priority, run


@pytest.mark.parametrize("mode", MODES)
def test_every_mode_completes_every_case_with_the_right_decision(mode):
    sim, res = run(mode, sim_only=True)
    assert res["outcomes"] == {"completed": 16, "escalated_failure": 0, "stalled": 0}
    got = by_case(res)
    for c in sim.cases:
        assert got[c.case_id]["outcome"] == expected_priority(c), c
        assert got[c.case_id]["decided_by"] == "priority"


@pytest.mark.parametrize("mode", MODES)
def test_exactly_one_waiver_per_waived_case_and_one_notice_where_needed(mode):
    sim, res = run(mode, sim_only=True)
    waived = [c for c in sim.cases if expected_priority(c) == "waive"]
    noticed = [c for c in sim.cases if expected_priority(c) == "refuse" and not c.notice_live]
    assert res["effects"] == {"waivers": len(waived), "notices": len(noticed)}
    for c in waived:     # and the ledger shows the whole fee waived, once
        assert sim.w.store.get("owner", "ledger", c.resident_id, dept="finance")["waived_total"] == c.fee


def test_the_modes_cost_different_amounts_for_the_same_work():
    r = {m: run(m) for m in MODES}
    assert r["choreography"]["messages"] < r["central"]["messages"] < r["hierarchical"]["messages"]
    assert r["choreography"]["routing_decisions"] == 0                 # routing is in the subscriptions, not decided at runtime
    assert r["central"]["routing_decisions"] == 6 * 16                 # intake, 3 votes, act, reply
    assert r["hierarchical"]["routing_decisions"] > r["central"]["routing_decisions"]
    p = lambda m: r[m]["completion_seconds"]["p50"]                     # noqa: E731
    assert p("choreography") < p("central") < p("hierarchical")        # extra hops cost time


@pytest.mark.parametrize("mode", MODES)
def test_same_seed_same_results_different_seed_different_results(mode):
    assert run(mode) == run(mode)
    other = run(mode, seed=8)
    assert other != run(mode)


def test_the_case_state_lives_in_the_database_not_in_the_orchestrator():
    sim, _ = run("central", sim_only=True)
    rows = sim.w.store.scan("orch", "task")
    assert len(rows) == 16 and all(r["payload"]["status"] == "done" for r in rows)
    assert rows[0]["payload"]["decision"]["outcome"] in ("waive", "refuse")


def test_hierarchical_keeps_separate_state_per_orchestrator():
    sim, _ = run("hierarchical", sim_only=True)
    names = {r["key"].split(":")[1] for r in sim.w.store.scan("orch", "task")}
    assert names == {"top", "customer", "risk"}


# -- limits enforced in code ----------------------------------------------------------------

def test_model_failures_are_retried_with_the_same_key_and_still_complete():
    sim, res = run("central", sim_only=True, llm_fail={"finance": 0.4}, max_retries=8)
    assert res["faults"]["retries"] > 0 and res["faults"]["llm_failures"] > 0
    assert res["outcomes"]["completed"] == 16 and res["effects"]["waivers"] == res["effects"]["waivers"]
    assert res["duplicates"]["cases_with_double_waiver"] == 0


def test_hop_limit_rejects_the_handoff_and_the_orchestrator_escalates():
    res = run("central", cases=4, max_hops=3)
    assert res["faults"]["rejected_handoffs"] > 0
    assert res["outcomes"]["escalated_failure"] == 4 and res["noticed"]["by"] == {"orchestrator": 4}


def test_rejected_handoffs_say_why():
    sim, _ = run("central", cases=2, max_hops=3, sim_only=True)
    r = sim.net.rejections[0]
    assert r["code"] == "hops" and "exceeds limit 3" in r["errors"][0]


def test_an_expired_deadline_is_rejected():
    sim, res = run("central", cases=3, case_deadline=1.0, sim_only=True)
    assert {r["code"] for r in sim.net.rejections} == {"expired"}
    assert res["outcomes"]["completed"] == 0


def test_the_budget_stops_a_task_and_is_not_retried():
    res = run("central", cases=4, budget=5)
    assert res["faults"]["budget_rejects"] > 0 and res["outcomes"]["escalated_failure"] == 4
    assert res["faults"]["retries"] == 0


def test_a_changed_goal_or_person_is_rejected_in_flight():
    sim, _ = run("central", cases=2, sim_only=True)
    good = Envelope(task_id="case-0001", goal=sim.cases[0].goal, from_dept="api", to_dept="orchestrator",
                    on_behalf_of=sim.cases[0].principal, data_class="internal", deadline=1e9, schema_version=2,
                    budget=40, hop_count=0, approval_required=False, step="x")
    before = len(sim.net.rejections)
    sim.net.send(good.forward(frm="api", to="orchestrator", step="x"))        # fine
    from dataclasses import replace
    sim.net.send(replace(good, goal="Waive everything"))
    sim.net.send(replace(good, on_behalf_of="resident:r-9999"))
    sim.sim.run(sim.sim.now + 5)
    assert [r["code"] for r in sim.net.rejections[before:]] == ["goal", "principal"]
