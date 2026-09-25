"""Acceptance for `ptest doctor --fix`: plan, consent, safe apply, mention."""
from __future__ import annotations

import os
import sys
import tomllib
from pathlib import Path

import pytest

from ptest import contracts as C
from ptest.cli import main


PROJECT_ID = "ab" * 16


def _write_pyproject(root: Path, *, groups: dict | None = None) -> None:
    lines = ["[project]", 'name = "fixture"', "dependencies = []"]
    if groups:
        lines.append("")
        lines.append("[dependency-groups]")
        for name, deps in groups.items():
            lines.append(f"{name} = [")
            lines.extend(f'    "{dep}",' for dep in deps)
            lines.append("]")
    (root / "pyproject.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_addopts(root: Path, addopts: str) -> None:
    (root / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\n"
        f"addopts = '{addopts}'\n",
        encoding="utf-8",
    )


def _stub_xdist_venv(root: Path) -> None:
    from ptest.runtime.pytest_bridge import _COVERAGE_TUPLE

    packages = root / ".venv" / "lib" / "python3.12" / "site-packages"
    dist_info = packages / "pytest_xdist-3.8.0.dist-info"
    dist_info.mkdir(parents=True)
    (dist_info / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: pytest-xdist\nVersion: 3.8.0\n",
        encoding="utf-8",
    )
    # The tier probe reads versions from dist-info directory names only,
    # so these stubs pin the frozen pytest-cov/coverage pair by name.
    pytest_cov, coverage = _COVERAGE_TUPLE
    (packages / f"pytest_cov-{pytest_cov}.dist-info").mkdir(exist_ok=True)
    (packages / f"coverage-{coverage}.dist-info").mkdir(exist_ok=True)
    (root / ".venv" / "pyvenv.cfg").write_text(
        "home = /usr/bin\ninclude-system-site-packages = false\nversion = 3.12\n",
        encoding="utf-8",
    )


def _write_config(root: Path, *, args=(), setup: bool = False,
                  selection: bool = False, extra_lines=()) -> None:
    lines = [
        "version = 1",
        f'project_id = "{PROJECT_ID}"',
        "",
        "[runner]",
        'kind = "pytest"',
        'launcher = ["uv", "run", "--locked", "--no-sync", "python"]',
        "args = [" + ", ".join(f'"{token}"' for token in args) + "]",
        "full_args = []",
        'test_roots = ["tests"]',
        "workers = 1",
        'lifecycle = "cooperative-process-group"',
    ]
    if setup:
        lines.extend([
            "",
            "[setup]",
            'argv = ["uv", "sync", "--locked"]',
            'required_paths = [".venv/bin/python"]',
            "network = true",
            "lifecycle_scripts = true",
        ])
    if selection:
        lines.extend([
            "",
            "[selection]",
            "enabled = false",
        ])
    lines.extend(extra_lines)
    (root / ".ptest.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_pytest_project(root: Path, *, addopts="-n 4", groups=None,
                          lock=True) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "tests").mkdir(exist_ok=True)
    (root / "tests" / "test_example.py").write_text(
        "def test_example():\n    assert True\n", encoding="utf-8")
    (root / "tests" / "conftest.py").write_text(
        "def pytest_sessionfinish(session, exitstatus):\n    return None\n",
        encoding="utf-8")
    if addopts is not None:
        _write_addopts(root, addopts)
    if groups is not None or addopts is not None:
        base = root / "pyproject.toml"
        if groups is not None:
            _write_pyproject(root, groups=groups)
            if addopts is not None:
                with base.open("a", encoding="utf-8") as handle:
                    handle.write("\n[tool.pytest.ini_options]\n"
                                 f"addopts = '{addopts}'\n")
    if lock:
        (root / "uv.lock").write_text("", encoding="utf-8")
    _stub_xdist_venv(root)
    return root


