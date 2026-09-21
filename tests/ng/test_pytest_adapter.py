"""Behavioral contract for the local pytest preparation profile."""
from __future__ import annotations

from pathlib import Path
import json
from dataclasses import replace
from types import SimpleNamespace
import sys
import shutil
import tomllib
import runpy

import pytest

from ptest import contracts as C
from ptest.adapters.pytest import inspect_capability, prepare
from ptest.runtime import pytest_bridge


@pytest.fixture(autouse=True)
def _isolate_bridge_environment(monkeypatch):
    for name in ("PTEST_BRIDGE_PROTOCOL", "PTEST_GRANT_WORKERS", "PTEST_EXECUTION", "PTEST_TEST_ROOTS",
                 "PTEST_RUN_ID", "PTEST_GRANT_NONCE",
                 "PTEST_PYTEST_REPORT_PATH", "PTEST_PYTEST_ATTEMPT",
                 "PTEST_PYTEST_EXECUTION", "PTEST_PYTEST_PROFILE",
                 "PTEST_PYTEST_CHECKOUT_ROOT", "PTEST_PYTEST_CONFIG_PATH"):
        monkeypatch.delenv(name, raising=False)


def _config(*, workers: int = 1, args: tuple[str, ...] = (),
            runner: C.RunnerConfig | None = None) -> C.Config:
    return C.Config(
        runner=runner or C.RunnerConfig(
            kind=C.RunnerKind.PYTEST,
            launcher=("python",), args=args, full_args=("-q",),
            test_roots=("tests",), workers=workers,
        ),
        setup=None,
        resources=C.ResourceConfig(),
        selection=C.SelectionPolicy(enabled=False, closed_inputs=False),
        project_id="ab" * 16,
        config_path=Path("/project/.ptest.toml"),
    )


def _plan(*, execution: str = "full", files: tuple[str, ...] = ()) -> C.Plan:
    mode = C.Mode.SCOPED if execution == "scoped" else C.Mode.FULL
    return C.Plan(mode=mode, execution=execution, files=files)


def _grant(slots: int = 1) -> C.Grant:
    return C.Grant(
        run_id="cd" * 16, nonce="ef" * 32, slots=slots,
        memory_estimate_mb=None, reserved_memory_mb=None, generation=1,
        domain_id="01" * 16,
    )


def _attempt(workers: int = 1) -> C.AttemptIdentity:
    return C.AttemptIdentity(
        run_id="cd" * 16, attempt_id="a001", resource_prefix="ptest_a001",
        worker_count=workers,
    )


def test_serial_preserves_literal_configured_argv():
    prepared = prepare(_config(workers=1), _plan(), _grant(1), _attempt(1))

    assert "-n" not in prepared.argv
    assert "no:xdist" not in prepared.argv
    assert prepared.summary.workers == 1
    assert prepared.argv[0] == "python"
    assert prepared.argv[-2:] == ("-q", "tests")
    assert prepared.argv[1].endswith("runtime/pytest_bridge.py")


def test_scoped_plan_preserves_literal_files_without_full_args():
    prepared = prepare(
        _config(workers=1, args=("-k", "quoted name [x]")),
        _plan(execution="scoped", files=("tests/test_unit.py",)),
        _grant(1), _attempt(1),
    )

    assert prepared.argv[2:] == ("-k", "quoted name [x]", "tests/test_unit.py")


@pytest.mark.parametrize("unsafe", [
    ("-n", "1"), ("--numprocesses=2",), ("--tx", "popen//python"),
    ("--px",), ("--rsyncdir", "src"), ("-f",), ("--looponfail",),
    ("-d",), ("--distload",),
])
def test_runner_parallel_or_remote_controls_are_rejected_before_bridge(unsafe):
    with pytest.raises(C.Problem, match="native-config-invalid"):
        prepare(_config(args=unsafe), _plan(), _grant(1), _attempt(1))


def test_grant_and_attempt_worker_mismatch_is_rejected():
    with pytest.raises(C.Problem, match="admission-invalid"):
        prepare(_config(workers=2), _plan(), _grant(2), _attempt(1))


def test_non_interpreter_launcher_is_rejected_before_bridge_execution():
    runner = C.RunnerConfig(
        kind=C.RunnerKind.PYTEST, launcher=("pytest",), test_roots=("tests",),
    )
    with pytest.raises(C.Problem, match="native-config-invalid"):
        prepare(_config(runner=runner), _plan(), _grant(1), _attempt(1))


def test_parallel_grant_is_refused_for_basic_serial_profile():
    with pytest.raises(C.Problem, match="admission-invalid"):
        prepare(_config(workers=2), _plan(), _grant(2), _attempt(2))


