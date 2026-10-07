"""Unit tests for the selection recorder (dynamic-selection T4).

In-process tests cover the duplicated literals, the path predicates, the
spawn table, outcome ranking, context switching and caps. Anything that
claims a real ``sys.monitoring`` tool id, installs the audit hook, or
writes a dependency file runs in a child interpreter ( tool ids are
process-global and audit hooks are permanent), each with an explicit
timeout of at most 60 s and no bare sleeps.

Real serial/xdist bridge twins live in
``tests/ng/test_selection_bridge_subprocess.py``.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys

from pathlib import Path
from types import SimpleNamespace

import pytest

from ptest.runtime import selection_recorder as recorder

REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Duplicated literals: stdlib-only copies pinned to the design values.
# ---------------------------------------------------------------------------

def test_duplicated_literals_have_frozen_values():
    assert recorder._DEPS_INFIX == ".deps-"
    assert recorder._DEPS_FORMAT == "ptest-selection-deps-v1"
    assert recorder._DEPS_MAX_BYTES == 64 * 1024 * 1024
    assert recorder._DESELECT_SUFFIX == ".deselect"
    assert recorder._DESELECT_FORMAT == "ptest-selection-deselect-v1"
    assert recorder._DESELECT_MAX_BYTES == 16 * 1024 * 1024
    assert recorder._RECORD_ENV == "PTEST_SELECTION_RECORD"
    assert recorder._DESELECT_ENV == "PTEST_SELECTION_DESELECT"
    assert recorder._TOOL_IDS == (3, 4, 2)
    assert recorder._TOOL_NAME == "ptest-selection"
    assert recorder._CONTEXT_MAX_FUNCTIONS == 100000
    assert recorder._CONTEXT_MAX_DATA == 4096
    assert recorder._DATA_MAX_BYTES == 16 * 1024 * 1024


def test_duplicated_literals_match_contracts():
    """Pin every duplicated literal equal to contracts.py."""
    from ptest import contracts as C

    assert recorder._DEPS_INFIX == C.SELECTION_DEPS_INFIX
    assert recorder._DEPS_FORMAT == C.SELECTION_DEPS_FORMAT
    assert recorder._DEPS_MAX_BYTES == C.SELECTION_DEPS_MAX_BYTES
    assert recorder._DESELECT_SUFFIX == C.SELECTION_DESELECT_SUFFIX
    assert recorder._DESELECT_FORMAT == C.SELECTION_DESELECT_FORMAT
    assert recorder._DESELECT_MAX_BYTES == C.SELECTION_DESELECT_MAX_BYTES
    assert recorder._RECORD_ENV == C.SELECTION_RECORD_ENV
    assert recorder._DESELECT_ENV == C.SELECTION_DESELECT_ENV
    assert recorder._TOOL_IDS == C.SELECTION_TOOL_IDS
    assert recorder._TOOL_NAME == C.SELECTION_TOOL_NAME
    assert recorder._CONTEXT_MAX_FUNCTIONS == C.SELECTION_CONTEXT_MAX_FUNCTIONS
    assert recorder._CONTEXT_MAX_DATA == C.SELECTION_CONTEXT_MAX_DATA
    assert recorder._DATA_MAX_BYTES == C.SELECTION_DATA_MAX_BYTES


# ---------------------------------------------------------------------------
# Path predicates: shared case table, asserted always and pinned to
# contracts when the barrier has landed.
# ---------------------------------------------------------------------------

_CODE_CASES = [
    ("pkg/core.py", True),
    ("tests/test_core.py", True),
    ("pkg/sub/mod.py", True),
    ("data/config.json", False),
    ("pkg/core.pyc", False),
    ("pkg/core.pyo", False),
    ("__pycache__/pkg/core.py", False),
    ("venv/pkg/core.py", False),
    (".venv/pkg/core.py", False),
    ("site-packages/pkg/core.py", False),
    ("node_modules/pkg/core.py", False),
    ("build/pkg/core.py", False),
    (".hidden/pkg/core.py", False),
    ("pkg.egg-info/x.py", False),
    (".pytest_cache/x.py", False),
    ("../evil.py", False),
    ("/abs/evil.py", False),
    ("", False),
    ("pkg\\core.py", False),
    ("nul\x00byte.py", False),
    ("<string>", False),
]

_DATA_CASES = [
    ("data/config.json", True),
    ("data/nested/file.yaml", True),
    ("pkg/core.py", False),
    ("pkg/core.pyc", False),
    ("pkg/core.pyo", False),
    ("data/x.PY", False),
    (".git/config", False),
    ("__pycache__/data.json", False),
    ("node_modules/data.json", False),
    ("build/out.json", False),
    (".venv/data.json", False),
    ("htmlcov/data.json", False),
    ("../evil.json", False),
    ("/abs/evil.json", False),
    ("", False),
    ("nul\x00byte.json", False),
]

_QUALNAME_CASES = [
    ("f", "f"),
    ("C.m", "C.m"),
    ("f.<locals>.g", "f"),
    ("C.<lambda>", "C"),
    ("<module>", None),
    ("<lambda>", None),
    ("<genexpr>", None),
    ("", None),
    ("a\x00b", None),
    ("outer.<locals>.Inner.method", "outer"),
]


def test_code_path_cases():
    for path, expected in _CODE_CASES:
        assert recorder._code_path(path) is expected, path


def test_data_path_cases():
    for path, expected in _DATA_CASES:
        assert recorder._data_path(path) is expected, path


def test_normalize_qualname_cases():
    for raw, expected in _QUALNAME_CASES:
        assert recorder._normalize_qualname(raw) == expected, raw


def test_predicates_match_contracts_on_shared_table():
    """The stdlib copies must agree with contracts on every shared case."""
    from ptest import contracts as C

    for path, _ in _CODE_CASES + _DATA_CASES:
        assert recorder._relpath_ok(path) == C.selection_relpath_safe(path), path
    for path, _ in _CODE_CASES:
        assert recorder._code_path(path) == C.selection_code_path(path), path
    for path, _ in _DATA_CASES:
        assert recorder._data_path(path) == C.selection_data_path(path), path
    for raw, _ in _QUALNAME_CASES:
        assert recorder._normalize_qualname(raw) == C.selection_normalize_qualname(raw), raw


# ---------------------------------------------------------------------------
# Hostile code filenames: never recorded, never raised.
# ---------------------------------------------------------------------------

def _fake_code(filename):
    return SimpleNamespace(co_filename=filename, co_qualname="f",
                           co_firstlineno=1, co_consts=(), co_name="f")


def test_hostile_filenames_are_dropped(tmp_path):
    rec = recorder.Recorder(checkout_root=str(tmp_path), run_id="r" * 32,
                            report_path=str(tmp_path / "rep.json"), role="controller")
    outside = tmp_path.parent / "outside.py"
    outside.write_text("x = 1\n", encoding="utf-8")
    link = tmp_path / "link.py"
    try:
        link.symlink_to(outside)
        have_link = True
    except OSError:
        have_link = False
    hostile = ["<string>", "", "../evil.py", "/abs/evil.py", "a\x00b.py",
               str(outside)]
    if have_link:
        hostile.append(str(link))
    for name in hostile:
        assert rec._resolve_code(_fake_code(name)) is None, name


def test_venv_and_cache_filenames_are_dropped(tmp_path):
    rec = recorder.Recorder(checkout_root=str(tmp_path), run_id="r" * 32,
                            report_path=str(tmp_path / "rep.json"), role="controller")
    (tmp_path / "pkg").mkdir()
    real = tmp_path / "pkg" / "core.py"
    real.write_text("def f():\n    return 1\n", encoding="utf-8")
    resolved = rec._resolve_code(_fake_code(str(real)))
    assert resolved is not None and resolved[0] == "pkg/core.py"
    for name in (str(tmp_path / ".venv" / "x.py"),
                 str(tmp_path / "venv" / "x.py"),
                 str(tmp_path / "site-packages" / "x.py"),
                 str(tmp_path / "__pycache__" / "x.py")):
        assert rec._resolve_code(_fake_code(name)) is None, name


# ---------------------------------------------------------------------------
# Spawn classification.
# ---------------------------------------------------------------------------

def test_spawn_table(tmp_path):
    outside = shutil.which("true")
    if outside is None:
        pytest.skip("no true binary on PATH")
    checkout = tmp_path / "proj"
    checkout.mkdir()
    script = checkout / "run.py"
    script.write_text("print(1)\n", encoding="utf-8")
    # The running interpreter always marks opaque.
    assert recorder._spawn_opaque(
        sys.executable, [sys.executable, "-c", "pass"], str(checkout)) is True
    assert recorder._spawn_opaque(sys.executable, None, str(checkout)) is True
    # A script inside the checkout always marks opaque.
    assert recorder._spawn_opaque(str(script), [str(script)], str(checkout)) is True
    # os.system / fork always mark opaque, whatever the command.
    assert recorder._force_opaque_event("os.system") is True
    assert recorder._force_opaque_event("os.fork") is True
    assert recorder._force_opaque_event("os.forkpty") is True
    assert recorder._force_opaque_event("subprocess.Popen") is False
    # An outside binary that cannot run project Python stays clean.
    assert recorder._spawn_opaque(outside, [outside], str(checkout)) is False
    # An unresolvable executable is opaque rather than trusted.
    assert recorder._spawn_opaque(
        "definitely-not-a-binary-xyz", None, str(checkout)) is True


def test_launcher_names_are_opaque():
    for name in ("python3", "python3.12", "uv", "uvx", "pytest", "ptest",
                 "sh", "bash", "env"):
        assert recorder._launcher_opaque(name) is True, name
    for name in ("true", "git", "ls"):
        assert recorder._launcher_opaque(name) is False, name


# ---------------------------------------------------------------------------
# Outcome ranking and context switching (no monitoring needed).
# ---------------------------------------------------------------------------

def _report(nodeid, when, outcome, **extra):
    fields = {"nodeid": nodeid, "when": when, "outcome": outcome,
              "failed": False, "skipped": False, "wasxfail": False}
    fields.update(extra)
    return SimpleNamespace(**fields)


def _recorder(tmp_path):
    return recorder.Recorder(checkout_root=str(tmp_path), run_id="r" * 32,
                             report_path=str(tmp_path / "rep.json"), role="controller")


def test_worst_phase_outcome_order(tmp_path):
    rec = _recorder(tmp_path)
    rec.enter_test("t.py::t1")
    rec.observe(_report("t.py::t1", "setup", "passed"))
    rec.observe(_report("t.py::t1", "call", "passed"))
    rec.observe(_report("t.py::t1", "teardown", "passed"))
    assert rec._nodes["t.py::t1"].outcome == "passed"
    # A later worse phase wins; a later better phase never downgrades.
    rec.observe(_report("t.py::t1", "teardown", "failed", failed=True))
    assert rec._nodes["t.py::t1"].outcome == "error"
    rec.observe(_report("t.py::t1", "call", "passed"))
    assert rec._nodes["t.py::t1"].outcome == "error"


def test_outcome_vocabulary(tmp_path):
    cases = [
        (_report("n", "setup", "failed", failed=True), "error"),
        (_report("n", "setup", "skipped", skipped=True), "skipped"),
        (_report("n", "call", "failed"), "failed"),
        (_report("n", "call", "passed"), "passed"),
        (_report("n", "call", "skipped"), "skipped"),
        (_report("n", "call", "passed", wasxfail=True), "xpassed"),
        (_report("n", "call", "skipped", wasxfail=True), "xfailed"),
        (_report("n", "call", "failed", wasxfail=True), "failed"),
        (_report("n", "teardown", "failed", failed=True), "error"),
    ]
    for index, (report, expected) in enumerate(cases):
        rec = _recorder(tmp_path)
        nodeid = f"t.py::t{index}"
        report.nodeid = nodeid
        rec.enter_test(nodeid)
        rec.observe(report)
        assert rec._nodes[nodeid].outcome == expected, (report, expected)


def test_observe_ignores_nodeids_without_a_context(tmp_path):
    """Forwarded xdist-controller reports never create node records."""
    rec = _recorder(tmp_path)
    rec.observe(_report("t.py::ghost", "call", "passed"))
    assert rec._nodes == {}


def test_unobserved_node_stays_unknown(tmp_path):
    rec = _recorder(tmp_path)
    rec.enter_test("t.py::t1")
    rec.exit_test()
    assert rec._nodes["t.py::t1"].outcome == "unknown"


def test_fixture_context_attribution_and_restore(tmp_path):
    rec = _recorder(tmp_path)
    rec.enter_test("t.py::t1")
    test_ctx = rec._current
    rec.enter_fixture(("base", "booted", "session"))
    assert rec._current is not test_ctx
    rec._record_function(("pkg/core.py", "boot", 3))
    rec.exit_fixture()
    assert rec._current is test_ctx
    assert ("pkg/core.py", "boot", 3) not in test_ctx.functions
    key = ("base", "booted", "session")
    assert ("pkg/core.py", "boot", 3) in rec._fixtures[key].functions


def test_fixture_key_shapes():
    assert recorder.Recorder.fixture_key(
        SimpleNamespace(baseid="", argname="booted", scope="session")) == ("", "booted", "session")
    assert recorder.Recorder.fixture_key(
        SimpleNamespace(baseid="m.py", argname="db", scope="module")) == ("m.py", "db", "module")
    assert recorder.Recorder.fixture_key(
        SimpleNamespace(baseid="t.py::t", argname="x", scope="function")) is None


def test_fixture_keys_for_item_uses_closure_defs():
    maker = SimpleNamespace(baseid="", argname="booted", scope="session")
    item = SimpleNamespace(
        fixturenames=["booted", "plain"],
        _request=SimpleNamespace(_fixture_defs={"booted": maker}),
        _fixtureinfo=SimpleNamespace(name2fixturedefs={
            "booted": [maker],
            "plain": [SimpleNamespace(baseid="t", argname="plain", scope="function")]}))
    assert recorder.Recorder.fixture_keys_for_item(item) == [("", "booted", "session")]


def test_node_fixture_refs_are_fixtures_indexes(tmp_path):
    """Frozen §2.6: node 'fixtures' are indexes, not key triples."""
    rec = _recorder(tmp_path)
    rec.enter_test("t.py::t1")
    alpha = ("base", "alpha", "session")
    booted = ("base", "booted", "session")
    rec.enter_fixture(alpha)
    rec.exit_fixture()
    rec.enter_fixture(booted)
    rec.exit_fixture()
    rec._nodes["t.py::t1"].fixtures.extend([booted, alpha])
    rec.exit_test()
    payload = rec._payload()
    assert [entry["key"] for entry in payload["fixtures"]] == [
        ["base", "alpha", "session"], ["base", "booted", "session"]]
    (entry,) = payload["nodes"]
    assert entry["fixtures"] == [0, 1]
    # The T3 ingest predicate over the same shape.
    assert all(isinstance(ref, int) and 0 <= ref < len(payload["fixtures"])
               for ref in entry["fixtures"])


def test_oversize_data_file_is_still_recorded(tmp_path):
    """Spec N1: a data file above _DATA_MAX_BYTES is still D(T)."""
    (tmp_path / "data").mkdir()
    big = tmp_path / "data" / "big.bin"
    with open(big, "wb") as handle:
        handle.truncate(20 * 1024 * 1024)
    assert big.stat().st_size == 20 * 1024 * 1024
    rec = _recorder(tmp_path)
    assert rec._resolve_data(str(big)) == "data/big.bin"


def test_caps_make_context_opaque(tmp_path, monkeypatch):
    rec = _recorder(tmp_path)
    rec.recording = True
    monkeypatch.setattr(recorder, "_CONTEXT_MAX_FUNCTIONS", 2)
    monkeypatch.setattr(recorder, "_CONTEXT_MAX_DATA", 1)
    rec.enter_test("t.py::t1")
    rec._record_function(("a.py", "f", 1))
    rec._record_function(("a.py", "g", 2))
    assert rec._current.opaque is False
    rec._record_function(("a.py", "h", 3))
    assert rec._current.opaque is True
    rec._record_data("d/1.json")
    assert rec._current.opaque is True
    rec._record_data("d/2.json")
    assert rec._current.opaque is True


def test_audit_hook_never_raises_on_garbage(tmp_path):
    rec = _recorder(tmp_path)
    for event, args in [("open", (None, None, None)),
                        ("open", (12345, "r", 0)),
                        ("exec", ()),
                        ("exec", (object(),)),
                        ("subprocess.Popen", ()),
                        ("subprocess.Popen", (None, None)),
                        ("os.posix_spawn", ("",)),
                        ("os.system", ()),
                        ("bogus-event", ("x",)),
                        ("open", ("a" * 9000, "r", 0))]:
        rec._audit(event, args)
    assert rec._nodes == {}
    assert rec._ambient.functions == set()


# ---------------------------------------------------------------------------
# Child-interpreter tests: real tool ids, the audit hook, the deps file.
# ---------------------------------------------------------------------------

_CHILD_PREAMBLE = (
    "import importlib.util, json, os, sys\n"
    "spec = importlib.util.spec_from_file_location(\n"
    "    'selection_recorder', {recorder_path!r})\n"
    "module = importlib.util.module_from_spec(spec)\n"
    "spec.loader.exec_module(module)\n"
)


def _run_child(code: str, *, cwd: Path, timeout: float = 60) -> subprocess.CompletedProcess:
    assert timeout <= 60
    env = {k: v for k, v in os.environ.items()
           if k not in ("PYTHONPATH", "PYTHONDONTWRITEBYTECODE")}
    return subprocess.run([sys.executable, "-c", code], cwd=str(cwd),
                          env=env, capture_output=True, text=True,
                          timeout=timeout, check=False)


def test_activation_records_real_execution(tmp_path):
    """PY_START on project code fires the callback; stdlib code stays dark."""
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "core.py").write_text(
        "def add(a, b):\n    return a + b\n", encoding="utf-8")
    code = (_CHILD_PREAMBLE.format(recorder_path=str(
        REPO_ROOT / "src" / "ptest" / "runtime" / "selection_recorder.py"))
        + "import sys\n"
        + f"rec = module.Recorder(checkout_root={str(tmp_path)!r}, run_id={'r' * 32!r},\n"
        + f"                      report_path={str(tmp_path / 'rep.json')!r}, role='controller')\n"
        + "assert rec.activate() is True, rec.inactive_reason\n"
        + "assert rec.recording is True\n"
        + "sys.path.insert(0, " + repr(str(tmp_path)) + ")\n"
        + "rec.enter_test('pkg/test_x.py::test_1')\n"
        + "import pkg.core as core\n"
        + "assert core.add(1, 2) == 3\n"
        + "len_before = len(rec._current.functions)\n"
        + "import json as real_json\n"
        + "real_json.dumps({'a': 1})\n"
        + "assert len(rec._current.functions) == len_before, 'stdlib must stay dark'\n"
        + "rec.exit_test()\n"
        + "node = rec._nodes['pkg/test_x.py::test_1']\n"
        + "assert ('pkg/core.py', 'add', 1) in node.ctx.functions\n"
        + "assert 'pkg/core.py' in node.ctx.modules\n"
        + "path = rec.write_deps()\n"
        + "print('wrote:' + str(path))\n"
        + "rec.deactivate()\n")
    done = _run_child(code, cwd=tmp_path)
    assert done.returncode == 0, done.stderr
    assert "wrote:" in done.stdout


def test_activation_arms_modules_imported_before_it(tmp_path):
    """Spec 6.1: the sys.modules walk arms pre-activation imports."""
    (tmp_path / "pkgx").mkdir()
    (tmp_path / "pkgx" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkgx" / "early.py").write_text(
        "def hello():\n    return 'hi'\n"
        "\n"
        "class Greeter:\n"
        "    def greet(self):\n        return 'yo'\n",
        encoding="utf-8")
    code = (_CHILD_PREAMBLE.format(recorder_path=str(
        REPO_ROOT / "src" / "ptest" / "runtime" / "selection_recorder.py"))
        + "import sys\n"
        + "sys.path.insert(0, " + repr(str(tmp_path)) + ")\n"
        + "import pkgx.early as early\n"
        + f"rec = module.Recorder(checkout_root={str(tmp_path)!r}, run_id={'r' * 32!r},\n"
        + f"                      report_path={str(tmp_path / 'rep.json')!r}, role='controller')\n"
        + "assert rec.activate() is True, rec.inactive_reason\n"
        + "m = sys.monitoring\n"
        + "assert m.get_local_events(rec._tool, early.hello.__code__) != 0\n"
        + "assert m.get_local_events(rec._tool, early.Greeter.greet.__code__) != 0\n"
        + "rec.enter_test('t.py::t1')\n"
        + "assert early.hello() == 'hi'\n"
        + "assert early.Greeter().greet() == 'yo'\n"
        + "rec.exit_test()\n"
        + "node = rec._nodes['t.py::t1']\n"
        + "assert ('pkgx/early.py', 'hello', 1) in node.ctx.functions\n"
        + "assert ('pkgx/early.py', 'Greeter.greet', 5) in node.ctx.functions\n"
        + "rec.deactivate()\n"
        + "print('walk-ok')\n")
    done = _run_child(code, cwd=tmp_path)
    assert done.returncode == 0, done.stderr
    assert "walk-ok" in done.stdout


def test_activation_arms_decorated_members_imported_before_it(tmp_path):
    """Spec 6.1/N6: the sys.modules walk arms wrappers, caches and containers."""
    (tmp_path / "pkgx").mkdir()
    (tmp_path / "pkgx" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkgx" / "late.py").write_text(
        "import contextlib\n"
        "import functools\n"
        "\n"
        "def plain():\n"
        "    return 'hi'\n"
        "\n"
        "@functools.lru_cache(maxsize=None)\n"
        "def cached():\n"
        "    return 'c'\n"
        "\n"
        "def deco(fn):\n"
        "    @functools.wraps(fn)\n"
        "    def wrapper(*a, **k):\n"
        "        return fn(*a, **k)\n"
        "    return wrapper\n"
        "\n"
        "@deco\n"
        "def decorated():\n"
        "    return 'd'\n"
        "\n"
        "@contextlib.contextmanager\n"
        "def cm():\n"
        "    yield 'v'\n"
        "\n"
        "HANDLERS = {'ping': lambda: 'pong'}\n"
        "CALLBACKS = [lambda: 'cb']\n",
        encoding="utf-8")
    code = (_CHILD_PREAMBLE.format(recorder_path=str(
        REPO_ROOT / "src" / "ptest" / "runtime" / "selection_recorder.py"))
        + "import sys\n"
        + "sys.path.insert(0, " + repr(str(tmp_path)) + ")\n"
        + "import pkgx.late as late\n"
        + f"rec = module.Recorder(checkout_root={str(tmp_path)!r}, run_id={'r' * 32!r},\n"
        + f"                      report_path={str(tmp_path / 'rep.json')!r}, role='controller')\n"
        + "assert rec.activate() is True, rec.inactive_reason\n"
        + "m = sys.monitoring\n"
        + "assert m.get_local_events(rec._tool, late.plain.__code__) != 0\n"
        + "assert m.get_local_events(rec._tool, late.cached.__wrapped__.__code__) != 0\n"
        + "assert m.get_local_events(rec._tool, late.decorated.__wrapped__.__code__) != 0\n"
        + "assert m.get_local_events(rec._tool, late.cm.__wrapped__.__code__) != 0\n"
        + "assert m.get_local_events(rec._tool, late.HANDLERS['ping'].__code__) != 0\n"
        + "assert m.get_local_events(rec._tool, late.CALLBACKS[0].__code__) != 0\n"
        + "rec.enter_test('t.py::t1')\n"
        + "assert late.plain() == 'hi'\n"
        + "assert late.cached() == 'c'\n"
        + "assert late.decorated() == 'd'\n"
        + "with late.cm() as v:\n"
        + "    assert v == 'v'\n"
        + "assert late.HANDLERS['ping']() == 'pong'\n"
        + "assert late.CALLBACKS[0]() == 'cb'\n"
        + "rec.exit_test()\n"
        + "node = rec._nodes['t.py::t1']\n"
        + "# co_firstlineno is the decorator line for decorated defs.\n"
        + "assert ('pkgx/late.py', 'plain', 4) in node.ctx.functions\n"
        + "assert ('pkgx/late.py', 'cached', 7) in node.ctx.functions\n"
        + "assert ('pkgx/late.py', 'decorated', 17) in node.ctx.functions\n"
        + "assert ('pkgx/late.py', 'cm', 21) in node.ctx.functions\n"
        + "assert 'pkgx/late.py' in node.ctx.modules\n"
        + "rec.deactivate()\n"
        + "print('walk-decorated-ok')\n")
    done = _run_child(code, cwd=tmp_path)
    assert done.returncode == 0, done.stderr
    assert "walk-decorated-ok" in done.stdout


def test_no_free_tool_id_records_nothing_and_says_why(tmp_path):
    code = (_CHILD_PREAMBLE.format(recorder_path=str(
        REPO_ROOT / "src" / "ptest" / "runtime" / "selection_recorder.py"))
        + "import sys\n"
        + "m = sys.monitoring\n"
        + "m.use_tool_id(3, 'squat'); m.use_tool_id(4, 'squat'); m.use_tool_id(2, 'squat')\n"
        + "try:\n"
        + f"    rec = module.Recorder(checkout_root={str(tmp_path)!r}, run_id={'r' * 32!r},\n"
        + f"                          report_path={str(tmp_path / 'rep.json')!r}, role='controller')\n"
        + "    assert rec.activate() is False\n"
        + "    assert rec.recording is False\n"
        + "    assert rec.inactive_reason == 'no free sys.monitoring tool id'\n"
        + "    path = rec.write_deps()\n"
        + "    print('wrote:' + str(path))\n"
        + "finally:\n"
        + "    m.free_tool_id(3); m.free_tool_id(4); m.free_tool_id(2)\n")
    done = _run_child(code, cwd=tmp_path)
    assert done.returncode == 0, done.stderr
    assert "wrote:" in done.stdout
    deps = list(tmp_path.glob("rep.json.deps-*"))
    assert len(deps) == 1
    payload = json.loads(deps[0].read_text(encoding="utf-8"))
    assert payload["recording"] is False
    assert payload["inactive_reason"] == "no free sys.monitoring tool id"
    assert payload["nodes"] == []


def test_no_global_events_after_activation(tmp_path):
    """N13: only set_local_events on project code, never set_events."""
    code = (_CHILD_PREAMBLE.format(recorder_path=str(
        REPO_ROOT / "src" / "ptest" / "runtime" / "selection_recorder.py"))
        + "import sys\n"
        + "m = sys.monitoring\n"
        + f"rec = module.Recorder(checkout_root={str(tmp_path)!r}, run_id={'r' * 32!r},\n"
        + f"                      report_path={str(tmp_path / 'rep.json')!r}, role='controller')\n"
        + "assert rec.activate() is True, rec.inactive_reason\n"
        + "try:\n"
        + "    assert m.get_events(rec._tool) == 0\n"
        + "    import json as stdlib_mod\n"
        + "    assert m.get_local_events(rec._tool, stdlib_mod.dumps.__code__) == 0\n"
        + "finally:\n"
        + "    rec.deactivate()\n"
        + "print('n13-ok')\n")
    done = _run_child(code, cwd=tmp_path)
    assert done.returncode == 0, done.stderr
    assert "n13-ok" in done.stdout


def test_tamper_marks_contexts_opaque(tmp_path):
    code = (_CHILD_PREAMBLE.format(recorder_path=str(
        REPO_ROOT / "src" / "ptest" / "runtime" / "selection_recorder.py"))
        + "import sys\n"
        + "m = sys.monitoring\n"
        + f"rec = module.Recorder(checkout_root={str(tmp_path)!r}, run_id={'r' * 32!r},\n"
        + f"                      report_path={str(tmp_path / 'rep.json')!r}, role='controller')\n"
        + "assert rec.activate() is True, rec.inactive_reason\n"
        + "m.get_tool(rec._tool)\n"
        + "m.free_tool_id(rec._tool)\n"
        + "m.use_tool_id(rec._tool, 'intruder')\n"
        + "try:\n"
        + "    rec.enter_test('t.py::t1')\n"
        + "    assert rec.tamper is True\n"
        + "    assert rec._current.opaque is True\n"
        + "    rec.exit_test()\n"
        + "    assert rec._nodes['t.py::t1'].ctx.opaque is True\n"
        + "finally:\n"
        + "    m.free_tool_id(rec._tool)\n"
        + "print('tamper-ok')\n")
    done = _run_child(code, cwd=tmp_path)
    assert done.returncode == 0, done.stderr
    assert "tamper-ok" in done.stdout


def test_deps_file_is_private_and_exclusive(tmp_path):
    code = (_CHILD_PREAMBLE.format(recorder_path=str(
        REPO_ROOT / "src" / "ptest" / "runtime" / "selection_recorder.py"))
        + f"rec = module.Recorder(checkout_root={str(tmp_path)!r}, run_id={'r' * 32!r},\n"
        + f"                      report_path={str(tmp_path / 'rep.json')!r}, role='controller')\n"
        + "rec.inactive_reason = 'no free sys.monitoring tool id'\n"
        + "path = rec.write_deps()\n"
        + "print('wrote:' + str(path))\n"
        + "assert rec.write_deps() is None, 'second write must refuse O_EXCL'\n"
        + "print('excl-ok')\n")
    done = _run_child(code, cwd=tmp_path)
    assert done.returncode == 0, done.stderr
    deps = list(tmp_path.glob("rep.json.deps-*"))
    assert len(deps) == 1
    stamp = deps[0].lstat()
    assert stat.S_ISREG(stamp.st_mode)
    assert stamp.st_nlink == 1
    assert stamp.st_mode & 0o777 == 0o600
    assert "excl-ok" in done.stdout


def test_deps_file_refuses_symlink(tmp_path):
    target = tmp_path / "target.json"
    target.write_text("{}", encoding="utf-8")
    code = (_CHILD_PREAMBLE.format(recorder_path=str(
        REPO_ROOT / "src" / "ptest" / "runtime" / "selection_recorder.py"))
        + "import os\n"
        + f"rec = module.Recorder(checkout_root={str(tmp_path)!r}, run_id={'r' * 32!r},\n"
        + f"                      report_path={str(tmp_path / 'rep.json')!r}, role='controller')\n"
        + "rec.inactive_reason = 'x'\n"
        + "os.symlink(" + repr(str(target)) + ", str(rec._deps_path()))\n"
        + "assert rec.write_deps() is None\n"
        + "print('symlink-refused')\n")
    done = _run_child(code, cwd=tmp_path)
    assert done.returncode == 0, done.stderr
    assert "symlink-refused" in done.stdout
    assert target.read_text(encoding="utf-8") == "{}"


def test_deps_json_matches_section_2_6(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "core.py").write_text(
        "def add(a, b):\n    return a + b\n", encoding="utf-8")
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "config.json").write_text("{}", encoding="utf-8")
    code = (_CHILD_PREAMBLE.format(recorder_path=str(
        REPO_ROOT / "src" / "ptest" / "runtime" / "selection_recorder.py"))
        + "import sys\n"
        + f"rec = module.Recorder(checkout_root={str(tmp_path)!r}, run_id={'a' * 32!r},\n"
        + f"                      report_path={str(tmp_path / 'rep.json')!r}, role='controller')\n"
        + "assert rec.activate() is True, rec.inactive_reason\n"
        + "sys.path.insert(0, " + repr(str(tmp_path)) + ")\n"
        + "rec.enter_test('tests/test_x.py::test_1')\n"
        + "import pkg.core as core\n"
        + "core.add(1, 2)\n"
        + "open(" + repr(str(tmp_path / "data" / "config.json")) + ").close()\n"
        + "rec.observe(__import__('types').SimpleNamespace(\n"
        + "    nodeid='tests/test_x.py::test_1', when='call', outcome='passed',\n"
        + "    failed=False, skipped=False, wasxfail=False))\n"
        + "rec.exit_test()\n"
        + "rec.set_deselect('applied', 2)\n"
        + "path = rec.write_deps()\n"
        + "print('wrote:' + str(path))\n"
        + "rec.deactivate()\n")
    done = _run_child(code, cwd=tmp_path)
    assert done.returncode == 0, done.stderr
    deps = list(tmp_path.glob("rep.json.deps-*"))
    assert len(deps) == 1
    payload = json.loads(deps[0].read_text(encoding="utf-8"))
    assert payload["format"] == "ptest-selection-deps-v1"
    assert payload["run_id"] == "a" * 32
    assert payload["role"] == "controller"
    assert payload["worker_id"] is None
    assert payload["pid"] == int(deps[0].name.rsplit("-", 1)[1])
    assert payload["python"] == list(sys.version_info[:2])
    assert payload["recording"] is True
    assert payload["inactive_reason"] is None
    assert payload["workers"] == []
    assert payload["overflow"] is False
    assert payload["tamper"] is False
    assert payload["deselect"] == "applied"
    assert payload["deselected"] == 2
    paths = payload["paths"]
    assert paths == sorted(paths)
    assert set(paths) == {"pkg/__init__.py", "pkg/core.py", "data/config.json"}
    functions = [(paths[entry[0]], entry[1], entry[2])
                 for entry in payload["functions"]]
    assert functions == [("pkg/core.py", "add", 1)]
    assert payload["nodes"] != []
    node = payload["nodes"][0]
    assert node["nodeid"] == "tests/test_x.py::test_1"
    assert node["outcome"] == "passed"
    assert set(node) == {"nodeid", "outcome", "fixtures", "functions",
                         "modules", "data", "opaque"}
    assert node["fixtures"] == []
    assert [functions[index] for index in node["functions"]] == [
        ("pkg/core.py", "add", 1)]
    assert {paths[index] for index in node["modules"]} == {
        "pkg/__init__.py", "pkg/core.py"}
    assert [paths[index] for index in node["data"]] == ["data/config.json"]
    assert node["opaque"] is False
    assert payload["ambient"] == {"functions": [], "modules": [],
                                 "data": [], "opaque": False}
    assert payload["fixtures"] == []
    raw = deps[0].read_bytes()
    assert raw.decode("ascii") == json.dumps(
        payload, ensure_ascii=True, separators=(",", ":"))


def test_overflow_file_drops_records(tmp_path, monkeypatch):
    monkeypatch.setattr(recorder, "_DEPS_MAX_BYTES", 10)
    rec = _recorder(tmp_path)
    rec.inactive_reason = None
    rec.recording = True
    rec.enter_test("t.py::t1")
    rec._record_function(("a.py", "f", 1))
    rec.exit_test()
    path = rec.write_deps()
    assert path is not None
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    assert payload["overflow"] is True
    assert payload["paths"] == []
    assert payload["functions"] == []
    assert payload["fixtures"] == []
    assert payload["nodes"] == []



def test_ingest_accepts_real_serial_output(tmp_path):
    """Cross-check: T3 ingest reads this task's real deps file as complete."""
    from ptest import selection_ingest
    (tmp_path / "reports").mkdir()
    report = tmp_path / "reports" / ("native-a001-" + "b" * 32 + ".json")
    report.write_text("{}", encoding="utf-8")
    rec = recorder.Recorder(checkout_root=str(tmp_path), run_id="b" * 32,
                            report_path=str(report), role="controller")
    rec.recording = True
    rec.enter_test("tests/test_x.py::test_1")
    rec._record_function(("pkg/core.py", "add", 1))
    rec.exit_test()
    assert rec.write_deps() is not None
    run = selection_ingest.read_run(report, run_id="b" * 32, expected_workers=1)
    assert run.complete is True




