# T1 report — core: static linked-worktree detection, config-uncommitted resolution, init refusal and --from-main copy

- taskWorktree: /home/ingmar/worktrees/ptest/cc-worktree-safe-config/ptest-T1
- taskBranch: feature/worktree-safe-config-T1
- commit: 31804c5 ("T1: static linked-worktree detection, config-uncommitted, init refusal and --from-main copy")
- base: 890e1ee (chain tip at start; no divergence handling needed)
- status: DONE — all acceptance criteria met, scoped suite green, committed, not pushed.

## What was built

- `src/ptest/worktree.py` (new): `CONFIG_NAME`, `AGENT_RULE_FILES`, `MANAGED_MARKER`,
  `LinkedWorktree`, `MainConfig`, `linked_worktree`, `main_config`,
  `uncommitted_config_files` — verbatim per design §3.1. Static detection only
  (lstat, no-symlink walks via `files._check_no_symlink_prefixes`, bounded
  `read_regular` reads, back-link check, `common.name == ".git"`); never raises;
  no subprocess for detection. Tracked-state via one
  `source._git(ls-tree -r -z --name-only HEAD -- ...)` with 2 s `_Scan` deadline;
  any error/unborn HEAD/non-git → `()`.
- `src/ptest/contracts.py`: `config-uncommitted` ∈ REASON_CODES,
  `config.uncommitted` ∈ FINDING_CODES, `InitOptions.from_main: bool = False`
  (validated as `init.from_main`), `InitResult.commit_paths` with repo-relative
  posix validation, `serialize_init_result`/`_project_init_payload`/
  `_validate_init_payload`/`PUBLIC_SCHEMAS["init"]` updated (property optional,
  not required).
- `src/ptest/config.py`: `CONFIG_UNCOMMITTED`, `config_uncommitted()`,
  `git_root()` (both never raise), `resolve_config` reroute to
  config-uncommitted (exact §3.3 message, root=physical_cwd, never loads main
  config), `init_project` refusal (incl. dry_run) + `--from-main` verbatim copy
  via `create_exclusive` (children first, never overwrite, stopgap warning,
  `commit_paths=()`) + normal-path `commit_paths` (created configs relative to
  git boundary; PREVIEW/EXISTING → `()`).
- `docs/schemas/v1/init.json`: regenerated via `scripts/export-schemas.py`;
  `--check` exits 0; it is the only generated file that changed.
- Tests: new `tests/ng/test_worktree.py` (44 tests, incl. verbatim
  `_linked_worktree` helper); appended config-uncommitted/from_main/commit_paths
  cases to `test_config.py`, `test_init.py`, `test_contracts.py` (also with the
  verbatim helper); updated the exact field/key-set assertions that the new
  fields intentionally change (`InitOptions` fields, init roundtrip keys,
  `_PUBLIC_DATA_KEYS["init"]`).

## Verification (observed)

- `ptest --workers 2 --queue-timeout 1800 tests/ng/test_worktree.py
  tests/ng/test_config.py tests/ng/test_init.py tests/ng/test_contracts.py`
  → **397 passed** in 65.86s (exit 0). Coverage row: `src/ptest/worktree.py
  237 stmts, 1 miss, 99%` (≥ 90% bar). The single miss is the intentionally
  unreachable `main_root == root` fail-closed guard (line 138: `common` is a
  real dir named `.git` whose parent is `root`, while `root/.git` is proven a
  regular file two checks earlier — both cannot hold at once).
- `uv run python scripts/export-schemas.py --check` → exit 0, no output.
- `graphify update .` → graph updated (graphify-out, untracked helper output
  only; no source changes from it).
- Files touched (all T1-owned): `src/ptest/worktree.py`, `src/ptest/config.py`,
  `src/ptest/contracts.py`, `docs/schemas/v1/init.json`,
  `tests/ng/test_worktree.py`, `tests/ng/test_config.py`,
  `tests/ng/test_init.py`, `tests/ng/test_contracts.py`. Nothing else.

