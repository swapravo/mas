"""Department boundaries: three attacks, three layers, and proof of which layer stops which attack."""
import os

import pytest

from enterprise import attacks
from enterprise.attacks import ATTACKS, CONFIGS, LAYER_FOR
from enterprise.envelope import Envelope
from enterprise.gateway import CallContext, Denied
from enterprise.settings import Defences, Settings
from enterprise.world import World
from harness import ToolError
from helpers import run

# leaked? for each attack under each defence set. Read it as: which layer matters for which attack.
EXPECT = {
    "direct_read":     {"no defences": True, "rls only": False, "gateway only": True,  "obo only": True,  "all on": False},
    "injection":       {"no defences": True, "rls only": True,  "gateway only": False, "obo only": True,  "all on": False},
    "confused_deputy": {"no defences": True, "rls only": True,  "gateway only": True,  "obo only": False, "all on": False},
}


def test_the_matrix_of_which_attack_leaks_when_which_layer_is_off():
    assert attacks.matrix() == EXPECT


@pytest.mark.parametrize("attack", ATTACKS)
def test_every_attack_leaks_with_no_defences(attack):
    out = attacks.run(attack, Defences.none())
    assert out.leaked and out.blocked_by is None, out


@pytest.mark.parametrize("attack", ATTACKS)
def test_every_attack_is_blocked_by_its_intended_layer(attack):
    layer = LAYER_FOR[attack]
    for defences in (Defences(), Defences.only(layer)):            # all on, and ONLY that layer on
        out = attacks.run(attack, defences)
        assert not out.leaked and out.blocked_by == layer, (attack, defences, out)


def test_the_confused_deputy_is_blocked_only_with_on_behalf_of_scoping():
    everything_but_obo = Defences(rls=True, gateway=True, obo=False)
    assert attacks.run("confused_deputy", everything_but_obo).leaked          # RLS and the gateway both said yes
    assert not attacks.run("confused_deputy", Defences.only("obo")).leaked    # obo alone is enough
    assert attacks.run("confused_deputy", everything_but_obo).evidence.startswith("finance's agent returned CANARY")


def test_a_layer_that_is_not_the_intended_one_does_not_stop_the_attack():
    for attack in ATTACKS:
        for layer in ("rls", "gateway", "obo"):
            if layer != LAYER_FOR[attack]:
                assert attacks.run(attack, Defences.only(layer)).leaked, (attack, layer)


# -- the gateway and on-behalf-of, one layer at a time -----------------------------------------------

def world_and_env(**s):
    from enterprise.attacks import _world
    w, mine, other = _world(Defences(), None)
    w.settings = Settings(**s) if s else w.settings
    env = Envelope(task_id=mine.case_id, goal=mine.goal, from_dept="orchestrator", to_dept="collections",
                   on_behalf_of=mine.principal, data_class="internal", deadline=900.0, schema_version=2, budget=40,
                   hop_count=1, approval_required=False, step="x")
    return w, env


def test_the_allow_list_says_who_may_call_what_and_every_refusal_is_audited():
    w, env = world_and_env()
    with pytest.raises(Denied, match="collections is not allowed to call legal.get_legal_notes"):
        w.gateway.call(CallContext("collections", env), "legal.get_legal_notes", {"resident_id": "r-0001"})
    refused = [r for r in w.store.audit_rows if r["action"] == "tool_refused"]
    assert refused and refused[0]["details"]["layer"] == "gateway"
    w.gateway.call(CallContext("legal", env), "legal.get_notices", {"resident_id": "r-0001"})
    assert [r["action"] for r in w.store.audit_rows][-1] == "tool_call"        # allowed calls are audited too


def test_on_behalf_of_travels_with_an_agent_to_agent_call():
    w, env = world_and_env()
    own = w.gateway.call(CallContext("collections", env), "ask_dept", {"dept": "finance", "what": "ledger", "resident_id": "r-0001"})
    assert own["data"]["amount"] == 1500                                         # a legitimate ask still works
    inner = [r for r in w.store.audit_rows if r["target"] == "finance.get_ledger" and r["action"] == "tool_call"][0]
    assert inner["details"]["role"] == "finance" and inner["details"]["on_behalf_of"] == "resident:r-0001"
    with pytest.raises(ToolError, match="finance could not answer"):              # for someone else's data: refused
        w.gateway.call(CallContext("collections", env), "ask_dept", {"dept": "finance", "what": "ledger", "resident_id": "r-0002"})


