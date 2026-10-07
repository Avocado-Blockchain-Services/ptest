# Context pack — ptest 0.5 dependency-recorded test selection

Spec (authoritative): `docs/specs/2026-10-07-ptest-0.5-selection.md` (copy of
`/home/ingmar/worktrees/ptest/specs/2026-10-07-ptest-0.5-selection.md`).
Worktree: `/home/ingmar/worktrees/ptest/cc-dynamic-selection/ptest`, branch
`feature/dynamic-selection`, cut from local `main` @ f478189 (0.4.10).

## Runner and test commands
- Runner: **ptest** (root `.ptest.toml`, committed on main). Read `docs/ptest-agent.md`.
- Per task: `ptest <your own test files>` from the worktree root, e.g.
  `ptest tests/ng/test_source_index.py tests/ng/test_impact.py`. Never bare
  `ptest`/`--base` during tasks; never pytest directly; never add --workers/--timeout.
- Integrated gate, once, at Verify only: `ptest --full`.
- installCmd / coverageCommand / coverageGate: none (ptest runs `uv sync --locked --extra test` itself).
- envFiles: none. localOnlyPaths: none. Migration head: n/a (no DB migrations; the
  new store carries its own schema version).
- `.pipeline/progress.md` and `.pipeline/context-pack.md` are tracked from an old
  run: never stage `.pipeline/` (no `git add -A`, no `commit -a`).

## Precedent to imitate
`src/ptest/stack_dumps.py` (0.4.10): host-side, bounded, private-file-checked
(O_NOFOLLOW, owner, mode, nlink) ingest of per-process files the bridge writes
next to the native report (`<report>.stack-<pid>`; bridge side:
`_stack_dump_path`/`_register_stack_dump` in `runtime/pytest_bridge.py` ~L894-966;
name constants in `contracts.py` L134-136, L1259-1271). The dependency file,
deselect binding and store follow the same identity/abuse rules.

## Files this feature touches
Existing:
- `src/ptest/impact.py` — 0.4 static planner (`plan()` L390; reads+parses every
  .py twice). Gains the conftest→tests-under-dir edge (5.9) and the parse cache.
  `full_ratio` / `MAX_SELECTED=200` stay static-only. `Impact` dataclass L32.
- `src/ptest/cli.py` — changed routing: `_impact_note` ~L3001 (start lines),
  `_impact_run_request` ~L3027, folder changed ~L3364-3500, monorepo ~L3633.
- `src/ptest/operations.py` — `execute()` L2945; report binding, history publish
  ~L3960, finalize/cleanup ~L3988-4015. Store update (N8: valid handoff, inputs
  unchanged), deselect binding, self-audit on full runs go here.
- `src/ptest/adapters/pytest.py` — env_updates for the bridge (~L388, ~L468).
- `src/ptest/runtime/pytest_bridge.py` (stdlib-only, run as a script; runtime dir is
  `sys.path[0]`, inserted for xdist workers ~L3354, so a sibling stdlib module
  `runtime/selection_recorder.py` is importable). Project hooks
  `pytest_runtest_protocol`/`pytest_sessionfinish` from registered modules are
  refused: the recorder lives inside the bridge's own `OwnedPlugin` (L1600).
- `src/ptest/contracts.py` — `SelectionPolicy` L508. Adding `dynamic` must NOT
  change `_policy_digest` (= sha256(repr(config.selection)), selection.py L14 and
  operations.py L360) for the default: use `field(default=True, repr=False)` or
  equivalent, else every existing baseline/verified proof is invalidated.
- `src/ptest/config.py` — `[selection]` parse L484-526, render L1214 (render
  `dynamic` only when false).
- `src/ptest/history.py` — `_PERSISTED_REASON_ALIASES` L1866 (N9). Prefer no new
  persisted reason codes at all; any new code is aliased here.
- `src/ptest/monorepo.py` — `route_scopes` L292 (first-segment match → longest
  declared prefix, 6.8).
- `src/ptest/uninstall.py`, status in `cli.py` — remove/show the store.
- `src/ptest/storage.py` — `open_database` (SQLite open, hot-journal recovery):
  reuse, do not fork. `src/ptest/source.py` `_mac`/`_key` — keyed digests.
- `src/ptest/verified.py` — `projects/<project_id>` state dir convention.
- Docs: `docs/ptest-agent.md`, `src/ptest/resources/agent-guide.md`,
  `src/ptest/resources/repository-agent-guide.md`, `README.md`, `docs/changelog.md`
  (start-line strings also live in `src/ptest/progress.py`).
Untouched: `src/ptest/selection.py` (advanced/closed-inputs/groups/shadow), Vitest.

New:
- `src/ptest/selection_model.py` — frozen shared dataclasses (index, records,
  dependency-file schema, selection result).
- `src/ptest/source_index.py` — per-content AST index + fingerprints + resolver.
- `src/ptest/selection_planner.py` — 5.1-5.8 + self-audit miss computation.
- `src/ptest/selection_store.py` — `projects/<id>/selection.db`.
- `src/ptest/selection_ingest.py` — read dependency files, write deselect binding.
- `src/ptest/runtime/selection_recorder.py` — sys.monitoring/audit recorder.
- `scripts/selection_eval.py` — evaluation harness (built + unit-tested; the A1-A5
  campaign is NOT run in tasks).
- tests under `tests/ng/` (see task list).

## Hard rules
N1-N13 and the section 8 abuse table are acceptance criteria; each abuse case gets
a test seen failing first (secure-by-spec). N9: no new persisted RunResult fields;
a test decodes persisted rows with a frozen 0.4.10 field/code list. Python < 3.12
test processes: static planner only. Planning never imports project code (N11).
No version bump, tag, push, or `ptest update`. `graphify update .` after source
changes (no semantic extraction). graphify-out/ exists in the main checkout.
