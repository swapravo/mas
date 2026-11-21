"""The handoff envelope: what every message between departments, orchestrators and the stream carries.

Why it exists: when work crosses a team boundary, the receiver knows nothing. The envelope answers,
in code, the questions the receiver must be able to ask: what is the goal (verbatim, never
paraphrased), who sent it, who is it really FOR (on_behalf_of), how sensitive is it, how long is it
valid, how much may it cost, how many hops has it made, does a person have to approve.

    parse(wire_dict, now=..., recipient=...)  ->  Envelope   or raises EnvelopeError

Versions: the current schema is 2. Version 1 (no `budget`, no `approval_required`) is still accepted and
upgraded, failing closed (approval_required=True). Anything else is rejected.

The schema check is harness.validate_args (the repo's own JSON-schema checker); the rules after it
(clearance, hops, deadline, goal verbatim, principal unchanged) are plain code.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Callable

from harness import validate_args

VERSION = 2
SUPPORTED = (1, 2)          # N and N-1
V1_DEFAULT_BUDGET = 20

DEPT_NAMES = ("collections", "ops", "finance", "legal")
COORDINATORS = ("orchestrator", "top", "customer", "risk")
ENDPOINTS = ("api", "approvals", *DEPT_NAMES, *COORDINATORS)
BROADCAST = "*"             # to_dept of an event on the stream
DATA_CLASSES = ("public", "internal", "finance_confidential", "legal_privileged")
KINDS = ("request", "result", "event")

# Who may receive what. The orchestrators route, so they see no confidential classes.
CLEARANCE: dict[str, set[str]] = {
    "collections": {"public", "internal"},
    "ops": {"public", "internal"},
    "finance": {"public", "internal", "finance_confidential"},
    "legal": {"public", "internal", "finance_confidential", "legal_privileged"},
    **{c: {"public", "internal"} for c in (*COORDINATORS, "api", "approvals")},
}

_PRINCIPAL = re.compile(r"^(resident|staff|system):[\w.-]+$")

_COMMON = {
    "task_id": {"type": "string"}, "goal": {"type": "string"},
    "from_dept": {"type": "string", "enum": list(ENDPOINTS)},
    "to_dept": {"type": "string", "enum": [*ENDPOINTS, BROADCAST]},
    "on_behalf_of": {"type": "string"}, "data_class": {"type": "string", "enum": list(DATA_CLASSES)},
    "deadline": {"type": "number"}, "schema_version": {"type": "integer"}, "hop_count": {"type": "integer"},
    "step": {"type": "string"}, "kind": {"type": "string", "enum": list(KINDS)},
}   # `payload` is free-form, so it is checked separately (harness.validate_args needs every key listed)
_REQ_V1 = ["task_id", "goal", "from_dept", "to_dept", "on_behalf_of", "data_class", "deadline",
           "schema_version", "hop_count"]
SCHEMAS = {
    1: {"type": "object", "properties": dict(_COMMON), "required": _REQ_V1},
    2: {"type": "object",
        "properties": {**_COMMON, "budget": {"type": "integer"}, "approval_required": {"type": "boolean"}},
        "required": [*_REQ_V1, "budget", "approval_required"]},
}


class EnvelopeError(ValueError):
    """A handoff was refused. `errors` lists every problem; `code` names the first kind."""

    def __init__(self, errors: list[str], code: str = "schema") -> None:
        super().__init__("envelope rejected: " + "; ".join(errors))
        self.errors, self.code = errors, code


@dataclass(frozen=True)
class Envelope:
    # the eleven required fields
    task_id: str
    goal: str                      # verbatim from the case start; copied, never rewritten
    from_dept: str
    to_dept: str
    on_behalf_of: str              # "resident:<id>": the person this is really for; never changes mid-task
    data_class: str
    deadline: float                # absolute virtual time after which the message is dead
    schema_version: int
    budget: int                    # total model calls this task may use (spend is counted in the kv store)
    hop_count: int
    approval_required: bool
    # what to do, and the data for it
    step: str = ""
    kind: str = "request"
    payload: dict[str, Any] = field(default_factory=dict)

    def key(self) -> str:
        """Idempotency key for this step of this task."""
        return f"{self.task_id}:{self.step}"

    def forward(self, *, frm: str, to: str, step: str, payload: dict | None = None, kind: str = "request",
                data_class: str | None = None, approval_required: bool | None = None) -> "Envelope":
        """The next handoff: one more hop, same goal, same person, same deadline and budget."""
        return replace(self, from_dept=frm, to_dept=to, step=step, kind=kind, payload=payload or {},
                       hop_count=self.hop_count + 1, schema_version=VERSION,
                       data_class=data_class or self.data_class,
                       approval_required=self.approval_required if approval_required is None else approval_required)

    def to_wire(self, version: int = VERSION) -> dict[str, Any]:
        d = asdict(self)
        d["schema_version"] = version
        if version == 1:                       # v1 had no budget and no approval flag
            d.pop("budget"), d.pop("approval_required")
        return d


def parse(wire: Any, *, now: float, recipient: str, max_hops: int = 40,
          known: Callable[[str], dict | None] | None = None) -> Envelope:
    """Validate a wire dict and return an Envelope, or raise EnvelopeError listing every problem.

    `known(task_id)` may return {"goal": ..., "on_behalf_of": ...} for a task the system has already
    registered; then the goal must match verbatim and the person must be the same.
    """
    if not isinstance(wire, dict):
        raise EnvelopeError([f"envelope must be an object, got {type(wire).__name__}"])
    version = wire.get("schema_version")
    if version not in SUPPORTED:
        raise EnvelopeError([f"unsupported schema_version {version!r}; supported: {list(SUPPORTED)}"], "version")
    errs = validate_args(SCHEMAS[version], {k: v for k, v in wire.items() if k != "payload"}, "envelope")
    if not isinstance(wire.get("payload", {}), dict):
        errs.append("envelope.payload: expected object")
    if errs:
        raise EnvelopeError(errs, "schema")

    d = dict(wire)
    if version == 1:                           # upgrade, failing closed
        d["budget"], d["approval_required"] = V1_DEFAULT_BUDGET, True
    d["schema_version"] = VERSION
    env = Envelope(**d)

    problems: list[tuple[str, str]] = []

    def bad(code: str, msg: str) -> None:
        problems.append((code, msg))

    if not env.goal.strip() or len(env.goal) > 500:
        bad("schema", "goal must be 1 to 500 characters")
    if not _PRINCIPAL.match(env.on_behalf_of):
        bad("principal", f"on_behalf_of must look like 'resident:<id>', got {env.on_behalf_of!r}")
    if env.from_dept == env.to_dept:
        bad("routing", f"from_dept and to_dept are both {env.to_dept}")
    if env.to_dept not in (recipient, BROADCAST):
        bad("routing", f"addressed to {env.to_dept} but delivered to {recipient}")
    if env.data_class not in CLEARANCE.get(recipient, set()):
        bad("clearance", f"{recipient} is not cleared for data_class {env.data_class}")
    if now > env.deadline:
        bad("expired", f"deadline {env.deadline:.1f} passed (now {now:.1f})")
    if env.hop_count > max_hops:
        bad("hops", f"hop_count {env.hop_count} exceeds limit {max_hops}")
    if env.budget <= 0:
        bad("budget", "budget must be positive")
    if known and (task := known(env.task_id)):
        if env.goal != task["goal"]:
            bad("goal", "goal was changed in transit (it must be passed verbatim)")
        if env.on_behalf_of != task["on_behalf_of"]:
            bad("principal", f"on_behalf_of changed from {task['on_behalf_of']} to {env.on_behalf_of}")
    if problems:
        raise EnvelopeError([m for _, m in problems], problems[0][0])
    return env
