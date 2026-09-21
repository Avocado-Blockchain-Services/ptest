Review the ptest NG PRODUCT SPECIFICATION, read-only. You are the explicitly
requested Claude Fable 5.1 reviewer at high effort. Do not delegate or change model.

Read in order:
1. AGENTS.md and .pipeline/context.md (user intent and agreed workflow).
2. /home/ingmar/.agents/skills/audit-spec/SKILL.md (read-only evidence-based audit).
3. docs/specs/2026-09-17-ptest-ng-product.md (the candidate spec).
4. docs/research/2026-09-17-agent-interoperability.md if present, plus surrounding
   README/code only as needed to verify migration/current-contract assertions.

You may use only read/search tools. Do not modify files or git, run tests, install
dependencies, launch commands, contact providers, inspect secrets, push or deploy.
Specification-only stage; implementation/design is deliberately later.

Judge against the explicit user requirements, not the historical cloud design or
the provisional implementation. The product must remain local/model-independent,
work reasonably with many coding TUIs, schedule simultaneous invocations safely,
select tests conservatively with full fallback and unresolved-failure handling,
and provide a genuinely useful doctor/repair workflow. Preserve explicit scopes,
runner output/exit status and coverage gates. Doctor findings must not certify
safety, dangerous probes must be explicit, and cleanup must be ownership-scoped.

Audit specification coherence, missing material behaviors, finite/implementable
scope, acceptance/negative criteria, state/failure/cancellation/invalidation cases,
security/resource boundaries, migration/support declarations, and honest measurable
performance claims. Do not demand low-level module/schema/algorithm decisions that
properly belong to the subsequent detailed design, unless the omission makes the
product contract inconsistent or untestable. Do not reopen explicit settled choices
without new concrete conflicting evidence. Separate true defects from preferences.

Every finding needs a spec section/file line plus a concrete scenario and a repair.
Try to refute each finding; an empty findings list is valid. No invented failures.
Use the supplied JSON schema. `blocking` holds concrete required corrections,
`notes` holds non-blocking observations. Explain unverified areas in `coverage`.
Do not claim you executed tests or verified native TUI integrations.