def test_fork_exec_python_marks_opaque(tmp_path):
    """N3: spawn/forkserver pool workers via _posixsubprocess.fork_exec.

    Multiprocessing Pool and ProcessPoolExecutor start workers through
    _posixsubprocess.fork_exec (the audit event the recorder ignored);
    the executable arrives as a one-element bytes list. Spawning the
    running interpreter must mark the context opaque. Failed before the
    fix: no branch handled the event and opaque stayed False.
    """
    rec = _recorder(tmp_path)
    rec.recording = True
    rec.enter_test("t.py::t1")
    assert rec._current.opaque is False
    exe = sys.executable.encode()
    rec._handle_audit("_posixsubprocess.fork_exec",
                      ([exe], [exe, b"-c", b"pass"], None))
    assert rec._current.opaque is True


_BOOT = "import sys;exec(eval(sys.stdin.readline()))"


def _gateway_argv(exe=None):
    exe = exe or sys.executable
    return [exe, "-u", "-c", _BOOT]


def test_xdist_gateway_spawn_keeps_controller_ambient_clean(tmp_path):
    """The controller starting xdist workers is accounted for (each worker
    records its own deps file). Before the fix this one spawn made the
    ambient context, and so every test of an xdist run, opaque."""
    rec = _recorder(tmp_path)
    rec.recording = True
    rec._handle_audit("subprocess.Popen",
                      (sys.executable, _gateway_argv(), None, None))
    exe = sys.executable.encode()
    rec._handle_audit("_posixsubprocess.fork_exec",
                      ([exe], [part.encode() for part in _gateway_argv()],
                       None))
    assert rec._ambient.opaque is False


