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
    assert ready.verdict() == "runs: yes"
    assert ready.to_public() == {"status": "executable", "detail": "ready", "fix": None}

    caveat = E.Executability(
        project=".", runner="pytest", status=E.STATUS_CAVEAT,
        caveats=("parallel: 4 workers (xdist, --dist load)", "second caveat"),
        reason=None, fix=None, full=True, example=None)
    assert caveat.verdict() == (
        "runs: yes; parallel: 4 workers (xdist, --dist load); second caveat")
    assert caveat.to_public() == {
        "status": "caveat",
        "detail": "parallel: 4 workers (xdist, --dist load); second caveat",
        "fix": None}

    blocked = E.Executability(
        project="api", runner="pytest", status=E.STATUS_NOT_EXECUTABLE,
        caveats=(),
        reason="remote xdist workers (--tx, --rsyncdir, --px) are not supported",
        fix="remove --tx, --rsyncdir and --px from your pytest addopts",
        full=False, example=None)
    assert blocked.verdict() == (
        "runs: no — remote xdist workers (--tx, --rsyncdir, --px) are not supported"
        " → remove --tx, --rsyncdir and --px from your pytest addopts")
    assert blocked.to_public() == {
        "status": "not-executable",
        "detail": "remote xdist workers (--tx, --rsyncdir, --px) are not supported",
        "fix": "remove --tx, --rsyncdir and --px from your pytest addopts"}


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


def test_xdist_addopts_without_serial_is_runnable_with_parallel_fallback(tmp_path):
    _write(tmp_path / "pyproject.toml",
           '[tool.pytest.ini_options]\naddopts = "-n 4 --dist=loadgroup"\n')

    result = E.check_config(_config(tmp_path), project=".")

    assert result.status == E.STATUS_CAVEAT
    assert result.runner == "pytest"
    assert result.parallel == (
        "no — ptest cannot verify pytest-xdist for launcher python; "
        "use an absolute interpreter or a uv launcher to run in parallel")
    assert result.full is True


def test_xdist_addopts_with_serial_is_caveat(tmp_path):
    _write(tmp_path / "pyproject.toml",
           '[tool.pytest.ini_options]\naddopts = "-n 4 --dist=loadgroup"\n')

    result = E.check_config(_config(tmp_path, args=("-n", "0")), project=".")

    assert result.status == E.STATUS_CAVEAT
    assert result.caveats == (
        "parallel: no — ptest cannot verify pytest-xdist for launcher python; "
        "use an absolute interpreter or a uv launcher to run in parallel",)
    assert result.full is True


def test_no_xdist_suppresses_activation(tmp_path):
    _write(tmp_path / "pyproject.toml",
           '[tool.pytest.ini_options]\naddopts = "-n 4 -p no:xdist"\n')
    (tmp_path / "tests").mkdir()

    result = E.check_config(_config(tmp_path), project=".")

    assert result.status == E.STATUS_CAVEAT
    assert result.caveats == (
        "parallel: no — xdist is not enabled in your pytest config",)
    assert result.full is True


@pytest.mark.parametrize("tokens,active", [
    (("-n0",), False),
    (("-n", "0"), False),
    (("--numprocesses=0",), False),
    (("--numprocesses", "0"), False),
    (("--dist", "no"), False),
    (("--dist=no",), False),
    (("-vn0",), False),
    (("-n", "2"), True),
    (("-n2",), True),
    (("-nauto",), True),
    (("--numprocesses=2",), True),
    (("--dist", "load"), True),
    (("--dist=load",), True),
    (("-n",), True),
])
def test_xdist_activation_is_value_aware(tokens, active):
    assert E._xdist_active(tokens) is active


@pytest.mark.parametrize("tokens,expected", [
    (("-vx",), ("-vx",)),
    (("-xvs",), ("-xvs",)),
    (("-lx",), ("-lx",)),
    (("-xl",), ("-xl",)),
    (("-vk", "foo"), ("-vk",)),
    (("-kfoo",), ("-k",)),
    (("-c", "other.ini"), ("-c",)),
    (("tests/test_a.py::test_x",), ("tests/test_a.py::test_x",)),
    (("--collect-only",), ("--collect-only",)),
    (("--exitfirst",), ("--exitfirst",)),
    (("-q",), ()),
])
def test_full_tokens_match_bridge_full_refusals(tokens, expected):
    """Behavior pins retargeted to the live bridge (was _narrowing_tokens)."""
    from ptest.runtime.pytest_bridge import full_refusal_name

    assert tuple(
        name for index in range(len(tokens))
        if (name := full_refusal_name(tokens, index)) is not None
    ) == expected


@pytest.mark.parametrize("tokens", [
    ("-rxXs",), ("-rsx",), ("-vrx",), ("-ra",), ("-rA",),
])
def test_report_char_clusters_are_not_full_refusals(tokens):
    """Behavior pins retargeted to the live bridge (was _narrowing_tokens)."""
    from ptest.runtime.pytest_bridge import full_refusal_name

    assert full_refusal_name(tokens, 0) is None


def test_clustered_x_addopts_are_project_filtered_full(tmp_path):
    """Section F flips this twin: checked-in clusters are allowed and labelled."""
    _write(tmp_path / "pyproject.toml",
           '[tool.pytest.ini_options]\naddopts = "-vx"\n')
    _write(tmp_path / "tests" / "test_a.py", "def test_a():\n    assert True\n")

    result = E.check_config(_config(tmp_path), project=".")

    assert result.full is True
    assert result.full_suite == "your pytest config: -vx"
    assert result.caveats == (
        "parallel: no — xdist is not enabled in your pytest config",
        "full suite = your pytest config: -vx")
    assert "ptest --full" in E.commands((result,))


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


@pytest.mark.parametrize("hook", ["pytest_runtest_logreport", "pytest_collectreport"])
def test_reporting_hook_conftest_makes_full_unavailable(tmp_path, hook):
    """Round 18: static prediction mirrors the bridge refusal for these hooks."""
    _write(tmp_path / "tests" / "conftest.py",
           "def %s(*args):\n    return None\n" % hook)

    result = E.check_config(_config(tmp_path), project=".")

    assert result.status == E.STATUS_CAVEAT
    assert result.full_blocked == "tests/conftest.py defines %s" % hook
    assert result.caveats == (
        "parallel: no — xdist is not enabled in your pytest config",
        "full suite: not available — tests/conftest.py defines %s" % hook)
    assert result.full is False


