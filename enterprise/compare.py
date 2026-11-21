"""Run all three orchestration modes on the SAME cases and print one table.

    python -m enterprise.compare
    python -m enterprise.compare --break finance --strategy priority
    python -m enterprise.compare --sweep          # the table with nothing broken, then the coordinator killed, then Finance killed
"""
from __future__ import annotations

import argparse
import json

from enterprise.cli import add_common, build
from enterprise.settings import MODES

HEAD = ["mode", "messages", "routing", "p50 s", "p95 s", "completed", "escalated", "stalled", "who noticed", "dups inj/supp/double", "leaked"]


def row(res: dict) -> list[str]:
    o, d, n = res["outcomes"], res["duplicates"], res["noticed"]["by"]
    who = ", ".join(f"{k} x{v}" for k, v in n.items()) or "-"
    c = res["completion_seconds"]
    return [res["mode"], str(res["messages"]), str(res["routing_decisions"]), str(c["p50"] if c["p50"] is not None else "-"),
            str(c["p95"] if c["p95"] is not None else "-"), str(o["completed"]), str(o["escalated_failure"]), str(o["stalled"]), who,
            f"{d['injected']}/{d['suppressed']}/{d['cases_with_double_waiver'] + d['cases_with_double_notice']}",
            str(res["leaked"]["count"])]


def table(results: list[dict]) -> str:
    rows = [HEAD, *[row(r) for r in results]]
    w = [max(len(r[i]) for r in rows) for i in range(len(HEAD))]
    fmt = lambda r: "  ".join(x.ljust(w[i]) if i in (0, 8) else x.rjust(w[i]) for i, x in enumerate(r))   # noqa: E731
    return "\n".join([fmt(rows[0]), "  ".join("-" * x for x in w), *[fmt(r) for r in rows[1:]]])


def compare(args) -> list[dict]:
    return [build(args, m).run() for m in MODES]


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="python -m enterprise.compare", description=__doc__.split("\n")[0])
    ap.add_argument("--sweep", action="store_true", help="run with nothing broken, the coordinator killed, then Finance killed")
    ap.add_argument("--out", default="", help="write every result to this JSON file")
    add_common(ap)
    args = ap.parse_args(argv)
    out = {}
    for brk in (("none", "coordinator", "finance") if args.sweep else (args.brk,)):
        args.brk = brk
        results = compare(args)
        out[brk] = results
        print(f"\nstrategy={args.strategy} cases={args.cases} seed={args.seed} break={brk} "
              f"defences={args.defences} attacks every {args.attack_every}   (seconds are virtual)")
        print(table(results))
    if args.out:
        with open(args.out, "w") as f:
            json.dump(out, f, indent=2, default=str)
            f.write("\n")
        print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