def test_xdist_gateway_spawn_inside_a_test_is_opaque(tmp_path):
    rec = _recorder(tmp_path)
    rec.recording = True
    rec.enter_test("t.py::t1")
    rec._handle_audit("subprocess.Popen",
                      (sys.executable, _gateway_argv(), None, None))
    assert rec._current.opaque is True


def test_xdist_gateway_spawn_from_a_worker_is_opaque(tmp_path):
    rec = recorder.Recorder(checkout_root=str(tmp_path), run_id="r" * 32,
                            report_path=str(tmp_path / "rep.json"),
                            role="worker", worker_id="gw0")
    rec.recording = True
    rec._handle_audit("subprocess.Popen",
                      (sys.executable, _gateway_argv(), None, None))
    assert rec._ambient.opaque is True


@pytest.mark.parametrize("argv", [
    [sys.executable, "-c", _BOOT],
    [sys.executable, "-u", "-c", _BOOT + ";print(1)"],
    [sys.executable, "-u", "-c", "import sys"],
])
def test_near_gateway_spawns_stay_opaque(tmp_path, argv):
    rec = _recorder(tmp_path)
    rec.recording = True
    rec._handle_audit("subprocess.Popen", (argv[0], argv, None, None))
    assert rec._ambient.opaque is True


def test_fork_exec_outside_binary_stays_clean(tmp_path):
    outside = shutil.which("true")
    if outside is None:
        pytest.skip("no true binary on PATH")
    rec = _recorder(tmp_path)
    rec.recording = True
    rec.enter_test("t.py::t1")
    blob = outside.encode()
    rec._handle_audit("_posixsubprocess.fork_exec",
                      ([blob], [blob], None))
    assert rec._current.opaque is False


