"""Task 12c probe contracts: unsupported capabilities fail closed."""
from __future__ import annotations

import pytest

from ptest import contracts as C, config as config_api, operations
from ptest.cli import parse_argv


@pytest.mark.parametrize("timeout", ["1", "30", "31", "120"])
def test_probe_timeout_bounds_are_inclusive(timeout):
    parsed = parse_argv(("doctor", "--probe", "--scope", "tests",
                         "--attempt-timeout", timeout))
    assert parsed.probe.attempt_timeout_s == float(timeout)


@pytest.mark.parametrize("option, values", [
    ("--repeat", ("1", "5")),
    ("--workers", ("1", "64")),
])
def test_probe_repeat_and_worker_bounds_are_inclusive(option, values):
    for value in values:
        parsed = parse_argv(("doctor", "--probe", "--scope", "tests",
                             option, value))
        assert getattr(parsed.probe, {
            "--repeat": "repeat", "--workers": "workers"}[option]) == int(value)


@pytest.mark.parametrize("option, values", [
    ("--repeat", ("0", "6")),
    ("--workers", ("0", "65")),
])
def test_probe_repeat_and_worker_bounds_reject_out_of_range(option, values):
    for value in values:
        with pytest.raises(C.Problem) as error:
            parse_argv(("doctor", "--probe", "--scope", "tests",
                        option, value))
        assert error.value.code == "invalid-config"


@pytest.mark.parametrize("argv", [
    ("doctor", "--repeat", "2"),
    ("doctor", "--workers", "2"),
    ("doctor", "--attempt-timeout", "30"),
])
def test_probe_only_options_require_probe_even_at_defaults(argv):
    with pytest.raises(C.Problem) as error:
        parse_argv(argv)
    assert error.value.code == "invalid-config"


def test_probe_grammar_requires_scope_and_preserves_bounded_options():
    with pytest.raises(C.Problem, match="scope") as missing:
        parse_argv(("doctor", "--probe"))
    assert missing.value.code == "invalid-config"
    parsed = parse_argv(("doctor", "--attempt-timeout", "120", "--repeat", "5",
                         "--workers", "8", "--scope", "tests" , "--probe"))
    assert parsed.probe == C.ProbeOptions(scope="tests", repeat=5,
                                           workers=8, attempt_timeout_s=120)


@pytest.mark.parametrize("timeout", ["0", "121", "nan", "inf"])
def test_probe_invalid_timeout_is_rejected_before_admission(timeout):
    with pytest.raises(C.Problem) as error:
        parse_argv(("doctor", "--probe", "--scope", "tests",
                    "--attempt-timeout", timeout))
    assert error.value.code == "invalid-config"


def test_probe_unqualified_profile_never_launches(case, monkeypatch):
    domain = case.domain(slots=2, jobs=2)
    (case.base / "tests").mkdir()
    config = case.config(
        config_path=case.base / ".ptest.toml",
        resources=C.ResourceConfig(probe_isolation="run-worker-namespaced"),
    )
    launched = []
    monkeypatch.setattr(operations, "_probe_scope_path",
                        lambda *args, **kwargs: case.base / "tests")
    monkeypatch.setattr(operations.scheduler, "enqueue",
                        lambda *args, **kwargs: launched.append(True))
    with pytest.raises(C.Problem) as error:
        operations.execute(
            domain, config,
            C.RunRequest(mode=C.Mode.PROBE,
                         probe=C.ProbeOptions(scope="tests")),
        )
    assert error.value.code == "unsupported-capability"
    assert launched == []


def test_probe_requires_declared_worker_namespace_before_profile(case):
    domain = case.domain()
    (case.base / "tests").mkdir()
    config = case.config(config_path=case.base / ".ptest.toml")
    with pytest.raises(C.Problem) as error:
        operations.execute(
            domain, config,
            C.RunRequest(mode=C.Mode.PROBE,
                         probe=C.ProbeOptions(scope="tests")),
        )
    assert error.value.code == "probe-isolation-required"


def test_probe_unqualified_profile_precedes_capacity_check(case, monkeypatch):
    domain = case.domain()
    (case.base / "tests").mkdir()
    config = case.config(
        config_path=case.base / ".ptest.toml",
        resources=C.ResourceConfig(probe_isolation="run-worker-namespaced"),
    )
    monkeypatch.setattr(
        operations.scheduler, "effective_limits",
        lambda _domain: C.EffectiveLimits(max_slots=1, max_jobs=1),
    )
    launched = []
    monkeypatch.setattr(operations.scheduler, "enqueue",
                        lambda *args, **kwargs: launched.append(True))
    with pytest.raises(C.Problem) as error:
        operations.execute(
            domain, config,
            C.RunRequest(mode=C.Mode.PROBE,
                         probe=C.ProbeOptions(scope="tests")),
        )
    assert error.value.code == "unsupported-capability"
    assert launched == []


def test_probe_qualified_profile_then_checks_capacity(case, monkeypatch):
    domain = case.domain()
    (case.base / "tests").mkdir()
    config = case.config(
        config_path=case.base / ".ptest.toml",
        resources=C.ResourceConfig(probe_isolation="run-worker-namespaced"),
    )
    order = []

    class QualifiedAdapter:
        def qualified_profile(self, _config):
            order.append("profile")
            return {"runner": "pytest", "profile": "probe"}

        def compound_support(self, _config, *, qualified_profile=None):
            order.append("support")
            return C.CompoundSupport(
                selection=True, parallel_identity=True, profile="probe")

    monkeypatch.setattr(operations, "adapter_for", lambda _kind: QualifiedAdapter())
    monkeypatch.setattr(
        operations.scheduler, "effective_limits",
        lambda _domain: (order.append("capacity")
                         or C.EffectiveLimits(max_slots=1, max_jobs=1)),
    )
    with pytest.raises(C.Problem) as error:
        operations.execute(
            domain, config,
            C.RunRequest(mode=C.Mode.PROBE,
                         probe=C.ProbeOptions(scope="tests")),
        )
    assert error.value.code == "capacity-exceeded", order
    assert order == ["profile", "support", "capacity"]


def test_probe_scope_validation_precedes_setup_and_profile_refusal(case):
    domain = case.domain()
    config = case.config(
        config_path=case.base / ".ptest.toml",
        setup=C.SetupConfig(argv=("true",), required_paths=("required",),
                            network=False, lifecycle_scripts=False),
        resources=C.ResourceConfig(probe_isolation="run-worker-namespaced"),
    )
    with pytest.raises(C.Problem) as error:
        operations.execute(
            domain, config,
            C.RunRequest(mode=C.Mode.PROBE,
                         probe=C.ProbeOptions(scope="../outside")),
        )
    assert error.value.code == "unsafe-path"


def test_probe_scope_rejects_traversal_and_symlink(case):
    root = case.base
    (root / "tests").mkdir()
    outside = root / "outside"
    outside.write_text("local fixture", encoding="utf-8")
    (root / "tests" / "link").symlink_to(outside)
    config = case.config(config_path=root / ".ptest.toml")
    with pytest.raises(C.Problem) as traversal:
        operations._probe_scope_path(
            config, C.ProbeOptions(scope="../outside"))
    assert traversal.value.code == "unsafe-path"
    with pytest.raises(C.Problem) as symlink:
        operations._probe_scope_path(
            config, C.ProbeOptions(scope="tests/link"))
    assert symlink.value.code == "unsafe-path"
