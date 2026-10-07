# Design: ptest 0.5 dependency-recorded test selection

Date: 2026-10-07. Author: architect. Branch: feature/dynamic-selection
(base f478189, 0.4.10). Worktree: /home/ingmar/worktrees/ptest/cc-dynamic-selection/ptest.
Authoritative spec: docs/specs/2026-10-07-ptest-0.5-selection.md (byte copy of
/home/ingmar/worktrees/ptest/specs/2026-10-07-ptest-0.5-selection.md): sections
5, 6.1-6.8, N1-N13, abuse table, R1-R9, A1-A6. This document fixes decisions,
frozen interfaces, ownership and tests. Where it is silent, the spec governs.
Precedent for every private file the bridge writes or reads:
src/ptest/stack_dumps.py plus bridge `_register_stack_dump` (O_EXCL|O_NOFOLLOW,
0600, owner/mode/nlink checks on read, bounded scans, never follow links).

## 0. Barrier: shared contracts content (applied before T1-T6 start)

All `src/ptest/contracts.py` changes are in section 12 ("Shared-file
content"), and they are a barrier. A transcription step applies them to the
chain base before the task worktrees branch. Every task imports these types
and constants. T5 is the owner of record for contracts.py and makes NO further
contracts.py edits: it only adds tests. If any task finds a defect in the
frozen block, it stops and reports BLOCKED with the exact problem. It never
patches contracts.py locally.

After the barrier, `repr(SelectionPolicy(...))` and therefore `_policy_digest`,
the source-snapshot compatibility MAC and every existing baseline/verified
proof stay byte-identical to 0.4.10. This was checked against the base:
sha256(repr(default policy)) starts with 43bc98485c25 before and after.

## 1. Decisions

D1  Engine split. `impact.plan` (T1) stays the static engine with an
    unchanged positional signature (the CLI tests stub it with 4 positional
    parameters). The dynamic engine is a refinement step in the new
    `selection_engine.py` (T5). That step runs only when the static verdict
    says `dynamic_ok` (D3), the runner is pytest, `[selection] dynamic` is
    true and a usable store exists. On any failure it falls back to the
    static verdict with a named reason (G2: falling back is never an error).
    A `dynamic_ok` verdict whose `project_index` is None is not a failure:
    the step builds the index itself (section 2.7, "Index rule").

D2  Parse cache handoff. `impact.plan` takes optional keyword-only
    `key`/`cache`/`conftest_edges`, and otherwise reads the ContextVar
    `impact.PLANNING` (a `C.PlanningContext`). The CLI sets that ContextVar
    around its existing positional calls, so the stubbed test modules keep
    working. `impact.plan` returns the `ProjectIndex` it built in
    `Impact.project_index` under the D3 index invariant, and the dynamic
    step reuses it, so the index is built once per plan. On the early-return
    verdicts that 0.4 produced without a walk ("is outside the import
    graph", "is test support") and on test-file-only changes (no seeds),
    T1 now builds the index too; that walk costs what any source edit
    already cost in 0.4.

D3  `Impact.dynamic_ok` is True exactly for verdicts the import graph
    produced:
    - kind "selected";
    - kind "none" with a non-empty `relevant`;
    - kind "full" whose reason is the full_ratio cut, the 200-file limit,
      "is test support" or "is outside the import graph".

    It is False for git unavailable, any full trigger, vitest or a non-pytest
    runner, selection disabled, an unparseable changed file, an import graph
    that is too large, and "no relevant changes".

    Index invariant (frozen; T1 guarantees it, T5 relies on it):
    - `dynamic_ok is True` ⇔ `project_index` is a `C.ProjectIndex` returned
      by `source_index.build_project_index(project_root, config, key=...,
      cache=..., conftest_edges=...)` during this `plan` call (same
      key/cache/conftest_edges as the plan), with `complete is True`.
    - This holds on every dynamic_ok path, including the three that return
      before 0.4 built any graph: "{path} is outside the import graph",
      "{path} is test support", and "selected"/"none" verdicts where only
      test files changed (no seeds). T1 builds the index on those paths
      before returning.
    - When the build on such a path returns `complete is False` (the walk
      exceeded MAX_SCAN_FILES), the verdict's kind, files and reason stay
      exactly as in 0.4, but `dynamic_ok` is False and `project_index` is
      None (the "too large" rule above).
    - On every other verdict `project_index` is None.
    - `engine == "static"` exactly when `dynamic_ok` is True, else `""`.

D4  File digest. The digest is `C.selection_file_digest(key, raw)`, which is
    HMAC-SHA256 with the domain fingerprint key. It is byte-identical to
    `source.snapshot` FileFingerprint digests, so records (from the run's
    input snapshot) and plans (from the source index) compare directly.
    Without a key (no run has ever executed) the engine is static ("no
    dependency records yet") and no cache is used. Unkeyed content hashes
    are never stored.

D5  Run baselines (soundness of name changes). Every ingested run stores a
    baseline: the keyed digest of every project `.py` file and every recorded
    data path at that run, plus the run's ambient deps. A test record points
    at its run. The planner judges each test against its own run's baseline,
    never against "the newest version". This keeps N1 for constants or other
    names a test reads but never executed as code. Analyses are memoised by
    the run's stale signature `frozenset((path, baseline digest))`, with at
    most C.SELECTION_MAX_SIGNATURES distinct signatures per plan. Tests of
    the oldest runs beyond that cap become unrecorded (static rule).

D6  Qualname identity. A recorded code object maps to
    `(relpath, C.selection_normalize_qualname(co_qualname))`, where None
    means module-level code (a module-body record). The source index uses the
    same normalised names: nested functions, lambdas and comprehensions fold
    into their outermost function or method scope, whose body fingerprint
    includes them.

D7  Narrowed execution. argv is the selected test files, at most 200, as
    today. `RunRequest.deselect` (barrier field) carries the recorded,
    unselected node ids inside partially selected files. Operations writes
    them to the private binding `<report>.deselect`, and the bridge deselects
    exactly those ids, in scoped execution only, through
    `pytest_deselected`. Unknown ids always run. Any problem with the binding
    means it is ignored and every test in the argv files runs (a superset,
    safe).

D8  Full conversion. A dynamic selection runs as `--full` when any of these
    holds:
    - `selected >= full_ratio * recorded` (recorded > 0);
    - `len(files) >= full_ratio * total_files`;
    - `len(files) > impact.MAX_SELECTED` (200; the argv limit);
    - the planner returns `full_reason`.

    Static selections keep the 0.4 cut-offs unchanged.

D9  Recording is enabled for a run when all of these hold: runner pytest,
    `config.selection.enabled`, `config.selection.dynamic`, not shadow, no
    probe, and a report binding exists. Operations then sets
    `PTEST_SELECTION_RECORD=1` in the report env block. The bridge records
    only when it is `__main__` (the controller) or the `-p pytest_bridge`
    import in an xdist worker, and Python is 3.12 or newer. An in-process
    `run()`, which ptest's own suite uses, never records.

D10 Ingest (N8). Records are written only when all of these hold: the guard
    handoff was valid, the native report was consumed with no
    report_reason, there was no cancellation, no guard problem and no
    setup failure, `input_after.digest` is not None,
    `_source_invalidation(input_before, input_after) is None`, and
    `read_run(...).complete`. Otherwise no dependency data is written, and:
    - node ids observed with outcome failed or error in any valid deps file
      get `mark_outcomes(... "failed"/"error")` (N4);
    - when files are missing or invalid, `invalidate(argv test files, or
      None for full)` marks those records outcome "unknown", so they rerun.

    These two writes only ever widen selection.

D11 Self-audit. It runs only on an ingestible full run
    (`plan.execution == "full"`). Before `update`, the engine plans against
    the pre-update snapshot and the current index with `changed=frozenset()`.
    It calls `selection_planner.audit_misses`, then `store.demote(misses)`,
    then `store.record_audit`, and prints the audit line. A demotion is keyed
    by the test file's keyed digest and clears when that digest changes
    (the planner checks it).

D12 No history change (N9). No new RunResult field, reason code or
    persisted text. Engine, units, audit and store facts go to stderr lines
    only. `REASON_CODES` and `_PERSISTED_REASON_ALIASES` stay as at f478189.

D13 Monorepo children. A plan is made per child with the child's config and
    project id (`projects/<child project_id>/selection.db`). Paths are
    child-relative, like the snapshot. Root-level `../` snapshot entries are
    never recorded.

D14 The bridge stays stdlib-only. `runtime/selection_recorder.py` is imported
    by the bridge with
    `try: from . import selection_recorder except ImportError: import selection_recorder`.
    Both imports sit inside one `try/except Exception` that leaves
    `_selection_recorder = None`, because tests copy or shadow the bridge
    file alone (test_pytest_parallel_subprocess) and the bridge must still
    import and run. After import the bridge verifies that the module's
    realpath is beside `pytest_bridge.py`. If the import failed or the
    module is elsewhere, recording is inactive with reason
    "recorder could not start".

D15 `uninstall` moves from the triage's T5 to T3, which owns the store path
    and deletion rules. `ptest status` (cli.py) stays with T5.

## 2. Frozen interfaces

### 2.1 contracts.py (section 12, barrier)

- Constants: SELECTION_*, SOURCE_INDEX_VERSION.
- Helpers:
  - `selection_relpath_safe`, `selection_code_path`, `selection_data_path`;
  - `selection_nodeid_safe`, `selection_test_file`;
  - `selection_normalize_qualname`, `selection_name_key`, `selection_key_path`;
  - `selection_file_digest`, `selection_ids`, `selection_static_reach`;
  - `selection_store_path`, `selection_deps_path`, `selection_deps_pid`,
    `selection_deselect_path`, `selection_empty_context`.
- Types:
  - `ImportIndex`, `ScopeIndex`, `ClassIndex`, `StatementIndex`, `FileIndex`;
  - `ProjectFile`, `ProjectIndex`, `ParseCache` (Protocol: get_many/put_many),
    `PlanningContext`;
  - `DepVocabulary`, `ContextDeps` (arrays of ids, built only via
    `selection_ids`), `RunBaseline`, `NodeRecord`, `FixtureRecord`,
    `DependencySnapshot`, `SelectionStoreMeta`;
  - `RecordedNode`, `RunDependencies`;
  - `SelectionInputs`, `ChangedUnit`, `SelectionDecision`.
- `SelectionPolicy.dynamic: bool = field(default=True, repr=False)`.
- `RunRequest.deselect: tuple = ()`. It is validated with
  `selection_nodeid_safe`, at most 200000 ids, and is scoped mode only.

Name keys are `f"{path}:{name}"`. Every id array is
`array("I")`, sorted and unique, and is only ever produced by
`C.selection_ids(...)`. Every snapshot id is a dense index into its
vocabulary tuples.

### 2.2 impact.py (T1 produces; T5 and T6 consume)

```python
PLANNING: contextvars.ContextVar[C.PlanningContext | None]  # name "ptest_impact_planning", default None
MAX_SCAN_FILES = 20000; MAX_FILE_BYTES = 2 * 1024 * 1024; MAX_SELECTED = 200   # unchanged

@dataclass(frozen=True, slots=True)
class Impact:
    kind: str                                  # "none" | "selected" | "full" | "vitest"
    changed: tuple[str, ...] = ()
    files: tuple[str, ...] = ()
    direct: int = 0
    via: int = 0
    total: int = 0
    reason: str = ""
    ignored: int = 0
    engine: str = ""                           # "" | "static" | "dynamic"
    dynamic_ok: bool = False                   # D3
    relevant: tuple[str, ...] = ()             # relevant changed paths under the project, sorted
    static_reason: str = ""                    # T5: frozen phrase (section 2.8)
    deselect: tuple[str, ...] = ()             # T5 dynamic
    tests: int = 0                             # T5 dynamic: recorded tests that will run
    reached: int = 0                           # T5 dynamic none
    units: tuple[str, ...] = ()                # T5 dynamic: summary phrase parts (section 2.8)
    details: tuple[str, ...] = ()              # T5: -v line bodies (unescaped)
    project_index: "C.ProjectIndex | None" = field(default=None, compare=False, repr=False)  # D3 index invariant

def plan(top, project_root, config, repo_changed, *, key: bytes | None = None,
         cache: C.ParseCache | None = None, conftest_edges: bool = True) -> Impact
```

- When `key` and `cache` are both None, `plan` uses `PLANNING.get()` if set.
- It sets `engine="static"` exactly when `dynamic_ok` is True and `""`
  otherwise.
- It follows the D3 index invariant: `dynamic_ok` ⇔ a complete
  `project_index` built in this call; None on every other verdict.
- `conftest_edges=False` reproduces 0.4.10 selection exactly (the T6
  harness uses it as v1).

### 2.3 source_index.py (T1 produces; T5 and T6 consume)

```python
def index_source(raw: bytes) -> C.FileIndex          # parse only, never import/exec (N11)
def encode_index(index: C.FileIndex) -> bytes        # deterministic, versioned with C.SOURCE_INDEX_VERSION
def decode_index(blob: bytes) -> C.FileIndex | None  # None on any malformed/other-version blob
def iter_python_files(project_root: Path) -> Iterator[str]   # = impact's walk (no symlinks, skip rules)
def read_source(project_root: Path, rel: str) -> bytes | None  # no-follow regular file <= impact.MAX_FILE_BYTES
def build_project_index(project_root: Path, config: C.Config, *, key: bytes | None,
                        cache: C.ParseCache | None, conftest_edges: bool = True) -> C.ProjectIndex
```

- `build_project_index` makes at most one `cache.get_many` call (for all
  digests) and at most one `cache.put_many` call (for the misses).
- Fingerprints are `sha256(ast.dump(node(s), include_attributes=False))` hex.
  Multiple nodes are joined with "\n".
- `ProjectFile.modules` is computed by impact's `_module_names` rules.
  `test`/`support` follow impact's `_is_test_file`/`_is_test_support`.
- `ProjectFile.imports` holds the resolved project paths of every import
  anywhere in the file, including package `__init__.py` prefixes (the 0.4
  `_import_edges` semantics).
- `reverse[p]` is the set of files whose `imports` contain p. With
  conftest_edges on, `reverse[".../conftest.py"]` also contains every test
  file under that conftest's directory.

Resolution: raw chains resolve against module-level bindings to name keys.

| Binding of the chain head | Resolves to |
|---|---|
| def/class/assign in this file | `this:head` |
| `import a.b` (`aliased=False`) | module `a`, then the chain continues |
| `import a.b as c` | module `a.b`, then the chain continues |
| `from M import y` (absolute or relative, resolved with the file's package) | a module when `M.y` is a project module, else `path(M):y` |
| star import from M | `path(M):head` when M binds `head` |
| anything else | unresolved and dropped |

- Walking into a module: if `m.part` is a project module, continue there;
  otherwise the result is `path(m):part`.
- An ambiguous module name (several paths) resolves to every path.
- `statement_refs` of an import statement are the resolved targets of its
  `from` imports.
- `statement_bound` of a star import is every name M binds that does not
  start with "_".

### 2.4 selection_planner.py (T2 produces; T5 consumes)

```python
def plan(inputs: C.SelectionInputs) -> C.SelectionDecision
def needed_versions(index: C.ProjectIndex, deps: C.DependencySnapshot, compatibility: str) -> frozenset[str]
def needed_data_paths(deps: C.DependencySnapshot, compatibility: str) -> frozenset[str]
def is_selected(decision: C.SelectionDecision, nodeid: str) -> bool
def audit_misses(decision: C.SelectionDecision, deps: C.DependencySnapshot,
                 failed: Iterable[str]) -> tuple[str, ...]
```

- `needed_versions`: the baseline digests of stale `.py` paths across
  compatible runs; the caller fetches them from the cache.
- `needed_data_paths`: every recorded data path of compatible runs; the
  caller digests them now.
- `is_selected`: `full_reason is not None`, or the node's file is in `files`
  and the node is not in `deselect`.
- `audit_misses`: sorted failed node ids whose prior record outcome is in
  PASSING and that `is_selected` would not run.

It is pure: no I/O, no clock, deterministic output ordering. The normative
rules are in section 4.

### 2.5 selection_store.py and selection_ingest.py (T3 produces; T5 and T6 consume)

```python
# selection_store.py
def open_store(domain: C.DomainPaths, project_id: str, *, create: bool) -> SelectionStore
    # Raises C.Problem only:
    #   state-unavailable    absent with create=False, or I/O
    #   unsafe-path          symlink/foreign/mode/nlink on the dir or db
    #   coordinator-corrupt  unreadable schema
    #   capacity-exceeded
    # Uses storage.open_database (pragmas, hot-journal recovery) under
    # files.ensure_private_dir(<state>/projects/<id>).
def remove_store(domain: C.DomainPaths, project_id: str) -> bool
    # Identity-checked unlink of selection.db (+ "-journal"); never follows links.

class SelectionStore:                      # context manager; close() idempotent
    def get_many(self, digests: Iterable[str]) -> dict[str, bytes]      # ParseCache (current SOURCE_INDEX_VERSION only)
    def put_many(self, blobs: Mapping[str, bytes]) -> None              # ParseCache
    def snapshot(self) -> C.DependencySnapshot
    def meta(self) -> C.SelectionStoreMeta
    def update(self, run: C.RunDependencies, *, run_id: str, recorded_at: float,
               compatibility: str, digests: Mapping[str, str | None], full: bool) -> None
    def mark_outcomes(self, outcomes: Mapping[str, str]) -> None        # existing records only
    def invalidate(self, test_files: Iterable[str] | None) -> None      # outcome "unknown"; None = all
    def demote(self, nodeids: Mapping[str, str]) -> None                # nodeid -> test-file digest
    def record_audit(self, checked: int, misses: int) -> None
    def note_inactive(self, reason: str | None, python: tuple[int, int] | None) -> None
```

`update` is one `BEGIN IMMEDIATE` transaction:
- It replaces exactly `run.nodes` (other nodes are kept), replaces the
  fixture records it carries, and stores a RunBaseline from `digests` (every
  `.py` and recorded data path) plus `run.ambient`.
- A recorded code or data path whose digest is None or missing makes every
  referencing context opaque.
- When `full` is true, it prunes nodes absent from this and the previous
  full inventory.
- It drops unreferenced runs, then evicts the oldest runs' nodes while the
  size exceeds SELECTION_STORE_TARGET_BYTES, keeping the newest run. If
  SQLITE_FULL hits at the hard cap, it evicts and retries once, else raises
  `C.Problem("capacity-exceeded")`.
- Cache blobs unused for 14 days and unreferenced are pruned.

```python
# selection_ingest.py
def read_run(report_path: Path, *, run_id: str, expected_workers: int) -> C.RunDependencies   # never raises
def write_deselect_binding(report_path: Path, run_id: str, nodeids: Sequence[str]) -> Path     # raises C.Problem
def cleanup(report_path: Path) -> None   # own deps files + deselect binding only; never raises
```

### 2.6 Bridge file contracts (T4 writes the deps file and reads the binding; T3 the reverse)

Deps file: `C.selection_deps_path(report_path, os.getpid())`, opened with
`os.open(path, O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW|O_CLOEXEC, 0o600)`. Its
content is a single JSON object, `json.dumps(..., ensure_ascii=True,
separators=(",", ":"))`, written once at session end:
- the serial controller writes it after `pytest.main` returns;
- the xdist controller writes it after `pytest.main` returns;
- each xdist worker writes it in its worker-half `pytest_sessionfinish`.

```
{"format": "ptest-selection-deps-v1", "run_id": <PTEST_RUN_ID>, "role": "controller"|"worker",
 "worker_id": null|"gwN", "pid": <int, equals the name>, "python": [major, minor],
 "recording": <bool>, "inactive_reason": null|<str <= 200 chars>,
 "workers": [<"gwN" ids that reported collection or node-down>]   (controller; [] otherwise),
 "overflow": <bool>, "tamper": <bool>, "deselect": "none"|"applied"|"ignored", "deselected": <int>,
 "paths": [<checkout-relative posix path>, ...],
 "functions": [[<path index>, <raw co_qualname>, <co_firstlineno>], ...],
 "ambient": CTX,
 "fixtures": [{"key": [<baseid>, <argname>, <scope>], ...CTX}, ...],
 "nodes": [{"nodeid": <str>, "outcome": <SELECTION_OUTCOMES>, "fixtures": [<fixtures index>...], ...CTX}, ...]}
CTX = {"functions": [<functions index>...], "modules": [<paths index>...],
       "data": [<paths index>...], "opaque": <bool>}
```

- The worst-phase outcome order is error (setup/teardown failure) >
  failed (call) > xpassed > passed > xfailed > skipped. A node that never
  got a final report is "unknown".
- On a Python older than 3.12 the bridge writes `recording: false`,
  `inactive_reason: "Python 3.N has no sys.monitoring"` and no nodes.
- When the encoded size exceeds C.SELECTION_DEPS_MAX_BYTES, it writes the
  same header with `overflow: true` and empty paths, functions, fixtures and
  nodes, and an empty ambient.
- Ingest normalises qualnames with `selection_normalize_qualname`. A None
  result becomes a module-body entry.
- A path entry that fails `selection_code_path` (for a function or module
  reference) or `selection_data_path` (for a data reference) makes its
  context opaque. Ingest never drops such entries silently.

Completeness (`RunDependencies.complete`):
- serial (`expected_workers == 1`): exactly one valid controller file with
  overflow false;
- xdist: one valid controller file, `len(workers) == expected_workers`, and
  one valid, non-overflow worker file per listed worker id.

Deselect binding: path `C.selection_deselect_path(report_path)`, written by
`write_deselect_binding` with O_EXCL|O_NOFOLLOW|O_CLOEXEC and mode 0600.
Content:

```
{"format":"ptest-selection-deselect-v1","run_id":"<32 hex>","nodeids":[...]}
```

- The writer raises ValueError if any id fails `selection_nodeid_safe`, so
  the caller drops unsafe ids for that whole file first (D7).
- The bridge accepts the binding only when all of these hold:
  - env `PTEST_SELECTION_DESELECT == str(report) + ".deselect"`;
  - lstat shows a regular file with the current uid, `mode & 0o077 == 0`,
    nlink 1 and size within C.SELECTION_DESELECT_MAX_BYTES;
  - it opens with O_NOFOLLOW;
  - format and run_id match;
  - nodeids is a list of str;
  - `PTEST_EXECUTION == "scoped"`.

  Otherwise it records `"deselect": "ignored"` and deselects nothing.

Environment (set by operations, T5): `PTEST_SELECTION_RECORD=1` (D9), and
`PTEST_SELECTION_DESELECT=<binding path>` when a binding was written.

### 2.7 selection_engine.py (T5 produces; T6 consumes `preview`)

```python
def preview(domain: C.DomainPaths, top: Path | None, project_root: Path, config: C.Config,
            repo_changed: tuple[str, ...] | None) -> "impact.Impact"
```

It is static `impact.plan` plus the dynamic refinement, with no stderr output
and no record writes; only parse-cache puts are allowed. It never raises for
state problems. The CLI path, `after_run` and the compatibility function are
T5-internal. The engine reaches T1-T3 only through module-level lazy accessor
functions (`_impact()`, `_source_index()`, `_planner()`, `_store()`,
`_ingest()`), which T5's unit tests monkeypatch with fakes.

Index rule (T5; consumes the D3 index invariant). When the static verdict
has `dynamic_ok` True:
- `project_index` is a `C.ProjectIndex` with `complete` True: use it as
  `SelectionInputs.index`. Never build a second index.
- `project_index` is None (a stubbed or foreign planner; T1 never does
  this): build it once with
  `_source_index().build_project_index(project_root, config, key=key,
  cache=cache)`, using the same key and cache as the planning context, and
  continue with the dynamic step. This is not a fallback and prints no
  reason.
- The index (given or built) has `complete` False, or the build raises:
  static verdict with reason `dynamic planner failed`.

Compatibility fingerprint (T5-internal; it must be identical at plan and
ingest time): `hmac(key)` over the JSON of
- `C.SELECTION_PROTOCOL`;
- `sha256(repr(config.selection))`;
- `config.runner.kind.value`, `list(config.runner.launcher)`,
  `list(config.runner.args)`;
- `platform.system()`, `platform.machine()`;
- a sorted list of `[name, keyed digest or ""]` for these files at the
  project root when present: uv.lock, poetry.lock, Pipfile.lock,
  pyproject.toml, setup.py, setup.cfg, pytest.ini, tox.ini, .ptest.toml,
  .python-version, requirements*.txt;
- each literal `full_triggers` path.

### 2.8 User-visible text (T5 emits; T6 documents verbatim)

`{head}` is the existing `"{reference}: {first} (+N files)"`. Counts use
correct singular and plural forms (`C.plural` where it fits). Every path,
label and reason is passed through `render.terminal_text`.

Start-line notes:
- Dynamic selection: `{head} → {T} tests in {F} of {M} files (dynamic · {summary})`
  - `{summary}` joins the non-zero parts with ", " in this order:
    `N functions changed`, `N names changed`, `N modules changed`,
    `N data files changed`, `N test files changed`,
    `N unrecorded test files`.
  - With no non-zero part it is `dependencies changed`.
  - The parts come from `decision.units`, counting entries by kind:
    function, name, module, data, file, unrecorded.
- Dynamic, nothing selected, R > 0:
  `{head} → no tests affected: {R} tests reach these changes and already passed on this code`
  (singular: `1 test reaches`). With R == 0 the 0.4 text is kept:
  `{head} → no tests affected · ptest --full runs everything`.
- Static fallback on a pytest project, kind selected:
  `{head} → {N} of {M} test files (static: {reason} · {D} direct · {V} via importers)`.
  For non-pytest runners, stubbed planners and static verdicts other than
  "selected", the 0.4 note is unchanged byte for byte.
- Dynamic → full:
  - `{reference} → full suite: {T} of {R} recorded tests reach full_ratio {ratio:g} (dynamic)`;
  - `{reference} → full suite: {F} of {M} test files reach full_ratio {ratio:g} (dynamic)`;
  - `{reference} → full suite: {F} test files exceed the 200-file scoped limit (dynamic)`;
  - `{reference} → full suite: {full_reason} (dynamic)`.

Static reasons (exact):
- `no dependency records yet — any run records them`
- `dynamic = false in .ptest.toml`
- `dependency store unavailable`
- `dependency records are from another configuration`
- `test Python 3.N cannot record dependencies (needs 3.12+)`
- `recording was unavailable: {inactive_reason}`
- `dynamic planner failed`

Recorder inactive reasons (bridge, exact): `Python 3.N has no
sys.monitoring`, `no free sys.monitoring tool id`, `recorder could not
start`. T2 full_reason (exact): `ambient data file {path} changed`.

`-v` lines (prefix `ptest: -v selection: `):
- `engine dynamic · {n} tests recorded · coverage {pct}% of test files · store {size} · newest {age} ago`
- `engine static: {reason}`
- `changed {kind} {label} → {n} tests` (at most 20, then `… +{k} more changed units`)
- `fallback {target}: {reason}` (at most 20, then `… +{k} more fallbacks`)
- `recorded {n} tests from {p} processes` / `not recorded: {reason}`

Audit:
- Always (respects `-q`):
  `ptest: selection audit: {N} failing tests would not have been selected — they now run whenever a change statically reaches them`
  (singular: `1 failing test would not have been selected`).
- With `-v`, even with no misses:
  `ptest: -v selection audit: {checked} failing tests checked · {misses} misses`.

Status (human only; JSON unchanged): one line per store that exists,
`selection store: {n} tests recorded · {size} · newest {age} ago`. In a
monorepo the line is `selection store {declaration}: …`. Status never
creates a store or writes to one.

## 3. Data flow

1. CLI changed routing (T5). `with engine.planning(domain, config)` sets
   `impact.PLANNING` to `(key, store-as-cache)`, then calls
   `impact.plan(top, root, config, changed)` (T1), then
   `engine.refine(...)`, which uses the T1 index (or builds it per the 2.7
   index rule), the T3 snapshot and the T2
   plan to return an Impact. That becomes `RunRequest(mode=SCOPED,
   argv=files, deselect=...)`, or FULL (D8), or a "none" line.
2. Operations (T5) sets the recording env (D9). For a scoped request with
   `deselect`, it calls `selection_ingest.write_deselect_binding` (T3) and
   sets the env var.
3. The bridge (T4):
   - activates `selection_recorder`: the serial controller just before
     `pytest.main`; an xdist worker at bridge-module import;
   - switches contexts in `pytest_runtest_protocol` and in a new
     `pytest_fixture_setup` wrapper for non-function scopes, calling
     `restart_events()` on each switch;
   - deselects in `pytest_collection_modifyitems` (scoped only);
   - writes the deps files at session end.
4. Operations, after report consumption:
   `engine.after_run(...)` → `selection_ingest.read_run` (T3) → (full)
   self-audit with the T2 planner → `store.update` or D10 fallbacks. It
   prints audit and `-v` lines before the end line. Its `finally` calls
   `selection_ingest.cleanup(report)`.

## 4. Planner decision procedure (normative for T2)

Notation:
- I is the current ProjectIndex and S the DependencySnapshot (vocabulary V,
  runs, nodes, fixtures, demotions).
- `cur(p)` is `I.files[p].digest`, or "" when the path is absent.
- PASSING is `C.SELECTION_PASSING_OUTCOMES`.

4.1 Usable records.
- A run r is compatible when `r.compatibility == inputs.compatibility`.
- A node n is usable when all of these hold:
  - its run is compatible;
  - `n.test_file ∈ I.test_files`;
  - its run's signature is within the cap (D5);
  - every fixture id it uses has a FixtureRecord. A missing one makes the
    node opaque, not unusable.
- Unusable nodes are "unrecorded".

4.2 Staleness of run r with baseline B:
- `S_r` = {.py paths p in B ∪ I : B.get(p) != cur(p)}. A p that is not in
  B is "added"; a p that is not in I is "removed".
- `D_r` = {recorded data paths d in B : B[d] != inputs.data_digests.get(d, "")}.
- The signature is `σ_r = frozenset((p, B.get(p)) for p in S_r) | frozenset(("data", d) for d in D_r)`.

4.3 Per-path diff, for p ∈ S_r, with old = `inputs.versions.get(B[p])` and
new = `I.files[p].index`.

Whole-path change applies when p is added or removed, old is missing, or
either side has `parsed=False`:
- every function of p counts as changed (path-wide flag PW);
- every name of p counts as changed (wildcard W);
- the effect flag is set (AM ∋ p) and the skeleton flag is set (SK ∋ p);
- if p is a test file, it is selected whole (WF).

Exception: a p that is added and not a test file contributes no PW (its
functions never ran) but does contribute W, AM and SK.

Otherwise the 5.3 rows apply:

| Old vs new | Effect |
|---|---|
| scope q missing in new, or body differs | CF ∋ (p, q) |
| skeleton differs | CF ∋ (p, q), and NAMES ∋ p:first-segment(q) |
| q new in a test file | WF ∋ p |
| class c missing in either, or its skeleton or body differs | NAMES ∋ p:first-segment(c); CF ∋ (p, c) and every (p, q) with q starting with c + "." in old ∪ new |
| new class in a test file | WF ∋ p |
| definition statements (def/class/import/assign): per bound name, the ordered tuple of fingerprints differs | NAMES ∋ p:name |
| a star-import (`"*"`) sequence differs | W ∋ p |
| effect statements: the multiset of fingerprints differs | AM ∋ p |
| any NAMES or AM entry for p | SK ∋ p |
| test file p with anything other than CF on scopes that already existed | WF ∋ p |

"doc" statements are ignored.

4.4 Propagation (fixpoint over I, per signature). A ref set matches when
any key k has `k ∈ NAMES`, or `selection_key_path(k) ∈ W`. Repeat until
NAMES stops growing:
- a statement i of file f whose `statement_refs` match: if its kind is
  def/class/import/assign, add `statement_bound[i]` to NAMES; if it is
  effect, add f to AM;
- a class c of file f whose `class_refs` match: NAMES ∋ f:first-segment(c),
  plus CF ∋ (f, c) and every method;
- a scope q of file f whose `scope_refs` match: AF ∋ (f, q). Functions do
  not propagate further.

Finally, a test file f with any NAMES key of f, or f ∈ AM, goes to WF.

4.5 A usable node n of run r is selected when the first matching clause
fires. Every clause has a negative-twin test.

| Clause | Selected when |
|---|---|
| a | `n.outcome ∉ PASSING` |
| b | n is opaque, or any of its fixtures is opaque, or the run's ambient is opaque, or n is demoted (`demotions[n] == cur(test_file)`), AND any of: `test_file ∈ reach(S_r ∪ changed_py)` (4.7); D_r is non-empty; `inputs.changed` has a non-`.py` path. Otherwise it is NOT selected: the static rule proved it unaffected (N3). |
| c | `test_file ∈ WF` |
| d | for any function id in `n.deps.functions` or its fixtures' functions, with (p, q) = V.functions[id]: p ∈ PW, or (p, q) ∈ CF ∪ AF |
| e | any module path in n's or its fixtures' `modules` is in SK |
| f | any data path in n's or its fixtures' `data` is in D_r |
| g | ambient of run r: functions hit as in d mark their path ambient-changed; AM entries are ambient-changed too; `test_file ∈ reach(those paths)` selects. Any ambient data path in D_r sets `full_reason = f"ambient data file {d} changed"`, and nothing else matters. |

4.6 Unrecorded test files (no usable node). The file is selected whole when
it is in `reach(changed_py)` (4.7) or `inputs.changed` contains any
non-`.py` path. `changed_py` is the `.py` paths in `inputs.changed`.

4.7 `reach(seeds)` is
`C.selection_static_reach(I.reverse, seeds) ∩ I.test_files`, plus, for
each seed that is a support file other than conftest.py, every test file
under the test root that contains it.

4.8 Output:
- `files` = sorted(files of selected nodes ∪ WF ∪ 4.6 files).
- `deselect` = for each file in `files` not in `whole_files`, its usable
  nodes that were not selected, sorted.
- `selected` counts usable nodes that will run.
- `reached` counts usable unselected nodes whose deps (functions' or
  modules' paths, or data) touch S_r ∪ D_r ∪ `inputs.changed`.
- `recorded` is the number of usable nodes, and `total_files` is
  `len(I.test_files)`.
- `units` holds one ChangedUnit per changed function (CF only), name
  (NAMES seeds from 4.3, not propagated), AM module, D_r data path, changed
  test file, and unrecorded selected file. Each carries the count of nodes
  it selected; they are sorted by count descending, then label.
- `fallbacks` holds (target, reason) with the exact reasons:
  - `opaque: spawned a process or overflowed`
  - `demoted by the selection audit`
  - `no dependency record`
  - `record from another configuration`
  - `fixture record missing`
  - `too many recorded code versions`
- `coverage` is the share of test files with at least one usable node.

## 5. Tasks, acceptance criteria and tests

Global rules for every task:
- Follow secure-by-spec: write tests first, and show each abuse or negative
  test failing against the pre-change code (or a mutation of the new
  control) in the task report.
- Run only `ptest <your own test files>` from the worktree root. Never use
  bare `ptest`, `--base` or `--full` in a task, never run pytest directly,
  and never add `--workers`/`--timeout`.
- Run `graphify update .` after source changes.
- Commit explicit paths only, never `.pipeline/`.
- Do not use real-time sleeps for synchronisation.
- Tests above 3 s need a documented integration boundary in the module
  docstring. Use factories and small fixtures.
- Do not bump the version, tag, push, or run `ptest update`.
- Do not run the A1-A5 evaluation campaign.

### T1 Shared model consumers: source index, parse cache, static conftest edge

Owns:
- src/ptest/source_index.py (new)
- src/ptest/impact.py
- tests/ng/test_source_index.py (new)
- tests/ng/test_impact.py

(`selection_model.py` is dropped: the shared model is the contracts barrier.)

Acceptance:
- [ ] `index_source` produces scopes, classes, statements and imports per
  sections 2.1 and 2.3.
  - Comment, whitespace, blank-line and line-shift edits give identical
    fingerprints.
  - Changing a default changes the skeleton only; changing a body changes
    the body only.
  - A method body edit does not change its class's body fingerprint.
  - Nested defs and lambdas fold into the outer scope.
  - Duplicate qualnames (property setters, conditional defs) merge into one
    entry whose fingerprints cover both.
  - A syntax error, invalid UTF-8 or more than MAX_FILE_BYTES gives
    `parsed=False`.
  - The kind table is covered: a docstring is "doc"; `if TYPE_CHECKING:`,
    a call, for, with and try are "effect"; a star import has bound
    `("*",)`.
- [ ] N11: indexing a file whose import would `raise SystemExit`, write a
  file or print leaves no side effect, and no module is imported
  (sys.modules is unchanged).
- [ ] `encode_index`/`decode_index` round trip equal. `decode_index`
  returns None for truncated input, garbage, a wrong version and a
  wrong-type field.
- [ ] `build_project_index`:
  - module names equal `impact._module_names`;
  - the reverse graph equals the 0.4 name graph on a fixture tree
    (asserted against the old algorithm, kept as a private test helper);
  - conftest edges are present;
  - resolution covers aliases, `module.attr`, from-import of a module
    versus an attribute, relative imports, re-exports through `__init__`
    (import statement refs point at the target key), star-import expansion,
    and unresolved builtins being dropped;
  - one `get_many` and at most one `put_many` per build;
  - a warm build calls `ast.parse` zero times (counted with monkeypatch);
  - `key=None` uses no cache.
- [ ] `impact.plan`:
  - every existing test_impact test passes unchanged;
  - each file is read and parsed at most once per plan;
  - a module imported only by `tests/conftest.py` selects the tests under
    `tests/` (and nothing with `conftest_edges=False`, the 0.4.10 result);
  - `dynamic_ok`, `relevant`, `engine` and `project_index` follow D3 and
    its index invariant. One test per dynamic_ok path asserts
    `dynamic_ok is True`, `engine == "static"`,
    `isinstance(project_index, C.ProjectIndex)`, `project_index.complete`,
    and `set(project_index.files)` equal to the files of a direct
    `build_project_index` on the same tree. The paths are:
    1. "selected" through the graph (a source seed);
    2. "selected" with only a test file changed (no seeds);
    3. "none" with non-empty `relevant` from a source seed that reaches no
       test;
    4. "none" with non-empty `relevant` from a deleted test file only (no
       seeds);
    5. "full" by the full_ratio cut;
    6. "full" by the 200-file limit;
    7. "full" with "{path} is test support";
    8. "full" with "{path} is outside the import graph" (a data file).
  - One test per dynamic_ok False path asserts `project_index is None` and
    `engine == ""`: full trigger, non-pytest runner, vitest, selection
    disabled, unparseable changed file, import graph too large, and no
    relevant changes.
  - With `MAX_SCAN_FILES` monkeypatched below the tree size, paths 2, 7 and
    8 keep the exact 0.4 kind, files and reason, with `dynamic_ok` False and
    `project_index` None;
  - on paths 2, 7 and 8 (built with a counting dict-backed cache),
    `get_many` is called exactly once, so the index is built once per plan;
  - `PLANNING` is honoured, and explicit keyword arguments win over it;
  - `Impact(...)` built with only the 0.4 keyword arguments still works.
- [ ] Tests call the real module only. They need no store: a dict-backed
  fake `ParseCache` is enough.

### T2 Planner v2

Owns:
- src/ptest/selection_planner.py (new)
- tests/ng/test_selection_planner.py (new)

Acceptance:
- [ ] Every row of 4.3 and every clause of 4.5 has a positive test and a
  negative twin. They are built from hand-made contracts dataclasses with
  small helper builders in the test module.
- [ ] Propagation fixpoint coverage:
  - a constant used by a function body;
  - a decorator or default referencing a changed name (bound name changes);
  - a subclass of a changed class;
  - a re-export chain across two `__init__` hops;
  - wildcard paths;
  - an effectful statement referencing a changed name (AM);
  - a parametrize decorator in a test file referencing a changed constant
    (WF).
- [ ] Per-run baselines:
  - two runs with different baseline digests for the same file judge their
    nodes independently;
  - a node re-recorded after a constant change is not reselected, while a
    node from the older run still is (D5 / N1 regression);
  - signature memoisation is limited by the cap, and over the cap the oldest
    runs' nodes fall to the static rule.
- [ ] Fixtures:
  - a function changed inside a session-fixture record selects every node
    using that fixture (N1);
  - a missing fixture record makes the node opaque.
- [ ] Ambient:
  - an ambient function body change selects by static reach;
  - ambient data gives `full_reason`;
  - an opaque ambient makes the run's nodes opaque.
- [ ] Opaque or demoted nodes are never skipped while static reach hits
  (N3), and are skipped when static reach provably misses. A demotion
  clears when the test file digest changes.
- [ ] Unrecorded files follow 4.6 (N2). New parametrised ids are not in
  `deselect`. Deselect only lists usable nodes of partially selected files.
- [ ] A failed or errored last outcome is always selected (N4), including
  when nothing changed.
- [ ] `reached`, `recorded`, `coverage`, `units` (kinds, counts, order) and
  `fallbacks` (exact reasons) are correct. `needed_versions`,
  `needed_data_paths`, `is_selected` and `audit_misses` are covered.
- [ ] A missing old version gives the conservative whole-path result,
  never fewer selections.
- [ ] Determinism: identical inputs give identical output (tuples sorted).
- [ ] Optional realistic section: a class `TestWithSourceIndex` builds
  inputs through `ptest.source_index`, gated with
  `pytest.mark.skipif(importlib.util.find_spec("ptest.source_index") is None, reason="awaiting T1")`.
  The orchestrator confirms it runs after the merge.

### T3 Dependency store, ingest, deselect binding writer, uninstall

Owns:
- src/ptest/selection_store.py (new)
- src/ptest/selection_ingest.py (new)
- src/ptest/uninstall.py
- tests/ng/test_selection_store.py (new)
- tests/ng/test_selection_ingest.py (new)
- tests/ng/test_uninstall.py

Acceptance:
- [ ] Store basics:
  - `update` → `snapshot` round trip, with dense ids and arrays from
    `selection_ids`;
  - only the nodes that ran are replaced, and fixture replacement works;
  - a RunBaseline is stored per run, and unreferenced runs are pruned;
  - `full=True` prunes nodes absent from two full inventories;
  - `mark_outcomes`, `invalidate`, `demote`, `record_audit`,
    `note_inactive` and `meta` work.
- [ ] Concurrency: two connections (threads, or two processes through
  `multiprocessing` with a spawn context and no sleeps) update different
  runs and the same node concurrently. No error leaks (busy_timeout and
  BEGIN IMMEDIATE), no torn rows, and the last committed writer wins.
- [ ] Private files:
  - a symlinked `selection.db`, a symlinked `projects/<id>` dir, mode 0644,
    a foreign owner (monkeypatched `os.getuid`/lstat as in
    test_stack_dumps) and a hard-linked db are refused with
    `unsafe-path` and never followed;
  - the store is never created when `create=False`;
  - a hot journal recovers through `storage.open_database` (use
    `tests/ng/support.leave_hot_journal`);
  - a corrupt file gives `coordinator-corrupt`, after which `remove_store`
    plus `open_store(create=True)` rebuilds it.
- [ ] Size (A5): a synthetic 20k-node store (about 150 functions per node)
  is at most SELECTION_STORE_MAX_BYTES on disk. Eviction is tested by
  monkeypatching the target down: the oldest runs' nodes are evicted, a run
  never fails, and the cache LRU prune works.
- [ ] N7: records written for project id A are never visible through
  project id B.
- [ ] Never stored: no file contents, no source text, no unkeyed hashes.
  Scan the db bytes for a sentinel string present in the source and data
  files.
- [ ] Ingest, using hand-written deps files per section 2.6:
  - valid serial and xdist runs merge;
  - completeness is checked in every way: a missing worker, a wrong count,
    an overflow file;
  - malformed input never raises and never yields a wrong skip, and each
    case is covered: truncated JSON, wrong format, wrong run_id, pid
    mismatch, symlink, foreign owner, mode 0644, hard link, oversize,
    unsafe or hostile paths (`..`, absolute, NUL, `.venv/...`; the context
    becomes opaque), invalid outcome, wrong types;
  - qualname normalisation: `<module>` and `<lambda>` become module bodies;
  - a duplicate node keeps the worst outcome and the union of deps;
  - files of another report name are ignored, and the directory scan is
    bounded (4096 entries).
- [ ] Deselect writer: O_EXCL, refuses an existing file or symlink, mode
  0600, exact JSON, rejects unsafe ids, and node ids with brackets, `::`,
  unicode or newlines round-trip. `cleanup` removes only this report's deps
  files and binding, never through links.
- [ ] Uninstall:
  - plans REMOVE entries (internal kind "selection-store") for
    `projects/<project_id>/selection.db` (and `-journal`) of the root
    config and of each declared child config that has a valid project_id;
  - a dry run touches nothing; apply removes them with identity checks;
  - a symlinked or foreign store is SKIPPED;
  - the existing uninstall tests stay green and the JSON document shape is
    unchanged.

### T4 Bridge recorder and deselect binding in pytest_bridge

Owns:
- src/ptest/runtime/selection_recorder.py (new)
- src/ptest/runtime/pytest_bridge.py
- tests/ng/test_selection_recorder.py (new)
- tests/ng/test_selection_bridge_subprocess.py (new)
- tests/ng/fixtures/selection_project/ (new directory, files below)
- tests/ng/test_pytest_bridge_unit.py (only if a pinned hook-table
  expectation changes)

Implementation constraints (spec 6.1, N10, N13):
- The recorder uses only the stdlib, and the bridge still imports no ptest
  code. The bridge duplicates the literals it uses (deps infix and format,
  the deselect suffix and format, the env names, the tool ids and name, the
  caps, skip/output dirs, the launcher pattern). It also duplicates the
  `selection_code_path`/`selection_data_path`/`selection_normalize_qualname`
  rules as a stdlib copy. A test pins every literal equal to contracts, and
  pins the copied predicates equal on a shared case table.
- Tool id: the first id with `sys.monitoring.get_tool(i) is None` among
  (3, 4, 2), via `use_tool_id(i, "ptest-selection")`. If none is free, the
  recorder is inactive with reason `no free sys.monitoring tool id`, and
  nodes are recorded opaque with their outcomes.
- Events:
  - only `set_local_events(tool, code, PY_START)` on project code objects,
    never `set_events` (global);
  - the callback adds the code to the current context's set and returns
    DISABLE; it is wrapped so it never raises;
  - `restart_events()` runs on every context switch;
  - project code is discovered from the audit `exec` event (walking
    `co_consts` recursively) plus one `sys.modules` walk at activation;
  - the `exec` of a project module code object inside a test or fixture
    context records the module path.
- Path filter: `co_filename` is project code only when
  `realpath == abspath` (no symlink component), it lies inside the
  realpath of `PTEST_PYTEST_CHECKOUT_ROOT`, and the relative path passes the
  code-path rule. Anything else (`<string>`, `..`, NUL, outside the
  checkout) is dropped. Data paths come from the audit `open` event with
  path str, bytes or PathLike (fd ints are ignored). Pure-write opens
  (mode containing "w" or "x" without "+", or os.open flags with
  O_ACCMODE == O_WRONLY) are skipped. A data path is recorded as the
  relative path of its realpath, only when that is inside the checkout and
  passes the data-path rule.
- Spawn rules:
  - `os.system`, `os.fork` and `os.forkpty` always make the context opaque;
  - `subprocess.Popen`, `os.posix_spawn`, `os.exec` and `os.spawn` resolve
    the executable (or argv[0]) via PATH and then realpath. The context
    becomes opaque if the result is inside the checkout, inside
    `realpath(sys.prefix)`, equal to `realpath(sys.executable)`, or its
    basename fullmatches `C.SELECTION_LAUNCHER_PATTERN`.
- Caps: a context's functions above SELECTION_CONTEXT_MAX_FUNCTIONS or data
  above SELECTION_CONTEXT_MAX_DATA makes it opaque. The file byte cap gives
  an overflow file (section 2.6).
- Tamper: at each context switch, if `get_tool(tool) != "ptest-selection"`,
  then `tamper=true`, this context and all later ones are opaque, and
  recording stops.
- Contexts:
  - a test context covers setup, call and teardown in the
    `pytest_runtest_protocol` wrapper;
  - `pytest_fixture_setup` is a new wrapper (add
    `("pytest_fixture_setup", {"wrapper": True, "tryfirst": True})` to
    `_BRIDGE_HOOK_MARKS` plus a module-level worker delegate) that switches
    to the fixture context `(baseid, argname, scope)` for non-function
    scopes and restores the previous context afterwards;
  - fixture keys used by a test are its closure's applicable defs for
    `item.fixturenames` (scope != function), plus best-effort
    `item._request._fixture_defs`;
  - everything else is ambient;
  - outcomes come from the process's own runtest reports for items that
    entered a test context there. The xdist controller ignores forwarded
    reports.
- Activation (D9, D14):
  - the serial or xdist controller activates just before `pytest.main`,
    only when `__name__ == "__main__"`, `PTEST_SELECTION_RECORD == "1"` and
    a report binding exists;
  - an xdist worker activates at bridge-module import when
    `__name__ == "pytest_bridge"`, `PYTEST_XDIST_WORKER` matches `gw\d+`,
    the record env is "1" and `_worker_report_path()` is valid;
  - the audit hook is a permanent no-op when not recording.
- Deselect: in OwnedPlugin `pytest_collection_modifyitems` after the yield,
  scoped execution only, with the section 2.6 binding checks. Removed items
  go to `config.hook.pytest_deselected(items=...)`. Serial and xdist
  workers deselect identically.
- No behaviour change: argv, outcomes, exit codes, the stall marker and
  stack dumps, coverage and faulthandler stay as they are. Any recorder
  exception leaves the run unrecorded (a `recording: false` file or no
  file), never failed.

Fixture project `tests/ng/fixtures/selection_project/`. Every Python file
carries a `.py.txt` suffix, like the existing tests/ng/fixtures/pytest
projects. pyproject `testpaths = ["tests/ng"]` recurses into fixtures, so a
real `.py` or `conftest.py` there would be collected by ptest's own suite.
The files are:
- `pkg/__init__.py.txt`;
- `pkg/core.py.txt` (functions and a constant);
- `pkg/lazy.py.txt` (only imported inside a test);
- `data/config.json`;
- `tests/conftest.py.txt` (a session fixture calling `pkg.core.boot()`);
- `tests/test_core.py.txt`: one test each that
  - calls a function;
  - lazily imports `pkg.lazy`;
  - reads `data/config.json`;
  - runs `sys.executable -c pass` (opaque);
  - runs the `true` binary found via `shutil.which` (not opaque);
  - fails, errors in setup, skips and xfails;
  - is parametrized with ids containing brackets and unicode.

The tests copy it to tmp_path, stripping the `.txt` suffix, before
running. It is never run in place.

Acceptance:
- [ ] Unit (`test_selection_recorder.py`):
  - path filters and hostile `co_filename` cases;
  - the spawn classification table;
  - context switching and restore;
  - outcome worst-phase order;
  - caps leading to opaque and overflow;
  - tool ids taken giving the inactive reason;
  - tamper giving opaque;
  - `get_events(tool) == 0` after activation, and `get_local_events` is 0
    on a stdlib code object (N13);
  - the audit hook never raises on garbage args (N10);
  - the deps file is O_EXCL/O_NOFOLLOW and 0600, and a pre-created symlink
    is not written through;
  - JSON matches section 2.6 exactly;
  - constants and predicates are pinned to contracts.

  Tests that call `use_tool_id` must free it in a finally and must not
  assume id 3 is free. Prefer running such cases in a child interpreter.
- [ ] Real subprocess (`test_selection_bridge_subprocess.py`, launched like
  `test_stall_bridge.py` with a private report dir and binding env), serial
  and `-n 2`:
  - F/M/X/D/opaque and outcomes are recorded as expected for the fixture
    project;
  - the deselect binding deselects exactly the listed ids (including ids
    with brackets, unicode and a newline) and reports them as deselected;
  - invalid binding variants are ignored and every test runs;
  - exit codes and the pytest summary counts are identical with and without
    recording;
  - `--cov` totals are identical with and without recording (N6);
  - a session-fixture call is attributed to the fixture key, not to the
    first test.
- [ ] Cross-check gated on `importlib.util.find_spec("ptest.selection_ingest")`:
  `selection_ingest.read_run` accepts the real serial and xdist output as
  `complete`. The orchestrator confirms it runs (not skipped) after the
  merge.

### T5 Integration

Owns:
- src/ptest/contracts.py (barrier owner; tests only)
- src/ptest/config.py
- src/ptest/history.py (expected unchanged)
- src/ptest/cli.py
- src/ptest/operations.py
- src/ptest/adapters/pytest.py (expected unchanged)
- src/ptest/progress.py
- src/ptest/selection_engine.py (new)
- tests/ng/test_config.py
- tests/ng/test_contracts.py
- tests/ng/test_selection_engine.py (new)
- tests/ng/test_selection_history_compat.py (new)
- tests/ng/test_selection_e2e.py (new)
- tests/ng/test_changed_default.py
- tests/ng/test_operations.py
- tests/ng/test_cli.py

Acceptance:
- [ ] Config:
  - `[selection] dynamic` accepts a bool only (anything else is
    `invalid-config`, exit 2);
  - it defaults to true;
  - it is rendered as `dynamic = false` after `full_ratio` only when false;
  - the policy digest is unchanged for both values (assert the 43bc9848…
    prefix for the default policy).
- [ ] Contracts tests: barrier constants and helpers; `RunRequest.deselect`
  validation (unsafe ids, non-scoped mode, over-limit counts);
  `SelectionPolicy` repr identical with dynamic false.
- [ ] selection_engine (unit, with fakes injected through the lazy
  accessors):
  - every static reason in 2.8;
  - mapping from decision to Impact, including D8;
  - an exception anywhere gives `dynamic planner failed` and the static
    verdict;
  - the 2.7 index rule: a `dynamic_ok` verdict carrying a complete
    `project_index` never calls `_source_index().build_project_index`; a
    `dynamic_ok` verdict with `project_index=None` calls it exactly once
    with the planning key and cache and reaches `engine == "dynamic"` (one
    case each for kind "selected", "none" with relevant, and "full" with
    "is outside the import graph"); an index with `complete` False, or a
    raising build, gives `dynamic planner failed`;
  - the compatibility function is stable and order-insensitive, and
    changes when a lockfile byte changes;
  - `preview` writes no records and prints nothing;
  - `after_run` covers the full D10 matrix: valid, cancelled,
    changed-during-run, report-invalid, guard problem, setup failure,
    incomplete deps (mark_outcomes and invalidate only), and recording
    disabled (cleanup only);
  - the self-audit order is snapshot, then plan, then demote, then update;
  - nothing ever raises out of `after_run`.
- [ ] CLI (test_changed_default and test_cli, through the existing stub
  seams plus a stubbed `cli._selection_api`):
  - the notes in 2.8 are byte-exact;
  - a dynamic selection maps to a SCOPED request with `deselect`, and a
    full conversion maps to FULL;
  - a folder run keeps only `deselect` entries under the folder files and
    never deselects inside explicitly named files;
  - the monorepo child path uses the child's project id;
  - command-kind and stubbed-plan notes are byte-identical to 0.4;
  - `-v` lines are escaped (ANSI or OSC in a path comes out escaped);
  - the status line appears only when a store exists, and status never
    creates state (the existing `_tree_bytes` assertion still holds).
- [ ] Operations:
  - the record env appears exactly per D9;
  - the deselect binding is written only for scoped requests with
    `deselect`, and a write failure still runs (no env, every test runs);
  - `selection_ingest.cleanup` runs in `finally` on every outcome;
  - `after_run` is called with the ingestible flag per D10;
  - a basic pytest scoped pass with deselect publishes no baseline,
    verified record or full proof (N5).
- [ ] N9 (`test_selection_history_compat.py`, not gated):
  - frozen literals: the 0.4.10 `REASON_CODES`, the 0.4.10
    `serialize_run_result` key sets (top level and nested), copied from
    `git show f478189:src/ptest/contracts.py`;
  - `C.REASON_CODES` equals the 0.4.10 set and `_PERSISTED_REASON_ALIASES`
    is unchanged;
  - summaries persisted through `history.publish_outcome` for a dynamic
    scoped result, a full result with audit misses, and a static fallback
    result decode with only frozen keys and codes.
- [ ] E2E (`test_selection_e2e.py`): in-process CLI on an inline tmp git
  project, with a fixture domain and real pytest through the bridge, using
  the test_pytest_scoped_subprocess pattern. Cases:
  1. edit one function body → only its tests run (node-level, with
     deselect);
  2. edit a module constant → the tests of referencing functions run;
  3. an effectful module change → static reach;
  4. an empty store → static note `no dependency records yet — any run records them`;
  5. a second git worktree of the same project on identical code → `no tests affected: N tests reach…`;
  6. a failing test always reruns;
  7. a self-audit miss (a test reading a value through `getattr` by
     string, R2) → the audit line, then a demotion, then the test is
     selected by static reach;
  8. edit only one test function's body in a test file → the note contains
     `(dynamic · ` and only that test runs (the rest of the file is
     deselected);
  9. edit a data file read by exactly one recorded test → the note
     contains `(dynamic · ` and only that test runs (0.4 would run the full
     suite: "is outside the import graph");
  10. edit a function in a test-support module (for example
      `tests/helpers.py`) called by exactly one recorded test → the note
      contains `(dynamic · ` and only that test runs (0.4: "is test
      support" → full).

  The module docstring documents the integration boundary. The module has
  `pytestmark = pytest.mark.skipif(not _ready(), reason="selection e2e: awaiting T1-T4 integration")`,
  where `_ready()` imports `ptest.source_index`, `ptest.selection_planner`,
  `ptest.selection_store`, `ptest.selection_ingest` and
  `ptest.runtime.selection_recorder` and checks `hasattr(impact, "PLANNING")`.
  The orchestrator MUST confirm 0 skipped after the merge.

### T6 Nested monorepo scopes, evaluation harness, docs and spec copy

Owns:
- src/ptest/monorepo.py
- tests/ng/test_monorepo_scopes.py
- scripts/selection_eval.py (new)
- tests/ng/test_selection_eval.py (new)
- docs/specs/2026-10-07-ptest-0.5-selection.md (new in git; byte copy)
- docs/ptest-agent.md
- src/ptest/resources/agent-guide.md
- src/ptest/resources/repository-agent-guide.md
- README.md
- docs/changelog.md
- src/ptest/agent_rules.py
- tests/ng/test_agent_rules.py
- tests/ng/test_resources.py
- tests/ng/fixtures/previous-guides/f478189-ptest-agent.md (new)

Acceptance:
- [ ] Monorepo:
  - `route_scopes` and `split_scopes` match the longest declared child by
    path segments (a shared private helper);
  - `services/control-plane`, `services/control-plane/tests` and
    `services/control-plane/tests/test_x.py::t` route to the nested child;
  - `services/api-v2/tests` routes to `services/api-v2`, never to
    `services/api`;
  - siblings stay separate;
  - error messages for unknown or cross-child scopes are unchanged;
  - all existing test_monorepo_scopes tests stay green.
- [ ] Harness (`scripts/selection_eval.py`, stdlib plus ptest imports at
  call time only). CLI:
  `--project PATH --mutants N --seed S --out DIR [--classes ...] [--dry-run] [--benchmark-overhead] [--ptest CMD]`.
  - Mutation classes per 6.7.
  - Each mutant is applied in a scratch `git worktree` under
    `--out/scratch`, which is always removed.
  - v1 is `impact.plan(..., conftest_edges=False)` and v2 is
    `selection_engine.preview(...)`.
  - Ground truth runs `uv run ptest <union files>` (or the test roots when
    v1 is full and affordable) with `--result-json`. Failing node ids come
    from `selection_store.open_store(...).snapshot()` nodes with that
    run_id.
  - `projects/<id>/selection.db` is copied before and restored after every
    ground-truth run, so mutants never pollute the store.
  - A per-file-set baseline run marks pre-existing or flaky failures.
  - A miss is a test failing under the mutant, passing in the baseline and
    not selected by v2. Misses are classified R1-R9 or "unclassified".
  - It writes `selection-eval.json` and `selection-eval.md`, deterministic
    given `--seed`.
  - The overhead benchmark runs `ptest --full --again` with dynamic false
    and true, back to back, three times, and reports the median.
  - Unit tests cover only pure logic:
    - site sampling determinism and class coverage;
    - each mutation operator's text transform (the result parses, and
      exactly one site changes);
    - the union/full choice;
    - miss classification;
    - baseline flake marking;
    - report rendering;
    - argument parsing;
    - `--dry-run` listing mutants without touching git or running anything
      (run with monkeypatched subprocess).

  The campaign itself is never run in the task.
- [ ] Docs:
  - `docs/ptest-agent.md` and `repository-agent-guide.md` stay
    byte-identical and at most 100 lines (reflow prose; never relax the
    limit test). They contain:
    - the dynamic start-line row;
    - the static-fallback row (it must still contain `→ N of M test files`
      and `via importers`);
    - the "no tests affected" row with the reach clause;
    - the `selection audit:` row;
    - a statement that a changed green, dynamic or static, is iteration
      only.

    Every phrase test_resources asserts stays, and the text never contains
    "xdist", "fingerprint" or "expected:".
  - agent-guide.md gains one short paragraph with the same meaning.
  - README: a short "Dependency-recorded selection" section, the
    `[selection] dynamic = false` key, and output-table rows using the 2.8
    strings.
  - changelog: a new top `## Unreleased` section (G1-G7, the new key, the
    audit line, the nested-child fix, N9 compatibility). No version bump.
- [ ] agent_rules: `_PREVIOUS_GUIDE_SHA256S` gains
  `fa30a22ce8eb88a1687437fe8f8578c16e81b46ce54af2a955db6b565b8fc062` with
  the comment `# 4fb5935 (0.4.10): guide before dependency-recorded selection rows.`
  The fixture `tests/ng/fixtures/previous-guides/f478189-ptest-agent.md` is
  a byte copy of the base guide. A new test proves the 0.4.10 guide
  upgrades in place, and that an edited copy raises already-exists.
  `test_every_shipped_guide_version_hashes_into_previous_set` stays green.
- [ ] The spec copy is byte-identical to
  /home/ingmar/worktrees/ptest/specs/2026-10-07-ptest-0.5-selection.md
  (the untracked file already in the chain worktree; commit it).

## 6. Ownership matrix (pairwise disjoint)

- **T1:** src/ptest/source_index.py, src/ptest/impact.py,
  tests/ng/test_source_index.py, tests/ng/test_impact.py
- **T2:** src/ptest/selection_planner.py, tests/ng/test_selection_planner.py
- **T3:** src/ptest/selection_store.py, src/ptest/selection_ingest.py,
  src/ptest/uninstall.py, tests/ng/test_selection_store.py,
  tests/ng/test_selection_ingest.py, tests/ng/test_uninstall.py
- **T4:** src/ptest/runtime/selection_recorder.py,
  src/ptest/runtime/pytest_bridge.py, tests/ng/test_selection_recorder.py,
  tests/ng/test_selection_bridge_subprocess.py,
  tests/ng/test_pytest_bridge_unit.py, tests/ng/fixtures/selection_project/
  (pkg/__init__.py.txt, pkg/core.py.txt, pkg/lazy.py.txt,
  data/config.json, tests/conftest.py.txt, tests/test_core.py.txt)
- **T5:** src/ptest/contracts.py, src/ptest/config.py, src/ptest/history.py,
  src/ptest/cli.py, src/ptest/operations.py, src/ptest/adapters/pytest.py,
  src/ptest/progress.py, src/ptest/selection_engine.py,
  tests/ng/test_config.py, tests/ng/test_contracts.py,
  tests/ng/test_selection_engine.py,
  tests/ng/test_selection_history_compat.py, tests/ng/test_selection_e2e.py,
  tests/ng/test_changed_default.py, tests/ng/test_operations.py,
  tests/ng/test_cli.py
- **T6:** src/ptest/monorepo.py, tests/ng/test_monorepo_scopes.py,
  scripts/selection_eval.py, tests/ng/test_selection_eval.py,
  docs/specs/2026-10-07-ptest-0.5-selection.md, docs/ptest-agent.md,
  src/ptest/resources/agent-guide.md,
  src/ptest/resources/repository-agent-guide.md, README.md,
  docs/changelog.md, src/ptest/agent_rules.py, tests/ng/test_agent_rules.py,
  tests/ng/test_resources.py,
  tests/ng/fixtures/previous-guides/f478189-ptest-agent.md

graphify-out/ updates from `graphify update .` are not task-owned. Leave
them unstaged.

## 7. Parallelism, barriers and the orchestrator checklist

Every cross-task dependency goes through the contracts barrier (types and
helpers) or through a frozen signature above.
- T2 never imports T1 or T3.
- T3 never imports T1, T2 or T4.
- T4 imports no ptest code.
- T5's engine reaches T1-T3 only through lazy accessors and is unit-tested
  with fakes.
- T6's harness imports ptest modules only at call time.

Only these gated test modules need the merged branch:
- T5 test_selection_e2e.py;
- the T4 ingest cross-check;
- the optional T2 `TestWithSourceIndex` class.

After merging T1-T6, the orchestrator:
1. Applies nothing new; the barrier is already on the base.
2. Runs the gated modules once
   (`ptest tests/ng/test_selection_e2e.py tests/ng/test_selection_bridge_subprocess.py tests/ng/test_selection_planner.py`)
   and confirms 0 skipped.
3. Runs `ptest --full` once at Verify (A6).
4. Then runs the A1-A5 campaign with `scripts/selection_eval.py` outside
   the tasks.

## 8. Abuse case → owner and test

| Abuse case | Owner / test |
|---|---|
| store, deps file or binding pre-created as a symlink or foreign-owned | T3 (store and ingest refuse; writer O_EXCL\|O_NOFOLLOW), T4 (deps file not written through; binding ignored), T5 (static fallback reason) |
| different project id | T3 N7 test; T5 per-child project id |
| two worktrees record concurrently | T3 concurrency test |
| gate worktree on identical code | T2 (no stale paths → reached only), T5 e2e case 5 |
| hostile co_filename | T4 unit; T3 ingest (unsafe path → opaque) |
| huge dependency output | T4 caps and overflow; T3 oversize file → incomplete |
| malformed deps file or store row | T3 ingest/store; T5 engine fallback |
| node ids with brackets, `::`, unicode or newlines | T3 writer, T4 bridge deselect, contracts `selection_nodeid_safe` (T5 tests) |
| test code tampers with the recorder | T4 tamper → opaque (R5 otherwise) |
| file changes mid-run | T5 `after_run` matrix (no update; mark/invalidate only) |
| interrupted, cancelled or stalled run | T5 `after_run` matrix |
| corrupt store or hot journal | T3 |
| xdist worker crash | T3 completeness; T5 invalidate |
| `-v` prints names and paths | T5 escape test; JSON unchanged |
| `.env` or secrets opened | T4 (dot paths never recorded); T3 sentinel scan (no content stored) |

## 9. Negative contracts → owner

| Contract | Owner |
|---|---|
| N1 | T2 (d/e/f/fixtures/baselines), T5 e2e 1-2 |
| N2 | T2 4.6 |
| N3 | T2 b |
| N4 | T2 a, T5 D10 mark_outcomes, e2e 6 |
| N5 | T5 operations test, D8 |
| N6 | T4 subprocess A/B, T5 never-raise |
| N7 | T3 |
| N8 | T5 `after_run` |
| N9 | T5 compat test, D12 |
| N10 | T4 |
| N11 | T1 |
| N12 | T3 eviction |
| N13 | T4 local-events tests |

## 10. Risks

| Risk | Impact | Mitigation |
|---|---|---|
| E2E needs T1-T5 merged | It cannot pass inside T5 | readiness gate; the orchestrator checks 0 skipped |
| Barrier not applied before branching | every task is red | section 0; tasks report BLOCKED and never patch contracts |
| Per-run baselines grow | plan latency and store size | signature memoisation and cap; SELECTION_MAX_RUNS; eviction |
| Worker activation happens after early conftest code | missed ambient functions | activate at `-p pytest_bridge` import (before initial conftests); serial activates before pytest.main |
| Cold first index of a large repo at ingest | slow first full run | cached afterwards; harness A3 measures |
| Static start-line text change breaks docs or tests | doc or test drift | 0.4 text kept except for pytest static fallback; strings frozen in 2.8 |
| Recorder overhead trips 1 s pytest-timeout | flaky suites | local events only; per-test p95 in the A4 benchmark |

## 11. Out of scope

Vitest selection; the selection.py advanced, groups and shadow paths;
running the A1-A5 campaign; version bump, tag, push and release; replacing
the installed CLI; doctor upgrade notes.

## 12. Shared-file content (exact; src/ptest/contracts.py)

This is reproduced verbatim from the architect output field
`sharedFileContent`.

```text
FILE: src/ptest/contracts.py — apply these six anchored edits verbatim. Each OLD block occurs exactly once at base f478189. Replace OLD with NEW byte-for-byte. No other contracts.py change. The NEW of EDIT F is OLD followed by the selection block.

===== EDIT A — imports
----- OLD (exact):
import hashlib
import json
import math
import re
import struct
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
----- NEW (exact):
import hashlib
import hmac
import json
import math
import re
import struct
from array import array
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Protocol
===== END EDIT

===== EDIT B — SelectionPolicy.dynamic field
----- OLD (exact):
    full_ratio: float = 0.70
    groups: tuple = ()
----- NEW (exact):
    full_ratio: float = 0.70
    groups: tuple = ()
    # repr=False keeps repr(SelectionPolicy), and so every policy digest
    # derived from it, byte-identical to 0.4.10 for any value of the key.
    dynamic: bool = field(default=True, repr=False)
===== END EDIT

===== EDIT C — SelectionPolicy.dynamic check
----- OLD (exact):
        object.__setattr__(self, "full_ratio", _check_float("selection.full_ratio", self.full_ratio, lo=0.1, hi=1.0))
----- NEW (exact):
        object.__setattr__(self, "full_ratio", _check_float("selection.full_ratio", self.full_ratio, lo=0.1, hi=1.0))
        object.__setattr__(self, "dynamic", _check_bool("selection.dynamic", self.dynamic))
===== END EDIT

===== EDIT D — RunRequest.deselect field
----- OLD (exact):
    changed_note: str | None = None
    next_hint: bool = False
----- NEW (exact):
    changed_note: str | None = None
    next_hint: bool = False
    # Recorded, unselected node ids inside the scoped argv files; the bridge
    # deselects exactly these (unknown ids always run). Never persisted.
    deselect: tuple = ()
===== END EDIT

===== EDIT E — RunRequest.deselect check
----- OLD (exact):
        object.__setattr__(self, "next_hint",
                           _check_bool("request.next_hint", self.next_hint))
----- NEW (exact):
        object.__setattr__(self, "next_hint",
                           _check_bool("request.next_hint", self.next_hint))
        object.__setattr__(self, "deselect",
                           _check_deselect("request.deselect", self.deselect))
        if self.deselect and self.mode is not Mode.SCOPED:
            raise ValueError("request.deselect requires scoped mode")
===== END EDIT

===== EDIT F — selection contract block (insert after stack_dump_pid)
----- OLD (exact):
            or not digits.isdigit() or digits[0] == "0"):
        return None
    return int(digits)
----- NEW (exact):
            or not digits.isdigit() or digits[0] == "0"):
        return None
    return int(digits)



# ---------------------------------------------------------------------------
# Dependency-recorded test selection (ptest 0.5).
#
# One frozen contract shared by impact/source_index (T1), selection_planner
# (T2), selection_store/selection_ingest (T3), the bridge recorder (T4) and
# the integration (T5). The bridge imports no ptest code: it duplicates the
# literal values it needs and its tests pin them equal to these. Nothing here
# reaches RunResult, history or any public JSON (N9): the dependency store is
# a separate SQLite file that older ptest never opens.

SELECTION_PROTOCOL = "ptest-selection-v1"
SOURCE_INDEX_VERSION = 1
SELECTION_STORE_DIR = "projects"
SELECTION_STORE_NAME = "selection.db"
SELECTION_STORE_MAX_BYTES = 64 * 1024 * 1024
SELECTION_STORE_TARGET_BYTES = 48 * 1024 * 1024
SELECTION_MAX_RUNS = 256
SELECTION_MAX_SIGNATURES = 128
SELECTION_RECORD_ENV = "PTEST_SELECTION_RECORD"
SELECTION_DESELECT_ENV = "PTEST_SELECTION_DESELECT"
SELECTION_DEPS_INFIX = ".deps-"
SELECTION_DEPS_FORMAT = "ptest-selection-deps-v1"
SELECTION_DEPS_MAX_BYTES = 64 * 1024 * 1024
SELECTION_DESELECT_SUFFIX = ".deselect"
SELECTION_DESELECT_FORMAT = "ptest-selection-deselect-v1"
SELECTION_DESELECT_MAX_BYTES = 16 * 1024 * 1024
SELECTION_DESELECT_MAX_IDS = 200000
SELECTION_NODEID_MAX_BYTES = 4096
SELECTION_PATH_MAX_BYTES = 4096
SELECTION_CONTEXT_MAX_FUNCTIONS = 100000
SELECTION_CONTEXT_MAX_DATA = 4096
SELECTION_DATA_MAX_BYTES = 16 * 1024 * 1024
SELECTION_TOOL_IDS = (3, 4, 2)
SELECTION_TOOL_NAME = "ptest-selection"
SELECTION_ABSENT_DIGEST = ""
SELECTION_UNREADABLE_DIGEST = "?"
SELECTION_OUTCOMES = frozenset({
    "passed", "failed", "error", "skipped", "xfailed", "xpassed", "unknown",
})
SELECTION_PASSING_OUTCOMES = frozenset({"passed", "skipped", "xfailed"})
SELECTION_FIXTURE_SCOPES = frozenset({"class", "module", "package", "session"})
SELECTION_STATEMENT_KINDS = frozenset({"def", "class", "import", "assign", "doc", "effect"})
SELECTION_DEFINITION_KINDS = frozenset({"def", "class", "import", "assign"})
SELECTION_SKIP_DIRS = frozenset({
    "node_modules", "__pycache__", "site-packages", "build", "dist", "venv",
    ".tox", "htmlcov",
})
SELECTION_OUTPUT_DIRS = frozenset({
    "build", "dist", "node_modules", ".venv", "venv", "__pycache__",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", "htmlcov",
    ".hypothesis",
})
# Spawned executables that can run project Python (basename, fullmatch).
SELECTION_LAUNCHER_PATTERN = (
    r"(?:python|pypy)[0-9.]*(?:\.exe)?|uv|uvx|pytest|py\.test|ptest"
    r"|sh|bash|dash|zsh|fish|env|nohup|timeout|xargs"
)


def _selection_relpath_ok(path: object) -> bool:
    if not isinstance(path, str) or not path or "\x00" in path or "\\" in path:
        return False
    if path.startswith("/") or re.match(r"^[A-Za-z]:", path):
        return False
    try:
        if len(path.encode("utf-8")) > SELECTION_PATH_MAX_BYTES:
            return False
    except UnicodeEncodeError:
        return False
    return all(part not in ("", ".", "..") for part in path.split("/"))


def selection_relpath_safe(path: object) -> bool:
    """A checkout-relative POSIX path that can never escape the checkout."""
    return _selection_relpath_ok(path)


def selection_code_path(path: object) -> bool:
    """A project ``.py`` path the source index walks (impact's skip rules)."""
    if not _selection_relpath_ok(path) or not path.endswith(".py"):
        return False
    parts = path.split("/")
    return not any(part.startswith(".") or part in SELECTION_SKIP_DIRS
                   or part.endswith(".egg-info") for part in parts[:-1]) \
        and not parts[-1].startswith(".")


def selection_data_path(path: object) -> bool:
    """A project data path a test may depend on (never code or tool output)."""
    if not _selection_relpath_ok(path):
        return False
    lowered = path.lower()
    if lowered.endswith((".py", ".pyc", ".pyo")):
        return False
    return not any(part.startswith(".") or part in SELECTION_SKIP_DIRS
                   or part in SELECTION_OUTPUT_DIRS or part.endswith(".egg-info")
                   for part in path.split("/"))


def selection_nodeid_safe(nodeid: object) -> bool:
    """A node id that may be bound for exact-match deselection."""
    if not isinstance(nodeid, str) or "::" not in nodeid or "\x00" in nodeid:
        return False
    try:
        if len(nodeid.encode("utf-8")) > SELECTION_NODEID_MAX_BYTES:
            return False
    except UnicodeEncodeError:
        return False
    return _selection_relpath_ok(nodeid.split("::", 1)[0])


def selection_test_file(nodeid: str) -> str:
    """The test file part of a node id (text before the first ``::``)."""
    return nodeid.split("::", 1)[0]


def selection_normalize_qualname(qualname: object) -> str | None:
    """Map a raw ``co_qualname`` to its indexed scope; None is module-level code.

    ``f.<locals>.g`` -> ``f``; ``C.m`` -> ``C.m``; ``C.<lambda>`` -> ``C``;
    ``<module>``, ``<genexpr>``, ``<lambda>`` at module level and any invalid
    value -> None.
    """
    if (not isinstance(qualname, str) or not qualname or "\x00" in qualname
            or len(qualname) > 1024):
        return None
    kept: list[str] = []
    for part in qualname.split("."):
        if not part or part.startswith("<"):
            break
        kept.append(part)
    return ".".join(kept) or None


def selection_name_key(path: str, name: str) -> str:
    """Canonical key of one module-level name: ``<path>:<name>``."""
    return f"{path}:{name}"


def selection_key_path(key: str) -> str:
    """The path part of a name key (names never contain ``:``)."""
    return key.rsplit(":", 1)[0]


def selection_file_digest(key: bytes, raw: bytes) -> str:
    """Keyed content digest; byte-identical to ``source.snapshot`` file digests."""
    return hmac.new(key, raw, hashlib.sha256).hexdigest()


def selection_ids(values: Iterable[int]) -> array:
    """The one id-set representation: sorted, unique, ``array('I')``."""
    return array("I", sorted(set(values)))


def selection_static_reach(reverse: Mapping[str, frozenset[str]],
                           seeds: Iterable[str]) -> frozenset[str]:
    """Seeds plus every path that transitively imports one (reverse edges)."""
    queue = [seed for seed in seeds if isinstance(seed, str)]
    seen = set(queue)
    while queue:
        current = queue.pop()
        for importer in reverse.get(current, ()):
            if importer not in seen:
                seen.add(importer)
                queue.append(importer)
    return frozenset(seen)


def selection_store_path(domain_root: Path, project_id: str) -> Path:
    """``<state>/projects/<project_id>/selection.db`` (never created here)."""
    if not isinstance(project_id, str) or not re.fullmatch(r"[0-9a-f]{32}", project_id):
        raise ValueError("selection store needs a 32-hex project id")
    return Path(domain_root) / SELECTION_STORE_DIR / project_id / SELECTION_STORE_NAME


def selection_deps_path(report_path: Path, pid: int) -> Path:
    """One bridge process's dependency file: ``<report>.deps-<pid>``."""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise ValueError("dependency file pid must be a positive int")
    return Path(f"{report_path}{SELECTION_DEPS_INFIX}{pid}")


def selection_deps_pid(report_name: str, name: str) -> int | None:
    """The PID in ``name`` when it names a dependency file of ``report_name``."""
    prefix = report_name + SELECTION_DEPS_INFIX
    if not name.startswith(prefix):
        return None
    digits = name[len(prefix):]
    if (not 1 <= len(digits) <= 10 or not digits.isascii()
            or not digits.isdigit() or digits[0] == "0"):
        return None
    return int(digits)


def selection_deselect_path(report_path: Path) -> Path:
    """The private deselect binding of one attempt: ``<report>.deselect``."""
    return Path(f"{report_path}{SELECTION_DESELECT_SUFFIX}")


def _check_deselect(name: str, value: object) -> tuple:
    items = _check_tuple(name, value)
    if len(items) > SELECTION_DESELECT_MAX_IDS:
        raise ValueError(f"{name} exceeds {SELECTION_DESELECT_MAX_IDS} node ids")
    for item in items:
        if not isinstance(item, str):
            raise TypeError(f"{name}[] must be str, got {type(item).__name__}")
        if not selection_nodeid_safe(item):
            raise ValueError(f"{name}[] is not a safe node id")
    return items


# -- source index (T1 builds; T2 diffs; T3 caches the encoded bytes) --------

@dataclass(frozen=True, slots=True)
class ImportIndex:
    """One import of a file, unresolved (content-only, path independent)."""

    level: int                 # 0 absolute; n leading dots
    module: str                # dotted text after the dots ("" for ``from . import x``)
    name: str | None           # None for ``import a.b``; the name for ``from``; "*" for star
    local: str | None          # bound module-level name; None when not at module top level
    aliased: bool              # ``import a.b as c`` binds a.b (else ``import a.b`` binds a)
    statement: int             # index into FileIndex.statements; -1 when nested


@dataclass(frozen=True, slots=True)
class ScopeIndex:
    """One function or method scope (nested code folded into it)."""

    qualname: str              # selection_normalize_qualname form, never "<locals>"
    body: str                  # sha256 hex of the AST-normalised body
    skeleton: str              # sha256 hex: decorators, signature, defaults, returns, type params, async
    refs: tuple[tuple[str, ...], ...]   # raw dotted Load chains in the body, sorted, unique


@dataclass(frozen=True, slots=True)
class ClassIndex:
    """One class (nested classes are their own entries)."""

    qualname: str
    skeleton: str              # bases, keywords, decorators, type params
    body: str                  # non-function class-body statements in order
    refs: tuple[tuple[str, ...], ...]   # raw chains in the skeleton and non-function body


@dataclass(frozen=True, slots=True)
class StatementIndex:
    """One top-level statement of a module."""

    kind: str                  # one of SELECTION_STATEMENT_KINDS
    bound: tuple[str, ...]     # module-level names bound, sorted; ("*",) for a star import
    refs: tuple[tuple[str, ...], ...]   # raw chains: def/class skeleton only; import none; else whole statement
    fingerprint: str           # def: function skeleton; class: class skeleton; else whole statement


@dataclass(frozen=True, slots=True)
class FileIndex:
    """Everything the planner needs from one file content (cache value)."""

    parsed: bool               # False: unreadable, undecodable, too large or a syntax error
    imports: tuple[ImportIndex, ...]
    scopes: tuple[ScopeIndex, ...]          # sorted by qualname, unique (duplicates merged)
    classes: tuple[ClassIndex, ...]         # sorted by qualname, unique
    statements: tuple[StatementIndex, ...]  # module order


@dataclass(frozen=True, slots=True, eq=False)
class ProjectFile:
    """One indexed file with plan-time resolution (path dependent)."""

    path: str
    digest: str                # selection_file_digest of the bytes; "" when no key
    index: FileIndex
    modules: tuple[str, ...]   # dotted module names (impact._module_names)
    test: bool                 # a test file under a test root
    support: bool              # a non-test file under a test root
    imports: frozenset[str]    # project paths imported anywhere in the file (static edges)
    scope_refs: Mapping[str, frozenset[str]]     # scope qualname -> resolved name keys
    class_refs: Mapping[str, frozenset[str]]     # class qualname -> resolved name keys
    statement_refs: tuple[frozenset[str], ...]   # aligned with index.statements
    statement_bound: tuple[frozenset[str], ...]  # aligned; star imports expanded


@dataclass(frozen=True, slots=True, eq=False)
class ProjectIndex:
    """The current source index of one project (built once per plan)."""

    root: Path
    files: Mapping[str, ProjectFile]
    test_files: frozenset[str]
    reverse: Mapping[str, frozenset[str]]   # path -> importers (+ conftest.py -> tests under its dir)
    unparsed: frozenset[str]
    complete: bool             # False when the walk exceeded impact.MAX_SCAN_FILES


class ParseCache(Protocol):
    """Digest-keyed encoded FileIndex blobs (the store implements it)."""

    def get_many(self, digests: Iterable[str]) -> Mapping[str, bytes]: ...

    def put_many(self, blobs: Mapping[str, bytes]) -> None: ...


@dataclass(frozen=True, slots=True, eq=False)
class PlanningContext:
    """Ambient planning inputs for ``impact.plan`` (set through impact.PLANNING)."""

    key: bytes | None
    cache: ParseCache | None


# -- dependency records (T4 writes files; T3 reads/stores; T2 decides) ------

@dataclass(frozen=True, slots=True, eq=False)
class DepVocabulary:
    """Interned ids. Every id below indexes one of these tuples."""

    paths: tuple[str, ...]                        # path id -> checkout-relative path
    functions: tuple[tuple[int, str], ...]        # function id -> (path id, normalised qualname)
    fixtures: tuple[tuple[str, str, str], ...]    # fixture id -> (baseid, argname, scope)


@dataclass(frozen=True, slots=True, eq=False)
class ContextDeps:
    """What one test, fixture or ambient context executed (selection_ids arrays)."""

    functions: array           # function ids
    modules: array             # path ids whose module body ran in this context
    data: array                # path ids of data files opened
    opaque: bool


def selection_empty_context(*, opaque: bool = False) -> ContextDeps:
    return ContextDeps(functions=array("I"), modules=array("I"),
                       data=array("I"), opaque=opaque)


@dataclass(frozen=True, slots=True, eq=False)
class RunBaseline:
    """The code a run executed against, and its ambient (collection-time) deps."""

    run_id: str
    recorded_at: float
    compatibility: str
    digests: Mapping[int, str]  # path id -> keyed digest for every .py and recorded data path
    ambient: ContextDeps


@dataclass(frozen=True, slots=True, eq=False)
class NodeRecord:
    nodeid: str
    test_file: str
    outcome: str               # one of SELECTION_OUTCOMES
    run_id: str
    deps: ContextDeps
    fixtures: array            # fixture ids (higher-scope fixtures the test used)


@dataclass(frozen=True, slots=True, eq=False)
class FixtureRecord:
    fixture: int
    run_id: str
    deps: ContextDeps


@dataclass(frozen=True, slots=True, eq=False)
class DependencySnapshot:
    """The whole store as the planner sees it."""

    vocabulary: DepVocabulary
    runs: Mapping[str, RunBaseline]
    nodes: Mapping[str, NodeRecord]
    fixtures: Mapping[int, FixtureRecord]
    demotions: Mapping[str, str]   # nodeid -> keyed digest of its test file when demoted


@dataclass(frozen=True, slots=True)
class SelectionStoreMeta:
    size_bytes: int
    nodes: int
    runs: int
    newest_recorded_at: float | None
    python: tuple[int, int] | None
    inactive_reason: str | None
    audit_checked: int
    audit_misses: int
    demoted: int


@dataclass(frozen=True, slots=True, eq=False)
class RecordedNode:
    nodeid: str
    outcome: str
    deps: ContextDeps          # ids into RunDependencies.vocabulary
    fixtures: array


@dataclass(frozen=True, slots=True, eq=False)
class RunDependencies:
    """One run's merged dependency files (run-local vocabulary)."""

    vocabulary: DepVocabulary
    nodes: Mapping[str, RecordedNode]
    fixtures: Mapping[int, ContextDeps]
    ambient: ContextDeps
    complete: bool             # every expected process file present, valid, not dropped
    recording: bool            # at least one process had an active recorder
    python: tuple[int, int] | None
    inactive_reason: str | None
    notes: tuple[str, ...]


# -- planner (T2) -------------------------------------------------------------

@dataclass(frozen=True, slots=True, eq=False)
class SelectionInputs:
    index: ProjectIndex
    deps: DependencySnapshot
    versions: Mapping[str, FileIndex]    # recorded keyed digest -> old FileIndex (when cached)
    data_digests: Mapping[str, str]      # recorded data path -> current digest ("" absent, "?" unreadable)
    compatibility: str                   # current compatibility fingerprint
    changed: frozenset[str]              # git-changed relevant paths under the project
    test_roots: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ChangedUnit:
    kind: str                  # "function" | "name" | "module" | "data" | "file" | "unrecorded"
    label: str                 # path::qualname | path:name | path
    tests: int                 # recorded tests selected because of it


@dataclass(frozen=True, slots=True)
class SelectionDecision:
    full_reason: str | None    # the dynamic engine requires the full suite
    files: tuple[str, ...]     # test files to run, sorted
    deselect: tuple[str, ...]  # recorded, unselected node ids inside partially selected files, sorted
    whole_files: tuple[str, ...]  # files selected whole (static rule, unrecorded, changed test file)
    selected: int              # recorded tests that will run
    reached: int               # recorded tests that executed changed code but already passed on it
    recorded: int              # usable recorded tests in current test files
    total_files: int           # test files on disk
    units: tuple[ChangedUnit, ...]
    fallbacks: tuple[tuple[str, str], ...]   # (node id or test file, reason)
    coverage: float            # share of test files with a usable record
===== END EDIT

Verification after applying: `python -c "import ptest.contracts as C; p=C.SelectionPolicy(enabled=True, closed_inputs=False); import hashlib; assert hashlib.sha256(repr(p).encode()).hexdigest().startswith('43bc98485c25')"` (policy digest unchanged from 0.4.10).
```
