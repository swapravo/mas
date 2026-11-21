"""Turn a finished simulation into one plain dict (what results.json holds and compare prints)."""
from __future__ import annotations

from collections import Counter


def percentile(values: list[float], p: float) -> float | None:
    """Nearest-rank percentile. Deterministic, no interpolation."""
    if not values:
        return None
    v = sorted(values)
    return round(v[max(0, min(len(v) - 1, -(-int(p * len(v) * 1000) // 100000) - 1))], 2)


def incidents(w, waivers: list[dict]) -> dict:
    """Distinct leak incidents -> the layer that should have stopped them.

    Three sources, de-duplicated: a check was off and the call went through (gateway/obo); a role was handed another
    department's row (rls); a waiver exists that no decision asked for (effect). The last one needs no switch to be off:
    it is how an injected instruction that reaches Finance's own tool shows up even with every layer on.
    """
    out: dict[tuple, str] = {}
    for g in w.gateway.leaks:
        out[(g["task"], "waiver") if g["tool"] == "finance.apply_waiver" else (g["task"], g["tool"], g["layer"])] = g["layer"]
    for r in w.store.leaks:
        out[(r["role"], r["kind"], r["key"])] = "rls"
    for r in waivers:
        if not r["payload"].get("step", "").endswith(":act.waive"):
            out.setdefault((r["payload"]["case_id"], "waiver"), "effect")
    return out


def summarize(sw) -> dict:
    s, w = sw.settings, sw.w
    rows = sorted(sw.registry.all(), key=lambda r: r["case_id"])
    done = [r for r in rows if r["status"] == "completed"]
    failed = [r for r in rows if r["status"] == "escalated_failure"]
    stuck = [r for r in rows if r["status"] not in ("completed", "escalated_failure")]
    times = [r["finished"] - r["started"] for r in done]
    noticed = Counter(r["noticed_by"] or "nobody" for r in failed + stuck)
    delays = [r["noticed_at"] - r["started"] for r in rows if r["noticed_at"] is not None]
    waivers, letters = w.store.scan("owner", "waiver_applied"), w.store.scan("owner", "notice_letter")

    def doubled(rs):
        c = Counter(r["payload"]["case_id"] for r in rs)
        return sum(1 for n in c.values() if n > 1)

    leaked = incidents(w, waivers)
    return {
        "mode": s.mode, "strategy": s.strategy, "seed": s.seed, "cases": len(rows),
        "messages": sw.net.sent,
        "routing_decisions": w.stats["routing"],
        "completion_seconds": {"p50": percentile(times, 50), "p95": percentile(times, 95),
                               "max": round(max(times), 2) if times else None},
        "outcomes": {"completed": len(done), "escalated_failure": len(failed), "stalled": len(stuck)},
        "noticed": {"by": dict(sorted(noticed.items())), "first_notice_after_seconds": round(min(delays), 2) if delays else None},
        "decisions": {"outcome": dict(Counter(r.get("outcome") for r in done)),
                      "decided_by": dict(Counter(r.get("decided_by") for r in done)),
                      "policy_blocked": w.stats["policy_blocked"]},
        "duplicates": {"injected": w.stats["dup_injected"], "suppressed": w.stats["dup_suppressed"],
                       "cases_with_double_waiver": doubled(waivers), "cases_with_double_notice": doubled(letters)},
        "effects": {"waivers": len(waivers), "notices": len(letters)},
        "leaked": {"count": len(leaked), "by_layer": dict(Counter(leaked.values())),
                   "attempts_blocked": len(w.gateway.blocked)},
        "faults": {"retries": w.stats["retries"], "escalations": w.stats["escalations"], "rejected_handoffs": w.stats["rejected"],
                   "lost_bus_down": w.stats["lost_bus_down"], "dead_letters": w.stats["dead_letters"],
                   "budget_rejects": w.stats["budget_rejects"], "llm_failures": w.stats["llm_failures"]},
        "per_case": [{"case": r["case_id"], "status": r["status"], "outcome": r.get("outcome"), "decided_by": r.get("decided_by"),
                      "seconds": round(r["finished"] - r["started"], 2) if r["finished"] is not None else None}
                     for r in rows],
    }