def test_forkserver_connect_marks_opaque(tmp_path, monkeypatch):
    """N3: a pool served by an already-running forkserver still taints.

    Once the forkserver runs, later pools raise no spawn audit event in
    this process — only socket.connect to the server socket. Dialling
    that exact address must mark the context opaque; any other socket
    use must stay clean.
    """
    server = "/tmp/pymp-test/sock-abc123"
    fake = SimpleNamespace(
        _forkserver=SimpleNamespace(_forkserver_address=server))
    monkeypatch.setitem(sys.modules, "multiprocessing.forkserver", fake)
    rec = _recorder(tmp_path)
    rec.recording = True
    rec.enter_test("t.py::t1")
    assert rec._current.opaque is False
    rec._handle_audit("socket.connect", (object(), server))
    assert rec._current.opaque is True


def test_other_socket_connect_stays_clean(tmp_path, monkeypatch):
    fake = SimpleNamespace(_forkserver=SimpleNamespace(
        _forkserver_address="/tmp/pymp-test/sock-abc123"))
    monkeypatch.setitem(sys.modules, "multiprocessing.forkserver", fake)
    rec = _recorder(tmp_path)
    rec.recording = True
    rec.enter_test("t.py::t1")
    rec._handle_audit("socket.connect", (object(), "/tmp/pymp-test/sock-other"))
    assert rec._current.opaque is False
    rec._handle_audit("socket.connect", (object(), ("127.0.0.1", 8080)))
    assert rec._current.opaque is False
    rec._handle_audit("socket.connect", ())
    assert rec._current.opaque is False


