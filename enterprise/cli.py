"""Command-line plumbing shared by `run` and `compare`: flags -> Settings, Chaos, Defences -> a Simulation."""
from __future__ import annotations

import argparse
import dataclasses
import json

from enterprise.runner import Simulation
from enterprise.settings import MODES, STRATEGIES, Chaos, Defences, Settings

COORDINATOR = {"central": "orchestrator", "hierarchical": "top", "choreography": "bus"}


def add_common(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--strategy", choices=STRATEGIES, default="rules", help="how conflicts are settled")
    ap.add_argument("--cases", type=int, default=40, help="how many disputes")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--break", dest="brk", choices=("none", "coordinator", "finance"), default="none",
                    help="coordinator = the orchestrator (central), the top orchestrator (hierarchical) or the event bus (choreography)")
    ap.add_argument("--kill", default="", help="advanced: comma list of components to kill, e.g. ops,risk")
    ap.add_argument("--kill-at", type=float, default=0.0, help="virtual second when the kill happens")
    ap.add_argument("--watchdog", choices=("default", "on", "off"), default="default",
                    help="default: on for orchestrated modes, off for choreography")
    ap.add_argument("--latency", type=float, default=0.0, help="extra seconds on every message hop")
    ap.add_argument("--slow", action="append", default=[], metavar="DEPT=SECONDS", help="extra seconds of work in one department")
    ap.add_argument("--duplicate", type=int, default=0, metavar="N", help="deliver every Nth message twice (1 = all)")
    ap.add_argument("--no-idempotency", action="store_true", help="switch idempotency off (to see what it was protecting)")
    ap.add_argument("--defences", default="all", help="all, none, or a comma list of rls,gateway,obo")
    ap.add_argument("--attack-every", type=int, default=10, help="every Nth case carries hostile resident text (0 = none)")
    ap.add_argument("--no-text-in-events", action="store_true", help="choreography: keep the resident's text off the event stream")
    ap.add_argument("--approvers", type=int, default=3)
    ap.add_argument("--review-time", type=float, default=45.0)
    ap.add_argument("--rush-threshold", type=int, default=6)
    ap.add_argument("--auto-approve", action="store_true", help="mitigation: low-risk items skip the approval queue")
    ap.add_argument("--batch", action="store_true", help="mitigation: review similar items together")
    ap.add_argument("--store", choices=("memory", "pg"), default="memory", help="pg needs ENT_PG_DSN and enterprise/schema.sql applied")
    ap.add_argument("--transport", choices=("memory", "redis"), default="memory",
                    help="redis: event stream and idempotency keys on real Redis (REDIS_URL)")


def defences_from(text: str) -> Defences:
    if text == "all":
        return Defences()
    if text == "none":
        return Defences.none()
    names = [n.strip() for n in text.split(",") if n.strip()]
    bad = set(names) - {"rls", "gateway", "obo"}
    if bad:
        raise SystemExit(f"unknown defence {sorted(bad)}; use all, none, or rls,gateway,obo")
    return Defences.only(*names)


def build(args, mode: str) -> Simulation:
    kill = [k for k in args.kill.split(",") if k]
    if args.brk == "coordinator":
        kill.append(COORDINATOR[mode])
    elif args.brk == "finance":
        kill.append("finance")
    slow = {}
    for item in args.slow:
        dept, _, sec = item.partition("=")
        slow[dept] = float(sec)
    chaos = Chaos(kill=tuple(kill), kill_at=args.kill_at, net_latency=args.latency, dept_latency=slow, duplicate_every=args.duplicate)
    settings = Settings(
        seed=args.seed, mode=mode, strategy=args.strategy, cases=args.cases, attack_every=args.attack_every,
        idempotent=not args.no_idempotency, events_carry_text=not args.no_text_in_events,
        watchdog={"default": None, "on": True, "off": False}[args.watchdog],
        approvers=args.approvers, review_time=args.review_time, rush_threshold=args.rush_threshold,
        auto_approve=args.auto_approve, batch=args.batch)
    defences, store, kv, log = defences_from(args.defences), None, None, None
    if args.store == "pg":
        from enterprise.pgstore import PgStore
        store = PgStore(rls=defences.rls)
        store.reset()
    if args.transport == "redis":
        import redis
        import config
        from enterprise.eventlog import RedisEventLog
        from enterprise.kv import RedisKV
        client = redis.Redis.from_url(config.REDIS_URL)
        kv = RedisKV(client)
        kv.clear()
        log = RedisEventLog(client)
    return Simulation(settings, chaos, defences, store=store, kv=kv, log=log)


def dump(sim: Simulation, res: dict, path: str) -> None:
    run = {"settings": dataclasses.asdict(sim.settings), "chaos": dataclasses.asdict(sim.w.chaos),
           "defences_on": sim.w.defences.on()}
    with open(path, "w") as f:
        json.dump({"run": run, "results": res, "decision_cards": sim.cards}, f, indent=2, default=str)
        f.write("\n")
