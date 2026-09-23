"""Init smoke run twins (requirements section E, strict TDD).

After config write and a passing executability check, init offers one
smoke run of a small real test per project through ptest's own scoped
runner, in-process. These tests pin: deterministic candidate choice,
consent matrix, setup-missing skips, config/exit preservation on
failure, sanitized output, and one smoke line per monorepo project.
"""
from __future__ import annotations

import os
import sys

import pytest

from ptest import contracts as C


def _git(root):
    marker = root / ".git"
    marker.mkdir(exist_ok=True)
    (marker / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (marker / "config").write_text(
        "[core]\n\trepositoryformatversion = 0\n", encoding="utf-8")


def _pytest_repo(root, files):
    _git(root)
    tests = root / "tests"
    tests.mkdir(exist_ok=True)
    for name, body in files.items():
        (tests / name).write_text(body, encoding="utf-8")


def _fake_python_on_path(monkeypatch, bindir):
    """Point bare ``python`` at the test interpreter (which owns pytest).

    A wrapper script, not a symlink: invoking the venv interpreter through
    a foreign path would lose its ``pyvenv.cfg`` and the pytest it owns.
    """
    bindir.mkdir(exist_ok=True)
    launcher = bindir / "python"
    launcher.write_text(
        "#!/bin/sh\n" f'exec {sys.executable} "$@"\n', encoding="utf-8")
    launcher.chmod(0o755)
    monkeypatch.setenv(
        "PATH", str(bindir) + os.pathsep + os.environ.get("PATH", ""))


# --- candidate choice (pure, no execution) ---------------------------------


def test_candidate_prefers_smallest_fixture_free_test(tmp_path):
    from ptest import init_smoke

    _pytest_repo(tmp_path, {
        "test_big.py": "VALUE = 12345678901234567890\n\n\ndef test_big():\n    assert True\n",
        "test_small.py": "def test_small():\n    assert True\n",
        "test_db.py": ("import sqlalchemy\n\n\n"
                       "def test_db():\n    assert True\n"),
        "test_net.py": ("import requests\n\n\n"
                        "def test_net():\n    requests.get('http://x')\n"),
    })

    assert init_smoke.choose_candidate(
        tmp_path, C.RunnerKind.PYTEST, ("tests",)) == "tests/test_small.py"


def test_candidate_never_picks_db_or_network_test(tmp_path):
    from ptest import init_smoke

    _pytest_repo(tmp_path, {
        "test_db.py": "import sqlalchemy\n\n\ndef test_db():\n    assert True\n",
        "test_net.py": "import httpx\n\n\ndef test_net():\n    httpx.get('https://x')\n",
    })

    assert init_smoke.choose_candidate(
        tmp_path, C.RunnerKind.PYTEST, ("tests",)) is None


def test_candidate_picks_vitest_shape(tmp_path):
    from ptest import init_smoke

    _git(tmp_path)
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "b.test.ts").write_text("import {db} from './db';\n", encoding="utf-8")
    (tests / "a.test.ts").write_text("export {};\n", encoding="utf-8")

    assert init_smoke.choose_candidate(
        tmp_path, C.RunnerKind.VITEST, ("tests",)) == "tests/a.test.ts"


def _persea_web_shape(root):
    """Persea-web-shaped fixture: Playwright specs in e2e/, unit tests in src/."""
    _git(root)
    (root / "vitest.config.ts").write_text(
        "import { defineConfig, configDefaults } from 'vitest/config';\n"
        "export default defineConfig({\n"
        "  test: {\n"
        "    exclude: [...configDefaults.exclude, 'e2e/**'],\n"
        "  },\n"
        "});\n",
        encoding="utf-8")
    (root / "playwright.config.ts").write_text(
        "import { defineConfig } from '@playwright/test';\n"
        "export default defineConfig({ testDir: './e2e' });\n",
        encoding="utf-8")
    e2e = root / "e2e"
    e2e.mkdir()
    (e2e / "agents.spec.ts").write_text(
        "import { test } from '@playwright/test';\n"
        "test('flow', () => {});\n",
        encoding="utf-8")
    src = root / "src"
    src.mkdir()
    (src / "i18n-defaults.test.ts").write_text(
        "import { it } from 'vitest';\n"
        "it('defaults to Spanish when browser locale starts with es', () => {});\n",
        encoding="utf-8")


def test_candidate_finds_src_test_under_dot_root(tmp_path):
    """Diagnosed persea-web failure: test_roots "." yielded no candidate."""
    from ptest import init_smoke

    _persea_web_shape(tmp_path)

    assert init_smoke.choose_candidate(
        tmp_path, C.RunnerKind.VITEST, (".",)) == "src/i18n-defaults.test.ts"


