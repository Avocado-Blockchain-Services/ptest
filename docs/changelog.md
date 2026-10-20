# Changelog

## 0.7.0

- Tiered iteration selection, one gate per spec (spec 2026-10-19). For
  pytest projects, a bare `ptest` now runs only the tests that are certain to
  matter (tier T1) and defers the rest (tier T2: shared-file, data and
  static-reach guesses). Vitest changed delegation is unchanged. Five parts,
  numbered as in the spec:
  - P1: evidence tiers. Dynamic iteration runs never widen to a large static
    or ratio-driven set (the static fallback still widens by `full_ratio`);
    the old `full_reason` widening is gone, and what was deferred is
    reported by reason and by file.
  - P2: observers. The gate (`ptest --full`) runs the deferred tests, learns
    which of them really depend on the change (learned observers), and
    unlearns demoted ones. `[selection] observers` declares paths whose
    dependents are always checked in iteration; learned observers are kept
    per node (64 per node, 100000 per store).
  - P3: records chosen by tree distance. A node keeps up to K = 3 records,
    and planning uses the record closest to the current tree.
  - P4: hint, guide and JSON. The end lines, `run.json` `data.selection`
    (nullable, additive) and the agent guide describe the loop: iterate with
    `ptest`, gate once at spec end.
  - P5: `[selection] ambient_fingerprint` is a command whose output digest
    says whether ambient state changed; unchanged means the deferred tests
    need not run, a changed or absent digest promotes them. It runs with no
    shell, a 60 s limit and capped output. The guard manifest gains
    `attempt_decision_timeout_s`; guard protocol version stays 2.
- Selection store v2. A v1 store is migrated in place in one transaction
  (nodes keyed by node id and run, `runs.fingerprint`, an `observers` table,
  gate marker keys); the migration is idempotent and loses no row. Each node
  keeps up to K = 3 records. The store target is 96 MiB with a 128 MiB cap.
  Dependency records and the policy digest are unchanged for an unchanged
  `.ptest.toml`, so existing records keep matching. A 0.6.x ptest opening a
  v2 store plans statically and records nothing (`coordinator-corrupt:
  selection store has an unknown schema`, or `capacity-exceeded` over its
  smaller cap); it never deletes the store.
- Config compatibility. 0.6.x treats `observers` and `ambient_fingerprint`
  as unknown `[selection]` keys, so a config carrying either gets the
  `policy-invalid` warning and automatic selection is disabled there (it is
  not an error in 0.6.x). In 0.7.0 a bad value of either key is
  `invalid-config` (exit 2). Adding either key costs one full re-seed; a
  config without them keeps its 0.6.8 records.
- Planning latency. On a synthetic project (200 modules, 20000 recorded
  nodes with 3 records each, one function body edited) planning took a warm
  median of 3.13 s, against 2.99 s before this release (about 0.14 s added).
  The 1.5 s warm budget from the 0.5 spec is not met on that synthetic
  project, before or after this release. No real repository was used.
  With empty-diff planning (R10) a bare `ptest` at a monorepo root now plans
  each clean pytest child, so it can take a few seconds per child where
  0.6.8 printed `no changes` at once.
- Under pytest-cov (the advanced bridge path) selection now records real test
  outcomes; before, every node was recorded `unknown`. One-time upgrade cost:
  the first bare `ptest` after upgrading such a project reruns those
  previously-`unknown` nodes once (once per clean monorepo child); later runs
  are normal.
- Not run: the bounded real-repository step (fullon2 `api` child) is pending
  the user's approval of each `ptest --full`; this release was verified on
  fixture repositories only.

## 0.6.8

- Test selection keeps a full run's dependency records. Before, the next
  small run (any checkout of the project) evicted them oldest-first to meet
  the store's size target, so selection fell back to the static rule and a
  change to a widely imported file ran the whole suite again, every time.
  In persea, one scoped run cut 14,328 recorded tests to 1; with the
  records kept, the same change selects 37 tests instead of 14,328. Under
  size pressure the store now drops cached file versions that no record
  references first (spec 2.5), then the runs holding the fewest records,
  and never trades a larger run for a smaller newer one.

## 0.6.7

- `ptest plan` at a monorepo root no longer dies on an `AssertionError`
  traceback with exit 1 (an agent in persea read it as a test failure). It
  prints the static preview and one line per child (`api · full (static
  preview)`); `ptest plan --json` returns the same plan document as in a
  single project.

## 0.6.6

