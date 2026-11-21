"""The approval queue: people are a bottleneck, and a busy queue makes people sloppy.

A pool of `approvers` people. Each review takes `review_time` seconds. THE RUBBER-STAMP EFFECT: when the queue behind a
review is longer than `rush_threshold`, the reviewer rushes, and the review takes only `rush_time`. Nobody is lazy; a long
queue and a quiet pressure to clear it does this to real teams. We record each review so the effect is visible:

    wait time per item, queue length, and the share of reviews that were rushed.

Two mitigations, each behind a flag in Settings:
    auto_approve  a LOW-risk item (nobody disagreed) skips the queue. Fewer things to review, so less pressure.
    batch         similar pending items are reviewed together, as one review a little longer than a single one.

What goes through the queue: every reply draft (draft-then-approve: nothing is sent without a person) and every decision
card (a conflict the rules could not settle).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from enterprise.metrics_util import percentile


@dataclass
class Item:
    kind: str                         # "reply" | "card"
    case_id: str
    risk: str                         # "low" | "high"
    signature: tuple                  # items with the same signature are "similar" (batching)
    on_done: Callable[[str], None]    # called with the approver ("auto" or "person")
    created: float = 0.0


@dataclass
class Review:
    t: float
    queue_behind: int                 # items still waiting when this review started
    rushed: bool
    seconds: float
    items: int
    kinds: tuple


@dataclass
class ApproverPool:
    sim: object
    s: object                         # Settings
    free: int = 0
    queue: list = field(default_factory=list)
    waits: list = field(default_factory=list)
    queue_at_submit: list = field(default_factory=list)
    reviews: list = field(default_factory=list)
    submitted: int = 0
    auto: int = 0
    in_review: int = 0

    def __post_init__(self) -> None:
        self.free = self.s.approvers

    def submit(self, item: Item) -> None:
        item.created = self.sim.now
        self.submitted += 1
        if self.s.auto_approve and item.risk == "low":
            self.auto += 1
            self.waits.append(0.0)
            return item.on_done("auto")
        self.queue.append(item)
        self.queue_at_submit.append(len(self.queue))
        self._dispatch()

    def _take(self) -> list[Item]:
        first = self.queue.pop(0)
        if not self.s.batch:
            return [first]
        same = [i for i in self.queue if i.signature == first.signature][: self.s.batch_max - 1]
        for i in same:
            self.queue.remove(i)
        return [first, *same]

    def _dispatch(self) -> None:
        while self.free > 0 and self.queue:
            batch = self._take()
            self.free -= 1
            behind = len(self.queue)
            rushed = behind > self.s.rush_threshold                       # the rubber-stamp rule
            seconds = self.s.rush_time if rushed else self.s.review_time * (1 + 0.2 * (len(batch) - 1))
            self.waits += [self.sim.now - i.created for i in batch]
            self.reviews.append(Review(round(self.sim.now, 2), behind, rushed, seconds, len(batch),
                                       tuple(sorted({i.kind for i in batch}))))
            self.in_review += len(batch)
            self.sim.at(seconds, self._done, batch)

    def _done(self, batch: list[Item]) -> None:
        self.free += 1
        self.in_review -= len(batch)
        for i in batch:
            i.on_done("person")
        self._dispatch()

    # -- numbers ------------------------------------------------------------------------
    def stats(self) -> dict:
        items = sum(r.items for r in self.reviews)
        rushed = sum(r.items for r in self.reviews if r.rushed)
        cards = sum(r.items for r in self.reviews if r.kinds == ("card",))
        cards_rushed = sum(r.items for r in self.reviews if r.kinds == ("card",) and r.rushed)
        q = self.queue_at_submit
        return {
            "submitted": self.submitted, "auto_approved": self.auto, "reviewed_by_people": items,
            "review_sessions": len(self.reviews), "rushed": rushed,
            "rushed_share": round(rushed / items, 3) if items else 0.0,
            "cards_reviewed": cards, "cards_rushed": cards_rushed,
            "wait_seconds": {"p50": percentile(self.waits, 50), "p95": percentile(self.waits, 95),
                             "max": round(max(self.waits), 2) if self.waits else None},
            "queue_length": {"max": max(q) if q else 0, "mean": round(sum(q) / len(q), 2) if q else 0.0},
            "still_waiting_at_end": len(self.queue) + self.in_review,
        }