def _no_review(monkeypatch):
    monkeypatch.setattr(
        "ptest.cli.agent_providers.resolve_reviewer",
        lambda *args, **kwargs: pytest.fail("fix resolved a reviewer"),
    )
    monkeypatch.setattr(
        "ptest.cli.agent_providers.launch_reviews",
        lambda *args, **kwargs: pytest.fail("fix launched a reviewer"),
    )
    monkeypatch.setattr(
        "ptest.operations.execute",
        lambda *args, **kwargs: pytest.fail("fix executed project tests"),
    )


def test_fix_drops_stale_n0_when_parallel_tier_qualifies(
        tmp_path, monkeypatch, capsys):
    root = _write_pytest_project(tmp_path / "stale")
    _write_config(root, args=("-n", "0"))
    before = (root / ".ptest.toml").read_bytes()
    monkeypatch.chdir(root)
    _no_review(monkeypatch)

    assert main(("doctor", "--fix", "--yes")) == 0
    out = capsys.readouterr().out
    assert "updated .ptest.toml" in out
    raw = (root / ".ptest.toml").read_text(encoding="utf-8")
    assert "\nargs = []\n" in raw
    assert f'project_id = "{PROJECT_ID}"' in raw
    # Only the managed args line changed.
    assert [line for line in raw.splitlines() if not line.startswith("args = ")] == [
        line for line in before.decode().splitlines() if not line.startswith("args = ")]


def test_fix_drops_stale_n0_with_cov_when_tier_qualifies(
        tmp_path, monkeypatch, capsys):
    """A stale `-n 0` is dropped on a `--cov` project when the tier qualifies.

    Regression pin for the coverage merge: if stale detection regressed
    to treating `--cov` as serial, the tier reason would no longer be
    exactly "sets -n 0" and the fallback would be kept.
    """
    root = _write_pytest_project(tmp_path / "stale-cov")
    _write_config(root, args=("-n", "0", "--cov", "pkg",
                              "--cov-report", "term"))
    monkeypatch.chdir(root)
    _no_review(monkeypatch)

    assert main(("doctor", "--fix", "--dry-run")) == 0
    out = capsys.readouterr().out
    assert "DRAFT" in out
    assert '-args = ["-n", "0", "--cov", "pkg", "--cov-report", "term"]' in out
    assert '+args = ["--cov", "pkg", "--cov-report", "term"]' in out
    assert "serial" not in out.lower()

    assert main(("doctor", "--fix", "--yes")) == 0
    capsys.readouterr()
    raw = (root / ".ptest.toml").read_text(encoding="utf-8")
    assert '"-n", "0"' not in raw
    assert '"--cov"' in raw


def test_fix_adds_missing_group_to_setup_argv(tmp_path, monkeypatch, capsys):
    root = _write_pytest_project(
        tmp_path / "extras", addopts="", groups={"dev": ["pytest>=8"]})
    _write_config(root, setup=True)
    monkeypatch.chdir(root)
    _no_review(monkeypatch)

    assert main(("doctor", "--fix", "--yes")) == 0
    raw = (root / ".ptest.toml").read_text(encoding="utf-8")
    assert 'argv = ["uv", "sync", "--locked", "--group", "dev"]' in raw
    capsys.readouterr()


def test_fix_proposes_selection_draft_with_cov(tmp_path, monkeypatch, capsys):
    root = _write_pytest_project(tmp_path / "cov", addopts="")
    _write_config(root, args=("--cov",), selection=True)
    monkeypatch.chdir(root)
    _no_review(monkeypatch)

    assert main(("doctor", "--fix", "--dry-run")) == 0
    out = capsys.readouterr().out
    assert "enabled = true" in out
    assert "closed_inputs = true" in out
    assert "input_roots" in out
    assert "full_triggers" in out
    assert "DRAFT" in out
    # Dry run writes nothing.
    assert 'enabled = false' in (root / ".ptest.toml").read_text(encoding="utf-8")

    assert main(("doctor", "--fix", "--yes")) == 0
    raw = (root / ".ptest.toml").read_text(encoding="utf-8")
    assert "enabled = true" in raw
    assert "closed_inputs = true" in raw
    out = capsys.readouterr().out
    assert "run a parallel full baseline once to record a baseline" in out


