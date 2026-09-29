# Context pack — ptest update check + `ptest update` (2026-09-29)

Spec (authoritative): /tmp/claude-1000/-home-ingmar-code-tools-ptest/e8898983-6043-4619-a933-8d5abd5c42c4/scratchpad/update-check-brief.md
Worktree: /home/ingmar/worktrees/ptest/cc-update-check/ptest (branch feature/update-check, base local main 985d43e)
Read first: AGENTS.md, docs/ptest-agent.md (test rules). Follow secure-by-spec. Do not read .pipeline history dated before 2026-09-22.

## Stack
Python 3.11+, setuptools src layout (`src/ptest`), uv (`uv.lock`), pytest + pytest-cov under ptest (`.ptest.toml`, runner kind pytest, launcher `uv run --locked --no-sync python`).

## installCmd
`uv sync --locked --extra test` (same as `.ptest.toml` [setup].argv). Never pip. Never share .venv.

## Files this feature touches
T1 (code):
- NEW `src/ptest/update.py` — version resolution (follow redirect of https://github.com/Avocado-Blockchain-Services/ptest/releases/latest), 24h cache in state area (`storage` / PTEST_STATE_DIR), 2s timeout, download + `.sha256` verify, safe tar extraction, run bundled `install.sh`/`scripts/install.py` as `get.sh` does, install-layout detection, `update` document data.
- `src/ptest/cli.py` — parse `update [--check] [--version X] [--json]`, add to command set, startup check hook (skip help/version/update, `--fixture-domain`, `PTEST_NO_UPDATE_CHECK=1`, truthy `CI`; `-q` suppresses notice; TTY prompt + re-exec).
- `src/ptest/help.py` — `ptest help update` topic + index lines (tests/ng/test_help.py enumerates topics).
- `src/ptest/contracts.py` — register `update` public document kind.
- `docs/schemas/v1/update.json` and `src/ptest/runtime/protocol-v1.json` — regenerate with `uv run --locked --no-sync python scripts/export-schemas.py` (drift-checked by tests/ng/test_contracts.py).
- NEW `tests/ng/test_update.py`; extend `tests/ng/test_contracts.py`, `tests/ng/test_help.py`, `tests/ng/test_cli.py` as needed.
- Reference only (do not change unless required): `get.sh`, `install.sh`, `scripts/install.py`, `tests/ng/test_get_sh.py`, `tests/ng/test_install.py`, `tests/ng/support.py` (`invoke()` rejects timeouts < 20 s unless `expect_timeout=True`).
T2 (docs): `README.md`, `docs/installation.md`, `docs/changelog.md`, `src/ptest/resources/agent-guide.md` (package-data source of the agent guide) and `docs/ptest-agent.md` (this repo's generated copy).

## Precedent
`src/ptest/uninstall.py` (+ its wiring `_run_uninstall` in cli.py, `_UNINSTALL` in help.py, `uninstall` in contracts.py/docs/schemas/v1/uninstall.json, tests/ng/test_uninstall.py): a self-contained command module touching the install root, with TTY consent, `--json` public document and schema.

## Test commands (all through ptest, from worktree root; never raw pytest)
- Scoped (TDD): `ptest --workers 2 --queue-timeout 1800 tests/ng/test_update.py tests/ng/test_cli.py tests/ng/test_help.py tests/ng/test_contracts.py`
- Coverage (scoped): same command — `.ptest.toml` runner args already add `--cov=ptest --cov-report=term`, so the scoped run prints the coverage table.
- Full (once, at the end): `ptest --full` (ptest caps workers via its scheduler; machine-wide queue).
- The ptest queue is machine-wide and may be busy (setup verification waited ~19 min in queue for an 11 s run); `waiting for N slots` is normal — wait, do not start a second run. Use a long tool timeout or run in background.

## Coverage gate
none — no CI config in the repo (no .github/ or other CI files) and no `fail_under` in pyproject.toml. Preserve existing coverage; new module should be covered by its tests.

## Migrations
none (no database migrations in this repo).

## After source changes
`graphify update .` in the worktree. Commit only in the worktree; no push/release by task agents.
