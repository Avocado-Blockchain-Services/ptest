# Muse / Dan bounded task repair

Use the exact task worktree and current submitted commit from the launch message.
Same muse-spark-1.3, medium effort; no delegation or silent model substitution.
The launch message supplies the exact required findings file and numbered scope.
Read it, the task report, original task brief, actual Dan role/reference, AGENTS.md,
approved design and relevant source bytes. Treat root's provisional observations
as hypotheses, not orders, and resolve against the actual reviewed findings.

Read receiving-code-review and systematic-debugging skills before implementing
feedback. Refute or reproduce each finding first. Honor the existing approved
contracts; if a true ambiguity remains, report the concrete producer/consumer
failure and smallest contract question. Do not redesign the product or weaken
tests/coverage/errors to manufacture a green result.

Direct file writes are disabled. Use shell apply_patch with BARE @@ hunks, not
numbered unified diff headers. Correct syntax:

```text
*** Begin Patch
*** Update File: relative/file.py
@@
-old exact line
+new exact line
*** End Patch
```

Resolve the existing apply_patch executable and check its exit and edited bytes.
Never use delete/re-add or shell source rewrites just to avoid understanding the
patch syntax. No edits outside the original task ownership; preserve artifacts.

Add durable regression tests for every feasible required finding. Observe the
intended failure on the reviewed bytes through scoped scripts/ptest-bootstrap,
then implement and run GREEN. Capture FULL raw logs and the ptest exit itself:
do not pipe to tail then report tail's zero exit. Redirect logs, save $? immediately,
then inspect output; or use bash pipefail with explicit PIPESTATUS if needed.
Temporary probes may support diagnosis but do not replace committed regressions.
Use explicit watchdogs for blocking behavior; no hanging FIFO/child tests.

Use the existing own venv and worktree-local UV_CACHE_DIR for uv operations.
PTEST_BOOTSTRAP remains frozen. No full suite or raw runners, no shared deps,
global config, live CLI install, real service mutation, main merge or push.

Update the task's existing report with per-finding disposition and exact evidence,
retaining previous failed attempts and distinguishing implemented/verified/untested.
Correct overclaims, abbreviated command paths, and unsupported assertions. Re-read
all changed bytes, re-anchor against acceptance, do Dan's evidence smell sweep,
run schema drift if relevant, durations, graphify AST update and git diff --check.
Commit explicit owned files; return SHA, tests and remaining blockers. Root owns
mechanical verification and focused Opus re-review; do not launch one yourself.


## Task0 repair round1 — exact assignment

Worktree: /home/ingmar/worktrees/ptest/cx-ng-product/task-0
Submitted review HEAD: c545671cc13d8cf98be5f67bb263c699ebb0c0a7
Original task base:042dde1. Own the SAME Task0 files only, including its report.
Read .pipeline/local/review-1/opus-findings.json completely. Opus rejected with
T0-1 through T0-7. Resolve all seven, preserving the approved design and existing
working behavior. Root independently reran49tests/0.05s; green is insufficient.

Read these complete skills:
/home/ingmar/.codex/plugins/cache/openai-curated-remote/superpowers/6.3.0/skills/receiving-code-review/SKILL.md
/home/ingmar/.codex/plugins/cache/openai-curated-remote/superpowers/6.3.0/skills/systematic-debugging/SKILL.md
Follow your existing task-0-launch.md for all other role/security/TDD instructions.

Specific constraints/dispositions:
- T0-1 structural path safety must support Application Support, spaces, Unicode,
  bracketed filenames without relaxing traversal/NUL/slash/no-follow protection.
- T0-2 prove descriptor stability on successful AND failed nested read/write calls.
  Prefer small deterministic accounting/watchdogs to thousands of slow fsyncs.
- T0-3 exercise the actual RunResult serializer, not a hand-written safe payload.
- T0-4 freeze ALL already-specified public subrecords recursively, not just the
  examples in the finding. Use one descriptor authority for schemas and validation;
  no arbitrary object islands, unsafe nested extras, or omitted frozen fields.
  No changed public contract/design. Fully populated fixtures and negative cases
  must exercise the actual serialization and independent schema expectations.
- T0-5 reject declared-depth violations and pathological parser depth with bounded
  typed errors. Retain strict length/nonce/version checks.
- T0-6 no raw constructor/exception/input/argv text in public Problem messages.
- T0-7 is suspected: reproduce/trace it, then ensure the frozen helper default
  result export works. Do NOT drop its mandated default to avoid the issue. Use a
  simple project-root unique filename or explicitly create owned private parents.
  Exercise argument/env assembly and read-back with a test-only miniature target;
  no production CLI/scheduler stub, no claim real T11 integration ran. The actual
  candidate entrypoint declaration is ptest.cli:main; do not depend on an absent
  ptest.__main__ module without a documented owner.

Close these closely related review notes in the same owned area: strict value
validation for every ControlFrame kind; Capability.limitations element types;
storage connection cleanup on every failure and read-only SQLite URI escaping;
accurate file IO errors; ignore generated egg-info/graph output. Keep the frozen
bootstrap routing/full refusal. It intentionally uses legacy worktree resolution.

Correct report overclaims and retain exact raw RED/GREEN/provenance/sensitivity
commands, exit codes, full log paths. Abbreviated /tmp/... evidence is not enough.
Keep feasible regression tests committed, not only scratch sabotage checks.
Root observed the initial FIFO hang and interrupted only the owned test; preserve
that failed attempt, do not call it passing or pre-existing. You may reuse the
existing venv/lock in THIS task worktree. UV_CACHE_DIR should be this worktree's
.pipeline/local/uv-cache. Root's live installed ptest remains frozen/unmodified.

After scoped checks, updated report, all Dan passes and graphify AST update,
commit explicit owned files and return the repair SHA and any genuine blockers.
No final full suite or self-launched audit. Root will run mechanical verification
and focused Opus re-review of c545671..repair against the original findings.

