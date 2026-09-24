# Changelog

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
