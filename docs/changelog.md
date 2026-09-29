# Changelog

## Unreleased

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
