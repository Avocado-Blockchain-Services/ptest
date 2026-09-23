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

    hostile = "x\x1b[2J\ny\u202e"
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