- `ptest doctor --fix` no longer declares a whole directory when ptest's
  own byproducts live in it too. In fullon2's integrations it drafted
  `launchpad/__pycache__` for one stale `.pyc` (its test was deleted),
  which would have made the current bytecode beside it a fingerprinted
  input; it now drafts only the stale file.
- `ptest doctor --fix` says when it could not check a project's gitignored
  files (for example `input fingerprint byte limit exceeded` for a large
  `.next` build directory) instead of drafting nothing silently.

## 0.6.5

- A run in the full mode no longer ends `incomplete (exit 70)` because the
  checkout's history file is an empty leftover with the wrong permissions
  (a zero-byte `history.sqlite3`, mode 0644, made by some other program;
  136 checkouts on one machine had one). The full mode is the first to open
  that file, so `reuse = true` exposed it: muse-msp passed all 48 tests and
  recorded nothing. A writer now moves such a file aside
  (`history.sqlite3.empty-0644.<time>`) and starts a fresh private store; a
  reader treats it as "no history yet" and moves nothing. A store with
  content, a link or a hard-linked file is still refused.
- A writer now refuses a history store that is not private (mode 0600), as
  every reader already did; before, it appended to a world-readable store.
- When history cannot be saved, the end line says why (for example
  `path …/history.sqlite3 must be mode 0600`), not only "advanced attempt
  history could not be committed".
- Test suite: a guard test no longer depends on how fast the machine's
  whole process table can be scanned; it failed while another project's
  run was in progress.

## 0.6.4

