"""Run one scenario.

    python -m enterprise.run --mode central --strategy rules --cases 40 --break none --out results.json

All times are virtual seconds: the run takes milliseconds and gives the same answer every time for the same seed.
"""
from __future__ import annotations

import argparse
import json

from enterprise.cli import add_common, build, dump
from enterprise.settings import MODES


def describe(res: dict) -> str:
    o, c, d, a = res["outcomes"], res["completion_seconds"], res["duplicates"], res["approvals"]
    by = ", ".join(f"{k} x{v}" for k, v in res["noticed"]["by"].items()) or "nothing went wrong"
    lines = [
        f"messages {res['messages']}   routing decisions {res['routing_decisions']}",
        f"completion  p50 {c['p50']}s   p95 {c['p95']}s   (virtual seconds, completed cases)",
        f"cases       completed {o['completed']}   escalated-failure {o['escalated_failure']}   stalled {o['stalled']}",
        f"noticed by  {by}" + (f"   (first after {res['noticed']['first_notice_after_seconds']}s)"
                               if res["noticed"]["first_notice_after_seconds"] is not None else ""),
        f"decisions   {res['decisions']['outcome']}   decided by {res['decisions']['decided_by']}"
        + (f"   policy-blocked {res['decisions']['policy_blocked']}" if res["decisions"]["policy_blocked"] else ""),
        f"duplicates  injected {d['injected']}   suppressed {d['suppressed']}   double waivers {d['cases_with_double_waiver']}   "
        f"double notices {d['cases_with_double_notice']}",
        f"leaked      {res['leaked']['count']} {res['leaked']['by_layer'] or ''}   attacks blocked {res['leaked']['attempts_blocked']}",
        f"approvals   {a['submitted']} items, {a['auto_approved']} auto, {a['reviewed_by_people']} by people in "
        f"{a['review_sessions']} sessions; rushed {a['rushed_share']:.0%}; wait p50 {a['wait_seconds']['p50']}s "
        f"p95 {a['wait_seconds']['p95']}s; queue max {a['queue_length']['max']}",
    ]
    return "\n".join("  " + x for x in lines)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="python -m enterprise.run", description=__doc__.split("\n")[0])
    ap.add_argument("--mode", choices=MODES, default="central")
    ap.add_argument("--out", default="", help="write full results (and the decision cards) to this JSON file")
    ap.add_argument("--cards", type=int, default=2, help="how many decision cards to print as JSON")
    add_common(ap)
    args = ap.parse_args(argv)

    sim = build(args, args.mode)
    res = sim.run()
    print(f"mode={args.mode} strategy={args.strategy} cases={args.cases} seed={args.seed} break={args.brk} "
          f"watchdog={'on' if sim.settings.watchdog_on() else 'off'} defences={','.join(sim.w.defences.on()) or 'none'}")
    print(describe(res))
    if sim.cards and args.cards:
        print(f"\n  decision cards ({min(args.cards, len(sim.cards))} of {len(sim.cards)}):")
        for card in sim.cards[: args.cards]:
            print("  " + json.dumps(card))
    if args.out:
        dump(sim, res, args.out)
        print(f"\n  wrote {args.out}")


if __name__ == "__main__":
    main()
