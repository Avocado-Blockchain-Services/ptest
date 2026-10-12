"""Spec 2026-10-12-reuse-opt-in: ``[runner] reuse`` puts a pytest project
without coverage into the full mode, so a repeat ``ptest --full`` on the same
clean tree can say "already verified"."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

import support
from ptest import config as config_api
from ptest import contracts as C
from ptest.adapters import pytest as pytest_adapter
from ptest.cli import main
from support import git_commit_all, init_git_repo, write_ptest_toml
from test_doctor_fix import _no_review, _write_pytest_project

_PROJECT = "ab" * 16


@pytest.fixture(autouse=True)
def _clear_native_pytest_environment(monkeypatch):
    """Committed-Git child runs stay independent of an outer pytest run."""
    for name in (
        "PYTEST_ADDOPTS", "PYTHONDONTWRITEBYTECODE", "PTEST_EXECUTION",
        "PTEST_RUN_ID", "PTEST_GRANT_NONCE",
        "PTEST_PYTEST_REPORT_PATH", "PTEST_PYTEST_ATTEMPT",
        "PTEST_PYTEST_EXECUTION", "PTEST_PYTEST_CHECKOUT_ROOT",
        "PTEST_PYTEST_CONFIG_PATH",
    ):
        monkeypatch.delenv(name, raising=False)


def _config(root: Path, *, kind: str = "pytest", extra: str = "") -> None:
    write_ptest_toml(root, kind=kind, launcher=(sys.executable,), args=(),
                     full_args=(), project_id=_PROJECT, runner_extra=extra)


def _resolve(root: Path):
    return config_api.resolve_config(root)


# --- R1: config grammar ---------------------------------------------------------

@pytest.mark.parametrize("value", ['"true"', "1", "0", "[]", "{}", '"yes"'])
def test_reuse_must_be_a_boolean(tmp_path, value):
    """R1 hostile input: only a TOML boolean is accepted, before anything runs."""
    (tmp_path / "tests").mkdir()
    _config(tmp_path, extra=f"reuse = {value}")
    resolution = _resolve(tmp_path)
    assert resolution.config is None
    assert resolution.problem is not None
    assert resolution.problem.code == "invalid-config"


@pytest.mark.parametrize("kind", ["vitest", "go", "cargo", "command"])
def test_reuse_is_pytest_only(tmp_path, kind):
    """R1: the key on another runner is a config error, never silently ignored."""
    (tmp_path / "tests").mkdir()
    _config(tmp_path, kind=kind, extra="reuse = true")
    resolution = _resolve(tmp_path)
    assert resolution.config is None
    assert resolution.problem is not None
    assert resolution.problem.code == "invalid-config"


@pytest.mark.parametrize("value", [True, False])
def test_reuse_parses_and_round_trips(tmp_path, value):
    """R1: the writer emits ``reuse = true`` only when set, and reads back."""
    (tmp_path / "tests").mkdir()
    _config(tmp_path, extra=f"reuse = {str(value).lower()}")
    config = _resolve(tmp_path).config
    assert config.runner.reuse is value
    text = config_api._serialize_fresh(config).decode()
    assert ("reuse = true" in text) is value
    (tmp_path / ".ptest.toml").write_text(text)
    assert _resolve(tmp_path).config.runner.reuse is value


def test_reuse_default_is_off(tmp_path):
    (tmp_path / "tests").mkdir()
    _config(tmp_path)
    assert _resolve(tmp_path).config.runner.reuse is False


# --- R2 / R4: entry and identity -------------------------------------------------

def test_reuse_qualifies_a_project_without_coverage(tmp_path):
    """R2: reuse = true is an entry ticket to the full mode, like coverage."""
    (tmp_path / "tests").mkdir()
    _config(tmp_path)
    assert pytest_adapter.qualified_profile(_resolve(tmp_path).config) is None
    _config(tmp_path, extra="reuse = true")
    profile = pytest_adapter.qualified_profile(_resolve(tmp_path).config)
    assert profile is not None
    assert pytest_adapter.compound_support(
        _resolve(tmp_path).config, qualified_profile=profile).profile is not None


def test_reuse_does_not_admit_unowned_controls(tmp_path):
    """R2 negative: the key never bypasses the ptest-owned-controls check."""
    (tmp_path / "tests").mkdir()
    write_ptest_toml(tmp_path, kind="pytest", launcher=(sys.executable,),
                     args=("-n", "4"), full_args=(), project_id=_PROJECT,
                     runner_extra="reuse = true")
    assert pytest_adapter.qualified_profile(_resolve(tmp_path).config) is None


def test_reuse_is_not_part_of_the_compatibility_fingerprint():
    """R4: toggling the key changes no runtime/compatibility identity."""
    base = dict(kind=C.RunnerKind.PYTEST, launcher=("python",),
                test_roots=("tests",))
    assert repr(C.RunnerConfig(**base)) == repr(C.RunnerConfig(**base, reuse=True))


def test_reuse_on_a_non_pytest_runner_config_is_rejected():
    with pytest.raises(ValueError):
        C.RunnerConfig(kind=C.RunnerKind.VITEST, launcher=("npx", "vitest"),
                       test_roots=("tests",), reuse=True)


# --- R3 / R5 / R6: end to end ---------------------------------------------------

def _project(case, domain, *, extra: str = "reuse = true",
             test_source: str = "def test_a():\n    assert True\n",
             conftest: str | None = None) -> Path:
    root = case.project(domain, kind="pytest")
    # Real projects ignore interpreter caches.
    files = {"tests/test_a.py": test_source,
             ".gitignore": "__pycache__/\n.pytest_cache/\n"}
    if conftest is not None:
        files["tests/conftest.py"] = conftest
    for relative, content in files.items():
        support.write_file(root / relative, content)
    _config(root, extra=extra)
    init_git_repo(root)
    return root


# A conftest that silently narrows the inventory: both modes refuse it.
_NARROWING_CONFTEST = ("def pytest_configure(config):\n"
                       "    config.option.keyword = 'test_a'\n")


def _full(case, domain, root):
    """One ``ptest --full``; the harness's own result export is removed so
    the next run sees the tree the project left, not the harness's file."""
    completed = case.invoke(domain, root, "--full", timeout=60)
    for exported in root.glob(f"{support.RESULT_EXPORT_PREFIX}*.json"):
        exported.unlink()
    return completed