def test_prepared_bridge_receives_the_generated_protocol_descriptor():
    prepared = prepare(_config(), _plan(), _grant(1), _attempt(1))
    env = dict(prepared.env_updates)

    descriptor = json.loads(Path(env["PTEST_BRIDGE_PROTOCOL"]).read_text())
    assert descriptor["protocol"] == 1
    assert prepared.argv[1].endswith("runtime/pytest_bridge.py")


def test_bridge_rejects_an_unsupported_interpreter_before_import(monkeypatch):
    monkeypatch.setattr(pytest_bridge.sys, "version_info", (3, 10, 0))

    with pytest.raises(RuntimeError, match="unsupported CPython version"):
        pytest_bridge._python_version()


@pytest.mark.parametrize("workers", ["", "0", "65", "one"])
def test_bridge_rejects_missing_or_out_of_range_grant_workers(monkeypatch, workers):
    monkeypatch.setenv("PTEST_GRANT_WORKERS", workers)

    with pytest.raises(RuntimeError, match="invalid granted worker count"):
        pytest_bridge._workers()


def test_lower_grant_is_the_worker_authority():
    slots = 1
    prepared = prepare(_config(workers=4), _plan(), _grant(slots), _attempt(slots))
    assert prepared.summary.workers == slots
    assert "-n" not in prepared.argv


def test_grant_run_identity_must_match_attempt():
    with pytest.raises(C.Problem, match="admission-invalid"):
        prepare(_config(), _plan(), _grant(), replace(_attempt(), run_id="12" * 16))


def test_full_preparation_advertises_basic_serial_with_claim_limits():
    slots = 1
    prepared = prepare(_config(workers=slots), _plan(), _grant(slots), _attempt(slots))
    assert prepared.capability.execution is C.ExecutionTier.BASIC_SERIAL
    assert prepared.capability.selection is False
    assert any("inventory" in reason.message for reason in prepared.capability.limitations)
    assert "PTEST_GRANT_NONCE" not in dict(prepared.env_updates)


@pytest.mark.parametrize("args", [("-n", "2"), ("--tx", "popen//python"), ("@native-args.txt",)])
def test_static_where_marks_shared_controls_unavailable(args):
    capability = inspect_capability(_config(args=args))

    assert capability.execution is C.ExecutionTier.UNAVAILABLE
    assert any("shared" in item.message or "parallel" in item.message
               or "native controls" in item.message for item in capability.limitations)


def test_static_where_keeps_scoped_basic_serial_for_full_only_controls():
    capability = inspect_capability(_config(args=("-k", "slow")))

    assert capability.execution is C.ExecutionTier.BASIC_SERIAL
    assert any("full execution" in item.message or "native controls" in item.message
               for item in capability.limitations)


def test_unqualified_preparation_refuses_selected_plan():
    with pytest.raises(C.Problem, match="unsupported-capability"):
        prepare(_config(), _plan(execution="selected", files=("tests/a.py",)), _grant(), _attempt())


@pytest.mark.parametrize("launcher", [
    ("sh", "-c", "echo unsafe", "python"), ("uv", "run", "python"),
    ("uv", "run", "--locked", "--project", "/project/fixture", "python"),
    ("python3.11-config",), ("python3.10",), ("python3.15",),
    ("python", "-c", "python"), ("wrapper", "python"),
])
def test_launcher_cannot_smuggle_an_arbitrary_command_prefix(launcher):
    with pytest.raises(C.Problem, match="native-config-invalid"):
        prepare(_config(runner=replace(_config().runner, launcher=launcher)), _plan(), _grant(), _attempt())


@pytest.mark.parametrize("launcher", [
    ("python3.11",), ("/project/.venv/bin/python",),
    ("uv", "run", "--locked", "--no-sync", "python"),
    ("uv", "run", "--locked", "--no-sync", "--project", "/project/fixture", "python"),
])
def test_supported_interpreter_launchers_preserve_literal_prefix(launcher):
    prepared = prepare(_config(runner=replace(_config().runner, launcher=launcher)), _plan(), _grant(), _attempt())
    assert prepared.argv[:len(launcher)] == launcher


@pytest.mark.parametrize("unsafe", [("-qn4",), ("-xn2",), ("@args.txt",), ("--tx=popen",)])
@pytest.mark.parametrize("source", ["args", "files", "full_args"])
def test_native_controls_cannot_bypass_preparation_through_other_sources(unsafe, source):
    runner = replace(_config().runner, **{source: unsafe}) if source != "files" else _config().runner
    plan = _plan(execution="scoped", files=unsafe) if source == "files" else _plan()
    with pytest.raises(C.Problem, match="native-config-invalid"):
        prepare(_config(runner=runner), plan, _grant(), _attempt())


