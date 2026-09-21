# Opus focused helper re-review

Read-only; no shell/tests/edits/delegation. Follow audit-spec and common contract:
/home/ingmar/.agents/skills/audit-spec/SKILL.md
/home/ingmar/worktrees/ptest/cx-ng-product/ptest/.pipeline/briefs/task-review-common.md

Cwd /home/ingmar/worktrees/ptest/cx-ng-product/task-0-helper. Exact repair base
4fe6d2701bc52c297af04860feed32bacc7e8278, HEAD0f41c78c57a9e57094b627c67f375d8c3eafafe7.
Only support.py, test_files.py helper cases and unique helper report changed.
Review package /home/ingmar/worktrees/ptest/cx-ng-product/ptest/.pipeline/out/task-0-helper-repair2.diff
Original review /home/ingmar/worktrees/ptest/cx-ng-product/ptest/.pipeline/out/task-0-helper-review-opus.json
Assignment /home/ingmar/worktrees/ptest/cx-ng-product/ptest/.pipeline/briefs/task-0-helper-repair-2.md

This is a FOCUSED correction review: resolve H-1,H-2, inspect repair regression
risks and the closely related authorized changes listed by assignment; do not
re-audit unchanged contracts/files/storage or absent T11. Public branch has its
own simultaneous Opus review. No task0 source is integrated/approved yet.

Root independently ran scripts/ptest-bootstrap tests/ng/test_files.py
tests/ng/test_contracts.py --durations=10 in this cwd: exit0,92passed/2.56s.
Raw .pipeline/local/root-helper-repair2.log. Slowest1.05s post-exit retained pipe,
1.01s watchdog; remaining<=0.03s. Read worker .pipeline/local/red-repair2b.log
and immediate .exit, green-repair2-all/final logs/exits and helper report.
Initial red-repair2.log included malformed test-target generation (fixed before
red-repair2b); do not count syntax failures as product RED. Retained earlier logs
remain history, not all current claims. Actualmodelmuse-spark-1.3 medium.

Check independent stdout/stderr exactcap/overcap, safe project-relative export
reader for absolute/symlink/FIFO, actual typed oversize behavior, prefix flags and
native tail, purge inherited env THEN explicit fixture overrides (plan deliberate
override negative, no production weakening), stream-error failure and cleanup,
shape dict|None, timing sensitivity. No new supervisor/dependency. Root checked
F.read_regular really reads a bounded prefix, so its BOUND+1 read detects files
larger than cap; don't hypothesize an unimplemented file-size rejection path.

Correct the original review's wrong-root skill-absence assertion: required skills
exist under /home/ingmar/.codex/plugins/cache/openai-curated-remote/superpowers/6.3.0/skills,
not only /home/ingmar/.agents/skills. Worker now claims full filesystem reads.
Workflow limitation retained by root: worker used cat to create temporary patch
files /tmp/red_fix.txt and /tmp/rep*.txt, then apply_patch for actual repo edits;
this is not perfect patch-only compliance. Report honestly, not a runtime finding.

Return supplied JSON with H-1/H-2 dispositions, real remaining required findings
or empty blocking, checked/notchecked coverage. No full suite at this gate.
