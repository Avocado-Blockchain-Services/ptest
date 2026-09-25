"""Shared NG fixture factories (test-only, Task0 owned)."""
from __future__ import annotations

import hashlib
import json
import math
import os
import pwd
import secrets
import select
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import MappingProxyType
from typing import NamedTuple

from ptest import contracts as C
from ptest import files as F

BOUND_OUTPUT_BYTES = 1 << 20
RESULT_EXPORT_PREFIX = "ptest-result-"

CONTROL_VARS = (
    "PTEST_RUN_ID",
    "PTEST_GRANT_NONCE",
    "PTEST_ATTEMPT_ID",
    "PTEST_WORKER_ID",
    "PTEST_RESOURCE_PREFIX",
    "PTEST_PROJECT_ID",
    "PTEST_CHECKOUT_ID",
    "PTEST_STATE_DIR",
    "PTEST_HOME",
    "PTEST_FIXTURE_DOMAIN",
)
# Frozen execution grammar (design section 4): leading wrapper options only.
# Value flags consume one following token; boolean flags consume none.
# Scanning stops at the first unknown/native token or "--", which starts the
# untouched native tail. Never infer runner option arity inside that suffix.
_WRAPPER_VALUE_OPTS = (
    "--fixture-domain", "--base", "--workers", "--queue-timeout",
    "--timeout", "--result-json",
)
_WRAPPER_BOOL_OPTS = (
    "--changed", "--full", "--no-setup", "--shadow",
)
def _prefix_result_json(args: tuple) -> str | None:
    found = None
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--":
            return found
        if token in _WRAPPER_BOOL_OPTS:
            index += 1
            continue
        if token not in _WRAPPER_VALUE_OPTS:
            return found
        if index + 1 >= len(args):
            return found
        if token == "--result-json":
            found = args[index + 1]
        index += 2
    return found


_READ_CHUNK = 65536
_EXIT_DRAIN_GRACE_S = 1.0
def _check_timeout(timeout: float) -> float:
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise ValueError("invoke requires an explicit positive finite keyword timeout")
    deadline_s = float(timeout)
    if not math.isfinite(deadline_s) or deadline_s <= 0:
        raise ValueError("invoke requires an explicit positive finite keyword timeout")
    return deadline_s
def _close_stream(stream) -> None:
    try:
        stream.close()
    except (OSError, ValueError):
        pass
def _kill_owned_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        pass
    try:
        proc.kill()
    except (OSError, ValueError):
        pass
def _read_export_file(root: Path, name: str):
    """Read back a project-relative result export.

    Returns the parsed dict, or None when the export is legitimately
    absent (missing file), rejected as unsafe (absolute path, symlink,
    FIFO, escape), or not a JSON dict (malformed, scalar, list).
    Raises ValueError when the export exceeds the bound: truncated
    bytes are incomplete evidence, never a missing file. All reads go
    through the shared non-blocking regular-file reader; absolute
    names are rejected without opening them, so a FIFO can never wedge
    the caller.
    """
    try:
        raw = F.read_regular(root, name, BOUND_OUTPUT_BYTES + 1)
    except (OSError, ValueError, C.Problem):
        return None
    if len(raw) > BOUND_OUTPUT_BYTES:
        raise ValueError(
            f"invoke result export exceeded {BOUND_OUTPUT_BYTES}-byte bound; "
            "captured output is incomplete evidence, not a complete result"
        )
    try:
        parsed = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed
def _drain_bounded(proc: subprocess.Popen, deadline_s: float) -> tuple:
    deadline = time.monotonic() + deadline_s
    buffers = [bytearray(), bytearray()]
    streams = (proc.stdout, proc.stderr)
    for stream in streams:
        try:
            os.set_blocking(stream.fileno(), False)
        except (OSError, ValueError):
            pass
    live = [True, True]
    exited_at = None
    while True:
        now = time.monotonic()
        if proc.poll() is not None and exited_at is None:
            exited_at = now
        if exited_at is not None:
            if not any(live):
                break
            if now - exited_at >= _EXIT_DRAIN_GRACE_S:
                break
            wait = min(0.05, _EXIT_DRAIN_GRACE_S - (now - exited_at))
            if deadline - now <= 0:
                break
            wait = min(wait, deadline - now)
        else:
            remaining = deadline - now
            if remaining <= 0:
                _kill_owned_group(proc)
                _close_stream(proc.stdout)
                _close_stream(proc.stderr)
                try:
                    proc.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    pass
                raise TimeoutError(f"invoke timeout after {deadline_s}s; owned child session killed")
            wait = min(0.05, remaining)
        try:
            ready, _, _ = select.select([s for s, flag in zip(streams, live) if flag], [], [], wait)
        except (OSError, ValueError) as exc:
            _kill_owned_group(proc)
            _close_stream(proc.stdout)
            _close_stream(proc.stderr)
            try:
                proc.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass
            raise OSError(f"invoke stream select failure ({exc}); owned child session killed") from exc
        for stream in ready:
            index = 0 if stream is proc.stdout else 1
            try:
                piece = os.read(stream.fileno(), _READ_CHUNK)
            except BlockingIOError:
                continue
            except (OSError, ValueError) as exc:
                name = "stdout" if index == 0 else "stderr"
                _kill_owned_group(proc)
                _close_stream(proc.stdout)
                _close_stream(proc.stderr)
                try:
                    proc.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    pass
                raise OSError(f"invoke stream {name} read failure ({exc}); owned child session killed") from exc
            if not piece:
                live[index] = False
                continue
            room = BOUND_OUTPUT_BYTES - len(buffers[index])
            if room > 0:
                buffers[index] += piece[:room]
            if len(piece) > max(room, 0):
                name = "stdout" if index == 0 else "stderr"
                _kill_owned_group(proc)
                _close_stream(proc.stdout)
                _close_stream(proc.stderr)
                try:
                    proc.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    pass
                raise ValueError(f"invoke {name} exceeded {BOUND_OUTPUT_BYTES}-byte bound; captured output is incomplete evidence, not a complete result")
    for stream in streams:
        _close_stream(stream)
    if proc.poll() is None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _kill_owned_group(proc)
            try:
                proc.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass
            raise TimeoutError(f"invoke timeout after {deadline_s}s; owned child session killed")
        try:
            proc.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            _kill_owned_group(proc)
            try:
                proc.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass
            raise TimeoutError(f"invoke timeout after {deadline_s}s; owned child session killed")
    _kill_owned_group(proc)
    try:
        proc.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        pass
    return bytes(buffers[0]), bytes(buffers[1])
class Completed(NamedTuple):
    code: int
    stdout: bytes
    stderr: bytes
    result: dict | None


class PipeBarrier:
    """Owned pipe-based synchronization primitive for lifecycle tests."""

    def __init__(self) -> None:
        self._read, self._write = os.pipe()

    def signal(self) -> None:
        os.write(self._write, b"\x00")

    def wait(self, timeout: float) -> bool:
        ready, _, _ = select.select([self._read], [], [], timeout)
        if not ready:
            return False
        os.read(self._read, 1)
        return True

    def close(self) -> None:
        os.close(self._read)
        os.close(self._write)


def _hex32(seed: str = "") -> str:
    stamp = f"{time.time_ns()}-{secrets.token_hex(8)}-{seed}"
    return hashlib.sha256(stamp.encode()).hexdigest()[:32]


def _hex64(seed: str = "") -> str:
    stamp = f"{time.time_ns()}-{secrets.token_hex(8)}-{seed}"
    return hashlib.sha256(stamp.encode()).hexdigest()


