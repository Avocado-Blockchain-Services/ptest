"""Task 11D: scoped native pytest execution contracts."""
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import sys
import subprocess
import time
from types import SimpleNamespace

import pytest

from ptest import config as config_api, contracts as C, operations
from ptest.adapters.pytest import prepare
from ptest.runtime import pytest_bridge


def _grant(slots: int = 1) -> C.Grant:
    return C.Grant(
        run_id="12" * 16,
        nonce="34" * 32,
        slots=slots,
        memory_estimate_mb=None,
        reserved_memory_mb=None,
        generation=0,
        domain_id="56" * 16,
    )


def _attempt(workers: int = 1) -> C.AttemptIdentity:
    return C.AttemptIdentity(
        run_id="12" * 16,
        attempt_id="a001",
        resource_prefix="pt_checkout_run_a001_w000",
        worker_count=workers,
    )


def test_pytest_automatic_is_refused_before_admission(case, monkeypatch):
    domain = case.domain()
    config = case.config(runner_kind="pytest")
    request = C.RunRequest(mode=C.Mode.AUTOMATIC)

    def enqueue(*args, **kwargs):
        raise AssertionError("native pytest must be rejected before admission")

    monkeypatch.setattr(operations.scheduler, "enqueue", enqueue)

    with pytest.raises(C.Problem, match="unsupported-capability"):
        operations.execute(domain, config, request)


@pytest.mark.parametrize("mode", [C.Mode.AUTOMATIC])
def test_pytest_non_scoped_modes_are_refused_before_admission(case, monkeypatch, mode):
    domain = case.domain()
    config = case.config(runner_kind="pytest")

    monkeypatch.setattr(
        operations.scheduler,
        "enqueue",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("native pytest must be rejected before admission")),
    )

    with pytest.raises(C.Problem, match="unsupported-capability"):
        operations.execute(domain, config, C.RunRequest(mode=mode, workers=8))


def test_pytest_setup_is_admitted_before_scheduler(case, monkeypatch):
    domain = case.domain()
    setup = C.SetupConfig(
        argv=("uv", "sync"), required_paths=(".venv",),
        network=False, lifecycle_scripts=False,
    )
    config = case.config(
        runner_kind="pytest", setup=setup,
        config_path=case.base / ".ptest.toml",
    )
    monkeypatch.setattr(
        operations.scheduler,
        "enqueue",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("pytest setup reached scheduler admission")),
    )

    with pytest.raises(AssertionError, match="reached scheduler admission"):
        operations.execute(domain, config, C.RunRequest(mode=C.Mode.SCOPED))


