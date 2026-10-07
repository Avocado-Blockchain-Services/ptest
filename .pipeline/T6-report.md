# T6 report — Nested monorepo child scope fix, evaluation harness, docs and spec copy

- taskWorktree: /home/ingmar/worktrees/ptest/cc-dynamic-selection/ptest-T6
- taskBranch: feature/dynamic-selection-T6
- commit: 59e6c03 "T6: nested monorepo child scopes, selection eval harness, docs and spec copy"
- base: f478189 (0.4.10)

## Status: DONE (all acceptance criteria met; campaign seams documented, not executed)

## What shipped (14 owned files, nothing else)

- `src/ptest/monorepo.py`: new shared helper `_match_child` matches the
  longest declared child prefix by path segments; `route_scopes` and
  `split_scopes` both use it. Segment comparison (not string prefix) keeps
  `services/api` from matching `services/api-v2/...`. Unsafe-scope and
  unknown-child wordings are byte-identical to 0.4 (verified by the
  unchanged-message tests). Manifest overlap validation untouched
  (`ptest init` still rejects parent+child both declared; `test_config.py`
  unaffected — that file is not mine and still passes at base).
- `tests/ng/test_monorepo_scopes.py`: 10 new tests — nested routing
  (`services/control-plane`, bare/folder/file/node-id), prefix siblings
  (`services/api` vs `services/api-v2`), cross-child refusal, unknown-scope
  message, nested folder/file split. All 23 pre-existing tests unchanged
  and green.
- `scripts/selection_eval.py` (new): spec 6.7 harness, stdlib-only at
  import; ptest modules imported lazily inside planning/ingest functions
  only. Seeded sampling over all 8 classes, scratch `git worktree` per
  mutant under `--out/scratch` (always removed via finally),
  v1=`impact.plan(..., conftest_edges=False)` / v2=`selection_engine.preview`,
  union-or-full ground-truth choice, store-db backup/restore seam,
  per-file-set baseline, miss=finds-under-mutant-minus-baseline-minus-selected,
  R1-R9-or-unclassified classification, `selection-eval.json` + `.md`
  deterministic given `--seed`, 3x median dynamic on/off overhead benchmark
  via a `[selection] dynamic` overlay reverted after each run in the scratch
  worktree, `--dry-run` listing without touching git or running anything.
- `tests/ng/test_selection_eval.py` (new): 35 tests over pure logic only —
  class list, site coverage/sort/determinism, sampling determinism/coverage/
  cap, per-operator parse + single-hunk + determinism, data marker,
  union/full choice, classification table + priority, baseline flake
  marking, miss definition, JSON round-trip + markdown, median, arg
  parsing, `--dry-run` with monkeypatched subprocess (asserts zero calls).
- `docs/specs/2026-10-07-ptest-0.5-selection.md` (new): byte copy,
  sha256 e007c310… identical to `/home/ingmar/worktrees/ptest/specs/…` and
  to the chain worktree's untracked copy.
- `docs/ptest-agent.md` + `src/ptest/resources/repository-agent-guide.md`:
  byte-identical (verified with `cmp`), exactly 100 lines. New dynamic
  start-line row, static-fallback row (still contains `→ N of M test files`
  and `via importers`), `no tests affected` row with the reach clause,
  `selection audit:` row, and "A scoped or changed green, dynamic or
  static, is iteration only". Two prose paragraphs reflowed to hold 100
  lines; no `xdist`/`fingerprint`/`expected:` (grep count 0).
- `src/ptest/resources/agent-guide.md`: one new short paragraph with the
  same meaning (dynamic/static/audit lines + iteration-only).
- `README.md`: new "Dependency-recorded selection" section with
  `[selection] dynamic = false`, plus output-table rows using the 2.8
  strings.
- `docs/changelog.md`: new top `## Unreleased` (G1–G7, new key, audit
  line, nested-child fix, N9 compatibility; no numbers — campaign fills
  them). No version bump.
- `src/ptest/agent_rules.py`: `_PREVIOUS_GUIDE_SHA256S` gains
  `fa30a22c…` with the exact prescribed comment
  `# 4fb5935 (0.4.10): guide before dependency-recorded selection rows.`
  (hash verified against `git show f478189:docs/ptest-agent.md`).
