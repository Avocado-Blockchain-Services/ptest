# Design: offline doctor static path, bounded ranking, complete commit reminder

Date: 2026-09-29. Author: architect. Base: `feature/doctor-offline` at 985d43e (0.3.7).
Spec: `/tmp/claude-1000/-home-ingmar-code-tools-ptest/e8898983-6043-4619-a933-8d5abd5c42c4/scratchpad/doctor-offline-brief.md`.
Context pack: `.pipeline/context-pack.md`. Precedent: `src/ptest/agent_assessment.py`
(`build_packets` / `_build_one_packet`, `_review_checkpoint`, frozen dataclasses, `_fail`).

Three tasks run in parallel on the same base, each in its own worktree. Their file sets are
disjoint and no task imports anything that another task creates. There are no shared-file
edits and no transcription step.

| Task | Owns (only these files) |
|---|---|
| T1 offline static path | `src/ptest/agent_assessment.py`, `src/ptest/cli.py`, `src/ptest/help.py` (doctor topic only, D3a), `src/ptest/doctor.py` (no edit expected), NEW `tests/ng/test_doctor_offline_light.py`, NEW `scripts/bench_doctor_offline.py` |
| T2 ranking speed | `src/ptest/review_evidence.py`, NEW `tests/ng/test_rank_candidates_perf.py`, NEW `scripts/bench_rank_candidates.py` |
| T3 commit reminder | `src/ptest/config.py`, `src/ptest/init_render.py`, NEW `tests/ng/test_init_commit_reminder.py` |