- Gitignored tool output no longer blocks the full mode's baseline:
  `graphify-out`, `.ruff_cache` and `.mypy_cache` at the project root (or a
  monorepo child's root) count as outputs no test reads, without any
  configuration. A tracked file under them stays an input, and a config
  entry that names them wins (`ignored_inputs = [".mypy_cache"]` keeps it
  fingerprinted). Existing baselines stay valid. Found in the 0.6.3 reuse
  trial: specvm passed `ptest --full` twice and recorded nothing because of
  `graphify-out/`.
- `ptest doctor --fix` declares the remaining gitignored files the run
  counts as test inputs: directories with a valid `CACHEDIR.TAG` go to
  `non_input_outputs`, anything else to `ignored_inputs`, where it is
  fingerprinted. `.env` files and symlinks are named, not declared: tests
  may read a `.env`, and a worktree's `.env` is a symlink, which
  `ignored_inputs` refuses. To let the full gate reuse a green run anyway,
  list it in `non_input_outputs` and run `ptest --full --again` after
  editing it. The run's "gitignored
  paths count as test inputs" line now names `ptest doctor --fix`. The
  once-per-version "can improve this config" notice ignores these drafts.
  Spec: `docs/specs/2026-10-12-tool-outputs.md`.

## 0.6.3

- New `[runner] reuse = true` (pytest): puts a project that declares no
  coverage in the full mode, so a clean `ptest --full` records a baseline and
  a repeat on the same code says "already verified" in seconds; an unchanged
  monorepo child is skipped and a fast-forward merge of a green tip is not
  rerun. Before, only declaring coverage did that. Nothing changes for a
  config without the key. The full mode is stricter: if it refuses a project,
  the end line says `fix the cause above or remove reuse = true from
  <config>`. ptest 0.6.2 and older reject the key. `ptest doctor` suggests
  it; `doctor --fix` never sets it. Spec: `docs/specs/2026-10-12-reuse-opt-in.md`.

## 0.6.2

- Faster runs on projects with a long history: a run read its history
  five times and re-validated every stored snapshot each time (about 2.3 s
  per read on a 2,600-file project). A stored run is now validated once
  per process; every read still queries the store, so a row another run
  added or changed is validated again. Digest checks are also faster.
- `ptest doctor --fix` writes a new key (such as `coverage_args`) among
  the table's own settings instead of after the blank line and comments
  that lead into the next table, and replaces a value written over
  several lines whole: an `args` array spanning lines is now migrated
  instead of refused as "cannot be patched safely".
- Test suite: a process-exit check no longer fails when the process is
  reaped between opening and reading its `/proc` entry.

## 0.6.1

- Fix: `ptest --cov` on a project that declares coverage only in
  `[runner] coverage_args` could end `incomplete` (exit 70,
  changed-during-run), because the `.coverage` data file the run wrote
  counted as a source change. Declared `coverage_args` now make coverage
  data files tool byproducts, as `--cov` in `args` always did.
- `ptest doctor`'s Test policy section reads a `--cov-fail-under` gate in
  `coverage_args`, and a `--cov` run's command summary lists the coverage
  options it added.

## 0.6.0

- Coverage is on request. Coverage options now live in `[runner]
  coverage_args` and reach pytest only when a run asks with `--cov`, so the
  edit loop (`ptest`, `ptest <path>`) and plain `ptest --full` no longer pay
  for coverage. Run the handoff gate as `ptest --full --cov`. `coverage_args`
  accepts pytest-cov options only (`--cov`, `--cov-report`, `--cov-config`,
  `--cov-branch`, `--cov-fail-under`, `--cov-context`); anything else is a
  config error.
- Declaring coverage no longer measures it. The advanced profile (baselines,
  verified reuse, recorded selection) is still chosen by declaring coverage,
  now in `coverage_args` too, so every project keeps its tier. Runs without
  `--cov` stay on that profile, and pytest-cov is required only by runs
  that measure coverage. Doctor's selection advice now names `coverage_args`.
- `ptest doctor --fix` moves `--cov` options from `args`/`full_args` to
  `coverage_args`. After upgrading, ptest says once when a config would
  change ("can improve this config … run ptest doctor --fix"). A config that
  keeps `--cov` in `args` still measures coverage on every run, as before.
- Reuse respects coverage. `ptest --full --cov` counts as already verified,
  verified elsewhere, an unchanged monorepo child, or a joinable running run
  only when that green measured the same coverage on the same inputs; a plain
  `--full` can reuse either. A project with coverage configured ends a plain
  green with `· next: ptest --full --cov before handoff`. `--cov` on a project
  with nothing to add prints `coverage not configured` and runs anyway.
- One-time cost after upgrading: the first changed run on a pytest project
  runs its full suite once, because stored baselines from 0.5 are no longer
  compatible (the runtime identity stopped counting coverage).
- Agent guidance: the handoff row is `ptest --full --cov`, and `--cov` is
  never passed while iterating. Update installed copies with `ptest rules
  --apply`.

## 0.5.3

- Agents get clearer guidance on writing tests. The installed guide has a new
  "Writing tests" section: name the oracle for every test, start a bug fix
  with a failing test that reproduces it, use one table-driven hostile-input
  test where input crosses a trust boundary, and never add tests only to
  raise coverage. The existing coverage gate stays as it is. `ptest guide
  tests` prints the full checklist, and `ptest guide` now includes it.
  Update the installed copy with `ptest rules --apply`.
- Opt-in stricter test policy. `ptest init --test-policy` or
  `ptest rules --apply --test-policy` adds `docs/ptest-test-policy.md` and
  one reference line to the managed instruction block; `--no-test-policy`
  skips the question. A terminal init asks once (default No), never in CI, a
  pipe, `--json` or `--dry-run`. The files that will change are printed
  first, lines in your instruction files that name a coverage percentage are
  listed and never edited, and `ptest uninstall` removes an unedited policy
  file.
- `ptest doctor` prints a static, read-only Test policy section: the coverage
  gate you already have, branch or line-only counting, omit patterns,
  `pragma: no cover` counts and instruction lines that name a coverage
  percentage. It sends nothing, writes nothing, and changes no score or JSON
  field; Vitest thresholds are reported as not inspected.
- Agent eval: two new scenarios (a bug report that must start with a failing
  test, and a coverage gate that fails by a little), 16 in all, and a scorer
  rule that checks the order of steps in an answer.

## 0.5.2

- ptest's own repository: full runs record a baseline again. Gitignored
  build and tool outputs (`graphify-out`, the egg-info, `build`, `dist`,
  the terraform lock and the Claude scheduler lock) are declared in
  `[selection] non_input_outputs`, and the config carries the
  `ptest doctor --fix` selection policy (closed inputs over `src/ptest`
  and `tests`, full triggers for `pyproject.toml`, `uv.lock` and
  `tests/ng/conftest.py`).
- Test fix: the group CPU-sample probe runs in its own process group, so
  other workers starting processes in pytest's group no longer make the
  member count flaky.

## 0.5.1

- A folder or file run that passes no longer marks the whole project as
  tested. `ptest <folder>`, `ptest --full <folder>` and `ptest <file>` used
  to move the project's last-green point, so an untested edit elsewhere in
  the project read as "no changes" on the next bare `ptest` (seen in a
  monorepo child: a green `server/tests/fullon_log/` hid an edit to the
  `fullon_exchange` conftest). Only runs that cover every change in the
  project move the point: bare `ptest`, `--changed`, `--full`, or a folder
  that is the whole project. Failures from any run still join the
  last-failed set.
- `ptest doctor` no longer calls a project unrunnable for conftest
  `pytest_runtest_protocol` / `pytest_runtest_logreport` /
  `pytest_collectreport` hooks that only observe (time or log tests and
  return nothing). The pytest bridge already admitted them; doctor now
  applies the bridge's own scan to the source. Hooks that return a value,
  wrap the call, or write to `item`/`report` stay refused.

## 0.5.0

- Dependency-recorded test selection for pytest projects. Once a project
  has a full run behind it, changed runs execute only the tests whose
  recorded code, data or fixture dependencies changed, down to single test
  node ids inside a file (G1). Anything unknown or unprovable falls back to
  the 0.4 static rule per test, or to the full suite when the whole picture
  is unknown; falling back is never an error (G2). Recording runs on every
  pytest run with a bounded overhead, and planning reuses a per-file parse
  cache (G3). The start line states the engine, test and file counts and
  the main changed units, e.g. `changed: <path> (+N files) → T tests in F
  of M files (dynamic · …)`; without usable records the static fallback
  prints `… → N of M test files (static: <reason> · D direct · V via
  importers)` (G6). A changed green, dynamic or static, is iteration only;
  only `ptest --full` completes the change. Disable with `[selection]
  dynamic = false` (default true). ptest 0.4.10 and older reject that key,
  so a team on mixed versions upgrades before committing it.
- Self-audit on every full run (G4): ptest checks whether the selector
  would have missed any test that failed and prints `ptest: selection
  audit: N failing tests would not have been selected — they now run
  whenever a change statically reaches them`; the affected tests are
  demoted to the static rule. A builder worktree's records serve the gate
  worktree on the same code, so unchanged code reports `… → no tests
  affected: R tests reach these changes and already passed on this code`
  without running anything (G5). `scripts/selection_eval.py` is the
  committed seeded mutation harness behind the release thresholds (G7).
  Measured on persea's control-plane (1002 tests, xdist, FastAPI) from one
  recording: 150 seeded mutants planned; ground truth from full runs for
  24 of them, weighted to the import-time classes. Zero confirmed misses
  (every other candidate passed on a mutant rerun: load flakes), where the
  0.4.10 static plan missed failing tests on 5 of the 6 mutants first
  measured, among them a dynamically loaded router (18 of 18) and a
  migration edit (585 of 585). Median tests selected,
  0.4.10 → 0.5: function bodies 199 → 73 (comparison) and 271 → 77
  (raise), import edits 331 → 15, defaults 309 → 103. Decorators,
  framework classes and constants they read still run every test that
  shared the import (route tables and schemas are read without running
  project code), where 0.4.10 often ran fewer and missed. The full
  150-mutant truth campaign and the overhead benchmark (A4) were not run.
- State built once per process counts for every test that reuses it: code,
  lazily imported modules and data files first run by one test, caused by a
  caller or fixture that later tests also run, are dependencies of those
  tests too, across xdist workers of the same run (a template database one
  worker migrates and the others reuse, a memoised engine, an `lru_cache`
  loader). Tests that start the project's
  own interpreter (`python -m pkg`, a checkout script) follow that entry
  module's imports under the static rule.
- Changes only callers can notice select like body changes: a new default
  of a plain function or method, or an attribute of a plain class, runs the
  tests that ran that code instead of every test that imported the module.
  Decorated routes, framework models, computed defaults (`Depends(...)`,
  `Query(...)`) and dunder methods keep the import-time rule, and a module
  that no selected test still imports falls back to it, so an edit that
  breaks the import is still caught. The parse cache re-indexes once.
- A full run that records no baseline because gitignored files inside the
  scope count as inputs now names them and the settings that declare them
  (`[selection] non_input_outputs` or `ignored_inputs`), instead of
  "uncommitted changes".
- Nested monorepo child scopes: `route_scopes` matches the longest declared
  child prefix, so `ptest services/control-plane/tests` and `ptest --full
  services/control-plane` reach the nested child, and `services/api-v2`
  no longer routes to `services/api`. Sibling and error behavior is
  unchanged.
- History compatibility (N9): no new persisted RunResult fields and no new
  persisted reason codes; 0.4.10 decodes everything 0.5 writes.

## 0.4.10

- Post-test stall detection (pytest runner only): once the tests have
  finished and the runner processes then stay CPU-idle for `[runner]
  stall_timeout` seconds (default 120, `0` disables, otherwise 10..86400),
  the run ends incomplete (exit 70) with the new `post-test-stall` problem
  code instead of holding queue slots. Rerun once alone; report a repeat
  with the stack dump. There is no CLI flag.
- Stack dumps before guard kills: on a compound/attempt deadline kill or a
  post-test stall, the pytest controller and every xdist worker first write
  all-thread stacks (other processes, and non-pytest runners, write none);
  ptest prints them on stderr as `ptest: stack dumps (<n> processes) —
  <reason>`, bounded and escaped, never in `--json` or history. A dump the
  `uv run` launcher made a process write twice prints once. The agent guide
  names the `post-test-stall` row and the `[runner] stall_timeout` key.
- History stores a stalled run's reason as `state-unavailable` (message
  kept), so a 0.4.9 ptest that reads the same history during an update or
  after a downgrade does not disable it as `coordinator-corrupt`.

