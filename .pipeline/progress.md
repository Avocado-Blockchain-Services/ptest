# Pipeline ledger

## 2026-09-17 — setup

- User approved the adapted single-repo workflow and aggressive independent parallelism.
- Final audit order: Opus integrated audit, Astra xhigh integrated audit, then Sol
  repair/re-review if necessary.
- Chain worktree: /home/ingmar/worktrees/ptest/cx-ng-product/ptest, branch cx-ng-product.
- Base: bae8863. Main clean; provisional cx-ng-foundation branch/worktree preserved.
- Current stage: specification and parallel compatibility/runtime-preflight research.
- No implementation, suite runs, dependency setup, live installation or main merge
  authorized for this stage. Next human gate: reviewed product specification.

## Runtime preflight

- Astra xhigh spec author and an independent Astra xhigh TUI-compatibility researcher
  dispatched concurrently in separate spec/compat worktrees; docs-only ownership.
- Claude Fable requested as `claude-fable-5-1` with `--effort high`; isolated
  no-tools READY probe succeeded, modelUsage confirms claude-fable-5-1.
- Claude Opus requested via `opus` alias with high effort; no-tools READY probe
  succeeded, current alias resolved to claude-opus-5.
- Muse no-shell/no-write/no-web READY probe succeeded; configured Meta model is
  muse-spark-1.3. Untrusted workspaces skip AGENTS.md, so actual assigned worktrees
  must explicitly use --trust-workspace. No role inheritance is assumed.
- All probes exited zero. No product tests were run; these are tool-availability
  probes, not product verification. No credentials were read or printed.
- Follow-up trusted Muse probe completed with the exact heading of the assigned
  AGENTS.md despite no tools: `# ptest NG product workflow`. Rule ingestion is
  verified in that mode. This does not verify the future product or native role
  overlays. Selected results: .pipeline/out/runtime-preflight.json; raw trusted
  trace retained locally (ignored) in .pipeline/out/muse-trust-preflight.jsonl.
- Fable spec audit brief and structured verdict schema prepared. Runtime primary
  aliases are pinned in evidence; Fable will run read/search tools only, no shell.

## Parallel specification inputs completed

- TUI compatibility research: worker commit 17e44ed, integrated as 6059a7b. Six
  families covered using primary documentation and bounded local Muse evidence.
- Product specification: Astra xhigh worker commit feb2b680, integrated as
  05736da. Owns docs/specs/2026-09-17-ptest-ng-product.md and author manifest.
- Documentation validation passed (JSON parsing, whitespace and ownership checks).
  No product tests, dependency installations, or runtime edits in this stage.
- Fable 5.1 high specification audit launched against this immutable draft with
  only Read/Glob/Grep tools. Output: .pipeline/out/spec-review-fable.json.
- Human gate remains pending: reviewed product spec approval. The Linux/macOS
  preference question is optional; proposed default remains Linux+macOS v1.

## Fable specification review — round 1

- Completed with requested claude-fable-5-1/high; 23 turns, no permission denials,
  exit 0. Raw structured result retained at .pipeline/out/spec-review-fable.json.
- Verdict label: approved_with_notes. Required-correction array contains F1–F3,
  so the spec gate is NOT treated as passed yet.
- F1: admission-domain identity / nested ptest fixture deadlock.
- F2: changed-during-run/full-gate semantics and separating outputs from inputs.
- F3: define implementable command redaction and honest residual limitations.
- Astra xhigh author resumed to repair/disposition the findings and actionable
  clarification notes; no code or tests. Suggested fixes are not automatically
  accepted when they weaken correctness (notably exempting tracked snapshots or
  returning a misleading full-gate success on changed source).
- Next: scoped Fable re-review of the repair, then present spec for user approval.

## Astra specification repair

- Worker commit 1370334 integrated as e87e58c; exact dispositions in
  .pipeline/out/spec-repair-astra.json. Only specification/author-report files changed.
- F1: canonical host/account domain; immediate same-domain nested execution error;
  explicit isolated synthetic/miniature fixture domain, never silent env activation.
- F2: changed relevant input invalidates automatic/full evidence (child 0 -> final
  70); original child failure codes retained; only non-input outputs are exempt.
  Snapshot/source/expectation updates require a subsequent unchanged full gate.
- F3: default command summaries omit arbitrary argv; explicit local unredacted
  reveal is warned, not retained, and never includes an environment dump.
- Clarified initialization/migration, cwd, signals, guide availability, scan order,
  coverage-failure obligations and side-by-side legacy limits. Smaller arbitrary-
  wrapper reservations remain deliberately deferred with a rationale.
- JSON parsing and whitespace checks passed; scoped ownership inspected.
- Repair-only Fable 5.1/high review launched with read/search tools and streamed
  local trace .pipeline/out/spec-rereview-fable.jsonl; no tests or source changes.

## Specification review gate passed; awaiting human approval

- Focused Fable re-review completed: requested claude-fable-5-1/high, 12 turns,
  exit 0, no permission denials. Verdict approved_with_notes; blocking list empty.
  Portable result: .pipeline/out/spec-rereview-fable.json. Raw trace kept locally.
- F1, F2, F3 each resolved; reviewer explicitly accepted Astra's alternatives.
- All actionable N1–N9 clarifications resolved or deliberately dispositioned;
  the smaller arbitrary-wrapper reservation was a preference and remains parked.
- Two nonblocking wording observations remain: T3 could repeat T4's existing rule
  that failures survive an invalidated run; P2's pre-init command list could repeat
  the guide availability already explicit in section 9/A1. No requirement gap
  was established. Carry those references into design, not another speculative loop.
- Orchestrator updated only the spec's status line to reflect the passed review;
  no substantive contract edits after the reviewed repair.
- Next required action is USER APPROVAL of the reviewed product spec. Do not begin
  detailed design, Muse coding, code removal, installation, main merge, or release
  on the strength of this report alone. Linux+macOS remains the proposed v1 scope;
  native Windows is deferred, and license/publication decisions remain M5 gates.

## Human specification approval; design started

- User explicitly replied "approved!" to the reviewed specification on 2026-09-17.
- Product contract is now approved, including Linux/macOS first-release scope.
- Proceeding with Astra xhigh detailed design/contracts/task plan and independent
  parallel design research, then Opus review before Muse/Dan implementation.
- No runtime edits, tests, dependency installation, or legacy removal at this gate.
- Final integrated review remains Opus then Astra; subsequent corrections use Sol.
