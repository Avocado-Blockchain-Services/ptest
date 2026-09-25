"""Configurable + dynamic compound run deadline (T1).

Covers config parsing/rendering of [runner] timeout/full_timeout, the CLI
--timeout flag and its precedence, the history-derived dynamic deadline with
its clamp, the guard's compound-expiry message, and the contextvar
propagation from execute() to _launch_guard.
"""
from __future__ import annotations

import json
import os
import select
import socket
import struct
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

from ptest import (
    cli as cli_api,
    contracts as C,
    guard as guard_api,
    history as history_api,
    operations,
    platform,
    scheduler,
)
from ptest.config import _serialize_fresh, resolve_config

_FIXTURES = Path(__file__).parent / "fixtures" / "processes"
_WATCHDOG_S = 45

EXPECTED_FRESH_TOML = (
    "version = 1\n"
    'project_id = "abababababababababababababababab"\n'
    "\n"
    "[runner]\n"
    'kind = "command"\n'
    'launcher = ["echo"]\n'
    'args = ["hello"]\n'
    "full_args = []\n"
    "test_roots = []\n"
    "workers = 1\n"
    'lifecycle = "cooperative-process-group"\n'
    "\n"
    "[resources]\n"
    "locks = []\n"
    "memory_mb_per_worker = 0\n"
    'probe_isolation = "undeclared"\n'
    "\n"
    "[selection]\n"
    "enabled = false\n"
    "closed_inputs = false\n"
    "input_roots = []\n"
    "ignored_inputs = []\n"
    "environment = []\n"
    "full_triggers = []\n"
    "always = []\n"
    "no_tests = []\n"
    "non_input_outputs = []\n"
    "full_ratio = 0.7\n"
)

EXPECTED_FRESH_TOML_WITH_TIMEOUTS = EXPECTED_FRESH_TOML.replace(
    'lifecycle = "cooperative-process-group"\n',
    'lifecycle = "cooperative-process-group"\n'
    "timeout = 120\n"
    "full_timeout = 300\n",
)


def _minimal_runner(**overrides):
    fields = {
        "kind": C.RunnerKind.COMMAND,
        "launcher": ("echo",),
        "args": ("hello",),
        "full_args": (),
        "test_roots": (),
        "workers": 1,
        "lifecycle": "cooperative-process-group",
    }
    fields.update(overrides)
    return C.RunnerConfig(**fields)


def _minimal_config(**runner_overrides):
    return C.Config(
        runner=_minimal_runner(**runner_overrides),
        setup=None,
        resources=C.ResourceConfig(),
        selection=C.SelectionPolicy(enabled=False, closed_inputs=False),
        project_id="ab" * 16,
    )


def _scoped_request(**overrides):
    fields = {"mode": C.Mode.SCOPED}
    fields.update(overrides)
    return C.RunRequest(**fields)


# --- contracts: constants and new fields ------------------------------------


def test_deadline_constants():
    assert C.DEFAULT_COMPOUND_TIMEOUT_S == 600.0
    assert C.MIN_COMPOUND_TIMEOUT_S == 60.0
    assert C.MAX_DYNAMIC_COMPOUND_TIMEOUT_S == 21600.0
    assert C.MAX_COMPOUND_TIMEOUT_S == 86400.0
    assert C.COMPOUND_TIMEOUT_SAFETY_FACTOR == 3.0
    assert C.COMPOUND_TIMEOUT_PER_TEST_S == 0.25


def test_runner_timeout_fields_default_none_and_accept_bounds():
    runner = _minimal_runner()
    assert runner.timeout_s is None
    assert runner.full_timeout_s is None
    assert _minimal_runner(timeout_s=1, full_timeout_s=86400).timeout_s == 1.0
    assert _minimal_runner(timeout_s=120.5).timeout_s == 120.5


@pytest.mark.parametrize("value", [0, -5, 86401, True, False, "120", float("nan"),
                                   float("inf"), [1]])
