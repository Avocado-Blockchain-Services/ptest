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
    (("-kfoo",), ("-kfoo",)),
    (("-c", "other.ini"), ("-c",)),
    (("tests/test_a.py::test_x",), ("tests/test_a.py::test_x",)),
    (("--collect-only",), ("--collect-only",)),
    (("--exitfirst",), ("--exitfirst",)),
    (("-q",), ()),
])
def test_narrowing_tokens_match_bridge_full_refusals(tokens, expected):
    assert E._narrowing_tokens(tokens) == expected


@pytest.mark.parametrize("tokens", [
    ("-rxXs",), ("-rsx",), ("-vrx",), ("-ra",), ("-rA",),
])
def test_report_char_clusters_are_not_narrowing(tokens):
    assert E._narrowing_tokens(tokens) == ()


def test_clustered_x_addopts_are_project_filtered_full(tmp_path):
    """Section F flips this twin: checked-in clusters are allowed and labelled."""
    _write(tmp_path / "pyproject.toml",
           '[tool.pytest.ini_options]\naddopts = "-vx"\n')
    _write(tmp_path / "tests" / "test_a.py", "def test_a():\n    assert True\n")

    result = E.check_config(_config(tmp_path), project=".")

    assert result.full is True
    assert result.caveats == ("full (project-filtered: -vx)",)
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


def test_dot_test_root_is_caveat_without_full(tmp_path):
    (tmp_path / "tests").mkdir()

    result = E.check_config(_config(tmp_path, test_roots=(".",)), project=".")

    assert result.status == E.STATUS_CAVEAT
    assert result.caveats == ('ptest --full unavailable: test_roots is "."',)
    assert result.full is False


def test_narrowing_addopts_are_project_filtered_full(tmp_path):
    """Section F flips this twin: checked-in -m is allowed and labelled."""
    _write(tmp_path / "pyproject.toml",
           '[tool.pytest.ini_options]\naddopts = \'-m "not slow"\'\n')
    (tmp_path / "tests").mkdir()

    result = E.check_config(_config(tmp_path), project=".")

    assert result.status == E.STATUS_CAVEAT
    assert result.caveats == (
        "full (project-filtered: -m not slow)",)
    assert result.full is True


def test_maxfail_nonzero_is_project_filtered_full(tmp_path):
    """Section F flips this twin: checked-in --maxfail is allowed and labelled."""
    _write(tmp_path / "pyproject.toml",
           '[tool.pytest.ini_options]\naddopts = "--maxfail=3"\n')
    (tmp_path / "tests").mkdir()

    result = E.check_config(_config(tmp_path), project=".")

    assert result.status == E.STATUS_CAVEAT
    assert result.caveats == (
        "full (project-filtered: --maxfail=3)",)
    assert result.full is True


def test_redirect_addopts_stay_unavailable(tmp_path):
    _write(tmp_path / "pyproject.toml",
           '[tool.pytest.ini_options]\naddopts = "-c other.ini"\n')
    (tmp_path / "tests").mkdir()

    result = E.check_config(_config(tmp_path), project=".")

    assert result.status == E.STATUS_CAVEAT
    assert result.caveats == (
        "ptest --full unavailable: pytest addopts redirect native configuration (-c)",)
    assert result.full is False


def test_collection_hook_is_project_filtered_full(tmp_path):
    """Section F: a conftest collection hook is allowed and labelled."""
    _write(tmp_path / "tests" / "conftest.py",
           "def pytest_collection_modifyitems(items):\n    return None\n")

    result = E.check_config(_config(tmp_path), project=".")

    assert result.status == E.STATUS_CAVEAT
    assert result.caveats == (
        "full (project-filtered: conftest collection hook)",)
    assert result.full is True


def test_persea_shaped_addopts_are_project_filtered_full_with_serial_caveat(tmp_path):
    """Persea api shape: xdist addopts plus a -m filter stay executable."""
    _write(tmp_path / "pyproject.toml",
           '[tool.pytest.ini_options]\n'
           'addopts = \'-p xdist.plugin -n 2 --dist=loadgroup -m "not slow"\'\n')
    (tmp_path / "tests").mkdir()

    result = E.check_config(_config(tmp_path, args=("-n", "0")), project=".")

    assert result.status == E.STATUS_CAVEAT
    assert result.caveats == (
        "serial: xdist disabled under ptest (-n 0)",
        "full (project-filtered: -m not slow)")
    assert result.full is True


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


def test_declared_setup_is_caveat_even_when_paths_present(tmp_path):
    setup = C.SetupConfig(argv=("uv", "sync", "--locked"),
                          required_paths=("tests",),
                          network=False, lifecycle_scripts=False)
    _write(tmp_path / "tests" / "test_a.py", "def test_a():\n    assert True\n")

    result = E.check_config(_config(tmp_path, setup=setup), project=".")

    assert result.status == E.STATUS_CAVEAT
    assert result.caveats == (
        "setup runs when required paths or its fingerprint are missing: "
        "uv sync --locked",)
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
        "exclusive: Vitest runs as one command and manages its own workers",
        "setup runs when required paths or its fingerprint are missing: npm ci")


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
