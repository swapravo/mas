"""Conflict arbitration: three ways to turn disagreeing votes into one decision.

A vote is {dept, vote: waive|refuse|abstain, reason, t, seq} (t/seq = when it arrived).

  last      last-writer-wins   the last non-abstain vote to ARRIVE wins. Cheap, needs no rules, and the answer
                               depends on who was fastest: change the latency and the decision flips.
  priority  fixed priority     Legal > Finance > Ops. Deterministic and no human, but it overrules people
                               silently, even where the money is large.
  rules     rules, then ask    (1) nobody disagrees: follow the votes. (2) Legal objects while a notice is live
                               and the fee is within Finance's own limit: Legal's non-negotiable wins. (3) anything
                               else (a real conflict over a large fee): stop and raise a DECISION CARD for a person.
                               This follows tests/code-shipper-pack/orchestrator/orchestrator_prompt.md:
                               legal first, money escalates, record every overruled position.

Every Decision lists the votes it overruled.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

PRIORITY = ("legal", "finance", "ops")
ESCALATION_PATH = ["finance_manager", "head_of_operations", "general_counsel"]


@dataclass
class Decision:
    outcome: str                     # waive | refuse | escalate
    decided_by: str                  # last | priority | rule | human
    why: str
    conflict: bool
    overruled: list[dict] = field(default_factory=list)
    card: dict | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _real(votes: list[dict]) -> list[dict]:
    return [v for v in votes if v["vote"] in ("waive", "refuse")]


def has_conflict(votes: list[dict]) -> bool:
    return {v["vote"] for v in votes} >= {"waive", "refuse"}


def _overruled(votes: list[dict], outcome: str) -> list[dict]:
    return [{"dept": v["dept"], "vote": v["vote"], "reason": v["reason"]} for v in _real(votes) if v["vote"] != outcome]


def last_writer_wins(votes: list[dict]) -> Decision:
    real = sorted(_real(votes), key=lambda v: (v["t"], v["seq"]))
    win = real[-1]
    return Decision(win["vote"], "last", f"{win['dept']} was the last vote to arrive", has_conflict(votes),
                    _overruled(votes, win["vote"]))


def fixed_priority(votes: list[dict]) -> Decision:
    by = {v["dept"]: v for v in _real(votes)}
    for dept in PRIORITY:
        if dept in by:
            return Decision(by[dept]["vote"], "priority", f"{dept} ranks highest of those who voted (legal > finance > ops)",
                            has_conflict(votes), _overruled(votes, by[dept]["vote"]))
    return Decision("refuse", "priority", "nobody voted to waive", False)


def rules_then_escalate(votes: list[dict], *, fee: int, limit: int, case: dict, now: float, decide_by: float) -> Decision:
    real = _real(votes)
    if not has_conflict(votes):
        outcome = real[0]["vote"] if real else "refuse"
        return Decision(outcome, "rule", "no department disagrees", False)
    legal = next((v for v in real if v["dept"] == "legal"), None)
    if fee <= limit and legal and legal["vote"] == "refuse":
        return Decision("refuse", "rule", "a live notice blocks any waiver: Legal's objection is a non-negotiable, and the fee "
                        "is within Finance's own limit so no money decision is left", True, _overruled(votes, "refuse"))
    return Decision("escalate", "rule", "real conflict over a fee above Finance's limit: a person must decide", True,
                    card=decision_card(votes, fee=fee, case=case, now=now, decide_by=decide_by))


def decision_card(votes: list[dict], *, fee: int, case: dict, now: float, decide_by: float) -> dict:
    """What a person needs to decide in under a minute: the case, the disagreement, the choices, our advice."""
    waive = [{"dept": v["dept"], "reason": v["reason"]} for v in votes if v["vote"] == "waive"]
    refuse = [{"dept": v["dept"], "reason": v["reason"]} for v in votes if v["vote"] == "refuse"]
    legal_objects = any(v["dept"] == "legal" and v["vote"] == "refuse" for v in votes)
    return {
        "case": {"case_id": case["case_id"], "resident_id": case["resident_id"], "fee_rupees": fee},
        "conflict": {"wants_waiver": waive, "against_waiver": refuse},
        "options": [{"id": "waive", "effect": f"waive Rs {fee:,}; needs your approval (above Finance's limit)"},
                    {"id": "refuse", "effect": "keep the fee; Legal serves a notice if none is live"}],
        "recommendation": {"option": "refuse" if legal_objects else "waive",
                           "why": ("a live legal notice: Legal's objection should not be overruled by a repair delay"
                                   if legal_objects else "a repair has been open 7+ days and nothing legal stands in the way")},
        "raised_at": round(now, 1), "decide_by": round(now + decide_by, 1), "escalation_path": ESCALATION_PATH,
    }


def decide(strategy: str, votes: list[dict], *, fee: int, limit: int, case: dict, now: float, decide_by: float) -> Decision:
    if strategy == "last":
        return last_writer_wins(votes)
    if strategy == "priority":
        return fixed_priority(votes)
    if strategy == "rules":
        return rules_then_escalate(votes, fee=fee, limit=limit, case=case, now=now, decide_by=decide_by)
    raise ValueError(f"unknown strategy {strategy!r}")
