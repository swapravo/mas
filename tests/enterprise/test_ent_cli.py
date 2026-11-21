"""The two commands from the brief, run in-process."""
import json

import pytest

from enterprise import compare, run as run_cli


def test_run_prints_a_summary_the_decision_cards_as_json_and_writes_results(tmp_path, capsys):
    out = tmp_path / "results.json"
    run_cli.main(["--mode", "central", "--strategy", "rules", "--cases", "24", "--break", "none", "--out", str(out)])
    text = capsys.readouterr().out
    assert "mode=central strategy=rules cases=24" in text and "decision cards (2 of" in text
    card = json.loads(next(l for l in text.splitlines() if l.strip().startswith('{"case"')))
    assert set(card) >= {"case", "conflict", "options", "recommendation", "decide_by", "escalation_path"}
    doc = json.loads(out.read_text())
    assert set(doc) == {"run", "results", "decision_cards"} and doc["results"]["outcomes"]["completed"] == 24
    assert doc["run"]["settings"]["mode"] == "central" and len(doc["decision_cards"]) > 0


@pytest.mark.parametrize("mode, killed", [("central", "orchestrator"), ("hierarchical", "top"), ("choreography", "bus")])
def test_break_coordinator_kills_the_right_thing_in_each_mode(mode, killed, tmp_path):
    out = tmp_path / "r.json"
    run_cli.main(["--mode", mode, "--cases", "6", "--break", "coordinator", "--out", str(out)])
    doc = json.loads(out.read_text())
    assert doc["run"]["chaos"]["kill"] == [killed] and doc["results"]["outcomes"]["completed"] == 0


def test_break_finance_and_the_watchdog_flag(tmp_path):
    out = tmp_path / "r.json"
    run_cli.main(["--mode", "choreography", "--cases", "6", "--break", "finance", "--out", str(out)])
    assert json.loads(out.read_text())["results"]["noticed"]["by"] == {"nobody": 6}
    run_cli.main(["--mode", "choreography", "--cases", "6", "--break", "finance", "--watchdog", "on", "--out", str(out)])
    assert json.loads(out.read_text())["results"]["noticed"]["by"] == {"watchdog": 6}


def test_compare_prints_one_table_with_every_column_the_brief_names(capsys):
    compare.main(["--cases", "24"])
    text = capsys.readouterr().out
    for col in ("messages", "routing", "p50 s", "p95 s", "completed", "stalled", "who noticed", "dups inj/supp/double", "leaked"):
        assert col in text
    for mode in ("central", "hierarchical", "choreography"):
        assert mode in text


def test_compare_sweep_shows_who_notices_what(capsys):
    compare.main(["--cases", "12", "--sweep"])
    text = capsys.readouterr().out
    assert text.count("break=") == 3
    assert "orchestrator x12" in text and "risk x12" in text and "nobody x12" in text and "watchdog x12" in text


def test_defences_flag_changes_what_leaks(capsys, tmp_path):
    out = tmp_path / "r.json"
    run_cli.main(["--mode", "central", "--cases", "20", "--defences", "none", "--out", str(out)])
    assert json.loads(out.read_text())["results"]["leaked"]["count"] > 0
    run_cli.main(["--mode", "central", "--cases", "20", "--defences", "gateway,obo", "--out", str(out)])
    assert json.loads(out.read_text())["results"]["leaked"]["count"] == 0


def test_a_bad_defence_name_is_a_clear_error():
    with pytest.raises(SystemExit, match="unknown defence"):
        run_cli.main(["--defences", "rsl"])
