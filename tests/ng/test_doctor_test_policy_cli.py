"""T2 doctor CLI: Test policy facts end to end through ``main``.

Strict TDD: written before ``src/ptest/policy_facts.py`` /
``src/ptest/policy_render.py``; failed at collection until they existed.

Seam: the doctor grid wiring (``cli._test_policy_outputs``) lands with the
barrier/T3. These tests pin everything T2 owns: the facts-to-text pipeline
on the same resolution doctor uses, human/JSON output compatibility, and
that doctor writes nothing. The orchestrator re-runs this file post-merge,
when the terminal block also appears after the grid.
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


def test_barrier_wiring_reports_through_doctor_when_present(
        tmp_path, monkeypatch, capsys):
    """Barrier call sites print the Test policy block after the grid."""
    import ptest.cli as cli_module
    _repo(tmp_path, files={
        "pyproject.toml": "[tool.coverage.report]\nfail_under = 85\n"})
    outputs = getattr(cli_module, "_test_policy_outputs", None)
    out = _human_output(monkeypatch, capsys, tmp_path)
    if outputs is None:
        # Barrier/T3 not merged yet: the block is produced by the T2
        # pipeline directly (proven above) and doctor output is unchanged.
        assert "Test policy" not in out
    else:
        assert "Test policy" in out


# ---- JSON stays byte-compatible ----

def test_doctor_json_has_no_test_policy_keys(tmp_path, monkeypatch, capsys):
    _repo(tmp_path, files={
        "pyproject.toml": "[tool.coverage.report]\nfail_under = 85\n",
        "AGENTS.md": "Keep coverage above 90%.\n"})
    payload = json.loads(_json_output(monkeypatch, capsys, tmp_path))
    assert "Test policy" not in json.dumps(payload)
    assert "test_policy" not in json.dumps(payload)


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
    _human_output(monkeypatch, capsys, tmp_path)
    assert _snapshot(tmp_path) == before
    assert not (tmp_path / "recommendations.md").exists()