def test_agent_to_agent_calls_obey_the_hop_limit():
    w, env = world_and_env(max_hops=1)
    with pytest.raises(ToolError, match="hop_count 2 exceeds limit 1"):
        w.gateway.call(CallContext("collections", env), "ask_dept", {"dept": "finance", "what": "ledger", "resident_id": "r-0001"})


def test_the_gateway_catalogue_is_the_same_for_every_agent_but_calls_are_scoped_per_role():
    w, env = world_and_env()
    names = {t.name for t in w.gateway.catalogue()}
    assert {"finance.apply_waiver", "legal.get_legal_notes", "ops.get_repairs"} <= names
    assert w.gateway.allow["collections"] == {"collections.read_dispute", "collections.draft_reply", "ask_dept"}
    assert "finance.apply_waiver" not in w.gateway.allow["ops"] | w.gateway.allow["legal"] | w.gateway.allow["collections"]


# -- the same attacks inside a full simulation -------------------------------------------------------

def test_attack_cases_in_a_real_run_are_blocked_and_the_workflow_still_finishes():
    for mode in ("central", "hierarchical"):
        res = run(mode, cases=20, attack_every=5)
        assert res["leaked"]["count"] == 0 and res["leaked"]["attempts_blocked"] == 4, mode
        assert res["outcomes"]["completed"] == 20


def test_with_no_defences_the_same_run_leaks():
    res = run("central", cases=20, attack_every=5, defences=Defences.none())
    assert res["leaked"]["count"] == 4 and set(res["leaked"]["by_layer"]) == {"gateway", "obo"}


def test_events_that_carry_the_resident_text_widen_the_blast_radius():
    """Choreography: the brief event reaches every department. An injected instruction that lands in FINANCE
    calls Finance's own, legitimately allowed, waiver tool: no gateway rule can tell. Only keeping the text off the
    bus (or a decision reference the tool checks) helps."""
    central = run("central", cases=20, attack_every=5)
    wide = run("choreography", cases=20, attack_every=5)
    narrow = run("choreography", cases=20, attack_every=5, events_carry_text=False)
    assert central["leaked"]["count"] == 0
    assert wide["leaked"]["count"] > 0 and wide["leaked"]["by_layer"] == {"effect": wide["leaked"]["count"]}
    assert wide["leaked"]["attempts_blocked"] > central["leaked"]["attempts_blocked"]     # more agents exposed
    assert narrow["leaked"]["count"] == 0


# -- the same checks on real Postgres (skipped when it is not reachable) ----------------------------------

@pytest.fixture(scope="module")
def pg_ready():
    admin = os.environ.get("ENT_PG_ADMIN_DSN")
    if not admin:
        pytest.skip("set ENT_PG_ADMIN_DSN (a superuser DSN) to run the real-Postgres checks")
    from enterprise.pgstore import apply_schema
    try:
        apply_schema(admin)
    except Exception as exc:
        pytest.skip(f"postgres not available: {exc}")


@pytest.fixture
def pg(pg_ready):
    from enterprise.pgstore import PgStore
    made = []

    def make(rls=True):
        s = PgStore(rls=rls)
        s.reset()
        made.append(s)
        return s
    yield make
    for s in made:
        s.close()


@pytest.mark.parametrize("attack", ATTACKS)
@pytest.mark.parametrize("name", list(CONFIGS))
def test_postgres_gives_the_same_matrix_as_the_emulation(pg, attack, name):
    d = CONFIGS[name]
    assert attacks.run(attack, d, store=pg(rls=d.rls)).leaked == EXPECT[attack][name]


def test_postgres_rls_blocks_cross_department_writes_too(pg):
    from enterprise.store import RowLevelDenied
    s = pg()
    s.put("finance", "ledger", "r1", {"amount": 1})
    with pytest.raises(RowLevelDenied):
        s.put("ops", "ledger", "r2", {"amount": 1}, dept="finance")
    assert s.get("ops", "ledger", "r1", dept="finance") is None


def test_postgres_audit_log_is_insert_only(pg):
    s = pg()
    s.audit("agent", "tool_call", "x", {})
    with pytest.raises(Exception, match="insert only"):
        s._conn("owner").execute("DELETE FROM ent_audit")


def test_a_whole_simulation_runs_on_postgres_and_matches_the_in_memory_run(pg):
    from enterprise.runner import Simulation
    s = Settings(mode="central", strategy="priority", cases=8)
    on_pg = Simulation(s, store=pg()).run()
    assert on_pg == Simulation(Settings(mode="central", strategy="priority", cases=8)).run()