def test_candidate_skips_playwright_specs_under_dot_root(tmp_path):
    from ptest import init_smoke

    _persea_web_shape(tmp_path)
    src = tmp_path / "src"
    # Smaller than i18n-defaults.test.ts, so it is scanned first: only the
    # Playwright-import check can reject it.
    (src / "a-play.spec.ts").write_text(
        "import{test}from'@playwright/test';test('f',()=>{});\n",
        encoding="utf-8")

    assert init_smoke.choose_candidate(
        tmp_path, C.RunnerKind.VITEST, (".",)) == "src/i18n-defaults.test.ts"


# --- consent matrix (execute stubbed: consent plumbing only) ----------------


def _stub_execute(monkeypatch, calls, result):
    from ptest import operations

    def fake(domain, config, request):
        calls.append((config, request))
        return result

    monkeypatch.setattr(operations, "execute", fake)


def _passed_result(case):
    return case.result(status=C.Status.PASSED, exit_code=0)


def test_non_tty_without_smoke_never_runs(tmp_path, monkeypatch, capsys, case):
    from ptest import cli, operations

    _pytest_repo(tmp_path, {"test_tiny.py": "def test_tiny():\n    assert True\n"})
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(
        operations, "execute",
        lambda *a, **k: pytest.fail("non-interactive init ran smoke"))

    assert cli.main(("init", "--runner", "pytest", "--agents", "none")) == 0
    captured = capsys.readouterr()
    assert "Smoke" not in captured.out


def test_non_tty_smoke_flag_runs_and_reports_passed(
        tmp_path, monkeypatch, capsys, case):
    from ptest import cli

    _pytest_repo(tmp_path, {"test_tiny.py": "def test_tiny():\n    assert True\n"})
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    calls = []
    _stub_execute(monkeypatch, calls, _passed_result(case))
    domain = case.domain()

    assert cli.main((
        "--fixture-domain", str(domain.root),
        "init", "--runner", "pytest", "--agents", "none", "--smoke")) == 0
    assert len(calls) == 1
    assert calls[0][1].mode is C.Mode.SCOPED
    assert tuple(calls[0][1].argv) == ("tests/test_tiny.py",)
    out = capsys.readouterr().out
    assert "Smoke" in out
    assert "passed: ptest tests/test_tiny.py" in out


def test_no_smoke_suppresses_tty_prompt(tmp_path, monkeypatch, capsys):
    from ptest import cli, operations

    _pytest_repo(tmp_path, {"test_tiny.py": "def test_tiny():\n    assert True\n"})
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setattr("builtins.input",
                        lambda: pytest.fail("unexpected smoke prompt"))
    monkeypatch.setattr(
        operations, "execute",
        lambda *a, **k: pytest.fail("no-smoke init ran smoke"))

    assert cli.main((
        "init", "--runner", "pytest", "--agents", "none",
        "--no-doctor", "--no-smoke")) == 0
    assert "Smoke" not in capsys.readouterr().out


def test_dry_run_with_smoke_never_runs(tmp_path, monkeypatch, capsys):
    from ptest import cli, operations

    _pytest_repo(tmp_path, {"test_tiny.py": "def test_tiny():\n    assert True\n"})
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(
        operations, "execute",
        lambda *a, **k: pytest.fail("dry-run init ran smoke"))

    assert cli.main((
        "init", "--runner", "pytest", "--agents", "none",
        "--dry-run", "--smoke")) == 0
    assert "Smoke" not in capsys.readouterr().out


def test_tty_decline_and_eof_never_run(tmp_path, monkeypatch, capsys):
    from ptest import cli, operations

    _pytest_repo(tmp_path, {"test_tiny.py": "def test_tiny():\n    assert True\n"})
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setattr(
        operations, "execute",
        lambda *a, **k: pytest.fail("declined smoke ran"))
    answers = iter(["n", ""])

    def fake_input(*args, **kwargs):
        answer = next(answers)
        if answer == "":
            raise EOFError
        return answer

    monkeypatch.setattr("builtins.input", fake_input)

    assert cli.main((
        "init", "--runner", "pytest", "--agents", "none", "--no-doctor")) == 0
    first = capsys.readouterr()
    assert "Run a quick smoke test to confirm ptest works? [Y/n]" in first.err
    assert "Smoke" not in first.out

    assert cli.main((
        "init", "--runner", "pytest", "--agents", "none", "--no-doctor")) == 0
    second = capsys.readouterr()
    assert "Smoke" not in second.out