def test_normal_domain_setup_bootstraps_account_coordinator(case, monkeypatch, tmp_path):
    """A real uv setup can follow normal-domain coordinator initialization."""
    home = tmp_path / "account"
    home.mkdir(mode=0o700)
    monkeypatch.setattr(operations.platform.pwd, "getpwuid",
                        lambda _uid: SimpleNamespace(pw_dir=str(home)))
    monkeypatch.setattr(operations.platform, "_filesystem_type",
                        lambda _path: "ext4")
    domain = operations.platform.domain_paths(None)
    root = home / "project"
    root.mkdir(mode=0o700)
    (root / "tests").mkdir(mode=0o700)
    (root / "tests" / "test_native.py").write_text(
        "def test_native():\n    assert True\n", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        "[project]\nname = 'normal-fixture'\nversion = '0.1.0'\n"
        "[dependency-groups]\ndev = ['pytest']\n"
        "[tool.pytest.ini_options]\n", encoding="utf-8")
    (root / ".gitignore").write_text(".venv/\n", encoding="utf-8")
    subprocess.run(("uv", "lock"), cwd=root, check=True,
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    subprocess.run(("git", "init"), cwd=root, check=True,
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    subprocess.run(("git", "config", "user.email", "fixture@example.test"),
                   cwd=root, check=True, stdout=subprocess.PIPE,
                   stderr=subprocess.PIPE)
    subprocess.run(("git", "config", "user.name", "Fixture"), cwd=root,
                   check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    config_text = (
        "version = 1\nproject_id = \"" + "ab" * 16 + "\"\n"
        "[runner]\nkind = \"pytest\"\nlauncher = [\"" + str(root / ".venv/bin/python") + "\"]\n"
        "args = []\nfull_args = []\ntest_roots = [\"tests\"]\nworkers = 1\n"
        "lifecycle = \"cooperative-process-group\"\n"
        "[setup]\nargv = [\"uv\", \"sync\", \"--locked\"]\n"
        "required_paths = [\".venv/bin/python\"]\nnetwork = true\n"
        "lifecycle_scripts = false\n[selection]\nnon_input_outputs = [\".venv\"]\n"
    )
    (root / ".ptest.toml").write_text(config_text, encoding="utf-8")
    subprocess.run(("git", "add", "."), cwd=root, check=True,
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    subprocess.run(("git", "commit", "-m", "normal setup fixture"), cwd=root,
                   check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    config = config_api.resolve_config(root).config
    assert config is not None
    # Initialize the account-scoped coordinator before history/guard work.
    operations.scheduler.initialize(domain)

    # Perform the real declared setup as the candidate would, then verify both
    # private outputs exist. Native candidate launch is covered separately.
    subprocess.run(("uv", "sync", "--locked"), cwd=root, check=True,
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert domain.root.is_dir()
    assert (root / ".venv" / "bin" / "python").exists()


@pytest.mark.parametrize(
    "run_request",
    [
        pytest.param(C.RunRequest(mode=C.Mode.SCOPED, shadow=True), id="shadow"),
        pytest.param(
            C.RunRequest(
                mode=C.Mode.SCOPED,
                probe=C.ProbeOptions(scope="tests/test_a.py"),
            ),
            id="probe",
        ),
    ],
)
def test_pytest_shadow_and_probe_stay_refused_with_setup(case, monkeypatch, run_request):
    domain = case.domain()
    setup = C.SetupConfig(
        argv=("uv", "sync"), required_paths=(".venv",),
        network=False, lifecycle_scripts=False,
    )
    config = case.config(
        runner_kind="pytest", setup=setup,
        config_path=case.base / ".ptest.toml",
    )
    monkeypatch.setattr(
        operations.scheduler,
        "enqueue",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("shadow/probe must be refused before admission")),
    )

    with pytest.raises(C.Problem, match="pytest shadow and probe are unavailable"):
        operations.execute(domain, config, run_request)


def test_pytest_scoped_preparation_is_basic_serial_and_never_adds_xdist():
    config = C.Config(
        runner=C.RunnerConfig(
            kind=C.RunnerKind.PYTEST,
            launcher=("python",),
            args=("-s", "--reporter", "custom"),
            full_args=("--cov",),
            test_roots=("tests",),
            workers=8,
        ),
        setup=None,
        resources=C.ResourceConfig(),
        selection=C.SelectionPolicy(enabled=False, closed_inputs=False),
        project_id="ab" * 16,
        config_path=Path("/project/.ptest.toml"),
    )
    plan = C.Plan(mode=C.Mode.SCOPED, execution="scoped", files=("tests/test_a.py",))

    prepared = prepare(config, plan, _grant(1), _attempt(1))

    assert "-n" not in prepared.argv
    assert prepared.summary.workers == 1
    assert prepared.capability.execution is C.ExecutionTier.BASIC_SERIAL
    assert prepared.capability.selection is False
    assert prepared.argv[-1] == "tests/test_a.py"


def _setup_project(case, domain, setup_code, *, present=False):
    root = case.project(domain, kind="pytest")
    project_id = (root / ".ptest.toml").read_text(encoding="utf-8").split(
        'project_id = "', 1
    )[1].split('"', 1)[0]
    (root / "tests").mkdir()
    (root / "tests" / "test_native.py").write_text(
        "from pathlib import Path\n"
        "Path('execution-marker').write_text('ran')\n"
        "def test_body():\n"
        "    assert True\n",
        encoding="utf-8",
    )
    if present:
        (root / "ready").write_text("present", encoding="utf-8")
    config = (
        "version = 1\n"
        f'project_id = "{project_id}"\n'
        "[runner]\n"
        f"launcher = {json.dumps([sys.executable])}\n"
        'kind = "pytest"\n'
        "args = []\n"
        "full_args = []\n"
        'test_roots = ["tests"]\n'
        "workers = 1\n"
        'lifecycle = "cooperative-process-group"\n'
        "[setup]\n"
        f"argv = {json.dumps([sys.executable, '-c', setup_code])}\n"
        'required_paths = ["ready"]\n'
        "network = false\n"
        "lifecycle_scripts = false\n"
    )
    (root / ".ptest.toml").write_text(config, encoding="utf-8")
    return root


def _run_setup_project(case, domain, root, monkeypatch, *, no_setup=False):
    observed = {}
    original = operations._run_guard

    def run_guard(*args, **kwargs):
        raw, frames, elapsed = original(*args, **kwargs)
        observed["setup_phase"] = frames.setup_phase
        observed["setup_facts"] = frames.setup_facts
        observed["execution_phase"] = frames.phase
        observed["execution_facts"] = frames.facts
        return raw, frames, elapsed

    monkeypatch.setattr(operations, "_run_guard", run_guard)
    config = config_api.resolve_config(root).config
    result = operations.execute(
        domain,
        config,
        C.RunRequest(
            mode=C.Mode.SCOPED,
            argv=("tests/test_native.py",),
            no_setup=no_setup,
        ),
    )
    return result, observed


def test_pytest_missing_required_path_runs_setup_before_execution(case, monkeypatch):
    domain = case.domain()
    root = _setup_project(
        case,
        domain,
        "from pathlib import Path; Path('setup-marker').write_text('ran'); Path('ready').write_text('ready')",
    )

    result, observed = _run_setup_project(case, domain, root, monkeypatch)

    assert result.status is C.Status.PASSED
    assert (root / "setup-marker").read_text() == "ran"
    assert (root / "execution-marker").read_text() == "ran"
    assert observed["setup_phase"] is True
    assert observed["setup_facts"]["phase"] == "setup"
    assert observed["setup_facts"]["attempt_id"] == "a001"
    assert observed["execution_phase"] is True
    assert observed["execution_facts"]["phase"] == "execution"
    assert observed["execution_facts"]["attempt_id"] == "a001"


def test_pytest_present_required_path_skips_setup(case, monkeypatch):
    domain = case.domain()
    root = _setup_project(
        case,
        domain,
        "from pathlib import Path; Path('setup-marker').write_text('must-not-run')",
        present=True,
    )
    config = config_api.resolve_config(root).config
    operations._record_setup_fingerprint(
        domain, operations._checkout(config),
        operations._setup_fingerprint(config, operations._checkout(config)),
    )

    result, observed = _run_setup_project(case, domain, root, monkeypatch)

    assert result.status is C.Status.PASSED
    assert not (root / "setup-marker").exists()
    assert (root / "execution-marker").read_text() == "ran"
    assert observed["setup_phase"] is False
    assert observed["setup_facts"] is None
    assert observed["execution_phase"] is True


def test_pytest_present_without_baseline_becomes_stale_after_lock_change(case, monkeypatch):
    domain = case.domain()
    root = _setup_project(
        case, domain,
        "from pathlib import Path; Path('setup-marker').write_text('ran')",
        present=True,
    )
    (root / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    config = config_api.resolve_config(root).config

    with pytest.raises(C.Problem, match="stale") as exc:
        operations.execute(
            domain, config,
            C.RunRequest(mode=C.Mode.SCOPED,
                         argv=("tests/test_native.py",), no_setup=True),
        )
    assert exc.value.phase == "setup"
    assert not (root / "setup-marker").exists()

    first, observed = _run_setup_project(case, domain, root, monkeypatch)
    assert first.status is C.Status.PASSED
    assert observed["setup_phase"] is True

    (root / "uv.lock").write_text("version = 2\n", encoding="utf-8")
    with pytest.raises(C.Problem, match="stale") as exc:
        operations.execute(
            domain, config_api.resolve_config(root).config,
            C.RunRequest(mode=C.Mode.SCOPED,
                         argv=("tests/test_native.py",), no_setup=True),
        )
    assert exc.value.phase == "setup"


def test_pytest_no_setup_blocks_missing_required_path_without_children(case, monkeypatch):
    domain = case.domain()
    root = _setup_project(
        case,
        domain,
        "from pathlib import Path; Path('setup-marker').write_text('must-not-run'); Path('ready').write_text('ready')",
    )
    config = config_api.resolve_config(root).config

    with pytest.raises(C.Problem, match="required setup") as exc:
        operations.execute(
            domain,
            config,
            C.RunRequest(
                mode=C.Mode.SCOPED,
                argv=("tests/test_native.py",),
                no_setup=True,
            ),
        )

    assert exc.value.phase == "setup"
    assert not (root / "setup-marker").exists()
    assert not (root / "execution-marker").exists()


def test_pytest_nonzero_setup_blocks_execution_and_preserves_setup_result(case, monkeypatch):
    domain = case.domain()
    root = _setup_project(
        case,
        domain,
        "from pathlib import Path; Path('setup-marker').write_text('ran'); raise SystemExit(23)",
    )

    result, observed = _run_setup_project(case, domain, root, monkeypatch)

    assert result.status is C.Status.FAILED
    assert result.exit_code == 23
    assert result.runner_exit_code is None
    assert result.exit_origin == "setup"
    assert result.attempts[0].phase == "setup"
    assert result.attempts[0].raw_exit_code == 23
    assert (root / "setup-marker").read_text() == "ran"
    assert not (root / "execution-marker").exists()
    assert observed["setup_facts"]["phase"] == "setup"


def test_pytest_setup_zero_without_required_path_stops_before_execution(case, monkeypatch):
    domain = case.domain()
    root = _setup_project(case, domain, "raise SystemExit(0)")

    result, observed = _run_setup_project(case, domain, root, monkeypatch)

    assert result.status is C.Status.INCOMPLETE
    assert result.exit_code != 0
    assert result.exit_origin == "ptest"
    assert [attempt.phase for attempt in result.attempts] == ["setup", "execution"]
    assert result.attempts[0].status is C.Status.PASSED
    assert result.attempts[0].raw_exit_code == 0
    assert result.attempts[0].final_exit_code == 0
    assert result.attempts[1].status is C.Status.NOT_RUN
    assert observed["setup_facts"]["raw_exit_code"] == 0
    assert observed["execution_phase"] is False
    assert not (root / "execution-marker").exists()


def test_pytest_setup_timeout_blocks_execution_with_truthful_outcome(case, monkeypatch):
    domain = case.domain()
    root = _setup_project(case, domain, "import time; time.sleep(1)")
    monkeypatch.setattr(C, "DEFAULT_SETUP_TIMEOUT_S", 0.1)

    result, observed = _run_setup_project(case, domain, root, monkeypatch)

    assert result.status is C.Status.INCOMPLETE
    assert result.exit_code != 0
    assert result.exit_origin == "setup"
    assert result.runner_exit_code is None
    assert result.status is not C.Status.PASSED
    assert result.attempts[0].phase == "setup"
    assert observed["setup_facts"]["phase"] == "setup"
    assert observed["execution_phase"] is False
    assert not (root / "execution-marker").exists()


def test_pytest_missing_setup_executable_blocks_execution(case, monkeypatch):
    domain = case.domain()
    setup_code = "from pathlib import Path; Path('setup-marker').write_text('ran')"
    root = _setup_project(case, domain, setup_code)
    config_text = (root / ".ptest.toml").read_text(encoding="utf-8")
    config_text = config_text.replace(
        json.dumps([sys.executable, "-c", setup_code]),
        json.dumps([str(root / "missing-setup-tool"), "-c", setup_code]),
    )
    (root / ".ptest.toml").write_text(config_text, encoding="utf-8")

    result, observed = _run_setup_project(case, domain, root, monkeypatch)

    assert result.status is C.Status.FAILED
    assert result.exit_code == 127
    assert result.runner_exit_code is None
    assert result.exit_origin == "setup"
    assert result.attempts[0].phase == "setup"
    assert observed["setup_facts"]["problem"]["code"] == "missing-executable"
    assert observed["execution_phase"] is False
    assert not (root / "execution-marker").exists()


def test_pytest_setup_lingering_descendant_blocks_execution(case, monkeypatch):
    domain = case.domain()
    root = _setup_project(
        case,
        domain,
        "import subprocess, sys; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(10)'])",
    )

    result, observed = _run_setup_project(case, domain, root, monkeypatch)

    assert result.status is C.Status.INCOMPLETE
    assert result.exit_code != 0
    assert result.exit_origin == "setup"
    assert result.runner_exit_code is None
    assert result.status is not C.Status.PASSED
    assert result.attempts[0].phase == "setup"
    assert observed["setup_facts"]["raw_exit_code"] == 0
    assert observed["execution_phase"] is False
    assert not (root / "execution-marker").exists()


def test_pytest_setup_cancellation_after_success_has_no_protocol_error(case, monkeypatch):
    domain = case.domain()
    root = _setup_project(
        case, domain,
        "from pathlib import Path; Path('ready').write_text('ready')",
    )
    def cancel_before_execution(*args, **kwargs):
        os.kill(os.getpid(), 2)
        return False

    monkeypatch.setattr(operations, "_send_attempt_decision",
                        cancel_before_execution)
    result, observed = _run_setup_project(case, domain, root, monkeypatch)

    assert result.status is C.Status.INCOMPLETE
    assert result.exit_code in {130, 143}
    assert result.exit_origin == "signal"
    assert all(reason.code != "protocol-mismatch" for reason in result.reasons)
    assert observed["setup_facts"]["raw_exit_code"] == 0
    assert observed["execution_phase"] is False
    assert not (root / "execution-marker").exists()


def test_pytest_successful_setup_is_recorded_with_timing(case, monkeypatch):
    domain = case.domain()
    root = _setup_project(
        case, domain,
        "from pathlib import Path; Path('ready').write_text('ready')",
    )

    result, observed = _run_setup_project(case, domain, root, monkeypatch)

    assert result.status is C.Status.PASSED
    assert [attempt.phase for attempt in result.attempts] == ["setup", "execution"]
    assert result.attempts[0].attempt_id == result.attempts[1].attempt_id == "a001"
    assert result.attempts[0].timings.setup_s is not None
    assert result.attempts[0].timings.setup_s >= 0
    assert result.timings.setup_s == result.attempts[0].timings.setup_s
    assert observed["setup_facts"]["phase"] == "setup"


def test_pytest_setup_environment_has_only_non_secret_identity(case, monkeypatch):
    domain = case.domain()
    root = _setup_project(
        case,
        domain,
        "import os; open('setup-env', 'w').write('|'.join(name for name in os.environ if name.startswith('PTEST_'))) ; open('ready', 'w').write('ready')",
    )

    monkeypatch.setenv("PTEST_GRANT_NONCE", "must-not-leak")
    monkeypatch.setenv("PTEST_PYTEST_FUTURE_SECRET", "must-not-leak")
    monkeypatch.setenv("PTEST_PYTEST_REPORT_PATH", "must-not-leak")
    monkeypatch.setenv("PTEST_TEST_ROOTS", "must-not-leak")
    monkeypatch.setenv("PTEST_VITEST_FUTURE_SECRET", "must-not-leak")

    result, _ = _run_setup_project(case, domain, root, monkeypatch)

    assert result.status is C.Status.PASSED
    names = (root / "setup-env").read_text().split("|")
    assert set(names) == {
        "PTEST_PROJECT_ID", "PTEST_CHECKOUT_ID", "PTEST_RUN_ID",
        "PTEST_ATTEMPT_ID", "PTEST_WORKER_ID", "PTEST_RESOURCE_PREFIX",
    }


def test_pytest_setup_fingerprint_changes_when_tool_bytes_change(case):
    domain = case.domain()
    root = _setup_project(case, domain, "raise SystemExit(0)")
    tool = root / "setup-tool"
    tool.write_text("tool-v1\n", encoding="utf-8")
    tool.chmod(0o700)
    config_text = (root / ".ptest.toml").read_text(encoding="utf-8")
    config_text = config_text.replace(
        json.dumps([sys.executable, "-c", "raise SystemExit(0)"]),
        json.dumps([str(tool), "-c", "raise SystemExit(0)"]),
    )
    (root / ".ptest.toml").write_text(config_text, encoding="utf-8")
    config = config_api.resolve_config(root).config
    checkout = operations._checkout(config)
    first = operations._setup_fingerprint(config, checkout)
    tool.write_text("tool-v2\n", encoding="utf-8")
    second = operations._setup_fingerprint(config, checkout)
    assert first != second


def test_pytest_bare_setup_tool_skips_regular_file_path_entry(case, monkeypatch):
    domain = case.domain()
    setup_code = "from pathlib import Path; Path('ready').write_text('ready')"
    root = _setup_project(case, domain, setup_code)
    tool_name = Path(sys.executable).name
    config_text = (root / ".ptest.toml").read_text(encoding="utf-8")
    config_text = config_text.replace(
        json.dumps([sys.executable, "-c", setup_code]),
        json.dumps([tool_name, "-c", setup_code]),
    )
    (root / ".ptest.toml").write_text(config_text, encoding="utf-8")
    regular_file_entry = root / "not-a-directory"
    regular_file_entry.write_text("not a directory\n", encoding="utf-8")
    monkeypatch.setenv(
        "PATH",
        os.pathsep.join((str(regular_file_entry), str(Path(sys.executable).parent))),
    )
    config = config_api.resolve_config(root).config

    fingerprint = operations._setup_fingerprint(config, operations._checkout(config))
    result, _ = _run_setup_project(case, domain, root, monkeypatch)

    assert isinstance(fingerprint, str)
    assert len(fingerprint) == 64
    assert result.status is C.Status.PASSED


def test_pytest_setup_revalidation_oserror_is_typed(case, monkeypatch):
    domain = case.domain()
    setup_code = "from pathlib import Path; Path('ready').write_text('ready')"
    root = _setup_project(case, domain, setup_code)
    config = config_api.resolve_config(root).config
    original = operations._setup_fingerprint
    calls = 0

    def fail_during_revalidation(config_arg, checkout):
        nonlocal calls
        calls += 1
        if calls >= 3:
            raise OSError("revalidation read failed")
        return original(config_arg, checkout)

    monkeypatch.setattr(operations, "_setup_fingerprint", fail_during_revalidation)

    result, observed = _run_setup_project(case, domain, root, monkeypatch)

    assert result.status is C.Status.INCOMPLETE
    assert result.exit_code != 0
    assert result.exit_origin == "ptest"
    assert result.runner_exit_code is None
    assert any(reason.code == "state-unavailable" for reason in result.reasons)
    assert any(
        reason.message == "setup tool/lock fingerprint could not be revalidated"
        for reason in result.reasons
    )
    assert observed["setup_facts"]["phase"] == "setup"
    assert observed["execution_phase"] is False
    assert not (root / "execution-marker").exists()


def test_pytest_setup_fingerprint_skips_nonregular_root_inputs(case):
    domain = case.domain()
    root = _setup_project(case, domain, "raise SystemExit(0)")
    (root / "uv.lock").mkdir()
    (root / "pyproject.toml").mkdir()
    config = config_api.resolve_config(root).config

    fingerprint = operations._setup_fingerprint(config, operations._checkout(config))

    assert len(fingerprint) == 64


def test_pytest_setup_fingerprint_follows_regular_root_symlink(case):
    domain = case.domain()
    root = _setup_project(case, domain, "raise SystemExit(0)")
    lock_dir = root / "shared"
    lock_dir.mkdir()
    target = lock_dir / "uv.lock"
    target.write_text("version = 1\n", encoding="utf-8")
    (root / "uv.lock").symlink_to(target)
    config = config_api.resolve_config(root).config
    checkout = operations._checkout(config)

    first = operations._setup_fingerprint(config, checkout)
    target.write_text("version = 2\n", encoding="utf-8")
    second = operations._setup_fingerprint(config, checkout)

    assert first != second


def test_pytest_setup_fingerprint_dangling_root_symlink_is_unavailable(case):
    domain = case.domain()
    root = _setup_project(case, domain, "raise SystemExit(0)")
    (root / "uv.lock").symlink_to(root / "missing-lock")
    config = config_api.resolve_config(root).config

    with pytest.raises(C.Problem) as exc:
        operations._setup_fingerprint(config, operations._checkout(config))

    assert exc.value.code == "state-unavailable"
    assert exc.value.phase == "setup"


@pytest.mark.parametrize("failure", ["problem", "os-error"])
def test_pytest_setup_fingerprint_regular_read_failure_is_unavailable(
        case, monkeypatch, failure):
    domain = case.domain()
    root = _setup_project(case, domain, "raise SystemExit(0)")
    (root / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    config = config_api.resolve_config(root).config
    original = operations.files.read_regular

    def fail_regular(parent, relative, max_bytes):
        if relative == "uv.lock":
            if failure == "problem":
                raise C.Problem(code="unsafe-path", message="read blocked", phase="setup")
            raise OSError("read blocked")
        return original(parent, relative, max_bytes)

    monkeypatch.setattr(operations.files, "read_regular", fail_regular)
    with pytest.raises(C.Problem) as exc:
        operations._setup_fingerprint(config, operations._checkout(config))

    assert exc.value.code == "state-unavailable"
    assert exc.value.phase == "setup"


def test_pytest_relative_setup_tool_fingerprint_is_checkout_rooted(case, monkeypatch):
    domain = case.domain()
    root = _setup_project(case, domain, "raise SystemExit(0)")
    tools = root / "tools"
    tools.mkdir()
    tool = tools / "setup-tool"
    tool.write_text("#!/bin/sh\nprintf ready > ready\nexit 0\n", encoding="utf-8")
    tool.chmod(0o700)
    config_text = (root / ".ptest.toml").read_text(encoding="utf-8")
    config_text = config_text.replace(
        json.dumps([sys.executable, "-c", "raise SystemExit(0)"]),
        json.dumps(["./tools/setup-tool"]),
    )
    (root / ".ptest.toml").write_text(config_text, encoding="utf-8")
    config = config_api.resolve_config(root).config
    checkout = operations._checkout(config)
    subdir = root / "subdir"
    subdir.mkdir()
    monkeypatch.chdir(subdir)

    first_result = operations.execute(
        domain, config,
        C.RunRequest(mode=C.Mode.SCOPED, argv=("tests/test_native.py",)),
    )
    assert first_result.status is C.Status.PASSED
    second_result = operations.execute(
        domain, config,
        C.RunRequest(mode=C.Mode.SCOPED, argv=("tests/test_native.py",), no_setup=True),
    )
    assert second_result.status is C.Status.PASSED

    first = operations._setup_fingerprint(config, checkout)
    tool.write_text("#!/bin/sh\nprintf ready > ready\nprintf changed > changed\nexit 0\n",
                    encoding="utf-8")
    second = operations._setup_fingerprint(config, checkout)

    assert first != second
    with pytest.raises(C.Problem, match="stale"):
        operations.execute(
            domain, config,
            C.RunRequest(mode=C.Mode.SCOPED,
                         argv=("tests/test_native.py",), no_setup=True),
        )


def test_pytest_dangling_required_path_is_missing(case, monkeypatch):
    domain = case.domain()
    root = _setup_project(case, domain, "raise SystemExit(0)")
    (root / "ready").symlink_to(root / "missing-ready")
    config = config_api.resolve_config(root).config

    assert "required setup path is missing" in operations._required_paths_issue(
        config, operations._checkout(config))
    with pytest.raises(C.Problem, match="required setup") as exc:
        operations.execute(
            domain, config,
            C.RunRequest(mode=C.Mode.SCOPED,
                         argv=("tests/test_native.py",), no_setup=True),
        )
    assert exc.value.phase == "setup"


def test_pytest_setup_fingerprint_stale_then_refreshes_for_no_setup(case, monkeypatch):
    domain = case.domain()
    setup_code = (
        "from pathlib import Path; Path('setup-marker').write_text('ran'); "
        "Path('ready').write_text('ready')"
    )
    root = _setup_project(
        case, domain, setup_code,
    )
    alias = root / "python-alias"
    alias.symlink_to(sys.executable)
    config_text = (root / ".ptest.toml").read_text(encoding="utf-8")
    config_text = config_text.replace(
        json.dumps([sys.executable, "-c", setup_code]),
        json.dumps([str(alias), "-c", setup_code]),
    )
    (root / ".ptest.toml").write_text(config_text, encoding="utf-8")
    (root / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    (root / "pyproject.toml").write_text("[project]\nname = 'fixture'\n", encoding="utf-8")

    first, observed = _run_setup_project(case, domain, root, monkeypatch)
    assert first.status is C.Status.PASSED
    assert observed["setup_phase"] is True

    (root / "uv.lock").write_text("version = 2\n", encoding="utf-8")
    config = config_api.resolve_config(root).config
    with pytest.raises(C.Problem, match="stale") as exc:
        operations.execute(
            domain, config,
            C.RunRequest(mode=C.Mode.SCOPED,
                         argv=("tests/test_native.py",), no_setup=True),
        )
    assert exc.value.phase == "setup"
    assert (root / "setup-marker").read_text() == "ran"

    refreshed, observed = _run_setup_project(case, domain, root, monkeypatch)
    assert refreshed.status is C.Status.PASSED
    assert observed["setup_phase"] is True

    fresh, observed = _run_setup_project(
        case, domain, root, monkeypatch, no_setup=True)
    assert fresh.status is C.Status.PASSED
    assert observed["setup_phase"] is False


def test_frames_require_declared_setup_before_execution_ready():
    grant = _grant()
    peer, controller = socket.socketpair()
    try:
        identity = C.ProcessIdentity(pid=1, birth=1.0, uid=os.getuid(), pgid=1)
        frames = operations._Frames(
            controller, grant, identity, expect_setup=True)
        frames._accept(C.ControlFrame(
            protocol=C.GUARD_PROTOCOL_VERSION, run_id=grant.run_id,
            nonce=grant.nonce, kind="registered",
            payload={"guard": {"pid": 1, "birth": 1.0,
                                "uid": os.getuid(), "pgid": 1}},
        ))
        frames._accept(C.ControlFrame(
            protocol=C.GUARD_PROTOCOL_VERSION, run_id=grant.run_id,
            nonce=grant.nonce, kind="attempt-ready",
            payload={"attempt_id": "a001", "previous_attempt_id": None,
                     "generation": 0, "gate_token": "56" * 16,
                     "deadline_monotonic": time.monotonic() + 1},
        ))
        assert frames.invalid is not None
    finally:
        peer.close()
        controller.close()


def test_frames_reject_setup_when_manifest_has_none():
    grant = _grant()
    peer, controller = socket.socketpair()
    try:
        identity = C.ProcessIdentity(pid=1, birth=1.0, uid=os.getuid(), pgid=1)
        frames = operations._Frames(
            controller, grant, identity, expect_setup=False)
        frames._accept(C.ControlFrame(
            protocol=C.GUARD_PROTOCOL_VERSION, run_id=grant.run_id,
            nonce=grant.nonce, kind="registered",
            payload={"guard": {"pid": 1, "birth": 1.0,
                                "uid": os.getuid(), "pgid": 1}},
        ))
        frames._accept(C.ControlFrame(
            protocol=C.GUARD_PROTOCOL_VERSION, run_id=grant.run_id,
            nonce=grant.nonce, kind="phase",
            payload={"phase": "setup", "attempt_id": "a001"},
        ))
        assert frames.invalid is not None
    finally:
        peer.close()
        controller.close()


def test_bridge_writes_private_terminal_report_after_native_exit(tmp_path, monkeypatch):
    report_dir = tmp_path / "reports"
    report_dir.mkdir(mode=0o700)
    report_path = report_dir / "native-a001-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.json"
    descriptor = Path(pytest_bridge.__file__).with_name("protocol-v1.json")
    monkeypatch.setenv("PTEST_BRIDGE_PROTOCOL", str(descriptor))
    monkeypatch.setenv("PTEST_GRANT_WORKERS", "1")
    monkeypatch.setenv("PTEST_RUN_ID", "12" * 16)
    monkeypatch.setenv("PTEST_GRANT_NONCE", "34" * 32)
    monkeypatch.setenv("PTEST_PYTEST_ATTEMPT", "a001")
    monkeypatch.setenv("PTEST_PYTEST_EXECUTION", "scoped")
    monkeypatch.setenv("PTEST_PYTEST_REPORT_PATH", str(report_path))

    class FakePytest:
        __version__ = "9.1.1"
        UsageError = RuntimeError
        hookimpl = staticmethod(lambda **kwargs: (lambda fn: fn))

        @staticmethod
        def main(argv, plugins):
            assert argv == ["tests/test_a.py"]
            assert len(plugins) == 1
            return 23

    monkeypatch.setitem(__import__("sys").modules, "pytest", FakePytest)

    assert pytest_bridge.run(["tests/test_a.py"]) == 23
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload == {
        "protocol": 1,
        "run_id": "12" * 16,
        "nonce": "34" * 32,
        "attempt_id": "a001",
        "runner": "pytest",
        "observed_runtime_version": "9.1.1",
        "execution_mode": "scoped",
        "effective_profile": "basic_serial",
        "terminal_complete": True,
        "native_exit_code": 23,
        "bridge_exit_code": 23,
        "problem": "native-failure",
    }