## 0.4.9

- A ptest process that died mid-write left a hot SQLite rollback journal,
  and every read-only command (`ptest status`, `history`, `uninstall`) then
  reported the healthy coordinator or history store as `coordinator-corrupt`
  until something wrote to it. Read-only opens now let SQLite roll the
  journal back to the last committed state once and retry. A live writer's
  journal is never touched, recovery never creates a missing database, and
  real damage still fails closed.

## 0.4.8

- Reuse controller-verified native Vitest whole-suite successes for unchanged
  `ptest --full` runs, including unchanged monorepo children and worktrees.
  Preserve literal runner arguments and full native parallelism. Evidence
  binds current source, configuration, policy and installed runtime bytes;
  it never fabricates a pytest inventory or coverage baseline. Initial
  qualification supports standard npm installations without executable
  Vitest/Vite configs. Unsupported inputs run normally without reuse.
- Failed or incomplete full reruns withdraw reusable evidence. Preexecution
  publication tokens prevent an older delayed success from restoring it
  after another checkout fails. Shared ledger version 2 declines old
  version-1 evidence; a genuine new full success can establish proof again.

## 0.4.7

- A run started inside a sandbox with its own PID namespace (Codex,
  bubblewrap, `unshare`) no longer holds its slots forever after it ends,
  and a sandboxed ptest no longer frees slots that host runs still use.
  Each run now holds a private lease lock file for its lifetime (owner and
  guard only; the test runner never inherits it) and records which PID
  namespace it came from. ptest judges a run from another namespace by
  that lock alone and frees it only once the lock is free; a missing or
  replaced lock file never counts as proof. A run from another sandbox
  cannot be checked for processes it left behind, and runs started by
  0.4.6 or older are still judged by process IDs. The coordinator database
  is unchanged, so older installed versions keep working alongside.
