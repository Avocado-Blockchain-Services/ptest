# ptest init Monorepo Bootstrap Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `ptest init` from a repository root initialize a safe single-project or monorepo configuration and optionally add repository-local agent guidance without overwriting user-owned files.

**Architecture:** Extend the existing initialization path with a bounded bootstrap planner. It resolves the Git root, uses current-directory native evidence for single projects, and inspects only immediate non-symlink child directories when root evidence is ambiguous. It writes a root v2 dispatcher plus missing child v1 configs atomically after complete validation; runtime dispatch remains unchanged.

**Tech Stack:** Python 3.11–3.14, argparse-like closed CLI grammar, TOML, pathlib/lstat, pytest through `ptest`, packaged Markdown guidance.

**Spec:** `docs/specs/2026-09-21-ptest-init-bootstrap-v01.md`

## Global Constraints

- Existing valid child `.ptest.toml` files remain authoritative and byte-identical.
- Bootstrap inspection is bounded to the Git root and immediate child directories; runtime execution performs no discovery or glob expansion.
- Never follow symlinks, write outside the resolved repository root, execute runners/package managers, use the network, or spawn nested ptest processes during init.
- Validate all planned targets before the first write; dry-run and `--agents none` remain pure.
- Existing agent text, guides, provider files, and trust settings are preserved; provider integration is repository-local and opt-in.
- All tests run through `ptest`; run `graphify update .` after source changes and one final `ptest --full`.

## Review Focus

- A root invocation must write at the Git root while child-local initialization remains compatible; Task 1 tests both boundaries.
- An immediate child containing a symlinked native config must not become a candidate; Task 1 tests non-following candidate inspection.
- A later invalid child must cause zero writes, including no first child config; Task 2 tests two-phase planning.
- Existing agent files with conflicting managed delimiters must block all writes; Task 3 tests rules integration preflight.
- Provider choices must never create global/home files or execute installation; Task 3 tests repository-local targets and no subprocess seam.

### Task 1: Bootstrap discovery and config planning

**Files:**
- Modify: `src/ptest/config.py`
- Modify: `src/ptest/contracts.py` only if immutable init result types need new fields
- Test: `tests/ng/test_config.py`

**Interfaces:**
- `resolve_git_root(cwd: Path) -> Path`
- `plan_init(cwd: Path, options: InitOptions) -> InitPlan`
- `InitPlan` contains root, action, planned root/child writes, existing targets, and warnings; it performs no writes.
- `apply_init_plan(plan: InitPlan) -> InitResult` performs only prevalidated exclusive writes.

- [ ] Write failing tests for Git-root resolution from nested directories, current v1 single-project initialization, ambiguous root evidence, explicit child/runner pairs, immediate-child-only inspection, unsafe/symlink paths, existing child preservation, and zero writes after late validation failure.
- [ ] Run the focused tests through `ptest` and confirm failures are caused by the missing bootstrap behavior.
- [ ] Implement bounded candidate inspection using `lstat`, existing `_native_candidates`, and explicit child/runner pairs; do not reuse recursive config discovery.
- [ ] Implement v2 root manifest serialization and a two-phase plan/apply path that preserves existing configs.
- [ ] Run the focused config tests through `ptest` and confirm green.
- [ ] Commit the config planner and tests.

### Task 2: Closed CLI grammar and interactive/noninteractive selection

**Files:**
- Modify: `src/ptest/cli.py`
- Modify: `src/ptest/contracts.py` if `InitOptions` needs children/agents fields
- Test: `tests/ng/test_cli.py`

**Interfaces:**
- Parse repeated `--child PATH --runner KIND` pairs and `--agents LIST|none`.
- Preserve existing `ptest init --runner KIND`, `--dry-run`, `--reveal-command`, and JSON behavior.
- Interactive selection uses injected input/output seams so tests never read a real terminal.

- [ ] Write failing parser and CLI tests for pair validation, unknown agents, explicit single-project compatibility, root monorepo selection, and dry-run purity.
- [ ] Run the focused CLI tests through `ptest` and confirm expected failures.
- [ ] Implement the closed grammar and call the config planner/apply seam; use bounded automatic candidates and explicit pairs for ambiguous cases.
- [ ] Ensure init never calls runner, package-manager, network, or subprocess execution paths.
- [ ] Run the focused CLI tests through `ptest` and confirm green.
- [ ] Commit the CLI and tests.

### Task 3: Agent guidance and provider-local integration

**Files:**
- Modify: `src/ptest/agent_rules.py` only where the existing safe planner needs shared target planning
- Modify: `src/ptest/resources/repository-agent-guide.md`
- Test: `tests/ng/test_agent_rules.py`
- Test: `tests/ng/test_cli.py` for end-to-end init/rules choice wiring if needed
- Modify: `docs/installation.md` and `docs/changelog.md`

**Interfaces:**
- Reuse `rules --apply` validation and managed-block semantics.
- Add a pure provider integration planner returning repository-relative targets and actions.
- Supported provider set is `claude`, `codex`, `opencode`, `gemini`, and `none`; no provider writes unless explicitly selected.

- [ ] Write failing tests for review-before-edit behavior, missing-guidance insertion, idempotency, conflicting delimiters, `--agents none`, provider target containment, and no home-directory mutation.
- [ ] Run the focused agent-rule tests through `ptest` and confirm expected failures.
- [ ] Add root-dispatch guidance to the canonical guide and implement provider-local managed references without duplicating the guide body or overwriting existing files.
- [ ] Route init through the same preflight-before-write rules behavior and preserve existing `ptest rules` semantics.
- [ ] Run focused config, CLI, and agent-rule tests through `ptest`.
- [ ] Run `graphify update .` in this checkout.
- [ ] Commit guidance and integration changes.

### Task 4: Integrated gates and audit

**Files:**
- Review all task diffs and the spec/plan; no unplanned files.

- [ ] Run `git diff --check`, inspect changed-file ownership, and verify no TODO/debug residue.
- [ ] Run the complete focused init/config/CLI/agent-rule suites through `ptest`.
- [ ] Run one final `ptest --full`.
- [ ] Perform a read-only audit against the spec, including negative contracts and direct-operation/no-subprocess evidence.
- [ ] Record verification results in the final handoff; do not push, publish, deploy, or replace the installed CLI without explicit instruction.