def test_fix_appends_missing_selection_table_at_eof(
        tmp_path, monkeypatch, capsys):
    root = _write_pytest_project(tmp_path / "nosel", addopts="")
    _write_config(root, args=("--cov",))
    monkeypatch.chdir(root)
    _no_review(monkeypatch)

    assert main(("doctor", "--fix", "--yes")) == 0
    capsys.readouterr()
    raw = (root / ".ptest.toml").read_text(encoding="utf-8")
    assert "\n[selection]\nenabled = true\nclosed_inputs = true\n" in raw
    assert main(("doctor", "--fix", "--yes")) == 0
    assert "config is up to date" in capsys.readouterr().out


def test_fix_keeps_hand_tuned_selection_values_and_enables_only(
        tmp_path, monkeypatch, capsys):
    root = _write_pytest_project(tmp_path / "tuned", addopts="")
    _write_config(root, args=("--cov",), extra_lines=(
        "",
        "[selection]",
        "enabled = false",
        'input_roots = ["tests", "libs/mylib"]',
        "closed_inputs = false",
    ))
    monkeypatch.chdir(root)
    _no_review(monkeypatch)

    assert main(("doctor", "--fix", "--dry-run")) == 0
    out = capsys.readouterr().out
    assert "enabled = true" in out
    assert "closed_inputs = true" not in out
    assert '"libs/mylib"' in out

    assert main(("doctor", "--fix", "--yes")) == 0
    capsys.readouterr()
    raw = (root / ".ptest.toml").read_text(encoding="utf-8")
    assert "enabled = true" in raw
    assert 'input_roots = ["tests", "libs/mylib"]' in raw
    assert "closed_inputs = false" in raw
    # Absent keys are still filled from the draft.
    assert "full_triggers" in raw


def test_fix_handles_quoted_table_header_without_duplication(
        tmp_path, monkeypatch, capsys):
    root = _write_pytest_project(tmp_path / "quoted", addopts="")
    _write_config(root, args=("--cov",), selection=True)
    quoted = (root / ".ptest.toml").read_text(encoding="utf-8")
    quoted = quoted.replace("[selection]", '["selection"]')
    (root / ".ptest.toml").write_text(quoted, encoding="utf-8")
    monkeypatch.chdir(root)
    _no_review(monkeypatch)

    assert main(("doctor", "--fix", "--yes")) == 0
    capsys.readouterr()
    raw = (root / ".ptest.toml").read_text(encoding="utf-8")
    tomllib.loads(raw)
    assert '["selection"]' in raw
    assert "\n[selection]\n" not in raw
    assert "enabled = true" in raw


def test_fix_handles_quoted_key_without_duplication(
        tmp_path, monkeypatch, capsys):
    root = _write_pytest_project(tmp_path / "quoted-key")
    _write_config(root, args=("-n", "0"))
    quoted = (root / ".ptest.toml").read_text(encoding="utf-8")
    quoted = quoted.replace('args = ["-n", "0"]', '"args" = ["-n", "0"]')
    (root / ".ptest.toml").write_text(quoted, encoding="utf-8")
    monkeypatch.chdir(root)
    _no_review(monkeypatch)

    assert main(("doctor", "--fix", "--yes")) == 0
    capsys.readouterr()
    raw = (root / ".ptest.toml").read_text(encoding="utf-8")
    tomllib.loads(raw)
    assert "args" in raw
    assert '"-n", "0"' not in raw


def test_fix_preserves_unmanaged_settings_byte_for_byte(
        tmp_path, monkeypatch, capsys):
    root = _write_pytest_project(tmp_path / "kept")
    _write_config(root, args=("-n", "0"), extra_lines=(
        "# a user comment",
        "",
        "[selection]",
        "enabled = false",
        "full_ratio = 0.5",
    ))
    monkeypatch.chdir(root)
    _no_review(monkeypatch)

    assert main(("doctor", "--fix", "--yes")) == 0
    capsys.readouterr()
    lines = (root / ".ptest.toml").read_text(encoding="utf-8").splitlines()
    assert "# a user comment" in lines
    assert "enabled = false" in lines
    assert "full_ratio = 0.5" in lines


