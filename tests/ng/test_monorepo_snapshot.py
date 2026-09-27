"""Twins: monorepo child source snapshot from a REAL git monorepo.

Fixture: root manifest plus api and web children (both pytest; the task
allows a second pytest instead of a vitest child, and only a runnable
child can record the baselines this file pins).

Section: source.snapshot supports a checkout that is a subdirectory of its
Git root (``Git root does not match checkout`` must never fire for a child).
Rules pinned here:

- child scope: the child directory, paths child-relative, repo HEAD identity;
- root inputs: files named by the child's ``full_triggers`` plus the root
  ``.ptest.toml`` manifest, reported as ``../`` aliases (never colliding
  with child-relative paths);
- sibling dirt (dirty or committed) never enters the child's digest, changes,
  or ``clean`` flag;
- anything that cannot be classified stays ``unknown-input`` (fail closed).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from support import git, init_git_repo


@pytest.fixture(autouse=True)
def _clear_native_pytest_environment(monkeypatch):
    """Keep committed-Git child runs independent of an outer pytest run."""
    for name in (
        "PYTEST_ADDOPTS", "PYTHONDONTWRITEBYTECODE", "PTEST_EXECUTION",
        "PTEST_RUN_ID", "PTEST_GRANT_NONCE",
        "PTEST_PYTEST_REPORT_PATH", "PTEST_PYTEST_ATTEMPT",
        "PTEST_PYTEST_EXECUTION", "PTEST_PYTEST_CHECKOUT_ROOT",
        "PTEST_PYTEST_CONFIG_PATH",
    ):
        monkeypatch.delenv(name, raising=False)


def _launcher():
    """Frozen pytest-cov tuple: the only launcher the advanced gate admits."""
    supplied = os.environ.get("PTEST_TEST_PYTHON_9_1_1_COV")
    candidates = [supplied] if supplied else [sys.executable]
    for candidate in candidates:
        try:
            checked = subprocess.run(
                [candidate, "-c", (
                    "import pytest, pytest_cov, coverage; "
                    "print(pytest.__version__, pytest_cov.__version__, "
                    "coverage.__version__)"
                )], capture_output=True, text=True, timeout=10, check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
        if (checked.returncode == 0
                and checked.stdout.strip() == "9.1.1 7.1.0 7.15.0"):
            return candidate
    pytest.skip("unqualified: frozen pytest-cov fixture unavailable")


def _child_toml(project_id: str, launcher: str, module: str, test: str) -> str:
    return (
        "version = 1\nproject_id = \"" + project_id + "\"\n[runner]\n"
        "kind = \"pytest\"\nlauncher = " + json.dumps([launcher]) + "\n"
        "args = [\"-s\", \"-p\", \"no:xdist\", \"--cov=" + module + "\", "
        "\"--cov-report=term\"]\nfull_args = []\n"
        "test_roots = [\"tests\"]\nworkers = 1\n"
        "lifecycle = \"cooperative-process-group\"\n"
        "[selection]\nenabled = true\nclosed_inputs = true\n"
        "input_roots = [\"" + module + ".py\"]\n"
        "non_input_outputs = [\".coverage\", \"ptest-result-child-full.json\", "
        "\"ptest-result-child-changed.json\"]\n"
        "groups = [{ name = \"suite\", sources = [\"" + module + ".py\", \"tests\"], "
        "tests = [\"tests/" + test + "\"] }]\n"
    )


def _api_toml(launcher: str) -> str:
    return _child_toml("0" * 32, launcher, "api_module", "test_api.py")


def _web_toml(launcher: str) -> str:
    return _child_toml("1" * 32, launcher, "web_module", "test_web.py")


def _monorepo(case, domain, *, api_toml: str | None = None,
              extra_root_files: dict | None = None) -> Path:
    """REAL git monorepo: root manifest plus api and web (both pytest)."""
    launcher = _launcher()
    root = domain.root / "mono"
    root.mkdir(parents=True, exist_ok=True)
    (root / ".ptest.toml").write_text(
        "version = 2\n[monorepo]\nchildren = [\"api\", \"web\"]\n",
        encoding="utf-8")
    api = root / "api"
    (api / "tests").mkdir(parents=True)
    (api / ".ptest.toml").write_text(
        api_toml if api_toml is not None else _api_toml(launcher),
        encoding="utf-8")
    (api / "tests" / "test_api.py").write_text(
        "import api_module\n\n\ndef test_api():\n    assert api_module.VALUE == 7\n",
        encoding="utf-8")
    (api / "tests" / "__init__.py").write_text("", encoding="utf-8")
    (api / "api_module.py").write_text("VALUE = 7\n", encoding="utf-8")
    web = root / "web"
    (web / "tests").mkdir(parents=True)
    (web / ".ptest.toml").write_text(_web_toml(launcher), encoding="utf-8")
    (web / "tests" / "test_web.py").write_text(
        "import web_module\n\n\ndef test_web():\n    assert web_module.VALUE == 3\n",
        encoding="utf-8")
    (web / "tests" / "__init__.py").write_text("", encoding="utf-8")
    (web / "web_module.py").write_text("VALUE = 3\n", encoding="utf-8")
    (root / ".gitignore").write_text(
        "__pycache__/\n.pytest_cache/\n.coverage\n"
        "ptest-result-child-full.json\nptest-result-child-changed.json\n",
        encoding="utf-8")
    for relative, content in (extra_root_files or {}).items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    init_git_repo(root)
    assert (root / ".git").is_dir()
    return root


def _children(root: Path):
    from ptest import monorepo as monorepo_api

    manifest = monorepo_api.parse_monorepo_manifest(
        (root / ".ptest.toml").read_bytes(), root / ".ptest.toml")
    targets = monorepo_api.preflight_children(root, manifest)
    return {child.declaration: child for child in targets}


def _snapshot(domain, config, **kwargs):
    """Unit boundary: emulate independently verified T11 native identity."""
    from ptest.source import snapshot as actual_snapshot

    if kwargs.get("pytest_full_outputs"):
        kwargs.pop("runtime_identity", None)
    else:
        kwargs.setdefault("runtime_identity", "a" * 64)
    return actual_snapshot(domain, config, None, None, **kwargs)


def _api_snapshot(case, domain, root: Path, **kwargs):
    from ptest.source import ensure_fingerprint_key

    ensure_fingerprint_key(domain)
    return _snapshot(domain, _children(root)["api"].config, **kwargs)


def test_child_snapshot_has_digest_below_git_root(case):
    domain = case.domain()
    root = _monorepo(case, domain)

    result = _api_snapshot(case, domain, root)

    assert result.digest is not None, result.limitations
    assert result.head == git(root, "rev-parse", "HEAD")
    paths = {item.path for item in result.files}
    assert "tests/test_api.py" in paths
    assert "api_module.py" in paths
    assert ".ptest.toml" in paths
    # The root manifest is always a child input, under a ../ alias that
    # can never collide with a child-relative path.
    assert "../.ptest.toml" in paths
    assert not any(path.startswith(("api/", "web/", "/")) for path in paths)
    assert all(path == "../.ptest.toml" or ".." not in path.split("/")
               for path in paths)


def test_child_digest_ignores_sibling_but_not_own_changes(case):
    domain = case.domain()
    root = _monorepo(case, domain)
    before = _api_snapshot(case, domain, root).digest

    (root / "web" / "tests" / "test_web.py").write_text(
        "import web_module\n\n\ndef test_web():\n    assert web_module.VALUE == 3\n# sibling\n",
        encoding="utf-8")
    assert _api_snapshot(case, domain, root).digest == before

    (root / "api" / "tests" / "test_api.py").write_text(
        "def test_api():\n    assert True\n# own\n", encoding="utf-8")
    assert _api_snapshot(case, domain, root).digest != before


def test_child_clean_ignores_sibling_dirt(case):
    """A dirty sibling never makes the child's tree dirty (pinned rule)."""
    domain = case.domain()
    root = _monorepo(case, domain)

    (root / "web" / "tests" / "test_web.py").write_text("dirty\n", encoding="utf-8")
    (root / "web" / "new_untracked.py").write_text("new\n", encoding="utf-8")
    result = _api_snapshot(case, domain, root)

    assert result.digest is not None
    assert result.clean is True
    assert result.changes == ()
    assert all("web" not in (change.old or "") and "web" not in (change.new or "")
               for change in result.changes)

    (root / "api" / "tests" / "test_api.py").write_text("dirty\n", encoding="utf-8")
    own = _api_snapshot(case, domain, root)
    assert own.clean is False
    assert any(change.new == "tests/test_api.py" for change in own.changes)


