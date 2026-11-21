"""Conflict arbitration: last-writer-wins, fixed priority, rules-then-escalate. Unit and end to end, in all three modes."""
import json

import pytest

from enterprise import arbitration as arb
from enterprise.settings import Policy
from helpers import MODES, by_case, run

LIMIT = Policy().fee_limit


def vote(dept, v, t, seq=0, reason="r"):
    return {"dept": dept, "vote": v, "reason": reason, "t": t, "seq": seq}


# -- the strategies on hand-made votes ---------------------------------------------------------

def test_last_writer_wins_depends_on_arrival_order_alone():
    a = [vote("finance", "waive", t=1), vote("legal", "refuse", t=2)]
    b = [vote("finance", "waive", t=2), vote("legal", "refuse", t=1)]
    assert arb.last_writer_wins(a).outcome == "refuse" and arb.last_writer_wins(b).outcome == "waive"
    assert arb.last_writer_wins(a).decided_by == "last"
    ties = [vote("finance", "waive", t=1, seq=1), vote("legal", "refuse", t=1, seq=2)]
    assert arb.last_writer_wins(ties).outcome == "refuse"                      # same instant: the later seq wins
    assert arb.last_writer_wins([vote("ops", "abstain", 9), vote("finance", "waive", 1)]).outcome == "waive"   # abstaining is not writing


def test_fixed_priority_is_legal_then_finance_then_ops_whatever_the_order():
    for order in ([("ops", "waive"), ("finance", "refuse"), ("legal", "refuse")],
                  [("legal", "refuse"), ("finance", "refuse"), ("ops", "waive")]):
        votes = [vote(d, v, t=i) for i, (d, v) in enumerate(order)]
        d = arb.fixed_priority(votes)
        assert d.outcome == "refuse" and d.decided_by == "priority" and [o["dept"] for o in d.overruled] == ["ops"]
    assert arb.fixed_priority([vote("legal", "abstain", 1), vote("finance", "waive", 2), vote("ops", "refuse", 3)]).outcome == "waive"


def case_ctx():
    return dict(case={"case_id": "case-0001", "resident_id": "r-0001"}, now=10.0, decide_by=600.0, limit=LIMIT)


def test_rules_follow_agreement_then_legal_within_finances_limit_then_escalate():
    agree = arb.rules_then_escalate([vote("finance", "waive", 1), vote("ops", "waive", 2), vote("legal", "abstain", 3)], fee=1500, **case_ctx())
    assert (agree.outcome, agree.decided_by, agree.conflict, agree.card) == ("waive", "rule", False, None)
    small = arb.rules_then_escalate([vote("finance", "waive", 1), vote("legal", "refuse", 2)], fee=2000, **case_ctx())
    assert (small.outcome, small.decided_by, small.conflict, small.card) == ("refuse", "rule", True, None)
    big = arb.rules_then_escalate([vote("finance", "refuse", 1), vote("ops", "waive", 2), vote("legal", "abstain", 3)], fee=2001, **case_ctx())
    assert (big.outcome, big.decided_by, big.conflict) == ("escalate", "rule", True) and big.card


def test_the_decision_card_has_everything_a_person_needs_and_is_plain_json():
    d = arb.rules_then_escalate([vote("finance", "refuse", 1, reason="over my limit"), vote("ops", "waive", 2, reason="repair 9 days"),
                                 vote("legal", "refuse", 3, reason="notice live")], fee=4000, **case_ctx())
    card = json.loads(json.dumps(d.card))
    assert set(card) == {"case", "conflict", "options", "recommendation", "decide_by", "escalation_path", "raised_at"}
    assert card["case"] == {"case_id": "case-0001", "resident_id": "r-0001", "fee_rupees": 4000}
    assert {x["dept"] for x in card["conflict"]["against_waiver"]} == {"finance", "legal"}
    assert [o["id"] for o in card["options"]] == ["waive", "refuse"]
    assert card["recommendation"]["option"] == "refuse"                       # legal's objection is not overruled by a repair delay
    assert card["decide_by"] == 610.0 and card["escalation_path"][0] == "finance_manager"


# -- end to end: the same cases through every mode ---------------------------------------------------

def conflicting_large(c):
    """Large fee and a department wants a waiver: that is the case the rules cannot settle."""
    return c.large() and c.repair_open()


