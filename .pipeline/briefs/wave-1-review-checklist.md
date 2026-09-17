# Wave1 Opus audit preparation

Root attaches task-specific base/head/diff/report and fresh scoped test logs after
the mechanical gate. This file is a checklist, not a pre-issued verdict. Follow
task-review-common.md and audit-spec. Read complete changed bytes and relevant
unchanged contracts; no tests/edits from reviewer. Return review.json schema.

## T1 config/init

- Existing targets preserved even invalid/version999 and even dry-run; exclusive
  create, no symlink/traversal overwrite or native config execution.
- Physical cwd and nearest config stop at Git checkout boundary; no accidental
  parent-checkout adoption, correct moved/no-git identity and mixed-runner choice.
- Full typed grammar/bounds, no echoed arbitrary token values; selection-only
  invalid policy full-fallback versus invalid execution refusal.
- Explicit PTEST location overrides reject, HOME/XDG nonauthority; bounded legacy
  migration is read-only, ambiguous/remote/compound adoption cannot execute.
- Finite launcher/setup profiles preserve native semantics; setup network/scripts
  declared and init command profile not invented. No new CLI/public shape.
- Inherited Muse partial code distinguished from Luna additions; no collection
  errors claimed behavioral RED and no scratch probe/placeholder shipped.

## T2 platform

- Account canonical roots and fixed parents, equal real/effective uid, supported
  local filesystem only; static resolution cannot create/chmod normal state.
- Private fixture strict marker, owner/mode/dev/inode/containment validation;
  symlink components, hardlinks, unknown/network filesystem fail safely.
- Birth/uid/pgid/boot identity conservative under absence, inaccessible identity,
  reused PID and changed boot. No argv/env reads and no nonzero signal.
- Existing0755 canonical shared parents and legacy0644 siblings unchanged;
  absent parents resolved without creation. Real Mac evidence stays unverified.

## T3 history

- Explicit DomainPaths and per-checkout state only; no platform/env/normal-root
  fallback, no writes on absent inspection, shared safe storage/file primitives.
- Immutable allowlisted public run summaries; private sequence/snapshots/policy
  never leak through public history. Enforce count/byte bounds independently.
- Failure obligations survive skip/xfail/incomplete/old-late/incompatible/dirty
  passes, coverage-only failures and teardown failure. Conclusive applicable
  complete outcomes only; deleted IDs require successful full reconciliation.
- Basic serial unknown identities cannot erase obligations or seed baseline;
  baseline requires clean source-valid compatible complete advanced full result.
- Transactional races/idempotence/retention/quota/corruption preserve protected
  evidence or disable selection visibly. Full execution remains independently
  possible; missing required publication never asserted committed.

Cross-task scheduling/CLI integration is not yet implemented; record those limits
without claiming they pass. Task0 R1 granted_workers schema maximum remains an
explicit final-audit residual, not silently repaired in disjoint wave1 tasks.