class CaseFactory:
    def __init__(self, base: Path) -> None:
        self.base = Path(base)
        self._counter = 0

    def _next(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}-{self._counter}"

    def config(self, **overrides) -> C.Config:
        runner_kind = overrides.pop("runner_kind", "pytest")
        workers = overrides.pop("workers", 1)
        selection_enabled = overrides.pop("selection_enabled", False)
        closed_inputs = overrides.pop("closed_inputs", False)
        allowed = {
            "runner", "setup", "resources", "selection",
            "project_id", "checkout", "config_path",
        }
        unknown = set(overrides) - allowed
        if unknown:
            raise ValueError(f"unknown config override(s): {sorted(unknown)}")
        if isinstance(runner_kind, str):
            runner_kind = C.RunnerKind(runner_kind)
        launcher = ("python",) if runner_kind == C.RunnerKind.PYTEST else ("true",)
        runner = overrides.get("runner") or C.RunnerConfig(
            kind=runner_kind,
            launcher=launcher,
            args=(),
            full_args=(),
            test_roots=("tests",),
            workers=workers,
            lifecycle="cooperative-process-group",
        )
        selection = overrides.get("selection") or C.SelectionPolicy(
            enabled=selection_enabled,
            closed_inputs=closed_inputs,
            input_roots=("src", "tests"),
            ignored_inputs=(),
            environment=(),
            full_triggers=("pyproject.toml",),
            always=(),
            no_tests=(),
            non_input_outputs=(),
            full_ratio=0.70,
            groups=(),
        )
        return C.Config(
            runner=runner,
            setup=overrides.get("setup"),
            resources=overrides.get("resources") or C.ResourceConfig(
                locks=(), memory_mb_per_worker=0,
                probe_isolation="undeclared",
            ),
            selection=selection,
            project_id=overrides.get("project_id", "ab" * 16),
            checkout=overrides.get("checkout"),
            config_path=overrides.get("config_path"),
        )

    def snapshot(self, **overrides) -> C.InputSnapshot:
        allowed = {
            "digest", "compatibility", "head", "clean",
            "changes", "limitations", "files",
        }
        unknown = set(overrides) - allowed
        if unknown:
            raise ValueError(f"unknown snapshot override(s): {sorted(unknown)}")
        return C.InputSnapshot(
            digest=overrides.get("digest", _hex64("digest")),
            compatibility=overrides.get("compatibility", "test-compat-v1"),
            head=overrides.get("head", "a" * 40),
            clean=overrides.get("clean", True),
            changes=overrides.get("changes", ()),
            limitations=overrides.get("limitations", ()),
            files=overrides.get("files", ()),
        )

    def history(self, with_baseline: bool = False, **overrides) -> C.HistoryView:
        allowed = {"baseline", "obligations", "selection_disabled", "limitations"}
        unknown = set(overrides) - allowed
        if unknown:
            raise ValueError(f"unknown history override(s): {sorted(unknown)}")
        baseline = overrides.get("baseline")
        if baseline is None and with_baseline:
            inventory = self.inventory(("tests/test_a.py",), outcome="passed")
            baseline = C.Baseline(
                run_id=_hex32("run"),
                head="a" * 40,
                input_digest=_hex64("input"),
                compatibility="test-compat-v1",
                inventory=inventory,
                policy_digest=_hex64("policy"),
                created_at="2026-09-17T00:00:00+00:00",
            )
        return C.HistoryView(
            baseline=baseline,
            obligations=overrides.get("obligations", ()),
            selection_disabled=overrides.get("selection_disabled", False),
            limitations=overrides.get("limitations", ()),
        )

    def request(self, **overrides) -> C.RunRequest:
        allowed = {
            "mode", "argv", "base", "workers", "queue_timeout_s",
            "no_setup", "shadow", "result_path", "fixture_domain", "probe",
        }
        unknown = set(overrides) - allowed
        if unknown:
            raise ValueError(f"unknown request override(s): {sorted(unknown)}")
        return C.RunRequest(
            mode=overrides.get("mode", C.Mode.AUTOMATIC),
            argv=tuple(overrides.get("argv", ())),
            base=overrides.get("base"),
            workers=overrides.get("workers"),
            queue_timeout_s=overrides.get("queue_timeout_s", 1800.0),
            no_setup=overrides.get("no_setup", False),
            shadow=overrides.get("shadow", False),
            result_path=overrides.get("result_path"),
            fixture_domain=overrides.get("fixture_domain"),
            probe=overrides.get("probe"),
        )

    def result(self, sequence: int = 0, **overrides) -> C.RunResult:
        allowed = {
            "run_id", "project_id", "checkout_id", "mode", "status", "phase",
            "started_at", "finished_at", "plan", "command", "granted_workers",
            "memory_estimate_mb", "reserved_memory_mb", "runner_exit_code",
            "exit_code", "exit_origin", "signal", "source_valid",
            "full_gate_eligible", "baseline_published", "counts", "timings",
            "attempts", "reasons", "limitations", "artifact_id",
            "input_before", "input_after", "policy_digest",
        }
        unknown = set(overrides) - allowed
        if unknown:
            raise ValueError(f"unknown result override(s): {sorted(unknown)}")
        plan = overrides.get("plan") or C.Plan(
            mode=C.Mode.FULL, execution="full", files=(),
            reasons=(), input_digest=None, compatibility=None,
            baseline_run_id=None, static_preview=False,
        )
        command = overrides.get("command") or C.summarize_command(
            C.RunnerKind.PYTEST, C.Mode.FULL, ["-q"],
            workers=1, provenance=("test",),
        )
        return C.RunResult(
            run_id=overrides.get("run_id", _hex32("run")),
            project_id=overrides.get("project_id", "ab" * 16),
            checkout_id=overrides.get("checkout_id", _hex32("checkout")),
            mode=overrides.get("mode", C.Mode.FULL),
            status=overrides.get("status", "passed"),
            phase=overrides.get("phase", "complete"),
            started_at=overrides.get("started_at", "2026-09-17T00:00:00+00:00"),
            finished_at=overrides.get("finished_at", "2026-09-17T00:00:01+00:00"),
            plan=plan,
            command=command,
            granted_workers=overrides.get("granted_workers", 1),
            memory_estimate_mb=overrides.get("memory_estimate_mb"),
            reserved_memory_mb=overrides.get("reserved_memory_mb"),
            runner_exit_code=overrides.get("runner_exit_code", 0),
            exit_code=overrides.get("exit_code", 0),
            exit_origin=overrides.get("exit_origin", "runner"),
            signal=overrides.get("signal"),
            source_valid=overrides.get("source_valid", True),
            full_gate_eligible=overrides.get("full_gate_eligible", True),
            baseline_published=overrides.get("baseline_published", False),
            counts=overrides.get("counts"),
            timings=overrides.get("timings"),
            attempts=overrides.get("attempts", ()),
            reasons=overrides.get("reasons", ()),
            limitations=overrides.get("limitations", ()),
            artifact_id=overrides.get("artifact_id"),
            sequence=sequence,
            input_before=overrides.get("input_before"),
            input_after=overrides.get("input_after"),
            policy_digest=overrides.get("policy_digest"),
        )

    def inventory(self, files: tuple, **overrides) -> C.Inventory:
        allowed = {"adapter", "version", "complete", "outcome", "digest"}
        unknown = set(overrides) - allowed
        if unknown:
            raise ValueError(f"unknown inventory override(s): {sorted(unknown)}")
        outcome = overrides.get("outcome", "passed")
        if isinstance(outcome, str):
            outcome = C.Outcome(outcome)
        records = tuple(
            C.TestRecord(
                id=f"{name}::test_x", file=name, outcome=outcome,
                setup_s=None, call_s=0.01, teardown_s=None,
            )
            for name in files
        )
        return C.Inventory(
            adapter=overrides.get("adapter", "pytest"),
            version=overrides.get("version", "9.1.1"),
            complete=overrides.get("complete", True),
            tests=records,
            digest=overrides.get("digest", _hex64("inventory")),
        )

    def checkout(self, domain: C.DomainPaths) -> C.CheckoutIdentity:
        real = os.path.realpath(domain.root)
        checkout_id = hashlib.sha256(os.fsencode(real)).hexdigest()[:32]
        return C.CheckoutIdentity(
            project_id="ab" * 16,
            checkout_id=checkout_id,
            root=domain.root,
        )

    def domain(self, slots: int = 1, jobs: int = 1) -> C.DomainPaths:
        name = self._next("domain")
        path = F.ensure_private_dir(self.base, name)
        stamp = os.stat(path)
        fixture_id = _hex32(name)
        body = (
            "version = 1\n"
            "fixture = true\n"
            f"max_slots = {int(slots)}\n"
            f"max_jobs = {int(jobs)}\n"
            f"uid = {os.getuid()}\n"
            f"directory_device = {stamp.st_dev}\n"
            f"directory_inode = {stamp.st_ino}\n"
            f'fixture_id = "{fixture_id}"\n'
            'workload = "synthetic-or-miniature"\n'
        )
        F.create_exclusive(path, "fixture-domain.toml", body.encode("utf-8"))
        return C.DomainPaths(
            root=path,
            machine_config=path / "machine.toml",
            ledger=path / "coordinator.sqlite3",
            marker=path / "fixture-domain.toml",
            fixture=True,
            domain_id=fixture_id,
        )

    def project(self, domain: C.DomainPaths, kind: str = "command") -> Path:
        name = self._next("proj")
        path = F.ensure_private_dir(domain.root, name)
        project_id = _hex32(name)
        lines = [
            "version = 1",
            f'project_id = "{project_id}"',
            "[runner]",
            f'kind = "{kind}"',
            'launcher = ["echo"]',
            'args = ["hello"]',
            "full_args = []",
            "workers = 1",
            'lifecycle = "cooperative-process-group"',
        ]
        F.create_exclusive(
            path, ".ptest.toml", ("\n".join(lines) + "\n").encode("utf-8"),
            private=False,
        )
        return path

    def invoke(self, domain: C.DomainPaths, root: Path, *args: str,
               env: dict | None = None, timeout: float) -> Completed:
        deadline_s = _check_timeout(timeout)
        caller_export = _prefix_result_json(args)
        export_rel = None
        if caller_export is None:
            # Project-root unique file: the parent (the project root itself)
            # already exists, so exclusive creation never needs a mkdir chain.
            export_rel = f"{RESULT_EXPORT_PREFIX}{secrets.token_hex(4)}.json"
        cmd = [
            sys.executable, "-m", "ptest",
            "--fixture-domain", str(domain.root),
        ]
        if export_rel is not None:
            cmd += ["--result-json", export_rel]
        cmd += list(args)
        # Purge inherited control variables first so the fixture child
        # never inherits orchestrator state; explicit env= overrides are
        # applied after, letting negative tests deliberately pass one.
        child_env = {
            key: value for key, value in os.environ.items()
            if key not in CONTROL_VARS
        }
        for key, value in (env or {}).items():
            child_env[key] = value
        proc = subprocess.Popen(
            cmd, cwd=str(root), env=child_env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            stdout, stderr = _drain_bounded(proc, deadline_s)
        except BaseException:
            _kill_owned_group(proc)
            _close_stream(proc.stdout)
            _close_stream(proc.stderr)
            try:
                proc.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass
            raise
        parsed = None
        export_name = export_rel if export_rel is not None else caller_export
        if export_name is not None:
            parsed = _read_export_file(Path(root), export_name)
        return Completed(
            code=proc.returncode,
            stdout=stdout,
            stderr=stderr,
            result=parsed,
        )

    def barrier(self, name: str) -> PipeBarrier:
        del name
        return PipeBarrier()

    def events(self, domain: C.DomainPaths) -> tuple:
        try:
            raw = F.read_regular(Path(domain.root), "events.jsonl", BOUND_OUTPUT_BYTES)
        except (OSError, ValueError, C.Problem):
            return ()
        items = []
        for line in raw.splitlines():
            try:
                items.append(json.loads(line))
            except ValueError:
                continue
        return tuple(items)


# ---------------------------------------------------------------------------
# Frozen shared builders (ptest-parallel-suite design, section 3.2).
# Topic factory plugins (factories_*.py) build on these and never import
# each other.  Every builder writes only below a caller-supplied path.
# ---------------------------------------------------------------------------

XDIST_GROUPS = ("process-table", "uv-cache", "toolchain")

GIT_IDENTITY = MappingProxyType({
    "GIT_AUTHOR_NAME": "Fixture",
    "GIT_AUTHOR_EMAIL": "fixture@example.test",
    "GIT_COMMITTER_NAME": "Fixture",
    "GIT_COMMITTER_EMAIL": "fixture@example.test",
})

GIT_TIMEOUT_S = 30.0
PYTHON_SHEBANG = f"#!{sys.executable}\n"


def _real_tool_env() -> dict:
    """Toolchain caches resolved from the REAL account at import time.

    The only real-home paths tests may use: concurrency-safe, read-mostly
    caches, so an isolated HOME never forces a network download.
    """
    home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    cache = Path(os.environ.get("XDG_CACHE_HOME") or home / ".cache")
    data = Path(os.environ.get("XDG_DATA_HOME") or home / ".local" / "share")
    candidates = {
        "UV_CACHE_DIR": cache / "uv",
        "UV_PYTHON_INSTALL_DIR": data / "uv" / "python",
        "npm_config_cache": home / ".npm",
        "CARGO_HOME": home / ".cargo",
        "RUSTUP_HOME": home / ".rustup",
        "GOPATH": home / "go",
        "GOCACHE": cache / "go-build",
    }
    values = {}
    for name, default in candidates.items():
        explicit = os.environ.get(name)
        if explicit:
            values[name] = explicit
        elif default.is_dir():
            values[name] = str(default)
    return values


REAL_TOOL_ENV = MappingProxyType(_real_tool_env())

_GITCONFIG = (
    "[user]\n\tname = Fixture\n\temail = fixture@example.test\n"
    "[init]\n\tdefaultBranch = main\n"
    "[commit]\n\tgpgsign = false\n"
    "[tag]\n\tgpgsign = false\n"
    "[core]\n\thooksPath = /dev/null\n"
)


class IsolatedEnv(NamedTuple):
    root: Path
    home: Path
    state_dir: Path
    environ: MappingProxyType


def build_isolated_env(root: Path) -> IsolatedEnv:
    """Create a private per-test environment below ``root`` (all 0700).

    ``state_dir`` is created but NOT exported: PTEST_STATE_DIR stays unset
    by default (the passwd home is patched instead); a test that wants an
    explicit state directory sets it via monkeypatch.
    """
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    home = root / "home"
    state = root / "state"
    xdg = root / "xdg"
    xdg_dirs = {
        "XDG_CONFIG_HOME": xdg / "config",
        "XDG_CACHE_HOME": xdg / "cache",
        "XDG_DATA_HOME": xdg / "data",
        "XDG_STATE_HOME": xdg / "state",
    }
    for path in (home, state, xdg, *xdg_dirs.values()):
        path.mkdir(exist_ok=True)
        os.chmod(path, 0o700)
    gitconfig = root / "gitconfig"
    gitconfig.write_text(_GITCONFIG, encoding="utf-8")
    environ = {
        "HOME": str(home),
        **{name: str(path) for name, path in xdg_dirs.items()},
        "GIT_CONFIG_GLOBAL": str(gitconfig),
        "GIT_CONFIG_NOSYSTEM": "1",
        **REAL_TOOL_ENV,
    }
    return IsolatedEnv(root=root, home=home, state_dir=state,
                       environ=MappingProxyType(environ))


def patch_account_home(monkeypatch, home: Path) -> Path:
    """Make ``pwd.getpwuid(os.getuid()).pw_dir`` resolve to ``home``."""
    home = Path(home)
    home.mkdir(parents=True, exist_ok=True)
    os.chmod(home, 0o700)
    uid = os.getuid()
    previous = pwd.getpwuid

    def getpwuid(query):
        record = previous(query)
        if query != uid:
            return record
        return pwd.struct_passwd((
            record.pw_name, record.pw_passwd, record.pw_uid, record.pw_gid,
            record.pw_gecos, str(home), record.pw_shell,
        ))

    monkeypatch.setattr(pwd, "getpwuid", getpwuid)
    return home


def git_env(extra: dict | None = None) -> dict:
    """Hermetic git environment: no inherited GIT_*, no user/system config."""
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("GIT_") and key not in CONTROL_VARS}
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env.update(GIT_IDENTITY)
    env.update(extra or {})
    return env


