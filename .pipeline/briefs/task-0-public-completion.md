# Task0 public contract completion — Muse / Dan

Use muse-spark-1.3 at medium effort, actual Muse CLI, no delegation. Worktree:
/home/ingmar/worktrees/ptest/cx-ng-product/task-0, branch cx-ng-product-task-0.
Current base 071d4af includes prior repair ced109b plus Astra's approved narrow
public-summary addendum (author2162cf2). Read AGENTS.md, task-0-launch.md,
task-0-brief.md, muse-dan-common.md, original Dan role and Muse reference completely.
Read approved design including its new public-summary section, task plan T0 and
integration/interface barriers, .pipeline/out/design-public-summary-addendum.json,
.pipeline/out/task-0.json and .pipeline/local/review-1/opus-findings.json.

Read receiving-code-review, systematic-debugging, secure-by-spec, TDD (including
writing-good-tests), graphify skills before their respective actions. No further
product approval needed. The author resolved the real schema ambiguity; implement
the exact ruling, do not redesign or leave known shape gaps as completion.

You are NOT alone. Another Muse owns tests/ng/support.py and helper regressions in
tests/ng/test_files.py in a separate worktree. Own ONLY src/ptest/contracts.py,
tests/ng/test_contracts.py, Task0-generated schemas/public/* and schemas/protocol/*
when legitimately affected, and .pipeline/out/task-0.json. Do not touch helper,
files/storage modules, other tests, scripts, design/plan, dependencies, or legacy.
Do not integrate another branch. Root combines disjoint repairs before Opus review.

Exact remaining scope:
- Implement InitAction and validated ConfigSummary/EffectiveLimits frozen by the
  addendum, init precedence/action relations, history RunResult payload reuse.
- Finish ALL already-frozen nested public records, no arbitrary object islands:
  Capability, LeaseView, Obligation, Readiness, Finding, ScanLimits/Usage, counts,
  timings, attempts etc. One descriptor authority for validation and schemas.
- Public producers emit allowlisted fields only; public consumers recursively
  validate known required fields then return known-field projection. Same-major
  additive unknowns are ignored/dropped, NEVER trusted or re-emitted. Do not reject
  harmless additive public fields merely because private protocols are strict.
  Do keep private control/launch decoding strict. No raw argv/config/exception
  text or RunResult's four internal fields may leak via any nesting/history/init.
- Preserve previous seven-finding repairs; test actual production serializers,
  not handcrafted safe substitutes. Independently assert generated schema shapes.

Use apply_patch for ALL local code/test/scratch edits; direct writer is disabled.
Bare @@ patch hunks, not numbered unified diffs. No shell cat/Python source writes
or delete/re-add workarounds. Add durable regressions FIRST and capture meaningful
RED against current bytes through scripts/ptest-bootstrap. Missing imports alone
are not a behavioral RED. Then GREEN and assertion-sensitivity checks. Keep FULL
raw logs under .pipeline/local, save ptest exit immediately before viewing output,
no masked tail/pipeline exit. Use existing OWN venv, UV_CACHE_DIR under this tree,
and supplied frozen PTEST_BOOTSTRAP. No raw runners/full/global config/live install,
network services/main merge/push. Scoped test_contracts plus test_files/storage
only if relevant; generated schema drift --check. No production CLI stubs (T11).

Update the existing report preserving earlier attempts and honest limitations,
exact commands/cwd/exits/raw logs, acceptance negatives, Dan three passes. Report
what is implemented vs verified vs deferred, not global product completeness.
Run graphify update . AST-only, git diff --check, inspect duration outliers, and
commit explicit OWNED files only. Return SHA, test evidence and remaining issues.
Root owns combined mechanical checks and Opus review; do not self-launch auditors.
