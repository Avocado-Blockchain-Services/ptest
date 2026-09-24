# pipeline — Doctor and init v2 for ptest: /home/ingmar/worktrees/ptest/cc-reviewer-choice/ptest/docs/superpowers/specs/2026-09-23-doctor-init-v2-requirements.md is the authoritative spec (read it first; it also points to docs/superpowers/specs/2026-09-22-agent-doctor-design.md). IMPORTANT BASE: cut the chain branch from the LOCAL branch cc-reviewer-choice at commit 193346e (it contains the requirements doc and the unmerged interactive reviewer-menu commits), NOT from main/origin. Scope: (A) ptest must execute real projects — pytest with xdist in addopts runs serially under ptest, vitest children execute, and init runs a deterministic executability check; (B) much better init output and accurate, non-duplicated guidance/skills; (C) doctor v2 — evidence admission priority excluding agent/pipeline artifacts, one focused cheap-model review per (project, checklist item) with per-item prompts, labels, evidence routing, and deterministic N/A skips, bounded parallel fan-out, cheap-model discovery from the provider CLI list with an exact-match LLM pick and fallbacks, and a per-project checklist terminal display; plus the three LOW test fixes from the reviewer-menu audit. The real-world target is /home/ingmar/code/persea_content_maker_unified (api pytest+xdist, web vitest); tasks must not modify that repo or launch real claude/codex/opencode — the controller validates there afterwards. Tests via ptest only. No push, no merge to main.

(This file replaces the pre-2026-09-22 ledger; older history stays in git.)

## 2026-09-23 — setup
- Worktree /home/ingmar/worktrees/ptest/cc-doctor-init-v2/ptest, branch feature/doctor-init-v2, cut from local 193346e (cc-reviewer-choice). Default branch: main (origin/main..main empty).
- Prewarmed: `uv sync --locked`. No .env to share. No CI config, no coverage gate, no migrations.
- Verified `ptest where` (pytest basic_serial) and scoped `ptest tests/ng/test_init_render.py tests/ng/test_agent_rules.py` (47 passed).
- Context pack: .pipeline/context-pack.md. Triage: 6 disjoint tasks (T1..T6).
- 2026-09-23T21:35:17Z [controller] fix1 audit APPROVE WITH CHANGES (2 MEDIUM, 4 LOW, 1 OOS) saved .ctl/fix1-audit-findings.md; batched after smoke round.
- 2026-09-23T21:46:36Z [controller] smoke audit BLOCK (HIGH setup-fingerprint skip). Decision: TTY consented setup-through-ptest; non-TTY working advice; smoke always no_setup. fix3 (smoke + fix1 findings) launched.
- 2026-09-23T22:11:09Z [controller] fix3 re-audit BLOCK (HIGH -r cluster regression; MEDIUM double smoke after setup; LOW private walker). fix4 launched.
- 2026-09-23T22:28:56Z [controller] fix4 re-audit APPROVE WITH CHANGES (LOW duplicated post-setup record; NOTE -vrk). fix5 launched.
- 2026-09-23T22:41:23Z [controller] full gate 6c9095b: 3100 passed/31 skipped exit 0. Persea validation: api bridge refuses anyio pytest_pyfunc_call; web picks e2e spec / no smoke candidate; api --full unavailable (-m addopts + conftest collection hook) -> user decision. fix6 launched.
- 2026-09-23T23:24:30Z [controller] fix6 audit APPROVE WITH CHANGES (MEDIUM glob matcher, MEDIUM box lost config action) -> fix8 after fix7.
- 2026-09-23T23:49:30Z [controller] fix7 Muse stalled (idle 10+ min after approval assessment); killed pid, relaunching with continuation.
- 2026-09-23T23:53:02Z [controller] fix7b 3b4d50e (section F) merged ff into chain; fix8 worktree ptest-r8 created; fix7 audit + fix8 in parallel.
- 2026-09-23T23:59:48Z [controller] fix7 audit BLOCK (HIGH unlabelled narrowed full via static-label vs runtime-permission split). fix9 queued after fix8.
- 2026-09-24T00:08:44Z [controller] fix8 dec8a27 merged into chain. fix9 (section F BLOCK) launched in ptest-r9; fix8 audit in parallel.
- 2026-09-24T00:10:50Z [controller] fix8 audit APPROVE WITH CHANGES (MEDIUM re.error crash, LOW brace bound) -> batch with fix9 audit.
- 2026-09-24T00:47:22Z [controller] fix9 461e31d merged into chain (884 passed). fix9 audit + fix10 (glob crash/bound) in parallel.
- 2026-09-24T00:59:20Z [controller] fix10 f6ae51b merged. fix9 re-audit BLOCK (HIGH unchecked collect hooks; MEDIUM exit-70 flake). fix11 launched on chain.
- 2026-09-24T01:26:29Z [controller] fix11 re-audit BLOCK (HIGH pytest_collection_finish/fixture drop). Round-10 globs verified. fix12 = collected-vs-run reconciliation.
- 2026-09-24T01:48:16Z [controller] fix12 5197a39 APPROVED. Full gate starting.
- 2026-09-24T01:52:17Z [controller] full gate 5197a39: 2 failed (stale pins: 11d report dict, integration wrap). fix13 launched.
- 2026-09-24T02:02:29Z [controller] fix13 3082fc3 (test pins). Controller full gate run.
- 2026-09-24T02:03:45Z [controller] persea: api+web scoped runs pass via ptest; init --smoke passes both. fix14 (conftest sessionfinish in full, label split) launched; real doctor runs in parallel.
- 2026-09-24T02:13:58Z [controller] REAL doctor persea: claude all 11 replies fenced -> provider-failed; codex picked gpt-6-luna, replies valid, publish refused stale-evidence on empty api/tests/__init__.py. fix15 launched in ptest-r15 (parallel to fix14).
- 2026-09-24T02:32:44Z [controller] REAL persea doctor: claude exit0 220s (haiku, 22 calls), codex exit0 80s — both publish. fix14+15 audit BLOCK (exitstatus manipulation incl. pre-existing unconfigure/add_cleanup). fix16 = bridge-derived verdict.
- 2026-09-24T02:35:38Z [controller] persea doctor quality: codex api Database isolation SATISFIED (DB-002 false positive fixed); haiku ~50% item validation failures (backticks, citation ranges/stale). fix17 brief ready (after fix16).
- 2026-09-24T02:55:09Z [controller] fix16 7f147a8 (bridge-derived verdict; muse full gate 3267 passed). fix16 audit + fix17 (ptest-r17) in parallel.
- 2026-09-24T02:58:45Z [controller] fix16 audit BLOCK (logreport/collectreport wrapper rewrite). Decision: refuse in full, document for scoped. fix18 on chain (parallel to fix17).