def git(root, *args: str, env: dict | None = None, check: bool = True) -> str:
    """Run hermetic ``git -C root ...``; return stripped stdout."""
    completed = subprocess.run(
        ("git", "-c", "init.defaultBranch=main", "-c", "commit.gpgsign=false",
         "-c", "tag.gpgsign=false", "-c", "core.hooksPath=" + os.devnull,
         "-C", str(root), *args),
        env=git_env(env), check=check, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=GIT_TIMEOUT_S,
    )
    return completed.stdout.decode("utf-8", "replace").strip()


def _relative_file(relative) -> Path:
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError(f"file key must be a relative path: {relative!r}")
    return path


def write_file(path, content) -> Path:
    """Write str (utf-8) or bytes to ``path``, creating parents."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    elif isinstance(content, str):
        path.write_text(content, encoding="utf-8")
    else:
        raise TypeError("content must be str or bytes")
    return path


def write_executable(path, text: str) -> Path:
    path = write_file(path, text)
    os.chmod(path, 0o755)
    return path


def init_git_repo(root, *, files: dict | None = None, branch: str = "main",
                  commit: bool = True, message: str = "initial") -> Path:
    """``git init -b branch`` with fixture identity, files, optional commit."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "-q", "-b", branch)
    git(root, "config", "user.name", GIT_IDENTITY["GIT_AUTHOR_NAME"])
    git(root, "config", "user.email", GIT_IDENTITY["GIT_AUTHOR_EMAIL"])
    for relative, content in (files or {}).items():
        write_file(root / _relative_file(relative), content)
    if commit:
        git(root, "add", "-A")
        git(root, "commit", "-q", "--allow-empty", "-m", message)
    return root


