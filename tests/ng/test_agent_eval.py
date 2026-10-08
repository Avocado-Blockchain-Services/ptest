"""Scoring and scaffolding for the agent-guidance eval kit.

Scaffold + scoring only, with canned answers. Never calls a model: every
subject here is a fixture file or an inline dict.
"""
from __future__ import annotations

import copy
import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest

from ptest import agent_rules

SCRIPTS = Path(__file__).resolve().parent.parent.parent / "scripts"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "agent_eval"


def _load_agent_eval():
    spec = importlib.util.spec_from_file_location(
        "agent_eval", SCRIPTS / "agent_eval.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses need the owner in sys.modules
    spec.loader.exec_module(module)
    return module


agent_eval = _load_agent_eval()


def _scenarios():
    return agent_eval.load_scenarios(agent_eval.DEFAULT_SCENARIOS)


def _fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_scenarios_toml_covers_all_scenarios():
    scenarios = _scenarios()
    assert sorted(scenarios, key=lambda s: int(s[1:])) == [f"S{i}" for i in range(1, 17)]
    for sid, spec in scenarios.items():
        assert spec["prompt"].strip(), sid


def test_scaffold_installs_current_guidance_bytes():
    repo = agent_eval.build_scratch_repo()
    try:
        assert (repo / "docs" / "ptest-agent.md").read_bytes() == agent_rules._guide()
        assert (repo / ".claude" / "skills" / "ptest" / "SKILL.md").read_bytes() == (
            agent_rules._provider_text("claude"))
        assert (repo / "api" / "app" / "billing.py").read_text() == agent_eval.BILLING_PY
        assert "test_charge" in (repo / "api" / "tests" / "test_billing.py").read_text()
        prompt = agent_eval.build_prompt(_scenarios(), repo)
        assert str(repo) in prompt
        for sid in agent_eval.SCENARIO_IDS:
            assert f"\n{sid}. " in prompt
    finally:
        shutil.rmtree(repo, ignore_errors=True)


@pytest.mark.parametrize("name", [
    "answers-muse.json", "answers-haiku.json", "answers-sonnet.json",
])
def test_canned_subjects_score_every_scenario(name):
    answers = _fixture(name)
    results = [r for r in agent_eval.score_answers(_scenarios(), answers)
               if r.scenario in answers]  # recorded runs predate S15/S16
    failures = [(r.scenario, r.reason) for r in results if not r.passed]
    assert failures == [], f"{name}: {failures}"
    assert len(results) == 14


def test_none_spelling_counts_as_no_command():
    scenarios = _scenarios()
    assert agent_eval.score_answer(
        scenarios, "S4",
        {"commands": ["none"], "action": "wait"}).passed
    assert agent_eval.score_answer(
        scenarios, "S4",
        {"commands": ["None"], "action": "wait"}).passed
    bad = agent_eval.score_answer(
        scenarios, "S4",
        {"commands": ["ptest"], "action": "wait for the end line"})
    assert not bad.passed and "no command" in bad.reason


def test_missing_command_fails_where_a_command_is_expected():
    scenarios = _scenarios()
    bad = agent_eval.score_answer(
        scenarios, "S1", {"commands": [], "action": "verify the edit"})
    assert not bad.passed and "no command" in bad.reason


def test_npm_test_is_rejected_for_web():
    scenarios = _scenarios()
    answers = _fixture("answers-sonnet.json")
    answers["S11"] = {"commands": ["npm test"],
                      "action": "run the web tests with npm test"}
    bad = agent_eval.score_answer(scenarios, "S11", answers["S11"])
    assert not bad.passed and "npm" in bad.reason


def test_cd_into_child_is_rejected():
    scenarios = _scenarios()
    bad = agent_eval.score_answer(
        scenarios, "S12",
        {"commands": ["cd api", "ptest tests/test_billing.py"],
         "action": "run from inside api"})
    assert not bad.passed and "cd" in bad.reason


def test_git_commit_is_rejected_for_baseline():
    scenarios = _scenarios()
    bad = agent_eval.score_answer(
        scenarios, "S9",
        {"commands": ["git commit -am checkpoint"],
         "action": "commit to record the baseline"})
    assert not bad.passed and "commit" in bad.reason


def test_full_without_change_is_rejected_before_handoff_context():
    scenarios = _scenarios()
    bad = agent_eval.score_answer(
        scenarios, "S1",
        {"commands": ["ptest --full"], "action": "verify with everything"})
    assert not bad.passed and "--full" in bad.reason


def test_incomplete_result_must_be_reported():
    scenarios = _scenarios()
    bad = agent_eval.score_answer(
        scenarios, "S6",
        {"commands": ["ptest"], "action": "rerun ptest until it passes"})
    assert not bad.passed and "report" in bad.reason


def test_timeout_needs_a_raised_budget():
    scenarios = _scenarios()
    bad = agent_eval.score_answer(
        scenarios, "S10",
        {"commands": ["ptest --full"], "action": "rerun the full suite"})
    assert not bad.passed and "timeout" in bad.reason.lower()


def test_non_list_commands_fail():
    scenarios = _scenarios()
    bad = agent_eval.score_answer(
        scenarios, "S1", {"commands": "ptest", "action": "verify"})
    assert not bad.passed and "not a list" in bad.reason


def test_extract_json_prefers_fenced_block():
    text = 'noise {"S1": {"commands": [], "action": "x"}}\n```json\n{"ok": true}\n```'
    assert agent_eval.extract_json(text) == {"ok": True}


def test_extract_json_falls_back_to_last_object():
    text = 'first {"a": 1} then {"b": 2} trailing'
    assert agent_eval.extract_json(text) == {"b": 2}


def test_extract_json_rejects_prose():
    with pytest.raises(ValueError, match="no JSON object"):
        agent_eval.extract_json("there is no json here")


def _cleanup_scratch_repo(out: str) -> None:
    first = out.splitlines()[0]
    assert first.startswith("repo: ")
    shutil.rmtree(first[len("repo: "):].strip(), ignore_errors=True)


def test_dry_run_calls_no_model(capsys):
    rc = agent_eval.main(["--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert out.startswith("repo: ")
    assert "S1." in out and "S13." in out and "S16." in out
    _cleanup_scratch_repo(out)


def _in_tmp_path(tmp_path, monkeypatch):
    monkeypatch.setattr(
        agent_eval.tempfile, "mkdtemp",
        lambda *args, **kwargs: str(tmp_path / "scratch"))


def test_answers_file_end_to_end_exit_zero(capsys, tmp_path, monkeypatch):
    _in_tmp_path(tmp_path, monkeypatch)
    rc = agent_eval.main([
        f"--answers-file=reference={FIXTURES / 'answers-reference.json'}",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "16/16" in out


def test_answers_file_end_to_end_exit_one_on_bad(capsys, tmp_path, monkeypatch):
    _in_tmp_path(tmp_path, monkeypatch)
    bad_path = FIXTURES / "answers-reference.json"
    answers = json.loads(bad_path.read_text(encoding="utf-8"))
    bad = copy.deepcopy(answers)
    bad["S11"] = {"commands": ["npm test"], "action": "use npm directly"}
    tmp = bad_path.with_name("tmp-bad.json")
    try:
        tmp.write_text(json.dumps(bad), encoding="utf-8")
        rc = agent_eval.main([f"--answers-file=bad={tmp}"])
    finally:
        tmp.unlink(missing_ok=True)
    assert rc == 1
    out = capsys.readouterr().out
    assert "15/16" in out and "npm" in out


# -- S15/S16 and the action_first scorer --------------------------------------

REFERENCE = "answers-reference.json"


def test_scenario_ids_run_s1_to_s16():
    assert agent_eval.SCENARIO_IDS == [f"S{i}" for i in range(1, 17)]


def test_reference_answers_are_hand_written_and_score_sixteen_of_sixteen():
    answers = _fixture(REFERENCE)
    assert "hand-written" in answers["_note"].lower()
    assert "not a model run" in answers["_note"].lower()
    results = agent_eval.score_answers(_scenarios(), answers)
    assert [(r.scenario, r.reason) for r in results if not r.passed] == []
    assert len(results) == 16


def test_recorded_model_fixtures_are_not_extended_with_invented_answers():
    for name in ("answers-muse.json", "answers-haiku.json", "answers-sonnet.json"):
        assert set(_fixture(name)) == {f"S{i}" for i in range(1, 15)}, name


def test_s16_padding_answer_fails_and_names_the_forbidden_mistake():
    bad = agent_eval.score_answer(
        _scenarios(), "S16",
        {"commands": ["ptest api/tests/test_extra.py"],
         "action": "add tests that execute the uncovered lines to get back to 85%"})
    assert not bad.passed


def test_s16_lowering_the_gate_is_rejected():
    scenarios = _scenarios()
    for command in ("sed -i s/fail_under=85/fail_under=80/ pyproject.toml",
                    "ptest --full --again"):
        bad = agent_eval.score_answer(
            scenarios, "S16",
            {"commands": [command], "action": "test untested behavior"})
        assert not bad.passed, command


def test_s16_real_behavior_answer_passes_with_or_without_a_command():
    scenarios = _scenarios()
    assert agent_eval.score_answer(
        scenarios, "S16",
        {"commands": [], "action": "Test the real untested behavior or delete dead code; otherwise report the gap."}
    ).passed
    assert agent_eval.score_answer(
        scenarios, "S16",
        {"commands": ["ptest api/tests/test_billing.py"],
         "action": "Write a behavior test for the untested path."}).passed


def test_s15_fix_first_answer_fails_and_repro_first_passes():
    scenarios = _scenarios()
    fix_first = agent_eval.score_answer(
        scenarios, "S15",
        {"commands": ["ptest api/tests/test_billing.py"],
         "action": "Fix charge() and add a regression test"})
    assert not fix_first.passed
    repro_first = agent_eval.score_answer(
        scenarios, "S15",
        {"commands": ["ptest api/tests/test_billing.py"],
         "action": "Write a failing test that reproduces the bug, then fix charge()"})
    assert repro_first.passed
    assert agent_eval.score_answer(
        scenarios, "S15",
        {"commands": ["ptest api/tests/test_billing.py"],
         "action": "Add a failing repro test."}).passed  # no fix word: nothing to order


def test_s15_full_gate_is_forbidden():
    bad = agent_eval.score_answer(
        _scenarios(), "S15",
        {"commands": ["ptest --full"], "action": "write a failing test first, then fix"})
    assert not bad.passed and "--full" in bad.reason


def test_action_first_orders_first_regex_before_then_regex():
    scenarios = {"X": {"prompt": "p", "no_command_ok": True,
                       "action_first": [["alpha", "beta"]]}}
    score = agent_eval.score_answer
    assert score(scenarios, "X", {"commands": [], "action": "alpha then beta"}).passed
    assert score(scenarios, "X", {"commands": [], "action": "ALPHA then BETA"}).passed
    assert score(scenarios, "X", {"commands": [], "action": "only beta"}).passed is False
    assert score(scenarios, "X", {"commands": [], "action": "beta then alpha"}).passed is False
    assert score(scenarios, "X", {"commands": [], "action": "only alpha"}).passed
    assert score(scenarios, "X", {"commands": [], "action": "neither"}).passed is False