def test_runner_timeout_fields_reject_invalid(value):
    with pytest.raises((TypeError, ValueError)):
        _minimal_runner(timeout_s=value)
    with pytest.raises((TypeError, ValueError)):
        _minimal_runner(full_timeout_s=value)


def test_run_request_timeout_defaults_none_and_accepts_bounds():
    assert _scoped_request().timeout_s is None
    assert _scoped_request(timeout_s=86400).timeout_s == 86400.0


@pytest.mark.parametrize("value", [0, 86401, True, "60", float("nan")])
def test_run_request_timeout_rejects_invalid(value):
    with pytest.raises((TypeError, ValueError)):
        _scoped_request(timeout_s=value)


def test_runner_repr_omits_deadline_fields():
    rendered = repr(_minimal_runner(timeout_s=120, full_timeout_s=300))
    assert "timeout_s" not in rendered
    assert rendered == (
        "RunnerConfig(kind=<RunnerKind.COMMAND: 'command'>, "
        "launcher=('echo',), args=('hello',), full_args=(), test_roots=(), "
        "workers=1, lifecycle='cooperative-process-group')"
    )


# --- operations.resolve_compound_timeout ------------------------------------


def test_resolve_cli_overrides_everything():
    runner = _minimal_runner(timeout_s=200, full_timeout_s=300)
    request = C.RunRequest(mode=C.Mode.FULL, timeout_s=100)
    assert operations.resolve_compound_timeout(runner, request, (500.0, 4000)) == (100.0, "cli")


def test_resolve_full_timeout_wins_in_full_mode():
    runner = _minimal_runner(timeout_s=200, full_timeout_s=300)
    request = C.RunRequest(mode=C.Mode.FULL)
    assert operations.resolve_compound_timeout(runner, request, (500.0, 4000)) == (300.0, "config")


def test_resolve_full_timeout_ignored_outside_full_mode():
    runner = _minimal_runner(timeout_s=200, full_timeout_s=300)
    assert operations.resolve_compound_timeout(
        runner, _scoped_request(), (None, None)) == (200.0, "config")
    automatic = C.RunRequest(mode=C.Mode.AUTOMATIC)
    assert operations.resolve_compound_timeout(
        runner, automatic, (None, None)) == (200.0, "config")


def test_resolve_runner_timeout_beats_history():
    runner = _minimal_runner(timeout_s=200)
    assert operations.resolve_compound_timeout(
        runner, _scoped_request(), (500.0, 4000)) == (200.0, "config")


def test_resolve_history_combines_duration_and_count():
    runner = _minimal_runner()
    # max(200*3, 100*0.25) = 600 -> history
    assert operations.resolve_compound_timeout(
        runner, _scoped_request(), (200.0, 100)) == (600.0, "history")
    # count dominates: max(10*3, 4000*0.25) = 1000 -> history
    assert operations.resolve_compound_timeout(
        runner, _scoped_request(), (10.0, 4000)) == (1000.0, "history")
    # a 60k-test project escapes the old 600s cap
    assert operations.resolve_compound_timeout(
        runner, _scoped_request(), (100.0, 60000)) == (15000.0, "history")


def test_resolve_history_without_count_uses_duration_only():
    runner = _minimal_runner()
    assert operations.resolve_compound_timeout(
        runner, _scoped_request(), (200.0, None)) == (600.0, "history")


def test_resolve_history_clamps_to_floor_and_ceiling():
    runner = _minimal_runner()
    assert operations.resolve_compound_timeout(
        runner, _scoped_request(), (10.0, 5)) == (60.0, "history")
    assert operations.resolve_compound_timeout(
        runner, _scoped_request(), (100000.0, 100000)) == (21600.0, "history")


def test_resolve_empty_history_falls_back_to_default():
    runner = _minimal_runner()
    assert operations.resolve_compound_timeout(
        runner, _scoped_request(), (None, None)) == (600.0, "default")


# --- history.comparable_run_evidence ------------------------------------------


def _summary(mode, status, execution, collected):
    return {
        "mode": mode,
        "status": status,
        "timings": None if execution == "missing" else {"execution": execution},
        "counts": None if collected == "missing" else {"collected": collected},
    }


