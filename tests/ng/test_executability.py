"""Deterministic executability checks per project (no model, no subprocess)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ptest import contracts as C
from ptest import executability as E


def _config(tmp_path, *, kind=C.RunnerKind.PYTEST, launcher=("python",),
            args=(), full_args=(), test_roots=("tests",), setup=None,
            name=".ptest.toml"):
    return C.Config(
        runner=C.RunnerConfig(
            kind=kind, launcher=launcher, args=args, full_args=full_args,
            test_roots=test_roots, workers=1,
        ),
        setup=setup,
        resources=C.ResourceConfig(),
        selection=C.SelectionPolicy(enabled=False, closed_inputs=False),
        project_id="ab" * 16,
        config_path=tmp_path / name,
    )


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_verdict_and_public_shapes():
    ready = E.Executability(
        project=".", runner="pytest", status=E.STATUS_EXECUTABLE,
        caveats=(), reason=None, fix=None, full=True, example="tests/test_a.py")
    assert ready.verdict() == "ready"
    assert ready.to_public() == {"status": "executable", "detail": "ready", "fix": None}

    caveat = E.Executability(
        project=".", runner="pytest", status=E.STATUS_CAVEAT,
        caveats=("serial: xdist disabled under ptest (-n 0)", "second caveat"),
        reason=None, fix=None, full=True, example=None)
    assert caveat.verdict() == (
        "ready with caveats: serial: xdist disabled under ptest (-n 0); second caveat")
    assert caveat.to_public() == {
        "status": "caveat",
        "detail": "serial: xdist disabled under ptest (-n 0); second caveat",
        "fix": None}

    blocked = E.Executability(
        project="api", runner="pytest", status=E.STATUS_NOT_EXECUTABLE,
        caveats=(), reason="pytest addopts enable xdist, which ptest runs serially",
        fix='add "-n", "0" to [runner] args in api/.ptest.toml',
        full=True, example=None)
    assert blocked.verdict() == (
        "not runnable: pytest addopts enable xdist, which ptest runs serially"
        ' — fix: add "-n", "0" to [runner] args in api/.ptest.toml')
    assert blocked.to_public() == {
        "status": "not-executable",
        "detail": "pytest addopts enable xdist, which ptest runs serially",
        "fix": 'add "-n", "0" to [runner] args in api/.ptest.toml'}


def test_pytest_launcher_must_be_supported(tmp_path):
    result = E.check_config(
        _config(tmp_path, launcher=("pytest",)), project=".")

    assert result.status == E.STATUS_NOT_EXECUTABLE
    assert result.reason == "pytest launcher is not a supported Python interpreter launcher"
    assert result.fix == (
        'set [runner] launcher = ["uv", "run", "--locked", "--no-sync", "python"]'
        ' or ["python"] in .ptest.toml')
    assert result.runner == "pytest"


def test_runner_parallel_control_is_not_executable(tmp_path):
    result = E.check_config(_config(tmp_path, args=("-n", "2")), project=".")

    assert result.status == E.STATUS_NOT_EXECUTABLE
    assert result.reason == "runner args contain a parallel, remote or argfile control (-n)"
    assert result.fix == "remove -n from [runner] args in .ptest.toml"


def test_xdist_addopts_without_serial_is_not_executable(tmp_path):
    _write(tmp_path / "pyproject.toml",
           '[tool.pytest.ini_options]\naddopts = "-n 4 --dist=loadgroup"\n')

    result = E.check_config(_config(tmp_path), project=".")

    assert result.status == E.STATUS_NOT_EXECUTABLE
    assert result.runner == "pytest"
    assert result.reason == "pytest addopts enable xdist, which ptest runs serially"
    assert result.fix == 'add "-n", "0" to [runner] args in .ptest.toml'


def test_xdist_addopts_with_serial_is_caveat(tmp_path):
    _write(tmp_path / "pyproject.toml",
           '[tool.pytest.ini_options]\naddopts = "-n 4 --dist=loadgroup"\n')

    result = E.check_config(_config(tmp_path, args=("-n", "0")), project=".")

    assert result.status == E.STATUS_CAVEAT
    assert result.caveats == ("serial: xdist disabled under ptest (-n 0)",)
    assert result.full is True


def test_no_xdist_suppresses_activation(tmp_path):
    _write(tmp_path / "pyproject.toml",
           '[tool.pytest.ini_options]\naddopts = "-n 4 -p no:xdist"\n')
    (tmp_path / "tests").mkdir()

    result = E.check_config(_config(tmp_path), project=".")

    assert result.status == E.STATUS_EXECUTABLE
    assert result.full is True


def test_scoped_refused_conftest_hook_is_not_executable(tmp_path):
    _write(tmp_path / "tests" / "conftest.py",
           "def pytest_runtest_protocol(item, nextitem):\n    return None\n")

    result = E.check_config(_config(tmp_path), project=".")

    assert result.status == E.STATUS_NOT_EXECUTABLE
    assert result.reason == (
        "tests/conftest.py defines pytest_runtest_protocol, which ptest refuses")
    assert result.fix == (
        "move pytest_runtest_protocol out of conftest.py into an installed plugin,"
        " or configure a command profile")


def test_dot_test_root_is_caveat_without_full(tmp_path):
    (tmp_path / "tests").mkdir()

    result = E.check_config(_config(tmp_path, test_roots=(".",)), project=".")

    assert result.status == E.STATUS_CAVEAT
    assert result.caveats == ('ptest --full unavailable: test_roots is "."',)
    assert result.full is False


def test_narrowing_addopts_are_caveat_without_full(tmp_path):
    _write(tmp_path / "pyproject.toml",
           '[tool.pytest.ini_options]\naddopts = \'-m "not slow"\'\n')
    (tmp_path / "tests").mkdir()

    result = E.check_config(_config(tmp_path), project=".")

    assert result.status == E.STATUS_CAVEAT
    assert result.caveats == (
        "ptest --full unavailable: pytest addopts narrow the inventory (-m)",)
    assert result.full is False


def test_maxfail_nonzero_narrows_without_full(tmp_path):
    _write(tmp_path / "pyproject.toml",
           '[tool.pytest.ini_options]\naddopts = "--maxfail=3"\n')
    (tmp_path / "tests").mkdir()

    result = E.check_config(_config(tmp_path), project=".")

    assert result.status == E.STATUS_CAVEAT
    assert result.caveats == (
        "ptest --full unavailable: pytest addopts narrow the inventory (--maxfail)",)
    assert result.full is False


def test_maxfail_zero_is_not_narrowing():
    assert E._narrowing_tokens(("--maxfail=0",)) == ()
    assert E._narrowing_tokens(("--maxfail", "0")) == ()
    assert E._narrowing_tokens(("--maxfail=3",)) == ("--maxfail",)
    assert E._narrowing_tokens(("--maxfail", "3")) == ("--maxfail",)
    assert E._narrowing_tokens(("--maxfail=0", "-m", "not slow")) == ("-m",)


def test_full_refused_conftest_hook_is_caveat_without_full(tmp_path):
    _write(tmp_path / "tests" / "conftest.py",
           "def pytest_sessionfinish(session, exitstatus):\n    return None\n")

    result = E.check_config(_config(tmp_path), project=".")

    assert result.status == E.STATUS_CAVEAT
    assert result.caveats == (
        "ptest --full unavailable: tests/conftest.py defines pytest_sessionfinish",)
    assert result.full is False


def test_vitest_bad_launcher_is_not_executable(tmp_path):
    result = E.check_config(
        _config(tmp_path, kind=C.RunnerKind.VITEST, launcher=("python",)),
        project=".")

    assert result.status == E.STATUS_NOT_EXECUTABLE
    assert result.reason == "vitest launcher must be node"
    assert result.fix == 'set [runner] launcher = ["node"] in .ptest.toml'


def test_vitest_missing_entry_without_setup_is_not_executable(tmp_path):
    result = E.check_config(
        _config(tmp_path, kind=C.RunnerKind.VITEST, launcher=("node",)),
        project=".")

    assert result.status == E.STATUS_NOT_EXECUTABLE
    assert result.reason == "node_modules/vitest/vitest.mjs is missing"
    assert result.fix == (
        'install dependencies, or declare [setup] argv = ["npm", "ci"] in .ptest.toml')


def test_vitest_is_executable_with_exclusive_caveat(tmp_path):
    entry = tmp_path / "node_modules" / "vitest" / "vitest.mjs"
    _write(entry, "export {};\n")

    result = E.check_config(
        _config(tmp_path, kind=C.RunnerKind.VITEST, launcher=("node",)),
        project=".")

    assert result.status == E.STATUS_CAVEAT
    assert result.caveats == (
        "exclusive: Vitest runs as one command and manages its own workers",)
    assert result.full is True


def test_command_is_executable_with_exclusive_caveat(tmp_path):
    result = E.check_config(
        _config(tmp_path, kind=C.RunnerKind.COMMAND,
                launcher=("python", "run.py"), test_roots=(".",)),
        project=".")

    assert result.status == E.STATUS_CAVEAT
    assert result.caveats == ("exclusive: runs as one literal command",)
    assert result.full is True


@pytest.mark.parametrize("kind", [C.RunnerKind.GO, C.RunnerKind.CARGO])
def test_native_go_and_cargo_are_not_executable(tmp_path, kind):
    launcher = ("go",) if kind is C.RunnerKind.GO else ("cargo",)

    result = E.check_config(
        _config(tmp_path, kind=kind, launcher=launcher, test_roots=(".",)),
        project=".")

    assert result.status == E.STATUS_NOT_EXECUTABLE
    assert result.runner == kind.value
    assert result.reason == f"native {kind.value} execution is not available in this release"
    assert result.fix == (
        'configure kind = "command" with an explicit launcher in .ptest.toml')
    assert result.full is False


def test_missing_setup_path_is_trailing_caveat(tmp_path):
    setup = C.SetupConfig(argv=("npm", "ci"),
                          required_paths=("node_modules/.ptest-setup-done",),
                          network=True, lifecycle_scripts=True)
    entry = tmp_path / "node_modules" / "vitest" / "vitest.mjs"
    _write(entry, "export {};\n")

    result = E.check_config(
        _config(tmp_path, kind=C.RunnerKind.VITEST, launcher=("node",),
                setup=setup),
        project=".")

    assert result.status == E.STATUS_CAVEAT
    assert result.caveats == (
        "exclusive: Vitest runs as one command and manages its own workers",
        "first run executes setup: npm ci")


def test_example_prefers_first_pytest_test_file(tmp_path):
    tests = tmp_path / "tests"
    _write(tests / "test_b.py", "def test_b():\n    assert True\n")
    _write(tests / "test_a.py", "def test_a():\n    assert True\n")

    result = E.check_config(_config(tmp_path), project=".")

    assert result.status == E.STATUS_EXECUTABLE
    assert result.example == "tests/test_a.py"


def test_commands_lists_scoped_runs_then_full():
    items = (
        E.Executability(project=".", runner="pytest",
                        status=E.STATUS_EXECUTABLE, caveats=(),
                        reason=None, fix=None, full=True,
                        example="tests/test_a.py"),
        E.Executability(project="web", runner="vitest",
                        status=E.STATUS_CAVEAT,
                        caveats=("exclusive: Vitest runs as one command and manages its own workers",),
                        reason=None, fix=None, full=True,
                        example="src/a.test.ts"),
    )

    assert E.commands(items) == (
        "ptest tests/test_a.py", "ptest web/src/a.test.ts", "ptest --full")


def test_commands_skips_full_when_any_project_lacks_it():
    items = (
        E.Executability(project="api", runner="pytest",
                        status=E.STATUS_CAVEAT,
                        caveats=('ptest --full unavailable: test_roots is "."',),
                        reason=None, fix=None, full=False,
                        example="tests/test_a.py"),
        E.Executability(project="web", runner="vitest",
                        status=E.STATUS_CAVEAT,
                        caveats=("exclusive: Vitest runs as one command and manages its own workers",),
                        reason=None, fix=None, full=True, example=None),
    )

    assert E.commands(items) == ("ptest api/tests/test_a.py",)


def test_check_resolution_standalone(tmp_path):
    (tmp_path / "tests").mkdir()
    config = _config(tmp_path)
    resolution = C.ConfigResolution(root=tmp_path, path=tmp_path / ".ptest.toml",
                                    config=config)

    (items,) = E.check_resolution(resolution)

    assert items.project == "."
    assert items.runner == "pytest"
    assert items.status == E.STATUS_EXECUTABLE


def test_check_resolution_reports_missing_child(tmp_path):
    from ptest.config import init_project
    from ptest.contracts import InitOptions
    api = tmp_path / "api"
    web = tmp_path / "web"
    api.mkdir()
    web.mkdir()
    (api / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    (web / "package.json").write_text('{"devDependencies": {"vitest": "^3"}}\n')
    init_project(tmp_path, InitOptions(
        runner=None, dry_run=False, reveal_command=False,
        children=(("api", C.RunnerKind.PYTEST), ("web", C.RunnerKind.VITEST))))
    (web / ".ptest.toml").unlink()
    from ptest.config import resolve_config
    resolution = resolve_config(tmp_path)

    items = E.check_resolution(resolution)
    by_project = {item.project: item for item in items}

    assert set(by_project) == {"api", "web"}
    assert by_project["web"].status == E.STATUS_NOT_EXECUTABLE
    assert by_project["web"].runner == "unknown"
    assert by_project["web"].reason == "child configuration is missing or invalid"
    assert by_project["web"].fix == "run ptest init from the repository root"
    assert by_project["web"].full is False
    assert by_project["web"].example is None


def test_check_resolution_without_configuration(tmp_path):
    resolution = C.ConfigResolution(
        root=tmp_path, path=None, config=None,
        problem=C.Problem(code="initialization-required",
                          message="project configuration is required",
                          phase="config", retryable=False))

    (items,) = E.check_resolution(resolution)

    assert items.project == "."
    assert items.runner == "unknown"
    assert items.status == E.STATUS_NOT_EXECUTABLE
    assert items.reason == "no ptest configuration"
    assert items.fix == "run ptest init from the repository root"


def test_check_never_starts_a_subprocess_or_imports_project_code(tmp_path, monkeypatch):
    import subprocess

    def _forbidden(*args, **kwargs):
        raise AssertionError("executability must not use subprocess")

    monkeypatch.setattr(subprocess, "run", _forbidden)
    monkeypatch.setattr(subprocess, "Popen", _forbidden)
    monkeypatch.setattr(subprocess, "call", _forbidden)
    monkeypatch.setattr(subprocess, "check_output", _forbidden)
    _write(tmp_path / "pyproject.toml",
           '[tool.pytest.ini_options]\naddopts = "-n 4"\n')
    _write(tmp_path / "tests" / "test_a.py", "def test_a():\n    assert True\n")

    result = E.check_config(_config(tmp_path), project=".")

    assert result.status == E.STATUS_NOT_EXECUTABLE
    source = Path(E.__file__).read_text(encoding="utf-8")
    assert "import subprocess" not in source
    assert "from subprocess" not in source
    assert "subprocess.run" not in source
    assert "subprocess.Popen" not in source
    assert "import_module" not in source
    assert "__import__" not in source


def test_non_xdist_addopts_are_untouched_by_serial_detection(tmp_path):
    _write(tmp_path / "pyproject.toml",
           '[tool.pytest.ini_options]\naddopts = \'-m "not slow" --timeout=300\'\n')
    (tmp_path / "tests").mkdir()
    from ptest.config import _fresh_config

    config = _fresh_config(tmp_path, tmp_path / ".ptest.toml", C.RunnerKind.PYTEST)

    assert config.runner.args == ()