The following are read-only for every task: `worktree.py`, `review_context.py`,
`deterministic_items.py`, `render.py`, `contracts.py`, `tests/ng/support.py`,
`tests/ng/factories_agents.py`, `tests/ng/test_help.py` (it must pass unmodified; T1's new
help assertions live in T1's new test file), and every existing test file except where a criterion below
says a task may update an existing assertion that its intended behaviour change breaks.

---

## 1. Findings that drive the design

1. Today every offline mode (`doctor --offline`, offline `--json`, and init's declined-review
   fallback via `_declined_review_output` -> `_doctor_static_output`) calls
   `cli._offline_assessment_parts` -> `agent_assessment.build_packets(workspace, resolution)`.
   That call has no deadline and no progress. `_build_one_packet` then runs
   `_candidate_text_pool` -> `RE.rank_candidates` -> `RE.item_source_chains` -> chain admission
   -> `_resolve_tier4` -> late admission.
2. What the offline output actually reads from a packet:
   - Rows. Model items become `unknown` rows with no evidence. Deterministic answers cite only
     the child `.ptest.toml` and the pytest config files, which `_answer_child_row` looks up by
     path. Those are *root-decision* (core) excerpts, admitted before any ranking. Skip rows
     cite tier-0 manifests, and `_plan_one` produces none today.
   - `_assessment_limitations`. It reads `excerpts` (empty or not), `excluded_count`,
     `truncated_count`, `context.config_status`, `len(context.known_excluded)` and
     `dependencies`.
   - `dependencies`. `_dependency_facts(set(paths), ...)` takes the **basenames of every
     admitted excerpt**. Root `setup.py`, `Gemfile`, `CMakeLists.txt`, `meson.build` and
     `build.gradle` are *not* root decisions: the full path admits them late. The static
     packet must still admit them, or the `dependency-unsupported` limitation would disappear.
   - `packet_sha256` (JSON only). It hashes the whole admitted excerpt list, in rank-dependent
     order, plus `_inventory_sha256` over the whole candidate text pool.
   - The grid renders rows, findings, the per-child "partial evidence" marker (whether any
     `partial-evidence` limitation exists), and the dependency limitation lines. It never
     renders `packet_sha256`.
3. `_admit_candidate` never returns False, so the full path *attempts* every non-core
   non-test regular file (chain or late) exactly once. Each attempt ends as admitted,
   `skipped += 1` (empty, NUL, non-UTF-8 or unreadable) or `truncated += 1` (a cap was hit, or
   the file was cut at `max_bytes_per_file`).
4. Consequence: `packet_sha256` **cannot** stay byte-identical without reading and ranking the
   pool, which requirement 1 forbids. All other fields can stay identical on fixtures within
   the documented conditions (section 2.4).

## 2. T1 decisions: offline static packet

### D1. Separate public builder; `build_packets` is untouched

The online path keeps calling `build_packets` exactly as today, with the same signature and
bytes. Offline calls a new builder:

```python
# src/ptest/agent_assessment.py  (FROZEN; T1-internal but fixed here)
@dataclass(frozen=True, slots=True)
class StaticPackets:
    packets: tuple[EvidencePacket, ...]          # one per selected child, workspace order
    deadline_expired: tuple[str, ...]            # declarations whose static packet stopped at the deadline, in order

def build_static_packets(workspace, resolution,
                         limits: EvidenceLimits = EvidenceLimits(), *,
                         deadline: float | None = None,
                         on_child: Callable[[str, int], None] | None = None,
                         ) -> StaticPackets: ...
```

- It uses the same type checks as `build_packets`, the same 256-child bound, the same
  `invalid-config` check, and the same `_packet_scope_context` validation for all children
  first. `unsafe-path` and similar problems propagate unchanged.
- Per child it calls `_build_one_packet(..., deadline=deadline, progress=None, static=True, on_child=on_child)`.
  `static` and `on_child` are new keyword-only parameters on `_build_one_packet`, defaulting
  to `False` and `None`. With `static=False`, code and behaviour are byte-for-byte today's.
- **Graceful deadline.** If `deadline` has already passed before a child starts, or that
  child's build raises `C.Problem` with `code == "review-timeout"`, then that child and every
  later child get `_deadline_packet(root, repo, resolution, scope_context)` and their
  declarations go into `deadline_expired`. Any other Problem propagates. `build_static_packets`
  never raises `review-timeout`.
- `on_child(declaration, file_count)` is called exactly once per child that is walked, right
  after `_iter_regular_files` and the lockfile filter. `file_count = len(regular)` after lock
  removal, before suite skips. Children cut off by the deadline before the walk get no call.

### D2. What a static packet (`static=True`) does

In order: walk, lockfile filter, `_collect_packet_context`, and `_conclusive_suite_skips`
with the `known_excluded` update. These are identical to the full path; context collection
is offline-visible fact collection (config status, suite exclusions, roles) and is kept.
Then:

1. **Core admission.** Admit `core_paths` (root decisions plus configured setups), computed
   exactly as today.
2. **Marker admission.** Take every regular file (after suite skips) that is not in
   `core_paths` and whose basename is in `_DECLARATIONS` or `_UNSUPPORTED_MARKERS`. Sort by
   `rel` and admit each through `_admit_candidate`. These files feed dependency facts, which
   offline shows.
3. **Metadata-projected accounting.** This step reads nothing. The candidates `N` are the
   regular files (after suite skips) that are not core and not markers, with test-role files
   excluded unless `unresolved_suite` holds (the same test filter as today's `late`). Sort `N`
   by `(_context_priority(rel, role_of, linked, config_paths), _admission_tier(rel), rel)`.
   Starting from `emu_files = len(state.excerpts)` and `emu_bytes = state.byte_count`, walk
   `N` and mirror the order of checks in `_admit_candidate` using sizes only:
   - if `emu_files >= limits.max_files_per_child`, then `truncated += 1`
   - else set `chunk = min(size, limits.max_bytes_per_file)` and `was_cut = size > limits.max_bytes_per_file`
   - if `chunk == 0`, then `skipped += 1`
   - else if `emu_bytes + chunk > limits.max_bytes_per_child`, then `truncated += 1`
   - else `emu_files += 1`, `emu_bytes += chunk`, and if `was_cut`, `truncated += 1`

   This step changes only `state.skipped` and `state.truncated`. It never touches the
   candidate read ledger, `state.excerpts`, `byte_count` or `paths`. It returns the number of
   emulated admissions (the count of `emu_files += 1` steps). Name it
   `_project_static_admission(state, candidates: list[tuple[str, int]], limits) -> int`.
   The return value becomes the packet's `_projected_admitted` (step 6, D2a).
4. **Dependencies.** `_dependency_facts(...)` runs exactly as today on the static `paths` and
   state, with `package.json` text taken from the core excerpt.
5. **Prompt cap.** The prompt-cap trim loop runs unchanged.
6. **Packet fields.** `_inventory_sha256=None`. `packet_sha256 = packet_hash(packet)`, which is
   the body hash; an online packet always carries an inventory digest, so the two identities
   are built differently and cannot be confused. Set
   `_item_chains = tuple((digest, item.id, ()) for item in model_items)` so that
   `RE.select_item_sources` uses the bound empty chains and **never** calls
   `item_source_chains` offline. Set `_projected_admitted` to step 3's return value.
7. **Never called with `static=True`:** `_candidate_text_pool`, `RE.rank_candidates`,
   `RE.item_source_chains`, `_resolve_tier4`, `complete_chain` and `admit_chain`. No
   non-core, non-marker file is read by admission.

`_deadline_packet(root, repo, resolution, scope_context) -> EvidencePacket`:

- `project_id`, `runner_kind` and `scope` are resolved exactly as at the head of
  `_build_one_packet`, including the fallback for `declaration == "."` with no config.
- `excerpts=()`, `dependencies=()`, all counts `0`, `context=None` (becomes
  `RC.empty_context`), `_inventory_sha256=None`, `_projected_admitted=0`.
- `_item_chains` is bound to its own digest, as in step 6.

### D2a. Evidence presence survives the static packet (frozen, T1)

`_assessment_limitations` (cli.py:1651, 1666) branches on `not packet.excerpts` to choose
between "No source files were admitted to this project packet. " and "Evidence limits: ",
and to decide whether a `partial-evidence` limitation exists at all. A static packet admits
only core and marker files, so under `--scope <subdir>` (every rel is nested, so there are
no priority-0 root files, and conftest.py has role `fixture`, not `setup`) or in an
uninitialized repo with no root decision or marker file, `excerpts == ()`. The full path
would late-admit the tree's non-test files there. The static packet therefore carries the
projected admission count, and the limitation reads evidence presence through one helper:

```python
# src/ptest/agent_assessment.py  (FROZEN)
@dataclass(frozen=True, slots=True)
class EvidencePacket:
    ...                                   # every existing field unchanged, same order
    _inventory_sha256: str | None = None
    _projected_admitted: int = 0          # NEW, last field. Static packets only; 0 on every
                                          # packet built by build_packets. __post_init__
                                          # rejects bool and negative values (TypeError/ValueError).

def packet_has_evidence(packet) -> bool:
    """bool(packet.excerpts) or getattr(packet, "_projected_admitted", 0) > 0."""
```

- `_projected_admitted` is **not** part of `_packet_body`, `packet_hash` or any rendered or
  serialized field. Online `packet_sha256` values and T2's base-captured goldens are
  unaffected.
- `cli._assessment_limitations` replaces both `not packet.excerpts` tests (the `if`
  condition and the message prefix choice) with `not agent_assessment.packet_has_evidence(packet)`.
  This is the only edit to a function the online path shares. It is behaviour-neutral
  there because `build_packets` packets always have `_projected_admitted == 0`, and
  existing tests that build packets or fakes directly (`test_agent_assessment.py:1346-1389`)
  must pass unmodified. `getattr` keeps fakes without the attribute working.
- Why this is exact: under the exactness condition (D4), every candidate that step 3 counts as
  admitted is a non-empty UTF-8 file that the full path late-admits, so
  `packet_has_evidence(static) == bool(full.excerpts)` whenever step 3 admits at least one
  file or a core or marker file was admitted. The remaining case is D4 row 4.

### D3. cli wiring (frozen shapes, T1)

```python
# src/ptest/cli.py
_OFFLINE_DEADLINE_MESSAGE = ("Offline static inspection stopped at the doctor deadline "
                             f"({_REVIEW_TOTAL_TIMEOUT_S} s); this project was not fully inspected.")

def _offline_assessment_parts(resolution, domain, workspace, *,
                              deadline: float | None = None,
                              on_child: Callable[[str, int], None] | None = None): ...
def _doctor_offline_assessment_json(resolution, domain, workspace, *,
                                    deadline: float | None = None,
                                    on_child: Callable[[str, int], None] | None = None) -> bytes: ...
def _offline_progress(parsed: ParsedArgs, resolution) -> Callable[[str, int], None] | None: ...
```

- `_doctor_static_output` keeps `started = time.monotonic()` as its first statement. It sets
  `deadline = started + _REVIEW_TOTAL_TIMEOUT_S`, reusing the existing 1800 s review budget
  and inventing no new constant, and passes `deadline` and `_offline_progress(parsed, resolution)`
  to both the JSON and grid paths.
- `_offline_assessment_parts` calls `agent_assessment.build_static_packets(workspace, resolution, deadline=deadline, on_child=on_child)`
  through the module attribute, so tests can monkeypatch it. Everything after it is unchanged
  (`_plan_item_reviews`, `_assemble_with_parallel`, `_child_assessment_data`), iterating
  `result.packets`.
- For each packet whose declaration is in `deadline_expired`, append
  `{"code": "partial-evidence", "message": _OFFLINE_DEADLINE_MESSAGE, "paths": [packet.scope]}`
  to that child's limitations, deduplicated. Also append it to the top-level limitations after
  `_assessment_limitations(packets, top_level=True)` and the initialization blocker, keeping
  the existing `[:64]` bound. No new limitation code is added: `contracts.py` is untouched.
- `_offline_progress` returns `None` when `parsed.quiet` is set or `sys.stderr.isatty()` is
  false. `parsed.quiet` can be True on this path only through `doctor --offline -q` (D3a).
  `ptest init` and default `ptest doctor` gain no quiet flag, so their declined-review
  fallback always has `quiet == False` and narrates only on a TTY (a declined review has
  already interacted on that TTY). Otherwise it returns an emitter that writes exactly one line per call to **stderr
  only**, flushed:
  `ptest: doctor: inspecting {label} · {C.plural(file_count, 'file')}`.
  `label` is the `declaration`, or `resolution.root.name` for `"."`, passed through
  `render.terminal_text`. Match the non-UTF-8 fallback of the existing `ptest:` stderr
  narration. The emitter swallows `OSError`. Stdout is never written, including under `--json`.
- The online path (`_run_doctor_review`) and `build_packets` get **zero** edits. The only
  shared-function edit is the behaviour-neutral `_assessment_limitations` change in D2a.
- `doctor.py` needs no change: the file count comes from the packet walk. If T1 finds it must
  edit `doctor.py`, the change stays within `inspect_workspace` and changes no output.

### D3a. `doctor --offline -q | --quiet` grammar (frozen public CLI contract, T1)

Only the `doctor` branch of `_parse_inspection` changes. The `init` parser and the execution
parser are untouched.

- Tokens: `-q` and `--quiet`, which are synonyms. Track `quiet_seen`. A second occurrence of
  either token raises `invalid-config` "option cannot be repeated" (same rule as doctor's `-v`).
- Validation, in the parser's existing order (first failing rule wins):
  1. With `--probe`, the existing output-mode check becomes
     `if json_output or verbose_seen or quiet_seen:` and raises the existing
     "doctor probe cannot combine output modes".
  2. With `--fix`, the existing check becomes `if json_output or scope is not None or limits or quiet_seen:`
     and raises the existing "--fix takes no output, scope, or scan-limit options".
  3. After the `--fix` block and before `if fix_dry or verbose_seen:`, add:
     `if quiet_seen and not offline: raise _problem("invalid-config", "--quiet requires --offline")`.
  4. Otherwise `--offline -q` is accepted with and without `--json`, `--scope` and the scan
     limits. The final `ParsedArgs(...)` gets `quiet=quiet_seen`. Under `--json` it is a
     no-op, because JSON never narrates.
- Effect: it suppresses only the D3 offline progress lines. Stdout, the exit status and
  every other stderr line are unchanged.
- Help (`src/ptest/help.py`, `_DOCTOR` only): the offline syntax line becomes
  `ptest doctor --offline [--json] [-q | --quiet] [--scope PATH] [--max-entries N]` (the
  continuation line is unchanged). Add one Notes sentence: "Offline static inspection
  prints one progress line per project to stderr on a TTY; -q/--quiet suppresses it."
  No other help topic, README or guide text changes. The text must pass the existing
  `test_help.py` checks unmodified, including the banned-terms check.

### D4. Documented, unavoidable differences (offline only; the online path is unchanged)

| Field | Difference | Why |
|---|---|---|
| `children[*].packet_sha256` (JSON only; never in the grid) | Always differs from 0.3.7: it is the static packet's body hash | The old value commits to ranked late excerpts and the pool inventory digest, which requirement 1 forbids building. Offline publication is always `skipped` and no report is written, so nothing compares it with an online identity. |
| `partial-evidence` counts, and in rare cases whether that limitation exists (so the grid's partial marker) | Can differ when a per-child cap binds (more than 64 admitted files, more than 512 KiB, or an exhausted candidate ledger), or when a non-core candidate is binary, non-UTF-8 or unreadable | The static accounting projects outcomes from size metadata only. Content and rank order are unknowable without the forbidden reads. |
| Lock `locked` vs `uninspectable`; scoped `ref_path` when two marker files share a basename | Can differ only when the full path's candidate ledger is exhausted, or when duplicate marker basenames exist in a scoped run | The static ledger is not drained by pool reads, and marker admission order is `rel`-sorted rather than rank-ordered. |
| The child's `partial-evidence` limitation (so the grid's partial marker, and the deduplicated top-level copy) when the static packet has **no evidence**: no core file, no marker file, and step 3 projects zero admissions. Typical cases are `--scope <dir>` over a tree of only test-role files plus empty `__init__.py`, and an uninitialized repo that holds only tests. | Static always emits "No source files were admitted to this project packet. ..." for that child. The full path emits the same text only if it admitted no test chain; otherwise it emits "Evidence limits: ..." or no `partial-evidence` limitation. | Every remaining file is test-role. The full path admits test files only as chain callers selected by `RE.item_source_chains` over read and ranked test texts. Knowing whether any chain exists requires exactly those reads, which requirement 1 forbids. The static message is literally true: the static packet admitted no source files. |

**Exactness condition (tested).** All of the following hold:
- No per-child cap binds.
- Every non-core candidate is either empty or UTF-8 text without NUL. This covers empty
  `__init__.py` files and a single non-test file cut at `max_bytes_per_file`.
- No chain-admitted test file exceeds `max_bytes_per_file`.
- The static packet has evidence (`packet_has_evidence`, D2a); that is, row 4 does not
  apply.

This holds for scoped runs and uninitialized repos too. Under that condition the grid text
(duration masked) is byte-identical to today's, and so is the JSON document except
`children[*].packet_sha256`.

## 3. T2 decisions: faster `rank_candidates`, identical selection

Public signatures are unchanged: `rank_candidates`, `rank_item_candidates`,
`select_item_sources`, `item_source_chains`, `source_units` and `source_id`. Existing private
helpers keep their current positional parameters. Any new parameter is keyword-only with a
default, because `select_item_sources` and the chain code call them positionally.

Frozen private names (tests target them):

```python
@dataclass(frozen=True, slots=True)
class _ContextIndex:
    roles: Mapping[str, str]          # dict(context.roles), last value wins (== old next(...) scan)
    outgoing: frozenset[str]          # relation sources
    incoming: frozenset[str]          # relation targets
    config_paths: frozenset[str]      # getattr(context, "config_paths", ())

def _build_context_index(context) -> _ContextIndex: ...
def _context_index(context, signal_cache: dict | None) -> _ContextIndex:
    """Memoized under signal_cache[("context-index", id(context))] = (context, index);
    reuse only if the stored object `is context` (the stored ref pins the id)."""

def _relation_score(path, context, *, index: _ContextIndex | None = None) -> int
def _source_role(path, context, *, index: _ContextIndex | None = None) -> str
def _is_config_path(path, context=None, *, index: _ContextIndex | None = None) -> bool

@functools.lru_cache(maxsize=256)
def _compiled_patterns(text_patterns: tuple[str, ...]) -> tuple[re.Pattern, ...]
    # _resource_patterns(entry) returns _compiled_patterns(tuple(entry.text_patterns))

def _call_names_hit(patterns, call_names: tuple[str, ...]) -> bool
    # == any(p.search(n) or p.search(n + "(") for n in call_names for p in patterns)

_IDENTIFIER_SPLIT_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|_+")
    # _python_identifier_text: _IDENTIFIER_SPLIT_RE.sub(" ", " ".join(names)), one pass per text
```

Rules:

- `rank_candidates` uses a local `signal_cache = {}` when given `None`. The cache only
  memoizes, so the output is unchanged. `_item_rank`, `_has_item_anchor` and
  `_unit_priority` get the index through `_context_index(context, signal_cache)`.
- `_call_names_hit` joins the names once:
  `joined = "\n".join(f"{n}\n{n}(" for n in call_names)`. For each pattern it runs a sound
  prefilter compiled with `pattern.flags | re.MULTILINE`, cached per pattern. **Only** when
  the prefilter hits does it run the exact original per-name loop for that pattern. Patterns
  whose source contains `\A`, `\Z`, `(?<`, `(?=` or `(?!` skip the prefilter and always use
  the exact loop. Result: one search per pattern per file on the common no-hit path, with
  semantics identical to the old code.
- Optional exact memoization is allowed: per-file `_GENERIC_CONTROL_RE`/`_TIME_*` results,
  and per-(file, pattern) `body_hit` results, in `signal_cache`. Any other optimization
  **must** keep `rank_candidates`, `item_source_chains` and `select_item_sources` outputs
  identical.
- No change to `_item_rank` tuple shape, to catalog data, or to any file outside
  `review_evidence.py`.

## 4. T3 decisions: complete commit reminder

The helper is frozen and lives in `config.py`:

```python
def _merged_commit_paths(boundary: Path | None, config_root: Path,
                         written: tuple[str, ...]) -> tuple[str, ...]:
    """`written` first (order kept), then every uncommitted ptest file, deduplicated,
    as boundary-relative posix paths. () when boundary is None."""
```

- It scans `_worktree.uncommitted_config_files(boundary, include_agent_rules=True)`. If
  `config_root != boundary` and `config_root` lies inside `boundary`, it also scans
  `uncommitted_config_files(config_root, include_agent_rules=False)`, prefixed with
  `config_root.relative_to(boundary).as_posix() + "/"`. The helper never raises: the worktree
  helper already returns `()` on any failure, and `written` is kept.
- `init_project` applies it to **every non-preview result it returns**:
  - `_existing_result` returns (config unchanged). Wrap at the `init_project` return sites;
    do not change `_existing_result`'s signature, because `_init_from_main` also uses it.
  - Monorepo CREATED returns.
  - Standalone CREATED returns.

  `boundary = _git_boundary(physical_cwd)` (already computed for created paths; compute it
  the same way for early returns, treating a raised Problem as `None`).
  `config_root = result.target.parent`.
- Unchanged, and asserted as negative contracts:
  - PREVIEW (`--dry-run`) results keep `commit_paths=()`.
  - `--from-main` results keep `()`: the stopgap warning governs them.
  - Non-git directories give `()`.
  - Files that are not ptest files are never listed.
- `cli.py` needs **no** edit. The existing guidance merge at `cli.py` around line 2420
  appends files that `agent_rules.apply` created or updated in this run, skipping any path
  already in `result.commit_paths`. That covers files written after `init_project`'s scan.
  JSON `commit_paths` follows automatically through `C.serialize_init_result`.
- `init_render._commit_reminder_lines`: update its docstring to "every uncommitted ptest
  file". It still renders from `result.commit_paths` and still wraps (no cap, no
  truncation). Paths stay sanitized through `terminal_text`.

## 5. Per-task acceptance criteria

All test runs go through `ptest --workers 2 --queue-timeout 1800 <paths>`. No test may
assert on wall-clock speed or sleep. `invoke()` timeouts are at least 20 s. Keep each test
under 2 s: fixtures are small and function-scoped. Commit only in your own worktree, run
`graphify update .` after source changes, and never push or merge.

### T1: offline static path

1. `tests/ng/test_doctor_offline_light.py`. **Negative contract:** with
   `ptest.review_evidence.rank_candidates`, `ptest.review_evidence.item_source_chains`,
   `ptest.agent_assessment._candidate_text_pool` and `ptest.agent_assessment._resolve_tier4`
   monkeypatched to `pytest.fail`, all three paths exit 0 and produce the document:
   `main(("doctor","--offline"))`, `main(("doctor","--offline","--json"))`, and init's
   declined-review fallback (`_declined_review_output`).
2. **Identity** (same test file). Compute the reference by monkeypatching
   `agent_assessment.build_static_packets` to return
   `StaticPackets(agent_assessment.build_packets(workspace, resolution), ())`, which is
   today's full path. Compare it with the static result:
   - JSON bytes equal after replacing each `children[*].packet_sha256`.
   - Grid stdout equal after masking only the header duration token.
   - `_offline_assessment_parts` child rows, findings, scores, execution, facts and
     limitations equal.

   The fake must accept and ignore `build_static_packets`' keyword arguments.

   Fixtures, all within the exactness condition:
   - standalone (`factories_agents.doctor_assessment_project`);
   - a 2-child v2 monorepo;
   - a pytest child with empty `__init__.py` files and one non-test file larger than
     `MAX_BYTES_PER_FILE`;
   - a child with a root `setup.py` (an unsupported marker);
   - a node child with `package.json`;
   - **scoped:** the 2-child monorepo run as `doctor --offline --scope api/tests` and
     `--offline --json --scope api/tests`, where `api/tests` holds test files, empty
     `__init__.py` and a non-empty `conftest.py`. Also a standalone project run with
     `--scope src/pkg`, holding one non-test module;
   - **uninitialized:** a standalone directory with no `.ptest.toml`, no root decision file
     and no marker, holding `src/app.py` and `tests/test_app.py`.

   For the scoped and uninitialized fixtures, also assert on the static packets directly
   that `excerpts == ()` and `_projected_admitted > 0`. This proves that D2a's branch, not
   luck, keeps the output identical.
3. **Documented differences.**
   - Cap row (D4 row 2): a fixture with more than 64 non-test files. Both paths show a
     `partial-evidence` limitation for that child and identical rows. Only counts and
     `packet_sha256` may differ; assert exactly that.
   - No-evidence row (D4 row 4): the 2-child monorepo with `--scope api/tests`, where
     `api/tests` holds only `test_*.py` files and empty `__init__.py`. The static child's
     limitations contain the `partial-evidence` entry whose message starts
     "No source files were admitted to this project packet. ". Rows are identical. After
     removing `children[*].packet_sha256` and every `partial-evidence` entry whose paths
     are `["api/tests"]` (child and top level), the static and reference JSON are equal.
     Assert exactly that, and nothing about the reference's message, because whether it
     admits a chain depends on ranking.
4. **Read contract.** In static mode, wrap `agent_assessment.read_regular` to record paths.
   No admission-phase read touches a non-core, non-marker source file, for example
   `src/app/service.py` in a pytest fixture. Context-collection reads are allowed; attribute
   them by running with `_collect_packet_context` wrapped. Static packets have
   `_inventory_sha256 is None` and every `_item_chains` entry is `()`.
5. **Deadline.** With `cli._REVIEW_TOTAL_TIMEOUT_S` monkeypatched to `0`, the offline grid
   and JSON exit 0. Every child is unknown/unscored, each child and the top level carry the
   `_OFFLINE_DEADLINE_MESSAGE` partial-evidence limitation, and the JSON decodes through
   `C.decode_public_document`. A direct `build_static_packets(..., deadline=time.monotonic() - 1)`
   returns every declaration in `deadline_expired` and does not raise. A non-timeout Problem,
   such as an unsafe scope, still raises.
6. **Progress.** Fake `sys.stderr.isatty` to True: exactly one
   `ptest: doctor: inspecting <label> · N files` line per child, with N equal to the child's
   regular-file count after lock removal, `.` rendered as the repo directory name, and
   singular `1 file`. When stderr is not a TTY there are no lines and existing
   `err == ""` assertions still hold. `--json` stdout contains no progress bytes. A
   declaration containing control characters is sanitized.
   **Quiet, through the CLI (D3a).** With the TTY faked, `main(("doctor","--offline","-q"))`
   and `main(("doctor","--offline","--quiet"))` exit 0 and write no progress line. Their
   stdout equals the un-quieted run's stdout, with the duration masked.
   `main(("doctor","--offline","--json","-q"))` exits 0 with the same JSON bytes as without
   `-q`. Parser contract through `cli.parse_argv`:
   - `("doctor","--offline","-q")` and `("doctor","--offline","--json","--quiet")` give
     `quiet is True`.
   - `("doctor","-q")` raises `invalid-config` "--quiet requires --offline".
   - `("doctor","--offline","-q","--quiet")` raises "option cannot be repeated".
   - `("doctor","--fix","-q")` raises "--fix takes no output, scope, or scan-limit options".
   - `("doctor","--probe","--scope","tests/x.py","-q")` raises "doctor probe cannot combine
     output modes".
   - **Negative contract:** `("init","-q")` still raises "unknown inspection option".

   `main(("help","doctor"))` output contains `[-q | --quiet]` and the D3a Notes sentence.
7. **Online unchanged.** `build_packets` gives the same `packet_sha256` as before on the
   standalone fixture: compute it through `build_packets` in the test and assert it differs
   from the static digest while `build_packets` itself was not edited (diff check in review).
   Every `build_packets` packet has `_projected_admitted == 0`, and adding that field leaves
   its `packet_sha256` unchanged.
   Existing online tests in `test_agent_assessment.py` and `test_agent_doctor_acceptance.py`
   pass unmodified.
8. **Benchmark.** `scripts/bench_doctor_offline.py` is self-contained and deterministic. It
   generates a monorepo with about 1000 files (2 children, pytest and vitest shaped, fixed
   `.ptest.toml` with a fixed `project_id`) in a `tempfile.TemporaryDirectory`, then times
   `ptest doctor --offline` and `--offline --json` in-process through `ptest.cli.main` from
   this checkout (never the installed CLI). It prints one JSON line per mode:
   `{"mode", "files", "seconds"}`. It must also run on base 985d43e: the before numbers come
   from a detached base worktree under `/home/ingmar/worktrees/ptest/cc-doctor-offline/`
   with its own `uv sync`. Run it once before and once after, and record both in the T1
   report. It is not collected as a test.
9. Scoped runs: `tests/ng/test_doctor_offline_light.py tests/ng/test_agent_doctor_acceptance.py tests/ng/test_doctor_grid.py tests/ng/test_cli.py tests/ng/test_agent_assessment.py tests/ng/test_init.py tests/ng/test_help.py tests/ng/test_doctor_fix.py`.

### T2: ranking speed

1. `tests/ng/test_rank_candidates_perf.py`. **Goldens captured at base.** First, write a
   test that builds deterministic fixtures and records `rank_candidates(...)` tuples and
   `build_packets(...)` `packet_sha256` values. Run it once on **unmodified** code, paste the
   literals, then optimize; the literals must still match afterwards. Fixtures:
   - a generated ~60-file python-and-JS child with more than 30 context roles, relations and
     fixture-use edges, and a fixed `.ptest.toml` project_id;
   - `factories_agents.doctor_assessment_project`;
   - one v2 child with a declaration prefix.

   Record the capture command and base commit in the T2 report.
2. **Differential oracles.** Keep the old implementations verbatim in the test as reference
   functions and compare them with the new ones over the fixture texts and a generated
   identifier corpus that includes `camelCase`, `HTTPServer`, `a1B`, `__dunder__`,
   `snake__case_`, non-ASCII identifiers, and syntax-error text:
   - `_python_identifier_text`
   - `_relation_score`, `_source_role` and `_is_config_path` over every path in the context
   - call-hit (old double loop vs `_call_names_hit`) over every catalog entry's patterns,
     plus crafted patterns using `^`, `$`, `\b`, `\s`, `\A` and lookbehind
3. **Operation counts** (deterministic):
   - `_build_context_index` runs once per `rank_candidates` call, independent of
     files × items; count it with a monkeypatched wrapper.
   - With no pattern hits, `_call_names_hit` performs at most `len(patterns)` prefilter
     searches and zero exact-loop searches; count by wrapping the prefilter cache or passing
     counting pattern proxies.
   - `_python_identifier_text` calls `_IDENTIFIER_SPLIT_RE.sub` once per text; count through
     a module-attribute proxy.
4. `scripts/bench_rank_candidates.py`: a self-contained generator of about 1000 files and
   about 700 roles. It times `rank_candidates`, captured through `build_packets` on the
   generated child, and must run on base as well. Record before and after once in the T2
   report.
5. Scoped runs: `tests/ng/test_rank_candidates_perf.py tests/ng/test_review_evidence.py tests/ng/test_agent_assessment.py tests/ng/test_review_context.py`. Existing tests pass unmodified.

### T3: commit reminder

1. `tests/ng/test_init_commit_reminder.py`, using `support.init_git_repo`, `git` and
   `write_ptest_toml`:
   - A git repo with a committed unrelated file and **untracked** `.ptest.toml`,
     `api/.ptest.toml`, `web/.ptest.toml` (v2 manifest) and
     `.claude/skills/ptest/SKILL.md`. Running `ptest init` (config unchanged, no agents) lists
     all four in `Commit these files:`, and `init --json` `commit_paths` equals the same
     ordered list.
   - The same repo after `git add` and commit shows no reminder and `commit_paths == []`.
   - A fresh standalone create has `.ptest.toml` first, followed by other pre-existing
     uncommitted ptest files, deduplicated.
   - Agent-rule files created in this run are appended once (cli merge) with no duplicates.
   - Untracked `README.md` and `notes.toml` never appear.
   - A non-git directory gives `[]`.
   - `--dry-run` gives no reminder and `[]`.
   - A nested config root (init from a subdirectory of the git root, config written at the
     cwd) lists that config with its boundary-relative path.
   - When `worktree.uncommitted_config_files` is monkeypatched to return `()`, the result
     falls back to the written list.
   - A path containing control characters is rendered sanitized.
2. Existing `tests/ng/test_init.py` and `tests/ng/test_agent_rules.py` assertions that pin
   `commit_paths` may be updated **only** where the new rule adds a file that is genuinely
   uncommitted in that fixture. List each changed assertion and the reason in the T3 report.
   Never delete or loosen an assertion.
3. Scoped runs: `tests/ng/test_init_commit_reminder.py tests/ng/test_init.py tests/ng/test_agent_rules.py tests/ng/test_agent_doctor_acceptance.py`.

## 6. Security and abuse cases (secure-by-spec)

- **No provider launch and no report write offline.** The existing acceptance tests
  monkeypatch `resolve_reviewer`/`launch_reviews` to fail and assert an unchanged tree; they
  must stay green.
- **No reads beyond necessity.** Static admission reads only core and marker files through
  the existing no-follow, byte-bounded `read_regular`. The walk still never follows symlinks.
- **Deadline cannot be bypassed.** Every existing `_review_checkpoint` is kept in the static
  path, with `progress=None` and `deadline` set. A timeout degrades to a deadline packet and
  never produces a traceback.
- **Terminal injection.** Progress labels and reminder paths go through
  `render.terminal_text`, and progress never touches stdout.
- **Commit reminder.** Only ptest-owned names from `worktree.AGENT_RULE_FILES` and the
  config set are listed. The git subprocess stays bounded (2 s inside the helper) and fails
  closed to `()`.
- **Online integrity.** `build_packets` and the online review are unedited by T1. T2 is
  guarded by base-captured goldens and differential oracles.

## 7. Integration (orchestrator)

Merge T1, T2 and T3 onto the chain branch in any order; no conflicts are expected because
the file sets are disjoint. Then run one integrated `ptest --full`. After merge, T1's
identity test exercises T2's ranking inside the reference full path. It must still pass,
which is an end-to-end check that T2 preserved selection.