def test_comparable_evidence_prefers_latest_matching_full_run(monkeypatch):
    newest_first = [
        _summary("full", "passed", 100.0, 400),
        _summary("full", "failed", 200.0, 800),
    ]
    seen = {}

    def fake_reader(domain, checkout, limit=None):
        seen["limit"] = limit
        return tuple(newest_first)

    monkeypatch.setattr(history_api, "read_history_summaries", fake_reader)
    assert history_api.comparable_run_evidence(
        object(), object(), full=True) == (100.0, 400)
    assert seen["limit"] == 20


def test_comparable_evidence_skips_unusable_rows(monkeypatch):
    newest_first = [
        _summary("full", "cancelled", 100.0, 400),
        _summary("full", "passed", "fast", 400),
        _summary("full", "passed", None, 400),
        _summary("automatic", "passed", 90.0, 360),
        _summary("full", "failed", 70.0, 140),
    ]
    monkeypatch.setattr(history_api, "read_history_summaries",
                        lambda domain, checkout, limit=None: tuple(newest_first))
    assert history_api.comparable_run_evidence(
        object(), object(), full=True) == (70.0, 140)


def test_comparable_evidence_keeps_duration_when_counts_unusable(monkeypatch):
    newest_first = [
        _summary("full", "passed", 50.0, "many"),
        _summary("full", "passed", 60.0, "missing"),
    ]
    monkeypatch.setattr(history_api, "read_history_summaries",
                        lambda domain, checkout, limit=None: tuple(newest_first))
    assert history_api.comparable_run_evidence(
        object(), object(), full=True) == (50.0, None)


def test_comparable_evidence_non_full_prefers_family_then_falls_back(monkeypatch):
    newest_first = [
        _summary("scoped", "passed", 40.0, 160),
        _summary("full", "passed", 100.0, 400),
    ]
    monkeypatch.setattr(history_api, "read_history_summaries",
                        lambda domain, checkout, limit=None: tuple(newest_first))
    assert history_api.comparable_run_evidence(
        object(), object(), full=False) == (40.0, 160)

    monkeypatch.setattr(history_api, "read_history_summaries",
                        lambda domain, checkout, limit=None: tuple(newest_first[1:]))
    assert history_api.comparable_run_evidence(
        object(), object(), full=False) == (100.0, 400)


def test_comparable_evidence_empty_history_yields_nothing(monkeypatch):
    monkeypatch.setattr(history_api, "read_history_summaries",
                        lambda domain, checkout, limit=None: ())
    assert history_api.comparable_run_evidence(
        object(), object(), full=True) == (None, None)
    assert history_api.comparable_run_evidence(
        object(), object(), full=False) == (None, None)


def test_comparable_evidence_never_raises(monkeypatch):
    def raising_reader(domain, checkout, limit=None):
        raise OSError("store gone")

    monkeypatch.setattr(history_api, "read_history_summaries", raising_reader)
    assert history_api.comparable_run_evidence(
        object(), object(), full=True) == (None, None)
    monkeypatch.setattr(history_api, "read_history_summaries",
                        lambda domain, checkout, limit=None: ({"bogus": 1}, None, 7))
    assert history_api.comparable_run_evidence(
        object(), object(), full=False) == (None, None)


def test_comparable_evidence_empty_store_yields_nothing(case):
    domain = case.domain()
    checkout = case.checkout(domain)
    assert history_api.comparable_run_evidence(
        domain, checkout, full=True) == (None, None)


# --- config: [runner] timeout / full_timeout ----------------------------------


def _write_runner_config(root: Path, runner_extra: str = "") -> Path:
    path = root / ".ptest.toml"
    path.write_text(
        "version = 1\n"
        'project_id = "ab1234567890ab1234567890ab123456"\n'
        "\n[runner]\n"
        'kind = "command"\n'
        'launcher = ["echo"]\n'
        'args = ["hello"]\n'
        + runner_extra +
        "\n[resources]\n"
        "locks = []\n"
        "memory_mb_per_worker = 0\n"
        'probe_isolation = "undeclared"\n'
        "\n[selection]\n"
        "enabled = false\n"
        "closed_inputs = false\n"
        "input_roots = []\n"
        "ignored_inputs = []\n"
        "environment = []\n"
        "full_triggers = []\n"
        "always = []\n"
        "no_tests = []\n"
        "non_input_outputs = []\n"
        "full_ratio = 0.7\n",
        encoding="utf-8",
    )
    return path