def test_child_full_records_baseline_then_changed_selects(case):
    """Bare `ptest --full` passes and records baselines; `--changed` selects."""
    from ptest import history as history_api
    from ptest import operations

    domain = case.domain()
    root = _monorepo(case, domain)

    completed = case.invoke(domain, root,
                              "--result-json", "ptest-result-child-full.json",
                              "--full", timeout=180)
    assert completed.code == 0, completed.stderr.decode(errors="replace")

    checkout = operations._checkout(_children(root)["api"].config)
    baseline = history_api.read_history(domain, checkout).baseline
    assert baseline is not None
    assert baseline.head == git(root, "rev-parse", "HEAD")

    web_baseline = history_api.read_history(
        domain, operations._checkout(_children(root)["web"].config)).baseline
    assert web_baseline is not None

    # The skip path writes no export: request none here.
    again = case.invoke(domain, root, "--full", timeout=120)
    assert again.code == 0, again.stderr.decode(errors="replace")
    assert b"already verified" in again.stderr

    # On the default branch only uncommitted work counts as changed, so
    # commit the api touch on a feature branch to exercise per-child lines.
    git(root, "checkout", "-b", "api-change")
    (root / "api" / "tests" / "test_api.py").write_text(
        "import api_module\n\n\ndef test_api():\n"
        "    assert api_module.VALUE == 7\n# committed change\n",
        encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-m", "touch api")
    changed = case.invoke(domain, root,
                          "--result-json", "ptest-result-child-changed.json",
                          "--changed", timeout=180)
    assert changed.code == 0, changed.stderr.decode(errors="replace")
    err = changed.stderr.decode(errors="replace")
    assert "ptest: web \u00b7 no changes" in err
    assert "ptest: api \u00b7 no changes" not in err


def test_child_declared_root_trigger_enters_digest(case):
    """Root files the child declares via full_triggers are child inputs."""
    from ptest.source import ensure_fingerprint_key

    domain = case.domain()
    launcher = _launcher()
    api_toml = _api_toml(launcher).replace(
        "input_roots = [\"api_module.py\"]\n",
        "input_roots = [\"api_module.py\"]\nfull_triggers = [\"uv.lock\"]\n")
    assert "full_triggers = [\"uv.lock\"]" in api_toml
    root = _monorepo(case, domain, api_toml=api_toml,
                     extra_root_files={"uv.lock": "version = 1\n"})
    ensure_fingerprint_key(domain)
    assert _children(root)["api"].config.selection.full_triggers == ("uv.lock",)
    before = _snapshot(domain, _children(root)["api"].config)

    (root / "web" / "tests" / "test_web.py").write_text("sibling\n", encoding="utf-8")
    assert _snapshot(domain, _children(root)["api"].config).digest == before.digest

    (root / "uv.lock").write_text("version = 2\n", encoding="utf-8")
    result = _snapshot(domain, _children(root)["api"].config)
    assert result.digest != before.digest
    assert any(change.new == "../uv.lock" for change in result.changes)
    assert "../uv.lock" in {item.path for item in result.files}


def test_child_root_manifest_change_enters_digest(case):
    """The root manifest is always a child input (it declares membership)."""
    domain = case.domain()
    root = _monorepo(case, domain)
    before = _api_snapshot(case, domain, root).digest

    with (root / ".ptest.toml").open("a", encoding="utf-8") as handle:
        handle.write("# membership comment\n")
    result = _api_snapshot(case, domain, root)

    assert result.digest != before
    assert any(change.new == "../.ptest.toml" for change in result.changes)


def test_child_tracked_symlink_has_stable_digest(case):
    """Twin of the real-world blocker: a tracked symlink inside a child
    fingerprints by its target text and stays stable; retargeting it
    changes the digest. The link is never followed."""
    domain = case.domain()
    root = _monorepo(case, domain)
    link = root / "api" / "AGENTS.md"
    link.symlink_to("api_module.py")
    git(root, "add", "api/AGENTS.md")
    git(root, "commit", "-m", "tracked child link")
    first = _api_snapshot(case, domain, root)
    assert first.digest is not None, first.limitations
    assert first.clean
    entry = [item for item in first.files if item.path == "AGENTS.md"]
    assert len(entry) == 1 and entry[0].mode == 0o120000
    assert _api_snapshot(case, domain, root).digest == first.digest
    link.unlink()
    link.symlink_to("tests/test_api.py")
    second = _api_snapshot(case, domain, root)
    assert second.digest is not None and second.digest != first.digest
    assert any(change.new == "AGENTS.md" for change in second.changes)


def test_child_undeclared_root_file_stays_out_and_trigger_swap_is_a_change(case):
    """Undeclared root files stay out; a declared trigger swapped for a
    symlink is a content change, not a failure (links are fingerprinted,
    never followed)."""
    from ptest.source import ensure_fingerprint_key

    domain = case.domain()
    launcher = _launcher()
    api_toml = _api_toml(launcher).replace(
        "input_roots = [\"api_module.py\"]\n",
        "input_roots = [\"api_module.py\"]\nfull_triggers = [\"uv.lock\"]\n")
    assert "full_triggers = [\"uv.lock\"]" in api_toml
    root = _monorepo(case, domain, api_toml=api_toml,
                     extra_root_files={"uv.lock": "version = 1\n",
                                       "notes.txt": "root notes\n"})
    ensure_fingerprint_key(domain)
    assert _children(root)["api"].config.selection.full_triggers == ("uv.lock",)
    before = _snapshot(domain, _children(root)["api"].config)

    (root / "notes.txt").write_text("undeclared root change\n", encoding="utf-8")
    assert _snapshot(domain, _children(root)["api"].config).digest == before.digest

    (root / "uv.lock").unlink()
    (root / "uv.lock").symlink_to("notes.txt")
    result = _snapshot(domain, _children(root)["api"].config)
    assert result.digest is not None
    assert result.digest != before.digest
    assert any(change.new == "../uv.lock" for change in result.changes)


def test_child_glob_shaped_sibling_stays_outside_scope(case):
    """A sibling a glob pathspec would catch never enters the child digest."""
    from ptest.source import ensure_fingerprint_key

    domain = case.domain()
    root = domain.root / "mono"
    root.mkdir(parents=True, exist_ok=True)
    (root / ".ptest.toml").write_text(
        "version = 2\n[monorepo]\nchildren = [\"api*\"]\n",
        encoding="utf-8")
    child = root / "api*"
    (child / "tests").mkdir(parents=True)
    (child / ".ptest.toml").write_text(_api_toml(_launcher()), encoding="utf-8")
    (child / "tests" / "test_api.py").write_text(
        "def test_api():\n    assert True\n", encoding="utf-8")
    (root / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n",
                                     encoding="utf-8")
    sibling = root / "apix"
    (sibling).mkdir(parents=True)
    (sibling / "other.py").write_text("one\n", encoding="utf-8")
    init_git_repo(root)
    ensure_fingerprint_key(domain)
    config = _children(root)["api*"].config
    before = _snapshot(domain, config)

    (sibling / "other.py").write_text("two\n", encoding="utf-8")
    after = _snapshot(domain, config)

    assert before.digest is not None
    assert after.digest == before.digest
    assert after.clean is True