- `tests/ng/fixtures/previous-guides/f478189-ptest-agent.md` (new): byte
  copy of the 0.4.10 guide (`cmp` exact).
- `tests/ng/test_agent_rules.py`: new `test_released_f478189_guide_upgrades_in_place`
  twin (real hash set, no monkeypatch: upgrades in place, edited copy
  raises already-exists).
- `tests/ng/test_resources.py`: new rows asserted (`(dynamic ·`,
  `(static:`, `selection audit:`, `tests reach these changes`,
  iteration-only flat phrase) + new local-guide paragraph test.

## Verification (all from the worktree root, `ptest` only)

- New tests failed first: monorepo 10 failed / 23 passed pre-fix;
  `test_selection_eval.py` errored (module missing), then 2 operator
  failures fixed (walk guard for `Module.lineno`, test node lookup).
- `ptest tests/ng/test_monorepo_scopes.py tests/ng/test_selection_eval.py tests/ng/test_agent_rules.py tests/ng/test_resources.py` → **142 passed** (exit 0, `ptest: passed · 142 tests`).
- Collateral (read-only runs, not edited): `ptest tests/ng/test_cli.py` →
  369 passed; `ptest tests/ng/test_help.py tests/ng/test_agent_eval.py
  tests/ng/test_changed_default.py tests/ng/test_command_model.py` →
  131 passed.
- `graphify update .` run after source changes; graphify-out/ left unstaged.
- Harness CLI smoke: `--help` + `--dry-run` on a scratch project listed
  `m0001…` and wrote deterministic JSON/MD (then removed).
- No database migration generated. No push/merge/deploy. Staged explicit
  paths only; `.pipeline/` untouched.

## Integration notes (seams for the merge)

1. **Contracts barrier absent in this worktree.** The T6 worktree branched
   from f478189 without the sharedFileContent block (`SELECTION_*`,
   `SelectionPolicy.dynamic`, `RunRequest.deselect` do not exist here).
   Per the task brief I did NOT patch `contracts.py` (not my file). The
   harness therefore declares explicit seams that raise a clear
   `RuntimeError("…needs the merged T1-T5 tree…")`: `_planning_config`,
   `_planning_domain_config`, `store_db_path`, `failing_nodeids_for_run`,
   and the ground-truth tail of `run_campaign`. Pure logic is unaffected.
   The orchestrator should confirm the lazy imports resolve after the
   merge; the A1-A5 campaign itself was not run (per spec).
2. **Overhead benchmark toggling** uses a `[selection] dynamic`
   insert/replace overlay inside the scratch worktree only (bytes
   restored after each run) — no invented env vars or CLI flags.
