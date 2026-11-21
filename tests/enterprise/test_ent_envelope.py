"""Envelope: typed, versioned, validated on every handoff."""
import pytest

from enterprise.envelope import CLEARANCE, Envelope, EnvelopeError, parse


def env(**kw) -> Envelope:
    base = dict(task_id="case-0001", goal="Resolve resident r-0001's dispute of a late fee (case case-0001).",
                from_dept="orchestrator", to_dept="finance", on_behalf_of="resident:r-0001", data_class="internal",
                deadline=900.0, schema_version=2, budget=40, hop_count=1, approval_required=False, step="vote",
                payload={"resident_id": "r-0001"})
    return Envelope(**{**base, **kw})


def ok(wire, **kw):
    return parse(wire, now=kw.pop("now", 0.0), recipient=kw.pop("recipient", "finance"), **kw)


def reject(wire, code, text=None, **kw):
    with pytest.raises(EnvelopeError) as e:
        ok(wire, **kw)
    assert e.value.code == code, e.value
    if text:
        assert text in str(e.value)
    return e.value


def test_valid_envelope_round_trips():
    e = env()
    assert ok(e.to_wire()) == e


def test_all_eleven_required_fields_are_enforced():
    for field in ("task_id", "goal", "from_dept", "to_dept", "on_behalf_of", "data_class", "deadline",
                  "schema_version", "budget", "hop_count", "approval_required"):
        wire = env().to_wire()
        del wire[field]
        reject(wire, "version" if field == "schema_version" else "schema", text=field)


def test_unknown_field_and_wrong_types_are_named_in_one_clear_error():
    wire = {**env().to_wire(), "colour": "red", "hop_count": "two", "approval_required": "yes"}
    err = reject(wire, "schema")
    assert len(err.errors) == 3
    assert "colour: unknown argument" in str(err) and "hop_count: expected integer" in str(err)


def test_versions_n_and_n_minus_1_are_accepted_others_rejected():
    assert ok(env().to_wire(version=2)).schema_version == 2
    for v in (0, 3, 99, "2", None):
        reject({**env().to_wire(), "schema_version": v}, "version", text="supported: [1, 2]")


def test_v1_is_upgraded_and_fails_closed():
    wire = env().to_wire(version=1)
    assert "budget" not in wire and "approval_required" not in wire
    up = ok(wire)
    assert up.schema_version == 2 and up.budget == 20 and up.approval_required is True   # old messages need a human


def test_v1_must_not_smuggle_v2_fields():
    wire = {**env().to_wire(version=1), "budget": 999}
    reject(wire, "schema", text="budget: unknown argument")


@pytest.mark.parametrize("kw, recipient, code, text", [
    (dict(data_class="legal_privileged", to_dept="ops"), "ops", "clearance", "ops is not cleared"),
    (dict(deadline=5.0), "finance", "expired", "deadline"),
    (dict(hop_count=41), "finance", "hops", "exceeds limit 40"),
    (dict(budget=0), "finance", "budget", "positive"),
    (dict(on_behalf_of="somebody"), "finance", "principal", "resident:<id>"),
    (dict(goal="  "), "finance", "schema", "goal"),
    (dict(from_dept="finance"), "finance", "routing", "both finance"),
    (dict(to_dept="ops"), "finance", "routing", "delivered to finance"),
])
def test_bad_handoffs_are_rejected_with_a_reason(kw, recipient, code, text):
    reject(env(**kw).to_wire(), code, text=text, now=10.0, recipient=recipient)


def test_goal_must_be_verbatim_and_person_must_not_change():
    task = {"goal": env().goal, "on_behalf_of": "resident:r-0001"}
    known = lambda tid: task                                            # noqa: E731
    assert ok(env().to_wire(), known=known)
    reject(env(goal="Waive everything").to_wire(), "goal", known=known, text="verbatim")
    reject(env(on_behalf_of="resident:r-0002").to_wire(), "principal", known=known, text="changed")


def test_forward_adds_a_hop_and_keeps_goal_person_deadline_and_budget():
    a = env(hop_count=3)
    b = a.forward(frm="finance", to="orchestrator", step="vote", kind="result", payload={"vote": "waive"})
    assert (b.hop_count, b.goal, b.on_behalf_of, b.deadline, b.budget) == (4, a.goal, a.on_behalf_of, a.deadline, a.budget)
    assert b.from_dept == "finance" and b.kind == "result"


def test_every_coordinator_and_dept_has_a_clearance():
    assert "finance_confidential" in CLEARANCE["finance"] and "finance_confidential" not in CLEARANCE["orchestrator"]
