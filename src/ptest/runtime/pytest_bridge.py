"""Dependency-light entry point executed by the selected project interpreter.

The module imports only the standard library until :func:`run` has validated
the generated protocol descriptor.  It intentionally has no ptest imports so
the target interpreter need only provide pytest itself.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any


class BridgeRefusal(RuntimeError):
    """A bridge-owned refusal, distinct from an exception in repository code."""

    def __init__(self, message: str, code: str = "native-config-invalid") -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


_REPORT_NAME = re.compile(r"native-a(00[1-9]|010)-[0-9a-f]{32}\.json\Z")


def _fail(message: str, code: str = "native-config-invalid") -> None:
    raise BridgeRefusal(message, code)


def _refusal_marker(code: str, message: str) -> None:
    # No argv, paths or project data: distinguish owned refusals from native exit 4.
    print("ptest-bridge-refusal: " + json.dumps({"code": code, "message": message}), file=sys.stderr)


def _protocol() -> dict[str, Any]:
    name = os.environ.get("PTEST_BRIDGE_PROTOCOL")
    if not name:
        _fail("missing protocol descriptor")
    try:
        raw = Path(name).read_bytes()
        data = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        _fail("unreadable protocol descriptor")
    if not isinstance(data, dict) or data.get("protocol") != 1:
        _fail("unsupported protocol descriptor")
    return data


def _workers() -> int:
    value = os.environ.get("PTEST_GRANT_WORKERS")
    try:
        workers = int(value or "")
    except ValueError:
        _fail("invalid granted worker count")
    if workers < 1 or workers > 64:
        _fail("invalid granted worker count")
    return workers


def _python_version() -> None:
    """Keep the tested interpreter matrix explicit before importing pytest."""
    version = sys.version_info
    if sys.implementation.name != "cpython" or version[0] != 3 or not 11 <= version[1] <= 14:
        _fail("unsupported CPython version or implementation", "unsupported-capability")


def _report_binding() -> tuple[Path, dict[str, str]] | None:
    """Read the executor-owned report binding, when this is an admitted run."""
    path_value = os.environ.get("PTEST_PYTEST_REPORT_PATH")
    identity_names = (
        "PTEST_RUN_ID", "PTEST_GRANT_NONCE", "PTEST_PYTEST_ATTEMPT",
        "PTEST_PYTEST_EXECUTION",
    )
    if path_value is None and not any(name in os.environ for name in identity_names):
        return None
    if not path_value or not os.path.isabs(path_value):
        _fail("executor must bind a private pytest report path")
    path = Path(path_value)
    if _REPORT_NAME.fullmatch(path.name) is None:
        _fail("executor report basename is invalid")
    run_id = os.environ.get("PTEST_RUN_ID", "")
    nonce = os.environ.get("PTEST_GRANT_NONCE", "")
    attempt = os.environ.get("PTEST_PYTEST_ATTEMPT", "")
    execution = os.environ.get("PTEST_PYTEST_EXECUTION", "")
    if (not re.fullmatch(r"[0-9a-f]{32}", run_id)
            or not re.fullmatch(r"[0-9a-f]{64}", nonce)
            or not re.fullmatch(r"a(00[1-9]|010)", attempt)
            or execution != "scoped"):
        _fail("invalid pytest report identity")
    if not path.parent.is_dir():
        _fail("pytest report directory is unavailable")
    try:
        parent = path.parent
        stamp = parent.stat()
        if stamp.st_uid != os.getuid() or stamp.st_mode & 0o077:
            _fail("pytest report directory is not private")
        if os.path.realpath(parent) != str(parent):
            _fail("pytest report directory is not private")
    except OSError:
        _fail("pytest report directory is unavailable")
    return path, {
        "run_id": run_id, "nonce": nonce, "attempt_id": attempt,
        "execution_mode": execution,
    }


def _write_report(path: Path, identity: dict[str, str], *, runtime: str,
                  native_exit: int | None, bridge_exit: int,
                  complete: bool, problem: str | None) -> None:
    payload = {
        "protocol": 1,
        **identity,
        "runner": "pytest",
        "observed_runtime_version": runtime,
        "effective_profile": "basic_serial",
        "terminal_complete": complete,
        "native_exit_code": native_exit,
        "bridge_exit_code": bridge_exit,
        "problem": problem,
    }
    raw = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
    if len(raw) > 65536:
        raise BridgeRefusal("pytest terminal report exceeds its bound", "capacity-exceeded")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        view = memoryview(raw)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("pytest terminal report write made no progress")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


class OwnedPlugin:
    """Additive profile gate; it neither replaces reporters nor parses addopts."""

    def __init__(self, workers: int) -> None:
        self.workers = workers

    def _refuse(self, message: str) -> None:
        from pytest import UsageError
        _refusal_marker("native-config-invalid", message)
        raise UsageError(f"native-config-invalid: {message}")

    def _validate(self, config: Any, *, generated: bool) -> None:
        option = config.option
        manager = getattr(config, "pluginmanager", None)
        if manager is not None:
            try:
                loaded = manager.list_name_plugin()
            except (AttributeError, TypeError):
                loaded = ()
            for name, plugin in loaded:
                module = getattr(plugin, "__name__", "")
                if name in {"xdist", "pytest-xdist"} or str(module).startswith("xdist"):
                    self._refuse("pytest xdist is not owned by the serial grant")
        if any(getattr(option, name, None) for name in ("px", "rsyncdir", "looponfail")):
            self._refuse("remote/proxy or loop-on-fail pytest execution is unsupported")
        try:
            rsyncdirs = config.getini("rsyncdirs")
        except ValueError:  # Option is not registered without xdist.
            rsyncdirs = []
        if rsyncdirs:
            self._refuse("pytest rsync configuration is unsupported")
        tx = list(getattr(option, "tx", None) or [])
        expected = ["popen"] * self.workers if generated and self.workers > 1 else []
        if tx != expected:
            self._refuse("explicit or unexpected pytest transports are unsupported")
        configured = getattr(option, "numprocesses", None)
        if self.workers == 1 and configured not in (None, 0, "0"):
            self._refuse("serial grant cannot use xdist")
        if self.workers > 1 and str(configured) != str(self.workers):
            self._refuse("xdist worker count differs from admission grant")
        maximum = getattr(option, "maxprocesses", None)
        if maximum is not None and str(maximum) != str(self.workers):
            self._refuse("xdist maximum worker count differs from admission grant")
        if os.environ.get("PTEST_EXECUTION") == "full":
            narrowing = ("keyword", "markexpr", "deselect", "lf", "failedfirst",
                         "stepwise", "stepwise_skip", "testmon", "ignore",
                         "ignore_glob", "maxfail", "collectonly", "pyargs",
                         "setuponly", "setupplan", "showfixtures",
                         "show_fixtures_per_test", "markers", "cacheshow",
                         "help", "version")
            if any(getattr(option, name, None) for name in narrowing):
                self._refuse("full pytest plans cannot narrow the inventory")
            try:
                roots = json.loads(os.environ.get("PTEST_TEST_ROOTS", "null"))
            except ValueError:
                roots = None
            if not isinstance(roots, list) or not roots or config.args != roots:
                self._refuse("full pytest inventory differs from configured roots")

    def pytest_cmdline_main(self, config: Any) -> Any:
        """Wrap before xdist replaces explicit tx with local popen transports."""
        self._validate(config, generated=False)
        return (yield)

    def pytest_configure(self, config: Any) -> None:
        self._validate(config, generated=True)

    def pytest_xdist_setupnodes(self, config: Any, specs: Any) -> None:
        """Check the final gateway boundary, before xdist creates any worker."""
        self._validate(config, generated=True)
        expected_keys = {"_spec", "env", "execmodel", "popen", "id"}
        worker_ids = set()
        for spec in specs:
            try:
                attributes = vars(spec)
            except TypeError:
                attributes = {}
            worker_id = attributes.get("id")
            if (set(attributes) != expected_keys
                    or attributes.get("_spec") != "execmodel=main_thread_only//popen"
                    or attributes.get("env") != {}
                    or attributes.get("execmodel") != "main_thread_only"
                    or attributes.get("popen") is not True
                    or not isinstance(worker_id, str)
                    or not worker_id.startswith("gw")
                    or not worker_id[2:].isdigit()):
                self._refuse("xdist gateway specifications differ from admission grant")
            worker_ids.add(worker_id)
        if len(specs) != self.workers or len(worker_ids) != self.workers:
            self._refuse("xdist gateway specifications differ from admission grant")


def run(argv: list[str] | tuple[str, ...] | None = None) -> int:
    """Run pytest natively after validating the immutable bridge descriptor."""
    _protocol()
    workers = _workers()
    _python_version()
    binding = _report_binding()
    runtime = "unknown"
    native_exit: int | None = None
    bridge_exit = 70
    complete = False
    problem: str | None = "bridge-refused" if binding is not None else None
    if argv is None:
        argv = tuple(sys.argv[1:])
    if not isinstance(argv, (list, tuple)) or not all(isinstance(item, str) for item in argv):
        _fail("pytest argv must be a string array")
    # Executing this file must have the same cwd imports as `python -m pytest`.
    sys.path[:1] = [os.getcwd()]
    try:
        try:
            import pytest
        except ImportError:
            _fail("pytest is unavailable in the selected interpreter")
        runtime = str(pytest.__version__)
        if runtime not in {"8.4.2", "9.0.3", "9.1.0", "9.1.1"}:
            _fail("pytest version is outside the candidate table", "unsupported-capability")
        # Mark hooks only after the selected interpreter and pytest have been checked.
        pytest.hookimpl(wrapper=True, tryfirst=True)(OwnedPlugin.pytest_cmdline_main)
        pytest.hookimpl(tryfirst=True)(OwnedPlugin.pytest_configure)
        pytest.hookimpl(tryfirst=True, optionalhook=True)(OwnedPlugin.pytest_xdist_setupnodes)
        native_exit = int(pytest.main(list(argv), plugins=[OwnedPlugin(workers)]))
        bridge_exit = native_exit
        if native_exit == 4:
            # Pytest's usage/configuration result is a bridge refusal, not a
            # native test failure.  The child exit remains visible to guard.
            return native_exit
        complete = True
        problem = "native-failure" if native_exit else None
        return native_exit
    except BridgeRefusal:
        bridge_exit = 4
        raise
    finally:
        if binding is not None:
            try:
                _write_report(binding[0], binding[1], runtime=runtime,
                              native_exit=native_exit if complete else None,
                              bridge_exit=bridge_exit, complete=complete,
                              problem=problem if complete else "bridge-refused")
            except (BridgeRefusal, OSError):
                # The executor treats an absent or malformed report as incomplete.
                pass


if __name__ == "__main__":
    try:
        raise SystemExit(run())
    except BridgeRefusal as error:
        _refusal_marker(error.code, error.message)
        raise SystemExit(4)