def test_dot_test_root_is_caveat_without_full(tmp_path):
    (tmp_path / "tests").mkdir()

    result = E.check_config(_config(tmp_path, test_roots=(".",)), project=".")

    assert result.status == E.STATUS_CAVEAT
    assert result.full_blocked == 'test_roots is "."'
    assert result.caveats == (
        "parallel: no — xdist is not enabled in your pytest config",
        'full suite: not available — test_roots is "."')
    assert result.full is False


def test_narrowing_addopts_are_project_filtered_full(tmp_path):
    """Section F flips this twin: checked-in -m is allowed and labelled."""
    _write(tmp_path / "pyproject.toml",
           '[tool.pytest.ini_options]\naddopts = \'-m "not slow"\'\n')
    (tmp_path / "tests").mkdir()

    result = E.check_config(_config(tmp_path), project=".")

    assert result.status == E.STATUS_CAVEAT
    assert result.full_suite == 'your pytest config: -m "not slow"'
    assert result.caveats == (
        "parallel: no — xdist is not enabled in your pytest config",
        'full suite = your pytest config: -m "not slow"')
    assert result.full is True


def test_maxfail_nonzero_is_project_filtered_full(tmp_path):
    """Section F flips this twin: checked-in --maxfail is allowed and labelled."""
    _write(tmp_path / "pyproject.toml",
           '[tool.pytest.ini_options]\naddopts = "--maxfail=3"\n')
    (tmp_path / "tests").mkdir()

    result = E.check_config(_config(tmp_path), project=".")

    assert result.status == E.STATUS_CAVEAT
    assert result.full_suite == "your pytest config: --maxfail=3"
    assert result.caveats == (
        "parallel: no — xdist is not enabled in your pytest config",
        "full suite = your pytest config: --maxfail=3")
    assert result.full is True


def test_redirect_addopts_stay_unavailable(tmp_path):
    _write(tmp_path / "pyproject.toml",
           '[tool.pytest.ini_options]\naddopts = "-c other.ini"\n')
    (tmp_path / "tests").mkdir()

    result = E.check_config(_config(tmp_path), project=".")

    assert result.status == E.STATUS_CAVEAT
    assert result.full_blocked == "pytest addopts redirect native configuration (-c)"
    assert result.caveats == (
        "parallel: no — xdist is not enabled in your pytest config",
        "full suite: not available — pytest addopts redirect native configuration (-c)")
    assert result.full is False


def test_collection_hook_is_project_filtered_full(tmp_path):
    """Section F: a conftest collection hook is allowed and labelled."""
    _write(tmp_path / "tests" / "conftest.py",
           "def pytest_collection_modifyitems(items):\n    return None\n")

    result = E.check_config(_config(tmp_path), project=".")

    assert result.status == E.STATUS_CAVEAT
    assert result.full_suite == "your pytest config: conftest.py hooks"
    assert result.caveats == (
        "parallel: no — xdist is not enabled in your pytest config",
        "full suite = your pytest config: conftest.py hooks")
    assert result.full is True


def test_persea_shaped_addopts_are_project_filtered_full_with_serial_caveat(tmp_path):
    """Persea api shape: xdist addopts plus a -m filter stay executable."""
    _write(tmp_path / "pyproject.toml",
           '[tool.pytest.ini_options]\n'
           'addopts = \'-p xdist.plugin -n 2 --dist=loadgroup -m "not slow"\'\n')
    (tmp_path / "tests").mkdir()

    result = E.check_config(_config(tmp_path, args=("-n", "0")), project=".")

    assert result.status == E.STATUS_CAVEAT
    assert result.parallel == (
        "no — ptest cannot verify pytest-xdist for launcher python; "
        "use an absolute interpreter or a uv launcher to run in parallel")
    assert result.full_suite == 'your pytest config: -m "not slow"'
    assert result.full is True


def test_maxfail_zero_is_not_narrowing():
    """Behavior pins retargeted to the live bridge (was _narrowing_tokens)."""
    from ptest.runtime.pytest_bridge import full_narrowing_text

    assert full_narrowing_text(("--maxfail=0",)) is None
    assert full_narrowing_text(("--maxfail", "0")) is None
    assert full_narrowing_text(("--maxfail=3",)) == "--maxfail=3"
    assert full_narrowing_text(("--maxfail", "3")) == "--maxfail 3"
    assert full_narrowing_text(("--maxfail=0", "-m", "not slow")) == "-m not slow"


def test_pytest_toml_addopts_are_project_filtered_full(tmp_path):
    """Section F HIGH: pytest 9 reads pytest.toml first, statically too."""
    _write(tmp_path / "pytest.toml", '[pytest]\naddopts = ["-k", "not slow"]\n')
    (tmp_path / "tests").mkdir()

    result = E.check_config(_config(tmp_path), project=".")

    assert result.status == E.STATUS_CAVEAT
    assert result.full_suite == 'your pytest config: -k "not slow"'
    assert result.full is True


def test_pytest_toml_wins_over_pytest_ini(tmp_path):
    """Section F HIGH: the first config file in pytest 9 order decides."""
    _write(tmp_path / "pytest.toml", '[pytest]\naddopts = ["-k", "not slow"]\n')
    _write(tmp_path / "pytest.ini", '[pytest]\naddopts = "-m \'not fast\'"\n')
    (tmp_path / "tests").mkdir()

    result = E.check_config(_config(tmp_path), project=".")

    assert result.full_suite == 'your pytest config: -k "not slow"'
    assert result.full is True


@pytest.mark.parametrize("addopts", ["--co", "--lf"])
def test_non_allowlisted_ini_narrowing_makes_full_unavailable(tmp_path, addopts):
    """Section F MEDIUM: observation controls predict a refused full run."""
    _write(tmp_path / "pytest.ini", "[pytest]\naddopts = %s\n" % addopts)
    (tmp_path / "tests").mkdir()

    result = E.check_config(_config(tmp_path), project=".")

    assert result.status == E.STATUS_CAVEAT
    assert result.full_blocked == (
        "pytest addopts narrow or observe the suite (%s)" % addopts)
    assert result.full is False


