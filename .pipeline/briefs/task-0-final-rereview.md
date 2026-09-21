# Opus final combined Task0 gate — focused correction review

Read-only, no shell/tests/edits/delegation. Follow audit-spec/common contract:
/home/ingmar/.agents/skills/audit-spec/SKILL.md
/home/ingmar/worktrees/ptest/cx-ng-product/ptest/.pipeline/briefs/task-review-common.md

Cwd /home/ingmar/worktrees/ptest/cx-ng-product/task-0-combined.
Combined submitted HEAD cd572ec0e03dba1f878d5eb774e45096d8647891. Original Task0
base042dde1. Public correction base5f5e501 (contracts bytes equal revieweddbac887),
repair8e3d370 cherry-picked ascd572ec. Helper4fe6d27 and0f41c78 were independently
approved by Opus; their combined cherry-picksd61b142/5f5e501 have identical bytes.
No downstream task implementation has started; chain/main/live ptest untouched.

Required prior findings / accepted helper gate:
/home/ingmar/worktrees/ptest/cx-ng-product/ptest/.pipeline/out/task-0-public-review-opus.json
/home/ingmar/worktrees/ptest/cx-ng-product/ptest/.pipeline/out/task-0-helper-rereview-opus.json
Exact public correction package:
/home/ingmar/worktrees/ptest/cx-ng-product/ptest/.pipeline/out/task-0-public-repair1.diff
Exact assignment:
/home/ingmar/worktrees/ptest/cx-ng-product/ptest/.pipeline/briefs/task-0-public-repair-1.md

Review the correction and its affected boundaries to close T0-8..T0-11, then verify
composition with the accepted helper. Do NOT repeat the entire unchanged original
audit: T0-1..T0-6 are already confirmed fixed by the prior public review; helper
H-1/H-2 and related corrections approved with blocking=[] by its own re-review.
Look for real repair regressions; no requirement for absent T1+ or actualmacOS now.

Root ran scoped test_contracts/test_files/test_storage on THESE COMBINED bytes:
raw cwd/.pipeline/local/root-combined-boundary.log. Frozen installed legacy
dispatcher with localbackend,worker1; no raw runners/full. Schema export --check0,
git diff --check0; own uv3.13 environment, no shared deps/global config mutation.
Earlier submitted combination had112passingtests; current correction adds7groups.
Read actual root log for current119 count/timing; do not infer a passing outcome.

Worker RED/GREEN and sabotage artifacts live in the original public worktree:
/home/ingmar/worktrees/ptest/cx-ng-product/task-0/.pipeline/local/red-boundary.log
plus red-boundary* and green-boundary-final* / sabotage-boundary* there. Main
report is current cwd/.pipeline/out/task-0.json, newest boundary repair section;
helper report cwd/.pipeline/out/task-0-helper-repair.json. Read cited actuallogs.
No shared aliases/raw-log paths should be mistaken for shipped runtime deps.

Specific review duties:
- FIRST encoded bytes enforce recursive payload/domain allowlist for all8kinds;
  decoder tests build independent hostile JSON rather than pass it through the
  now-sanitizing encoder. Check sentinel absence without vacuous roundtripping.
- Known fields (provenance/scope/artifact_id/IDs/bounds) match frozen records and
  schema requirements; malformed dict/list/bool/null enums yield fixed typed
  Problems. Check both required-field validation and additive unknown dropping.
- All private frame/per-kind/guard/problem/manifest/domain/grant/prepared/nested
  authority records reject unknown keys; retain lengths/nonces/defaults. Public
  argv/env extras drop, private extras reject, per explicit author addendum.
- Error messages do not echo supplied values/sentinels. Report duplicate keys
  fixed without deleting retained attempts. No producer-liberal/private-strict
  overclaims remain current. Check concrete drift, not cosmetic framework demands.
- Combined shared helper uses the repaired contracts correctly. Preserve accepted
  bounded output/watchdog/env/prefix behavior; no reintroduced helper changes.

Return supplied JSON, per-T0-8..11 disposition in notes, genuine remaining required
findings or empty blocking, explicit checked/not-checked scope. This gate may pass
with honest nonblocking future-platform/T11 limits. No full suite at this stage.
