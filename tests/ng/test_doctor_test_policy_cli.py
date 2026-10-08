"""T2 doctor CLI: Test policy facts end to end through ``main``.

Strict TDD: written before ``src/ptest/policy_facts.py`` /
``src/ptest/policy_render.py``; failed at collection until they existed.

These tests pin everything T2 owns end to end through ``main``: the
facts-to-text pipeline on the same resolution doctor uses, the Test policy
block after the grid (via the barrier ``cli._test_policy_outputs`` call
sites), human/JSON output compatibility, and that doctor writes nothing.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ptest import config as config_api
from ptest.cli import main
from support import write_file, write_ptest_toml

PID = "ab" * 16


def _repo(root: Path, *, files=None):
    root.mkdir(parents=True, exist_ok=True)
    write_ptest_toml(root, kind="command", launcher=("true",), args=(),
                     full_args=(), test_roots=(), workers=1,
                     project_id=PID)
    for rel, text in (files or {}).items():
        write_file(root / rel, text)
    return root


def _snapshot(root: Path):
    return {path: (path.stat().st_mtime_ns, path.read_bytes())
            for path in root.rglob("*") if path.is_file()}


def _human_output(monkeypatch, capsys, root: Path):
    monkeypatch.chdir(root)
    assert main(("doctor", "--offline")) == 0
    return capsys.readouterr().out


def _json_output(monkeypatch, capsys, root: Path):
    monkeypatch.chdir(root)
    assert main(("doctor", "--offline", "--json")) == 0
    return capsys.readouterr().out


# ---- facts-to-text pipeline on doctor's own resolution ----

def test_policy_text_matches_doctor_resolution(tmp_path):
    from ptest import policy_facts, policy_render
    _repo(tmp_path, files={
        "pyproject.toml":
        "[tool.coverage.report]\nfail_under = 85\n"
        "[tool.coverage.run]\nbranch = true\n",
        "AGENTS.md": "Keep coverage above 90%.\n"})
    resolution = config_api.resolve_config(tmp_path)
    report = policy_facts.collect(resolution)
    text = policy_render.render_terminal(report)
    assert text.startswith("\n")
    assert "Test policy" in text
    assert "85" in text


def test_doctor_offline_human_output_mentions_grid(tmp_path, monkeypatch,
                                                   capsys):
    _repo(tmp_path)
    out = _human_output(monkeypatch, capsys, tmp_path)
    assert "· offline ·" in out
    assert "checks" in out


def test_doctor_offline_prints_test_policy_block_after_grid(
        tmp_path, monkeypatch, capsys):
    """Barrier call sites print the Test policy block after the grid."""
    _repo(tmp_path, files={
        "pyproject.toml": "[tool.coverage.report]\nfail_under = 85\n"})
    out = _human_output(monkeypatch, capsys, tmp_path)
    assert "Test policy" in out
    assert out.index("Test policy") > out.index("· offline ·")


# ---- JSON stays byte-compatible ----

def test_doctor_json_has_no_test_policy_keys(tmp_path, monkeypatch, capsys):
    _repo(tmp_path, files={
        "pyproject.toml": "[tool.coverage.report]\nfail_under = 85\n",
        "AGENTS.md": "Keep coverage above 90%.\n"})
    payload = json.loads(_json_output(monkeypatch, capsys, tmp_path))
    assert "Test policy" not in json.dumps(payload)
    assert "test_policy" not in json.dumps(payload)


def test_doctor_json_identical_with_facts_toggled(tmp_path, monkeypatch,
                                                      capsys):
    """Design: JSON stdout byte-identical with the facts toggled.

    Same fixture; the toggle flips the facts (real ``collect`` versus an
    empty ``PolicyReport``), not the files. The human output must change
    across the toggle (proving the facts were really on in one arm) while
    ``--json`` stdout stays byte-identical.
    """
    from ptest import policy_facts
    _repo(tmp_path, files={
        "pyproject.toml": "[tool.coverage.report]\nfail_under = 85\n",
        "AGENTS.md": "Keep coverage above 90%.\n"})
    monkeypatch.chdir(tmp_path)
    assert main(("doctor", "--offline")) == 0
    human_with_facts = capsys.readouterr().out
    assert "Test policy" in human_with_facts
    assert main(("doctor", "--offline", "--json")) == 0
    json_with_facts = capsys.readouterr().out
    monkeypatch.setattr(policy_facts, "collect",
                        lambda resolution: policy_facts.PolicyReport())
    assert main(("doctor", "--offline")) == 0
    human_without_facts = capsys.readouterr().out
    assert "Test policy" not in human_without_facts
    assert main(("doctor", "--offline", "--json")) == 0
    json_without_facts = capsys.readouterr().out
    assert json_with_facts == json_without_facts
    assert "Test policy" not in json_with_facts


def test_doctor_json_deterministic_on_fixed_fixture(tmp_path, monkeypatch,
                                                      capsys):
    # Repo content legitimately flows into JSON through the pre-existing
    # static path (excerpts/limitations), so toggling files cannot be
    # byte-identical. The T2 guarantee instead: no policy keys leak into
    # JSON, gate text never appears there, and a fixed fixture is stable.
    _repo(tmp_path, files={
        "pyproject.toml": "[tool.coverage.report]\nfail_under = 85\n",
        "AGENTS.md": "Keep coverage above 90%.\n"})
    first = _json_output(monkeypatch, capsys, tmp_path)
    second = _json_output(monkeypatch, capsys, tmp_path)
    assert first == second
    assert "coverage gate" not in first
    assert "fail_under" not in first


# ---- doctor writes nothing ----

def test_doctor_offline_writes_nothing(tmp_path, monkeypatch, capsys):
    _repo(tmp_path, files={
        "pyproject.toml": "[tool.coverage.report]\nfail_under = 85\n",
        "AGENTS.md": "Keep coverage above 90%.\n"})
    before = _snapshot(tmp_path)
    out = _human_output(monkeypatch, capsys, tmp_path)
    # T2 code ran inside the measured window: the block is printed.
    assert "Test policy" in out
    assert _snapshot(tmp_path) == before
    assert not (tmp_path / "recommendations.md").exists()
