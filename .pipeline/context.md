# ptest NG context and approved workflow

Current phase (2026-09-17): user-approved product specification and Astra design
have passed Opus focused re-review (approved_with_notes, no blocking findings).
Astra clarified the non-blocking shared-contract notes in a001145. Muse Task0
first submission c545671 passed49 scoped tests but Opus rejected seven required
acceptance gaps. Muse repair round1 is authorized; wave1 waits for the corrected
runnable barrier. Linux/macOS v1 scope is accepted; release,
license, main merge, installation, and publication remain separate gates.

## User intent

Rebuild ptest in its existing Git repository into a local-first, open-source-ready
test tool. Install once, initialize per repo, then run tests intelligently and
quickly. No hosted service, model API, cloud credentials, or specific coding TUI
is required by ptest. Remote testing is deferred. Tool-driven dependency setup and
tests themselves may have network requirements, which must be explicit.

Local scheduling is a central capability: cooperate across simultaneous agent
sessions/repos/worktrees, not merely cap each invocation independently. Select
partial tests conservatively with explainable, auditable evidence and full-run
fallback. Never call a skipped workload passed or drop unresolved failures.

Doctor should identify test-suite readiness and parallelization blockers, provide
structured evidence and actionable repair prompts for the user's existing local
coding agent. Cover databases generically (reuse expensive initialization per
run/worker, not per test; clean state between tests), Redis/Valkey/other caches
(run/worker namespace; no shared destructive flush), files/ports/processes/time/
network and other shared mutable resources. Encourage factories/builders and
duration budgets; preserve assertions and coverage. Suspected static patterns
are not certification of safety. Dynamic probes must be separately explicit.

Support as many LLM coding TUIs as reasonable through ordinary local CLI, portable
instructions and versioned machine-readable output. Do not turn ptest into an
agent launcher or depend on provider-specific APIs. Distinguish documented generic
compatibility, tested adapters, and unverified native integrations.

## Existing material

- Original repo: /home/ingmar/code/tools/ptest, main at bae8863 (clean at start).
- Historical cloud-heavy proposal: /home/ingmar/code/persea_content_maker/ptest_ng.md.
  Read it for selection ideas, but newer user constraints above override remote scope.
- Provisional package/doctor implementation:
  /home/ingmar/worktrees/ptest/cx-ng-foundation/ptest, branch cx-ng-foundation,
  HEAD 33656db plus uncommitted assistant-owned fixes. READ ONLY reference; no
  design decision is automatically accepted. Do not discard or modify this tree.
- Legacy recovery tag: legacy/pre-ng-2026-09-17 -> bae8863.
- The installed ptest is a separate copied bundle. Leave it unchanged.

## Routing and stages (user approved)

1. Astra xhigh creates product specification with acceptance and negative criteria.
2. Claude Fable 5.1 high reviews specification; Astra xhigh resolves findings.
3. Present reviewed spec for user approval before detailed design/implementation.
4. Astra xhigh produces design, frozen contracts, test strategy and owned task plan.
5. Claude Opus reviews design; repair and review findings before coding.
6. Muse, given Dan Jefferies' actual instructions, implements bounded tasks with TDD.
7. Claude Opus audits implementation stages and the integrated result.
8. Astra xhigh independently audits the integrated result again.
9. If corrections remain after that Astra audit, use Sol for repairs/re-review.

Verify actual CLI/model identifiers. Never silently substitute a requested model.
For Muse read /home/ingmar/.codex/agent-memory/dan-jefferies-agent/reference-muse-code-cli.md.
Read /home/ingmar/.codex/agents/dan-jefferies-agent.toml as role source, translate
instructions explicitly; do not assume Muse inherits Codex agent definitions.
Include applicable AGENTS.md and any actually needed imported-agent brief.

Parallelize genuinely independent work. Keep sequential spec/review/design gates.
Use separate task worktrees and disjoint ownership for writers. No source-code
implementation until the spec/design gates pass. Root orchestrates; workers code.
Keep structured outputs, reviewer findings, decisions, and stage transitions in
.pipeline/. Do not push, merge main, install over live ptest, publish, or deploy.

## Acceptance priorities

Correctness before claimed speed; replayable benchmarks and measured overhead;
safe invalidation and uncertainty fallback; bounded memory/disk/scan growth;
fair admission and cancellation/crash recovery; portable configuration; explicit
runner/platform/TUI support levels; secure and non-destructive doctor/init;
real subprocess tests, isolated resource tests, independent-repo adoption smoke.
Public release needs a user-owned license choice and explicit release approval.