def test_tty_consent_names_the_file_and_runs(tmp_path, monkeypatch, capsys, case):
    from ptest import cli

    _pytest_repo(tmp_path, {"test_tiny.py": "def test_tiny():\n    assert True\n"})
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setattr("builtins.input", lambda *a, **k: "")
    calls = []
    _stub_execute(monkeypatch, calls, _passed_result(case))
    domain = case.domain()

    assert cli.main((
        "--fixture-domain", str(domain.root),
        "init", "--runner", "pytest", "--agents", "none", "--no-doctor")) == 0
    assert len(calls) == 1
    err = capsys.readouterr().err
    assert "tests/test_tiny.py" in err
    assert "Run a quick smoke test to confirm ptest works? [Y/n]" in err


def test_smoke_and_no_smoke_conflict(tmp_path, monkeypatch, capsys):
    from ptest import cli

    _git(tmp_path)
    monkeypatch.chdir(tmp_path)

    assert cli.main((
        "init", "--runner", "pytest", "--agents", "none",
        "--smoke", "--no-smoke")) == 2
    assert "smoke" in capsys.readouterr().err.lower()


def test_json_with_smoke_stays_machine_exact_and_never_runs(
        tmp_path, monkeypatch, capsys):
    from ptest import cli, operations

    _pytest_repo(tmp_path, {"test_tiny.py": "def test_tiny():\n    assert True\n"})
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setattr(
        operations, "execute",
        lambda *a, **k: pytest.fail("json init ran smoke"))

    assert cli.main((
        "init", "--runner", "pytest", "--agents", "none",
        "--json", "--smoke")) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    document = C.decode_public_document(captured.out)
    assert set(document.data) == {
        "action", "target", "exists", "warnings", "config"}


# --- setup, failure, sanitation, monorepo -----------------------------------