@pytest.mark.parametrize("mode", MODES)
def test_rules_escalate_exactly_the_conflicting_large_fee_cases_and_settle_the_rest_without_a_human(mode):
    sim, res = run(mode, "rules", cases=40, sim_only=True)
    rows = {r["case_id"]: r for r in sim.registry.all()}
    want = {c.case_id for c in sim.cases if conflicting_large(c)}
    assert want and {k for k, r in rows.items() if r["decided_by"] == "human"} == want
    assert {c["case"]["case_id"] for c in sim.cards} == want                   # one card per escalated case, no more
    assert all(r["decided_by"] == "rule" for k, r in rows.items() if k not in want)
    assert res["outcomes"]["completed"] == 40


@pytest.mark.parametrize("mode", MODES)
def test_cards_are_stored_in_the_database_and_a_person_approves_any_large_waiver(mode):
    sim, _ = run(mode, "rules", cases=40, sim_only=True)
    for card in sim.cards:
        assert sim.w.store.get("orch", "decision_card", card["case"]["case_id"]) == card
    waivers = [r["payload"] for r in sim.w.store.scan("owner", "waiver_applied")]
    large = [w for w in waivers if w["amount"] > LIMIT]
    assert large and all(w["approved_by"].startswith("human:") for w in large)   # Finance's own limit, enforced in code
    assert all(not w["approved_by"] for w in waivers if w["amount"] <= LIMIT)


@pytest.mark.parametrize("mode", MODES)
def test_the_rules_decisions_match_the_policy(mode):
    sim, res = run(mode, "rules", cases=40, sim_only=True)
    got = by_case(res)
    for c in sim.cases:
        if conflicting_large(c):
            want = "refuse" if c.notice_live else "waive"                     # a person accepts the recommendation
        elif c.notice_live:
            want = "refuse"                                                    # Legal's objection wins
        else:
            want = "waive" if not c.large() else "refuse"
        assert got[c.case_id]["outcome"] == want, (c, got[c.case_id])


@pytest.mark.parametrize("mode", MODES)
def test_priority_overrides_without_a_human_even_where_the_money_is_large(mode):
    sim, res = run(mode, "priority", cases=40, sim_only=True)
    rows = {r["case_id"]: r for r in sim.registry.all()}
    assert not sim.cards and res["decisions"]["decided_by"] == {"priority": 40}
    for c in sim.cases:
        if conflicting_large(c):
            assert rows[c.case_id]["conflict"] and rows[c.case_id]["outcome"] == "refuse"     # Ops's waiver was overruled


def slow(**who):
    base = {"collections": 1.2, "ops": 0.8, "finance": 1.0, "legal": 1.5}
    return {**base, **who}


# slow departments need cases to arrive slower, or the queue (not the arbitration) decides what happens
SPACED = dict(arrival_gap=15.0)


@pytest.mark.parametrize("mode", MODES)
def test_last_writer_wins_flips_with_arrival_order(mode):
    fin_last = run(mode, "last", cases=24, sim_only=True, llm_latency=slow(finance=6.0, legal=0.2, ops=0.5), **SPACED)
    leg_last = run(mode, "last", cases=24, sim_only=True, llm_latency=slow(finance=0.2, legal=6.0, ops=0.5), **SPACED)
    sim, a = fin_last
    _, b = leg_last
    assert a["outcomes"]["completed"] == b["outcomes"]["completed"] == 24
    A, B = by_case(a), by_case(b)
    for c in sim.cases:
        assert A[c.case_id]["outcome"] == ("refuse" if c.large() else "waive")        # Finance spoke last: Finance decides
        if c.notice_live:
            assert B[c.case_id]["outcome"] == "refuse"                                # Legal spoke last: Legal decides
    flipped = [c for c in sim.cases if A[c.case_id]["outcome"] != B[c.case_id]["outcome"]]
    assert flipped and all(c.notice_live and not c.large() for c in flipped)         # small fee + notice: waive vs refuse
    assert a["decisions"]["decided_by"] == {"last": 24}


def test_last_writer_wins_is_saved_by_the_waiver_limit_not_by_itself():
    """Ops speaks last on a large fee: LWW says waive. Finance's own code refuses a waiver over its limit with
    no approval, so the case becomes a refusal. The limit is a second line of defence the strategy does not provide."""
    sim, res = run("central", "last", cases=24, sim_only=True, llm_latency=slow(ops=6.0, finance=0.2, legal=0.2), **SPACED)
    assert res["decisions"]["policy_blocked"] > 0
    rows = {r["case_id"]: r for r in sim.registry.all()}
    blocked = [r for r in rows.values() if r.get("policy_blocked")]
    assert blocked and all(r["outcome"] == "refuse" for r in blocked)
    assert not [w for w in sim.w.store.scan("owner", "waiver_applied") if w["payload"]["amount"] > LIMIT]
