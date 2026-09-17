Perform a focused repair-only re-review of the ptest NG design and owned task plan.
Use Claude Opus at high effort, read/search tools only, no delegation/model change.
Read /home/ingmar/.agents/skills/audit-spec/SKILL.md and follow its read-only rules.
No tests, installs, edits, shell commands, credentials or external writes.

Read AGENTS.md and .pipeline/context.md for settled authority, then:
- .pipeline/out/design-review-findings.json (NG-1 through NG-9 and notes)
- .pipeline/out/plan-preflight.md (orchestrator P1 and P2)
- .pipeline/out/design-repair-astra.json (exact dispositions)
- .pipeline/out/design-repair.diff (the repair only)
- affected sections of docs/designs/2026-09-17-ptest-ng-design.md and
  docs/plans/2026-09-17-ptest-ng.md, approved product spec as needed.

Scope is closure of those findings and direct regressions introduced by repairs,
not another broad product/architecture audit. Check downstream typed interfaces,
task ownership/dependencies and tests affected by the changed contracts.

Judge alternatives by actual requirements rather than requiring the first review's
suggested implementation verbatim. In particular, unknown memory must remain
honestly unknown; untested versions cannot simply be assumed bounded; generic
wrapper exclusivity remains an approved escape hatch, not a first-class claim;
existing legacy state must remain untouched; fixture-domain state must not escape
to normal history/keys; runner argument values must not become wrapper options.

Return the supplied JSON schema. Empty blocking is valid and required for passing
this gate. Give concise NG-1..NG-9 and P1/P2 dispositions in notes, plus relevant
actionable-note disposition. Report concrete remaining defects/direct regressions
with cited evidence and severity proportional to consequence. No speculative
preference expansion. State what was not checked and that no tests were executed.
