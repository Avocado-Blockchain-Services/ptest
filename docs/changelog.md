# Changelog

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
