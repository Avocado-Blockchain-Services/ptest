Perform a bounded repair-only re-review of the ptest NG product specification.
Use the requested Claude Fable 5.1 at high effort. No delegation/model change.
Only Read/Glob/Grep tools are available. No writes, tests, installs, shell commands,
credentials, or external effects. Follow the read-only audit-spec rules.

Read .pipeline/context.md and AGENTS.md for settled user constraints, then:
- .pipeline/out/spec-review-findings.json (your first structured review, extracted
  without its duplicated string form)
- .pipeline/out/spec-repair-astra.json (author's exact disposition and evidence)
- .pipeline/out/spec-repair.diff (only the proposed repair)
- affected sections of docs/specs/2026-09-17-ptest-ng-product.md as needed

Scope: F1 scheduler domain/nesting, F2 result/input-artifact semantics, F3 privacy
display policy, and direct regressions introduced by their repairs. Check whether
the actionable clarification notes were resolved or reasonably dispositioned.
Do not conduct a fresh broad architecture/spec audit or manufacture new preferences.
This is a product contract, not the later detailed design; do not demand an
implementation algorithm where an observable enforceable contract suffices.

The author's fixes may differ from your suggestions. Judge the actual constraints:
- no silent capacity multiplication or nested deadlock;
- no false full-gate/baseline success on changed relevant source or snapshots;
- ordinary declared non-input reporting artifacts don't invalidate a run;
- runner failures remain visible, and operational statuses are explicit;
- privacy claims have a testable display policy and honest residual limits.

Return the supplied review JSON schema. An empty `blocking` list is valid and
required for passing the correction gate. In `notes`, give a concise disposition
for F1, F2, F3, including any accepted alternative resolution. Report only concrete
remaining defects/direct regressions with file/section evidence. State no tests
were executed. If you disagree with a chosen policy, identify the explicit user
constraint it violates; a different preference does not block approval.