- A recorded process ID now owned by a different process (a reused PID,
  or a kernel thread seen from outside a sandbox) counts as proof the run
  ended, instead of keeping its lease uncertain forever. Escaped or
  unreadable descendants still keep it held.
- New `ptest release <run_id>` frees a stuck run once its owner, guard and
  lease lock are provably gone, prints what it freed, and refuses
  otherwise; `--force` applies only to runs granted more than 25 hours
  ago and never frees a held lock. The agent guide names it as the step
  for a repeated `ownership-uncertain` line.
- Ctrl-C on a busy machine no longer ends a healthy run as incomplete
  (exit 70): after the cancel reaps the run's processes, ptest allows
  exiting processes a little longer (1.5 s) to settle before it judges
  the run. Escaped or unreadable processes still make it incomplete.
- At a monorepo root, `ptest --full` skips a child whose inputs are
  unchanged since its last green full run, even after new commits
  elsewhere in the repository: `ptest: <child> · unchanged since green at
  <sha> (<age> ago) · skipped — ptest --full --again to rerun`. A child's
  inputs are its own files and `.ptest.toml`, the root `.ptest.toml`, and
  its declared `full_triggers`; a change to a shared dependency file
  outside every child (`uv.lock`, `pyproject.toml`, `package.json`, …)
  reruns it. The total line counts skipped children separately
  (`N children skipped (unchanged)`), never as tests, and a skip records
  no new green. Tests that read files in a sibling child must declare them
  in `full_triggers`; `ptest -v` lists the inputs behind each skip.
  Single-project repositories still rerun the full gate on every new
  commit.

## 0.4.6

- Refreshing agent guidance works without a terminal. The outdated-guidance
  line now says `run ptest rules --apply to update`; `ptest rules --apply`
  also rewrites the older ptest skills already installed (never a new one,
  never one you edited). `ptest init` run by an agent or in CI rewrites
  only the older guide and skills (no instruction-file edits); a problem
  there is one line, never init's result. Before, only `init`'s interactive
  prompt refreshed guidance, so an agent following the hint changed nothing.
- `ptest rules` works at the repository root (as `init` does) even when run
  from a subfolder, so following the warning never scatters guidance copies.
