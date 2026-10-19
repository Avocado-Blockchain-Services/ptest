# ptest 0.7.0: tiered iteration selection, one gate per spec

Status: approved 2026-10-19 (auditor-agent, Opus, round 4: APPROVED after four
architect/auditor rounds; decisions delegated by the user under KISS and least
technical debt). Not yet implemented.

Pytest selection only (Part 1 states what Vitest gets). Five parts, one release:
P1 evidence tiers, P2 learned observers, P3 records chosen by tree distance,
P4 hint, guide and JSON contract, P5 declared ambient fingerprint. Each part has
its own contract clauses; the negative contracts, smoke tests and rollout cover
all five. The open questions of the first draft are settled in "Decisions
(2026-10-19)" at the end; the clauses below state them.

## Problem

Agent pipelines on any repository that uses ptest run nearly the whole suite on
every iteration, task check and retry. One example (not the design target): an
API with about 15k pytest tests; a feature round edits central modules; every
task check runs `ptest --base <parent branch>` up to three times per task, plus
the builder's own runs; each run selects about 14,700 tests.

The root pattern is structural. ptest's selection is a proof system: whenever it
cannot prove a test unaffected, it widens. Real applications are hub-shaped. The
app, its settings, models and routers are imported at collection time by every
test file, and framework classes (pydantic models, ORM tables, decorated routes)
register themselves at import. For such a tree "unprovable" is "everything".

Four widening paths turn a precise dynamic selection into the whole suite. All
four were verified against the code on 2026-10-19:

1. **Clause g** in `src/ptest/selection_planner.py` `plan()` (lines 534-560
   precompute, 669-681 select). `shared` selects every node of a run whose
   ambient context executed a changed function (`cf`, `af` or a `pw` path) or
   imported a module in `am`/`pw`. `ambient_reach` selects every test file in
   the static import reach of the `am` paths. `am` comes from `_diff_path`
   (lines 176-186 whole-path or unparseable changes, 195-201 an added decorated
   scope or method, 215-217 a non-local skeleton change, 218-222 a removed
   decorated scope or method, 230-241 any non-plain class change or removal,
   252-253 an effectful module statement) and from `_propagate` (lines 293-310
   a def or class whose skeleton references a changed name, 319-321 an effect
   statement referencing one, 330-335 a non-plain class elsewhere referencing
   one). A pydantic field, a dataclass default, an enum member, an ORM column, a
   decorated route: all `am`.
2. **Static import reach** for opaque or demoted nodes (clause b, lines
   614-631) and for test files without a usable record (4.6, lines 723-732).
   In a hub tree the reach of a central module is every test file.
3. **`[selection] full_ratio`** (default 0.70) promotes a dynamic selection to
   the full suite when it reaches 70% of recorded tests or of test files
   (`src/ptest/selection_engine.py` `_full_text`, lines 524-539) and a static
   selection likewise (`src/ptest/impact.py` lines 587-591, 677-681).
4. **`MAX_SELECTED = 200`** (`src/ptest/impact.py:23`) promotes any selection
   of more than 200 test files to the full suite (`impact.py` lines 592-595,
   682-685; `selection_engine.py` lines 540-546).

Facts that shape the design (verified; corrections to the earlier analysis are
marked):

- Repeat `--base` runs are already incremental for recorded passing nodes. Each
  node is judged against its own run's digests (`_stale_sets`, planner lines
  65-82 and 470-475). The `--base` diff feeds only the static reach of opaque,
  demoted and unrecorded nodes (`reach_b`, `reach_all`, `static_all`), `touch`
  and `nonpy_changed` (lines 464, 529-533, 725).
- An explicit `--base` skips the last-green cache (`src/ptest/cli.py`
  `_consult_reference`, lines 3378-3399; `src/ptest/lastgreen.py` `consult`,
  lines 348-355).
- The selection store is one SQLite file per account and committed
  `project_id` (`src/ptest/selection_store.py` module docstring), shared by
  every worktree of the repository. `nodes(nodeid TEXT PRIMARY KEY, ...)` plus
  `INSERT OR REPLACE` (`_store_nodes`, lines 1115-1129) means one record per
  node and **the last committed writer wins** (commit order, not
  `recorded_at`). A sibling worktree's central edit therefore makes every
  record it rewrote stale for everyone else.
- **Correction.** A run whose tests failed still ingests, when its handoff was
  valid: `ingestible` (`selection_engine.py` lines 791-795) checks handoff,
  unchanged inputs, guard, setup, completeness, recording and the key, not the
  test status; failing nodes are recorded with outcome `failed` and clause a
  reruns them. Only a cancelled run (lines 771-779, outcome widening only) and
  an invalid or incomplete handoff (lines 800-805, `_fallbacks` with
  invalidation) ingest nothing.
- `compatibility_fingerprint` (`selection_engine.py` lines 259-296) covers
  `uv.lock`, `poetry.lock`, `Pipfile.lock`, `pyproject.toml`, `setup.py`,
  `setup.cfg`, `pytest.ini`, `tox.ini`, `.ptest.toml`, `.python-version`,
  `requirements*.txt`, the policy repr, the runner kind, launcher and args, the
  platform and `full_triggers`. A lockfile bump therefore makes every record
  "another configuration" (`_OTHER_CONFIG`, line 494-496 when no run matches;
  `_OTHER_CONFIG_REASON` per node, planner lines 575-577). **Addition:** the
  same lockfile is also a full trigger at the static stage
  (`impact.py` `_TRIGGER_BASENAMES`, lines 146-150; lines 437-440 and 456-458),
  so every changed run whose diff still contains the bump is a full run,
  before any dynamic planning.
- Every narrowed green prints `next: ptest --full before handoff` or
  `next: ptest --full --cov before handoff` (`src/ptest/progress.py`
  lines 196-215; emitted from `src/ptest/operations.py` lines 2538-2545 and
  `cli.py` line 4092), and the guide's loop table says "Before handoff or
  merge". Pipelines obey and run the gate per task.
- The full-run self-audit (`selection_engine.py` `_self_audit`, lines
  997-1052) plans against the pre-update snapshot with `changed = ∅` (lines
  1015-1017), demotes every miss to the static rule (`store.demote`), and the
  planner then applies static reach to demoted nodes (`deps.demotions`, lines
  604-605, 614-631). In a hub tree a demoted test runs on every change.
- `ptest plan --json` is a static preview only (`cli.py` lines 3048-3070). The
  dynamic decision is visible only in the run itself.
- Vitest: a changed run delegates to `vitest run --changed <base sha>`
  (`impact.py:479`; `cli.py` lines 3355-3358, 3754-3757, 3855-3858). ptest has
  no per-test Vitest results and no JavaScript import index
  (`src/ptest/adapters/vitest.py` `selection=False`;
  `tests/ng/fixtures/vitest/NATIVE_MATRIX.md`). Vitest's `--changed` and
  `--related` are both transitive import reach, so no "direct first" split
  exists natively.

## Fixed policy (normative, not relitigated here)

`ptest --full` and `ptest --full --cov` run **once**, right before the whole
spec or chain is finished. Never per task, never per handoff, never during
iteration. Everything before that single gate is iteration. Soundness is owed
only by that gate. Iteration runs owe honesty: they must say exactly what they
did not run.

## Vocabulary

- **Iteration run**: bare `ptest`, `ptest --changed`, `ptest --base REF`, and a
  folder scope that goes through selection (`cli.py` `_run_impact_standalone`,
  `_run_changed_folders_standalone`, the monorepo twins). Not `--full`, not a
  literal file scope the user typed (always runs).
- **Gate**: a run executed in full mode (`execution == "full"`, the condition
  under which `_ingest_run` calls `_self_audit` today, `selection_engine.py`
  line 960): a `ptest --full` run (with or without `--cov`), or an iteration
  run that R8 forced to the full suite (R8a). The only gate the fixed policy
  plans for is the one `ptest --full --cov` at the end of the chain.
- **Gate marker**: the commit and dirty-file fingerprint of the last gate's
  tree (R27). **Since the gate** (`since_gate`): every path that differs
  between that tree and the current tree (R9).
- **Record**: one `nodes` row: a test node id, its dependency sets and the run
  (baseline digests) it was recorded under.
- **Chosen record**: the record the planner judges a node by (P3).
- **T1, run now**: positive evidence that the change reaches the test. Always
  runs in an iteration run.
- **T2, deferred**: the proof system cannot prove the test unaffected, and no
  T1 clause says it is affected. Never runs in an iteration run; the gate runs
  it. (Earlier drafts had a separate weaker "T2" and called this tier "T3";
  the weaker tier is merged into T1 and its codes remain.)
- **Deferred count**: the number of T2 tests of the current plan. It is
  recomputed from records, the gate marker and the tree on every plan; nothing
  is persisted per checkout.
- **Observer**: a test that is known or declared to notice import-time effects
  of changes it never executes (a route or schema snapshot test).
- **Ambient fingerprint**: the keyed digest of the stdout of a declared command
  that renders import-time state (an OpenAPI document, ORM metadata, a route
  manifest).

## Contract

### Part 1: evidence tiers for iteration runs

R1. **Tagging.** `selection_planner.plan` returns, for every recorded node and
every unrecorded test file it considered, exactly one tier and one clause code.
T1 codes, in order: `a` (non-passing chosen record), `c` (changed test file),
`d` (executed changed or propagated function), `f` (changed data file read),
`n` (new test file, whole), `o` (observer promoted, P2), `e` (the test context
itself imported a skeleton-changed module), `w` (soft-fallback import witness,
R4). T2 codes, in order: `g-shared` (the run's ambient context executed a
changed function or imported a changed module; planner lines 669-674),
`g-reach` (test file in the static reach of an `am` path; lines 675-681),
`g-data` (an ambient data file changed; today's planner `full_reason`, lines
496-516), `b-static` (opaque or demoted node reached statically; lines
614-631), `u-static` (test file without a usable record, reached statically;
lines 723-732). `SelectionDecision` gains `tiers: Mapping[str, tuple[str,
str]]` (node id or test file → (tier, code)); it is an in-memory contract, not
persisted. A node that matches several clauses gets T1 if any T1 clause holds,
and within its tier the first code in the order above. `SelectionDecision.reached`
(the "already passed on this code" count) never includes a T2 node:
`T2 ∩ reached = ∅`.

R2. **T1 always runs.** In an iteration run every T1 node is in the argv files
and never in the deselect binding. Clause order and semantics are those of
planner lines 611-667 and 686-721, with the changes in R3 and R4.

R3. **Opaque and demoted nodes get precise hits.** In an iteration run an opaque
or demoted node is T1 when clause a, c, d or f holds for its chosen record, and
only otherwise falls to T2 `b-static`. Today clause b is definitive for such
nodes (planner lines 614-631, `continue` before clause d) and
`test_demoted_node_ignores_precise_hit_on_static_miss` pins that a precise hit
on a static miss is skipped. Running on positive evidence is always safe; the
gate audit and `--full` are unchanged.