def git_commit_all(root, message: str = "update") -> str:
    git(root, "add", "-A")
    git(root, "commit", "-q", "--allow-empty", "-m", message)
    return git(root, "rev-parse", "HEAD")


_SETUP_KEYS = frozenset({"argv", "required_paths", "network", "lifecycle_scripts"})


def ptest_toml_text(*, kind: str = "command", launcher=("echo",),
                    args=("hello",), full_args=(), test_roots=None,
                    workers: int = 1, project_id: str | None = None,
                    runner_extra: str = "", setup: dict | None = None,
                    tail: str = "") -> str:
    """Canonical ``.ptest.toml`` text; ``runner_extra`` lands in [runner]."""
    if test_roots is None:
        test_roots = () if kind == "command" else ("tests",)
    lines = [
        "version = 1",
        f"project_id = {json.dumps(project_id or secrets.token_hex(16))}",
        "",
        "[runner]",
        f"kind = {json.dumps(kind)}",
        f"launcher = {json.dumps(list(launcher))}",
        f"args = {json.dumps(list(args))}",
        f"full_args = {json.dumps(list(full_args))}",
    ]
    if test_roots:
        lines.append(f"test_roots = {json.dumps(list(test_roots))}")
    lines.append(f"workers = {int(workers)}")
    lines.append('lifecycle = "cooperative-process-group"')
    if runner_extra:
        lines.append(runner_extra.rstrip("\n"))
    if setup is not None:
        unknown = set(setup) - _SETUP_KEYS
        if unknown:
            raise TypeError(f"unknown setup keys: {sorted(unknown)}")
        lines.extend([
            "",
            "[setup]",
            f"argv = {json.dumps(list(setup['argv']))}",
            f"required_paths = {json.dumps(list(setup.get('required_paths', ())))}",
            f"network = {str(bool(setup.get('network', False))).lower()}",
            "lifecycle_scripts = "
            f"{str(bool(setup.get('lifecycle_scripts', False))).lower()}",
        ])
    text = "\n".join(lines) + "\n"
    if tail:
        text += tail if tail.endswith("\n") else tail + "\n"
    return text


def write_ptest_toml(root, **kwargs) -> Path:
    return write_file(Path(root) / ".ptest.toml", ptest_toml_text(**kwargs))
