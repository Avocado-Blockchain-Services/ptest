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


## Task0 helper-bound repair — independent ownership

Worktree /home/ingmar/worktrees/ptest/cx-ng-product/task-0-helper
Base ced109bea3d91a15c942de26247b8fa2f187b5a9. You are NOT alone: another Muse
will repair public schema shapes in a different worktree. Own ONLY:
- tests/ng/support.py
- tests/ng/test_files.py (helper-related tests only)
- .pipeline/out/task-0-helper-repair.json (NEW unique report)
Do not edit contracts.py, schemas, plan/design, common task0 report, or other tests.
Do not integrate any other branch; root combines the disjoint repairs for review.

RESUME: root interrupted the first helper run to add the argument-ordering case
below. Existing edits in support.py/test_files.py are YOUR prior attempt, preserve
and inspect them and retained .pipeline/local logs. Do not revert them or claim a
fresh RED for behavior already repaired. Trace is retained by root as
.pipeline/out/task-0-helper-muse.jsonl; SIGINT exit130 was root-directed, not a test
failure. Finish the original repair plus this additional verified contract case.

Read your original task0 launch/brief/common/Dan source and the approved design.
This is a narrow completion of the frozen test-helper interface (plan lines59–63),
not a production CLI or process supervisor. T11 owns src/ptest/__main__.py and the
real CLI, so the helper's exact interpreter + '-m ptest' is valid deferred wiring.
Keep the new project-root unique default result filename from repair1.

Two concrete remaining failures, traced on ced109b:
1. CaseFactory.invoke(timeout:float=10.0) accepts omission, while the frozen API
   requires an explicit keyword watchdog. Remove the default; missing timeout must
   fail before launching any process. Keep strict positive/finite numeric checks.
2. invoke uses subprocess.run(..., stdout=PIPE, stderr=PIPE), then slices already
   captured output to BOUND_OUTPUT_BYTES. That allocates unbounded child output
   before truncation. The approved interface says captures bounded bytes, so the
   bound must apply while reading, not after collection. Export read-back similarly
   read_bytes() loads the full file before slicing; use the shared bounded reader.

Write durable behavioral regressions FIRST using apply_patch. Include both streams
past the cap, missing/invalid timeout, and a tiny child that hangs or exits while
an owned child retains a pipe: the watchdog must still be bounded, cleanup only
this fixture's owned processes, never unrelated jobs. Use explicit tiny test-only
miniature targets; no real application suites or production module stubs. Do not
run raw test runners. Keep tests fast and deterministic with barriers/fake clocks
where appropriate; a short real watchdog regression is justified and recorded.
Do not hide unbounded capture behind a temp file that merely moves the problem
from RAM to disk. Keep memory bounded while draining or produce a clear typed
fixture failure at the cap; never silently call clipped output complete evidence.
Avoid introducing a second production scheduler/guard or new dependency.

The shared Completed public helper shape remains code/stdout/stderr/result. Keep
existing result-export/read-back behavior and clean inherited control variables.
A declared caller --result-json should be parsed too per the frozen plan, not
silently result=None; add a positive test with an explicit existing-parent path.

3. The original helper appends generated --result-json AFTER *args. The approved
   CLI is prefix-only: parsing stops at the first unknown/native argument or --.
   Thus case.invoke(project, 'tests/test_x.py', timeout=...) currently sends the
   generated export option to the native runner, not ptest. Put generated wrapper
   options before the native tail, preserve every caller argument byte/order, and
   recognize explicit result overrides ONLY in the leading wrapper prefix.
   Membership checks across all args are wrong: ['-k', '--result-json'] and tokens
   after '--' are literal native values. Do not create a second full CLI parser;
   use the smallest shared/helper boundary consistent with the frozen interface.
   Add a miniature prefix-only parser target regression, not permissive argparse
   that would hide the real bug. Cover scoped path, -- delimiter, option-looking
   native values, and genuine leading explicit result path read-back.

Use YOUR own uv sync --locked and private UV_CACHE_DIR under .pipeline/local.
PTEST_BOOTSTRAP is supplied. Run only scoped scripts/ptest-bootstrap tests/ng/test_files.py
(and tests/ng/test_contracts.py if the fixture changes could affect it). Capture
unmasked RED/GREEN logs in your .pipeline/local; no scratch source via cat/heredoc
redirection—ALL local code/test edits, including temporary probes, use apply_patch.
Update your unique report with exact commands/exits/paths, findings and Dan passes.
Run graphify update . AST-only and git diff --check; commit explicit owned files.
Return SHA, test evidence, limitations. Root owns combined review; no full suite,
main merge/push/install, code outside ownership, or self-delegation.
