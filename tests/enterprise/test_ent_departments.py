"""Departments: scripted harness agents, their policies, idempotency, budget and failure."""
import json

import pytest

from enterprise.envelope import Envelope
from enterprise.settings import Settings
from enterprise.workload import Case, seed_store
from enterprise.world import World


def case(fee=1500, repair=None, notice=False, text="please help", n=1):
    return Case(f"case-{n:04d}", f"r-{n:04d}", "Asha", "Maple Court", fee, repair, notice, text)


def make(c: Case | None = None, **settings):
    w = World(Settings(**settings))
    c = c or case()
    seed_store(w.store, [c])
    return w, c


def env(c: Case, dept: str, step: str, budget=40, **payload) -> Envelope:
    return Envelope(task_id=c.case_id, goal=c.goal, from_dept="orchestrator", to_dept=dept, on_behalf_of=c.principal,
                    data_class="internal", deadline=900.0, schema_version=2, budget=budget, hop_count=1,
                    approval_required=False, step=step, payload={"resident_id": c.resident_id, "case_id": c.case_id, **payload})


def run(w, c, dept, step, **kw):
    return w.depts[dept].handle(env(c, dept, step, **kw))


@pytest.mark.parametrize("fee, vote", [(800, "waive"), (2000, "waive"), (2001, "refuse"), (9500, "refuse")])
def test_finance_may_waive_up_to_2000_alone(fee, vote):
    w, c = make(case(fee=fee))
    r = run(w, c, "finance", "vote")
    assert r.ok and r.payload["vote"] == vote and r.payload["amount"] == fee


@pytest.mark.parametrize("days, vote", [(None, "abstain"), (3, "abstain"), (6, "abstain"), (7, "waive"), (21, "waive")])
def test_ops_supports_a_waiver_if_a_repair_is_open_7_or_more_days(days, vote):
    w, c = make(case(repair=days))
    assert run(w, c, "ops", "vote").payload["vote"] == vote


@pytest.mark.parametrize("live, vote", [(True, "refuse"), (False, "abstain")])
def test_legal_objects_while_a_notice_is_live(live, vote):
    w, c = make(case(notice=live))
    assert run(w, c, "legal", "vote").payload["vote"] == vote


def test_collections_reads_the_dispute_and_drafts_but_never_sends():
    w, c = make(case(text="the lift is broken"))
    brief = run(w, c, "collections", "intake").payload
    assert brief["resident_id"] == c.resident_id and brief["text"] == "the lift is broken"
    r = run(w, c, "collections", "reply", outcome="waive", amount=1500)
    assert r.ok and r.payload["draft"] == "pending_approval"
    draft = w.store.get("owner", "draft", c.case_id, dept="collections")
    assert draft["status"] == "pending_approval" and "waived" in draft["body"]
    assert not w.store.scan("owner", "sent")                       # there is no send path at all


def test_a_draft_with_personal_data_is_refused_by_the_tool():
    w, c = make()
    from enterprise.gateway import CallContext
    with pytest.raises(Exception, match="personal data"):
        w.gateway.call(CallContext("collections", env(c, "collections", "reply")), "collections.draft_reply",
                       {"case_id": c.case_id, "body": "call me on 9876543210"})


def test_waiver_over_the_limit_needs_an_approver_in_code():
    w, c = make(case(fee=4000))
    r = run(w, c, "finance", "act", amount=4000)
    assert r.ok and r.payload["applied"] is False                       # the tool refused; the agent reported it
    assert not w.store.scan("owner", "waiver_applied")
    r2 = run(w, c, "finance", "act.approved", amount=4000, approved_by="human:fin-lead")
    assert r2.payload["applied"] is True
    assert len(w.store.scan("owner", "waiver_applied")) == 1


def test_a_repeated_step_runs_once_and_returns_the_saved_result():
    w, c = make()
    first = run(w, c, "finance", "act", amount=1500)
    again = run(w, c, "finance", "act", amount=1500)
    assert not first.cached and again.cached and again.payload == first.payload
    assert len(w.store.scan("owner", "waiver_applied")) == 1
    assert w.store.get("owner", "ledger", c.resident_id, dept="finance")["waived_total"] == 1500
    assert w.stats["dup_suppressed"] == 1


def test_without_idempotency_the_same_duplicate_waives_twice():
    w, c = make(idempotent=False)
    run(w, c, "finance", "act", amount=1500)
    run(w, c, "finance", "act", amount=1500)
    assert w.store.get("owner", "ledger", c.resident_id, dept="finance")["waived_total"] == 3000


def test_budget_is_counted_across_steps_and_enforced():
    w, c = make()
    assert run(w, c, "ops", "vote", budget=4).ok                        # 2 model calls
    assert run(w, c, "finance", "vote", budget=4).ok                    # 2 more: budget now spent
    r = run(w, c, "legal", "vote", budget=4)
    assert not r.ok and "budget exhausted" in r.error and w.stats["budget_rejects"] == 1


def test_a_failed_model_call_is_an_error_reply_and_a_retry_really_runs():
    w, c = make(llm_fail={"finance": 1.0})
    bad = run(w, c, "finance", "vote")
    assert not bad.ok and "model unavailable" in bad.error and bad.calls == 1
    w.settings.llm_fail = {}
    good = run(w, c, "finance", "vote")
    assert good.ok and not good.cached                                   # failure was not remembered


def test_model_latency_is_virtual_seconds_and_configurable():
    w, c = make(llm_latency={"legal": 10.0, "finance": 0.0})
    slow, fast = run(w, c, "legal", "vote"), run(w, c, "finance", "vote")
    assert 2 * 10 * 0.8 <= slow.service <= 2 * 10 * 1.2 + 0.1 and fast.service < 0.1


def test_the_agent_runs_on_the_real_harness_and_everything_is_audited():
    w, c = make()
    run(w, c, "ops", "vote")
    actions = [(r["action"], r["target"]) for r in w.store.audit_rows]
    assert ("tool_call", "ops.get_repairs") in actions
    assert any(a == "tool_result" for a, _ in actions) and any(a == "finished" for a, _ in actions)


def test_same_seed_same_numbers():
    def once():
        w, c = make(llm_latency={"finance": 3.0})
        return run(w, c, "finance", "vote").service
    assert once() == once()


def test_a_crash_after_the_side_effect_does_not_repeat_it(monkeypatch):
    """The waiver commits, then the model call that would report it fails. The retry must not waive again."""
    from harness import LLMUnavailable
    from enterprise.llm import observations
    w, c = make()
    real, boom = w.depts["finance"].policy, {"left": 1}

    def flaky(kind):
        inner = real(kind)

        def policy(system, messages):
            if observations(messages) and boom["left"]:          # the tool already ran
                boom["left"] -= 1
                raise LLMUnavailable("crashed after the waiver")
            return inner(system, messages)
        return policy
    monkeypatch.setattr(w.depts["finance"], "policy", flaky)
    assert not run(w, c, "finance", "act", amount=1500).ok
    assert len(w.store.scan("owner", "waiver_applied")) == 1       # it did happen once
    again = run(w, c, "finance", "act", amount=1500)
    assert again.ok and again.payload["applied"]
    assert len(w.store.scan("owner", "waiver_applied")) == 1
    assert w.store.get("owner", "ledger", c.resident_id, dept="finance")["waived_total"] == 1500