def test_missing_setup_skips_with_exact_command(
        tmp_path, monkeypatch, capsys, case):
    from ptest import cli, operations

    _git(tmp_path)
    web = tmp_path / "web"
    (web / "tests").mkdir(parents=True)
    (web / "tests" / "a.test.ts").write_text("export {};\n", encoding="utf-8")
    (tmp_path / ".ptest.toml").write_text(
        "version = 2\n\n[monorepo]\nchildren = [\"web\"]\n",
        encoding="utf-8")
    (web / ".ptest.toml").write_text(
        'version = 1\nproject_id = "dddddddddddddddddddddddddddddddd"\n'
        "[runner]\nkind = \"vitest\"\nlauncher = [\"node\"]\nargs = []\n"
        "full_args = []\ntest_roots = [\"tests\"]\nworkers = 1\n"
        'lifecycle = "cooperative-process-group"\n'
        "[setup]\nargv = [\"npm\", \"ci\"]\n"
        'required_paths = ["node_modules"]\n'
        "network = true\nlifecycle_scripts = true\n",
        encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(
        operations, "execute",
        lambda *a, **k: pytest.fail("setup-missing smoke executed"))
    domain = case.domain()

    assert cli.main((
        "--fixture-domain", str(domain.root),
        "init", "--agents", "none", "--smoke")) == 0
    out = capsys.readouterr().out
    assert "Smoke" in out
    assert "skipped" in out
    assert "npm ci" in out


def test_failing_smoke_keeps_config_and_exit_zero(
        tmp_path, monkeypatch, capsys, case):
    from ptest import cli

    _pytest_repo(tmp_path, {
        "test_tiny.py": "def test_tiny():\n    assert False, 'boom'\n"})
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    calls = []
    failed = case.result(
        status=C.Status.FAILED, exit_code=1,
        reasons=(C.Reason(code="prior-failure", message="1 failed"),))
    _stub_execute(monkeypatch, calls, failed)
    domain = case.domain()

    assert cli.main((
        "--fixture-domain", str(domain.root),
        "init", "--runner", "pytest", "--agents", "none", "--smoke")) == 0
    assert len(calls) == 1
    out = capsys.readouterr().out
    assert "failed: ptest tests/test_tiny.py (exit 1)" in out
    assert "prior-failure" in out
    assert (tmp_path / ".ptest.toml").is_file()


def test_hostile_names_are_sanitized_in_smoke_output():
    from ptest import init_smoke

    hostile = "x\x1b[2J\nforged-line\u202e"
    results = (
        init_smoke.SmokeResult(
            project=hostile, status="passed",
            command=f"ptest {hostile}", duration_s=0.4,
            exit_code=0, lines=(), reason=None),
        init_smoke.SmokeResult(
            project=".", status="failed",
            command="ptest tests/test_ok.py", duration_s=1.0,
            exit_code=1, lines=(f"oops {hostile}",), reason=None),
        init_smoke.SmokeResult(
            project="web", status="skipped",
            command="ptest web/tests/a.test.ts", duration_s=None,
            exit_code=None, lines=(), reason=f"setup {hostile}"),
    )

    text = init_smoke.format_smoke(results)

    assert "\x1b" not in text
    assert "\nforged" not in text
    assert "\u202e" not in text
    assert "Smoke" in text


def test_monorepo_reports_one_smoke_line_per_project(
        tmp_path, monkeypatch, capsys, case):
    from ptest import cli

    _git(tmp_path)
    api = tmp_path / "api"
    (api / "tests").mkdir(parents=True)
    (api / "tests" / "test_api.py").write_text(
        "def test_api():\n    assert True\n", encoding="utf-8")
    (api / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\n"
        "addopts = '-n 4 --dist=loadgroup -m \"not slow\"'\n",
        encoding="utf-8")
    (api / "tests" / "conftest.py").write_text(
        "def pytest_sessionfinish(session, exitstatus):\n    return None\n",
        encoding="utf-8")
    (api / ".ptest.toml").write_text(
        'version = 1\nproject_id = "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"\n'
        "[runner]\nkind = \"pytest\"\nlauncher = [\"python\"]\nargs = []\n"
        "full_args = []\ntest_roots = [\"tests\"]\nworkers = 1\n"
        'lifecycle = "cooperative-process-group"\n',
        encoding="utf-8")
    web = tmp_path / "web"
    (web / "tests").mkdir(parents=True)
    (web / "tests" / "a.test.ts").write_text("export {};\n", encoding="utf-8")
    (web / ".ptest.toml").write_text(
        'version = 1\nproject_id = "ffffffffffffffffffffffffffffffff"\n'
        "[runner]\nkind = \"vitest\"\nlauncher = [\"node\"]\nargs = []\n"
        "full_args = []\ntest_roots = [\"tests\"]\nworkers = 1\n"
        'lifecycle = "cooperative-process-group"\n'
        "[setup]\nargv = [\"npm\", \"ci\"]\n"
        'required_paths = ["node_modules"]\n'
        "network = true\nlifecycle_scripts = true\n",
        encoding="utf-8")
    (tmp_path / ".ptest.toml").write_text(
        "version = 2\n\n[monorepo]\nchildren = [\"api\", \"web\"]\n",
        encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    calls = []
    _stub_execute(monkeypatch, calls, _passed_result(case))
    domain = case.domain()

    assert cli.main((
        "--fixture-domain", str(domain.root),
        "init", "--agents", "none", "--smoke")) == 0
    # Nothing is executable here (api xdist, web setup missing): no run.
    assert calls == []
    out = capsys.readouterr().out
    assert "Smoke" in out
    smoke_lines = [line for line in out.splitlines()
                   if line.strip().startswith(("passed:", "failed:", "skipped:"))]
    assert len(smoke_lines) == 2
    assert sum("api" in line for line in smoke_lines) == 1
    assert sum("web" in line for line in smoke_lines) == 1


# --- real execution, in-process through the scoped runner --------------------


def test_real_passing_smoke_end_to_end(tmp_path, monkeypatch, capsys, case):
    from ptest import cli

    domain = case.domain()
    root = domain.root / "smoke-pass"
    root.mkdir()
    _pytest_repo(root, {"test_tiny.py": "def test_tiny():\n    assert True\n"})
    _fake_python_on_path(monkeypatch, root / "bin")
    monkeypatch.chdir(root)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)

    assert cli.main((
        "--fixture-domain", str(domain.root),
        "init", "--runner", "pytest", "--agents", "none", "--smoke")) == 0
    out = capsys.readouterr().out
    assert "passed: ptest tests/test_tiny.py" in out
    assert (root / ".ptest.toml").is_file()


def test_real_failing_smoke_keeps_config_and_exit_zero(
        tmp_path, monkeypatch, capsys, case):
    from ptest import cli

    domain = case.domain()
    root = domain.root / "smoke-fail"
    root.mkdir()
    _pytest_repo(root, {
        "test_tiny.py": "def test_tiny():\n    assert False, 'boom'\n"})
    _fake_python_on_path(monkeypatch, root / "bin")
    monkeypatch.chdir(root)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)

    assert cli.main((
        "--fixture-domain", str(domain.root),
        "init", "--runner", "pytest", "--agents", "none", "--smoke")) == 0
    out = capsys.readouterr().out
    assert "failed: ptest tests/test_tiny.py (exit " in out
    assert (root / ".ptest.toml").is_file()


# --- setup gating (controller decision b): TTY offers setup, else skip -----


def _pytest_toml_with_setup(*, setup_argv, required_paths):
    import json as _json

    return (
        'version = 1\nproject_id = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"\n'
        "[runner]\nkind = \"pytest\"\nlauncher = [\"python\"]\nargs = []\n"
        "full_args = []\ntest_roots = [\"tests\"]\nworkers = 1\n"
        'lifecycle = "cooperative-process-group"\n'
        "[setup]\n"
        f"argv = {_json.dumps(list(setup_argv))}\n"
        f"required_paths = {_json.dumps(list(required_paths))}\n"
        "network = false\nlifecycle_scripts = false\n"
    )


