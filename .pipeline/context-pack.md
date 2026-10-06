# Context pack — post-test stall detection + stack dumps (feature/post-test-stall)

Authoritative spec: /home/ingmar/worktrees/ptest/specs/2026-10-05-post-test-stall.md
(goals G1/G2, definitions, B1-B10, negative contracts N1-N12, abuse table). Read it first.
Worktree: /home/ingmar/worktrees/ptest/cc-post-test-stall/ptest (branch feature/post-test-stall, off local main 3ab75e8).

## Runner and test commands (ptest repo)
- runner: ptest (root `.ptest.toml`, committed on main; pytest runner, workers=8).
- After each edit, from the worktree root: `ptest` (bare; only the tests the change reaches).
- One file/folder: `ptest tests/ng/test_x.py`.
- The ONE integrated gate, once at the very end (Verify phase): `ptest --full`.
- Never call pytest directly; never add --workers/--timeout/--queue-timeout; never `ptest init`,
  never `ptest update` (tell the user instead). `config-uncommitted` => stop and report.
- coverageCommand / coverageGate / installCmd: "" (ptest owns setup: `uv sync --locked --extra test`).
- envFiles: none. Never symlink .venv. localOnlyPaths: `.pipeline/progress.md`, `.pipeline/context-pack.md`
  (these two are TRACKED legacy files overwritten here as working state — never stage/commit them).
- After source changes: `graphify update .` in the changed checkout.

## Precedent to imitate
`src/ptest/guard.py` — `_run_one` compound/attempt deadline path (`state.fail(_problem("execution-timeout", ...))`
then `_cancel_and_reap` → `_signal_group` SIGTERM/grace/SIGKILL). The stall path and the dump signal
are siblings of that code and must reuse `_signal_group` ownership checks. Config precedent:
`_optional_timeout` / render of `timeout` in `src/ptest/config.py` (~345-392, ~1163).

## Cross-task contract (fixed names — every task codes against these)
- Marker path: `str(report_path) + ".done"` (report name `native-aNNN-<32hex>.json`, dir
  `<domain>/checkouts/<id>/reports`, 0700). Created O_CREAT|O_EXCL|O_NOFOLLOW, 0600, empty.
- Dump path per process: `str(report_path) + f".stack-{pid}"`, O_CREAT|O_EXCL|O_NOFOLLOW|O_APPEND, 0600,
  kept open; `faulthandler.register(signal.SIGWINCH, file=fh, all_threads=True, chain=True)`.
- Bridge reads the bound path from existing env `PTEST_PYTEST_REPORT_PATH` (set by operations.py ~1779, ~3346);
  workers inherit it. No new argv / ini / `-p`.
- contracts.py: `DEFAULT_STALL_TIMEOUT_S = 120.0`; `RunnerConfig.stall_timeout_s: float | None = None`
  (None = absent → effective 120; 0 = disabled; else 10..86400); `LaunchManifest.stall_timeout_s: float | None = None`
  (None or 0 → no stall detection; encoded/decoded in encode/decode_launch_manifest); `"post-test-stall"` in `REASON_CODES`.
- Guard problem: `Problem("post-test-stall", f"tests finished but runner processes stayed idle for {N:g}s without exiting")`.
  Guard learns the marker from `prepared.report_path` (already in the manifest); runner kind gating is done by
  operations passing `stall_timeout_s` only for the pytest runner.
- Dump signal: SIGWINCH via `_signal_group`, then bounded ≤1.0 s cancel-interruptible wait, then existing
  `_cancel_and_reap`. Sent on deadline kills and stalls only — never on user/outside cancel.
- Guard's final return must treat `post-test-stall` like `execution-timeout` (not `_EXIT_PROTOCOL`, guard.py ~680).
- Operations: post-test-stall → Status.INCOMPLETE, exit 70; end line
  `incomplete (exit 70): post-test-stall — <message>; stack dumps above; rerun once alone, report a repeat`;
  not a compound-deadline history record (history.py kill_s keys on `execution-timeout` only — keep it so);
  no last-green/verified. Dumps printed to stderr after reap: header `ptest: stack dumps (<n> processes) — <reason>`,
  caps 32 files / 400 lines + 32 KiB per file / 128 KiB total, escaped via `render.terminal_text`-equivalent,
  never in --json/history. Cleanup of marker + dumps for every attempt outcome (B7).

## Files this feature touches
- src/ptest/runtime/pytest_bridge.py (3421 lines; `_report_binding` ~783, `_worker_bootstrap` ~3245,
  hooks `pytest_runtestloop` ~2203/2518, `pytest_runtest_logreport` ~2261/2702, module hooks ~3299-3400)
- src/ptest/contracts.py (REASON_CODES ~152, RunnerConfig ~423, LaunchManifest ~2167, codec ~3944/4065)
- src/ptest/config.py (runner table keys ~360, `_optional_timeout` ~345, render ~1163)
- src/ptest/guard.py (`_run_one` ~533, `_signal_group` ~305, `_cancel_and_reap` ~406, `run_guard` ~594)
- src/ptest/platform.py (process facts; group member CPU times via /proc if not present) — new src/ptest/stall.py for the pure idle-window logic
- src/ptest/operations.py (manifest build ~1262/3431, guard problem handling ~3486-3600, advanced ~1864)
- new src/ptest/stack_dumps.py (read/print/cleanup), src/ptest/progress.py (end-line text)
- docs: docs/specs/2026-10-05-post-test-stall.md (copy), docs/ptest-agent.md == src/ptest/resources/repository-agent-guide.md
  (byte-identical today; keep so), src/ptest/resources/agent-guide.md, README.md (config row), docs/changelog.md
  (new top section "Unreleased" — do not bump version), src/ptest/agent_rules.py previous-managed-guide recognition
  (`_previous_*` helpers ~100-235, fixture dir tests/ng/fixtures/previous-guides/).
- Tests: tests/ng/test_guard.py, test_run_deadline.py, test_pytest_bridge_unit.py, test_config.py, test_contracts.py,
  test_agent_rules.py, test_resources.py, real-process fixtures under tests/ng/fixtures/processes/.

## Migrations
None (no database migrations in this repo).