def test_conftest_sessionfinish_is_project_filtered_full(tmp_path):
    """Round 14: a conftest sessionfinish is allowed and labelled, like persea api."""
    _write(tmp_path / "tests" / "conftest.py",
           "def pytest_sessionfinish(session, exitstatus):\n    return None\n")

    result = E.check_config(_config(tmp_path), project=".")

    assert result.status == E.STATUS_CAVEAT
    assert result.full_suite == "your pytest config: conftest.py hooks"
    assert result.full is True


def test_persea_shaped_static_prediction_combines_narrowing_and_hooks(tmp_path):
    """Round 14: the persea api shape predicts one combined label in init."""
    _write(tmp_path / "pyproject.toml",
           '[tool.pytest.ini_options]\n'
           'addopts = \'-m "not extended_migration"\'\n')
    _write(tmp_path / "tests" / "conftest.py",
           "def pytest_collection_modifyitems(items):\n    return None\n"
           "\n"
           "def pytest_sessionfinish(session, exitstatus):\n    return None\n")

    result = E.check_config(_config(tmp_path), project=".")

    assert result.status == E.STATUS_CAVEAT
    assert result.full_suite == (
        'your pytest config: -m "not extended_migration", conftest.py hooks')
    assert result.full is True


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
    assert result.parallel == "inside vitest (its own workers)"
    assert result.parallel_short == "inside vitest"
    assert result.caveats == (
        "parallel: inside vitest (its own workers)",)
    assert result.full is True


def test_command_without_setup_is_executable(tmp_path):
    result = E.check_config(
        _config(tmp_path, kind=C.RunnerKind.COMMAND,
                launcher=("python", "run.py"), test_roots=(".",)),
        project=".")

    assert result.status == E.STATUS_EXECUTABLE
    assert result.caveats == ()
    assert result.full is True
    assert result.verdict() == "runs: yes"


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


def test_declared_setup_is_caveat_even_when_paths_present(tmp_path):
    setup = C.SetupConfig(argv=("uv", "sync", "--locked"),
                          required_paths=("tests",),
                          network=False, lifecycle_scripts=False)
    _write(tmp_path / "tests" / "test_a.py", "def test_a():\n    assert True\n")

    result = E.check_config(_config(tmp_path, setup=setup), project=".")

    assert result.status == E.STATUS_CAVEAT
    assert result.setup == "uv sync --locked"
    assert result.caveats == (
        "parallel: no — xdist is not enabled in your pytest config",
        "setup: uv sync --locked (ptest runs it when needed)",)
    assert result.full is True


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
        "parallel: inside vitest (its own workers)",
        "setup: npm ci (ptest runs it when needed)")


def test_example_prefers_first_pytest_test_file(tmp_path):
    tests = tmp_path / "tests"
    _write(tests / "test_b.py", "def test_b():\n    assert True\n")
    _write(tests / "test_a.py", "def test_a():\n    assert True\n")

    result = E.check_config(_config(tmp_path), project=".")

    assert result.status == E.STATUS_CAVEAT
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
    assert items.status == E.STATUS_CAVEAT


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

    assert result.status == E.STATUS_CAVEAT
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


def _persea_web_shape(root):
    """Persea-web-shaped fixture: Playwright specs in e2e/, unit tests in src/."""
    _write(root / "vitest.config.ts",
           "import { defineConfig, configDefaults } from 'vitest/config';\n"
           "export default defineConfig({\n"
           "  test: {\n"
           "    exclude: [...configDefaults.exclude, 'e2e/**'],\n"
           "  },\n"
           "});\n")
    _write(root / "playwright.config.ts",
           "import { defineConfig } from '@playwright/test';\n"
           "export default defineConfig({ testDir: './e2e' });\n")
    _write(root / "e2e" / "agents.spec.ts",
           "import { test } from '@playwright/test';\n"
           "test('flow', () => {});\n")
    _write(root / "src" / "i18n-defaults.test.ts",
           "import { it } from 'vitest';\n"
           "it('defaults', () => {});\n")
    _write(root / "node_modules" / "vitest" / "vitest.mjs", "export {};\n")


def _vitest_dot_config(tmp_path):
    return _config(tmp_path, kind=C.RunnerKind.VITEST, launcher=("node",),
                   test_roots=(".",))


def test_vitest_example_skips_excluded_e2e_spec(tmp_path):
    _persea_web_shape(tmp_path)

    result = E.check_config(_vitest_dot_config(tmp_path), project=".")

    assert result.example == "src/i18n-defaults.test.ts"


def test_vitest_example_skips_playwright_test_dir_without_config(tmp_path):
    _write(tmp_path / "e2e" / "agents.spec.ts",
           "import { test } from '@playwright/test';\n"
           "test('flow', () => {});\n")
    _write(tmp_path / "src" / "a.test.ts",
           "import { it } from 'vitest';\n"
           "it('works', () => {});\n")
    _write(tmp_path / "node_modules" / "vitest" / "vitest.mjs", "export {};\n")

    result = E.check_config(_vitest_dot_config(tmp_path), project=".")

    assert result.example == "src/a.test.ts"


def test_vitest_example_skips_playwright_import_outside_test_dir(tmp_path):
    # The Playwright file sorts first, so only the import check can skip it.
    _write(tmp_path / "src" / "a-play.spec.ts",
           "import { test } from '@playwright/test';\n"
           "test('flow', () => {});\n")
    _write(tmp_path / "src" / "z.test.ts",
           "import { it } from 'vitest';\n"
           "it('works', () => {});\n")
    _write(tmp_path / "node_modules" / "vitest" / "vitest.mjs", "export {};\n")

    result = E.check_config(_vitest_dot_config(tmp_path), project=".")

    assert result.example == "src/z.test.ts"


def test_vitest_example_applies_literal_include(tmp_path):
    _write(tmp_path / "vitest.config.ts",
           "import { defineConfig } from 'vitest/config';\n"
           "export default defineConfig({\n"
           "  test: { include: ['src/**/*.test.ts'] },\n"
           "});\n")
    _write(tmp_path / "e2e" / "agents.spec.ts",
           "import { it } from 'vitest';\n"
           "it('flow', () => {});\n")
    _write(tmp_path / "src" / "a.test.ts",
           "import { it } from 'vitest';\n"
           "it('works', () => {});\n")
    _write(tmp_path / "node_modules" / "vitest" / "vitest.mjs", "export {};\n")

    result = E.check_config(_vitest_dot_config(tmp_path), project=".")

    assert result.example == "src/a.test.ts"


