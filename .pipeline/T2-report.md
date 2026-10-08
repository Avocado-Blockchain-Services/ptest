# T2 report — Doctor Test policy facts and rendering

- status: DONE
- taskWorktree: /home/ingmar/worktrees/ptest/cc-test-policy/ptest-T2
- taskBranch: feature/test-policy-T2
- commit: 9e16a5f (T2: doctor Test policy facts and rendering)
- filesOwned:
  - src/ptest/policy_facts.py (new)
  - src/ptest/policy_render.py (new)
  - src/ptest/recommendations.py (kwarg only)
  - tests/ng/test_policy_facts.py (new)
  - tests/ng/test_policy_render.py (new)
  - tests/ng/test_recommendations.py (appended)
  - tests/ng/test_doctor_test_policy_cli.py (new)

## Result

Implemented static per-project Test policy facts (`policy_facts.collect`,
`src/ptest/policy_facts.py:538`), terminal/Markdown rendering
(`policy_render.render_terminal` `:172`, `render_markdown` `:219`), and the
optional `test_policy` parameter on `recommendations.render_recommendations`
(byte-identical when None; `## Test policy` section after the sentinel when
given). Scoped run from the worktree root:

`ptest tests/ng/test_policy_facts.py tests/ng/test_policy_render.py tests/ng/test_recommendations.py tests/ng/test_doctor_test_policy_cli.py`
(exit 0) → **157 passed, 1 skipped** (`test_real_barrier_scan_*`, gated on the
barrier; orchestrator must confirm 0 skipped after the merge).

Strict TDD: the four test files were written before the modules and failed at
collection (`ImportError`, 49 failed, 1 skipped); implementation then went
green. No test or assertion was weakened to get green; three implementation
bugs and two over-strict assertions found by failing tests were fixed on the
correct side (see Pass 1).

## Three-pass review (dan-jefferies-agent)

- Pass 1 (re-read bytes): five findings, all fixed — NameError in omit
  splitting (caught by failing omit tests); write-only `found_rc` /
  `has_settings` removed; pragma file opens hardened to
  `O_RDONLY|O_NOFOLLOW|O_NONBLOCK` (plain `open` would block on a FIFO and
  could follow a swapped-in symlink); double Markdown-escaping of
  already-neutralized heads removed; recommendations docstring corrected to
  the true insertion point. Post-fix suite re-run green (157 passed).
- Pass 2 (acceptance anchoring): gate table covers every D12 source
  (`test_coverage_py_gate_sources`, runner `args`/`full_args` `=`/space
  forms, addopts string and list, `pytest.ini`/`setup.cfg`/`tox.ini`,
  float values, missing gate); branch word spellings plus `--cov-branch`,
  line-only risk only with gate present and branch off; omit bounds
  (20 / 120 chars, total kept); pragma counts correct, partial at bounds,
  test-roots and vendored dirs excluded, symlinks never followed, FIFO never
  blocks; vitest fixed line with FIFO and `0o000` never-opened proofs;
  instruction wiring tested through a monkeypatched scanner plus one
  barrier-gated real-scanner test; terminal shape/suggestion/opt-in-pointer
  rules; JSON determinism plus absence of policy keys; offline writes
  nothing (bytes and mtimes); no readiness/row/score changes (cli.py and
  render.py untouched; sole `render_recommendations` caller uses defaults).
  Two deviations, both documented below.
- Pass 3 (smell sweep): no duplicate concept (`cov_fail_under` in
  `runtime/pytest_bridge.py:3237` is runtime identity, a different job);
  new-module names are design-mandated (D2); existing `render.terminal_text`
  reused, not duplicated; no TODOs/stubs; positives fail against a no-op
  module by construction (proven by the red phase). Security: all
  repository-derived strings sanitized at the render boundary
  (`terminal_text`, `_md`); numbers are counts only. No new dependency, no
  config scattering, no shipped shim.

## Key decisions (spec-literal readings, pinned by tests)

- v2 monorepos report children only (like `executability.check_resolution`);
  the root is covered once by `root_instructions`. Standalone reports one
  `.` project with `root_instructions=None`.
- Unquoted TOML `True`/`on`/`yes` are not valid TOML, so word spellings are
  tested quoted; native `branch = true` tested separately.
- Design's JSON "byte-identical with and without coverage config" sentence
  cannot hold literally: repo content already flows into offline JSON
  through the pre-existing static path (excerpts/limitations differ when
  files are added — observed, not assumed). Pinned instead: fixed-fixture
  determinism plus no policy keys and no gate text in JSON output.

## Integration notes / seams