_REAL_FORKSERVER_SCRIPT = r"""
import concurrent.futures, json, multiprocessing, sys
from ptest.runtime import selection_recorder as recorder

rec = recorder.Recorder(checkout_root=sys.argv[1], run_id="c" * 32,
                        report_path=sys.argv[1] + "/report.json",
                        role="controller")
rec.recording = True
sys.addaudithook(rec._handle_audit)
ctx = multiprocessing.get_context("forkserver")
seen = {}
for name in ("t.py::first", "t.py::second"):
    rec.enter_test(name)
    with concurrent.futures.ProcessPoolExecutor(1, mp_context=ctx) as pool:
        assert pool.submit(abs, -1).result(timeout=30) == 1
    seen[name] = rec._current.opaque
    rec.exit_test()
print(json.dumps(seen))
"""


def test_real_forkserver_second_pool_marks_opaque(tmp_path):
    """N3 against CPython's real forkserver module shape.

    The first pool starts the server (fork_exec); the second only dials
    its socket. Both contexts must be opaque. Failed before the fix: the
    address was read from the module, where CPython never stores it, so
    the second context stayed clean.
    """
    proc = subprocess.run(
        [sys.executable, "-c", _REAL_FORKSERVER_SCRIPT, str(tmp_path)],
        capture_output=True, text=True, timeout=60,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")})
    assert proc.returncode == 0, proc.stderr
    seen = json.loads(proc.stdout.strip().splitlines()[-1])
    assert seen == {"t.py::first": True, "t.py::second": True}


def test_no_forkserver_socket_connect_stays_clean(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "multiprocessing.forkserver",
                        SimpleNamespace())
    rec = _recorder(tmp_path)
    rec.recording = True
    rec.enter_test("t.py::t1")
    rec._handle_audit("socket.connect", (object(), "/tmp/pymp-test/sock-abc123"))
    assert rec._current.opaque is False


_RMTREE_SCRIPT = r"""
import json, os, shutil, sys
from ptest.runtime import selection_recorder as recorder

checkout, outside = sys.argv[1], sys.argv[2]
rec = recorder.Recorder(checkout_root=checkout, run_id="d" * 32,
                        report_path=checkout + "/report.json",
                        role="controller")
rec.recording = True
os.chdir(checkout)
sys.addaudithook(rec._handle_audit)
shutil.rmtree(outside)
print(json.dumps(sorted(rec._ambient.data)))
"""


def test_rmtree_outside_the_checkout_records_no_phantom_data(tmp_path):
    """pytest cleans old temp dirs with shutil.rmtree's fd walk; its
    dir_fd-relative opens must not land as data paths. Before the fix
    each removed subdirectory became one, overflowing the 4096 cap and
    making the whole run opaque."""
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    outside = tmp_path / "pytest-of-user" / "pytest-1"
    for index in range(30):
        (outside / f"test_case_{index}0" / "sub").mkdir(parents=True)
        (outside / f"test_case_{index}0" / "sub" / "f.txt").write_text("x")
    proc = subprocess.run(
        [sys.executable, "-c", _RMTREE_SCRIPT, str(checkout), str(outside)],
        capture_output=True, text=True, timeout=60,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")})
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout.strip().splitlines()[-1]) == []
    assert not outside.exists()


def test_directory_and_dir_fd_opens_are_not_data(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "cfg.json").write_text("{}")
    rec = _recorder(tmp_path)
    rec.recording = True
    rec.enter_test("t.py::t1")
    rec._handle_audit("open", ("data", None, os.O_RDONLY | os.O_DIRECTORY))
    rec._handle_audit("open", (str(tmp_path / "data"), "r", None))
    rec._handle_audit("open", ("phantom_dir0", None, os.O_RDONLY))
    assert rec._current.data == set()
    # A real relative os.open, and a builtin open of an absent file (an
    # absence the test may depend on), are still recorded.
    rec._handle_audit("open", ("data/cfg.json", None, os.O_RDONLY))
    rec._handle_audit("open", ("data/missing.json", "r", None))
    assert rec._current.data == {"data/cfg.json", "data/missing.json"}


# ---------------------------------------------------------------------------
# Lazy initialisation: state the first caller built and later tests reuse.
# ---------------------------------------------------------------------------

_LAZY_MODULE = '''\
import threading

_engine = None
_built = None


def _read():
    with open(DATA) as fh:
        return fh.read()


def build():
    from pkg import migr
    return migr.VALUE + _read()


def get_engine():
    global _engine
    if _engine is None:
        _engine = build()
    return _engine


def branch_only():
    return 1


def dispatch(flag):
    return branch_only() if flag else 0


def thread_init():
    global _built
    _built = "built"


def fixture_body():
    if _built is None:
        worker = threading.Thread(target=thread_init)
        worker.start()
        worker.join()
    return _built
'''

_LAZY_SCRIPT = r'''
import json, sys
from types import SimpleNamespace
checkout = sys.argv[1]
sys.path.insert(0, checkout)
from ptest.runtime import selection_recorder as module
import os
os.chmod(checkout, 0o700)
rec = module.Recorder(checkout_root=checkout, run_id="c" * 32,
                      report_path=checkout + "/rep.json", role="controller")
assert rec.activate() is True, rec.inactive_reason
import pkg.lazy as lazy
lazy.DATA = checkout + "/data/seed.txt"
fixture = SimpleNamespace(scope="function", baseid="", argname="state",
                          func=lazy.fixture_body)

def run(nodeid, body):
    rec.enter_test(nodeid)
    body()
    rec.exit_test()

def first():
    rec.enter_function_fixture(fixture)
    try:
        lazy.fixture_body()
    finally:
        rec.exit_function_fixture()
    lazy.get_engine()
    lazy.dispatch(False)

def second():
    rec.enter_function_fixture(fixture)
    try:
        lazy.fixture_body()
    finally:
        rec.exit_function_fixture()
    lazy.get_engine()
    lazy.dispatch(True)

def third():
    lazy.dispatch(False)

def fourth():
    lazy.dispatch(False)

run("tests/test_l.py::test_first", first)
run("tests/test_l.py::test_second", second)
run("tests/test_l.py::test_third", third)
run("tests/test_l.py::test_fourth", fourth)
path = rec.write_deps()
rec.deactivate()
from pathlib import Path
from ptest import selection_ingest
run = selection_ingest.read_run(Path(checkout) / "rep.json", run_id="c" * 32,
                                expected_workers=1)
assert run.complete, run.notes
vb = run.vocabulary
out = {}
for nodeid, node in run.nodes.items():
    out[nodeid.rsplit("::", 1)[1]] = {
        "functions": sorted(vb.paths[vb.functions[i][0]] + "::"
                            + vb.functions[i][1] for i in node.deps.functions),
        "modules": sorted(vb.paths[i] for i in node.deps.modules),
        "data": sorted(vb.paths[i] for i in node.deps.data),
        "opaque": node.deps.opaque,
    }
print(json.dumps(out))
'''


def _lazy_project(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "lazy.py").write_text(_LAZY_MODULE, encoding="utf-8")
    (tmp_path / "pkg" / "migr.py").write_text("VALUE = 'v'\n",
                                              encoding="utf-8")
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "seed.txt").write_text("s", encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "-c", _LAZY_SCRIPT, str(tmp_path)],
        capture_output=True, text=True, timeout=60, cwd=str(tmp_path),
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")})
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_memoised_state_reaches_every_later_caller(tmp_path):
    """A test that reuses state the first caller built depends on the code,
    lazily imported modules and data that built it. Before the fix only the
    first caller recorded them (persea control-plane: a template database
    migrated once per worker, 575 missed failures on a migration edit)."""
    nodes = _lazy_project(tmp_path)
    first, second = nodes["test_first"], nodes["test_second"]
    for name in ("pkg/lazy.py::build", "pkg/lazy.py::_read"):
        assert name in first["functions"]
        assert name in second["functions"]
    assert "pkg/migr.py" in first["modules"]
    assert "pkg/migr.py" in second["modules"]
    assert "data/seed.txt" in first["data"]
    assert "data/seed.txt" in second["data"]
    assert not second["opaque"]