def test_vitest_example_ignores_non_literal_config_parts(tmp_path):
    _write(tmp_path / "vitest.config.ts",
           "import { defineConfig, configDefaults } from 'vitest/config';\n"
           "const EXTRA = 'dist/**';\n"
           "export default defineConfig({\n"
           "  test: { exclude: [...configDefaults.exclude, SOME_CONST, 'e2e/**'] },\n"
           "});\n")
    _write(tmp_path / "e2e" / "agents.spec.ts",
           "import { it } from 'vitest';\n"
           "it('flow', () => {});\n")
    _write(tmp_path / "src" / "a.test.ts",
           "import { it } from 'vitest';\n"
           "it('works', () => {});\n")
    _write(tmp_path / "node_modules" / "vitest" / "vitest.mjs", "export {};\n")

    result = E.check_config(_vitest_dot_config(tmp_path), project=".")

    assert result.example == "src/a.test.ts"


# --- Round 8 audit twins: anchored glob matching ---------------------------


def _write_vitest_config(tmp_path, *, test_body):
    _write(tmp_path / "vitest.config.ts",
           "import { defineConfig, configDefaults } from 'vitest/config';\n"
           "export default defineConfig({\n"
           "  test: {\n" + test_body + "\n"
           "  },\n"
           "});\n")
    _write(tmp_path / "node_modules" / "vitest" / "vitest.mjs", "export {};\n")


def _vitest_unit(tmp_path, rel):
    _write(tmp_path / rel,
           "import { it } from 'vitest';\n"
           "it('works', () => {});\n")


def test_glob_match_is_left_anchored():
    assert E._glob_match("src/**/*.test.ts", "src/a.test.ts")
    assert E._glob_match("src/**/*.test.ts", "src/a/b/c/d.test.ts")
    assert E._glob_match("e2e/**", "e2e/a/b.spec.ts")
    assert not E._glob_match("src/**/*.test.ts", "packages/x/src/a.test.ts")
    assert not E._glob_match("e2e/**", "src/e2e/x.spec.ts")


def test_glob_include_reaches_deep_nested_files(tmp_path):
    """Repro 1: include src/**/*.test.ts plus a deep test gives an example."""
    _write_vitest_config(tmp_path, test_body="    include: ['src/**/*.test.ts'],")
    _vitest_unit(tmp_path, "src/a/b/deep.test.ts")

    result = E.check_config(_vitest_dot_config(tmp_path), project=".")

    assert result.example == "src/a/b/deep.test.ts"


def test_glob_exclude_reaches_deep_nested_files(tmp_path):
    """Repro 2: exclude e2e/** hides a deep spec that only imports fixtures."""
    _write_vitest_config(
        tmp_path,
        test_body="    exclude: [...configDefaults.exclude, 'e2e/**'],")
    _write(tmp_path / "e2e" / "auth" / "login.spec.ts",
           "import { helper } from '../fixtures';\n"
           "test('flow', () => {});\n")
    _vitest_unit(tmp_path, "z.test.ts")

    result = E.check_config(_vitest_dot_config(tmp_path), project=".")

    assert result.example == "z.test.ts"


def test_glob_include_does_not_match_nested_project_prefix(tmp_path):
    _write_vitest_config(tmp_path, test_body="    include: ['src/**/*.test.ts'],")
    _vitest_unit(tmp_path, "packages/x/src/a.test.ts")

    result = E.check_config(_vitest_dot_config(tmp_path), project=".")

    assert result.example is None


def test_glob_exclude_does_not_match_nested_e2e_dir(tmp_path):
    _write_vitest_config(
        tmp_path,
        test_body="    exclude: [...configDefaults.exclude, 'e2e/**'],")
    _vitest_unit(tmp_path, "src/e2e/x.spec.ts")

    result = E.check_config(_vitest_dot_config(tmp_path), project=".")

    assert result.example == "src/e2e/x.spec.ts"


def test_glob_extglob_include_accepts(tmp_path):
    _write_vitest_config(
        tmp_path, test_body="    include: ['**/*.{test,spec}.?(c|m)[jt]s?(x)'],")
    _vitest_unit(tmp_path, "src/a.test.ts")

    result = E.check_config(_vitest_dot_config(tmp_path), project=".")

    assert result.example == "src/a.test.ts"


def test_glob_extglob_exclude_is_ignored(tmp_path):
    _write_vitest_config(tmp_path, test_body="    exclude: ['src/*.?(c|m)ts'],")
    _vitest_unit(tmp_path, "src/a.test.ts")

    result = E.check_config(_vitest_dot_config(tmp_path), project=".")

    assert result.example == "src/a.test.ts"


def test_glob_expands_every_brace_group(tmp_path):
    _write_vitest_config(
        tmp_path, test_body="    include: ['**/*.{test,spec}.{ts,js}'],")
    _write(tmp_path / "src" / "a.spec.js",
           "import { it } from 'vitest';\n"
           "it('works', () => {});\n")
    _write(tmp_path / "node_modules" / "vitest" / "vitest.mjs", "export {};\n")

    result = E.check_config(_vitest_dot_config(tmp_path), project=".")

    assert result.example == "src/a.spec.js"


def test_static_reader_accepts_quoted_test_keys(tmp_path):
    _write(tmp_path / "vitest.config.ts",
           "import { defineConfig } from 'vitest/config';\n"
           "export default defineConfig({\n"
           '  "test": {\n'
           '    "include": [\'src/**/*.test.ts\'],\n'
           "  },\n"
           "});\n")
    _write(tmp_path / "node_modules" / "vitest" / "vitest.mjs", "export {};\n")
    # Deep path: the default e2e/** testDir glob misses it until the glob
    # fix, so only the quoted include can hide this file.
    _write(tmp_path / "e2e" / "auth" / "agents.spec.ts",
           "import { it } from 'vitest';\n"
           "it('flow', () => {});\n")

    result = E.check_config(_vitest_dot_config(tmp_path), project=".")

    assert result.example is None