R4. **Soft-fallback witness.** The caller-local fallback (planner lines
686-721) selects, in an iteration run, one witness per missing path instead of
the whole run or the whole static reach: the lexicographically smallest
recorded node of the lexicographically smallest test file among the files that
statically reach the path or whose nodes ran it; when that set is empty and the
run's ambient context imported the path, the smallest recorded node of the
run's smallest test file. The witness is T1 `w`; the rest of that set is T2 by
its own clause. An import that breaks shows at collection of that one file,
which is all the guard exists for. `--full` and the audit plan keep today's
behaviour (they run everything).

R5. **T2 is deferred.** In an iteration run T2 nodes are not in the argv files,
or are in the deselect binding when their file is in argv (partially selected,
or a conditional file, R44). No iteration run executes a T2 node, with one
widening exception: when `_bindable_deselect` (`selection_engine.py` lines
550-566) gives up an id (an unsafe id restores its whole file; over
`SELECTION_DESELECT_MAX_IDS` nothing is deselected), the T2 nodes it restores
run, count as run, and leave the deferred count. Deferred
nodes are counted, named by reason and reported (R30-R33). Their records are
not touched (`_store_nodes` writes only the nodes a run ran), and the paths
that made them T2 stay in `since_gate` (R9) until the next gate, so they stay
deferred on every later plan, whatever the diff, until a run executes them.

R6. **No size caps in dynamic iteration.** `_full_text` (`selection_engine.py`
lines 524-547) is deleted: `refine` is called only for iteration runs
(`cli.py` `_plan_and_refine`, line 3641), so neither `full_ratio` nor
`MAX_SELECTED` promotes a dynamic selection to the full suite any more. The
planner's `full_reason` (ambient data file) no longer promotes either: it
becomes T2 `g-data` for every node of the affected run, and the field is
removed from `SelectionDecision`. `full_ratio` and `MAX_SELECTED` keep applying
to the static engine (`impact.py` lines 587-595 and 677-685) exactly as today;
the README says so.

R7. Dropped (Decision 3). There is no iteration budget and no
`iteration_budget` key: every T2 test waits for the gate.

R8. **What still forces a full run in iteration.** Exactly these, all as today:
(1) a changed file that is a full trigger at the static stage
(`_is_trigger_basename`, `impact.py` lines 146-152 and 227-232:
`_TRIGGER_BASENAMES` `uv.lock`, `poetry.lock`, `Pipfile.lock`,
`pyproject.toml`, `setup.py`, `setup.cfg`, `pytest.ini`, `tox.ini`,
`conftest.py`, `package.json`, `package-lock.json`, `pnpm-lock.yaml`,
`yarn.lock`, `.ptest.toml`; the `_CONFIG_PREFIXES` `vitest.config.*`,
`vite.config.*`, `jest.config.*`; `requirements*.txt`, matched before the
`.txt` ignore, lines 451-458; and declared `full_triggers`, lines 437-440 and
456-458); lockfiles stay full triggers (Decision 5); (2) no usable records:
the static engine (`_NO_RECORDS`, `_DYNAMIC_FALSE`, `_STORE_DOWN`, Python below
3.12, recording inactive, no run whose `compatibility_fingerprint` equals the
current one (`_OTHER_CONFIG`), planner failure; `selection_engine.py` lines
131-147, 437-521) keeps today's static selection with its caps, since the first
run must record something and a full run records the most; (3) `--full`
itself; (4) `git changes are unavailable` (`impact.py` lines 416-422). Case (2)
is how a lockfile bump that is already on the base still costs a full run: the
first run after the bump in this store (shared by every worktree of the
repository) finds no run with the current fingerprint (`selection_engine.py`
lines 494-496), so the static engine runs, and in a hub tree its caps promote
it to the full suite. That run records under the new fingerprint, so later
runs in any worktree plan dynamically; a node whose only records are from the
old configuration (`_OTHER_CONFIG_REASON`, planner lines 575-577) is unusable
and its file follows 4.6, so it is T2 `u-static` when reached (R9), never a
full run.

R8a. **A forced full suite names its reason on the end line.** When an
iteration run executes the full suite for any R8 reason other than (3), the end
line's note (`format_end(note=...)`) is `full suite ({reason})`, where
`{reason}` is the text the start line already prints after `→ full suite: `
(`cli.py` `_impact_note` lines 3307-3309; for example `uv.lock is a full
trigger`, or `10 of 10 test files reaches full_ratio 0.7` for case (2)),
escaped through `render.terminal_text`. Example: `ptest: passed · 15012 tests ·
full suite (uv.lock is a full trigger) · 9.4 min`. JSON `selection.full_reason`
carries the same text (R34). No new reason code. Such a run executes in full
mode, so it is a gate for the store (Vocabulary): the R15 audit, the R27
marker and the R33 report apply to it exactly as to `ptest --full`, which is
today's behaviour (`_ingest_run` audits every full-mode execution). It also
publishes exactly what it publishes today: a green full-mode execution is
`full_gate_eligible` (`operations.py` line 4009, `plan.execution == "full"`),
so it may publish a full baseline and `already verified` proof, and
`_note_run` records its green point. It owes no hint: it deferred nothing and
recorded a gate. It never prints the coverage line of R29.

R9. **Deferral is measured since the gate.** The planner's diff-derived inputs
(`reach_b`, `reach_all`, `static_all`, `touch`, `nonpy_changed`; planner lines
464, 529-533, 725) are computed from `changed ∪ since_gate` instead of
`changed`, so clause b (`b-static`) and 4.6 (`u-static`) do not depend on
which reference the diff was taken against. `since_gate` is the set of
project paths (all files, not only `.py`) that differ from the gate marker
(R27), computed the way `lastgreen.consult` computes its set
(`src/ptest/lastgreen.py` lines 324-367): the names of
`monorepo.worktree_changed_files(top, gate_commit)` (committed, staged,
unstaged and untracked changes since the gate commit), plus every path that
was dirty at the gate whose keyed digest (R27) differs now; an oversize or
unreadable file on either side compares as changed, as the planner treats
`"?"` (`selection_planner.py` lines 78-81). This mirrors
`lastgreen.dirty_vs_fingerprint` except for the digest: lastgreen's plain
`_content_hash` returns None above 2 MiB and treats None == None as
unchanged (`lastgreen.py` lines 193-209, 283-286), which would hide a large
file that changed after the gate. `since_gate` then passes through exactly
the filter the changed set passes through before planning (`impact.py` lines
446-475): tool and build output (`_is_output`, `.coverage*`, `*.egg-info`),
ignored paths (`_is_ignored`: dot-paths, `.md`/`.rst`/`.txt`,
`_IGNORED_BASENAMES`, `no_tests`, `ignored_inputs`, `non_input_outputs`
including `BUILTIN_OUTPUTS` such as `graphify-out`) and paths outside the
project's code area are dropped, so untracked output never keeps a deferral
alive. A full-trigger basename in `since_gate` never forces a full run: only
the diff does (R8 (1)); a changed lockfile or config since the gate already
reaches the planner as "another configuration" (R8 (2)). When no usable
marker exists (none
recorded, the commit object does not exist in this repository, or the marker
is unreadable; ancestry is not required, because `worktree_changed_files`
diffs two trees, `monorepo.py` lines 584-590) `since_gate = ∅` and every line of the run says `no gate recorded`
(R29). New test files (added, never recorded) stay T1 `n` through `wf_all`
(planner lines 176-186, 775-776).