def test_config_parses_runner_timeouts(tmp_path):
    _write_runner_config(tmp_path, 'timeout = 120\nfull_timeout = 300\n')
    resolution = resolve_config(tmp_path)
    assert resolution.problem is None
    assert resolution.config is not None
    assert resolution.config.runner.timeout_s == 120.0
    assert resolution.config.runner.full_timeout_s == 300.0


def test_config_without_timeouts_leaves_them_unset(tmp_path):
    _write_runner_config(tmp_path)
    resolution = resolve_config(tmp_path)
    assert resolution.problem is None
    assert resolution.config is not None
    assert resolution.config.runner.timeout_s is None
    assert resolution.config.runner.full_timeout_s is None


@pytest.mark.parametrize("key", ["timeout", "full_timeout"])
@pytest.mark.parametrize("value", ["0", "-5", "86401", "true", '"120"', "nan", "[1]"])
def test_config_rejects_invalid_runner_timeouts(tmp_path, key, value):
    _write_runner_config(tmp_path, f"{key} = {value}\n")
    resolution = resolve_config(tmp_path)
    assert resolution.config is None
    assert resolution.problem is not None
    assert resolution.problem.code == "invalid-config"


def test_config_renders_without_timeouts_byte_identically():
    assert _serialize_fresh(_minimal_config()).decode("utf-8") == EXPECTED_FRESH_TOML


def test_config_renders_timeouts_after_lifecycle_only_when_set():
    rendered = _serialize_fresh(
        _minimal_config(timeout_s=120, full_timeout_s=300)).decode("utf-8")
    assert rendered == EXPECTED_FRESH_TOML_WITH_TIMEOUTS
    partial = _serialize_fresh(_minimal_config(timeout_s=45.5)).decode("utf-8")
    assert "timeout = 45.5\n" in partial
    assert "full_timeout" not in partial


# --- CLI: --timeout ------------------------------------------------------------


def test_cli_timeout_defaults_unset():
    assert cli_api.parse_argv(()).timeout_s is None


def test_cli_timeout_parses_seconds():
    assert cli_api.parse_argv(("--timeout", "120")).timeout_s == 120.0
    assert cli_api.parse_argv(("--timeout", "1.5", "--", "-k", "slow")).timeout_s == 1.5


def test_cli_timeout_accepted_in_all_modes():
    assert cli_api.parse_argv(("--full", "--timeout", "5")).mode is C.Mode.FULL
    assert cli_api.parse_argv(("--full", "--timeout", "5")).timeout_s == 5.0
    changed = cli_api.parse_argv(("--changed", "--timeout", "5"))
    assert changed.changed is True
    assert changed.timeout_s == 5.0
    scoped = cli_api.parse_argv(("--timeout", "5", "tests/a.py"))
    assert scoped.mode is C.Mode.SCOPED
    assert scoped.timeout_s == 5.0


def test_cli_timeout_rejects_repeat():
    with pytest.raises(C.Problem) as exc:
        cli_api.parse_argv(("--timeout", "1", "--timeout", "2"))
    assert exc.value.code == "invalid-config"
    assert "option cannot be repeated" in exc.value.message


@pytest.mark.parametrize("value,code", [
    ("abc", "invalid-config"),
    ("", "invalid-config"),
    ("0", "invalid-bound"),
    ("0.5", "invalid-bound"),
    ("86401", "invalid-bound"),
    ("nan", "invalid-bound"),
    ("inf", "invalid-bound"),
])
def test_cli_timeout_rejects_bad_values(value, code):
    with pytest.raises(C.Problem) as exc:
        cli_api.parse_argv(("--timeout", value))
    assert exc.value.code == code


