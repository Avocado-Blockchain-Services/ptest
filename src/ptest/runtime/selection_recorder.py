"""Dependency recorder for ptest dynamic selection (T4).

Stdlib only: the bridge imports this module without any ptest package, so
every literal it needs is duplicated here (pinned equal to contracts by
``tests/ng/test_selection_recorder.py``) and the predicates are stdlib
copies of the ``selection_*`` contracts helpers.

Recording uses one ``sys.monitoring`` tool (the first free id among 3, 4,
2) with ``PY_START`` enabled via ``set_local_events`` only on project code
objects, plus one audit hook. There are no global events (N13). Project
code is discovered through the audit ``exec`` event (``co_consts`` walked
recursively) plus one ``sys.modules`` walk at activation. The callback
stores the code in the current context and returns ``DISABLE``;
``restart_events()`` runs on every context switch. Both the hook and the
callback never raise (N10).

Contexts: one test context per ``pytest_runtest_protocol`` (setup, call
and teardown, including function-scoped fixtures), one fixture context per
higher-scope fixture keyed ``(baseid, argname, scope)``, and the ambient
context for everything else. The hook and callback are permanent no-ops
when the recorder is not active.

One dependency file per process is written next to the native report at
session end (``<report>.deps-<pid>``, O_EXCL|O_NOFOLLOW 0600). A crashed
worker leaves no file, so its tests stay unrecorded.

Integration boundary: real serial and xdist twins run the bridge against
``tests/ng/fixtures/selection_project`` in
``tests/ng/test_selection_bridge_subprocess.py``; each twin carries an
explicit timeout of at most 60 s.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import types
from pathlib import Path

# -- Duplicated literals (pinned equal to contracts; see module docstring).
_DEPS_INFIX = ".deps-"
_DEPS_FORMAT = "ptest-selection-deps-v1"
_DEPS_MAX_BYTES = 64 * 1024 * 1024
_DESELECT_SUFFIX = ".deselect"
_DESELECT_FORMAT = "ptest-selection-deselect-v1"
_DESELECT_MAX_BYTES = 16 * 1024 * 1024
_RECORD_ENV = "PTEST_SELECTION_RECORD"
_DESELECT_ENV = "PTEST_SELECTION_DESELECT"
_TOOL_IDS = (3, 4, 2)
_TOOL_NAME = "ptest-selection"
_CONTEXT_MAX_FUNCTIONS = 100000
_CONTEXT_MAX_DATA = 4096
# Frames searched for the project caller that owns a first execution.
_OWNER_MAX_DEPTH = 256
# Frozen literal pinned to contracts (SELECTION_DATA_MAX_BYTES): the bound the
# engine applies to its own digest reads. The recorder records data paths
# regardless of size and never uses this as a drop filter (spec N1).
_DATA_MAX_BYTES = 16 * 1024 * 1024
_SKIP_DIRS = frozenset({
    "node_modules", "__pycache__", "site-packages", "build", "dist", "venv",
    ".tox", "htmlcov",
})
_OUTPUT_DIRS = frozenset({
    "build", "dist", "node_modules", ".venv", "venv", "__pycache__",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", "htmlcov",
    ".hypothesis",
})
_LAUNCHER_PATTERN = (
    r"(?:python|pypy)[0-9.]*(?:\.exe)?|uv|uvx|pytest|py\.test|ptest"
    r"|sh|bash|dash|zsh|fish|env|nohup|timeout|xargs"
)
_LAUNCHER_RE = re.compile(_LAUNCHER_PATTERN)

# Worst-phase outcome order: error > failed > xpassed > passed >
# xfailed > skipped. A node with no final report stays "unknown".
_OUTCOME_RANK = {
    "unknown": -1,
    "skipped": 0,
    "xfailed": 1,
    "passed": 2,
    "xpassed": 3,
    "failed": 4,
    "error": 5,
}

_INACTIVE_NO_TOOL = "no free sys.monitoring tool id"
_INACTIVE_NO_START = "recorder could not start"

_FORCE_OPAQUE_EVENTS = frozenset({"os.system", "os.fork", "os.forkpty"})

# Bound on the data-path memo so adversarial open storms cannot grow it.
_MAX_PATH_MEMO = 32768


def record_active() -> bool:
    """True when the executor enabled selection recording for this process."""
    try:
        return os.environ.get(_RECORD_ENV) == "1"
    except Exception:
        return False


def _relpath_ok(path: object) -> bool:
    if not isinstance(path, str) or not path or "\x00" in path or "\\" in path:
        return False
    if path.startswith("/") or re.match(r"^[A-Za-z]:", path):
        return False
    try:
        if len(path.encode("utf-8")) > 4096:
            return False
    except UnicodeEncodeError:
        return False
    return all(part not in ("", ".", "..") for part in path.split("/"))


def _code_path(path: object) -> bool:
    """Stdlib copy of the contracts code-path rule."""
    if not _relpath_ok(path) or not path.endswith(".py"):
        return False
    parts = path.split("/")
    return not any(part.startswith(".") or part in _SKIP_DIRS
                   or part.endswith(".egg-info") for part in parts[:-1]) \
        and not parts[-1].startswith(".")


def _data_path(path: object) -> bool:
    """Stdlib copy of the contracts data-path rule."""
    if not _relpath_ok(path):
        return False
    lowered = path.lower()
    if lowered.endswith((".py", ".pyc", ".pyo")):
        return False
    return not any(part.startswith(".") or part in _SKIP_DIRS
                   or part in _OUTPUT_DIRS or part.endswith(".egg-info")
                   for part in path.split("/"))


def _normalize_qualname(qualname: object) -> str | None:
    """Stdlib copy of the contracts qualname rule; None is module-level."""
    if (not isinstance(qualname, str) or not qualname or "\x00" in qualname
            or len(qualname) > 1024):
        return None
    kept: list[str] = []
    for part in qualname.split("."):
        if not part or part.startswith("<"):
            break
        kept.append(part)
    return ".".join(kept) or None


def _launcher_opaque(basename: object) -> bool:
    """True when a resolved executable basename can run project Python."""
    if not isinstance(basename, str) or not basename:
        return True
    try:
        return _LAUNCHER_RE.fullmatch(basename) is not None
    except Exception:
        return True


def _is_within(path: str, root: str) -> bool:
    try:
        rel = os.path.relpath(path, root)
    except (OSError, ValueError):
        return False
    return rel != ".." and not rel.startswith(".." + os.sep) \
        and not os.path.isabs(rel)


def _spawn_opaque(executable: object, argv: object, checkout: str) -> bool:
    """True when spawning ``executable``/``argv[0]`` may run project Python."""
    candidates: list[str] = []
    for value in (executable,
                  argv[0] if isinstance(argv, (list, tuple)) and argv else None):
        if isinstance(value, (str, bytes, os.PathLike)):
            try:
                text = os.fspath(value)
            except Exception:
                return True
            if isinstance(text, bytes):
                try:
                    text = text.decode("utf-8", errors="strict")
                except UnicodeDecodeError:
                    return True
            if text and "\x00" not in text and text not in candidates:
                candidates.append(text)
    if not candidates:
        return True
    try:
        prefix_real = os.path.realpath(sys.prefix)
        self_real = os.path.realpath(sys.executable)
    except Exception:
        return True
    for candidate in candidates:
        try:
            if os.path.dirname(candidate):
                resolved = candidate
            else:
                resolved = shutil.which(candidate)
            if not resolved:
                return True
            real = os.path.realpath(resolved)
        except Exception:
            return True
        if real == self_real:
            return True
        if _is_within(real, checkout) or _is_within(real, prefix_real):
            return True
        if _launcher_opaque(os.path.basename(real)):
            return True
    return False


# execnet's popen bootstrap: how pytest-xdist starts its worker interpreters.
_XDIST_BOOTSTRAP = "import sys;exec(eval(sys.stdin.readline()))"


def _is_xdist_gateway(executable: object, argv: object) -> bool:
    """True for the controller starting an xdist worker of this interpreter.

    Each such worker records its own deps file and ingest requires one
    per worker, so the spawn is accounted for and does not hide project
    code. Only the exact execnet argv of the running interpreter matches.
    """
    try:
        if not isinstance(argv, (list, tuple)) or len(argv) < 4:
            return False
        tail = []
        for item in argv[-3:]:
            if isinstance(item, bytes):
                item = item.decode("utf-8", "strict")
            if not isinstance(item, str):
                return False
            tail.append(item)
        if tail != ["-u", "-c", _XDIST_BOOTSTRAP]:
            return False
        program = executable if executable is not None else argv[0]
        if isinstance(program, bytes):
            program = program.decode("utf-8", "strict")
        if not isinstance(program, str) or not program:
            return False
        return os.path.realpath(program) == os.path.realpath(sys.executable)
    except Exception:
        return False


def _force_opaque_event(event: object) -> bool:
    """Events that always mark the context opaque (fork/system)."""
    try:
        return event in _FORCE_OPAQUE_EVENTS
    except Exception:
        return False


def _forkserver_socket_path() -> str | None:
    """Live forkserver socket path, or None (never imports; N10/N11).

    ``multiprocessing`` stores the address on the module's ``ForkServer``
    singleton (``forkserver._forkserver._forkserver_address``) when the
    server starts. Reading it from ``sys.modules`` avoids importing
    anything inside the audit hook; a missing module or attribute simply
    means no forkserver is running in this process. Abstract-namespace
    addresses (leading NUL) compare like any other string.
    """
    try:
        module = sys.modules.get("multiprocessing.forkserver")
        server = getattr(module, "_forkserver", None) \
            if module is not None else None
        address = getattr(server, "_forkserver_address", None) \
            if server is not None else None
    except Exception:
        return None
    if isinstance(address, bytes):
        try:
            address = address.decode("utf-8", "surrogateescape")
        except Exception:
            return None
    if isinstance(address, str) and address:
        return address
    return None


def _is_forkserver_connect(args: object) -> bool:
    """True when ``socket.connect`` args dial the live forkserver (N3).

    Once the forkserver runs, worker requests raise no spawn audit event
    in this process — only a Unix-socket connect to the server. Project
    functions then execute in forked workers, unrecorded, so the context
    must go opaque. Only the exact live server address matches; every
    other socket use is unaffected.
    """
    try:
        if not isinstance(args, (tuple, list)) or len(args) < 2:
            return False
        address = args[1]
        if isinstance(address, bytes):
            address = address.decode("utf-8", "surrogateescape")
        if not isinstance(address, str) or not address:
            return False
        server = _forkserver_socket_path()
        return server is not None and address == server
    except Exception:
        return False


def _fork_exec_arg(value: object) -> object:
    """Unwrap one ``_posixsubprocess.fork_exec`` executable/argv slot.

    The executable arrives as a one-element bytes list; argv as a list.
    """
    try:
        if isinstance(value, (list, tuple)) and len(value) == 1:
            return value[0]
    except Exception:
        pass
    return value


def _is_pure_write(mode: object, flags: object) -> bool:
    """True for opens that cannot observe file content."""
    try:
        if isinstance(mode, str):
            if ("w" in mode or "x" in mode) and "+" not in mode:
                return True
        if isinstance(flags, int) and flags & os.O_ACCMODE == os.O_WRONLY:
            return True
    except Exception:
        pass
    return False


def _opens_directory(flags: object) -> bool:
    """``os.open(..., O_DIRECTORY)``: a directory is never a data read."""
    try:
        return (isinstance(flags, int)
                and bool(flags & getattr(os, "O_DIRECTORY", 0)))
    except Exception:
        return False


def _dir_fd_relative(path: object, mode: object) -> bool:
    """A bare-name ``os.open`` whose name does not exist under the cwd.

    The ``open`` audit event omits ``dir_fd``, so a name opened relative
    to a directory descriptor (``shutil.rmtree``'s fd walk, run by pytest
    when it cleans old temp dirs) would resolve against the cwd and land
    as a phantom data path. ``os.open`` reports ``mode`` as None; a
    relative name that exists under the cwd is still recorded.
    """
    try:
        if mode is not None:
            return False
        if isinstance(path, bytes):
            path = os.fsdecode(path)
        if not isinstance(path, str) or not path or os.path.isabs(path):
            return False
        return not os.path.lexists(path)
    except Exception:
        return False


class _Ctx:
    """What one test, fixture or ambient context executed."""

    __slots__ = ("functions", "modules", "data", "opaque")

    def __init__(self, *, opaque: bool = False) -> None:
        self.functions: set[tuple[str, str, int]] = set()
        self.modules: set[str] = set()
        self.data: set[str] = set()
        self.opaque = opaque

    def merge(self, other: "_Ctx") -> None:
        self.functions |= other.functions
        self.modules |= other.modules
        self.data |= other.data
        self.opaque = self.opaque or other.opaque


class _Node:
    """One test's merged contexts, outcome and fixture keys."""

    __slots__ = ("nodeid", "outcome", "fixtures", "ctx")

    def __init__(self, nodeid: str) -> None:
        self.nodeid = nodeid
        self.outcome = "unknown"
        self.fixtures: list[tuple[str, str, str]] = []
        self.ctx = _Ctx()