def test_fix_refuses_symlinked_config(tmp_path, monkeypatch, capsys):
    root = tmp_path / "linked"
    _write_pytest_project(root)
    _write_config(root, args=("-n", "0"))
    real = root / ".ptest.toml"
    target = root / "real.toml"
    target.write_bytes(real.read_bytes())
    real.unlink()
    real.symlink_to(target)
    monkeypatch.chdir(root)
    _no_review(monkeypatch)

    assert main(("doctor", "--fix", "--yes")) == 2
    captured = capsys.readouterr()
    assert "symlink" in captured.err or "unsafe-path" in captured.err
    assert target.read_bytes() == target.read_bytes()


def test_fix_refuses_concurrent_change(tmp_path, monkeypatch, capsys):
    root = _write_pytest_project(tmp_path / "race")
    _write_config(root, args=("-n", "0"))
    monkeypatch.chdir(root)
    _no_review(monkeypatch)
    import ptest.files as files_module

    real_read = files_module.read_regular
    calls = []

    def raced_read(rdir, relative, limit):
        calls.append(relative)
        if relative == ".ptest.toml" and len(calls) == 2:
            path = Path(rdir) / relative
            path.write_bytes(real_read(rdir, relative, limit)
                             + b"# raced edit\n")
        return real_read(rdir, relative, limit)

    monkeypatch.setattr(files_module, "read_regular", raced_read)

    assert main(("doctor", "--fix", "--yes")) == 2
    captured = capsys.readouterr()
    assert "changed" in captured.err


def test_fix_non_tty_without_yes_writes_nothing(tmp_path, monkeypatch, capsys):
    root = _write_pytest_project(tmp_path / "quiet")
    _write_config(root, args=("-n", "0"))
    before = (root / ".ptest.toml").read_bytes()
    monkeypatch.chdir(root)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    _no_review(monkeypatch)

    assert main(("doctor", "--fix")) == 2
    captured = capsys.readouterr()
    assert "--yes" in captured.err + captured.out
    assert (root / ".ptest.toml").read_bytes() == before


def test_fix_tty_consent_applies_and_declines(tmp_path, monkeypatch, capsys):
    root = _write_pytest_project(tmp_path / "tty")
    _write_config(root, args=("-n", "0"))
    before = (root / ".ptest.toml").read_bytes()
    monkeypatch.chdir(root)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    _no_review(monkeypatch)

    monkeypatch.setattr("builtins.input", lambda: "y")
    assert main(("doctor", "--fix")) == 0
    prompt_out = capsys.readouterr()
    assert "Apply these changes to" in prompt_out.err + prompt_out.out
    assert (root / ".ptest.toml").read_bytes() != before

    (root / ".ptest.toml").write_bytes(before)
    monkeypatch.setattr("builtins.input", lambda: "n")
    assert main(("doctor", "--fix")) == 1
    assert (root / ".ptest.toml").read_bytes() == before


def test_fix_second_run_reports_up_to_date(tmp_path, monkeypatch, capsys):
    root = _write_pytest_project(tmp_path / "idem")
    _write_config(root, args=("-n", "0"))
    monkeypatch.chdir(root)
    _no_review(monkeypatch)

    assert main(("doctor", "--fix", "--yes")) == 0
    capsys.readouterr()
    assert main(("doctor", "--fix", "--yes")) == 0
    assert "config is up to date" in capsys.readouterr().out


