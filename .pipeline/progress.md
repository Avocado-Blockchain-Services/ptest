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