class Recorder:
    """Per-process selection recorder (one instance per bridge process)."""

    def __init__(self, *, checkout_root: str, run_id: str,
                 report_path: str, role: str,
                 worker_id: str | None = None) -> None:
        try:
            self._checkout = os.path.realpath(checkout_root)
        except Exception:
            self._checkout = ""
        self.run_id = run_id if isinstance(run_id, str) else ""
        self._report_path = report_path if isinstance(report_path, str) else ""
        self._role = role if role in ("controller", "worker") else "controller"
        self._worker_id = worker_id if isinstance(worker_id, str) else None
        self.recording = False
        self.inactive_reason: str | None = None
        self.tamper = False
        self.overflow = False
        self._deselect_status = "none"
        self._deselected = 0
        self._workers: list[str] = []
        self._tool: int | None = None
        self._active = False
        self._tampered = False
        self._in_hook = False
        self._ambient = _Ctx()
        self._current = self._ambient
        self._stack: list[_Ctx] = []
        self._open_node: _Node | None = None
        self._nodes: dict[str, _Node] = {}
        self._fixtures: dict[tuple[str, str, str], _Ctx] = {}
        # Keyed by id(), never by the code object: code objects compare
        # structurally, so distinct files with identical bytecode (empty
        # __init__ modules included) would otherwise collide. The stored
        # reference anchors the id against reuse. Only project code is
        # cached; anything else resolves again on its next sighting.
        self._code_cache: dict[int, tuple[object, tuple[str, str | None, int]]] = {}
        self._path_memo: dict[str, str | None] = {}
        # Lazy initialisation: the first sighting in this process of each
        # project function (entry), module ("M", rel) and data file
        # ("D", rel), with the context that saw it and the project code
        # that caused it. State built once and reused by later tests is a
        # dependency of every test that runs the code that built it.
        self._first: dict[object, tuple[object, object]] = {}
        self._seen_codes: set[int] = set()
        self._test_ctx: _Ctx | None = None
        self._fixture_owners: list[object] = []
        self._file_project: dict[str, bool] = {}
        self._own_files: frozenset[str] = frozenset()

    def _lookup_code(
            self, code: object) -> tuple[str, str | None, int] | None:
        """Cached project identity of one code object, else None."""
        try:
            slot = self._code_cache.get(id(code))
        except Exception:
            return None
        if slot is not None and slot[0] is code:
            return slot[1]
        return None

    def _store_code(self, code: object,
                    entry: tuple[str, str | None, int] | None) -> None:
        """Cache a project-code resolution (drops for anything else)."""
        try:
            if entry is not None:
                self._code_cache[id(code)] = (code, entry)
        except Exception:
            pass

    # -- activation ------------------------------------------------------

    def activate(self) -> bool:
        """Claim a monitoring tool id and install the hooks. Never raises."""
        try:
            if self._active:
                return self.recording
            if sys.version_info[:2] < (3, 12):
                major, minor = sys.version_info[:2]
                self.inactive_reason = (
                    f"Python {major}.{minor} has no sys.monitoring")
                return False
            monitoring = sys.monitoring
            tool: int | None = None
            for candidate in _TOOL_IDS:
                try:
                    if monitoring.get_tool(candidate) is None:
                        tool = candidate
                        break
                except Exception:
                    continue
            if tool is None:
                self.inactive_reason = _INACTIVE_NO_TOOL
                return False
            try:
                monitoring.use_tool_id(tool, _TOOL_NAME)
            except Exception:
                self.inactive_reason = _INACTIVE_NO_TOOL
                return False
            self._tool = tool
            try:
                monitoring.register_callback(
                    tool, monitoring.events.PY_START, self._py_start)
                sys.addaudithook(self._audit)
            except Exception:
                self._release_tool()
                self.inactive_reason = _INACTIVE_NO_START
                return False
            self._walk_modules()
            self._active = True
            self.recording = True
            return True
        except Exception:
            try:
                self._release_tool()
            except Exception:
                pass
            self.inactive_reason = _INACTIVE_NO_START
            return False

    def deactivate(self) -> None:
        """Release the tool id; the audit hook stays a permanent no-op."""
        self._active = False
        try:
            self._release_tool()
        except Exception:
            pass

    def _release_tool(self) -> None:
        tool, self._tool = self._tool, None
        if tool is None:
            return
        try:
            monitoring = sys.monitoring
            try:
                monitoring.register_callback(
                    tool, monitoring.events.PY_START, None)
            except Exception:
                pass
            monitoring.free_tool_id(tool)
        except Exception:
            pass

    def _walk_modules(self) -> None:
        """Arm live project code imported before activation (best effort).

        The loader's ``get_code`` unmarshals a fresh copy, so arming it
        leaves the code objects module functions actually run
        (``fn.__code__``) dark. Walk live objects reachable from each
        project module instead: functions via ``__code__``, classes via
        their members. Never touches loaders (no ``.pyc`` writes).
        """
        try:
            modules = list(sys.modules.values())
        except Exception:
            return
        seen: set[int] = set()
        for module in modules:
            try:
                if getattr(module, "__name__", "") in (
                        __name__, "ptest.runtime.pytest_bridge",
                        "pytest_bridge"):
                    continue
                if not self._module_is_project(module):
                    continue
                self._arm_live(module, seen)
            except Exception:
                continue

    def _module_is_project(self, module: object) -> bool:
        """True when ``module.__file__`` is project code in the checkout."""
        try:
            filename = getattr(module, "__file__", None)
            if (not isinstance(filename, str) or not filename
                    or "\x00" in filename or filename.startswith("<")):
                return False
            try:
                absolute = os.path.abspath(filename)
                real = os.path.realpath(absolute)
            except (OSError, ValueError):
                return False
            if real != absolute:
                return False
            try:
                rel = os.path.relpath(real, self._checkout)
            except (OSError, ValueError):
                return False
            return _code_path(rel.replace(os.sep, "/"))
        except Exception:
            return False

    def _arm_live(self, value: object, seen: set[int]) -> None:
        """Arm live functions/classes reachable from ``value`` (best effort).

        ``vars()`` reads the namespace without invoking descriptors, so
        this has no import or execution side effects. Anything that is
        not project code is filtered by ``_discover`` itself.

        Decorated functions hide their original code behind the
        wrapper: ``functools.wraps``/``lru_cache``/``contextmanager``
        wrappers expose it as ``__wrapped__``, bare decorators keep it
        in ``__closure__`` cells, and ``lru_cache`` wrappers are not
        functions at all. Containers (dict/list/tuple) in a namespace
        may hold further callables. All of these are followed so that
        pre-activation imports record the same code as the post-
        activation audit ``exec`` walk.
        """
        try:
            stack = [value]
            while stack:
                current = stack.pop()
                try:
                    if id(current) in seen:
                        continue
                    seen.add(id(current))
                    if isinstance(current, types.FunctionType):
                        code = getattr(current, "__code__", None)
                        if isinstance(code, types.CodeType):
                            self._discover(code)
                        try:
                            wrapped = getattr(
                                current, "__wrapped__", None)
                        except Exception:
                            wrapped = None
                        if wrapped is not None:
                            stack.append(wrapped)
                        try:
                            closure = getattr(
                                current, "__closure__", None)
                        except Exception:
                            closure = None
                        if closure:
                            try:
                                cells = list(closure)
                            except TypeError:
                                cells = []
                            for cell in cells:
                                try:
                                    stack.append(cell.cell_contents)
                                except Exception:
                                    pass
                        continue
                    if isinstance(current, types.MethodType):
                        try:
                            stack.append(current.__func__)
                        except Exception:
                            pass
                        continue
                    if isinstance(current, (staticmethod, classmethod)):
                        try:
                            stack.append(current.__func__)
                        except Exception:
                            pass
                        continue
                    if isinstance(current, property):
                        for accessor in (current.fget, current.fset,
                                         current.fdel):
                            if isinstance(accessor, types.FunctionType):
                                stack.append(accessor)
                        continue
                    if isinstance(current, dict):
                        try:
                            stack.extend(current.values())
                        except Exception:
                            pass
                        continue
                    if isinstance(current, (list, tuple)):
                        try:
                            stack.extend(current)
                        except Exception:
                            pass
                        continue
                    namespace = None
                    if isinstance(current, types.ModuleType):
                        # Containers may smuggle in non-project modules,
                        # so re-check here (nested imports are already
                        # project-checked in the member loop below).
                        try:
                            if not self._module_is_project(current):
                                continue
                        except Exception:
                            continue
                        try:
                            namespace = vars(current)
                        except TypeError:
                            namespace = None
                    elif isinstance(current, type):
                        try:
                            namespace = vars(current)
                        except TypeError:
                            namespace = None
                    else:
                        # A non-function member carrying ``__wrapped__``
                        # (``functools.lru_cache`` wrappers and friends):
                        # prefer the instance dict so no descriptor fires.
                        try:
                            own = vars(current)
                        except TypeError:
                            own = None
                        if isinstance(own, dict):
                            if "__wrapped__" in own:
                                stack.append(own["__wrapped__"])
                        else:
                            try:
                                wrapped = getattr(
                                    current, "__wrapped__", None)
                            except Exception:
                                wrapped = None
                            if wrapped is not None:
                                stack.append(wrapped)
                        continue
                    if namespace is None:
                        continue
                    try:
                        members = list(namespace.values())
                    except Exception:
                        continue
                    for member in members:
                        if isinstance(member, types.ModuleType):
                            # Descend only into project modules; an
                            # imported stdlib module's namespace is
                            # walked nowhere.
                            try:
                                if self._module_is_project(member):
                                    stack.append(member)
                            except Exception:
                                pass
                        else:
                            stack.append(member)
                except Exception:
                    continue
        except Exception:
            pass

    # -- contexts (driven by the bridge) ----------------------------------

    def _new_ctx(self) -> _Ctx:
        return _Ctx(opaque=self._tampered or not self.recording)

    def _switch(self) -> None:
        """Ownership check plus restart on every context switch."""
        try:
            if self._tool is None or not self._active:
                return
            monitoring = sys.monitoring
            try:
                if monitoring.get_tool(self._tool) != _TOOL_NAME:
                    self._mark_tampered()
                    return
            except Exception:
                return
            try:
                monitoring.restart_events()
            except Exception:
                pass
        except Exception:
            pass

    def _mark_tampered(self) -> None:
        self.tamper = True
        self._tampered = True
        self.recording = False
        self._active = False
        try:
            self._current.opaque = True
        except Exception:
            pass

    def enter_test(self, nodeid: str) -> None:
        """Open a test context covering setup, call and teardown."""
        try:
            if not isinstance(nodeid, str) or not nodeid:
                return
            self._switch()
            self._stack.append(self._current)
            self._current = self._new_ctx()
            self._test_ctx = self._current
            node = self._nodes.get(nodeid)
            if node is None:
                node = _Node(nodeid)
                self._nodes[nodeid] = node
            self._open_node = node
        except Exception:
            pass

    def exit_test(self, item: object = None) -> None:
        """Merge the test context into its node and restore the caller."""
        try:
            keys = self.fixture_keys_for_item(item) if item is not None else []
            node, self._open_node = self._open_node, None
            self._test_ctx = None
            self._fixture_owners.clear()
            if node is not None:
                for key in keys:
                    if key not in node.fixtures:
                        node.fixtures.append(key)
                node.ctx.merge(self._current)
            if self._stack:
                self._current = self._stack.pop()
            else:
                self._current = self._ambient
            self._switch()
        except Exception:
            pass

    def enter_fixture(self, key: tuple[str, str, str]) -> None:
        """Open the fixture context for one higher-scope fixture setup."""
        try:
            self._switch()
            self._stack.append(self._current)
            fixture = self._fixtures.get(key)
            if fixture is None:
                fixture = self._new_ctx()
                self._fixtures[key] = fixture
            self._current = fixture
        except Exception:
            pass

    def exit_fixture(self) -> None:
        """Restore the context interrupted by a fixture setup."""
        try:
            if self._stack:
                self._current = self._stack.pop()
            else:
                self._current = self._ambient
            self._switch()
        except Exception:
            pass

    def enter_function_fixture(self, fixturedef: object) -> None:
        """Note a function-scoped fixture setup as the owner of code that
        runs without a project caller on its stack (another thread)."""
        try:
            func = getattr(fixturedef, "func", None)
            for _ in range(16):
                wrapped = getattr(func, "__wrapped__", None)
                if wrapped is None:
                    break
                func = wrapped
            code = getattr(func, "__code__", None)
            owner = None
            if isinstance(code, types.CodeType):
                entry = self._frame_entry(code)
                if entry is not None and entry[1] is not None:
                    owner = entry
            self._fixture_owners.append(owner)
        except Exception:
            pass

    def exit_function_fixture(self) -> None:
        try:
            if self._fixture_owners:
                self._fixture_owners.pop()
        except Exception:
            pass

    @staticmethod
    def fixture_key(fixturedef: object) -> tuple[str, str, str] | None:
        """The record key of one fixture def; None for function scope."""
        try:
            scope = getattr(fixturedef, "scope", "function")
            scope = str(scope) if scope is not None else "function"
            if scope == "function":
                return None
            baseid = getattr(fixturedef, "baseid", "")
            argname = getattr(fixturedef, "argname", "")
            if not isinstance(baseid, str) or not isinstance(argname, str):
                return None
            if not argname or scope not in ("class", "module", "package",
                                           "session"):
                return None
            return (baseid, argname, scope)
        except Exception:
            return None

    @staticmethod
    def fixture_keys_for_item(item: object) -> list[tuple[str, str, str]]:
        """Higher-scope fixture keys one test uses (best effort)."""
        found: list[tuple[str, str, str]] = []
        try:
            if item is None:
                return found
            names = getattr(item, "fixturenames", ())
            if not isinstance(names, (list, tuple)):
                return found
            defs: dict[object, object] = {}
            try:
                info = getattr(item, "_fixtureinfo", None)
                table = getattr(info, "name2fixturedefs", None)
                if isinstance(table, dict):
                    for name, entries in table.items():
                        if isinstance(entries, (list, tuple)) and entries:
                            defs[name] = entries[-1]
                        elif entries is not None:
                            defs[name] = entries
            except Exception:
                pass
            try:
                request = getattr(item, "_request", None)
                extra = getattr(request, "_fixture_defs", None)
                if isinstance(extra, dict):
                    for name, entry in extra.items():
                        defs.setdefault(name, entry)
            except Exception:
                pass
            for name in names:
                if not isinstance(name, str):
                    continue
                key = Recorder.fixture_key(defs.get(name))
                if key is not None and key not in found:
                    found.append(key)
        except Exception:
            pass
        return found

    # -- outcomes ----------------------------------------------------------

    @staticmethod
    def _classify(report: object) -> str | None:
        """Worst-phase outcome of one runtest report; None when not final."""
        try:
            when = str(getattr(report, "when", "") or "")
            outcome = str(getattr(report, "outcome", "") or "")
            failed = bool(getattr(report, "failed", False))
            skipped = bool(getattr(report, "skipped", False))
            wasxfail = bool(getattr(report, "wasxfail", False))
        except Exception:
            return None
        try:
            if when == "setup":
                if failed:
                    return "error"
                return "skipped" if skipped else None
            if when == "teardown":
                return "error" if failed else None
            if when != "call":
                return None
            if wasxfail:
                if outcome == "passed":
                    return "xpassed"
                return "xfailed" if outcome == "skipped" else "failed"
            if outcome in ("passed", "failed", "skipped"):
                return outcome
            return None
        except Exception:
            return None

    def observe(self, report: object) -> None:
        """Fold one runtest report into its node's outcome. Never raises."""
        try:
            nodeid = getattr(report, "nodeid", "")
            if not isinstance(nodeid, str) or not nodeid:
                return
            node = self._nodes.get(nodeid)
            if node is None:
                # Forwarded reports (the xdist controller) never entered a
                # test context in this process: ignore them.
                return
            outcome = self._classify(report)
            if outcome is None:
                return
            if _OUTCOME_RANK[outcome] > _OUTCOME_RANK[node.outcome]:
                node.outcome = outcome
        except Exception:
            pass

    # -- evidence ----------------------------------------------------------

    def _record_function(self, entry: tuple[str, str, int]) -> None:
        try:
            ctx = self._current
            if entry in ctx.functions:
                return
            if len(ctx.functions) >= _CONTEXT_MAX_FUNCTIONS:
                ctx.opaque = True
                return
            ctx.functions.add(entry)
        except Exception:
            pass

    def _record_data(self, rel: str) -> None:
        try:
            ctx = self._current
            if rel in ctx.data:
                return
            if len(ctx.data) >= _CONTEXT_MAX_DATA:
                ctx.opaque = True
                return
            ctx.data.add(rel)
        except Exception:
            pass

    def _py_start(self, code: object, _offset: int) -> object:
        """Monitoring callback: file one project code object, disarm."""
        try:
            monitoring = sys.monitoring
            if not self._active or self._tampered:
                return monitoring.DISABLE
            entry = self._lookup_code(code)
            if entry is None:
                entry = self._resolve_code(code)
                self._store_code(code, entry)
            if entry is not None:
                rel, qualname, firstlineno = entry
                if id(code) not in self._seen_codes:
                    self._first_sighting(code, entry)
                if qualname is None:
                    try:
                        self._current.modules.add(rel)
                    except Exception:
                        pass
                else:
                    self._record_function((rel, qualname, firstlineno))
            return monitoring.DISABLE
        except Exception:
            try:
                return sys.monitoring.DISABLE
            except Exception:
                return None

    # -- lazy initialisation ------------------------------------------------

    def _token(self) -> object:
        """The context a sighting lands in: the open node or a context."""
        current = self._current
        if current is self._test_ctx and self._open_node is not None:
            return self._open_node
        return current

    def _first_sighting(self, code: object,
                        entry: tuple[str, str | None, int]) -> None:
        """First run of one project code object in this process."""
        try:
            self._seen_codes.add(id(code))
            rel, qualname, _ = entry
            key = ("M", rel) if qualname is None else entry
            if key in self._first:
                return
            try:
                frame = sys._getframe(2).f_back
            except Exception:
                frame = None
            self._note_first(key, frame)
        except Exception:
            pass

    def _note_first(self, key: object, frame: object) -> None:
        """Record where ``key`` was first seen and the code that caused it:
        the nearest project frame from ``frame`` up, else the function
        fixture being set up. Ambient sightings need no owner."""
        try:
            token = self._token()
            owner = None
            if token is not self._ambient:
                depth = 0
                while frame is not None and depth < _OWNER_MAX_DEPTH:
                    entry = self._frame_entry(frame.f_code)
                    if entry is not None:
                        owner = (("M", entry[0]) if entry[1] is None
                                 else entry)
                        break
                    frame = frame.f_back
                    depth += 1
                if owner is None and self._fixture_owners:
                    owner = self._fixture_owners[-1]
            self._first[key] = (token, owner)
        except Exception:
            pass

    def _frame_entry(self, code: object) -> tuple[str, str | None, int] | None:
        """Project identity of a frame's code (memoised per filename)."""
        try:
            entry = self._lookup_code(code)
            if entry is not None:
                return entry
            filename = getattr(code, "co_filename", None)
            if not isinstance(filename, str):
                return None
            known = self._file_project.get(filename)
            if known is False:
                return None
            if known is None and filename in self._own_files_set():
                self._file_project[filename] = False
                return None
            entry = self._resolve_code(code)
            self._file_project[filename] = entry is not None
            self._store_code(code, entry)
            return entry
        except Exception:
            return None

    def _own_files_set(self) -> frozenset[str]:
        """Source files of the recorder and the bridge (never owners)."""
        if not self._own_files:
            names = set()
            for name in (__name__, "ptest.runtime.pytest_bridge",
                         "pytest_bridge"):
                filename = getattr(sys.modules.get(name), "__file__", None)
                if isinstance(filename, str):
                    names.add(filename)
            names.add(__file__)
            self._own_files = frozenset(names)
        return self._own_files

    def _apply_lazy_owners(self) -> None:
        """Give every context that ran an owner the state it built.

        A function or data file first seen in one context, seen in no
        other, and caused by code first run in that same context, is
        lazily built state: later contexts that run the owner reuse it
        instead of rebuilding it. A module body runs once per process, so
        a lazily imported module always belongs to its importer.
        """
        try:
            contexts = [*self._fixtures.values(),
                        *(node.ctx for node in self._nodes.values())]
            seen_in: dict[object, int] = {}
            for ctx in (self._ambient, *contexts):
                for entry in ctx.functions:
                    seen_in[entry] = seen_in.get(entry, 0) + 1
                for rel in ctx.data:
                    key = ("D", rel)
                    seen_in[key] = seen_in.get(key, 0) + 1
            children: dict[object, list[object]] = {}
            for key, (token, owner) in self._first.items():
                if owner is None or token is self._ambient:
                    continue
                first_owner = self._first.get(owner)
                if first_owner is None:
                    continue
                if key[0] != "M" and (first_owner[0] is not token
                                      or seen_in.get(key, 0) != 1):
                    continue
                children.setdefault(owner, []).append(key)
            if not children:
                return
            closures: dict[object, set[object]] = {}

            def closure(root: object) -> set[object]:
                done = closures.get(root)
                if done is not None:
                    return done
                found: set[object] = set()
                stack = list(children.get(root, ()))
                while stack:
                    key = stack.pop()
                    if key in found:
                        continue
                    found.add(key)
                    stack.extend(children.get(key, ()))
                closures[root] = found
                return found

            for ctx in contexts:
                gained: set[object] = set()
                for entry in ctx.functions:
                    if entry in children:
                        gained |= closure(entry)
                for rel in ctx.modules:
                    key = ("M", rel)
                    if key in children:
                        gained |= closure(key)
                for key in gained:
                    if key[0] == "M":
                        ctx.modules.add(key[1])
                    elif key[0] == "D":
                        if key[1] not in ctx.data:
                            if len(ctx.data) >= _CONTEXT_MAX_DATA:
                                ctx.opaque = True
                            else:
                                ctx.data.add(key[1])
                    elif key not in ctx.functions:
                        if len(ctx.functions) >= _CONTEXT_MAX_FUNCTIONS:
                            ctx.opaque = True
                        else:
                            ctx.functions.add(key)
        except Exception:
            pass

    def _discover(self, code: object) -> None:
        """Arm every project code object under ``code`` for PY_START."""
        try:
            if self._tool is None:
                return
            monitoring = sys.monitoring
            event = monitoring.events.PY_START
            seen: set[int] = set()
            stack = [code]
            while stack:
                current = stack.pop()
                if not isinstance(current, types.CodeType):
                    continue
                if id(current) in seen:
                    continue
                seen.add(id(current))
                try:
                    entry = self._lookup_code(current)
                    if entry is None:
                        entry = self._resolve_code(current)
                        self._store_code(current, entry)
                    if entry is None:
                        continue
                    monitoring.set_local_events(self._tool, current, event)
                    rel, _, _ = entry
                    if (current is code
                            and getattr(code, "co_name", "") == "<module>"
                            and self._current is not self._ambient):
                        try:
                            self._current.modules.add(rel)
                        except Exception:
                            pass
                    stack.extend(getattr(current, "co_consts", ()))
                except Exception:
                    continue
        except Exception:
            pass

    def _resolve_code(
            self, code: object) -> tuple[str, str | None, int] | None:
        """Project-relative identity of one code object, else None."""
        try:
            filename = getattr(code, "co_filename", None)
            if (not isinstance(filename, str) or not filename
                    or "\x00" in filename or filename.startswith("<")):
                return None
            try:
                absolute = os.path.abspath(filename)
                real = os.path.realpath(absolute)
            except (OSError, ValueError):
                return None
            if real != absolute:
                return None
            try:
                rel = os.path.relpath(real, self._checkout)
            except (OSError, ValueError):
                return None
            rel = rel.replace(os.sep, "/")
            if not _code_path(rel):
                return None
            qualname = _normalize_qualname(
                getattr(code, "co_qualname", getattr(code, "co_name", None)))
            try:
                firstlineno = int(getattr(code, "co_firstlineno", 0))
            except (TypeError, ValueError):
                firstlineno = 0
            return (rel, qualname, firstlineno)
        except Exception:
            return None

    def _resolve_data(self, path: object) -> str | None:
        """Project-relative data path of one opened path, else None."""
        try:
            if isinstance(path, (bytes, os.PathLike)):
                try:
                    path = os.fspath(path)
                except Exception:
                    return None
                if isinstance(path, bytes):
                    try:
                        path = path.decode("utf-8", errors="strict")
                    except UnicodeDecodeError:
                        return None
            if (not isinstance(path, str) or not path or "\x00" in path
                    or path.startswith("<")):
                return None
            try:
                absolute = os.path.abspath(path)
            except (OSError, ValueError):
                return None
            cached = self._path_memo.get(absolute)
            if cached is not None or absolute in self._path_memo:
                return cached
            try:
                real = os.path.realpath(absolute)
            except (OSError, ValueError):
                return None
            rel: str | None = None
            if real == absolute or _is_within(real, self._checkout):
                try:
                    candidate = os.path.relpath(real, self._checkout)
                except (OSError, ValueError):
                    candidate = None
                if (isinstance(candidate, str)
                        and _data_path(candidate.replace(os.sep, "/"))
                        and not os.path.isdir(real)):
                    # Recorded whatever its size: dropping a large file
                    # would silently lose D(T) and violate spec N1. The
                    # engine bounds its own digest reads.
                    rel = candidate.replace(os.sep, "/")
            try:
                if len(self._path_memo) >= _MAX_PATH_MEMO:
                    self._path_memo.clear()
                self._path_memo[absolute] = rel
            except Exception:
                pass
            return rel
        except Exception:
            return None

    # -- audit hook ---------------------------------------------------------

    def _audit(self, event: object, args: object) -> None:
        """Audit entry point: reentrancy guard plus a never-raise shell."""
        try:
            if not self._active or self._tampered or self._in_hook:
                return
            self._in_hook = True
            try:
                try:
                    frame = sys._getframe(1)
                except Exception:
                    frame = None
                self._handle_audit(event, args, frame)
            finally:
                self._in_hook = False
        except Exception:
            pass

    def _spawns_xdist_worker(self, executable: object, argv: object) -> bool:
        """The controller's ambient context starting an xdist worker."""
        return (self._role == "controller"
                and self._current is self._ambient
                and _is_xdist_gateway(executable, argv))

    def _handle_audit(self, event: object, args: object,
                      frame: object = None) -> None:
        try:
            name = str(event) if isinstance(event, str) else ""
            if not name or not isinstance(args, (tuple, list)):
                return
            if name == "exec":
                if args and isinstance(args[0], types.CodeType):
                    self._discover(args[0])
            elif name == "open":
                path = args[0] if len(args) > 0 else None
                mode = args[1] if len(args) > 1 else None
                flags = args[2] if len(args) > 2 else None
                if (_is_pure_write(mode, flags) or _opens_directory(flags)
                        or _dir_fd_relative(path, mode)):
                    return
                rel = self._resolve_data(path)
                if rel is not None:
                    key = ("D", rel)
                    if key not in self._first:
                        self._note_first(key, frame)
                    self._record_data(rel)
            elif _force_opaque_event(name):
                try:
                    self._current.opaque = True
                except Exception:
                    pass
            elif name == "subprocess.Popen" or name == "os.posix_spawn" \
                    or (name.startswith("os.spawn") or name.startswith("os.exec")):
                executable = args[0] if len(args) > 0 else None
                rest = args[1] if len(args) > 1 else None
                if self._spawns_xdist_worker(executable, rest):
                    return
                if _spawn_opaque(executable, rest, self._checkout):
                    try:
                        self._current.opaque = True
                    except Exception:
                        pass
                    cwd = (args[2] if name == "subprocess.Popen"
                           and len(args) > 2 else None)
                    entry = self._spawn_entry(executable, rest, cwd)
                    if entry is not None:
                        try:
                            self._current.modules.add(entry)
                        except Exception:
                            pass
            elif name == "_posixsubprocess.fork_exec":
                # spawn/forkserver workers (multiprocessing.Pool and
                # ProcessPoolExecutor): the executable arrives as a
                # one-element bytes list. Classify it like any spawn (N3).
                executable = _fork_exec_arg(
                    args[0] if len(args) > 0 else None)
                rest = args[1] if len(args) > 1 else None
                if self._spawns_xdist_worker(executable, rest):
                    return
                if _spawn_opaque(executable, rest, self._checkout):
                    try:
                        self._current.opaque = True
                    except Exception:
                        pass
            elif name == "socket.connect":
                # A live forkserver serves later pools/process executors
                # with no further spawn event in this process (N3).
                if _is_forkserver_connect(args):
                    try:
                        self._current.opaque = True
                    except Exception:
                        pass
        except Exception:
            pass

    def _spawn_entry(self, executable: object, argv: object,
                     cwd: object) -> str | None:
        """Project module or script a spawned Python interpreter runs.

        Only ``python [options] -m module`` and ``python [options]
        script.py`` resolve; ``-c`` code, other programs and anything
        outside the checkout give None. The child's imports from that
        entry are then followed by the static rule.
        """
        try:
            if not isinstance(argv, (list, tuple)) or not argv:
                return None
            texts: list[str] = []
            for item in argv[:64]:
                if isinstance(item, os.PathLike):
                    item = os.fspath(item)
                if isinstance(item, bytes):
                    item = item.decode("utf-8", "strict")
                if not isinstance(item, str) or "\x00" in item:
                    return None
                texts.append(item)
            program = executable if executable is not None else texts[0]
            if isinstance(program, os.PathLike):
                program = os.fspath(program)
            if isinstance(program, bytes):
                program = program.decode("utf-8", "strict")
            if not isinstance(program, str) or not program:
                return None
            base = os.path.basename(program)
            if (not re.fullmatch(r"(?:python|pypy)[0-9.]*(?:\.exe)?", base)
                    and os.path.realpath(program)
                    != os.path.realpath(sys.executable)):
                return None
            if isinstance(cwd, os.PathLike):
                cwd = os.fspath(cwd)
            if isinstance(cwd, bytes):
                cwd = cwd.decode("utf-8", "strict")
            if not isinstance(cwd, str) or not cwd:
                cwd = os.getcwd()
            args = texts[1:]
            index = 0
            while index < len(args):
                arg = args[index]
                if arg == "-c" or arg.startswith("-c"):
                    return None
                if arg == "-m" or (arg.startswith("-m")
                                   and not arg.startswith("--")):
                    module = (args[index + 1] if arg == "-m"
                              and index + 1 < len(args) else arg[2:])
                    return self._module_entry(module, cwd)
                if arg in ("-W", "-X"):
                    index += 2
                    continue
                if arg.startswith("-"):
                    index += 1
                    continue
                path = arg if os.path.isabs(arg) else os.path.join(cwd, arg)
                return self._entry_rel(path)
            return None
        except Exception:
            return None

    def _module_entry(self, module: str, cwd: str) -> str | None:
        """File ``python -m module`` runs, searched like the child would."""
        try:
            parts = module.split(".")
            if not module or not all(part.isidentifier() for part in parts):
                return None
            for root in [cwd, *sys.path]:
                if not isinstance(root, str):
                    continue
                if not root:
                    root = cwd
                base = os.path.join(root, *parts)
                for candidate in (os.path.join(base, "__main__.py"),
                                  base + ".py"):
                    if os.path.isfile(candidate):
                        return self._entry_rel(candidate)
            return None
        except Exception:
            return None

    def _entry_rel(self, path: str) -> str | None:
        try:
            real = os.path.realpath(path)
            if not os.path.isfile(real) or not _is_within(real, self._checkout):
                return None
            rel = os.path.relpath(real, self._checkout).replace(os.sep, "/")
            return rel if _code_path(rel) else None
        except Exception:
            return None

    # -- output ---------------------------------------------------------------

    def set_deselect(self, status: str, count: int) -> None:
        """Record the bridge's deselect binding outcome for the deps file."""
        try:
            if status in ("none", "applied", "ignored"):
                self._deselect_status = status
            self._deselected = max(0, int(count))
        except Exception:
            pass

    def set_workers(self, workers: object) -> None:
        """Record the controller's observed worker ids for the deps file."""
        try:
            if isinstance(workers, (list, tuple)):
                self._workers = sorted(
                    worker for worker in workers
                    if isinstance(worker, str) and worker)
        except Exception:
            pass

    def _deps_path(self) -> Path:
        return Path(f"{self._report_path}{_DEPS_INFIX}{os.getpid()}")

    def _ctx_payload(self, ctx: _Ctx, path_index: dict[str, int],
                     function_index: dict[tuple[str, str, int], int]) -> dict:
        return {
            "functions": sorted(function_index[entry]
                                for entry in ctx.functions),
            "modules": sorted(path_index[rel] for rel in ctx.modules),
            "data": sorted(path_index[rel] for rel in ctx.data),
            "opaque": bool(ctx.opaque),
        }

    def _payload(self) -> dict:
        paths: set[str] = set()
        functions: set[tuple[str, str, int]] = set()
        contexts = [self._ambient, *self._fixtures.values(),
                    *(node.ctx for node in self._nodes.values())]
        for ctx in contexts:
            paths |= ctx.modules | ctx.data
            paths |= {rel for (rel, _, _) in ctx.functions}
            functions |= ctx.functions
        ordered_paths = sorted(paths)
        path_index = {rel: index for index, rel in enumerate(ordered_paths)}
        ordered_functions = sorted(
            functions, key=lambda entry: (path_index[entry[0]], entry[1],
                                          entry[2]))
        function_index = {entry: index
                          for index, entry in enumerate(ordered_functions)}
        ordered_fixtures = sorted(self._fixtures)
        fixture_entries = []
        for key in ordered_fixtures:
            entry = {"key": [key[0], key[1], key[2]]}
            entry.update(self._ctx_payload(self._fixtures[key], path_index,
                                           function_index))
            fixture_entries.append(entry)
        # Frozen §2.6: node fixture refs are indexes into the fixtures
        # array, not key triples.
        fixture_index = {key: index
                         for index, key in enumerate(ordered_fixtures)}
        node_entries = []
        for nodeid in sorted(self._nodes):
            node = self._nodes[nodeid]
            entry = {
                "nodeid": nodeid,
                "outcome": node.outcome if node.outcome in _OUTCOME_RANK else "unknown",
                "fixtures": sorted(
                    fixture_index[key] for key in node.fixtures
                    if key in fixture_index),
            }
            entry.update(self._ctx_payload(node.ctx, path_index,
                                           function_index))
            node_entries.append(entry)
        try:
            python = [int(sys.version_info[0]), int(sys.version_info[1])]
        except Exception:
            python = [0, 0]
        reason = self.inactive_reason
        if not isinstance(reason, str):
            reason = None
        elif len(reason) > 200:
            reason = reason[:200]
        return {
            "format": _DEPS_FORMAT,
            "run_id": self.run_id,
            "role": self._role,
            "worker_id": self._worker_id,
            "pid": int(os.getpid()),
            "python": python,
            "recording": bool(self.recording),
            "inactive_reason": reason,
            "workers": list(self._workers),
            "overflow": bool(self.overflow),
            "tamper": bool(self.tamper),
            "deselect": self._deselect_status,
            "deselected": int(self._deselected),
            "paths": ordered_paths,
            "functions": [[path_index[rel], qualname, firstlineno]
                          for (rel, qualname, firstlineno) in ordered_functions],
            "ambient": self._ctx_payload(self._ambient, path_index,
                                         function_index),
            "fixtures": fixture_entries,
            "nodes": node_entries,
        }

    def _header(self, *, overflow: bool) -> dict:
        try:
            python = [int(sys.version_info[0]), int(sys.version_info[1])]
        except Exception:
            python = [0, 0]
        reason = self.inactive_reason
        if not isinstance(reason, str):
            reason = None
        elif len(reason) > 200:
            reason = reason[:200]
        return {
            "format": _DEPS_FORMAT,
            "run_id": self.run_id,
            "role": self._role,
            "worker_id": self._worker_id,
            "pid": int(os.getpid()),
            "python": python,
            "recording": bool(self.recording),
            "inactive_reason": reason,
            "workers": list(self._workers),
            "overflow": bool(overflow),
            "tamper": bool(self.tamper),
            "deselect": self._deselect_status,
            "deselected": int(self._deselected),
            "paths": [],
            "functions": [],
            "ambient": {"functions": [], "modules": [], "data": [],
                        "opaque": False},
            "fixtures": [],
            "nodes": [],
        }

    def write_deps(self) -> Path | None:
        """Write this process's dependency file; None when refused."""
        self._active = False
        try:
            if not self._report_path:
                return None
            self._apply_lazy_owners()
            payload = self._payload()
            try:
                raw = json.dumps(payload, ensure_ascii=True,
                                 separators=(",", ":")).encode("utf-8")
            except (TypeError, ValueError, UnicodeError):
                return None
            if len(raw) > _DEPS_MAX_BYTES:
                self.overflow = True
                try:
                    raw = json.dumps(self._header(overflow=True),
                                     ensure_ascii=True,
                                     separators=(",", ":")).encode("utf-8")
                except (TypeError, ValueError, UnicodeError):
                    return None
            path = self._deps_path()
            cloexec = getattr(os, "O_CLOEXEC", 0)
            nofollow = getattr(os, "O_NOFOLLOW", 0)
            try:
                fd = os.open(str(path),
                             os.O_WRONLY | os.O_CREAT | os.O_EXCL
                             | nofollow | cloexec, 0o600)
            except (OSError, ValueError, TypeError):
                return None
            try:
                view = memoryview(raw)
                while view:
                    written = os.write(fd, view)
                    if written <= 0:
                        raise OSError("deps write made no progress")
                    view = view[written:]
            except OSError:
                try:
                    os.close(fd)
                except OSError:
                    pass
                try:
                    os.unlink(path)
                except OSError:
                    pass
                return None
            try:
                os.close(fd)
            except OSError:
                return None
            return path
        except Exception:
            return None


def write_inactive(report_path: str, run_id: str, role: str, reason: str,
                   worker_id: str | None = None) -> Path | None:
    """Write a recording:false deps file without touching sys.monitoring.

    Used on Python older than 3.12 and when the recorder module cannot be
    used: no tool is claimed, no hook is installed. Never raises.
    """
    try:
        recorder = Recorder(checkout_root="", run_id=run_id,
                            report_path=report_path, role=role,
                            worker_id=worker_id)
        recorder.inactive_reason = reason
        return recorder.write_deps()
    except Exception:
        return None


__all__ = ["Recorder", "record_active", "write_inactive"]