# --- guard: compound-expiry message -------------------------------------------


EXPECTED_COMPOUND_MESSAGE = (
    "compound execution deadline expired after 0s; raise it with "
    "--timeout SECONDS or [runner] timeout / full_timeout in .ptest.toml"
)


def _manifest_with_deadline(case, compound_timeout_s, attempt_timeout_s=None):
    domain = case.domain()
    root = domain.root / "deadline"
    root.mkdir()
    checkout = C.CheckoutIdentity(
        project_id="a" * 32, checkout_id=os.urandom(16).hex(), root=root)
    ticket = scheduler.enqueue(domain, C.AdmissionRequest(
        run_id=os.urandom(16).hex(), checkout=checkout,
        owner=platform.process_identity(os.getpid()), slots=1,
        exclusive=False, fixture=True, deadline=time.monotonic() + 60))
    grant = scheduler.poll(domain, ticket).grant
    marker = root / "marker"
    prepared = C.PreparedRun(
        argv=(sys.executable, "-c",
              f"from pathlib import Path; Path({str(marker)!r}).write_text('ran')"),
        cwd=root)
    return C.LaunchManifest(
        protocol=C.GUARD_PROTOCOL_VERSION,
        domain=domain, grant=grant, setup=None,
        attempts=(prepared,), attempt_ids=("a001",),
        setup_timeout_s=5, attempt_timeout_s=attempt_timeout_s,
        compound_timeout_s=compound_timeout_s)


def test_compound_limit_and_message_helpers(case):
    manifest = _manifest_with_deadline(case, 0.5)
    assert guard_api._compound_limit_s(manifest) == 0.5
    assert guard_api._compound_timeout_message(manifest) == EXPECTED_COMPOUND_MESSAGE
    assert guard_api._compound_limit_s(
        replace(manifest, compound_timeout_s=None)) == C.DEFAULT_COMPOUND_TIMEOUT_S


def _exact_frame(peer, manifest):
    result = bytearray()
    while len(result) < 4:
        chunk = peer.recv(4 - len(result))
        assert chunk, "unexpected EOF in fixture protocol"
        result.extend(chunk)
    (length,) = struct.unpack(">I", bytes(result))
    body = bytearray()
    while len(body) < length:
        chunk = peer.recv(length - len(body))
        assert chunk, "unexpected EOF in fixture protocol"
        body.extend(chunk)
    return C.decode_control_frame(bytes(result) + bytes(body),
                                  expected_nonce=manifest.grant.nonce)