R10. **Empty diff still plans.** An iteration run whose changed set is empty
(`format_nothing_changed`, `format_no_green_changes`,
`format_no_changes_anywhere`, `format_no_changes_under`, the monorepo
`<child> · no changes` line) plans against records with `changed = ∅` and
`since_gate` (R6-R9). If the plan has T1 nodes (for example a sibling's
non-passing record is now the chosen one, or the K-trim removed this
worktree's own record), the run executes them like any selected run, with the
start line `{reference}: no changes → {T} tests in {F} of {M} files (dynamic ·
…)`. Otherwise nothing runs, as today, and the line follows R31. Planning is
the warm 1.5 s budget of the 0.5 spec.

R11. **`--full` unchanged.** A gate runs everything the full command runs today
(`operations.py` `_plan`, `_summary`). Tiers are computed only for the audit
(R15) and the report (R33). Start line, `already verified`, `verified
elsewhere`, join and the monorepo child skip are unchanged.

R12. **Vitest.** The tier split is out of scope for 0.7.0 (no per-test results,
no ptest-owned JavaScript import index, no selection store; the delegation is
transitive import reach). A Vitest changed run keeps `vitest run --changed
<sha>` and its start line `… → vitest --changed <label>`. Because ptest proves
nothing about the tests `--changed` leaves out and keeps no gate marker for
Vitest, its end line always ends with the bare `gate at spec end` (never `(no
gate recorded)`, never a deferred count), on every changed run including after
a Vitest `--full`. Its JSON `selection` block has `"engine": "vitest-changed"`,
`"run_now": null`, `"deferred": null` (R34). The guide text (R35) applies to
both runners.

### Part 2: observers learned at the gate

R13. **Declared observers.** `[selection] observers` is a TOML array of
project-relative literal path prefixes, default `[]`, validated by
`config._path_sequence` exactly like `full_triggers` (`config.py` lines
501-512: entries of at most 4096 characters, no leading `/`, no backslash, no
`~`, no drive letter, no empty, `.` or `..` segment, no control character).
Its entries count toward the 4096-entry budget that all `[selection]` path
fields share (`config.py` lines 515 and 527). Matching uses `selection._matches` (`selection.py` lines 19-21: a
test file path equal to the entry or under it); metacharacters are literal,
never a glob. Any node in a matching test file is an observer of every path
(`*`). A violating entry is `invalid-config` (exit 2), except an entry whose
path crosses a symlink, which `_path` rejects as `unsafe-path` (exit 2) as it
does for `full_triggers`. The field is
`repr=False` on `SelectionPolicy` (like `dynamic`, `contracts.py` lines
545-548).

R14. **Learned observers.** The store table `observers(nodeid, path,
learned_at, PRIMARY KEY(nodeid, path))` holds learned rows. A row `(N, p)`
means N noticed an import-time change of `p` it never executed. At most 64
rows per node; a node that would exceed 64 gets the single row `(N, "*")`
instead (observer of every path; this only widens, so it is not eviction). At
most 100000 rows per store; over that the oldest `learned_at` rows go first.
Learned rows are written, and cap eviction happens, inside the gate's
`store.update` `BEGIN IMMEDIATE` transaction (`_apply_update`), together with
the gate marker (R27); a rejected update writes neither. Eviction is reported,
never silent: the number of rows the store cap evicted in that transaction is
the gate's `observers_evicted` count, printed by the `-v` gate line (R33) and
carried as JSON `gate.observers_evicted` (R34). An evicted row only removes a
promotion; it never demotes a node.

R15. **Learning at the gate.** After a gate with a valid handoff, the self-audit
(`selection_engine.py` `_self_audit`) plans the pre-update snapshot with the
iteration rules (R1-R9, `changed = ∅` as today) and classifies every failing
node whose chosen record was passing: (a) T2 in that plan and `am ∪ pw` of its
chosen record's analysis non-empty → an observer row `(N, p)` for every `p` in
`analysis.am ∪ analysis.pw` (R14 cap), **no demotion**; (b) T2 and `am ∪ pw`
empty (a `b-static` opaque change, a `g-data` ambient data change, a
`u-static` file) → neither learned nor demoted, counted as `unlearned` in the
gate report (R33, R34); (c) not selected in any tier → a miss, demoted as
today (`store.demote`, planner `audit_misses`); (d) T1 → nothing (the
iteration run would have run it). The audit computes these outcomes; the
observer rows are written by the update transaction (R14); demotion keeps its
current write path. The audit line stays `ptest: selection audit: N failing
tests would not have been selected — …` for (c) only; (a) and (b) are reported
by R33.

R16. **Promotion.** In an iteration plan a node N with observer rows (declared
`*` or learned paths) is T1 `o` when, for its chosen record's analysis, a
learned path is in `am ∪ pw` or is absent from the current index (deleted), or
`*` is among its rows and `am ∪ pw` is non-empty. With a declared ambient
fingerprint the promotion is conditional instead (R44). Promotion only ever
adds tests: a node that is already T1 keeps its code.

R17. **Clearing.** Learned rows are removed only when the node id is absent from
the last two full inventories (the same pruning as `nodes`, `_prune_full`,
selection_store.py lines 1141-1171), by the R14 caps (reported), or by `ptest
uninstall`. No other command removes them, and no column beyond R14's is kept
for clearing. A row whose path no longer exists stays until its node is
pruned; it promotes its node when the deletion is in the node's stale set
(R16) and is otherwise inert. Declared observers live in the config and are
cleared by editing it. Decided (Decision 6): pruning only, no decay.

R18. Dropped (orchestrator decision 2026-10-19). `ptest doctor` and `doctor
--fix` get no observer or tier work in 0.7.0. The declared `observers` key
(R13) stays, because it is the only way to protect a known observer before its
first miss. `doctor --fix` must still preserve the new keys byte for byte
(negative contracts).

R19. **Status.** `ptest status` adds the observer count to the human selection
store line (`cli.py` `_selection_store_lines`); `status --json` is unchanged.

R20. **Observers are honest.** An observer that stays deferred (fingerprint
unchanged, R44) is counted as deferred like any T2 node. An observer is never
demoted by (a) of R15; a demoted node that fails after deferral stays demoted
and additionally becomes an observer.

### Part 3: records chosen by tree distance

R21. **K records per node.** The store keeps at most K = 3 records per node id,
one per run: `nodes` primary key becomes `(nodeid, run_id)` with an index on
`nodeid`. On update, after inserting the run's rows, every node that has more
than K rows loses its oldest rows by `runs.recorded_at` (never the row being
written) in the same `BEGIN IMMEDIATE` transaction (`_apply_update`, lines
1000-1014). K = 3 because one chain tip plus two sibling task worktrees is the
common parallel shape; K = 2 loses the tip record as soon as two siblings each
record a node. Decided (Decision 1): K is the fixed contract constant
`SELECTION_RECORDS_PER_NODE = 3`; there is no config key. Store changes this
forces:
- `_store_nodes` (lines 1108-1129) deletes a run's vanished rows with `DELETE
  FROM nodes WHERE nodeid = ? AND run_id = ?`, never by node id alone, and
  inserts with the `(nodeid, run_id)` key.
- `mark_outcomes` and `invalidate` (lines 1278-1302) update every row of the
  node id or test file, all K of them. This only turns records non-passing or
  unknown, so it only widens selection.
- `DependencySnapshot` (`contracts.py` line 1692) carries, per node id, the
  tuple of its records (at most K, each with its run id), plus the gate marker
  (R27), the learned observer rows (R14) and each run's ambient fingerprint
  (R43).

R22. **Chosen record.** For each node the planner chooses, among its records
whose run's `compatibility_fingerprint` equals the current one (today's check,
unchanged), the one with the smallest distance:
`|stale(run) ∩ (paths(F(N)) ∪ M(N) ∪ D(N) ∪ fixture paths ∪ ambient(run) ∪
{test_file})|`, where `stale(run)` is `_stale_sets` of that run; ties go to the
newest `recorded_at`, then the lexicographically smallest run id. All clauses
(R1-R4, R16) are evaluated against the chosen record only. A node whose chosen
record is non-passing is T1 `a`; an older non-passing record never overrides a
closer passing one, and a closer failing record is never hidden by a newer
passing one.

R23. **Memoisation stays bounded.** Analyses are still memoised per stale
signature (`SELECTION_MAX_SIGNATURES = 128`, planner lines 479-494). Runs past
the cap make their records unusable (`_CAP_REASON`) as today; with K records a
node falls back to its next usable record before becoming unrecorded.

R24. **Eviction order.** Under size pressure (`_evict_to_target`, lines
1223-1248) the order is: unreferenced parse cache (as 0.6.8), then surplus
records (rows of nodes that have at least two rows, oldest `recorded_at` first,
never a node's newest row, never the run being written), then runs holding the
fewest records, never trading a run with more records for a smaller newer one
(0.6.8). The hard-cap retry path (`_evict_oldest`) uses the same order. The
gate marker does not depend on any row (R27), so no row is protected for it.

R25. **Store caps.** Decided (Decision 2): `SELECTION_STORE_TARGET_BYTES`
becomes 96 MiB and `SELECTION_STORE_MAX_BYTES` 128 MiB (`contracts.py` lines
1337-1338), both contract constants with no config key. Estimate from 0.6.8:
14,328 persea records fit in under 27 MB next to a 21 MB cache under the 48 MiB
target, so about 1.9 KB per record; three records per node needs about 80 MB
for that project. `tests/ng/test_contracts.py` lines 2328-2329 and the stress
test `test_store_holds_20k_nodes_under_cap` change accordingly.

R26. **Last writer no longer wins.** Two worktrees recording the same node
produce two rows; neither replaces the other.
`test_same_node_last_writer_wins_sequentially` becomes
`test_same_node_keeps_one_row_per_run_up_to_k`.

R27. **Gate marker.** A complete gate ingest (valid handoff, complete
inventory, whatever the test outcomes) writes, in its update transaction, the
`kv` keys `last_gate_commit` (HEAD of the gate's checkout),
`last_gate_dirty` (JSON object: every path dirty at the gate → its digest) and
`last_gate_recorded_at`. The digest is `_digest_bounded(domain key, path,
_FILE_READ_CAP)` (`selection_engine.py` lines 211-248): an HMAC under the
domain key over the whole file, streamed, `"?"` above the 64 MiB cap and None
when unreadable; never lastgreen's plain sha256, because the store holds no
unkeyed digests (`selection_store.py` lines 8-10). `"?"` and None compare as
changed (R9). The dirty set is collected the way `lastgreen.record_pass`
collects it (lines 212-232). A path that was dirty at the gate and has since
been deleted digests to None, so it counts as changed and stays in
`since_gate` until the next gate; this is deliberately conservative. A gate whose dirty set exceeds `MAX_GREEN_FILES`
(1024, `lastgreen.py` line 31) clears the three keys instead, so the store
says `no gate recorded` rather than measure from a stale marker. A marker that
is missing, unreadable, or whose commit object does not exist in the current
repository is "no gate recorded" (R9); a 0.6.x writer that still holds a
connection opened before the migration cannot make it dangling, because the
marker names a commit, not a run. A v1 store migrated to v2 has no marker
until the next gate.

R28. **Workflow rule (guide).** After integrating task commits onto the chain,
the chain owner runs one bare `ptest` at the chain tip. It records the T1 tests
of the integration against the tip tree, so each later task worktree branched
from the tip finds a distance-0 record for them. It is an iteration run, not a
gate; it never clears deferred tests.

### Part 4: hint, guide and JSON changes

R29. **Hint strings.** `progress.NEXT_FULL` and `progress.NEXT_FULL_COV`
(`progress.py` lines 195-199) are removed. Two strings replace them:
`GATE_HINT = "gate at spec end"` and `GATE_HINT_NO_GATE = "gate at spec end
(no gate recorded)"`. For a pytest iteration run, `next_step` (lines 204-215)
returns `NEXT_FIX` on failure (unchanged); on any other status it returns
`GATE_HINT_NO_GATE` when no usable gate marker exists, else `GATE_HINT` when
the run deferred anything, else None. A static-engine iteration run (R8 case
(2) without promotion) cannot count deferrals and does not apply `since_gate`,
so it always gets the hint: `GATE_HINT_NO_GATE` when no marker is readable,
else `GATE_HINT`. The same rule gives the hint of every
no-run line (R31). Vitest changed runs follow R12. The coverage line for a
green full-mode run on a project that declares coverage without `--cov`
(`operations.py` lines 2538-2545) prints only for an explicit `ptest --full`
request (not an R8a forced run, whose request carries a `changed_note`), and
reads `coverage gate still owed: ptest --full --cov` instead of `next: ptest
--full --cov before handoff`. `reuse_refused_step` is unchanged.

R30. **Deferred segment.** When an iteration run deferred anything, the end
line's note (`format_end(note=...)`, `progress.py` lines 453-477) is
`deferred {N} ({reason})`, or `deferred {N} ({reason}, +{k} more)` when the
deferred tests span more than one T2 code, `{k}` being the number of other
codes. Numbers are plain integers like every other ptest count (Decision 9:
`deferred 14300`, no thousands separator, no rounding). `{reason}` renders the
T2 code with the most tests: `g-shared` → `shared import-time state via
{path}`, `g-reach` → `import-time reach via {path}`, `g-data` → `ambient data
{path}`, `b-static` → `static reach via {path}`, `u-static` → `unrecorded since
gate via {path}`. `{path}` is the changed project path with the most tests
under that code (for `g-shared`, the path of the module or function the
ambient context reached, qualname dropped), ties by path, escaped through
`render.terminal_text`. Example: `ptest: passed · 412 tests · deferred 14300
(shared import-time state via api/models.py) · 1.2 min · gate at spec end`.
With nothing deferred and a gate recorded the line is `ptest: passed · 412
tests · 1.2 min` with no hint. The `tests` count stays the executed count
(`format_counts`); deferred tests are never in it.

R31. **Lines without a run.** When an iteration run executes nothing:
- **Changed set non-empty** (`_impact_note`, `cli.py` lines 3301-3346, dynamic
  engine, `kind == "none"`): with T2 tests, the head is `{head} → nothing to
  run now · deferred {N} ({reason})`; with no T2 tests and `reached > 0`, it
  stays `{head} → no tests affected: R tests reach these changes and already
  passed on this code` (R1: `reached` holds no T2 node); with neither, `{head}
  → no tests affected`.
- **Changed set empty** (`no changes since last green run — nothing to test`,
  `no changes vs <label> — nothing to test`, `no changes — nothing to test`,
  `<child> · no changes`, `<project> · no changes under {scope} — nothing to
  test`): with T2 tests, `nothing to test` becomes `nothing to run now ·
  deferred {N} ({reason})` (for `<child> · no changes`, the segment ` ·
  deferred {N} ({reason})` is appended). `no changes — nothing to test` prints
  only when no child has T1 or T2 tests; otherwise each child prints its own
  line.
- Every such line ends with the R29 hint when one applies (` · gate at spec
  end` or ` · gate at spec end (no gate recorded)`). The suffixes `· ptest
  --full runs everything` and `· ptest --full {scope} runs all of them` are
  dropped.
- A no-run line writes no `--result-json` document, as today (the only writer
  is inside `operations.execute`, `operations.py` lines 1649-1657); its
  deferred count is on the line.
- Decided (Decision 10): the monorepo `total` line carries one summed count
  (`deferred {N} across {c} children`) and one hint; child lines keep their own
  counts.

R32. **`-v` detail.** `_detail_lines` (`selection_engine.py` lines 301-329)
adds, after the engine line: `tiers: {t1} run now ({codes}) · {t2} deferred
({codes})`; `deferred top: {file} {n} · …` (at most 20 files); `observers:
{d} declared · {l} learned · {p} promoted · {s} deferred`; `since gate: {k}
paths · gate {age} ago` or `since gate: no gate recorded`. The `fallback …`
lines stay.

R33. **Gate report.** After the audit of a gate the run prints `ptest:
selection gate: ran {n} previously deferred tests · {f} failed · {o} observers
learned` (every count rendered with `C.plural`, so `1 observer learned`) when
n > 0 or o > 0, with ` · {u} failed without a learnable path`
appended when u > 0, before the end line; with `-v` it prints `ptest: -v
selection gate: nothing was deferred` otherwise. With `-v` and `e > 0` it also
prints `ptest: -v selection gate: {e} observer rows evicted (store cap
100000)`. `n` counts the T2 nodes of the R15 audit plan that are in the gate's
inventory; `f` those among them that failed or errored; `o` the tests (node
ids) that gained at least one observer row (R15 a); `u` the R15 (b) tests;
`e` the rows evicted by the R14 store cap.

R34. **Result JSON.** `RunResult` gains `selection: SelectionReport | None`
and `serialize_run_result` (`src/ptest/contracts.py` lines 1954-1987) emits it
as `data.selection` of the `--result-json` document. Whether a document exists
depends only on whether `operations.execute` ran: every executed run writes
one, including the R44 conditional run that launches no runner; a no-run line
(R31) writes none, so a pipeline gets no JSON for the most common all-deferred
case and reads the count from the line. A no-run line says `nothing to run
now · deferred {N} ({reason})`; the R44 skipped launch ends `no tests needed ·
deferred {N} ({reason})`. A `--result-json` path holding a file from an
earlier run is left as it is by a no-run line, so a pipeline must not read a
stale document as this run's.

```
"selection": {
  "engine": "dynamic" | "static" | "vitest-changed" | null,
  "iteration": true | false,
  "run_now": 412 | null,
  "deferred": 14300 | null,
  "full_reason": "uv.lock is a full trigger" | null,
  "deferred_reasons": [{"code": "g-shared", "tests": 14100, "paths": ["api/models.py"]}],
  "deferred_files": [{"file": "tests/api/test_users.py", "tests": 120}],
  "deferred_files_truncated": false,
  "observers": {"promoted": 0, "deferred": 3,
                "fingerprint": "unchanged" | "changed" | "absent" | null},
  "gate": null | {"previously_deferred": 14300, "failed": 3,
                  "observers_learned": 1, "observers_evicted": 0,
                  "unlearned": 0, "demoted": 0}
}
```

- `run_now` and `deferred` are the T1 and T2 test counts. `deferred_reasons`
  holds the top reasons by test count, at most 5 entries with at most 3 paths
  each. `deferred_files` holds the at most 50 files with the most deferred
  tests (ties by path), sorted by path, with `deferred_files_truncated` true
  when more exist. `observers` carries only scalars (R16, R20, R45). `gate`
  carries only counts (R33). There are no per-test lists, no gate-marker
  object and no budget. Executed counts stay in `data.counts`.
- Per run kind: a dynamic iteration run has `"engine": "dynamic"`,
  `"iteration": true`, both counts, `"full_reason": null`, `"gate": null`. A
  static-engine iteration run (R8 case 2 without promotion) has `"engine":
  "static"`, `"run_now": null`, `"deferred": null`, empty lists. An R8a forced
  run has `"iteration": true`, `"engine"` `"static"` for case (2) or `null`
  for a trigger, `"run_now": null`, `"deferred": 0`, `"full_reason"` set and
  `"gate"` filled. A `ptest --full` run has `"iteration": false`, `"engine":
  null`, `"run_now": null`, `"deferred": 0`, `"gate"` filled. A Vitest changed
  run follows R12. A literal file scope has `"selection": null`.
- `docs/schemas/v1/run.json` is regenerated with `scripts/export-schemas.py`.
  **Persistence:** history keeps the frozen 0.4.10 key set:
  `history._insert_summary` drops `selection` (a persistence allowlist next to
  `_PERSISTED_REASON_ALIASES`, `src/ptest/history.py` lines 1944-1968), so
  `tests/ng/test_selection_history_compat.py` still holds and older ptest
  reading the history store sees nothing new. No new result reason code:
  deferred information never travels in `reasons` or `limitations`. R44 reuses
  the existing result code `no-tests-needed` (`history.py` lines 1912-1913);
  only its membership in `ATTEMPT_DECISION_STOP_REASONS` is new.

R35. **Guide and product strings.** `docs/ptest-agent.md` and
`src/ptest/resources/repository-agent-guide.md` stay byte-identical (`cmp` is
clean today) and `src/ptest/resources/agent-guide.md` says the same in its
words. Changes: the loop table row "Before handoff or merge" becomes "When the
whole spec or chain is finished (the orchestrator, once)" with the same
command; the sentence "only `ptest --full --cov` completes the change" becomes
"only the spec's one `ptest --full --cov` gate completes it; it runs once, at
the end of the chain, by the orchestrator, never per task or handoff"; a new
paragraph explains `deferred N (…) · gate at spec end` ("ptest proved nothing
about those N tests; they stay deferred until the gate runs them; never run
`ptest --full` during a task to clear a deferred count"); R28's seeding rule;
the output table replaces the `next: ptest --full …` row with rows for
`deferred …`, `nothing to run now`, `gate at spec end` / `(no gate recorded)`,
`full suite (…)` (R8a: "this iteration run had to run everything; nothing to
do"), `no tests needed · deferred …` (R44), `coverage gate still owed: …`, `selection gate: …` and `-v fingerprint:
…`; exit-code row 0 reads "Done; the spec's one `--full --cov` gate completes
it"; and a note that a no-run line writes no `--result-json` and leaves an
existing file at that path untouched. Every other product string that ties
`--full` to handoff says "once at the end of the spec or chain" instead:
`help.py` line 21, `cli.py` line 2498, `README.md` lines 30, 120, 122 ("run
`ptest --full`" before merging), 175, 192 ("the handoff gate") and 389, and
the current skill text `_provider_text` in `agent_rules.py` (lines 321-349)
only. The `_pre_…_provider_text` functions (lines 227-315) are the exact bytes
of previously shipped skills, which `_provider_target` recognises by exact
match (lines 388-402); they are never edited, or `ptest init` would raise
`already-exists` in every repository holding an older skill. Instead the
0.6.8 bytes of `_provider_text` become a new `_pre_one_gate_provider_text`,
added to that recognition tuple, so `ptest init` upgrades a 0.6.8 skill in
place. `agent_rules._PREVIOUS_GUIDE_SHA256S`
(`src/ptest/agent_rules.py` line 970) gets the sha256 of the guide bytes
shipped immediately before 0.7.0 (the working-tree guide that carries the
stacked-worktree `--base` sentence, once it is committed), so
`test_every_shipped_guide_version_hashes_into_previous_set`
(`tests/ng/test_agent_rules.py:1001`) passes and `ptest init` upgrades
repositories in place.

R36. Dropped (Decision 5). `compatibility_fingerprint`, the lockfile full
triggers and `[setup]` staleness are unchanged; a lockfile bump costs a full
run as today and says so (R8, R8a). The split is out of scope.

R37. **Explicit `--base` keeps skipping last-green.** Decided (Decision 4):
unchanged (`cli.py` `_consult_reference`, `lastgreen.consult` with
`explicit_base`). T1 is record-based, and since R9 deferral is measured from
the gate marker, so the reference the diff is taken against changes neither
the T1 set nor the deferred count; consulting the cache would only change what
`--base REF` means. The guide's stacked-worktree sentence stays valid.

R38. **Pass recording.** `_note_run` (`cli.py` lines 3576-3610) keeps recording
a green point for an iteration pass. Deferred tests are tracked by their stale
records and `since_gate` (R9), never by the green point, so moving the green
point never hides a deferred test.

R39. **Monorepo.** Each child plans and reports independently (R30-R31, R8a),
with its own store and gate marker; the total line sums deferred counts and
prints one hint (R31, Decision 10). A Vitest child reports per R12.

R40. **Literal scopes.** `ptest <file>` and `ptest --full <folder>` are
unchanged. `ptest <folder>` (changed under a folder) applies R1-R10 to the
folder's part of the plan: deferred nodes under the folder are counted; nodes
outside it are neither run nor counted (the line names the folder, as today).

### Part 5: declared ambient fingerprint

R41. **Config.** Decided (Decision 7): `[selection] ambient_fingerprint` is a
TOML array of argv tokens (not a shell string: literal argv is a product
rule), validated by `config._argv(allow_empty=False)` exactly like `[setup]
argv` (`config.py` lines 30-35 and 332-337: 1 to 256 tokens, each at most
16384 characters with no control character, at most 131072 bytes in total).
The timeout is the contract constant `AMBIENT_FINGERPRINT_TIMEOUT_S = 60`;
there is no timeout key. A violation is `invalid-config` (exit 2). The field is
`repr=False` on `SelectionPolicy`. The config writer renders it only when set.

R42. **Trust and execution.** The command is committed project code with the
same trust as `[runner] launcher`/`args` and `[setup] argv`. It runs once per
pytest run with recording enabled (`selection_engine.recording_enabled`),
including gates (Decision 7, so the gate records the fingerprint the next
iteration compares against), in the controller: inside `operations.execute`'s
`decide_attempt`, after `_finish_setup` and before `_capture_source`
(`operations.py` lines 3601-3612). This placement does not make side effects
harmless: `input_before` is captured before the guard launches (line 3582)
and compared with the post-decision snapshot (lines 3624-3638), so a command
that writes an input file invalidates the run with `changed-during-run`
(exit 70), fail-closed. The only tolerance is today's: when setup ran and every
changed path is ignored-class; `__pycache__` is never an input
(`source.py` lines 753-761). The guide requires the command to be read-only.
The guard calls `decide_attempt` once per
attempt (`_run_guard`, line 1493, `decision_sent`); the first attempt computes
the fingerprint and later attempts of the same run reuse it. Execution: cwd =
project root, no shell, the run's environment (no credentials added), stdin
`/dev/null`, stdout captured up to 16 MiB, stderr up to 64 KiB (shown only
with `-v`), in its own session (`start_new_session=True`), killed with
`os.killpg(…, SIGKILL)` on timeout, or when the controller's signal state
records a cancellation while it runs, the same spawn-and-kill pattern ptest
uses for its guard process (`operations.py` lines 1415-1425, 1450-1458).
ptest does not sandbox it and cannot enforce "no network"; the guide says the
command must be hermetic and read-only, and the end line never claims
otherwise. Planning stays pure (0.5 N11): `refine`, `ptest plan`, previews,
`status` and `doctor` never run it.

R42a. **Decision budget.** The guard waits `DEFAULT_ATTEMPT_DECISION_TIMEOUT_S
= 30` seconds for an attempt decision (`contracts.py` line 126; watchdog
`guard.py` lines 282-311, failing with `attempt-decision-timeout`), and the
controller checks the same bound on `attempt-ready` (`operations.py` lines
1086-1087, 1235-1236). The guard manifest gains `attempt_decision_timeout_s`,
set by the controller to `DEFAULT_ATTEMPT_DECISION_TIMEOUT_S` when no
`ambient_fingerprint` is declared (byte-identical behaviour) and to
`DEFAULT_ATTEMPT_DECISION_TIMEOUT_S + AMBIENT_FINGERPRINT_TIMEOUT_S` (90 s)
when it is; the guard watchdog and both controller checks use the manifest
value, and the compound deadline still caps it (`min(…, compound_deadline)`).
A fingerprint command that runs up to its own 60 s timeout therefore never
turns into a protocol failure. The guard runs from the same installed package
as the controller (it is spawned as `sys.executable -c _GUARD_SCRIPT`,
`operations.py` lines 1415-1419), so no mixed-version pair can see the new
field and `GUARD_PROTOCOL_VERSION` (`contracts.py` line 33) stays 2; the
manifest round-trip tests gain the field. Decoder change, following the
`stall_timeout_s` precedent (commit 11b759f, `contracts.py` lines 2713 and
2750-2751): `decode_launch_manifest` adds `attempt_decision_timeout_s` to its
key allowlist (`contracts.py` lines 4627-4632) and reads it with `obj.get`,
defaulting to 30 when absent; `LaunchManifest.__post_init__` bounds it to
1..90 seconds. A guard-side fingerprint step was
considered and rejected: the guard would have to carry the domain key and send
the digest back over the control channel, a larger protocol change than one
manifest field.

R43. **Digest and record.** The fingerprint is `HMAC(domain key, stdout)`
(never an unkeyed digest, never the stdout). It is stored on the run row
(`runs.fingerprint`, nullable) by the run's update transaction. Nonzero exit,
timeout, spawn failure or output over the cap leave the current fingerprint
absent, and absent on either side compares as **changed**. A failing or slow
command never changes the run's exit status, counts or narration beyond the
`-v` line of R45. A command that writes a project input file is the one
exception, and it fails closed: the run ends `incomplete` with
`changed-during-run` (exit 70, R42), because the guide requires a read-only
command.

R44. **Conditional promotion.** With a declared fingerprint, an R16 promotion
is conditional: the plan carries `RunRequest.conditional: tuple[(nodeid,
expected_fingerprint_or_None)]` (never persisted, scoped mode only, like
`deselect`), where the expected value is the fingerprint of the node's chosen
record's run. Routing and binding:
- The CLI routes every plan with conditional entries through
  `operations.execute`, even when it has no T1 node (today `kind == "none"`
  returns before execute, `selection_engine.py` lines 583-588, `cli.py` lines
  3696-3711).
- The conditional nodes' test files go into argv up front (before
  `adapter.prepare`, `operations.py` lines 3482-3485). The conditional node
  ids and every T2 node of those files go into the deselect binding written by
  `_selection_prepare_run` (lines 3570-3574). A conditional node whose id
  cannot be bound (`_bindable_deselect` restores it) is promoted
  unconditionally: it runs, never drops (R5 counts such restored nodes as
  run).
- In `decide_attempt`, after the fingerprint (R42), every conditional node
  whose expected value is absent or differs from the current one is promoted:
  the binding is rewritten to drop exactly the promoted ids, and nothing else.
  The rewrite writes a temporary file in the binding's directory with mode
  0600, named `<report>.deselect.tmp` (`selection_ingest.cleanup`, lines 710-724, is
  extended to remove a leftover one), unlinks it if the rename fails, and renames it over the binding
  path, so the bridge's checks (owner,
  mode, a single link, size, format and run id; `runtime/pytest_bridge.py`
  lines 982-1045) still hold. T2 neighbours stay deselected, so R5 holds.
- When nothing is left to run (no T1 node and nothing promoted),
  `decide_attempt` returns a stop decision with the existing reason code
  `no-tests-needed`, newly added to `ATTEMPT_DECISION_STOP_REASONS`
  (`contracts.py` lines 213-217). The guard handles it like every stop: it
  closes spawning and drains normally (`guard.py` lines 316-320). The
  controller, which today turns every stop into `incomplete` through
  `stopped_at_gate` (`operations.py` lines 3729 and 3751), classifies a stopped
  handoff whose reason is `no-tests-needed` as a completed policy decision
  instead: status `NO_TESTS_NEEDED` (the status the advanced no-test path
  already uses, lines 3205-3225), runner code 0, exit 0, no test executed.
  It writes nothing to the selection store: `execute` skips
  `_selection_after_run` (`operations.py` lines 4106-4120) when the stop
  reason is `no-tests-needed`. Today that call would treat the unlaunched run
  as an invalid handoff (`selection_ingest.py` lines 60-65 and 506-518,
  `_empty_run` with `complete=False`) and call `_fallbacks(…,
  invalidate=True)` (`selection_engine.py` lines 800-805, 873-878), marking
  every record of the argv files, which are exactly the conditional files
  (`operations.py` lines 395-406), `unknown`; that is non-passing
  (`contracts.py` line 1362), so the next plan would run the observer and its
  neighbours as T1 `a` with nothing changed. Skipping the call is the simpler
  fix: no record, outcome, fingerprint or marker is written, and repeating the
  same command at once ends `no tests needed` again. Its end line is `ptest: no tests needed · deferred {N} ({reason})
  · {duration}` followed by the R29 hint (` · gate at spec end`, or ` · gate
  at spec end (no gate recorded)` when no usable marker exists), and its
  result document carries the selection block. Any other stop reason keeps
  today's `incomplete`.
- When the fingerprint is unchanged the conditional nodes stay deferred (T2,
  counted and reported). Non-observer clause-g nodes are T2 either way.
  Without a declared fingerprint R16 applies at plan time.

R45. **Narration.** With `-v` the run prints `ptest: -v fingerprint: unchanged
· {k observers} kept deferred`, `changed · {k observers} promoted`, or `absent
({reason}) · treated as changed · {k observers} promoted`, where `{k
observers}` is `C.plural(k, "observer")` (`1 observer`, `2 observers`). The end line's
deferred note and the JSON `observers.fingerprint` field (R34) carry the
outcome without `-v`.

R46. **Compatibility.** The invariant is: the same `.ptest.toml` bytes give the
same policy digest and `compatibility_fingerprint` as 0.6.8. Declaring or
changing `observers` or `ambient_fingerprint` edits `.ptest.toml`, which is
both a static full trigger (R8) and an input of `compatibility_fingerprint`
(`selection_engine.py` lines 204-206, 269-273). The cost is the same as a
lockfile bump (R8): every iteration run whose diff contains the `.ptest.toml`
change is an R8 (1) forced full run (R8a), so a task that commits the change
pays a full run on each `ptest --base <parent>` check until the change is on
its base; a run measured against the green point (bare `ptest`) pays it once
if that forced full run is green (a failing one moves no green point, so the
next run is forced again).
The orchestrator should therefore land such a config change on the chain
before tasks branch from it. No separate command digest is kept: a changed
command is a changed config, and old fingerprints are never compared across
it.

R47. Dropped (orchestrator decision 2026-10-19, with R18). Doctor never
suggests or writes a fingerprint command.

R48. **Isolation.** The command's stdout, stderr and exit code are never
stored; only the keyed digest and the `-v` line (stderr tail escaped through
`render.terminal_text`, at most 10 lines). The command runs once per run, never
per worker.

## Negative contracts (abuse cases)

Identity and authorization have no new surface: a local CLI, one account, the
same per-project 0700/0600 store. Tenancy is covered under state. Each row is
one test, observed failing first.

| Axis | Must not happen | Test |
|---|---|---|
| State | two worktrees recording the same node leave more than K rows, or lose a row that neither wrote | `test_store_concurrent_updates_keep_at_most_k_rows_per_node` |
| State | a run's vanished-row delete removes another run's row of the same node | `test_store_nodes_delete_keys_on_run_id` |
| State | `mark_outcomes` or `invalidate` leaves one of a node's K rows passing | `test_outcome_updates_cover_all_k_rows` |
| State | a sibling worktree's hub edit defers or selects tests in another worktree whose own tree is at distance 0 for those nodes | `test_sibling_record_never_chosen_over_distance_zero_record` |
| State | a cancelled iteration run writes records, observers, a fingerprint or a gate marker | `test_cancelled_run_writes_no_tier_state` |
| State | an invalid or incomplete handoff, or a rejected store update, writes observers, a fingerprint or a gate marker | `test_rejected_or_incomplete_ingest_writes_no_observers_or_marker` |
| State | a run whose tests failed is not ingested (it must be, with failed outcomes) | `test_failed_tests_still_record_with_failed_outcome` (regression of the corrected fact) |
| State | eviction drops a node's newest row or the run being written | `test_eviction_never_drops_newest_row_or_written_run` |
| State | a missing, unreadable or unreachable-commit gate marker, or a gate with more than 1024 dirty files, is used to measure `since_gate` instead of reporting `no gate recorded` | `test_unusable_gate_marker_means_no_gate_recorded` (table) |
| State | a store with corrupt or unresolvable record ids leaves a node unaffected; it must be counted T2, never unaffected | `test_corrupt_ids_become_t2_never_unaffected` |
| State | a 0.6.x ptest opens a v2 store and reads, writes or deletes it | `test_v1_reader_refuses_v2_store_without_touching_it` (unit: monkeypatched `_SCHEMA_VERSION`, bytes compared before and after) |
| State | a 0.6.x ptest opening a v2 store larger than its 64 MiB cap removes, rewrites or records into it (it must get `capacity-exceeded`, plan static, record nothing) | `test_v1_reader_leaves_large_v2_store_untouched` |
| State | migration v1→v2 loses a record, changes a record's run or digest, or runs twice | `test_v1_store_migrates_in_place_idempotently` |
| State | an observer row evicted by the R14 store cap is not counted in the `-v` gate line and JSON `gate.observers_evicted`, or its eviction demotes a node | `test_observer_cap_eviction_is_reported_not_silent` |
| State | `doctor --fix`, editing a `[selection]` table that holds multi-line `observers` and `ambient_fingerprint` arrays, changes those lines or their parsed values | extend `test_fix_preserves_unmanaged_settings_byte_for_byte` (`tests/ng/test_doctor_fix.py:761`) |
| Input | an observer prefix with `..`, a leading `/`, a backslash, `~`, a control character, 4097 characters, or a 4097th entry is accepted | `test_observer_prefixes_reject_hostile_values` (table) |
| Input | an observer entry is matched as a glob (`tests/test_*` matches `tests/test_a.py`) or matches a path outside the project | `test_observer_prefixes_match_literally` |
| Input | `ambient_fingerprint` as a string, an empty array, a control character, a 257th token, a 16385-character token or more than 131072 bytes is accepted; an `ambient_fingerprint_timeout` key is accepted | `test_ambient_fingerprint_rejects_hostile_config` (table) |
| Input | a fingerprint command printing 17 MiB, binary bytes, hanging past 60 s, exiting nonzero, or absent from PATH fails the run, changes its exit code, or compares as unchanged | `test_fingerprint_failures_are_changed_never_fatal` (table) |
| Input | a hanging fingerprint command, or one running when SIGINT arrives, outlives the run or escapes its session | `test_fingerprint_timeout_and_sigint_kill_session` (table: timeout, SIGINT; SIGINT ends the run cancelled, exit 130, nothing ingested) |
| Input | the fingerprint command runs during `refine`, `ptest plan`, a preview, `ptest status` or `ptest doctor` | `test_fingerprint_never_runs_outside_execute` (table) |
| Input | a conditional node whose id cannot be bound is dropped instead of run | `test_unbindable_conditional_node_runs` |
| Input | a fingerprint command taking 45 s (under its 60 s timeout) changes the run's exit status or ends it with `attempt-decision-timeout`; or, without `ambient_fingerprint`, the manifest's `attempt_decision_timeout_s` differs from 30 | `test_slow_fingerprint_fits_decision_budget` (table; fake clock or scaled constants, never a 45 s wall-clock wait) |
| Input | the rewritten deselect binding is not mode 0600, has more than one link, or is observable half-written by the bridge | `test_conditional_binding_rewrite_keeps_bridge_checks` |
| Correctness | a stopped handoff with reason `no-tests-needed` ends `incomplete`, or any other stop reason ends `no-tests-needed` | `test_no_tests_needed_stop_is_completed_decision` (table) |
| State | a `no-tests-needed` run changes the selection store (invalidates the conditional files' records, writes an outcome, fingerprint or marker), so that an immediate repeat of the same command runs the observer or its neighbours | `test_skipped_launch_leaves_store_untouched` (store bytes compared; repeat ends `no tests needed`, `ran.txt` absent) |
| Input | a fingerprint command that writes a tracked input file lets the run pass instead of ending `incomplete` with `changed-during-run` (exit 70) | `test_writing_fingerprint_command_fails_closed` |
| Input | the launch manifest without `attempt_decision_timeout_s` is rejected, or one with 0, 91 or a non-number is accepted | `test_manifest_attempt_decision_timeout_optional_and_bounded` (table) |
| Correctness | T2 nodes restored by `_bindable_deselect` (unsafe id, id cap) are counted as deferred although they ran | `test_restored_deselect_nodes_count_as_run` |
| State | a gate-marker dirty digest is a plain sha256, or a dirty file changed after the gate stays out of `since_gate` because it is over 2 MiB, over the 64 MiB cap, or unreadable | `test_gate_dirty_digest_is_keyed_and_oversize_counts_changed` (table) |
| Regression | `ptest init` raises `already-exists` for a repository holding any previously shipped skill, including the 0.6.8 one, or edits a `_pre_…_provider_text` body | `test_init_upgrades_every_shipped_skill_version_in_place` (walks every recognised previous skill, like `test_every_shipped_guide_version_hashes_into_previous_set`) |
| Exposure | the end line, `-v` or JSON print fingerprint stdout, an unkeyed digest, or a path outside the checkout | `test_fingerprint_output_never_printed_or_stored` |
| Exposure | `deferred_reasons` (5 entries, 3 paths each) or `deferred_files` (50) exceed their caps or omit the truncation flag, or terminal lines carry unescaped control characters | `test_json_caps_and_terminal_escaping` |
| Exposure | the history store gains a key outside the frozen 0.4.10 set | `test_selection_block_never_persisted` (extends `test_dynamic_scoped_full_and_static_results_decode_frozen`) |
| Correctness | a deferred test is counted in `tests`, `passed` or `Counts` | `test_deferred_tests_never_counted_as_passed` |
| Correctness | an iteration run with deferred tests prints no deferred note or no gate hint | `test_deferred_always_reported_on_end_line` |
| Correctness | a no-run line says `no tests affected` or `already passed on this code` while T2 tests exist, or counts a T2 node in `reached` | `test_no_run_line_never_calls_deferred_unaffected` |
| Correctness | a deferral disappears after an iteration green moves the green point and the next diff is empty (an opaque node reached by a data file, an unrecorded file) | `test_deferral_survives_green_point_move` (table: `b-static`, `u-static`) |
| Correctness | an empty-diff plan that has T1 nodes runs nothing | `test_empty_diff_runs_t1_nodes` |
| Correctness | a conditional run executes a T2 neighbour of a promoted observer, or launches the runner when nothing is promoted and nothing is T1 | `test_conditional_binding_runs_only_promoted_ids` |
| Correctness | a narrowed iteration green (not an R8a full-mode execution) publishes a full baseline, `already verified` proof or last-green full proof (0.5 N5) | `test_iteration_green_publishes_no_full_proof` |
| Correctness | a gate skips a deferred test, or `already verified` fires while deferred tests exist on a tree the baseline did not run | `test_gate_runs_every_deferred_test` |
| Correctness | a failing previously deferred node with a learnable path is demoted instead of learned; one without (`b-static`, `g-data`, `u-static`) is learned, demoted or left out of `unlearned`; `observers_learned` counts rows instead of tests | `test_deferred_failure_learns_or_counts_unlearned` (table) |
| Correctness | `full_ratio`, `MAX_SELECTED` or an ambient data change promote a dynamic iteration run to the full suite (300 selected files, 90% of recorded tests, an ambient data change) | `test_iteration_never_promoted_by_removed_caps` (table) |
| Correctness | a precise hit on an opaque or demoted node is skipped in iteration | `test_opaque_and_demoted_precise_hit_is_t1` |
| Correctness | the soft-fallback witness selects more than one node per missing path, or none when a witness exists | `test_soft_fallback_witness_is_exactly_one` |
| Correctness | an iteration run forced to the full suite ends without `full suite ({reason})` on its end line or with a null `selection.full_reason`, or prints `coverage gate still owed` | `test_forced_full_suite_names_reason_on_end_line` (table: trigger in diff, bump on base, project with `coverage_args`) |
| Correctness | `--full` stderr differs from 0.6.8 on a tree with nothing deferred and no observers learned, apart from the R29 coverage-line wording | `test_full_run_output_byte_identical_when_nothing_deferred` (golden stderr, version line normalised) |
| Correctness | a Vitest changed run claims a deferred count, or prints `(no gate recorded)` | `test_vitest_changed_reports_engine_and_bare_gate_hint` |
| Correctness | an empty-diff line hides deferred tests | `test_empty_diff_still_reports_deferred` |
| Regression | the same `.ptest.toml` bytes give a policy digest or `compatibility_fingerprint` different from 0.6.8; a config without the new keys changes tier counts, argv or deselect binding on the same records | `test_policy_digest_and_compatibility_unchanged_for_same_config` |

## Smoke tests

Unit tests have missed bugs that only real projects exposed: xdist workers,
pytest temporary-directory cleanup, routers loaded through `importlib`, state
built lazily per process, eviction falling back to static. So the release
requires end-to-end smoke tests through the real ptest CLI as a subprocess
(`tests/ng/support.py` `CaseFactory.invoke`, line 530, the pattern of
`tests/ng/test_selection_e2e.py`), against committed hub-shaped fixture repos,
in addition to the unit tests. They live in `tests/ng/test_tiered_smoke.py`
(serial) and `tests/ng/test_tiered_smoke_xdist.py`, run through ptest like
every other test, and carry explicit timeouts of at most 120 s per invocation.
`invoke` runs ptest in a child process, so no smoke row monkeypatches ptest;
store setup happens through SQLite on the fixture's own store before the run.

### Fixture: `tests/ng/fixtures/tiered/` (materialised from `.py.txt`, like `selection_project`)

```
app/__init__.py      imports app.settings, app.models, app.routes, app.loader (the hub)
app/settings.py      SETTINGS = {...}
app/models.py        SCHEMAS = []; def schema(cls): SCHEMAS.append(cls); return cls
                     @schema class User: name: str; email: str   (decorated → non-plain → am)
app/routes.py        ROUTES = {}; def route(path): registers; @route("/users") def users(): ...
app/loader.py        importlib.import_module("app.plugins.audit") at import (importlib-loaded router)
app/plugins/audit.py @route("/audit") def audit(): ...
app/manifest.py      def manifest(): {"routes": sorted(ROUTES), "schemas": {c.__name__: sorted(c.__annotations__) for c in SCHEMAS}}
                     (reads the registries only, never names User); `python -m app.manifest` prints it as JSON
app/svc/users.py     def list_users(): ...        (plain body code, not imported by the hub)
app/svc/orders.py    def total(items): ...        (plain body code, not imported by the hub)
app/cache.py         _ENGINE = None; def engine(): lazily built per process
tests/conftest.py    import app   (collection-time import of the hub: ambient context)
tests/test_users.py  2 tests calling app.svc.users.list_users
tests/test_orders.py 2 tests calling app.svc.orders.total
tests/test_cache.py  2 tests using app.cache.engine() and tmp_path
tests/test_snapshot.py  test_manifest_snapshot: manifest() == tests/data/manifest.json (the observer);
                     test_manifest_file_is_json: json.loads of the data file (non-observer neighbour)
tests/test_mixed.py  test_mixed_users calls list_users; test_mixed_hub only asserts app imports
tests/test_hub_1.py … tests/test_hub_5.py  2 tests each, importing app only
tests/data/manifest.json
uv.lock              a small committed file (a full trigger and a compatibility input)
.ptest.toml          pytest, launcher sys.executable, args ["-q", "-p", "no:xdist"], test_roots ["tests"], [selection] enabled = true
```

Twenty tests in ten files; every test appends its node id to `ran.txt`, an
untracked file at the project root that the changed-set filter drops (`.txt`,
R9), so it never enters a diff or `since_gate`.
`conftest.py` imports the hub at collection, so the recorder puts the hub's
modules in every run's ambient context (`ambient.modules`), and an `am` change
in `app/models.py` or a hub-imported module makes every node T2 `g-shared`
(planner lines 554-558; `g-shared` precedes `g-reach` in R1). `recorded`
fixture: init git, one green `ptest --full` (records everything, writes the
gate marker and the green point), delete `ran.txt`. Lines below abbreviate
`shared import-time state via app/models.py` as `SHARED(models)`.

| Id | Setup | Command | Expected line(s) / exit | Pass criteria |
|---|---|---|---|---|
| S1 body edit, partial file | `recorded`; edit `list_users` body | `ptest --changed` | `ptest: passed · 3 tests · <t>`, exit 0, no hint | `ran.txt` = the two users tests and `test_mixed_users`; `test_mixed_hub` absent (deselect binding); JSON `run_now == 3`, `deferred == 0`, `gate == null` |
| S2 import-time edit, no run | `recorded`; add field `age: int` to `User` | `ptest --changed` | the start line ends `app/models.py → nothing to run now · deferred 20 (SHARED(models)) · gate at spec end`, exit 0 | `ran.txt` absent; no result document (R31) |
| S3 gate learns an observer | after S2 | `ptest --full` | `ptest: selection gate: ran 20 previously deferred tests · 1 failed · 1 observer learned`, then `ptest: failed · 1 failed, 19 passed …` (exit 1) | `observers` row `(tests/test_snapshot.py::test_manifest_snapshot, app/models.py)`; no `selection audit:` line; JSON `gate.observers_learned == 1`, `gate.unlearned == 0`, `gate.demoted == 0` |
| S4 observer runs, neighbour deferred | after S3: update `manifest.json`, commit, `ptest --full` green; add field `phone: str` | `ptest --changed` | `ptest: failed · 1 failed, 0 passed · deferred 19 (SHARED(models)) · <t> (exit 1) · fix the code under test, then rerun ptest` | `ran.txt` = `test_manifest_snapshot` only (`test_manifest_file_is_json` deselected); JSON `observers.promoted == 1`, `run_now == 1`, `deferred == 19` |
| S5 sibling worktrees (P3) | `recorded`; worktree A adds a `User` field and edits `list_users` body, runs `ptest --base main` (runs 3, defers 17); worktree B edits `total` body | in B: `ptest --base main` | `ptest: passed · 2 tests · <t>`, exit 0, no hint | B's `ran.txt` = the two orders tests; JSON `deferred == 0`; the store holds two rows (the `recorded` run's and A's) for each users test and for `test_mixed_users`, and B chose the `recorded` row for them |
| S6a lockfile bump in the diff | `recorded`; on a branch commit a byte change to `uv.lock` | `ptest --base main` | start line `ptest: … changed vs main → full suite: uv.lock is a full trigger`; end line `ptest: passed · 20 tests · full suite (uv.lock is a full trigger) · <t>`, exit 0, no hint | 20 names in `ran.txt`; JSON `full_reason == "uv.lock is a full trigger"`, `deferred == 0`, `gate` filled; `last_gate_commit` is this branch's HEAD (R8a) |
| S6b lockfile bump already on the base | a fresh `recorded` fixture (own store): commit a byte change to `uv.lock` directly on `main`, run no ptest; a new worktree from `main` adds field `age: int` to `User` | `ptest --base main` | start line `… changed vs main → full suite: 10 of 10 test files reaches full_ratio 0.7`; end line `… full suite (10 of 10 test files reaches full_ratio 0.7) · <t>` | 20 names in `ran.txt`; JSON `engine == "static"`, `full_reason` equals the start-line reason; a repeat `ptest --base main` after a `total` body edit plans dynamically, runs only the two orders tests and prints no deferred note |
| S6c second worktree after the bump | after S6b, without merging it: a second worktree from `main` edits the `total` body | `ptest --base main` | `ptest: passed · 2 tests · deferred 18 (SHARED(models)) · <t> · gate at spec end`, exit 0; no `full suite` text | `ran.txt` = the two orders tests; JSON `engine == "dynamic"`, `full_reason == null`, `deferred == 18` (S6b's run is the only one with the new fingerprint; its tree has the `age` field this worktree lacks: honest deferral, not a full run) |
| S7 fingerprint unchanged | after S3: update `manifest.json`, add `ambient_fingerprint = [<python>, "-m", "app.manifest"]` (same interpreter), commit, `ptest --full` green (the one re-seed, R46), delete `ran.txt`; then add `@staticmethod def _norm(v): …` to `User` | `ptest -v --changed --result-json r.json` | `-v` line `fingerprint: unchanged · 1 observer kept deferred`; end line `ptest: no tests needed · deferred 20 (SHARED(models)) · <t> · gate at spec end`, exit 0 | the runner was not launched (stop reason `no-tests-needed`, R44); JSON `status == "no-tests-needed"`; `ran.txt` absent; JSON `observers.fingerprint == "unchanged"`, `observers.deferred == 1`, `run_now == 0`, `deferred == 20`; the store bytes are identical before and after, and repeating the same command at once ends `no tests needed` again with `ran.txt` still absent |
| S8 fingerprint changed | as S7 (re-seed, delete `ran.txt`), add field `phone: str` instead | `ptest -v --changed` | `fingerprint: changed · 1 observer promoted`; `ptest: failed · 1 failed, 0 passed · deferred 19 (SHARED(models)) …` (exit 1) | `ran.txt` = `test_manifest_snapshot` only (binding rewritten for exactly that id); JSON `observers.promoted == 1` |
| S9 fingerprint failing | as S7 but `ambient_fingerprint = [<python>, "-c", "import sys; sys.exit(3)"]` (committed, re-seeded, `ran.txt` deleted) | `ptest -v --changed` | `fingerprint: absent (exit 3) · treated as changed · 1 observer promoted`; `ptest: passed · 1 test · deferred 19 (SHARED(models)) · <t> · gate at spec end`, exit 0 | `ran.txt` = `test_manifest_snapshot`; JSON `observers.fingerprint == "absent"` |
| S11 importlib router and lazy state | `recorded`; change the decorator argument in `app/plugins/audit.py` to `@route("/audit/v2")`; edit the `engine()` body in `app/cache.py` | `ptest --changed` | `ptest: passed · 2 tests · deferred 18 (shared import-time state via app/plugins/audit.py) · <t> · gate at spec end` | `ran.txt` = the two cache tests; JSON `deferred_reasons[0].paths` contains `app/plugins/audit.py` |
| S13 cancelled run | `recorded`; S1 and S2 edits (3 T1, 17 deferred); SIGINT after the first test starts (the `ran.txt` sentinel) | `ptest --changed` | `cancelled` (exit 130) | store rows, observers, fingerprints and gate marker byte-identical to before; the next `ptest --changed` runs the 3 tests and reports `deferred 17` |
| S14 unknown schema | `recorded`; set `kv schema_version` to `"99"` in the fixture's store; S1 edit | `ptest --changed` | `… → 2 of 10 test files (static: dependency store unavailable · …)`; end line ends `· gate at spec end (no gate recorded)` | store bytes identical before and after |
| S15 empty diff | (a) `recorded`, no edit; (b) S1 and S2 edits, bare `ptest` (runs 3, defers 17, moves the green point), then bare `ptest` again | `ptest` | (a) `ptest: no changes since last green run — nothing to test`, no hint; (b) second run `ptest: no changes since last green run — nothing to run now · deferred 17 (SHARED(models)) · gate at spec end` | nothing runs in (a) or in (b)'s second run |
| S16 Vitest delegation | the `fake_exec_node` project of `tests/ng/test_vitest_full_reuse.py`, one committed edit, after one Vitest `ptest --full` | `ptest --changed --result-json r.json` | start line `… → vitest --changed <label>`; end line ends `· gate at spec end` (never `(no gate recorded)`) | the recorded node argv contains `--changed <sha>`; JSON `engine == "vitest-changed"`, `deferred == null` |
| S17 deferral since the gate (R9) | `recorded`; delete the `nodes` rows of `tests/test_orders.py` in the fixture's store; edit `total` and `list_users` bodies; bare `ptest` | bare `ptest` again | first run: `ptest: passed · 3 tests · deferred 2 (unrecorded since gate via app/svc/orders.py) · <t> · gate at spec end`; second run: `ptest: no changes since last green run — nothing to run now · deferred 2 (unrecorded since gate via app/svc/orders.py) · gate at spec end` | the orders tests never run; the green point moved after the first run, yet the second still reports them |

### xdist variant (`test_tiered_smoke_xdist.py`)

S1, S3, S4 and S8 repeated with `args = ["-q", "-n", "2"]` against the real
pytest-xdist of the test environment (the 3.8.0 the parallel twins use,
`tests/ng/test_pytest_parallel_subprocess.py`). Same expectations; the
deselect binding (S1, S4), its R44 rewrite (S8) and recording must hold under
two workers, and `ran.txt` writes must be the union across workers.

### Bounded real-repository step (requires the user's explicit approval first)

Decided (Decision 8): exactly one repository, the `api` child of the user's
fullon2 monorepo (`/home/ingmar/code/fullon2_integrated`, child `api` with its
own `.ptest.toml`). Persea and every other client repository are excluded until
the user says otherwise. Rules:

- One scratch worktree `~/worktrees/fullon2/cc-tiered-smoke/repo` on a scratch
  branch, own `.venv` (never shared), `.env` symlinked; never pushed, merged
  or kept after step 6.
- Isolation: `api/.ptest.toml` gets a campaign-private `project_id`
  (`scripts/selection_eval.py` `campaign_project_id` and
  `overlay_project_id`). `.ptest.toml` is a full trigger (R8), so the overlay
  is committed on the scratch branch as commit `B0` before anything runs, and
  every iteration run uses `--base B0`; the diff then holds only the edit
  under test.
- Every command runs from the scratch monorepo root with the child prefix
  (`docs/ptest-agent.md`, Monorepo), never from inside `api`.
- At most two `ptest --full api` runs in total, and the user approves each one
  separately before it starts. No batch, no campaign, no `--cov`.

Steps:

1. `ptest --full api` seed (approval 1); record the end line.
2. One central-module edit (a field on a model every router imports), then
   `ptest --base B0 api`: record the start and end lines, `-v` tier counts,
   `selection` JSON when a document is written, planning time and run time.
3. Revert step 2's edit; one body edit in a service function, then `ptest
   --base B0 api`: same records. Expected: only the tests that executed the
   function run, and a deferred count only with a named reason.
4. Restore step 2's edit on top of step 3's, then `ptest --base B0 api`: the
   deferred count is at least step 2's count minus the tests step 3 ran (R5:
   a deferred test stays deferred until a run executes it).
5. `ptest --full api` (approval 2): record the gate report (R33) and any
   learned observers.
6. Remove the scratch worktree and branch, and delete the campaign store
   (`scripts/selection_eval.py` `store_db_path`, owned by this step only).
   Notes go in the scratchpad, never in the repository.

Stop at the first anomaly and report it; do not investigate client code.

## Acceptance and verification

A1. Every R clause has a unit test and a negative twin (planner, store, config,
engine, progress, contracts); every negative-contract row has its named test,
observed failing before the implementation (secure-by-spec).
A2. The smoke suite (S1-S9 with S6a-S6c, S11, S13-S17; the numbers S10 and S12
are retired with R7) and the xdist variant pass through `ptest`.
A3. `scripts/selection_eval.py` gains a `--tiers` report (T1 and T2 counts and
deferred-then-failing per mutant) so that the existing mutant classes show how
many failures iteration would have deferred; report only, no threshold, no
campaign is run for the release (memory: no heavy eval campaigns).
A4. Planning latency on the 0.5 A3 project stays within the warm 1.5 s budget
with K = 3 records, the distance choice and `since_gate` (measured once,
quoted in the changelog).
A5. Store stress: 20k nodes with 3 records each stay under
`SELECTION_STORE_MAX_BYTES`; eviction order verified
(`test_store_holds_20k_nodes_under_cap` updated).
A6. One integrated `ptest --full --cov` on the release commit, green: the single
gate of this spec's chain.
A7. The bounded real-repository step was run with approval and its notes are
quoted in the changelog, or the changelog says it was not run and why.

## Rollout and compatibility

- **Version** 0.7.0: `pyproject.toml:7`, `src/ptest/contracts.py:30`,
  `tests/ng/test_contracts.py` lines 20 and 342, `tests/ng/test_acceptance.py:191`,
  the golden document hashes `DOCTOR_GOLDEN_SHA256` and
  `DOCTOR_ERROR_GOLDEN_SHA256` in `tests/ng/test_agent_assessment_contract.py`
  lines 39-44 (the version is part of the bytes), `uv.lock`, changelog entry
  under `## 0.7.0`.
- **Guide and strings**: R35; both repository guides byte-identical;
  `_PREVIOUS_GUIDE_SHA256S` extended; `ptest init` upgrades managed guides in
  place.
- **Store**: `_SCHEMA_VERSION = "2"`. Opening a v1 store migrates in place in
  one `BEGIN IMMEDIATE` transaction: rebuild `nodes` with the `(nodeid,
  run_id)` key, add `runs.fingerprint` (NULL for old rows), create
  `observers`, set the version. The gate marker keys are written by the next
  gate. Idempotent, never loses a row. A 0.6.x ptest opening a v2 store gets
  `coordinator-corrupt: selection store has an unknown schema`
  (`selection_store.py` lines 193-195), or `capacity-exceeded` for a v2 store
  over its 64 MiB cap (lines 322-323); `_open_store_reason` treats neither as
  damaged and never removes the store (`selection_engine.py` lines 109-128,
  `test_engine_keeps_store_with_unknown_schema`); that run plans static and
  records nothing. A 0.6.x ingest whose connection opened before the
  migration may still write rows in the v2 table; its `DELETE … WHERE nodeid =
  ?` can only remove records (widening), and the gate marker names a commit,
  so it cannot dangle (R27). No corruption.
- **Records survive the upgrade**: `SELECTION_PROTOCOL` stays
  `ptest-selection-v1` (the recorder and dependency file format are unchanged),
  and for unchanged `.ptest.toml` bytes the policy digest and
  `compatibility_fingerprint` are unchanged (R46), so pre-0.7.0 rows match as
  before. No re-recording is needed until a project adds a new key.
- **Config**: two new `[selection]` keys (`observers`, `ambient_fingerprint`).
  0.6.x rejects unknown `[selection]` keys (`config.py` `_selection`'s `_keys`
  call, lines 491-493), so a config carrying them fails there with
  `invalid-config`; the changelog says so (the `reuse` precedent, spec
  2026-10-12 R1). Adding either key costs one full re-seed (R46). A config
  without them behaves as in 0.6.8 except for R1-R12 (with R8a) and R29-R33,
  which apply to every project.
- **Guard protocol**: `GUARD_PROTOCOL_VERSION` stays 2 (R42a); the manifest
  gains `attempt_decision_timeout_s` and `ATTEMPT_DECISION_STOP_REASONS` gains
  `no-tests-needed` (R44). Guard and controller always come from one install.
- **JSON**: `run.json` schema regenerated; `data.selection` is nullable so
  consumers that ignore unknown keys are unaffected; history persistence
  unchanged (R34).
- **Tests that change their oracle because of this spec** (listed so nobody
  "fixes" them the other way; each is verified before it is changed):
  - selection: `test_effectful_change_selects_static_reach` (e2e, becomes
    deferral; also pins `full_ratio 0.7 (dynamic)` at line 218),
    `test_demoted_node_ignores_precise_hit_on_static_miss` (iteration twin per
    R3), `test_dynamic_full_on_recorded_ratio`, the file-ratio case at
    `tests/ng/test_selection_engine.py:555` and
    `test_dynamic_full_over_argv_limit` (R6, `_full_text` deleted),
    `test_ambient_data_change_sets_full_reason` and
    `test_is_selected_full_reason_selects_everything`
    (`tests/ng/test_selection_planner.py:972, 1607`; `g-data`);
  - store: `test_same_node_last_writer_wins_sequentially` (R26),
    `test_eviction_is_oldest_first_and_never_fails` and
    `test_eviction_removes_only_oldest_run` (R24 order),
    `test_store_holds_20k_nodes_under_cap` and the contract constants at
    `tests/ng/test_contracts.py:2328-2329` (R25);
  - no-run suffixes (R31): `tests/ng/test_changed_default.py` lines 223, 238,
    417, 482-489 and 739; `tests/ng/test_last_green.py:108`;
    `tests/ng/test_monorepo_changed.py:169`; `tests/ng/test_natural_loop.py:196`;
    `tests/ng/test_command_model.py:127`;
  - hints and handoff strings (R29, R35): `tests/ng/test_changed_default.py`
    lines 365, 469-470, 501-502, 521 and 604;
    `tests/ng/test_coverage_on_request.py:671-673`;
    `tests/ng/test_init_changed.py:121, 176`;
    `tests/ng/test_agent_rules.py:769, 805`;
    `tests/ng/test_doctor_fix.py:409`; `tests/ng/test_resources.py:72`;
  - skill text (R35): the provider tests at `tests/ng/test_agent_rules.py`
    lines 649-663, 1086 and 1205 that read `_previous_provider_text` keep
    their bytes; tests that pin the current `_provider_text` move to the new
    strings.
- After source changes run `graphify update .` in the changed checkout
  (AGENTS.md).

## Out of scope

- Vitest tiering (needs a ptest-owned JavaScript reverse-import index and
  per-test results; R12 gives Vitest the honesty contract only).
- `ptest plan --json` exposing the dynamic decision or tiers (it is a static
  preview today; the run document carries the tiers).
- A result document for no-run lines (R31).
- Observer decay, observer editing commands, cross-machine sharing of records
  or observers.
- Changing the recorder, the dependency file format or the deselect binding
  mechanism (R44 only rewrites the binding's contents).
- Making the full mode or `reuse` the default (separate 0.7.x item).
- A sandbox for the fingerprint command.
- Any iteration budget (R7 dropped): T2 tests never run before the gate.
- Doctor work for observers or the fingerprint (R18, R47 dropped).
- Splitting `compatibility_fingerprint` into identity and environment so that a
  lockfile bump defers instead of forcing a full run (the dropped R36,
  Decision 5). A lockfile bump keeps today's behaviour and names its reason
  (R8a). Revisit in 0.7.x if the real-repository step shows it matters.

## Decisions (2026-10-19)

The user delegated the open questions with the rule "KISS and least technical
debt": no new knobs, no new concepts unless the pattern needs them.

1. **Records per node:** K = 3 is a fixed contract constant. No config key.
2. **Store caps:** raise to 96/128 MiB as contract constants (R25). No config key.
3. **`iteration_budget`:** dropped: not needed. Deferred tests always wait for
   the gate (R7).
4. **Explicit `--base`:** keeps skipping last-green (R37). No change.
5. **Lockfile mitigation (R36):** deferred out of 0.7.0. It adds a second
   fingerprint concept (identity vs environment) and two store columns for a
   rare event in a task chain. A lockfile bump keeps today's behaviour; the
   end line must say why it ran the full suite. `pyproject.toml` stays a full
   trigger. Revisit in 0.7.x only if the real-repository step shows it matters.
6. **Observer clearing:** pruning only (R17). No decay.
7. **Ambient fingerprint:** argv array (R41), a fixed 60 s timeout (no key),
   also run at gates.
8. **Real-repository smoke step:** one repository, the user's `fullon2`
   `api` child, and only after the user approves each `--full`. Persea is
   excluded until the user says otherwise.
9. **End-line numbers:** plain integers (`deferred 14300`), house style.
10. **Monorepo:** one summed `deferred` count and one hint on the total line.

Orchestrator decisions (2026-10-19), round 1 flags: R17 clears by node-id
pruning and the caps only; R14 cap eviction is reported as a count (`-v` and
JSON); declared `observers` stays, doctor work is dropped (R18, R47);
`iteration_budget` is dropped (R7, Decision 3); the JSON block is slim (R34);
`selection.full_reason` stays and a forced full suite counts as a gate (R8a).
Audit round 1: deferral is measured since the gate over all paths (R9, R27);
no-run lines say `nothing to run now` and an empty-diff plan runs its T1 tests
(R10, R31); observers are literal path prefixes (R13); the weaker tier is
merged into T1 (R1); `_full_text` is deleted (R6); the fingerprint timeout is a
constant and the command digest is dropped (R41, R43, R46).
Audit round 2: a skipped launch is a guard stop with reason `no-tests-needed`
that the controller classifies as `NO_TESTS_NEEDED` (R44); the decision
deadline grows by the fingerprint timeout only when `ambient_fingerprint` is
declared (R42a); R35 edits only `_provider_text` and keeps the 0.6.8 skill
bytes as a recognised previous version; gate-marker dirty digests are keyed
and bounded like `_digest_bounded`, oversize or unreadable files compare as
changed (R27, R9).
Audit round 3: a `no-tests-needed` run skips `_selection_after_run`, so it
writes nothing to the store (R44); a writing fingerprint command fails closed
(R42, R43); the manifest field is optional and bounded like `stall_timeout_s`
(R42a); a dirty path deleted since the gate stays in `since_gate` (R27).