- The warning appears only when that refresh can actually clear it: it
  uses the same file checks as the refresh, so a skill ptest refuses to
  rewrite (for example in a group-writable directory) no longer keeps the
  warning on forever.

## 0.4.5

- Agents stop making worker and timeout judgement calls. The agent guide
  now says to run `ptest` / `ptest --full` exactly as shown and never to
  add `--workers`, `--timeout` or `--queue-timeout` (or copy them from
  older notes): ptest sizes workers from the project and the machine-wide
  queue shares the load. ptest's own timeout line no longer says "raise it
  with --timeout": it says "rerun once unchanged (history raises the next
  limit)" when the limit is dynamic, or "this limit is fixed … report it"
  for a config, `--timeout` or 6 h-ceiling limit. The lasting fix is
  `[runner] timeout` / `full_timeout`. Exit 75 is rerun once, then report.
- `--workers N` below the project's own count prints `ptest: --workers N
  caps this run below the project's M workers …`, so a cap is visible.
- The default `--queue-timeout` is 4 hours (was 30 minutes): waiting in the
  FIFO queue is correct, and a busy machine no longer pushes callers to set
  it. Waiting lines show whole hours (`queue timeout 4h`) and repeat at
  15 s, 30 s, 60 s … up to every 5 minutes, so a long wait stays short.
- `ptest help run` no longer shows `--workers 2` as its example, and the
  agent help no longer offers `--workers` as a lever.
- Run `ptest init` to refresh the agent guidance in your repositories
  (ptest says `agent guidance is outdated` until you do).

## 0.4.4

- A leftover `REBASE_HEAD` no longer blocks change selection or the full
  gate (`in-progress Git operation prevents selection`). Git can leave it
  behind after a rebase completes; like `git status`, ptest now treats a
  rebase as in progress only while `rebase-merge/` or `rebase-apply/`
  exists. Selection still fails closed while `MERGE_HEAD`,
  `CHERRY_PICK_HEAD` or `REVERT_HEAD` exists.
- Test suite: the update-network guard records every hit and fails the
  test at teardown, so a hit inside the startup check's fetch thread can
  no longer pass silently; its loopback check parses the URL.

## 0.4.3

- Fix: in 0.4.2 the real update transport was miswired, so `ptest update`
  always reported `could not reach the ptest releases on GitHub` (exit 75)
  and the startup check never saw a new release. **If you run 0.4.2,
  update once with `get.sh`**:
  `curl -fsSL https://raw.githubusercontent.com/Avocado-Blockchain-Services/ptest/main/get.sh | sh`; from 0.4.3 on,
  `ptest update` works. The test suite now guards the network edge rather
  than replacing the transport, and exercises the real transport end to
  end against a loopback server.

## 0.4.2

- `ptest update` installs the latest release in place: `ptest update
  [--check] [--version X.Y.Z] [--json]`. The bundle downloads over HTTPS
  from the GitHub release only, its SHA-256 is verified before anything
  runs or extracts, downloads are capped at 64 MiB, and the install runs
  the same bundled `install.sh`. Bundles install side by side and the
  launcher switches atomically; a failed update keeps the old bundle.
  `--check` only reports; `--version` installs exactly the named release;
  `--json` emits the `update` public document.
  A bare `ptest update` (or `upgrade`) run beside a path of that name
  refuses and names `ptest ./update` for its tests. System and Homebrew
  Pythons (symlinked interpreters) are accepted, as `get.sh` accepts them.
- Startup update check: before most commands ptest looks for a newer
  release at most once a day (24 h cache, 2 s network limit, silent when
  offline). At a terminal it prompts `ptest X is available (you have Y).
  Update now? [Y/n]` and re-runs your command on the new version;
  otherwise it prints `ptest: update available: X (installed Y) — run:
  ptest update` and carries on. `PTEST_NO_UPDATE_CHECK=1`, a truthy `CI`,
  `--fixture-domain` and `-q` opt out; source checkouts say `installed
  from source; update it with git pull`.
- The first run of a new ptest version in a checkout checks once whether
  `ptest doctor --fix` would improve its `.ptest.toml` (init never rewrites
  an existing config) and says so: `ptest X can improve this config
  (N changes) — run ptest doctor --fix`.
- The agent guide has a row for both lines: run `ptest update` / `ptest
  doctor --fix`, then continue.

## 0.4.1

- The offline doctor (`ptest doctor --offline`, and the fallback `ptest init`
  runs when no reviewer is chosen) no longer builds the model-review
  evidence it throws away. On a 2,000-file monorepo it took 210 s and printed
  nothing, which looked like a hang; the same output now takes about 25 s,
  most of it the static workspace scan. It prints one progress line per
  project on a terminal (`-q` silences it) and has a deadline.
