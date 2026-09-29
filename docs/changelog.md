# Changelog

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