@pytest.mark.parametrize("args", [
    ("-k", "one"), ("-m", "slow"), ("-qkone",), ("--deselect=tests/a.py::test_a",),
    ("--lf",), ("--last-failed",), ("--ff",), ("--sw",), ("tests/a.py::test_a",),
    ("--ignore=tests/a.py",), ("--collect-only",), ("-x",),
    ("--setup-only",), ("--setup-plan",), ("--fixtures",),
    ("--fixtures-per-test",), ("--markers",), ("--cache-show",),
    ("-h",), ("--help",), ("-V",), ("--version",),
])
def test_full_preparation_refuses_narrowing(args):
    with pytest.raises(C.Problem, match="native-config-invalid"):
        prepare(_config(args=args), _plan(), _grant(), _attempt())


def test_full_preparation_allows_explicit_zero_maxfail():
    prepared = prepare(_config(args=("--maxfail", "0")), _plan(), _grant(), _attempt())

    assert prepared.argv[2:4] == ("--maxfail", "0")


@pytest.mark.parametrize("test_root", ["-V", "--version", "--co", "@args.txt"])
def test_full_preparation_refuses_control_test_roots(test_root):
    runner = replace(_config().runner, test_roots=(test_root,))

    with pytest.raises(C.Problem, match="native-config-invalid") as refused:
        prepare(_config(runner=runner), _plan(), _grant(), _attempt())

    assert refused.value.phase == "execution"


@pytest.mark.parametrize("test_roots", [
    ("tests",),
    ("tests/unit", "integration"),
])
def test_full_preparation_preserves_ordinary_test_roots(test_roots):
    runner = replace(_config().runner, test_roots=test_roots)

    prepared = prepare(_config(runner=runner), _plan(), _grant(), _attempt())

    assert prepared.argv[-len(test_roots):] == test_roots
    assert json.loads(dict(prepared.env_updates)["PTEST_TEST_ROOTS"]) == list(test_roots)


@pytest.fixture
def bridge_env(monkeypatch):
    descriptor = Path(pytest_bridge.__file__).with_name("protocol-v1.json")
    monkeypatch.setenv("PTEST_BRIDGE_PROTOCOL", str(descriptor))
    monkeypatch.setenv("PTEST_GRANT_WORKERS", "1")
    monkeypatch.setenv("PTEST_EXECUTION", "full")
    monkeypatch.setenv("PTEST_TEST_ROOTS", '["tests"]')


def _native_config(**options):
    defaults = dict(numprocesses=None, maxprocesses=None, tx=[], px=[], rsyncdir=[],
                    keyword="", markexpr="", deselect=[], lf=False, failedfirst=False,
                    stepwise=False, testmon=False, maxfail=0, collectonly=False,
                    ignore=[], ignore_glob=[], pyargs=False, looponfail=False,
                    setuponly=False, setupplan=False, showfixtures=False,
                    show_fixtures_per_test=False, markers=False, cacheshow=False,
                    help=False, version=False)
    defaults.update(options)
    return SimpleNamespace(option=SimpleNamespace(**defaults), args=["tests"],
                           getini=lambda name: [], invocation_params=SimpleNamespace(args=()))


def test_loaded_xdist_is_refused_even_when_native_options_are_inactive():
    plugin = object()
    manager = SimpleNamespace(
        list_name_plugin=lambda: (("xdist", plugin),),
        hook=SimpleNamespace(**{
            name: SimpleNamespace(get_hookimpls=lambda: [])
            for name in ("pytest_cmdline_main", "pytest_collection", "pytest_runtestloop",
                         "pytest_runtest_protocol", "pytest_runtest_call", "pytest_pyfunc_call",
                         "pytest_collection_modifyitems", "pytest_ignore_collect", "pytest_runtest_makereport",
                         "pytest_report_teststatus", "pytest_sessionfinish")
        }),
    )
    config = _native_config()
    config.pluginmanager = manager
    with pytest.raises(pytest.UsageError, match="xdist"):
        pytest_bridge.OwnedPlugin(1).pytest_configure(config)


@pytest.mark.parametrize("options", [
    {"numprocesses": 2, "tx": ["popen", "popen"], "maxprocesses": 3},
    {"numprocesses": 2, "tx": ["popen"]},
    {"numprocesses": 2, "tx": ["ssh=host", "popen"]},
    {"numprocesses": 2, "tx": ["popen", "popen"], "px": ["popen"]},
])
def test_effective_xdist_transport_and_capacity_are_bounded(options):
    with pytest.raises(pytest.UsageError, match="native-config-invalid"):
        pytest_bridge.OwnedPlugin(2).pytest_configure(_native_config(**options))


def test_serial_effective_parallel_control_is_a_usage_error():
    with pytest.raises(pytest.UsageError, match="native-config-invalid"):
        pytest_bridge.OwnedPlugin(1).pytest_configure(_native_config(numprocesses=4))


@pytest.mark.parametrize("option", [{"tx": ["popen"]}, {"px": ["popen"]}, {"rsyncdir": ["src"]}])
def test_explicit_transport_is_rejected_before_xdist_can_rewrite_it(option):
    plugin = pytest_bridge.OwnedPlugin(2)
    with pytest.raises(pytest.UsageError, match="native-config-invalid"):
        next(plugin.pytest_cmdline_main(_native_config(numprocesses=2, **option)))