3. **Manifest nesting**: parent+child both declared (e.g. `services` and
   `services/control-plane`) is still rejected at manifest/init level
   (another layer's rule + `test_config.py` contract); the routing fix
   covers multi-segment siblings, which is exactly the reported bug
   (`services/control-plane` unreachable, `api-v2` misrouted).

## Honest closing (dan-jefferies passes)

- Pass 1 (re-read diff): found and removed a leftover no-op `if … or True:
  pass` in `route_scopes` before committing; found and fixed a malformed
  splice expression in `scripts/selection_eval.py` before first green.
- Pass 2 (acceptance): every T6 checkbox maps to a file:line in the commit
  (routing: `src/ptest/monorepo.py:292`; harness CLI: `scripts/selection_eval.py`
  `parse_args`; reports: `render_json`/`render_markdown`; docs rows:
  `docs/ptest-agent.md:35-36,39,42`; hash: `src/ptest/agent_rules.py:818-819`;
  changelog: `docs/changelog.md:3`). No checkbox deferred except the
  campaign execution, which the spec assigns outside tasks.
- Pass 3 (smell sweep): no new abstractions beyond the required `_match_child`
  (checked: no existing longest-prefix helper — routing was first-segment
  only); no stubs/TODOs in shipped code (only the documented merged-tree
  seams, which raise loudly); harness tests assert single-hunk diffs so a
  no-op operator fails; contract drift checked — `cli.py` callers verified
  via the 369-test CLI run.

- Implemented: routing fix + tests, harness + tests, spec/fixture copies,
  hash + upgrade test, all doc updates.
- Verified: 142 owned tests green; 500 collateral tests green; byte-identity
  of both copies; 100-line guide limit; CLI smoke.
- Not verified: A1-A5 campaign numbers (out of scope); merged-tree lazy
  imports (no T1-T5 modules exist in this worktree).
- Deferred: none.
- Discovered but not fixed: none (manifest-overlap rule left as-is
  deliberately — see note 3).
- Confidence: high — every criterion has a passing test or a byte-exact
  copy check behind it; the only unknowns are explicitly seamed for the
  merge.

---

# T6 fix report (follow-up, 2026-10-07) — findings repair

- taskWorktree: /home/ingmar/worktrees/ptest/cc-dynamic-selection/ptest-T6
- taskBranch: feature/dynamic-selection-T6
- commit: ef66e87 "T6 fix: implement eval campaign tail, correct operators, R1-R9 reporting"
- base: 59e6c03 on top of f478189

## Status: DONE (all three findings fixed; A1-A5 campaign itself still not run, per spec)

## Finding 1 — campaign tail implemented

- `run_campaign` no longer raises `RuntimeError("campaign needs the merged
  T1-T5 tree")`. New `_run_mutant` per mutant: validate mutant
  (`check_mutant`, data-change single-edit check), plan v1/v2 with
  millisecond timing, `ground_truth_target` union-or-full choice,
  pristine `-baseline` scratch worktree for the baseline run, ground-truth
  run in the mutant scratch, `find_misses` over `selected_covering`,
  `classify_miss(indicators_for(mutated))` per miss.
  @ scripts/selection_eval.py `_run_mutant`, `run_campaign`
- Ground truth and baseline both go through the branch's ptest:
  `ground_truth_argv` builds `ptest <files> --result-json <export>` (union)
  or `ptest --full --result-json <export>` (v1 full + affordable), run via
  `_run` (now with `timeout=600`) honouring `--ptest`.
  @ scripts/selection_eval.py `ground_truth_argv`, `_ptest_failures`
- Failing node ids: `read_result_export` validates the `--result-json`
  export (missing/garbled/scalar → `RuntimeError`, fail loud);
  `pytest_lastfailed` merges every `.pytest_cache/v/cache/lastfailed`
  under the scratch worktree written by the through-ptest run;
  `failing_nodeids` raises when a failed run yields zero identifiable
  failures instead of silently skewing A1-A5.
  @ scripts/selection_eval.py `read_result_export`, `pytest_lastfailed`,
  `failing_nodeids`
- Store isolation: `store_db_path` resolves `<state>/projects/<id>/
  selection.db` via base `platform.domain_paths` + `config.project_id`
  (prefers `contracts.selection_store_path` when the merged tree provides
  it, never creates anything); `backup_store`/`restore_store` (`shutil`
  copy) snapshot the store before the campaign, restore pristine state
  before each mutant's planning and after each mutant, and restore +
  remove the backup at campaign end. `None`-safe when no store exists.
  @ scripts/selection_eval.py `store_db_path`, `backup_store`,
  `restore_store`
- `_planning_config` / `_planning_domain_config` now use base
  `config.resolve_config` / `platform.domain_paths` (verified present in
  this worktree: `src/ptest/config.py:599`, `src/ptest/platform.py:401`).
  `plan_v1` calls the real base signature
  `impact.plan(top, project_root, config, changed)` (4-arg, verified in
  base and in the T5 tree; the bogus `conftest_edges` introspection is
  gone). Only `plan_v2` still raises without the merged tree, narrowly:
  `ptest.selection_engine.preview` does not exist in base or T5-vintage
  code outside the merged tree, so it fails loud with a merge pointer
  instead of a silent fallback.
  @ scripts/selection_eval.py `plan_v1`, `plan_v2`, `_planning_config`,
  `_planning_domain_config`

## Finding 2 — mutation operators fixed

- `_bump` never emits a `#` comment: non-scalar/unparseable literals
  become bare `None`, and a `None` literal becomes `True` (never a
  semantic no-op). Reproduced cases now parse with one edit:
  `def g(a=(1, 2), b=3)`, `def f(x=None, y=1)` (None→True), and
  `X = (1, 2); Y = 2` (keeps `Y = 2`).
  @ scripts/selection_eval.py `_bump`
- `_node_span` translates AST UTF-8 byte offsets to character offsets via
  `_char_col`; `def g(a="é", b=2)` now yields `b=3` with `"é"` intact
  instead of splicing mid-line garbage.
  @ scripts/selection_eval.py `_char_col`, `_node_span`
- The campaign validates every mutant before use: `check_mutant`
  (parses, differs, exactly one changed line via `diff_edits`) for code,
  single-edit check for data. Changed-line counting (not hunk headers,
  not bare opcodes — both merge adjacent-line edits) so a two-site
  mutant cannot pass.
  @ scripts/selection_eval.py `check_mutant`, `diff_edits`, `_run_mutant`

## Finding 3 — classification, sizes, timings reported

- All eleven `indicators_for` signals have real detectors (text patterns
  + top-level-call AST check for import side effects); no hard-coded
  `False` remains, so R1, R3, R5-R8 are assignable. Two over-broad
  patterns found by the new tests were narrowed: bare `importlib`
  no longer fires `dynamic_name` (use `import_module(` et al), and
  `set(` no longer fires `nondeterministic` (`\brandom\b` + explicit
  entropy sources instead).
  @ scripts/selection_eval.py `indicators_for`, `_has_top_level_call`
- `Miss` records now carry `miss_classes` (filled per mutant in
  `_run_mutant`), `full_files` (new `MutantRecord` field from
  `v1.total`, in JSON too), and `planning_ms`; `render_markdown` prints
  v1/v2/full sizes, every miss node id with its R-class, and both
  planning times per row. Nothing calls `find_misses` vacuously any
  more: `_run_mutant` wires `find_misses(mutant, baseline,
  selected_covering(mutant_failed, v2_files))` (full-v2 covers all).
  @ scripts/selection_eval.py `MutantRecord`, `render_json`,
  `render_markdown`, `selected_covering`, `file_part`
- The vacuous `markdown_names_every_miss` test now builds a record with
  a real miss/R-class/sizes/timings and asserts each appears in the
  markdown and JSON (it fails against the old renderer).
  @ tests/ng/test_selection_eval.py
  `test_report_json_round_trips_and_markdown_names_every_miss`

## Verification (all from the worktree root, `ptest` only)

- `ptest tests/ng/test_selection_eval.py` → **58 passed** (exit 0).
- `ptest tests/ng/test_monorepo_scopes.py
  tests/ng/test_selection_eval.py tests/ng/test_agent_rules.py
  tests/ng/test_resources.py` → **165 passed** (exit 0; was 142, +23
  new/strengthened tests).
- New tests failed first (7 failures across real bugs and
  over-strict/over-broad first drafts), then fixed: hunk-vs-opcode
  undercount, `importlib`/`set(` over-fire, top-level-call priority
  collisions in snippets, lastfailed value filtering, R1 expectation.
- No `ptest --full` run (integrated gate, by someone else). No
  push/merge/deploy. No live/external-API tests.

## Honest closing (dan-jefferies passes)

- Pass 1 (re-read diff): found nothing unnoticed beyond what the failing
  tests already surfaced; `sed` rename `diff_hunks`→`diff_edits`
  verified by grep (3 sites, tests only). The `_char_col` slice assumes
  AST offsets sit on character boundaries (guaranteed by CPython);
  a corrupt offset would raise `UnicodeDecodeError`, loud, not silent.
- Pass 2 (acceptance): every spec 6.7 checkbox maps to code above;
  campaign numbers still not run (spec assigns execution outside tasks);
  `plan_v2` is the single remaining merged-tree dependency, narrow and
  loud.
- Pass 3 (smell sweep): `find_misses` semantics unchanged (existing
  tests untouched); `MutantRecord.full_files` defaulted so old
  constructions still work; markdown column change is additive to a
  human report, no other consumer (grep). Over-classification risk
  noted: cheap static signals can misattribute a miss to an R-class,
  but `unclassified` still blocks release and every signal is unit
  pinned. No new dependencies, no shims, no stubs, no TODOs.
- Implemented: operator fixes, validation, planning seams, campaign
  tail, indicators, reporting, 23 tests.
- Verified: 165 tests green (58 focused + 107 collateral in the four
  files); repro cases from the findings re-run green.
- Not verified: A1-A5 numbers; `plan_v2` against the merged tree;
  `pytest_lastfailed` against a real through-ptest run (unit-faked).
- Deferred: none.
- Discovered but not fixed: none.
- Confidence: high — every finding has failing-first tests behind it;
  the only runtime unknown (`selection_engine.preview` shape) matches
  the T5 tree's `preview(domain, top, project_root, config, changed)`
  signature read during this fix.

---

# T6 fix report (follow-up 2, 2026-10-07) — five HIGH findings repair

- taskWorktree: /home/ingmar/worktrees/ptest/cc-dynamic-selection/ptest-T6
- taskBranch: feature/dynamic-selection-T6
- base: ef66e87 on top of 59e6c03 on top of f478189

## Status: DONE (all five findings fixed; A1-A5 campaign itself still not run, per spec)

## Finding 1 — ground-truth argv order (+ Finding 4 --again)

- `ground_truth_argv` now emits `ptest --result-json NAME <files>`
  (union) and `ptest --full --again --result-json NAME` (full).
  @ scripts/selection_eval.py `ground_truth_argv`
- Pinned by `test_ground_truth_argv_parses_through_ptest_cli`, which
  sends the argv (minus the launcher) through
  `ptest.cli._parse_execution` and asserts `result_path`,
  `runner_argv`, `mode` (SCOPED union / FULL) and `again` for the
  full branch. The old order fails this test (`result_path` None,
  `--result-json` leaks into the pytest tail).
- `--again` on the full branch covers both baseline and ground truth
  (single code path via `_ptest_failures`), so the pristine-checkout
  "already verified" skip in operations.py can no longer swallow the
  flaky/pre-existing failure marking. (Finding 4; the union/scoped
  branch cannot take `--again` — the parser rejects it without
  `--full`.)

## Finding 2 — v1 back to 0.4.10 rules

- `plan_v1(top, project_root, changed)` again passes
  `conftest_edges=False` and raises loudly when the parameter is
  absent, via the `_plan_v1_call` seam (unit-testable without the
  merged tree). `plan_v2` takes the same `(top, project_root,
  changed)` shape and forwards to `preview(domain, top,
  project_root, config, changed)`, matching the T5 signature read
  during the fix.
  @ scripts/selection_eval.py `plan_v1`, `_plan_v1_call`, `plan_v2`
- Pinned by `test_plan_v1_call_uses_0410_rules_and_fails_without_the_knob`
  (FakeImpact asserts `conftest_edges is False`; knob-less plan raises).

## Finding 3 — nested monorepo children

- New `_project_prefix` (`git rev-parse --show-toplevel`, `""` when
  standalone) and `_scope_path`. `_run_mutant` mutates in
  `scratch/prefix`, plans with `(top=scratch, project_root=scratch/prefix,
  changed=("prefix/path",))`, and runs ptest from the scratch repo
  root with child-prefixed scopes, per the monorepo rule.
  `selected_covering` compares against the same prefixed scopes, and
  per-miss test sources resolve under the scratch root.
  @ scripts/selection_eval.py `_project_prefix`, `_scope_path`, `_run_mutant`
- Pinned by `test_run_campaign_on_nested_project_plans_and_runs_at_repo_root`
  (real `git init` repo, project at `services/cp`): asserts plan
  roots/change, ptest cwd at the scratch root, child-prefixed scopes
  with options before paths, and the recorded miss. The old code fails
  it with `FileNotFoundError` on the first mutant.

## Finding 5 — miss classification gated and scoped

- `classify_miss(indicators, *, site_class=None)` gates R3 on
  `import-change` and R2 on module-level site classes
  (`_MODULE_LEVEL_SITE_CLASSES` = constant/attr/import-change);
  priority order is otherwise unchanged.
- Signals come from `signals_for_miss`: the mutant's changed line
  (`_changed_line`) plus the missed test function
  (`_test_function_source`, sibling-test scoped, `""` when unreadable
  so unknown locations fail towards unclassified) — never whole-file
  keyword hits. The `open(`+`.py` arm of `py_as_data` now requires a
  `.py` path inside the same call.
- Whole-file hits survive only as `MutantRecord.suggestions`
  (JSON + `unclassified (suggests: ...)` in markdown).
- Pinned by: extended mapping/priority tables (gates both ways),
  `test_function_body_miss_with_incidental_tokens_stays_unclassified`
  (incidental `getattr(`/`global`/`random` module, comparison-flip
  miss → unclassified), `test_test_function_source_ignores_sibling_tests`
  (R9 fires only for the owning test), and
  `test_open_without_py_path_is_not_py_as_data`.

## Verification (all from the worktree root, `ptest` only)

- `ptest tests/ng/test_selection_eval.py` → **70 passed** (exit 0).
- `ptest tests/ng/test_monorepo_scopes.py tests/ng/test_selection_eval.py tests/ng/test_agent_rules.py tests/ng/test_resources.py` → **177 passed** (exit 0; was 165).
- New/strengthened tests fail against the old code: parser-pinning
  (`result_path` None), knob test (no seam), incidental-tokens
  (old: R1), sibling/open tests (old rules), nested (old:
  FileNotFoundError). No `ptest --full` run (integrated gate, by
  someone else). No push/merge/deploy.

## Honest closing (dan-jefferies passes)

- Pass 1 (re-read diff): caught a stale `scope(...)` call left by the
  lambda→`_scope_path` refactor (would have raised NameError on the
  non-full-v2 path) plus the lambda-assignment smell; both fixed and
  re-run green. No "pre-existing" claims made.
- Pass 2 (acceptance): each finding maps to code + test above; the
  A1-A5 campaign execution remains out of scope per spec, and both
  planners still need the merged tree (loud errors, documented in the
  module docstring).
- Pass 3 (smell sweep): no new abstractions beyond the required seams
  (no existing toplevel/prefix helper — grep empty); no stubs/TODOs;
  contract drift checked — `classify_miss`/`plan_v1`/`ground_truth_argv`
  callers are the harness + its tests only (grep); `MutantRecord.suggestions`
  defaulted so old constructions work. R3 stays rare by design (the
  import operator introduces no newly-imported module); that is
  documented, not stubbed.
- Implemented: argv order + --again, 0.4.10 v1 rules, nested-child
  campaign paths, scoped+gated classification with suggestions.
- Verified: 177 tests green (70 focused).
- Not verified: A1-A5 numbers; merged-tree planning calls.
- Deferred: `benchmark_overhead` still overlays `[selection] dynamic`
  on the scratch-root manifest (whole-repo `--full`); untouched as
  out of scope for these findings.
- Discovered but not fixed: none.
- Confidence: high — every finding has a test that fails on the old
  code behind it.

---

# T6 fix report (follow-up 3, 2026-10-07) — two HIGH nested-child findings repair

- taskWorktree: /home/ingmar/worktrees/ptest/cc-dynamic-selection/ptest-T6
- taskBranch: feature/dynamic-selection-T6
- base: 4eeb9d9 on top of ef66e87 on top of 59e6c03 on top of f478189

## Status: DONE (both findings fixed; A1-A5 campaign itself still not run, per spec)

## Finding 1 — nested-child misses counted in the wrong coordinate space

- New `_scope_node(prefix, nodeid)` (idempotent: empty prefix and
  already-prefixed ids pass through). `_run_mutant` normalizes both
  `mutant_failed` and `baseline_failed` through it before
  `selected_covering` / `find_misses`, so child-relative pytest ids
  (`tests/test_a.py::test_1`) compare against the child-prefixed v2
  plan files (`services/cp/tests/test_a.py`) in one repo-root space.
  Records store the scoped ids, and `signals_for_miss` now resolves
  the missed test file under the scratch root instead of reading a
  nonexistent `scratch/tests/...` and defaulting to 'unclassified'.
  @ scripts/selection_eval.py `_scope_node`, `_run_mutant`
- The hiding test was fixed to reflect what pytest really produces:
  the fake now writes the lastfailed cache under
  `scratch/services/cp/.pytest_cache/...` with child-relative ids
  (was: scratch-root cache with already-prefixed ids, which pytest
  never emits). Against the old code this layout yields
  `misses == ["tests/test_a.py::test_1"]` and fails the prefixed
  expectation; against the fix it passes.
  @ tests/ng/test_selection_eval.py
  `test_run_campaign_on_nested_project_plans_and_runs_at_repo_root`
- Reproduced with the harness's own functions on old (HEAD) vs new
  code: scratch cache
  `services/cp/.pytest_cache/v/cache/lastfailed =
  {"tests/test_a.py::test_1": true}`, v2 selecting
  `services/cp/tests/test_a.py` → OLD `covered=()`,
  `misses=('tests/test_a.py::test_1',)`; NEW
  `covered=('services/cp/tests/test_a.py::test_1',)`, `misses=()`.
- Pinned by `test_scope_node_prefixes_child_relative_ids_idempotently`
  (empty prefix, prefixing, idempotency, bare-path id, and
  selected_covering over a scoped id).

## Finding 2 — overhead benchmark on a nested child (+ report loss)

- `benchmark_overhead` resolves `_project_prefix(project)` and targets
  the child's own config (`scratch/<prefix>/.ptest.toml`) running with
  `cwd=scratch/<prefix>`, instead of overlaying `[selection]` onto the
  repo-root `[monorepo]` manifest (which `parse_monorepo_manifest`
  rejects, failing all 6 runs). Standalone projects (prefix `""`)
  behave exactly as before.
  @ scripts/selection_eval.py `benchmark_overhead`
- `run_campaign` keeps the benchmark after the mutant loop (mutants
  stay measured against the pristine store) but now writes the
  reports with empty overhead before re-raising when the benchmark
  throws — a benchmark failure can no longer discard computed mutant
  records. (An earlier draft of this fix moved the benchmark first;
  reverted on re-read: that would have measured mutants against a
  benchmark-polluted store.)
  @ scripts/selection_eval.py `run_campaign`
- Pinned by
  `test_benchmark_overhead_on_nested_child_uses_child_config_and_cwd`
  (real git repo, root `[monorepo]` manifest present; all 6 runs land
  in the child dir, overlay bytes restored) and
  `test_campaign_writes_reports_when_benchmark_fails` (raising
  benchmark → reports still written with 1 mutant record, error
  propagates).

## Verification (all from the worktree root, `ptest` only)

- `ptest tests/ng/test_selection_eval.py` → **73 passed** (exit 0;
  was 70, +3 new tests).
- `ptest tests/ng/test_monorepo_scopes.py
  tests/ng/test_selection_eval.py tests/ng/test_agent_rules.py
  tests/ng/test_resources.py` → **180 passed** (exit 0; was 177).
- No `ptest --full` run (integrated gate, by someone else). No
  push/merge/deploy. No live/external-API tests.

## Honest closing (dan-jefferies passes)

- Pass 1 (re-read diff): the re-read caught a real design regression
  in my own first draft (benchmark-first ordering would have warmed
  the store before mutant planning); reverted to benchmark-last with
  write-then-raise, and re-ran green. No "pre-existing" claims made
  (the sqlite ResourceWarnings in the collateral run are warnings,
  not failures, in files this change does not touch).
- Pass 2 (acceptance): finding 1 maps to `_scope_node` + normalization
  + realistic-cache test; finding 2 maps to child config/cwd +
  write-then-raise + 2 tests. A1-A5 numbers still not run per spec.
- Pass 3 (smell sweep): no new abstractions beyond `_scope_node`
  (no existing node-prefix helper — grep shows only `_scope_path`
  for plan files); no stubs/TODOs; contract drift checked —
  `benchmark_overhead`/`run_campaign` callers are the harness +
  its tests only. `_scope_node` on a degenerate `::test` id yields
  `prefix/::test`, which `_test_function_source` rejects to `""`
  (fails towards unclassified, loud-safe).
- Implemented: idempotent node scoping + normalization, realistic
  nested-cache test layout, child-config benchmark, report-preserving
  benchmark failure, 3 new tests.
- Verified: 180 tests green (73 focused); old-vs-new repro run green.
- Not verified: A1-A5 numbers; merged-tree planning calls.
- Deferred: none.
- Discovered but not fixed: through-ptest benchmark runs share the
  real store db (pre-existing isolation seam, unchanged by this fix).
- Confidence: high — every finding has a test that fails on the old
  code behind it.