def test_thread_started_by_a_fixture_reaches_later_fixture_users(tmp_path):
    """Code a fixture setup ran on another thread has no project caller on
    its stack; the active function-scoped fixture owns it."""
    nodes = _lazy_project(tmp_path)
    assert "pkg/lazy.py::thread_init" in nodes["test_first"]["functions"]
    assert "pkg/lazy.py::thread_init" in nodes["test_second"]["functions"]


def test_one_shot_branch_of_a_shared_caller_stays_local(tmp_path):
    """Precision: a branch only one test takes, under a caller other tests
    ran first, is that test's alone; tests that never ran the lazy caller
    get none of its state."""
    nodes = _lazy_project(tmp_path)
    assert "pkg/lazy.py::branch_only" in nodes["test_second"]["functions"]
    for name in ("test_first", "test_third", "test_fourth"):
        assert "pkg/lazy.py::branch_only" not in nodes[name]["functions"]
    for name in ("test_third", "test_fourth"):
        assert "pkg/lazy.py::build" not in nodes[name]["functions"]
        assert "pkg/migr.py" not in nodes[name]["modules"]
        assert nodes[name]["data"] == []


@pytest.mark.parametrize("argv_tail, expected", [
    (["-m", "pkg"], "pkg/__main__.py"),
    (["-u", "-X", "dev", "-m", "pkg.tool"], "pkg/tool.py"),
    (["-mpkg.tool", "--flag"], "pkg/tool.py"),
    (["scripts/run.py", "-m", "x"], "scripts/run.py"),
    (["-c", "import pkg"], None),
    (["-m", "absent_mod"], None),
])
def test_spawned_project_interpreter_records_its_entry(tmp_path, argv_tail,
                                                       expected):
    """A test that starts this interpreter on a project module or script
    stays opaque and records the entry, so the static rule can follow the
    child's imports."""
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "__main__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "tool.py").write_text("", encoding="utf-8")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "run.py").write_text("", encoding="utf-8")
    rec = _recorder(tmp_path)
    rec.recording = True
    rec.enter_test("t.py::t1")
    argv = [sys.executable, *argv_tail]
    rec._handle_audit("subprocess.Popen",
                      (sys.executable, argv, str(tmp_path), None))
    assert rec._current.opaque is True
    assert rec._current.modules == ({expected} if expected else set())
