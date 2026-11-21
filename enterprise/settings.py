"""Every knob in one place. All times are VIRTUAL seconds (see sim.py): nothing here reads a clock.

Three small dataclasses:
  Settings  - how the world is built (workload, latency, timeouts, approvers ...)
  Defences  - which security layers are switched on (rls, gateway, obo)
  Chaos     - what we break on purpose (kill a component, add latency, duplicate messages)
"""
from __future__ import annotations

from dataclasses import dataclass, field

DEPTS = ("collections", "ops", "finance", "legal")
MODES = ("central", "hierarchical", "choreography")
STRATEGIES = ("last", "priority", "rules")


@dataclass
class Policy:
    """The business rules from the brief. Enforced in code, mirrored in the agent prompts."""
    fee_limit: int = 2000      # Finance may waive up to this many rupees alone
    repair_days: int = 7       # Ops supports a waiver if a repair has been open this many days


@dataclass
class Defences:
    """Three independent security layers. Turn one off to see which attack gets through."""
    rls: bool = True       # database: a department role only sees its own rows
    gateway: bool = True   # tool gateway: a role may only call tools on its allow-list
    obo: bool = True       # on-behalf-of: a call may only touch the person it is made for

    @classmethod
    def none(cls) -> "Defences":
        return cls(False, False, False)

    @classmethod
    def only(cls, *names: str) -> "Defences":
        return cls(**{n: (n in names) for n in ("rls", "gateway", "obo")})

    def on(self) -> list[str]:
        return [n for n in ("rls", "gateway", "obo") if getattr(self, n)]


@dataclass
class Chaos:
    """Things to break. `kill` takes component names: orchestrator, top, customer, risk, bus,
    collections, ops, finance, legal. A killed component stops consuming; its queue just fills up."""
    kill: tuple[str, ...] = ()
    kill_at: float = 0.0                                  # virtual time when the kill happens
    net_latency: float = 0.0                              # extra seconds on every bus hop
    dept_latency: dict[str, float] = field(default_factory=dict)   # extra service seconds per department
    duplicate_every: int = 0                              # deliver every Nth message twice (1 = all)


@dataclass
class Settings:
    seed: int = 7
    mode: str = "central"
    strategy: str = "rules"
    cases: int = 40
    attack_every: int = 0            # every Nth case carries hostile resident text (0 = none)
    arrival_gap: float = 3.0         # seconds between disputes arriving

    # departments (scripted agents)
    llm_latency: dict[str, float] = field(default_factory=lambda: {
        "collections": 1.2, "ops": 0.8, "finance": 1.0, "legal": 1.5})
    llm_fail: dict[str, float] = field(default_factory=dict)        # dept -> chance a model call fails
    concurrency: int = 2             # parallel jobs per department
    max_agent_steps: int = 6

    # transport
    net_latency: float = 0.05        # seconds per bus hop
    redeliver_after: float = 10.0    # choreography: un-acked event is delivered again after this long

    # limits enforced in code
    step_timeout: float = 20.0       # an orchestrator waits this long for one step...
    max_retries: int = 2             # ...resends this many times, then escalates
    case_deadline: float = 900.0     # envelope deadline, relative to the case start
    max_hops: int = 40               # loop guard
    budget: int = 40                 # agent (model) calls allowed per case
    idempotent: bool = True          # handlers skip a step they have already done

    # monitoring
    watchdog: bool | None = None     # None = mode default (on for orchestrated modes, off for choreography)
    watchdog_timeout: float = 120.0  # a case open longer than this is "stalled"
    watchdog_interval: float = 10.0

    # approvals (people)
    approvers: int = 3
    review_time: float = 45.0        # seconds for a careful review
    rush_threshold: int = 6          # queue longer than this and reviews get rushed
    rush_time: float = 4.0           # seconds for a rushed review
    auto_approve: bool = False       # mitigation 1: skip the queue for low-risk items
    batch: bool = False              # mitigation 2: review similar items together
    batch_max: int = 10
    decide_by: float = 600.0         # decision card deadline, seconds after it is raised

    horizon: float = 3600.0          # the simulation stops here; anything unfinished is "stalled"
    policy: Policy = field(default_factory=Policy)

    def watchdog_on(self) -> bool:
        return self.mode != "choreography" if self.watchdog is None else self.watchdog
