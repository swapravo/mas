"""The approval queue: people are a bottleneck, a long queue means rushed reviews, and what helps."""
from helpers import run


def approvals(**kw):
    sim, res = run("central", "rules", cases=40, sim_only=True, **kw)
    return sim, res["approvals"]


def test_defaults_show_rubber_stamping_above_the_threshold_and_only_above_it():
    sim, a = approvals()
    s = sim.settings
    assert a["rushed_share"] > 0.5 and a["queue_length"]["max"] > s.rush_threshold
    for r in sim.approvals.reviews:                              # the rule, checked review by review
        assert r.rushed == (r.queue_behind > s.rush_threshold)
        assert r.seconds == (s.rush_time if r.rushed else s.review_time)
    assert a["submitted"] == 40 + 10 and a["reviewed_by_people"] == 50       # a draft per case, a card per escalation
    assert a["cards_rushed"] > 0                                  # even the high-stakes decisions get rushed


def test_the_numbers_the_brief_asks_for_are_recorded():
    _, a = approvals()
    assert a["wait_seconds"]["p50"] > 0 and a["wait_seconds"]["p95"] >= a["wait_seconds"]["p50"]
    assert a["queue_length"]["max"] >= a["queue_length"]["mean"] > 0
    assert 0 <= a["rushed_share"] <= 1 and a["still_waiting_at_end"] == 0


def test_a_short_queue_means_careful_reviews():
    _, small = approvals() if False else (None, run("central", "rules", cases=4)["approvals"])
    assert small["rushed_share"] == 0.0
    _, many = approvals(approvers=20)                              # enough people: nobody is rushed
    assert many["rushed_share"] == 0.0 and many["wait_seconds"]["p95"] == 0.0


def test_auto_approving_low_risk_reduces_rubber_stamping_and_never_skips_a_decision_card():
    sim, base = approvals()
    _, auto = approvals(auto_approve=True)
    assert auto["rushed_share"] < base["rushed_share"] and auto["auto_approved"] == 20
    assert auto["cards_reviewed"] == base["cards_reviewed"] == 10            # cards are high risk: always a person
    assert auto["reviewed_by_people"] == base["reviewed_by_people"] - auto["auto_approved"]
    low = [r for r in sim.registry.all() if not r["conflict"]]
    assert len(low) == auto["auto_approved"]                                  # exactly the cases nobody disagreed on


def test_batching_similar_cases_means_fewer_review_sessions_and_less_rushing():
    _, base = approvals()
    sim, batch = approvals(batch=True)
    assert batch["review_sessions"] < base["review_sessions"]
    assert batch["rushed_share"] < base["rushed_share"] and batch["reviewed_by_people"] == base["reviewed_by_people"]
    assert any(r.items > 1 for r in sim.approvals.reviews)
    for r in sim.approvals.reviews:                                         # a batch only ever holds one kind of thing
        assert len(r.kinds) == 1


def test_with_both_mitigations_rubber_stamping_nearly_disappears():
    _, base = approvals()
    _, both = approvals(auto_approve=True, batch=True)
    assert both["rushed_share"] < 0.1 < base["rushed_share"]


def test_the_trade_off_less_rushing_means_longer_waits_for_what_is_left():
    """Rubber-stamping clears a queue by cutting corners. Remove it without adding people and the careful reviews
    take their full time, so the slowest waits get longer. The mitigation is not free."""
    _, base = approvals()
    _, auto = approvals(auto_approve=True)
    assert auto["rushed_share"] < base["rushed_share"] and auto["wait_seconds"]["p95"] > base["wait_seconds"]["p95"]


def test_every_reply_ends_up_approved_and_nothing_is_sent_without_a_person():
    sim, _ = approvals()
    rows = sim.registry.all()
    assert all(r["reply_status"] == "approved" and r["reply_approved_by"] in ("person", "auto") for r in rows)
    drafts = sim.w.store.scan("owner", "draft", dept="collections")
    assert len(drafts) == 40 and all(d["payload"]["status"] == "approved" for d in drafts)
    assert not sim.w.store.scan("owner", "sent")
