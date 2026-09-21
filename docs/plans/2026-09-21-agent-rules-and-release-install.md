# Agent Rules and Release Install Implementation Plan

**Goal:** Add safe, opt-in repository instructions for coding agents and a
self-contained release-bundle installation path.

**Architecture:** A new pure rules planner derives stable target documents from
the resolved repository root; CLI preview/apply is its narrow write boundary.
The release wrapper delegates all wheel verification and atomic publication to
the existing Python installer.

## Task 1: Repository agent rules

**Files:** `src/ptest/agent_rules.py`, `src/ptest/cli.py`,
`src/ptest/resources/repository-agent-guide.md`, `tests/ng/test_agent_rules.py`,
`tests/ng/test_cli.py`.

1. Write tests for preview purity, apply output, idempotency, preservation of
   pre-existing files, rejection of symlinks/non-regular files/malformed blocks,
   and required guide content.
2. Run the focused tests through ptest and observe missing-command failures.
3. Implement a bounded planner plus descriptor-safe writes that create only the
   canonical guide and delimited references; expose `ptest rules` and
   `ptest rules --apply`.
4. Re-run focused tests through ptest.

## Task 2: Release-bundle installer

**Files:** `install.sh`, `scripts/build-release-bundle.py`,
`docs/installation.md`, `README.md`, `tests/ng/test_install.py`.

1. Write tests that a release layout installs through `install.sh` with no
   wheelhouse arguments, a source checkout refuses convenience mode, and a
   failed convenience upgrade retains the existing target.
2. Run the focused tests through ptest and observe the missing behavior.
3. Add the local bundle builder and wrapper argument routing; preserve explicit
   `--dest --wheelhouse --manifest` behavior and delegate validation/cutover to
   `scripts/install.py`.
4. Document archive download/checksum verification and offline installation.
5. Re-run focused tests through ptest, then one `ptest --full` gate.
