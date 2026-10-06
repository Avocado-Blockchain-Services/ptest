# Changelog

## Unreleased

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