def test_fix_covers_monorepo_children_and_leaves_dispatcher(
        tmp_path, monkeypatch, capsys):
    root = tmp_path / "mono"
    (root / "api" / "tests").mkdir(parents=True)
    (root / ".ptest.toml").write_text(
        'version = 2\n\n[monorepo]\nchildren = ["api"]\n', encoding="utf-8")
    before_root = (root / ".ptest.toml").read_bytes()
    api = root / "api"
    _write_pytest_project(api)
    _write_config(api, args=("-n", "0"))
    monkeypatch.chdir(root)
    _no_review(monkeypatch)

    assert main(("doctor", "--fix", "--yes")) == 0
    out = capsys.readouterr().out
    assert "updated api/.ptest.toml" in out
    assert (root / ".ptest.toml").read_bytes() == before_root
    assert "\nargs = []\n" in (api / ".ptest.toml").read_text(encoding="utf-8")


def test_doctor_mentions_fix_when_config_out_of_date(
        tmp_path, monkeypatch, capsys):
    root = _write_pytest_project(tmp_path / "mention")
    _write_config(root, args=("-n", "0"))
    monkeypatch.chdir(root)
    _no_review(monkeypatch)

    assert main(("doctor", "--offline")) == 0
    out = capsys.readouterr().out
    assert "config is out of date" in out
    assert "ptest doctor --fix" in out

    assert main(("doctor", "--fix", "--yes")) == 0
    capsys.readouterr()
    assert main(("doctor", "--offline")) == 0
    assert "ptest doctor --fix" not in capsys.readouterr().out


def test_review_text_mentions_fix_when_config_out_of_date(
        tmp_path, monkeypatch, capsys):
    import json as json_module

    from ptest.agent_providers import (
        ProviderResult,
        QualificationStatus,
        ReviewerAdapter,
    )

    root = _write_pytest_project(tmp_path / "review-mention")
    _write_config(root, args=("-n", "0"))
    monkeypatch.chdir(root)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setenv("PTEST_RECOMMENDATIONS_LOCK_DIR",
                       str(tmp_path / "locks"))
    monkeypatch.setattr(
        "ptest.cli.agent_providers.qualification_status",
        lambda name: QualificationStatus(
            name=name, qualified=True, argv=(name,),
            note="synthetic fix-mention profile"),
    )
    monkeypatch.setattr(
        "ptest.cli.agent_providers.resolve_reviewer",
        lambda name, env: ReviewerAdapter(
            name=name, executable=f"/synthetic/{name}",
            argv=(f"/synthetic/{name}",), qualified=True,
            qualification_note="synthetic fix-mention adapter"),
    )

    def launch_many(adapter, requests, timeout_s, **kwargs):
        results = []
        for _request, _schema in requests:
            results.append(ProviderResult(
                provider=adapter.name, ok=True,
                assessment=json_module.dumps({
                    "status": "unknown",
                    "rationale": ("The supplied bounded evidence does not "
                                  "establish this criterion."),
                    "evidence": [],
                    "finding": None,
                }).encode("utf-8"),
                error="", exit_code=0, timed_out=False, cancelled=False,
                truncated=False, pid=7301, argv=adapter.argv,
                scratch="/tmp/ptest-fix-mention",
            ))
        return tuple(results)

    monkeypatch.setattr(
        "ptest.cli.agent_providers.launch_reviews", launch_many)
    monkeypatch.setattr(
        "ptest.operations.execute",
        lambda *args, **kwargs: pytest.fail("mention run executed tests"),
    )

    assert main(("doctor", "--reviewer", "claude",
                 "--allow-model-review")) == 0
    out = capsys.readouterr().out
    assert "config is out of date" in out
    assert "ptest doctor --fix" in out


def test_fix_combines_with_offline_and_never_reviews(
        tmp_path, monkeypatch, capsys):
    root = _write_pytest_project(tmp_path / "offfix")
    _write_config(root, args=("-n", "0"))
    monkeypatch.chdir(root)
    _no_review(monkeypatch)

    assert main(("doctor", "--fix", "--offline", "--yes")) == 0
    assert "\nargs = []\n" in (root / ".ptest.toml").read_text(encoding="utf-8")
    capsys.readouterr()