def test_tty_setup_yes_runs_setup_through_ptest_then_passes(
        tmp_path, monkeypatch, capsys, case):
    """Uv-locked pytest with [setup]: TTY yes runs setup, then smoke passes."""
    import sys as _sys

    from ptest import cli

    domain = case.domain()
    root = domain.root / "smoke-setup"
    root.mkdir()
    _pytest_repo(root, {"test_tiny.py": "def test_tiny():\n    assert True\n"})
    (root / "uv.lock").write_text("# fake uv lock\n", encoding="utf-8")
    (root / "setup.py").write_text(
        "from pathlib import Path\nPath('.setup-done').touch()\n",
        encoding="utf-8")
    (root / ".ptest.toml").write_text(
        _pytest_toml_with_setup(
            setup_argv=[_sys.executable, "setup.py"],
            required_paths=[".setup-done"]),
        encoding="utf-8")
    _fake_python_on_path(monkeypatch, root / "bin")
    monkeypatch.chdir(root)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.delenv("CI", raising=False)
    answers = iter(["y", "y"])

    def fake_input(*args, **kwargs):
        try:
            return next(answers)
        except StopIteration:
            return pytest.fail("unexpected third init prompt")

    monkeypatch.setattr("builtins.input", fake_input)

    assert cli.main((
        "--fixture-domain", str(domain.root),
        "init", "--agents", "none", "--no-doctor")) == 0
    out = capsys.readouterr().out
    assert "passed: ptest tests/test_tiny.py" in out
    assert (root / ".setup-done").is_file()


def test_consented_setup_runs_smoke_exactly_once_per_init(
        tmp_path, monkeypatch, capsys, case):
    """Consented setup runs the smoke candidate exactly once per init.

    The candidate appends to a counter file; after "y, y" the counter
    holds one line (setup alone must not execute tests). A second init
    asks no setup question and appends exactly one more line.
    """
    import sys as _sys

    from ptest import cli

    domain = case.domain()
    root = domain.root / "smoke-setup-once"
    root.mkdir()
    _pytest_repo(root, {
        "test_counted.py": (
            "def test_counted():\n"
            "    from pathlib import Path\n"
            "    marker = Path(__file__).resolve().parent.parent / 'counter.txt'\n"
            "    with marker.open('a', encoding='utf-8') as handle:\n"
            "        handle.write('x\\n')\n"),
    })
    (root / "uv.lock").write_text("# fake uv lock\n", encoding="utf-8")
    (root / "setup.py").write_text(
        "from pathlib import Path\nPath('.setup-done').touch()\n",
        encoding="utf-8")
    (root / ".ptest.toml").write_text(
        _pytest_toml_with_setup(
            setup_argv=[_sys.executable, "setup.py"],
            required_paths=[".setup-done"]),
        encoding="utf-8")
    _fake_python_on_path(monkeypatch, root / "bin")
    monkeypatch.chdir(root)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.delenv("CI", raising=False)

    def _answers(words):
        owned = iter(words)

        def fake_input(*args, **kwargs):
            try:
                return next(owned)
            except StopIteration:
                return pytest.fail("unexpected extra init prompt")

        return fake_input

    monkeypatch.setattr("builtins.input", _answers(["y", "y"]))
    assert cli.main((
        "--fixture-domain", str(domain.root),
        "init", "--agents", "none", "--no-doctor")) == 0
    assert capsys.readouterr().out.count("passed: ptest tests/test_counted.py") == 1
    assert (root / "counter.txt").read_text(encoding="utf-8") == "x\n"

    monkeypatch.setattr("builtins.input", _answers(["y"]))
    assert cli.main((
        "--fixture-domain", str(domain.root),
        "init", "--agents", "none", "--no-doctor")) == 0
    captured = capsys.readouterr()
    assert "run it?" not in captured.err
    assert captured.out.count("passed: ptest tests/test_counted.py") == 1
    assert (root / "counter.txt").read_text(encoding="utf-8") == "x\nx\n"


def _vitest_toml_with_setup(*, launcher=("node",)):
    import json as _json

    return (
        'version = 1\nproject_id = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"\n'
        "[runner]\nkind = \"vitest\"\n"
        f"launcher = {_json.dumps(list(launcher))}\nargs = []\n"
        "full_args = []\ntest_roots = [\"tests\"]\nworkers = 1\n"
        'lifecycle = "cooperative-process-group"\n'
        "[setup]\nargv = [\"npm\", \"ci\"]\n"
        'required_paths = ["node_modules"]\n'
        "network = true\nlifecycle_scripts = true\n"
    )