- Candidate ranking for real model reviews is faster, with the same
  evidence selected.
- `ptest init`'s "Commit these files" lists every uncommitted ptest file in
  the checkout, not only the ones it wrote this run. It never lists a
  `--from-main` stopgap copy, and a dry run lists nothing.

## 0.4.0

- A green full gate is shared by every checkout of a pytest project. When a
  full gate passes and records its baseline, ptest adds it to a private,
  project-wide ledger; `ptest --full` in another checkout (a worktree, or the
  main checkout after a fast-forward merge) skips with
  `already verified at <sha> in <checkout>` when its clean tree has the same
  commit, source digest, compatibility, selection policy and position in the
  repository, and its own latest full run on that tree did not fail.
  Installed dependencies are not compared: the tracked lockfile stands in
  for them. A failed or unfinished full run on the same tree in any checkout
  withdraws the record; `--again` still forces a run. The check costs one
  source capture, plus one only for a matching record from a checkout with
  another runtime identity, however many worktrees exist.
- A pytest full gate that cannot reuse a green run says why on one line:
  `ptest: full gate runs: <reason>` (no green run yet, a new commit,
  uncommitted changes, limited source evidence, a changed policy or config,
  or a last full run on this tree that did not pass). Gitignored paths that
  count as test inputs are named, collapsed only to directories that hold no
  tracked, untracked or deleted file, with the choice of declaring them in
  `[selection] non_input_outputs` (tests never read them) or `ignored_inputs`.
