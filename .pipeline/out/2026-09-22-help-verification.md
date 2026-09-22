# Help UX verification — 2026-09-22

Approved scope: task-oriented `ptest help`, command-topic help and a concise
agent workflow, retaining the completed init onboarding work. Help remains
configuration-free and read-only. Literal runner tails and public JSON
contracts remain unchanged. No runner, selection, probe or monorepo execution
capabilities were added.

## Evidence

- Muse implemented tests first and observed missing-help failures. Two further
  documentation-consumer regressions caught forced pytest initialization of a
  monorepo and missing prefix-option examples before correction.
- Controller focused gate through ptest: **259 passed in 0.62s**, slowest listed
  changed-test call 0.02s; no changed-input warning.
- Final `.venv/bin/ptest --full`: **exit 0; 2346 passed, 31 skipped in 168.24s**;
  no changed-input warning. Existing opt-in/external/frozen-runner skips remain.
- Terra initially blocked inaccurate instructions; the six findings were
  corrected and its narrow closure verdict is **APPROVE**.
- Bandit and pip-audit gates, including sensitivity checks, passed. Pinned
  Gitleaks sensitivity checks and selected current source/tests/scripts/docs
  scans passed. Git/pipeline history scanning was omitted per user instruction.
- `git diff --check`: clean. `uv build --wheel`: passed; extracted wheel package
  matches `src/ptest` byte-for-byte excluding bytecode caches.
- `graphify update .`: AST-only, exit 0; 4505 nodes, 10291 edges. Eight schema/data
  files yielded zero AST nodes. No semantic extraction or runtime model APIs.
- Actual help commands succeeded in an empty non-repository directory without
  creating files. Mixed mutating/help options, hostile topics, no-config reads,
  and literal native tails are covered by focused tests.

## Limits and follow-through

History, plan, and live doctor probes still require single-project v1 configs;
help now explicitly distinguishes these from root-monorepo static doctor.
Root scopes remain one-child paths; native flag passthrough is standalone-only.
Static doctor findings are hypotheses, never evidence that isolation is ready.

One final editorial Muse invocation reached its eight-step cap after editing;
the controller independently verified the resulting tree with the gates above.

Commit/push, local install, ptest-only clean reinstall in the real unified repo,
and native Codex smoke are authorized follow-through, recorded separately in:
`/home/ingmar/worktrees/ptest/cx-init-onboarding/help-ux-8dQaWH/README.md`.
That directory also preserves raw logs, package artifacts, and the original
unified-repository ptest setup for recovery.