def _npm_locked_vitest_repo(root):
    _git(root)
    tests = root / "tests"
    tests.mkdir(exist_ok=True)
    (tests / "a.test.ts").write_text(
        "import { test, expect } from 'vitest';\n"
        "test('a', () => { expect(1).toBe(1); });\n",
        encoding="utf-8")
    (root / "package-lock.json").write_text(
        '{"name": "web", "lockfileVersion": 3}\n', encoding="utf-8")
    (root / ".ptest.toml").write_text(
        _vitest_toml_with_setup(), encoding="utf-8")


def test_tty_setup_no_skips_without_running(
        tmp_path, monkeypatch, capsys, case):
    """Npm-locked vitest: TTY smoke-yes plus setup-no skips, runs nothing."""
    from ptest import cli, operations

    _npm_locked_vitest_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.delenv("CI", raising=False)
    answers = iter(["y", "n"])

    def fake_input(*args, **kwargs):
        try:
            return next(answers)
        except StopIteration:
            return pytest.fail("unexpected third init prompt")

    monkeypatch.setattr("builtins.input", fake_input)
    monkeypatch.setattr(
        operations, "execute",
        lambda *a, **k: pytest.fail("declined setup executed"))
    domain = case.domain()

    assert cli.main((
        "--fixture-domain", str(domain.root),
        "init", "--agents", "none", "--no-doctor")) == 0
    out = capsys.readouterr().out
    assert "Smoke" in out
    assert "skipped" in out
    assert "setup baseline not recorded" in out
    assert "runs npm ci first" in out


def test_tty_setup_ctrl_c_skips_without_running(
        tmp_path, monkeypatch, capsys, case):
    """Ctrl-C at the setup prompt is a decline: skip, exit 0, no run."""
    from ptest import cli, operations

    _npm_locked_vitest_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.delenv("CI", raising=False)
    answers = iter(["y", KeyboardInterrupt()])

    def fake_input(*args, **kwargs):
        try:
            answer = next(answers)
        except StopIteration:
            return pytest.fail("unexpected third init prompt")
        if isinstance(answer, BaseException):
            raise answer
        return answer

    monkeypatch.setattr("builtins.input", fake_input)
    monkeypatch.setattr(
        operations, "execute",
        lambda *a, **k: pytest.fail("ctrl-c setup executed"))
    domain = case.domain()

    assert cli.main((
        "--fixture-domain", str(domain.root),
        "init", "--agents", "none", "--no-doctor")) == 0
    out = capsys.readouterr().out
    assert "skipped" in out
    assert "setup baseline not recorded" in out


def test_non_tty_setup_missing_skips_with_working_advice(
        tmp_path, monkeypatch, capsys, case):
    """Non-TTY --smoke never installs: skip with the working advice."""
    from ptest import cli, operations

    _npm_locked_vitest_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(
        "builtins.input",
        lambda *a, **k: pytest.fail("non-TTY init prompted"))
    monkeypatch.setattr(
        operations, "execute",
        lambda *a, **k: pytest.fail("non-TTY smoke executed with setup due"))
    domain = case.domain()

    assert cli.main((
        "--fixture-domain", str(domain.root),
        "init", "--agents", "none", "--smoke")) == 0
    out = capsys.readouterr().out
    assert "Smoke" in out
    assert "skipped" in out
    assert ("setup baseline not recorded; run: ptest tests/a.test.ts "
            "(runs npm ci first)") in out


def test_smoke_run_request_always_uses_no_setup_and_short_queue(
        tmp_path, monkeypatch, capsys, case):
    """Every smoke execution opts out of setup with a short queue bound."""
    from ptest import cli, init_smoke

    _pytest_repo(tmp_path, {"test_tiny.py": "def test_tiny():\n    assert True\n"})
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    calls = []
    _stub_execute(monkeypatch, calls, _passed_result(case))
    domain = case.domain()

    assert cli.main((
        "--fixture-domain", str(domain.root),
        "init", "--runner", "pytest", "--agents", "none", "--smoke")) == 0
    assert len(calls) == 1
    request = calls[0][1]
    assert request.no_setup is True
    assert request.queue_timeout_s == init_smoke.SMOKE_QUEUE_TIMEOUT_S
    assert init_smoke.SMOKE_QUEUE_TIMEOUT_S < 1800


