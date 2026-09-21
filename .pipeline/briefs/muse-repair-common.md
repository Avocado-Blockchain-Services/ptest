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
