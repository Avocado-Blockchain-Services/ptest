# Init onboarding and agent skills Implementation Plan

> **For agentic workers:** Use Muse for development with the subagent-driven-development workflow; Terra reviews implementation. Track steps below.

**Goal:** Produce an attractive, accurate init summary and discoverable ptest skills for Claude and Codex, including upgrades from released templates.

**Architecture:** Keep config inference and the public v1 init document intact. Carry internal configuration action records plus validated guidance actions to a pure terminal renderer. Use one provider mapping and one skill template generator.

**Tech Stack:** Python standard library, existing ptest CLI, pytest through ptest, installed Muse/Claude/Codex CLIs.

**Spec:** docs/specs/2026-09-22-init-onboarding-and-agent-skills.md, with the current-session Astra resolutions in .pipeline/out/2026-09-22-astra-spec-review.md.

## Global Constraints

- Preserve root-based commands, child config authority, public init JSON key sets, and runner inference.
- No new dependencies, network/model calls from init, global installs, source-repo smoke mutations, pushes or publication.
- Preserve user content and legacy .codex skills. Upgrade only recognized exact ptest-managed canonical templates.
- Tests run through this worktree's own .venv/bin/ptest, using uv for environment setup.
- Muse owns all runtime/test edits. Orchestrator owns plan, audit, evidence, and smoke orchestration.

## Review Focus

- Legacy Claude skills must upgrade rather than conflict; edited skills must survive untouched (Task 1).
- Existing invalid config must never produce a successful initialized header (Task 1).
- Unicode, long paths, control bytes, and no-agent selection must yield bounded truthful output (Task 1).
- Existing AGENTS -> CLAUDE aliases and mid-write failures must preserve user content (Task 1).
- Fresh native agent sessions must discover the skill without being told its path (Task 2).

## Task 1: Init implementation (one Muse worker, shared files sequentially)

**Owned files:** src/ptest/agent_rules.py, src/ptest/cli.py,
src/ptest/config.py, src/ptest/contracts.py (internal defaulted action field
only; do not change serialized JSON), src/ptest/render.py or a focused
src/ptest/init_render.py, tests/ng/test_agent_rules.py, tests/ng/test_cli.py,
tests/ng/test_init.py, tests/ng/test_render.py or tests/ng/test_init_render.py.
Update docs/installation.md for visible behavior if needed. No unrelated files.

**Interfaces:** keep preview(root, *, agents=()) and apply(root, *, agents=()).
Add defaulted internal action/status information to their dataclasses as needed;
retain .actions for callers. Keep init_project(cwd, options) -> InitResult and
serialize_init_result(result) unchanged in public shape. A pure renderer consumes
the completed InitResult, completed RulesResult or preview RulesPlan, dry-run and
selected-provider context; it returns one bounded string and never reads files.

- [x] Add behavior tests before implementation and capture failure:

```python
assert main(("init", "--runner", "pytest", "--agents", "codex,claude")) == 0
for directory in (".agents", ".claude"):
    text = (tmp_path / directory / "skills/ptest/SKILL.md").read_text()
    assert text.startswith("---\nname: ptest\n")
    assert "description:" in text.split("---", 2)[1]
assert not (tmp_path / ".codex/skills/ptest/SKILL.md").exists()
```

- [x] Cover fresh and existing single/monorepo roots, existing/missing child
  configs, all/none/provider subsets, TTY JSON without input(), preview/no writes,
  repeat-init with unchanged bytes, exact legacy Claude upgrade, legacy Codex
  preservation, edited skill conflict, unsafe paths, managed aliases, and
  injected mid-write failure with owned-change rollback. Assert both real file
  state and corresponding reported actions; do not merely count status glyphs.
- [x] Run `.venv/bin/ptest tests/ng/test_agent_rules.py tests/ng/test_init.py tests/ng/test_cli.py`
  and retain the expected regression failures.
- [x] Implement canonical paths and front matter. Required body behavior:
  read repository-root docs/ptest-agent.md, scoped tests via root ptest with
  child prefix when applicable, full gate, doctor assessment boundaries. Refer
  explicitly to repository-root paths because skill-relative docs is wrong.
- [x] Implement validated action planning and exact-template upgrades. Preserve
  custom text. Preflight before writes and report/rollback ordinary guidance
  write failures. Use existing descriptor-safe filesystem utilities.
- [x] Capture per-child actions at config planning/writing sites in internal
  data only. Never use filesystem-after-the-fact guessing. Preserve warnings.
- [x] Implement a polished fixed-width box with a clear ptest wordmark/title,
  config/guidance sections and aligned statuses. Wrap long paths, sanitize
  controls, use no new terminal library or animation. Show existing information
  and warnings truthfully, make dry-run conditional verbs explicit. JSON stays
  a single existing document, including when stdin is a TTY.
- [x] Run focused tests including renderer and contracts. Re-read actual diff,
  record commands/exit status and any untested cases, then return to orchestrator.

## Task 2: Verification and native discovery smoke (orchestrator)

- [x] Terra high reviews the Task 1 diff/spec; Muse repairs concrete findings.
- [x] Build a wheel into a new task-owned directory and install into its own venv.
  Compare installed source/resource bytes against the wheel/worktree.
- [x] Create small temporary Git repositories representing api/web monorepo
  and standalone pytest. Use candidate init with codex,claude; preserve output,
  JSON, dry-run and repeat-init observations. Never run init in original repos.
- [x] Launch Codex exec with explicit supported model and reasoning effort low,
  asking to use its registered ptest skill and state focused/full commands. Do
  not supply the skill path or contents. Retain transcript evidence that the
  skill is in the native catalog and that the actual SKILL.md was loaded.
- [x] Launch Claude --print --effort low in a fresh initialized repo with
  read/skill tools only, asking the equivalent question. Retain its native
  startup skills list and Skill tool invocation evidence; a model's assertion
  alone is insufficient. Record versions, arguments and exit statuses.
- [x] Capture hash inventories before/after read-only smoke to rule out writes.
- [x] Run graphify update . after source edits; never semantic extraction.
- [x] Run `.venv/bin/ptest --full` once after the final implementation repair.
- [x] Terra high completes final review using full-suite and native smoke logs.
  Fix only actionable findings and repeat checks affected by fixes.
- [x] Final report links spec, implementation, audit and native discovery
  evidence, and distinguishes actual execution from untested configurations.

## Plan self-review and rulings

Task 1 owns all coupled config/rules/CLI/render changes, so there is no shared
file race. Task 2 consumes the installed candidate and produces external evidence
only. Public serializer remains allowlisted; internal defaulted fields can
carry actions without altering v1. User explicitly selected Muse execution and
authorized completing the spec; no additional plan approval pause is required.