def test_executability_and_smoke_lines_agree_on_setup_project(
        tmp_path, monkeypatch, capsys, case):
    """Paths present but no fingerprint: caveat verdict plus smoke skip."""
    import sys as _sys

    from ptest import cli, config as config_api
    from ptest import executability as exec_check

    _pytest_repo(tmp_path, {"test_tiny.py": "def test_tiny():\n    assert True\n"})
    (tmp_path / "uv.lock").write_text("# fake uv lock\n", encoding="utf-8")
    (tmp_path / ".setup-done").write_text("done\n", encoding="utf-8")
    _git(tmp_path)
    (tmp_path / ".ptest.toml").write_text(
        _pytest_toml_with_setup(
            setup_argv=[_sys.executable, "setup.py"],
            required_paths=[".setup-done"]),
        encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(
        "builtins.input",
        lambda *a, **k: pytest.fail("non-TTY init prompted"))
    from ptest import operations
    monkeypatch.setattr(
        operations, "execute",
        lambda *a, **k: pytest.fail("non-TTY smoke executed with setup due"))
    domain = case.domain()

    resolution = config_api.resolve_config(tmp_path)
    item = exec_check.check_config(resolution.config, project=".")
    assert item.status == exec_check.STATUS_CAVEAT
    assert any("setup" in caveat for caveat in item.caveats)

    assert cli.main((
        "--fixture-domain", str(domain.root),
        "init", "--agents", "none", "--smoke")) == 0
    out = capsys.readouterr().out
    assert "ready with caveats" in out
    assert "skipped" in out
    assert "setup baseline not recorded" in out


def test_failing_smoke_points_at_runner_output_above(
        tmp_path, monkeypatch, capsys, case):
    """A real failing smoke says where the detail is, in banner order."""
    from ptest import cli

    domain = case.domain()
    root = domain.root / "smoke-fail-detail"
    root.mkdir()
    _pytest_repo(root, {
        "test_tiny.py": "def test_tiny():\n    assert False, 'boom'\n"})
    _fake_python_on_path(monkeypatch, root / "bin")
    monkeypatch.chdir(root)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)

    assert cli.main((
        "--fixture-domain", str(domain.root),
        "init", "--runner", "pytest", "--agents", "none", "--smoke")) == 0
    out = capsys.readouterr().out
    assert "failed: ptest tests/test_tiny.py (exit " in out
    assert "see runner output above" in out
    assert "no detail" not in out
    assert out.index("ptest initialized") < out.index("Smoke: running")
    assert out.index("Smoke: running") < out.index("failed:")


def test_ask_init_smoke_sanitizes_hostile_names(monkeypatch, capsys):
    """The consent prompt neutralizes ESC bytes and forged newlines."""
    from ptest import cli, init_smoke

    hostile_project = "we\x1b[2Jb\nforged-project"
    hostile_candidate = "tests/\nforged-test.py"
    plans = (init_smoke.SmokePlan(
        project=hostile_project, config=None,
        candidate=hostile_candidate, skip_reason=None),)
    monkeypatch.setattr("builtins.input", lambda *a, **k: "y")

    assert cli._ask_init_smoke(plans) is True
    err = capsys.readouterr().err
    assert "\x1b" not in err
    assert "\nforged" not in err
    assert "Run a quick smoke test to confirm ptest works? [Y/n]" in err