def test_static_reader_reads_every_test_block(tmp_path):
    _write(tmp_path / "vitest.config.ts",
           "import { defineConfig } from 'vitest/config';\n"
           "export default defineConfig({\n"
           "  test: { exclude: ['dist/**'] },\n"
           "});\n"
           "export const extra = defineConfig({\n"
           "  test: { include: ['src/**/*.test.ts'] },\n"
           "});\n")
    _write(tmp_path / "node_modules" / "vitest" / "vitest.mjs", "export {};\n")
    # Deep path: only the second block's include can hide this file.
    _write(tmp_path / "e2e" / "auth" / "agents.spec.ts",
           "import { it } from 'vitest';\n"
           "it('flow', () => {});\n")

    result = E.check_config(_vitest_dot_config(tmp_path), project=".")

    assert result.example is None


def test_vitest_config_wins_over_vite_config(tmp_path):
    # lib/ is outside both src/** and the Playwright testDir, so only the
    # vite.config include can hide it when configs are wrongly merged.
    _write(tmp_path / "vite.config.ts",
           "import { defineConfig } from 'vitest/config';\n"
           "export default defineConfig({\n"
           "  test: { include: ['src/**/*.test.ts'] },\n"
           "});\n")
    _write_vitest_config(
        tmp_path,
        test_body="    exclude: [...configDefaults.exclude, 'e2e/**'],")
    _vitest_unit(tmp_path, "lib/b.test.ts")

    result = E.check_config(_vitest_dot_config(tmp_path), project=".")

    assert result.example == "lib/b.test.ts"