- **Barrier missing**: the frozen `agent_rules` block (design s12) is in
  NEITHER the chain worktree nor this task worktree (both at `bbf1260`;
  `coverage_instruction_lines`/`TEST_POLICY_PATH` absent, verified by
  grep). Per the task brief the barrier was NOT touched: `policy_facts`
  resolves `_SCANNER`/`InstructionScan`/`TEST_POLICY_PATH` by `getattr`
  with a marked fallback (empty scan plus a plain "not inspected" note),
  and the doctor grid wiring (`cli._test_policy_outputs`, T3/barrier-owned)
  is covered by a conditional test that asserts the T2 pipeline output
  today and the after-grid block once the barrier lands. Re-run the four
  owned files post-merge (expect 0 skipped).
- T3 consumes: `collect`, `PolicyReport`, `render_terminal`,
  `render_markdown`, `render_recommendations(..., test_policy=...)` with
  the exact frozen signatures.
- Never pushed, merged, or deployed. Staged explicit paths only (7 files,
  +1676/-1). No migration generated (none requested; none needed).

---

## Fix report (2026-10-08, repair of the 4 review findings)

Worktree `/home/ingmar/worktrees/ptest/cc-test-policy/ptest-T2`,
branch `feature/test-policy-T2` (reattached onto the barrier commit below).
TDD throughout: each new test was run red before its fix
(nested-root: `assert 1 == 2`; percent-INI: `gates == {}`), then green.
No test or assertion was weakened, skipped, or deleted to get green; one
test was renamed when its premise became impossible
(`test_missing_scanner_degrades_to_note` ->
`test_raising_scanner_degrades_to_note`), and one over-broad conditional
test was replaced by a direct assertion now that the barrier is present.

### 1. Barrier applied; every shim dropped (was: report BLOCKED)

The barrier truly was absent (chain and T2 both at `bbf1260`; both greps
empty). Per the finding's expected course, the exact design section 12
transcription was applied to the chain base and committed there as
`6324b8c` ("Barrier: test-policy shared hunks (design section 12, B1-B6)",
8 files: both guide copies 100 -> 109 lines and byte-identical,
`agent_rules.py` B2+B4, `checklist.py` B5, `cli.py` B6, the three B3 test
files). Pre-change guide sha256 was verified against git history
(`git show bbf1260:...` = `5ac1f26c...` for both copies) before baking the
B2 hash. Chain-side evidence (all from the chain worktree root):
`ptest tests/ng/test_agent_rules.py tests/ng/test_resources.py
tests/ng/test_init_changed.py tests/ng/test_checklist.py` -> 96 passed;
`ptest tests/ng/test_doctor.py` -> 154 passed;
`ptest tests/ng/test_cli.py` -> 372 passed.
T2 (`9e16a5f`) was then rebased onto `6324b8c` (clean, no conflicts;
new tip `f18dcc1`) — no history under T3 was touched (T3 branched from
`bbf1260`, not from the chain tip), but T3 still lacks the barrier and
will need its own rebase; orchestrator to schedule it.
With the barrier present, `src/ptest/policy_facts.py` now uses the frozen
symbols directly (`:18`
`from .agent_rules import TEST_POLICY_PATH, InstructionScan,
coverage_instruction_lines`): `getattr` resolution, `_FallbackScan`,
the `Any` field types, and the `pragma: no cover - barrier seam` line are
all gone. Frozen types are used directly (`:76`
`instructions: InstructionScan`, `:83`
`root_instructions: InstructionScan | None`); `_scan_instructions` (`:351`)
calls the real scanner with a degrade-to-note except that returns the
empty frozen `InstructionScan` (the module's standing never-raise policy,
not a shim). `policy_render.py` getattr seams likewise became direct
access (`:73`, `:138`, `:140`). The dead `scan_instructions` flag on
`_project_facts` was removed (both callers passed `True`). Scanner tests
now monkeypatch `policy_facts.coverage_instruction_lines`
(`tests/ng/test_policy_facts.py:313-342`); the previously skipped
`test_real_barrier_scan_*` now runs. Public scan function the CLI reuses
is the barrier's `agent_rules.coverage_instruction_lines` (design 2.1
signature, unchanged); the CLI reuses T2 via barrier `_test_policy_outputs`
-> `policy_facts.collect` + `policy_render.render_terminal`, verified end
to end (`tests/ng/test_doctor_test_policy_cli.py:77` asserts the block
after the grid through `main`).

### 2. Nested test roots no longer hide their top-level dir

`_count_pragma` kept only the first path component
(`roots.add(text.split('/')[0])`), so `test_roots = ["app/tests"]`
excluded all of `app/`. It now stores full tuples and skips on full
relative prefix (`src/ptest/policy_facts.py:376-384`,
`_under_test_root`: `parts[:len(root)] == root`), applied at both the
directory-prune and file-count sites. New test
`tests/ng/test_policy_facts.py:240`
(`app/core.py` + `app/tests/test_a.py` + `tests2/b.py` with
`test_roots=("app/tests",)` expects 2) failed red (`assert 1 == 2`) and is
green; the `tests2/` row pins component-vs-prefix behavior.