def _verified(completed) -> bool:
    return b"already verified" in completed.stderr


def test_opted_in_repeat_full_is_already_verified(case):
    """R3: the first opted-in --full records proof; the repeat on the same
    clean tree skips. The end line points at plain --full (R5)."""
    domain = case.domain(slots=1, jobs=1)
    root = _project(case, domain)

    first = _full(case, domain, root)
    assert first.code == 0, first.stderr.decode()
    assert not _verified(first)
    assert first.result["data"]["baseline_published"] is True
    assert b"--cov" not in first.stderr

    second = _full(case, domain, root)
    assert second.code == 0, second.stderr.decode()
    assert _verified(second), second.stderr.decode()


def test_without_the_key_a_repeat_full_runs_again(case):
    """Regression pin (0.6.2 behaviour kept): no key, no proof, no skip."""
    domain = case.domain(slots=1, jobs=1)
    root = _project(case, domain, extra="")

    for _ in range(2):
        completed = _full(case, domain, root)
        assert completed.code == 0, completed.stderr.decode()
        assert not _verified(completed)


def test_normal_mode_green_is_not_proof_after_opting_in(case):
    """R3 negative: a green earned before the opt-in never satisfies the
    full mode's skip."""
    domain = case.domain(slots=1, jobs=1)
    root = _project(case, domain, extra="")
    assert _full(case, domain, root).code == 0
    _config(root, extra="reuse = true")
    git_commit_all(root, "opt in")

    completed = _full(case, domain, root)

    assert completed.code == 0, completed.stderr.decode()
    assert not _verified(completed)


def test_opted_in_failing_full_records_no_proof(case):
    """R3 negative: a failure never becomes a baseline or a skip."""
    domain = case.domain(slots=1, jobs=1)
    root = _project(case, domain,
                    test_source="def test_a():\n    assert False\n")

    first = _full(case, domain, root)
    assert first.code == 1
    assert first.result["data"]["baseline_published"] is False
    second = _full(case, domain, root)
    assert second.code == 1
    assert not _verified(second)


def test_refused_opted_in_project_names_the_fix(case):
    """R6 + R3 negative: a conftest that narrows the inventory is refused,
    records no proof, and the end line says how to get the normal mode back."""
    domain = case.domain(slots=1, jobs=1)
    root = _project(case, domain, conftest=_NARROWING_CONFTEST)

    completed = _full(case, domain, root)

    assert completed.code != 0
    assert completed.result["data"]["baseline_published"] is False
    assert b"remove reuse = true from .ptest.toml" in completed.stderr, (
        completed.stderr.decode())


def test_refusal_line_is_absent_when_coverage_also_qualifies(case):
    """R6 negative: removing the key would not change the mode of a project
    that declares coverage, so no such advice is printed."""
    domain = case.domain(slots=1, jobs=1)
    root = _project(case, domain, extra=(
        'reuse = true\ncoverage_args = ["--cov", "--cov-report", "term"]'),
        conftest=_NARROWING_CONFTEST)

    completed = _full(case, domain, root)

    assert completed.code != 0
    assert b"remove reuse = true" not in completed.stderr


# --- R1 / R7: doctor ---------------------------------------------------------------

def test_doctor_fix_keeps_the_key(tmp_path, monkeypatch, capsys):
    """R1: doctor --fix never removes reuse (here it also moves --cov)."""
    root = _write_pytest_project(tmp_path / "keep")
    write_ptest_toml(root, kind="pytest",
                     launcher=("uv", "run", "--locked", "--no-sync", "python"),
                     args=("--cov=pkg", "--cov-report=term"), project_id=_PROJECT,
                     runner_extra="reuse = true")
    monkeypatch.chdir(root)
    _no_review(monkeypatch)

    assert main(("doctor", "--fix")) == 0
    capsys.readouterr()
    runner = _resolve(root).config.runner
    assert runner.reuse is True
    assert runner.coverage_args == ("--cov=pkg", "--cov-report=term")


def test_doctor_fix_never_adds_the_key(tmp_path, monkeypatch, capsys):
    """R7: doctor --fix runs no tests, so it cannot know the project qualifies."""
    root = _write_pytest_project(tmp_path / "plain")
    write_ptest_toml(root, kind="pytest",
                     launcher=("uv", "run", "--locked", "--no-sync", "python"),
                     args=("-x",), project_id=_PROJECT)
    monkeypatch.chdir(root)
    _no_review(monkeypatch)

    assert main(("doctor", "--fix")) == 0
    capsys.readouterr()
    assert "reuse" not in (root / ".ptest.toml").read_text()
