---
name: ptest
description: Run repository tests through ptest whenever you run, add, change, or verify tests or code; pytest, vitest, and npm test intents go through ptest from the repository root.
---

# ptest skill

After each edit run bare `ptest` (no project name): it runs only the tests your change reaches.
| Command | Runs |
|---|---|
| `ptest` | changed tests, whole repo |
| `ptest <folder>` | changed tests under that folder |
| `ptest <file>` | that file, always |
| `ptest --full <folder>` | all tests under that folder |
| `ptest --full` | integrated gate, once before handoff |
Run tests only through `ptest` from the repository root. Never invoke pytest, vitest, or npm test directly. Never cd into a child directory. Never rerun `ptest --full` without a change.
Read `docs/ptest-agent.md` (relative to the repository root) for what each ptest output means.
