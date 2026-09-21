# Task 12b — real native qualification (Muse implementation brief)

You own this isolated worktree only:
`/home/ingmar/worktrees/ptest/cx-ng-product/task-12b`
on branch `cx-ng-product-task-12b`, base `cec84dae`.

Read before editing:

- `AGENTS.md`
- `.pipeline/context.md`
- `docs/specs/2026-09-17-ptest-ng-product.md`
- `docs/designs/2026-09-17-ptest-ng-design.md`
- `docs/designs/2026-09-18-ptest-ng-task12-amendment.md`
- Task 12b in `docs/plans/2026-09-17-ptest-ng.md`

This is a new local-only NG implementation. Do not revive, parse, wrap, call,
or fall back to legacy/global/remote/cloud/backend/model/API/TUI behaviour.
ptest must not launch agents. Do not change root dependencies, install the
live CLI, push, merge, or publish.

## Ownership

Own the Task 12b file set exactly:

- `src/ptest/adapters/pytest.py`, `src/ptest/runtime/pytest_bridge.py`
- `src/ptest/adapters/vitest.py`, `src/ptest/runtime/vitest_bridge.mjs`
- `src/ptest/runners.py`, `src/ptest/reports.py`
- sequential integration only where required: `src/ptest/contracts.py`,
  generated bridge descriptor, `src/ptest/operations.py`, `src/ptest/cli.py`,
  `src/ptest/history.py`, `src/ptest/selection.py`
- owned native tests/fixtures named by Task 12b, plus
  `tests/ng/test_compound_profiles.py` and `.pipeline/out/task-12b.json`

Do not revert or edit unrelated user/agent changes. Keep one source of truth;
remove affected dead/obsolete code safely rather than adding compatibility
layers.

Root transfer confirmation for this repair: Task12b may make the single
`tests/ng/support.py` `CONTROL_VARS` nonce-scrub change; it may retain the
narrow `src/ptest/source.py` worker-count compatibility normalization and add
its deterministic regression in `tests/ng/test_source.py`; and it may touch
`tests/ng/test_operations.py` only to declare the synthetic Pytest fixture's
explicit `-p no:xdist`. The temporary generated-result-filtering experiment
was removed. No other files in those owners' areas are transferred.

## Contracts and negative twins

Implement actual native Pytest/Vitest qualification, not static declarations.
Native bridge evidence must be complete, authenticated, and revalidated after
admission. A malformed/missing/forged report, missing/changed runtime identity,
duplicate/parameterized-ID ambiguity, incompatible config/plugin, dynamic
influence, missing coverage/reporter, or missing exact worker identity MUST
refuse promotion/qualification. Exact selected files MUST be preserved. Worker
namespace identity MUST be real, per-run/per-worker, and preserve a neighbor.
Basic serial remains execution-only; never infer advanced/parallel support from
runner kind or a metadata flag.

First implement ordinary advanced full/selected/scoped execution and a genuine
clean full baseline. Then prove actual selected-plan execution and separately
record Q-PY-SELECT, Q-PY-PROBE, Q-VT-SELECT, Q-VT-PROBE evidence. Unsupported
is truthful (`unsupported-capability`, exit 2) and is never a qualification
claim. T12c owns shadow/probe orchestration: do not implement it here.

## TDD and execution discipline

Write focused positive and negative tests first. All tests go through candidate
ptest only: `./.venv/bin/ptest ...` after ptest itself materializes it. Never
invoke pytest/vitest/npm/go/cargo directly, never run `uv sync` manually, never
use raw bridge runners as test commands. Scoped tests while iterating; no
`ptest --full` in this task. Use `apply_patch` for edits.

If an existing fixture/test encodes Task 11's superseded pre-admission setup
refusal, correct it only when it is within this owned Task 12b set and preserve
determinism: do not introduce a real networked dependency install into normal
unit tests.

Run `graphify update .` after source changes. Record every real tuple/platform
attempt and all failures in `.pipeline/out/task-12b.json`; never claim source-
only qualification.

Milestone status: Q-PY-SELECT is the only qualified native tuple. The local
xdist tuple is reachable for refusal/control evidence, but Q-PY-PROBE is
deliberately deferred to Task 12c; Q-VT-SELECT and Q-VT-PROBE remain explicit
unsupported-capability. Task 12/M3 therefore remains open until the required
qualification matrix is complete.

Start by inspecting the existing adapters, bridge protocol/report contracts,
and fixtures, then implement in small reviewed increments. Do not commit: stop
with a concise evidence summary for Opus precommit audit.