def test_dot_root_walk_dedupes_priority_bases(tmp_path):
    """Priority bases (src/) are not re-walked by the trailing root walk."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.test.ts").write_text("export {};\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_b.py").write_text(
        "def test_b():\n    assert True\n", encoding="utf-8")
    config = _config(tmp_path, test_roots=(".",))

    budget = [10]
    found = list(E.iter_candidates(
        tmp_path, ".", C.RunnerKind.PYTEST, budget))

    assert found == ["tests/test_b.py"]
    assert budget == [7]


def test_js_lexer_skips_regex_after_equals():
    tokens = E._js_tokens("const re = /a{b/; test: { include: ['x'] };")

    # The regex body never lexes; the only brace is the test: block's.
    assert ("ident", "a") not in tokens
    assert ("ident", "b") not in tokens
    assert tokens.count(("punct", "{")) == 1


# --- Round 10 audit twins: invalid classes are unknown, braces are bounded -


def test_glob_bad_range_never_matches_and_never_raises():
    assert E._glob_match("x[z-a]y", "xby") is False
    assert E._glob_match("[z-a]", "anything.test.ts") is False


def test_glob_bad_range_exclude_is_ignored(tmp_path):
    _write_vitest_config(tmp_path, test_body="    exclude: ['[z-a]'],")
    _vitest_unit(tmp_path, "src/a.test.ts")

    result = E.check_config(_vitest_dot_config(tmp_path), project=".")

    assert result.example == "src/a.test.ts"


def test_glob_bad_range_include_accepts(tmp_path):
    _write_vitest_config(tmp_path, test_body="    include: ['[z-a]'],")
    _vitest_unit(tmp_path, "src/a.test.ts")

    result = E.check_config(_vitest_dot_config(tmp_path), project=".")

    assert result.example == "src/a.test.ts"


def test_glob_brace_explosion_is_unknown():
    assert E._expand_braces("{a,b}" * 16) is None


def test_glob_brace_explosion_compiles_nothing(tmp_path, monkeypatch):
    calls: list[str] = []
    real = E._glob_to_regex

    def counting(pattern):
        calls.append(pattern)
        return real(pattern)

    monkeypatch.setattr(E, "_glob_to_regex", counting)

    assert E._glob_match("{a,b}" * 16, "src/a.test.ts") is False
    assert calls == []


def test_glob_brace_explosion_include_accepts(tmp_path):
    _write_vitest_config(
        tmp_path, test_body="    include: ['" + "{a,b}" * 16 + "'],")
    _vitest_unit(tmp_path, "src/a.test.ts")

    result = E.check_config(_vitest_dot_config(tmp_path), project=".")

    assert result.example == "src/a.test.ts"


def test_vitest_globs_compiled_once_per_filters_call(tmp_path, monkeypatch):
    _write_vitest_config(
        tmp_path,
        test_body="    include: ['src/**/*.test.ts'],\n    exclude: ['e2e/**'],")
    _vitest_unit(tmp_path, "src/a.test.ts")

    calls: list[str] = []
    real = E._glob_to_regex

    def counting(pattern):
        calls.append(pattern)
        return real(pattern)

    monkeypatch.setattr(E, "_glob_to_regex", counting)

    excludes, includes, test_dir = E._vitest_filters(tmp_path)
    compiled_at_filter = len(calls)
    assert compiled_at_filter > 0
    for _ in range(25):
        assert E._is_vitest_candidate(
            tmp_path, "src/a.test.ts", excludes, includes, test_dir)
    assert len(calls) == compiled_at_filter


# --- Parallel tier (T2): facts, ParallelRequest, fallback table ---

_UV = ("uv", "run", "--locked", "--no-sync", "python")


def _uv(tmp_path, **kwargs):
    kwargs.setdefault("launcher", _UV)
    return _config(tmp_path, **kwargs)


def _stub_venv(root, *, version="3.8.0", extra=(), pythons=("python3.12",),
               cov=None):
    venv = root / ".venv"
    venv.mkdir(parents=True, exist_ok=True)
    (venv / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
    for py in pythons:
        site = venv / "lib" / py / "site-packages"
        site.mkdir(parents=True, exist_ok=True)
        (site / f"pytest_xdist-{version}.dist-info").mkdir(exist_ok=True)
        if cov is not None:
            (site / f"pytest_cov-{cov[0]}.dist-info").mkdir(exist_ok=True)
            (site / f"coverage-{cov[1]}.dist-info").mkdir(exist_ok=True)
        for name in extra:
            (site / name).mkdir(exist_ok=True)
    return venv


#: The frozen pytest-cov/coverage tuple the parallel tier admits.
_FROZEN_COV = ("7.1.0", "7.15.0")


def test_fact_keys_exact_order():
    assert E.FACT_KEYS == (
        "project", "runner", "runs", "runs_reason", "runs_fix",
        "parallel", "parallel_short", "parallel_fix",
        "setup", "full_suite", "full_blocked",
    )


def test_mirror_constants_have_frozen_values():
    # T1 owns pytest_bridge.QUALIFIED_XDIST_VERSIONS / PARALLEL_DIST_MODES
    # (absent at this base); T5 asserts the cross-module equality post-merge.
    assert E.XDIST_QUALIFIED_VERSIONS == frozenset({"3.8.0"})
    assert E.XDIST_DIST_MODES == frozenset(
        {"load", "loadscope", "loadfile", "loadgroup", "worksteal"})


def test_facts_shape_runs_and_key_order():
    item = E.Executability(
        project="api", runner="pytest", status=E.STATUS_CAVEAT,
        caveats=("parallel: 4 workers",), reason=None, fix=None,
        full=True, example=None,
        parallel="4 workers (xdist, --dist loadgroup)",
        parallel_short="4 workers")
    facts = item.facts()
    assert tuple(facts) == E.FACT_KEYS
    assert facts["project"] == "api"
    assert facts["runner"] == "pytest"
    assert facts["runs"] is True
    assert facts["runs_reason"] is None
    assert facts["runs_fix"] is None
    assert facts["parallel"] == "4 workers (xdist, --dist loadgroup)"
    assert facts["parallel_short"] == "4 workers"
    assert facts["parallel_fix"] is None


def test_facts_reason_and_fix_only_when_not_runnable():
    item = E.Executability(
        project="api", runner="pytest", status=E.STATUS_NOT_EXECUTABLE,
        caveats=(), reason="remote xdist workers (--tx, --rsyncdir, --px) are not supported",
        fix="remove --tx, --rsyncdir and --px from your pytest addopts",
        full=False, example=None,
        parallel="no — remote xdist workers (--tx, --rsyncdir, --px) are not supported",
        parallel_short="no")
    facts = item.facts()
    assert facts["runs"] is False
    assert facts["runs_reason"] == item.reason
    assert facts["runs_fix"] == item.fix


def test_verdict_runs_wording():
    blocked = E.Executability(
        project="api", runner="pytest", status=E.STATUS_NOT_EXECUTABLE,
        caveats=(), reason="R", fix="F", full=False, example=None)
    assert blocked.verdict() == "runs: no — R → F"
    assert blocked.to_public() == {"status": "not-executable", "detail": "R", "fix": "F"}

    item = E.Executability(
        project=".", runner="pytest", status=E.STATUS_CAVEAT,
        caveats=("parallel: 4 workers",
                 "setup: uv sync --locked (ptest runs it when needed)",
                 "full suite = your pytest config: -m \"not slow\""),
        reason=None, fix=None, full=True, example=None)
    assert item.verdict() == (
        "runs: yes; parallel: 4 workers; "
        "setup: uv sync --locked (ptest runs it when needed); "
        "full suite = your pytest config: -m \"not slow\"")
    assert item.to_public() == {
        "status": "caveat",
        "detail": ("parallel: 4 workers; "
                   "setup: uv sync --locked (ptest runs it when needed); "
                   "full suite = your pytest config: -m \"not slow\""),
        "fix": None}


def test_parallel_qualified_n_workers(tmp_path):
    _write(tmp_path / "pyproject.toml",
           '[tool.pytest.ini_options]\naddopts = "-n 4 --dist=loadgroup"\n')
    _stub_venv(tmp_path)

    result = E.check_config(_uv(tmp_path), project=".")
    assert result.parallel == "4 workers (xdist, --dist loadgroup)"
    assert result.parallel_short == "4 workers"
    assert result.parallel_fix is None

    req = E.parallel_request(_uv(tmp_path), project=".")
    assert (req.active, req.workers, req.auto, req.dist) == (True, 4, False, "loadgroup")
    assert req.reason is None and req.config_level is False and req.runs is True


def test_parallel_auto_workers(tmp_path):
    _write(tmp_path / "pyproject.toml",
           '[tool.pytest.ini_options]\naddopts = "--numprocesses=auto"\n')
    _stub_venv(tmp_path)

    result = E.check_config(_uv(tmp_path), project=".")
    assert result.parallel == "one worker per granted slot (xdist -n auto, --dist load)"
    assert result.parallel_short == "auto"
    assert result.parallel_fix is None
    req = E.parallel_request(_uv(tmp_path), project=".")
    assert req.workers is None and req.auto is True and req.reason is None


def test_parallel_dist_no_maps_to_load(tmp_path):
    _write(tmp_path / "pyproject.toml",
           '[tool.pytest.ini_options]\naddopts = "-n 2 --dist=no"\n')
    _stub_venv(tmp_path)

    result = E.check_config(_uv(tmp_path), project=".")
    assert result.parallel == "2 workers (xdist, --dist load)"


def test_parallel_inactive_text(tmp_path):
    (tmp_path / "tests").mkdir()

    result = E.check_config(_config(tmp_path), project=".")

    assert result.parallel == "no — xdist is not enabled in your pytest config"
    assert result.parallel_short == "no"
    assert result.parallel_fix is None
    req = E.parallel_request(_config(tmp_path), project=".")
    assert req.active is False and req.reason is None and req.runs is True


def test_parallel_vitest_and_command_texts(tmp_path):
    entry = tmp_path / "node_modules" / "vitest" / "vitest.mjs"
    _write(entry, "export {};\n")
    vitest = E.check_config(
        _config(tmp_path, kind=C.RunnerKind.VITEST, launcher=("node",)),
        project=".")
    assert vitest.parallel == "inside vitest (its own workers)"
    assert vitest.parallel_short == "inside vitest"
    assert vitest.parallel_fix is None

    command = E.check_config(
        _config(tmp_path, kind=C.RunnerKind.COMMAND,
                launcher=("python", "run.py"), test_roots=(".",)),
        project=".")
    assert command.parallel is None
    assert command.parallel_short is None


def test_parallel_remote_is_not_runnable(tmp_path):
    _write(tmp_path / "pyproject.toml",
           '[tool.pytest.ini_options]\naddopts = "-n 4 --tx popen"\n')
    _stub_venv(tmp_path)

    result = E.check_config(_uv(tmp_path), project=".")

    assert result.status == E.STATUS_NOT_EXECUTABLE
    assert result.reason == "remote xdist workers (--tx, --rsyncdir, --px) are not supported"
    assert result.fix == "remove --tx, --rsyncdir and --px from your pytest addopts"
    assert result.parallel == (
        "no — remote xdist workers (--tx, --rsyncdir, --px) are not supported")
    assert result.parallel_short == "no"
    req = E.parallel_request(_uv(tmp_path), project=".")
    assert req.runs is False and req.config_level is False


@pytest.mark.parametrize("addopts,reason,config_level", [
    ("-n 4 --dist=each",
     "--dist each is not supported; ptest runs serially", True),
    ("-n 4 --cov",
     "pytest-cov is not installed in the project environment yet; "
     "ptest runs serially until setup installs it", False),
    ("-n 4 --maxprocesses=2",
     "--maxprocesses is not supported; ptest runs serially", True),
    ("-n 1",
     "your pytest config asks for 1 worker", False),
    ("-n 4",
     "pytest-xdist is not installed in the project environment yet; "
     "ptest runs serially until setup installs it", False),
])
def test_parallel_fallback_rows(tmp_path, addopts, reason, config_level):
    _write(tmp_path / "pyproject.toml",
           "[tool.pytest.ini_options]\naddopts = \"%s\"\n" % addopts)

    result = E.check_config(_uv(tmp_path), project=".")

    assert result.status == E.STATUS_CAVEAT
    assert result.parallel == "no — %s" % reason
    assert result.parallel_short == "no"
    assert result.parallel_fix is None
    assert result.verdict().startswith("runs: yes; parallel: no — ")
    req = E.parallel_request(_uv(tmp_path), project=".")
    assert req.reason == reason
    assert req.config_level is config_level
    assert req.runs is True


def test_parallel_cov_admitted_with_frozen_tuple(tmp_path):
    _write(tmp_path / "pyproject.toml",
           '[tool.pytest.ini_options]\naddopts = "-n 4 --cov"\n')
    _stub_venv(tmp_path, cov=_FROZEN_COV)

    req = E.parallel_request(_uv(tmp_path), project=".")
    assert req.active is True
    assert req.workers == 4
    assert req.reason is None

    result = E.check_config(_uv(tmp_path), project=".")
    assert result.parallel == "4 workers (xdist, --dist load)"
    assert result.parallel_short == "4 workers"


def test_parallel_cov_in_runner_args_admitted_with_frozen_tuple(tmp_path):
    _write(tmp_path / "pyproject.toml",
           '[tool.pytest.ini_options]\naddopts = "-n 4"\n')
    _stub_venv(tmp_path, cov=_FROZEN_COV)

    req = E.parallel_request(_uv(tmp_path, full_args=("--cov",)), project=".")
    assert req.active is True
    assert req.reason is None


def test_parallel_cov_version_mismatch_falls_back(tmp_path):
    _write(tmp_path / "pyproject.toml",
           '[tool.pytest.ini_options]\naddopts = "-n 4 --cov"\n')
    _stub_venv(tmp_path, cov=("7.0.0", "7.16.1"))

    req = E.parallel_request(_uv(tmp_path), project=".")
    assert req.active is True
    assert req.reason == (
        "pytest-cov 7.0.0/coverage 7.16.1 is outside the frozen qualification "
        "tuple (ptest supports pytest-cov 7.1.0 with coverage 7.15.0); "
        "ptest runs serially")
    assert req.config_level is False
    assert req.runs is True


def test_parallel_cov_passes_through_to_later_config_rows(tmp_path):
    _write(tmp_path / "pyproject.toml",
           '[tool.pytest.ini_options]\naddopts = "--cov --maxprocesses=2 -n 4"\n')
    _stub_venv(tmp_path, cov=_FROZEN_COV)

    req = E.parallel_request(_uv(tmp_path), project=".")
    assert req.reason == "--maxprocesses is not supported; ptest runs serially"
    assert req.config_level is True


def test_parallel_duplicate_dist_info_falls_back(tmp_path):
    _write(tmp_path / "pyproject.toml",
           '[tool.pytest.ini_options]\naddopts = "-n 4"\n')
    _stub_venv(tmp_path, extra=("pytest_xdist-3.8.0-2.dist-info",))

    result = E.check_config(_uv(tmp_path), project=".")

    assert result.parallel == (
        "no — more than one pytest-xdist install in the project environment; "
        "ptest runs serially")


def test_parallel_unqualified_version_falls_back(tmp_path):
    _write(tmp_path / "pyproject.toml",
           '[tool.pytest.ini_options]\naddopts = "-n 4"\n')
    _stub_venv(tmp_path, version="4.0.0")

    result = E.check_config(_uv(tmp_path), project=".")

    assert result.parallel == (
        "no — pytest-xdist 4.0.0 is not qualified (ptest supports 3.8.0); "
        "ptest runs serially")


def test_parallel_unverifiable_launcher_falls_back(tmp_path):
    _write(tmp_path / "pyproject.toml",
           '[tool.pytest.ini_options]\naddopts = "-n 4"\n')

    result = E.check_config(_config(tmp_path), project=".")

    assert result.parallel == (
        "no — ptest cannot verify pytest-xdist for launcher python; "
        "use an absolute interpreter or a uv launcher to run in parallel")


def test_parallel_ptest_n0_row_with_fix(tmp_path):
    _write(tmp_path / "pyproject.toml",
           '[tool.pytest.ini_options]\naddopts = "-n 4 --dist=loadgroup"\n')
    _stub_venv(tmp_path)

    result = E.check_config(_uv(tmp_path, args=("-n", "0")), project=".")

    assert result.parallel == "no — .ptest.toml sets -n 0"
    assert result.parallel_short == "no"
    assert result.parallel_fix == (
        'remove "-n", "0" from [runner] args in .ptest.toml to run 4 workers')


def test_parallel_ptest_n0_row_auto_fix(tmp_path):
    _write(tmp_path / "pyproject.toml",
           '[tool.pytest.ini_options]\naddopts = "-n auto"\n')
    _stub_venv(tmp_path)

    result = E.check_config(_uv(tmp_path, args=("-n", "0")), project="api")

    assert result.parallel == "no — api/.ptest.toml sets -n 0"
    assert result.parallel_fix == (
        'remove "-n", "0" from [runner] args in api/.ptest.toml to run in parallel')


@pytest.mark.parametrize("addopts,reason", [
    ("--tx popen --dist=each --cov --maxprocesses=2 -n 4",
     "remote xdist workers (--tx, --rsyncdir, --px) are not supported"),
    ("--dist=each --cov --maxprocesses=2 -n 4",
     "--dist each is not supported; ptest runs serially"),
    ("--cov --maxprocesses=2 -n 4",
     "pytest-cov is not installed in the project environment yet; "
     "ptest runs serially until setup installs it"),
    ("--maxprocesses=2 -n 4", "--maxprocesses is not supported; ptest runs serially"),
    ("-n 1 --dist=load", "your pytest config asks for 1 worker"),
])
def test_parallel_fallback_precedence(tmp_path, addopts, reason):
    _write(tmp_path / "pyproject.toml",
           "[tool.pytest.ini_options]\naddopts = \"%s\"\n" % addopts)

    req = E.parallel_request(_uv(tmp_path), project=".")

    assert req.reason == reason


def test_xdist_environment_version_shapes(tmp_path):
    version, problem = E.xdist_environment_version(_uv(tmp_path))
    assert version is None
    assert problem == ("pytest-xdist is not installed in the project environment yet; "
                       "ptest runs serially until setup installs it")

    _stub_venv(tmp_path)
    assert E.xdist_environment_version(_uv(tmp_path)) == ("3.8.0", None)

    version, problem = E.xdist_environment_version(_config(tmp_path))
    assert version is None and "launcher python" in problem


def test_absolute_interpreter_requires_pyvenv_cfg(tmp_path):
    venv = tmp_path / ".venv"
    (venv / "bin").mkdir(parents=True)
    launcher = (str(venv / "bin" / "python"),)
    _write(tmp_path / "pyproject.toml",
           '[tool.pytest.ini_options]\naddopts = "-n 4"\n')

    _, problem = E.xdist_environment_version(_config(tmp_path, launcher=launcher))
    assert problem is not None and "launcher python" in problem

    (venv / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
    site = venv / "lib" / "python3.12" / "site-packages"
    site.mkdir(parents=True)
    (site / "pytest_xdist-3.8.0.dist-info").mkdir()
    assert E.xdist_environment_version(_config(tmp_path, launcher=launcher)) == ("3.8.0", None)


def test_persea_shape_parallel_and_full_suite(tmp_path):
    _write(tmp_path / "pyproject.toml",
           '[tool.pytest.ini_options]\n'
           'addopts = \'-n 4 --dist=loadgroup -m "not extended_migration"\'\n')
    _write(tmp_path / "tests" / "conftest.py",
           "def pytest_collection_modifyitems(items):\n    return None\n"
           "\n"
           "def pytest_sessionfinish(session, exitstatus):\n    return None\n")
    _stub_venv(tmp_path)

    result = E.check_config(_uv(tmp_path), project=".")

    assert result.parallel_short == "4 workers"
    assert result.parallel == "4 workers (xdist, --dist loadgroup)"
    assert result.full is True
    assert result.full_suite == (
        'your pytest config: -m "not extended_migration", conftest.py hooks')
    assert result.full_blocked is None
    assert result.verdict() == (
        "runs: yes; parallel: 4 workers (xdist, --dist loadgroup); "
        "full suite = your pytest config: "
        '-m "not extended_migration", conftest.py hooks')


def test_full_suite_quotes_values_with_whitespace(tmp_path):
    _write(tmp_path / "pyproject.toml",
           '[tool.pytest.ini_options]\naddopts = \'-m "not slow"\'\n')
    (tmp_path / "tests").mkdir()

    result = E.check_config(_config(tmp_path), project=".")

    assert result.full_suite == 'your pytest config: -m "not slow"'


def test_narrowing_parts_match_bridge_text():
    from ptest.runtime.pytest_bridge import full_narrowing_text

    cases = [
        ("-m", "not slow"), ("-k", "not slow"), ("--maxfail=3",),
        ("-n", "4", "--dist=loadgroup", "-m", "not slow"),
        ("-vx",), ("--maxfail", "3"),
    ]
    for tokens in cases:
        parts = E._narrowing_parts(tokens)
        rendered = "; ".join(
            option if value is None else f"{option} {value}"
            for option, value in parts)
        assert rendered == full_narrowing_text(tokens)


def test_full_blocked_is_first_unavailable_suffix(tmp_path):
    _write(tmp_path / "pyproject.toml",
           '[tool.pytest.ini_options]\naddopts = "-c other.ini"\n')

    result = E.check_config(_config(tmp_path, test_roots=(".",)), project=".")

    assert result.full is False
    assert result.full_blocked == 'test_roots is "."'
    assert result.full_suite is None


def test_setup_field_and_caveat_line(tmp_path):
    setup = C.SetupConfig(argv=("uv", "sync", "--locked"),
                          required_paths=("tests",),
                          network=False, lifecycle_scripts=False)
    _write(tmp_path / "tests" / "test_a.py", "def test_a():\n    assert True\n")

    result = E.check_config(_config(tmp_path, setup=setup), project=".")

    assert result.setup == "uv sync --locked"
    assert result.caveats == (
        "parallel: no — xdist is not enabled in your pytest config",
        "setup: uv sync --locked (ptest runs it when needed)")
    assert result.verdict() == (
        "runs: yes; parallel: no — xdist is not enabled in your pytest config; "
        "setup: uv sync --locked (ptest runs it when needed)")


@pytest.mark.parametrize("name,text", [
    ("pytest.ini", "[pytest]\naddopts = -n 4\n"),
    ("tox.ini", "[tox:tox]\nskipsdist = true\n[pytest]\naddopts = -n 4\n"),
    ("setup.cfg", "[metadata]\nname = demo\n[tool:pytest]\naddopts = -n 4\n"),
])
def test_addopts_source_names_ini_family_file(tmp_path, name, text):
    """addopts_source reports the deciding INI-family file (DET3)."""
    _write(tmp_path / name, text)
    assert E.addopts_source(tmp_path) == name