- `[selection] non_input_outputs` entries may be symlinks (for example a
  worktree's `.env` shared from the main checkout); paths ptest reads stay
  symlink-free. Edits to a path declared this way need `ptest --full --again`.
- The agent guide, `ptest help agents` and the README teach the merge loop:
  bring the base branch in, `ptest --full`, fix and rerun until green,
  fast-forward, and never rerun for the merge.
- Vitest config reading treats a `//` right after a backslash (the end of a
  regex literal) as code, so a worker setting on the same line is still read.

## 0.3.8

- Vitest no longer takes the whole machine. Every Vitest run used to be
  admitted as exclusive, so with several agents one Vitest run at a time
  stalled every other ptest run. ptest now bounds Vitest 3+ to the granted
  slots through its environment (`VITEST_MAX_WORKERS`, `VITEST_MAX_THREADS`,
  `VITEST_MAX_FORKS`, with `VITEST_MIN_THREADS`/`_FORKS` = 1), which caps
  every pool on Vitest 3, 4 and 5 including 4.x projects, and admits it like
  pytest: the project's `workers` when above 1, else half the machine, and
  never above a literal worker ceiling in the Vitest config. Unreadable
  worker settings, `--config`/`--pool`/worker flags, Vitest 3 workspaces and
  unknown versions keep the exclusive run. Vitest suites may now overlap
  other runs: declare `[resources] locks` for a shared fixed port or
  database. The first capped full run of a suite that used to get every
  core may need longer than its history deadline; a deadline kill raises
  the next one.
- The waiting line names what a queued run really waits for when slots are
  free: an exclusive run holding the machine, a run that needs the whole
  machine, the job limit, a held lock, the same checkout, or earlier queued
  runs. `waiting for N slots (M of L free)` now only appears when slots are
  the shortfall, and never asks for more slots than the machine has.

## 0.3.7

- A scoped or changed-tests run no longer takes its execution deadline from
  an earlier, smaller scoped run. History does not record which scope a run
  covered, so a 5-test run could set a 60 s deadline that killed a healthy
  larger run (and its retry at three times that). Non-full runs now derive
  the deadline from the latest full run (looking back up to 200 runs), else
  from the test-count estimate; a scoped run killed by its deadline still
  raises the next scoped deadline, and never lowers it.
- An expired queue deadline always reports
  `queue-timeout: admission queue deadline expired` (retryable, exit 75),
  whichever part of ptest notices it first. JSON error documents for it now
  carry phase `execution` and `retryable: true`.
- Test suite: load-sensitive tests fixed at their cause. The cancellation
  test no longer races real pytest teardown against the 3 s cancel grace,
  the invoke helper rejects hang guards under 20 s unless the timeout itself
  is under test, and the doctor byte-cap test no longer depends on the scan
  deadline. Three consecutive full runs pass at load average 20-24.

## 0.3.6

- `config-uncommitted`: in a linked git worktree whose main checkout holds
  a `.ptest.toml` at the same path, ptest refuses with exit 2
  instead of running. The fix is to commit the config on the base branch
  (or, if it is already committed there, to update the branch from it);
  never run `ptest init` in the worktree.
- `ptest init --from-main` copies the main checkout's config verbatim as a
  temporary stopgap that may go stale; it cannot be combined with `--runner`
  or `--child`.
- A successful `ptest init` ends by listing the files to commit
  (`commit_paths`, also in `init --json`): worktrees and clones only get
  committed config.
- A run warns once on stderr when its config is not committed
  (`new worktrees won't have it`), and `ptest doctor` lists uncommitted
  ptest files with the commit-on-the-base-branch fix.
- The agent guide, `ptest help init`, `ptest help agents`, and the README
  teach the stop rule: `config-uncommitted` means stop and tell the user,
  never run `ptest init`.

## 0.3.5

- Licensed under MIT (`LICENSE`, declared in the package metadata).
- `CLAUDE.md` imports `AGENTS.md`, so Claude Code reads the same repository
  rules as other agents.

## 0.3.4

Tested against 14 open-source projects (click, attrs, pluggy, flask,
requests, django-rest-framework, fastapi, pydantic-settings, rich, httpx,
ufo, h3, uuid, itoa, full-stack-fastapi-template); none of the first eight
ran out of the box before this release.

- Supported version ranges instead of exact pins: pytest 8-9, pytest-xdist
  3.5+, pytest-cov 5-7 with coverage 7.
- Projects without a uv.lock get a setup-built `.venv` (uv venv + uv pip
  install of the project and its test deps from a requirements file, an
  extra, a dependency group or Poetry dev deps). uv projects whose pytest
  sits in a non-default group get `--group` added.
- Installed pytest plugins run (pytest-django, pytest-codspeed, ...);
  plugins that re-run or distribute tests stay refused, naming the plugin
  and the `-p no:<name>` escape.
- Vitest setup follows the lockfile (pnpm, yarn, bun, npm); a missing
  vitest is refused with the install command.
- Go (`go test ./...`) and Cargo (`cargo test --tests`) suites run.
- Gitignored writes during a run (logs, coverage data, caches, husky
  hooks) no longer mark it incomplete; tracked and untracked changes do.
- Test roots skip benchmark/docs/example folders and folders the project
  ignores in addopts; a workspace root with one runner child becomes a
  one-child monorepo; init errors say what was found and what to run.
- Committing the green dirty state unchanged is not a change.
- Removed the coverage-era `--changed-setup` init flow.

## 0.3.3

- Command model: `ptest` (changed tests, whole repo), `ptest <folder>`
  (changed tests under it), `ptest <file>[::test]` (always runs),
  `ptest --full <folder>`, `ptest --full` (the gate). Any path routes to its
  nearest `.ptest.toml`. Ctrl-C prints only `cancelled`.
- Bare `ptest` ignores build output and non-code files outside source/test
  areas.
- `ptest doctor --fix` turns on graph selection for pre-0.3 configs and
  widens `test_roots` that miss test files pytest itself collects.
- `init` derives pytest `test_roots` from `testpaths` or every top-level
  directory holding tests, so `--full` is really full.
- Common pytest plugins run under ptest (hypothesis, schemathesis,
  pytest-order, sugar, instafail, faker, mock); a refused plugin names the
  hook and the `-p no:<name>` escape. Conftests may re-export hooks from
  project source and observe tests (timing/logging hooks).
- Setup writing gitignored outputs (husky's `prepare`) and hypothesis'
  `.hypothesis/` database no longer end runs `changed-during-run`.
- One-line installer `get.sh` for macOS and Linux, with release bundles for
  linux-x86_64, linux-aarch64, macos-arm64 and macos-x86_64.
- Verified on real monorepos: persea_content_maker_unified and
  fullon2_integrated (7,118 + 2,464 + 1,064 + 442 + 7 tests under
  `ptest --full`).

## Unreleased — ptest NG

- Added bounded local acceptance and benchmark evidence harnesses.
- Added explicit support and performance boundaries for Linux, macOS,
  independent adoption, runner profiles, and coding-agent CLI use.
- Failed, capped, unavailable, and not-run attempts remain distinguishable;
  no remote testing or provider API was added.
- `ptest init` now anchors at the Git root, bootstraps bounded monorepo child
  configs, and can add opt-in repository-local agent skill references.
- Doctor now reports parallel execution as checklist item PARALLEL-001
  (checklist grows from 11 to 12 rows), answered deterministically from the
  pytest-xdist configuration and gated on the parallel-safety items.