def test_owned_wrapper_allows_xdist_generated_transports_and_preserves_native_result():
    config = _native_config(numprocesses=2)
    hook = pytest_bridge.OwnedPlugin(2).pytest_cmdline_main(config)
    next(hook)
    config.option.tx = ["popen", "popen"]
    with pytest.raises(StopIteration) as done:
        hook.send(5)
    assert done.value.value == 5


@pytest.mark.parametrize("options", [{"keyword": "one"}, {"markexpr": "slow"},
    {"deselect": ["tests/a.py::test_a"]}, {"lf": True}, {"failedfirst": True},
    {"stepwise": True}, {"testmon": True}, {"ignore": ["tests/a.py"]},
    {"maxfail": 1}, {"collectonly": True}, {"setuponly": True},
    {"setupplan": True}, {"showfixtures": True},
    {"show_fixtures_per_test": True}, {"markers": True}, {"cacheshow": True},
    {"help": True}, {"version": True}])
def test_full_bridge_rejects_effective_native_narrowing(bridge_env, options):
    with pytest.raises(pytest.UsageError, match="native-config-invalid"):
        next(pytest_bridge.OwnedPlugin(1).pytest_cmdline_main(_native_config(**options)))


def _install_fake_pytest(monkeypatch, main, version="9.1.1"):
    # This boundary replaces native execution; the bridge and owned hooks are real.
    fake = SimpleNamespace(__version__=version, main=main, UsageError=pytest.UsageError,
                           hookimpl=pytest.hookimpl)
    monkeypatch.setitem(sys.modules, "pytest", fake)