### 3. `%` in an INI value no longer drops the project's facts

Both INI parsers are now `ConfigParser(interpolation=None)` — the
established codebase pattern (`config.py:848`, `executability.py:206`) —
and every `parser.get` goes through `_ini_value`
(`src/ptest/policy_facts.py:176`), which catches `configparser.Error`,
adds one per-file not-inspected note, and keeps all facts gathered so
far. New test `tests/ng/test_policy_facts.py:386` uses the finding's
repro (`addopts` with `"%(asctime)s %(message)s"` plus a `.coveragerc`
gate of 80 plus one pragma line): red before (`gates == {}`), now both
gates plus `pragma_count == 1`, and the rendered block shows
`coverage gate: 90` / `coverage gate: 80` with no `none found`.

### 4. JSON acceptance test replaced by a literal one; schema half open

New `test_doctor_json_identical_with_facts_toggled`
(`tests/ng/test_doctor_test_policy_cli.py:98`) implements the design
sentence literally under the facts-toggle reading: same fixture, real
`collect` vs monkeypatched `PolicyReport()`, `doctor --offline --json`
stdout byte-identical across the toggle while human output changes
(`Test policy` present vs absent — proving the toggle was real, i.e. the
test cannot pass vacuously). The `validates against
docs/schemas/v1/doctor.json` half is NOT verified and is recorded as
such: no `doctor.json` exists anywhere in the tree (`ls docs/schemas/v1`
has 10 schemas, none for doctor; no test or src file references one;
`jsonschema` is not installed), schemas are frozen and unowned by T2, so
no schema file was invented. `test_doctor_offline_writes_nothing` (`:146`)
now asserts `Test policy` in the captured output, so the no-write
guarantee is proven over a run in which T2 code executed (it runs after
the barrier call sites, not before them).

### Verification (T2 worktree root, `ptest` only, never pytest directly)

- `ptest tests/ng/test_policy_facts.py tests/ng/test_policy_render.py
  tests/ng/test_recommendations.py tests/ng/test_doctor_test_policy_cli.py`
  -> **161 passed, 0 skipped** (was 157 passed + 1 skipped).
- Barrier-adjacent: `test_cli.py test_doctor.py test_agent_rules.py
  test_init_changed.py test_resources.py test_checklist.py` -> 621 passed,
  1 failed (see below, not T2-owned); `test_command_model.py
  test_help.py test_release_cli.py test_uninstall.py test_render.py` ->
  204 passed; scheduler disk-error subset -> 2 passed.
- Bare reach-selected `ptest` from the T2 root -> 5974 passed, 28 skipped,
  1 failed; the 1 failure is exactly the scope test below (it fails
  deterministically in every scope it runs in, and every other reached
  suite is green above).
- `graphify update .` run in both the chain and T2 checkouts.

### Discovered but NOT fixed (T3-owned; report only, no edit made)

`tests/ng/test_cli.py::test_doctor_scope_and_unsafe_scope_exit_codes_from_monorepo_root`
asserts `"web" not in captured.out` for
`doctor --offline --scope api`. With barrier+T2 integrated, the Test
policy block lists both `api` and `web` headers (reproduced by hand:
scoped run prints `api coverage gate: none found` then `web coverage
gate: none found`). `ConfigResolution` carries no scope and the
`_test_policy_outputs(resolution, ...)` call site is frozen barrier text
in T3-owned `cli.py`, so no T2-owned layer can scope the block without
changing a frozen signature or editing files T2 must never touch
(design: T2 never edits `cli.py`; never weaken a test to get green).
Referred to T3/orchestrator: either plumb scope to the policy call sites
(barrier amendment, orchestrator-approved) or narrow that assertion to the
grid portion. Touched-file set equals owned-file set (4 files below);
`cli.py`/`test_cli.py` deliberately untouched.

### Status

- Implemented: barrier transcription + T2 rebase; shim removal with
  frozen types; nested-root prefix fix; INI interpolation fix; JSON
  toggle-identity test + writes-nothing strengthening. Verified: all of
  the above per the runs quoted. Not verified: JSON-schema-file validity
  (schema file absent, out of scope to invent). Deferred: none.
  Discovered but not fixed: scoped-doctor project leak (T3).
- Confidence: high — every finding has a red-then-green test and a scoped
  or full run behind it; the one red and the one unverifiable half are
  both evidenced and owned elsewhere.