## Integration notes for the orchestrator (T2/T3 seam)

- T2/T3 may now import `ptest.worktree` and use `InitOptions.from_main`,
  `InitResult.commit_paths`, `config.config_uncommitted`, `config.git_root`,
  `REASON_CODES ∋ "config-uncommitted"`, `FINDING_CODES ∋ "config.uncommitted"`.
  Barrier satisfied — no stubbing needed.
- Known unowned breakage (do NOT fix in T1; T2 owns it):
  `tests/ng/test_init_smoke.py::test_json_with_smoke_stays_machine_exact_and_never_runs`
  asserts `set(document.data) == {"action","target","exists","warnings","config"}`;
  init JSON now correctly includes `commit_paths`, so that assertion needs the
  one-word update in T2's scope.
- No database migration generated. No push, no merge, no deploy; commit
  `31804c5` lives only in the T1 worktree.
- Process note: one intermediate raw `pytest --cov` probe was run for missing-line
  diagnosis; the gating runs were all through `ptest` as required.

## Fix report (2026-09-29) — schema-drift half-state after e339087

- Finding: tip commit e339087 ("revert out-of-lane docs/schemas/v1/init.json")
  removed the regenerated `commit_paths` property from the committed public
  schema, while `src/ptest/contracts.py` PUBLIC_SCHEMAS["init"] still emits it.
  Reproduced at HEAD before fixing: `uv run python scripts/export-schemas.py
  --check` → exit 1 (`schema drift: .../docs/schemas/v1/init.json`), and the
  `test_init_schema_file_matches_descriptors` equality expression evaluated to
  False. The prior gate's green run came from 31804c5, before the revert.
- Decision: restored the regenerated init.json in T1 (design.md §3.2, §4 T1
  'Owns', D9, §8 give `docs/schemas/v1/init.json` to T1 — the revert's
  owned-file premise conflicts with the design). No code or test touched; the
  source of truth is `contracts.py`, the file is generated output.
- Fix: `uv run python scripts/export-schemas.py` — only
  `docs/schemas/v1/init.json` changed (+6 lines, `commit_paths` array
  property; all other schemas byte-identical, exporter rewrote them
  identically). `--check` now exits 0; the schema-equality expression now
  evaluates to True.
- Verification (observed, worktree
  /home/ingmar/worktrees/ptest/cc-worktree-safe-config/ptest-T1):
  - `ptest --workers 2 --queue-timeout 1800 tests/ng/test_worktree.py
    tests/ng/test_config.py tests/ng/test_init.py` → **316 passed** in
    30.07s (exit 0; ptest wall 28m35s incl. queue). `src/ptest/worktree.py`
    still 237 stmts, 1 miss, 99%.
  - `ptest --workers 2 --queue-timeout 1800 tests/ng/test_contracts.py -k
    test_init_schema_file_matches_descriptors` → **1 passed** (exit 0).
  - `uv run python scripts/export-schemas.py --check` → exit 0, no output.
- Commit: fix committed in the T1 worktree (branch
  feature/worktree-safe-config-T1), not pushed, no merge, no deploy.
- dan-jefferies passes: (1) re-read the full `git diff` — exactly the 6-line
  `commit_paths` block, byte-identical to 31804c5's version, no other files;
  (2) acceptance criterion re-anchored — schema file == descriptors
  expression True, `--check` clean, scoped suite green; (3) smell sweep —
  no new helpers/abstractions, no synonyms to dedupe (`commit_paths` grep:
  only contracts.py producers/consumers + init.json + T1 tests), no
  stubs/TODOs, contract drift closed not opened. Known unowned breakage from
  the prior report (`test_init_smoke.py` key-set assertion, T2-owned) is
  unchanged by this fix and still left for T2.
- Status: DONE — tip is self-consistent again; criterion 'Tests pass via
  ptest ... tests/ng/test_contracts.py' met at the new tip for the named
  test (full test_contracts.py file not re-run; only the drift-covering test
  plus the three requested files).