class _DeadlineDriver:
    """Minimal guard_driver harness for the compound/attempt expiry messages."""

    def __init__(self, case, *, scope):
        manifest = _manifest_with_deadline(
            case, 0.5 if scope == "compound" else 20,
            attempt_timeout_s=0.5 if scope == "attempt" else None)
        self.manifest = manifest
        self.domain = manifest.domain
        self.root = self.domain.root / "deadline"
        self.control_guard, self.control = socket.socketpair()
        self.control.settimeout(_WATCHDOG_S)
        self.manifest_read, self.manifest_write = os.pipe()
        ready = self.domain.root / "deadliner"
        ready_listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        ready_listener.bind(str(ready))
        ready_listener.listen(8)
        ready_listener.settimeout(_WATCHDOG_S)
        self.ready_listener = ready_listener
        workload = C.PreparedRun(
            argv=(sys.executable, str(_FIXTURES / "guard_workload.py"),
                  "signal", str(ready), str(self.root / "marker")),
            cwd=self.root)
        self.manifest = replace(manifest, attempts=(workload,),
                                attempt_ids=("a001",))
        env = dict(os.environ,
                   GUARD_CONTROL_FD=str(self.control_guard.fileno()),
                   GUARD_MANIFEST_FD=str(self.manifest_read))
        self.process = subprocess.Popen(
            (sys.executable, str(_FIXTURES / "guard_driver.py")),
            cwd=self.root, env=env, start_new_session=True, close_fds=True,
            pass_fds=(self.control_guard.fileno(), self.manifest_read),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.control_guard.close()
        os.close(self.manifest_read)
        os.write(self.manifest_write, C.encode_launch_manifest(self.manifest))
        os.close(self.manifest_write)
        self.frames = []

    def read(self):
        frame = _exact_frame(self.control, self.manifest)
        self.frames.append(frame)
        if frame.kind == "attempt-ready":
            self.control.sendall(C.encode_control_frame(C.ControlFrame(
                protocol=C.GUARD_PROTOCOL_VERSION,
                run_id=self.manifest.grant.run_id,
                nonce=self.manifest.grant.nonce,
                kind="attempt-decision",
                payload={"attempt_id": frame.payload["attempt_id"],
                         "generation": frame.payload["generation"],
                         "gate_token": frame.payload["gate_token"],
                         "action": "continue", "reason": None})))
        return frame

    def finish(self):
        while self.control.fileno() >= 0:
            ready, _, _ = select.select([self.control], [], [], _WATCHDOG_S)
            assert ready, "guard control watchdog expired"
            if not self.control.recv(1, socket.MSG_PEEK):
                break
            self.read()
        self.process.wait(timeout=_WATCHDOG_S)
        facts = [frame.payload for frame in self.frames
                 if frame.kind == "runner-facts"]
        assert len(facts) == 1
        return facts[0]

    def close(self):
        for stream in (self.process.stdout, self.process.stderr):
            try:
                stream.close()
            except OSError:
                pass
        try:
            self.control.close()
        except OSError:
            pass
        try:
            self.ready_listener.close()
        except OSError:
            pass
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait(timeout=_WATCHDOG_S)


@pytest.mark.parametrize("scope", ["attempt", "compound"])
def test_real_expiry_message_names_scope_and_remedy(case, scope):
    driver = _DeadlineDriver(case, scope=scope)
    try:
        assert driver.read().kind == "registered"
        assert driver.read().kind == "attempt-ready"
        assert driver.read().kind == "phase"
        peer, _ = driver.ready_listener.accept()
        peer.settimeout(_WATCHDOG_S)
        facts = driver.finish()
    finally:
        driver.close()
    assert facts["problem"]["code"] == "execution-timeout"
    if scope == "compound":
        assert facts["problem"]["message"] == EXPECTED_COMPOUND_MESSAGE
    else:
        assert facts["problem"]["message"] == "attempt execution deadline expired"


# --- operations: resolved deadline reaches _launch_guard -----------------------


def test_execute_propagates_resolved_deadline_to_launch_guard(case, monkeypatch):
    domain = case.domain()
    root = case.project(domain, kind="command")
    from ptest.config import resolve_config as resolve
    config = resolve(root).config
    assert config is not None
    launch = operations._launch_guard
    seen = []
    manifests = []
    real_encode = C.encode_launch_manifest

    def capture_encode(manifest):
        manifests.append(manifest.compound_timeout_s)
        return real_encode(manifest)

    def spy(*args):
        seen.append(operations._COMPOUND_TIMEOUT_S.get())
        return launch(*args)

    monkeypatch.setattr(C, "encode_launch_manifest", capture_encode)
    monkeypatch.setattr(operations, "_launch_guard", spy)
    result = operations.execute(
        domain, config, C.RunRequest(mode=C.Mode.FULL, timeout_s=1234))
    assert result.status is C.Status.PASSED
    # The wrapper observed the resolved limit while execute() held it, and
    # the real guard baked that same value into the manifest it launched.
    assert seen == [1234.0]
    assert manifests == [1234.0]
    assert operations._COMPOUND_TIMEOUT_S.get() == C.DEFAULT_COMPOUND_TIMEOUT_S


def test_execute_full_gate_uses_full_family_evidence(case, monkeypatch):
    domain = case.domain()
    root = case.project(domain, kind="command")
    from ptest.config import resolve_config as resolve
    config = resolve(root).config
    assert config is not None
    wanted = {}

    def record_evidence(_domain, _checkout, *, full):
        wanted.setdefault("full", []).append(full)
        return (None, None)

    def fail(*args):
        raise OSError("injected launch failure")

    monkeypatch.setattr(history_api, "comparable_run_evidence", record_evidence)
    monkeypatch.setattr(operations, "_launch_guard", fail)
    # A bare AUTOMATIC command run executes the full gate even though the
    # request (and its history row) is recorded as AUTOMATIC: the deadline
    # must come from full-family evidence, never a scoped single-file row.
    operations.execute(domain, config, C.RunRequest(mode=C.Mode.AUTOMATIC))
    assert wanted["full"] == [True]
    # A scoped run keeps non-full evidence (with its fallback to full).
    operations.execute(domain, config, _scoped_request())
    assert wanted["full"] == [True, False]


def test_shadow_uses_full_family_evidence(case, monkeypatch):
    from types import SimpleNamespace

    from ptest import selection as selection_api

    domain = case.domain()
    root = case.project(domain, kind="command")
    project_id = root.joinpath(".ptest.toml").read_text().splitlines()[1]
    root.joinpath(".ptest.toml").write_text(
        "version = 1\n"
        f"{project_id}\n"
        "[runner]\n"
        'kind = "pytest"\n'
        'launcher = ["echo"]\n'
        'args = ["hello"]\n'
        "full_args = []\n"
        'test_roots = ["tests"]\n'
        "workers = 1\n"
        'lifecycle = "cooperative-process-group"\n',
        encoding="utf-8",
    )
    from ptest.config import resolve_config as resolve
    config = resolve(root).config
    assert config is not None
    assert config.runner.kind is C.RunnerKind.PYTEST
    wanted = {}

    def record_evidence(_domain, _checkout, *, full):
        wanted.setdefault("full", []).append(full)
        return (None, None)

    class _FakePytestAdapter:
        def qualified_profile(self, _config):
            return {"selection": True}

        def compound_support(self, _config, qualified_profile=None):
            return C.CompoundSupport(
                selection=True, parallel_identity=False,
                profile="fake", limitations=())

        def prepare_advanced(self, _config, _plan, _grant, _identity,
                             expected_runtime_identity=None):
            return C.PreparedRun(
                argv=("echo", "hello"), cwd=root, env_updates=())

    def fake_shadow_plans(_config, _snapshot, _history_view, _request, _support):
        return SimpleNamespace(
            selected=C.Plan(mode=C.Mode.AUTOMATIC, execution="selected",
                            files=("tests/test_a.py",)),
            full=C.Plan(mode=C.Mode.AUTOMATIC, execution="full"),
        )

    def fail(*args, **kwargs):
        raise OSError("injected launch failure")

    monkeypatch.setattr(history_api, "comparable_run_evidence", record_evidence)
    monkeypatch.setattr(operations, "adapter_for", lambda _kind: _FakePytestAdapter())
    monkeypatch.setattr(selection_api, "choose_shadow_plans", fake_shadow_plans)
    monkeypatch.setattr(operations, "_run_guard", fail)
    with pytest.raises(OSError, match="injected launch failure"):
        operations.execute(
            domain, config, C.RunRequest(mode=C.Mode.AUTOMATIC, shadow=True))
    # The shadow compound always runs the full-gate attempt a002, so its
    # deadline must come from full-family evidence.
    assert wanted["full"] == [True]
    assert operations._COMPOUND_TIMEOUT_S.get() == C.DEFAULT_COMPOUND_TIMEOUT_S


def test_execute_resets_deadline_after_launch_failure(case, monkeypatch):
    domain = case.domain()
    root = case.project(domain, kind="command")
    from ptest.config import resolve_config as resolve
    config = resolve(root).config
    assert config is not None

    def fail(*args):
        raise OSError("injected launch failure")

    monkeypatch.setattr(operations, "_launch_guard", fail)
    operations.execute(domain, config, C.RunRequest(mode=C.Mode.FULL))
    assert operations._COMPOUND_TIMEOUT_S.get() == C.DEFAULT_COMPOUND_TIMEOUT_S
