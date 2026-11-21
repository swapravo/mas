"""Shared test helpers: run a simulation, and the oracle for what each case SHOULD decide."""
from enterprise.runner import Simulation
from enterprise.settings import Chaos, Defences, Settings

MODES = ("central", "hierarchical", "choreography")


def run(mode="central", strategy="priority", cases=16, chaos=None, defences=None, sim_only=False, **kw):
    s = Settings(mode=mode, strategy=strategy, cases=cases, **kw)
    sim = Simulation(s, chaos, defences)
    res = sim.run()
    return (sim, res) if sim_only else res


def by_case(res):
    return {c["case"]: c for c in res["per_case"]}


def expected_priority(case):
    """Legal > Finance > Ops: a live notice refuses; otherwise Finance's vote: small fee waives, large refuses."""
    return "waive" if (not case.notice_live and not case.large()) else "refuse"