def test_ctrl_c_at_smoke_prompt_declines_without_traceback(
        tmp_path, monkeypatch, capsys):
    """Ctrl-C at the smoke prompt declines: exit 0, config kept, no Smoke."""
    from ptest import cli, operations

    _pytest_repo(tmp_path, {"test_tiny.py": "def test_tiny():\n    assert True\n"})
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.delenv("CI", raising=False)

    def fake_input(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", fake_input)
    monkeypatch.setattr(
        operations, "execute",
        lambda *a, **k: pytest.fail("ctrl-c smoke executed"))

    assert cli.main((
        "init", "--runner", "pytest", "--agents", "none",
        "--no-doctor")) == 0
    captured = capsys.readouterr()
    assert "Smoke" not in captured.out
    assert "Traceback" not in captured.err
    assert (tmp_path / ".ptest.toml").is_file()


def test_short_queue_timeout_means_skip(tmp_path, monkeypatch, capsys, case):
    """A queue timeout during smoke is a skip, and the queue bound is short."""
    from ptest import cli, init_smoke, operations

    _pytest_repo(tmp_path, {"test_tiny.py": "def test_tiny():\n    assert True\n"})
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    calls = []

    def fake_execute(domain, config, request):
        calls.append(request)
        raise C.Problem(code="queue-timeout",
                        message="admission queue timeout", phase="queue")

    monkeypatch.setattr(operations, "execute", fake_execute)
    domain = case.domain()

    assert cli.main((
        "--fixture-domain", str(domain.root),
        "init", "--runner", "pytest", "--agents", "none", "--smoke")) == 0
    out = capsys.readouterr().out
    assert "skipped" in out
    assert "admission queue timeout" in out
    assert len(calls) == 1
    assert calls[0].queue_timeout_s == init_smoke.SMOKE_QUEUE_TIMEOUT_S


def test_monorepo_child_executes_via_preflight_children(
        tmp_path, monkeypatch, capsys, case):
    """Monorepo smoke resolves the child through preflight, then executes."""
    from ptest import cli, config as config_api
    from ptest import monorepo as monorepo_api

    _git(tmp_path)
    api = tmp_path / "api"
    (api / "tests").mkdir(parents=True)
    (api / "tests" / "test_api.py").write_text(
        "def test_api():\n    assert True\n", encoding="utf-8")
    (api / ".ptest.toml").write_text(
        'version = 1\nproject_id = "cccccccccccccccccccccccccccccccc"\n'
        "[runner]\nkind = \"pytest\"\nlauncher = [\"python\"]\nargs = []\n"
        "full_args = []\ntest_roots = [\"tests\"]\nworkers = 1\n"
        'lifecycle = "cooperative-process-group"\n',
        encoding="utf-8")
    (tmp_path / ".ptest.toml").write_text(
        "version = 2\n\n[monorepo]\nchildren = [\"api\"]\n",
        encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    preflight_calls = []
    real_preflight = monorepo_api.preflight_children

    def spy_preflight(root_dir, manifest):
        preflight_calls.append((str(root_dir), tuple(manifest.children)))
        return real_preflight(root_dir, manifest)

    monkeypatch.setattr(monorepo_api, "preflight_children", spy_preflight)
    calls = []
    _stub_execute(monkeypatch, calls, _passed_result(case))
    domain = case.domain()
    resolution = config_api.resolve_config(tmp_path)
    assert tuple(resolution.monorepo.children) == ("api",)

    assert cli.main((
        "--fixture-domain", str(domain.root),
        "init", "--agents", "none", "--smoke")) == 0
    assert preflight_calls == [(str(tmp_path), ("api",))]
    assert len(calls) == 1
    out = capsys.readouterr().out
    assert "passed: ptest api/tests/test_api.py" in out


def test_monorepo_preflight_problem_skips_every_project(
        tmp_path, monkeypatch, capsys, case):
    """A preflight Problem skips all children; nothing executes."""
    from ptest import cli, operations
    from ptest import monorepo as monorepo_api

    _git(tmp_path)
    api = tmp_path / "api"
    (api / "tests").mkdir(parents=True)
    (api / "tests" / "test_api.py").write_text(
        "def test_api():\n    assert True\n", encoding="utf-8")
    (api / ".ptest.toml").write_text(
        'version = 1\nproject_id = "dddddddddddddddddddddddddddddddd"\n'
        "[runner]\nkind = \"pytest\"\nlauncher = [\"python\"]\nargs = []\n"
        "full_args = []\ntest_roots = [\"tests\"]\nworkers = 1\n"
        'lifecycle = "cooperative-process-group"\n',
        encoding="utf-8")
    (tmp_path / ".ptest.toml").write_text(
        "version = 2\n\n[monorepo]\nchildren = [\"api\"]\n",
        encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)

    def fake_preflight(root_dir, manifest):
        raise C.Problem(code="unsafe-path",
                        message="synthetic preflight refusal",
                        phase="monorepo")

    monkeypatch.setattr(monorepo_api, "preflight_children", fake_preflight)
    monkeypatch.setattr(
        operations, "execute",
        lambda *a, **k: pytest.fail("preflight-refused smoke executed"))
    domain = case.domain()

    assert cli.main((
        "--fixture-domain", str(domain.root),
        "init", "--agents", "none", "--smoke")) == 0
    out = capsys.readouterr().out
    assert "Smoke" in out
    assert "skipped" in out
    assert "synthetic preflight refusal" in out


def test_candidate_scan_uses_shared_walker_and_bounded_reader(
        tmp_path, monkeypatch):
    """Candidate choice walks with executability and reads bounded files."""
    from ptest import executability as exec_check
    from ptest import files as files_module
    from ptest import init_smoke

    _pytest_repo(tmp_path, {"test_a.py": "def test_a():\n    assert True\n"})
    walker_calls = []
    real_iter = exec_check.iter_files

    def spy_iter(root, start, depth, budget):
        walker_calls.append((str(start), depth))
        yield from real_iter(root, start, depth, budget)

    reader_calls = []
    real_read = files_module.read_regular

    def spy_read(root, relative, limit):
        reader_calls.append(relative)
        return real_read(root, relative, limit)

    monkeypatch.setattr(exec_check, "iter_files", spy_iter)
    monkeypatch.setattr(files_module, "read_regular", spy_read)

    assert init_smoke.choose_candidate(
        tmp_path, C.RunnerKind.PYTEST, ("tests",)) == "tests/test_a.py"
    assert walker_calls
    assert any(call.endswith("test_a.py") for call in reader_calls)