def test_run_restores_project_imports_and_preserves_literal_argv_and_exit(bridge_env, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PTEST_EXECUTION", "scoped")
    monkeypatch.setattr(sys, "path", [str(Path(pytest_bridge.__file__).parent), *sys.path[1:]])
    def native_main(argv, plugins):
        assert sys.path[0] == str(tmp_path)
        assert argv == ["-k", "quoted [x];$HOME"]
        assert len(plugins) == 1 and isinstance(plugins[0], pytest_bridge.OwnedPlugin)
        marker = plugins[0].pytest_cmdline_main.pytest_impl
        assert marker["wrapper"] is True
        return 5
    _install_fake_pytest(monkeypatch, native_main)
    assert pytest_bridge.run(["-k", "quoted [x];$HOME"]) == 5


@pytest.mark.parametrize("version", ["8.4.2", "9.0.3", "9.1.0", "9.1.1"])
def test_run_accepts_only_enumerated_pytest_candidates(bridge_env, monkeypatch, version):
    _install_fake_pytest(monkeypatch, lambda *a, **kw: 1, version)
    assert pytest_bridge.run([]) == 1


@pytest.mark.parametrize("version", ["8.4.1", "9.0.4", "9.1.2", "10.0.0", "9.1.1.dev0"])
def test_run_rejects_unknown_pytest_before_native_execution(bridge_env, monkeypatch, version):
    _install_fake_pytest(monkeypatch, lambda *a, **kw: pytest.fail("native runner reached"), version)
    with pytest.raises(RuntimeError, match="unsupported-capability"):
        pytest_bridge.run([])


def test_run_rejects_pypy_before_importing_pytest(bridge_env, monkeypatch):
    monkeypatch.setattr(sys, "implementation", SimpleNamespace(name="pypy"))
    monkeypatch.setitem(sys.modules, "pytest", None)
    with pytest.raises(RuntimeError, match="unsupported-capability.*CPython"):
        pytest_bridge.run([])


def test_run_rejects_unsupported_python_before_importing_pytest(bridge_env, monkeypatch):
    monkeypatch.setattr(sys, "version_info", (3, 10, 0))
    monkeypatch.setitem(sys.modules, "pytest", None)
    with pytest.raises(RuntimeError, match="unsupported-capability.*CPython"):
        pytest_bridge.run([])


@pytest.mark.parametrize("descriptor", [None, "missing", "bad", "version"])
def test_run_rejects_bad_protocol_before_import(bridge_env, monkeypatch, tmp_path, descriptor):
    if descriptor is None:
        monkeypatch.delenv("PTEST_BRIDGE_PROTOCOL")
    else:
        path = tmp_path / descriptor
        if descriptor != "missing":
            path.write_text("invalid" if descriptor == "bad" else '{"protocol": 2}')
        monkeypatch.setenv("PTEST_BRIDGE_PROTOCOL", str(path))
    monkeypatch.setitem(sys.modules, "pytest", None)
    with pytest.raises(RuntimeError, match="protocol descriptor"):
        pytest_bridge.run([])


@pytest.mark.parametrize("argv", ["tests", [42], [None]])
def test_run_rejects_non_literal_argv_before_import(bridge_env, monkeypatch, argv):
    monkeypatch.setitem(sys.modules, "pytest", None)
    with pytest.raises(RuntimeError, match="string array"):
        pytest_bridge.run(argv)


def test_missing_pytest_is_a_controlled_bridge_refusal(bridge_env, monkeypatch):
    monkeypatch.setitem(sys.modules, "pytest", None)
    with pytest.raises(RuntimeError, match="pytest is unavailable"):
        pytest_bridge.run([])


@pytest.mark.parametrize("unsafe", ["-ln4", "-hn4", "-Vn4", "-fn4", "-dn4"])
def test_other_flag_only_clusters_cannot_hide_parallel_control(unsafe):
    with pytest.raises(C.Problem, match="native-config-invalid"):
        prepare(_config(args=(unsafe,)), _plan(), _grant(), _attempt())


@pytest.mark.parametrize("location", ["args", "full_args"])
@pytest.mark.parametrize("args", [("-qc", "alternate.ini"), ("-vc", "alternate.ini"),
                                   ("-sc", "alternate.ini")])
def test_full_preparation_rejects_combined_native_redirects(args, location):
    config = _config(args=args)
    if location == "full_args":
        config = replace(config, runner=replace(config.runner, args=(), full_args=args))
    with pytest.raises(C.Problem, match="native-config-invalid"):
        prepare(config, _plan(), _grant(), _attempt())


@pytest.mark.parametrize("warning", [("-W", "error::DeprecationWarning"),
                                      ("-Wignore::DeprecationWarning",)])
def test_full_preparation_does_not_treat_warning_filter_as_node_selection(warning):
    prepare(_config(args=warning),
            _plan(), _grant(), _attempt())


def test_preparation_exports_full_scope_for_effective_native_validation():
    env = dict(prepare(_config(), _plan(), _grant(), _attempt()).env_updates)
    assert env["PTEST_EXECUTION"] == "full"
    assert json.loads(env["PTEST_TEST_ROOTS"]) == ["tests"]


def test_full_bridge_rejects_positional_narrowing_from_native_config(bridge_env):
    config = _native_config()
    config.args = ["tests/test_a.py::test_one", "tests"]
    with pytest.raises(pytest.UsageError, match="inventory differs"):
        next(pytest_bridge.OwnedPlugin(1).pytest_cmdline_main(config))


def test_full_bridge_keeps_captured_roots_after_environment_mutation(bridge_env, monkeypatch):
    plugin = pytest_bridge.OwnedPlugin(1, "full", ("tests",))
    monkeypatch.setenv("PTEST_EXECUTION", "scoped")
    monkeypatch.setenv("PTEST_TEST_ROOTS", '["other"]')
    config = _native_config(); config.args = ["other"]
    with pytest.raises(pytest.UsageError, match="inventory differs"):
        plugin.pytest_configure(config)


@pytest.mark.parametrize("value", ["-k hidden", "-x", "--deselect=tests/test_a.py::test_x",
                                    "-c alternate.ini", "--config-file=alternate.ini",
                                    "--rootdir=/tmp/other", "-qc alternate.ini",
                                    "-vc alternate.ini", "-sc alternate.ini"])
def test_full_bridge_rejects_addopts_controls_from_environment(bridge_env, monkeypatch, value):
    monkeypatch.setenv("PYTEST_ADDOPTS", value)

    with pytest.raises(pytest.UsageError, match="full pytest plans"):
        next(pytest_bridge.OwnedPlugin(1).pytest_cmdline_main(_native_config()))


@pytest.mark.parametrize("value", [["-k", "hidden"], ["-x"], ["--deselect=tests/a.py::test_x"],
                                    ["-c", "alternate.ini"], ["--rootdir=/tmp/other"],
                                    ["-qc", "alternate.ini"], ["-vc", "alternate.ini"],
                                    ["-sc", "alternate.ini"]])
def test_full_bridge_rejects_addopts_controls_from_ini(bridge_env, value):
    config = _native_config()
    config.getini = lambda name: value if name == "addopts" else []

    with pytest.raises(pytest.UsageError, match="full pytest plans"):
        next(pytest_bridge.OwnedPlugin(1).pytest_cmdline_main(config))


@pytest.mark.parametrize("value", ["-W error::DeprecationWarning",
                                    "-Wignore::DeprecationWarning"])
def test_full_bridge_does_not_treat_warning_filter_as_node_selection(bridge_env, monkeypatch, value):
    monkeypatch.setenv("PYTEST_ADDOPTS", value)
    hook = pytest_bridge.OwnedPlugin(1).pytest_cmdline_main(_native_config())
    next(hook)


def test_full_bridge_requires_trusted_effective_root_paths(bridge_env, monkeypatch, tmp_path):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    monkeypatch.setenv("PTEST_PYTEST_CHECKOUT_ROOT", str(checkout))
    config = _native_config()
    config.option.rootdir = tmp_path / "outside"
    config.rootpath = checkout
    config.inipath = checkout / "pytest.ini"
    config.option.inifilename = "pytest.ini"

    with pytest.raises(pytest.UsageError, match="native configuration paths"):
        pytest_bridge.OwnedPlugin(1).pytest_configure(config)


def test_full_bridge_accepts_trusted_effective_root_paths(bridge_env, monkeypatch, tmp_path):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    monkeypatch.setenv("PTEST_PYTEST_CHECKOUT_ROOT", str(checkout))
    config = _native_config()
    config.rootpath = checkout
    config.inipath = checkout / "pytest.ini"
    config.option.rootdir = checkout
    config.option.inifilename = "pytest.ini"
    (checkout / "pytest.ini").write_text("[pytest]\n")

    pytest_bridge.OwnedPlugin(1).pytest_configure(config)


@pytest.mark.parametrize("hook", ["pytest_collection_modifyitems", "pytest_ignore_collect",
                                   "pytest_runtest_makereport", "pytest_report_teststatus",
                                   "pytest_sessionfinish"])
def test_full_bridge_refuses_wrapper_full_only_external_hooks(bridge_env, hook):
    function = SimpleNamespace(__module__="project.conftest")
    function.pytest_hookimpl = {"wrapper": True}
    implementation = SimpleNamespace(plugin=object(), function=function)
    manager = SimpleNamespace(
        list_name_plugin=lambda: (),
        hook=SimpleNamespace(**{name: SimpleNamespace(get_hookimpls=lambda name=name: [implementation] if name == hook else [])
                                for name in ("pytest_cmdline_main", "pytest_collection", "pytest_runtestloop",
                                             "pytest_runtest_protocol", "pytest_runtest_call", "pytest_pyfunc_call",
                                             "pytest_collection_modifyitems", "pytest_ignore_collect", "pytest_runtest_makereport",
                                             "pytest_report_teststatus", "pytest_sessionfinish")}),
    )
    config = _native_config(); config.pluginmanager = manager
    with pytest.raises(pytest.UsageError, match="execution hook"):
        pytest_bridge.OwnedPlugin(1).pytest_configure(config)


@pytest.mark.parametrize("hook", ["pytest_collection_modifyitems", "pytest_runtest_makereport",
                                   "pytest_sessionfinish"])
def test_full_bridge_refuses_aliased_and_late_full_only_hooks(bridge_env, hook):
    implementation = SimpleNamespace(
        plugin=object(),
        function=SimpleNamespace(__module__="project.alias_plugin"),
        specname=hook, opts={"tryfirst": True},
    )
    late_manager = SimpleNamespace(
        list_name_plugin=lambda: (("alias-name", object()),),
        hook=SimpleNamespace(**{name: SimpleNamespace(get_hookimpls=lambda name=name: [implementation] if name == hook else [])
                                for name in ("pytest_cmdline_main", "pytest_collection", "pytest_runtestloop",
                                             "pytest_runtest_protocol", "pytest_runtest_call", "pytest_pyfunc_call",
                                             "pytest_collection_modifyitems", "pytest_ignore_collect", "pytest_runtest_makereport",
                                             "pytest_report_teststatus", "pytest_sessionfinish")}),
    )
    config = _native_config(); config.pluginmanager = late_manager
    with pytest.raises(pytest.UsageError, match="execution hook"):
        pytest_bridge.OwnedPlugin(1).pytest_configure(config)


@pytest.mark.parametrize("hook", ["pytest_collection_modifyitems", "pytest_ignore_collect",
                                   "pytest_runtest_makereport", "pytest_report_teststatus",
                                   "pytest_sessionfinish"])
def test_full_bridge_refuses_full_only_external_hooks(bridge_env, hook):
    implementation = SimpleNamespace(plugin=object(), function=SimpleNamespace(__module__="project.conftest"))
    manager = SimpleNamespace(
        list_name_plugin=lambda: (),
        hook=SimpleNamespace(**{name: SimpleNamespace(get_hookimpls=lambda name=name: [implementation] if name == hook else [])
                                for name in ("pytest_cmdline_main", "pytest_collection", "pytest_runtestloop",
                                             "pytest_runtest_protocol", "pytest_runtest_call", "pytest_pyfunc_call",
                                             "pytest_collection_modifyitems", "pytest_ignore_collect", "pytest_runtest_makereport",
                                             "pytest_report_teststatus", "pytest_sessionfinish")}),
    )
    config = _native_config(); config.pluginmanager = manager
    with pytest.raises(pytest.UsageError, match="execution hook"):
        pytest_bridge.OwnedPlugin(1).pytest_configure(config)


def test_ini_rsync_is_rejected_before_gateway_setup():
    config = _native_config(numprocesses=2)
    config.getini = lambda name: ["src"] if name == "rsyncdirs" else []
    with pytest.raises(pytest.UsageError, match="rsync"):
        next(pytest_bridge.OwnedPlugin(2).pytest_cmdline_main(config))


class _RealShapedXSpec:
    """Minimal execnet.XSpec shape after xdist's NodeManager rewrite."""

    chdir = dont_write_bytecode = installvia = nice = python = None
    socket = ssh = ssh_config = vagrant_ssh = via = None

    def __init__(self, worker_id, **changes):
        self._spec = "execmodel=main_thread_only//popen"
        self.env = {}
        self.execmodel = "main_thread_only"
        self.popen = True
        self.id = worker_id
        self.__dict__.update(changes)

    def __str__(self):
        return self._spec


def test_final_gateway_accepts_real_shaped_local_xdist_specs_for_exact_grant():
    config = _native_config(numprocesses=2, tx=["popen", "popen"])
    plugin = pytest_bridge.OwnedPlugin(2)
    plugin.pytest_xdist_setupnodes(config, [_RealShapedXSpec("gw0"), _RealShapedXSpec("gw1")])

    with pytest.raises(pytest.UsageError, match="gateway specifications"):
        plugin.pytest_xdist_setupnodes(config, [_RealShapedXSpec("gw0")])


@pytest.mark.parametrize("changes", [
    {"_spec": "execmodel=main_thread_only//popen//python=/other/python",
     "python": "/other/python"},
    {"_spec": "execmodel=main_thread_only//popen//chdir=/tmp", "chdir": "/tmp"},
    {"_spec": "execmodel=main_thread_only//popen//ssh=host", "ssh": "host"},
    {"_spec": "execmodel=main_thread_only//popen//socket=host:1234",
     "socket": "host:1234"},
    {"_spec": "execmodel=main_thread_only//popen//via=proxy", "via": "proxy"},
    {"_spec": "execmodel=main_thread_only//popen//env:TOKEN=value",
     "env": {"TOKEN": "value"}},
    {"_spec": "execmodel=main_thread_only//popen//foreign_transport=value",
     "foreign_transport": "value"},
])
def test_final_gateway_rejects_dangerous_or_foreign_xspec_attributes(changes):
    config = _native_config(numprocesses=2, tx=["popen", "popen"])
    specs = [_RealShapedXSpec("gw0"), _RealShapedXSpec("gw1", **changes)]

    with pytest.raises(pytest.UsageError, match="gateway specifications"):
        pytest_bridge.OwnedPlugin(2).pytest_xdist_setupnodes(config, specs)


def test_native_refusal_emits_safe_machine_distinguishable_marker(capsys):
    with pytest.raises(pytest.UsageError):
        pytest_bridge.OwnedPlugin(1).pytest_configure(_native_config(numprocesses=4))
    stderr = capsys.readouterr().err
    prefix, payload = stderr.strip().split(": ", 1)
    assert prefix == "ptest-bridge-refusal"
    assert json.loads(payload) == {"code": "native-config-invalid", "message": "serial grant cannot use xdist"}


def test_script_entrypoint_returns_controlled_missing_pytest_error(bridge_env, monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "pytest", None)
    monkeypatch.setattr(sys, "argv", [pytest_bridge.__file__])
    with pytest.raises(SystemExit) as done:
        runpy.run_path(pytest_bridge.__file__, run_name="__main__")
    assert done.value.code == 4
    stderr = capsys.readouterr().err
    assert json.loads(stderr.split(": ", 1)[1]) == {
        "code": "native-config-invalid", "message": "pytest is unavailable in the selected interpreter"}


def test_native_runtime_error_is_not_relabelled_as_bridge_refusal(bridge_env, monkeypatch):
    def native_main(*a, **kw):
        raise RuntimeError("project hook failed")
    _install_fake_pytest(monkeypatch, native_main)
    with pytest.raises(RuntimeError, match="^project hook failed$"):
        pytest_bridge.run([])


def _native_project(case, version):
    """Prepare fixture files only; candidate ptest owns all dependency execution."""
    domain = case.domain(slots=1, jobs=2)
    root = case.project(domain, kind="pytest")
    project_id = tomllib.loads((root / ".ptest.toml").read_text())["project_id"]
    fixtures = Path(__file__).parent / "fixtures" / "pytest"
    for name in ("pyproject.toml", "uv.lock"):
        shutil.copyfile(fixtures / version / name, root / name)
    (root / "tests").mkdir()
    shutil.copyfile(fixtures / "suite.py.txt", root / "tests" / "test_native.py")
    (root / "project_module.py").write_text("VALUE = 7\n")
    (root / ".ptest.toml").write_text(
        f'version = 1\nproject_id = "{project_id}"\n'
        '[runner]\nkind = "pytest"\n'
        'launcher = ["uv", "run", "--locked", "--no-sync", "python"]\n'
        'args = ["-s"]\nfull_args = []\ntest_roots = ["tests"]\nworkers = 1\n'
        '[setup]\nargv = ["uv", "sync", "--locked", "--no-dev"]\n'
        'required_paths = [".venv/bin/python"]\nnetwork = true\nlifecycle_scripts = true\n'
    )
    return domain, root


@pytest.mark.parametrize("version", ["8.4.2", "9.0.3", "9.1.0", "9.1.1"])
def test_native_fixture_is_resolvable_with_isolated_locked_dependencies(case, version):
    from ptest.config import resolve_config
    domain, root = _native_project(case, version)
    config = resolve_config(root).config
    assert config.runner.launcher == ("uv", "run", "--locked", "--no-sync", "python")
    assert config.setup.argv == ("uv", "sync", "--locked", "--no-dev")
    assert config.setup.lifecycle_scripts is True
    assert not (root / ".venv").exists()
    packages = tomllib.loads((root / "uv.lock").read_text())["package"]
    assert [(p["name"], p["version"]) for p in packages if p["name"] == "pytest"] == [("pytest", version)]
    assert not any(p["name"] == "pytest-xdist" for p in packages)


@pytest.mark.parametrize("version", ["8.4.2", "9.0.3", "9.1.0", "9.1.1"])
@pytest.mark.parametrize("mode", [(), ("--full",), ("--", "tests")],
                         ids=["automatic", "full", "scoped-setup"])
def test_native_cli_deferred_modes_and_setup_refuse_before_dependencies(case, version, mode):
    # The fixture deliberately has an invalid machine budget and a setup block
    # that is not admitted by this local-only matrix.  Automatic mode refuses
    # before admission; explicit modes preserve the scheduler's own
    # invalid-config refusal and must never provision dependencies.
    domain, root = _native_project(case, version)
    result = case.invoke(domain, root, *mode, timeout=10)
    assert result.code == 2, result.stderr
    expected = b"unsupported-capability" if not mode else b"invalid-config"
    assert expected in result.stderr
    assert not (root / "tests-ran").exists()
    assert not (root / ".venv").exists()
    assert not domain.ledger.exists()
    assert result.result is None


@pytest.mark.parametrize("source,args", [
    ("env", ("-k", "missing")), ("ini", ("-k", "missing")),
    ("argv", ("-qn4",)), ("argv", ("--tx=popen",)),
    ("argv", ("--px=popen",)), ("argv", ("@native-args.txt",)),
])
def test_native_cli_deferred_full_cannot_bypass_refusal_with_native_controls(case, source, args):
    domain, root = _native_project(case, "9.1.1")
    env = {}
    if source == "ini":
        (root / "pytest.ini").write_text("[pytest]\naddopts = " + " ".join(args) + "\n")
    elif source == "env":
        env["PYTEST_ADDOPTS"] = " ".join(args)
    else:
        (root / "native-args.txt").write_text("-n\n4\n")
    native = ("--", *args) if source == "argv" else ()
    result = case.invoke(domain, root, "--full", *native, env=env, timeout=60)
    assert result.code == 2
    assert not (root / "tests-ran").exists()
    # This fixture deliberately advertises max_jobs=2 with max_slots=1, so
    # the scheduler's exact pre-admission result is invalid-config for every
    # source.  The native controls therefore never reach dependency setup;
    # argv still has its own parser regression coverage below this boundary.
    assert b"invalid-config" in result.stderr
    assert not (root / ".venv").exists()
    assert not domain.ledger.exists()
    assert b"INTERNALERROR" not in result.stderr


@pytest.mark.parametrize("overrides", [
    ["strict=true"],
    ["strict_config=true"],
    ["strict_markers=true"],
    ["strict=true", "strict_config=true", "strict_markers=true"],
])
def test_full_bridge_preserves_safe_strict_overrides(bridge_env, overrides):
    """Pytest 9 implements --strict* flags as override_ini entries; keep them."""
    hook = pytest_bridge.OwnedPlugin(1).pytest_cmdline_main(_native_config(override_ini=overrides))
    next(hook)


@pytest.mark.parametrize("overrides", [
    ["addopts=-k hidden"],
    ["cache_dir=/tmp/other"],
    ["strict_markers=yes"],
    ["strict=true", "cache_dir=/tmp/other"],
    ["unknown_ini=1"],
    ["strict=false"],
])
def test_full_bridge_rejects_arbitrary_override_ini(bridge_env, overrides):
    """Only the exact strict-true entries survive; every other -o value refuses."""
    with pytest.raises(pytest.UsageError, match="native-config-invalid"):
        next(pytest_bridge.OwnedPlugin(1).pytest_cmdline_main(_native_config(override_ini=overrides)))


def test_full_preparation_preserves_safe_strict_controls():
    """Static admission already allows the --strict* flag forms for full."""
    config = _config(args=("--strict-markers", "--strict-config", "--strict"))
    prepare(config, _plan(), _grant(), _attempt())
