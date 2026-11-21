"""Small numeric helpers shared by metrics.py and approvals.py."""


def percentile(values: list[float], p: float) -> float | None:
    """Nearest-rank percentile. Deterministic, no interpolation."""
    if not values:
        return None
    v = sorted(values)
    return round(v[max(0, min(len(v) - 1, -(-int(p * len(v) * 1000) // 100000) - 1))], 2)
