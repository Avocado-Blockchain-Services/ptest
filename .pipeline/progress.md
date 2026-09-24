# pipeline — Parallel pytest (xdist) under ptest + init/doctor output redesign + fewer doctor unknowns. AUTHORITATIVE SPEC (read first, and copy it into the chain as docs/superpowers/specs/2026-09-24-parallel-and-output-requirements.md): /home/ingmar/worktrees/ptest/specs/2026-09-24-parallel-and-output-requirements.md. Base: main (3f399fb). Scope: (P) a real xdist parallel pytest tier owned by ptest (slots leased from the machine scheduler, worker processes owned and reaped, per-worker identity, --dist modes incl. loadgroup/xdist_group) that keeps every serial-bridge guarantee (bridge-derived verdict, collected-vs-run reconciliation across workers, labelled project filters, refused report-rewriting hooks) with a plain serial fallback; init stops writing -n 0 when the parallel tier qualifies; (O) output redesign for init and doctor: terminal width, no fixed box, plain-language 'runs / parallel / setup / full suite' lines, next steps only when actionable, one restart line, grouped file actions, compact doctor checklist with a reason on every unknown, a short disclosure; (U) fewer unknowns: deterministic TIMING/SELECTION answers from ptest's own facts, per-item evidence routing by code signals, sharper per-item prompts. Real-world target (read-only for tasks): /home/ingmar/code/persea_content_maker_unified. The final CLI wiring plus an integration test through cli.main are MANDATORY (the last run lost its wave-2 task). Tests via ptest only; never launch real claude/codex/opencode. No push, no merge to main.

## Prep (2026-09-24)
- Worktree /home/ingmar/worktrees/ptest/cc-parallel-and-output/ptest, branch feature/parallel-and-output off local main 3f399fb (origin/main..main: empty).
- Spec copied to docs/superpowers/specs/2026-09-24-parallel-and-output-requirements.md (uncommitted).
- Prewarmed: `uv sync --locked --extra test` (real pytest-xdist 3.8.0 + execnet installed). No .env to share.
- Verified: `ptest tests/ng/test_init_render.py tests/ng/test_executability.py` -> 122 passed; `ptest tests/ng/test_pytest_xdist_serial_subprocess.py` -> 3 passed.
- Tasks T1-T4 parallel (disjoint files); T5 is the wave-2 CLI wiring + cli.main integration test (MANDATORY, must run).
- 2026-09-24T15:15:41Z [controller] workflow halted (T1 Muse provider timeouts x2). Merged T2-T5 into chain (79db812). PARALLEL-001 missing -> T4b. T1 rerun + T4b launched in parallel.
- 2026-09-24T15:39:08Z [controller] T4b 50e952e merged (PARALLEL-001). T1 rerun at medium effort in progress.
- 2026-09-24T15:44:17Z [controller] T4b audit APPROVE WITH CHANGES (keep 12 rows + changelog). T4c launched on chain.
- 2026-09-24T15:54:39Z [controller] T4c re-audit APPROVE WITH CHANGES (MEDIUM launcher fix wording). T4d launched.
- 2026-09-24T16:24:00Z [controller] T1 (parallel bridge, 16 twins) merged. T1 audit + integrated tests.
- 2026-09-24T16:29:35Z [controller] integrated full gate 3f1389f: 2 failed (test_init_smoke x2). int1 launched in ptest-int1; T1 audit running.
- 2026-09-24T16:39:12Z [controller] T1 audit BLOCK (transport-hook forgery, bridge shadowing, twin new-session exit-70 cause). T1f launched in ptest-T1f.
- 2026-09-24T16:49:56Z [controller] int1 eb5468b merged ff (2 stale smoke twins; muse full gate 3489 passed exit 0). T1f running.
- 2026-09-24T17:22:23Z [controller] T1f d9b0ce3 merged. Re-audit.
- 2026-09-24T17:29:45Z [controller] persea REAL: init shows parallel 4 workers after setup; api contracts 90 items on 4 workers (89 pass, 1 persea generated-contract drift failure, not ptest); web ok; doctor codex 61s + claude 231s publish; BUG deterministic items downgraded to unknown (no .ptest.toml citation). DET1 launched.
- 2026-09-24T17:34:09Z [controller] T1f re-audit BLOCK (HIGH collection-error false refusal; LOW inherited PYTEST_XDIST_WORKER). Forgery routes confirmed closed. exit-70 root cause = pre-existing nested-guard new-session + machine-wide reconciliation (out of scope, report). T1g launched.
- 2026-09-24T17:43:26Z [controller] DET1 cb6aae0 merged (admit .ptest.toml tier 0). Audit.
- 2026-09-24T17:45:58Z [controller] DET1 audit APPROVE WITH CHANGES (LOW n/a citation section; NOTE cite pyproject). DET2 launched on chain.
- 2026-09-24T17:54:40Z [controller] DET2 audit APPROVE WITH CHANGES (2 LOW span parser). DET3 launched on chain.
- 2026-09-24T17:58:44Z [controller] T1g APPROVED + merged; DET3 done. Full gate next.
- 2026-09-24T18:03:59Z [controller] chain c486767+DET3 full gate 3518 passed/32 skipped exit 0. Final code review + persea recheck.
- 2026-09-24T18:10:29Z [controller] final whole-branch review APPROVE WITH CHANGES (2 MEDIUM, 5 LOW, 1 NOTE). FIN1 launched.
- 2026-09-24T18:38:47Z [controller] FIN1 confirm APPROVE WITH CHANGES (MEDIUM --cov loop other direction; 2 LOW). FIN2 launched.
- 2026-09-24T19:07:49Z [controller] FINAL: FIN2 81ac11a APPROVED. ptest --full 3536 passed/32 skipped exit 0. persea: init parallel 4 workers, smoke api+web ✓, 90 api tests on 4 workers, doctor PARALLEL-001 ✓. graphify updated. Task worktrees removed.
