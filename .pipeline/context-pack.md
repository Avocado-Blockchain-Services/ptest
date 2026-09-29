# Context pack — doctor offline light path (2026-09-29)

Spec: /tmp/claude-1000/-home-ingmar-code-tools-ptest/e8898983-6043-4619-a933-8d5abd5c42c4/scratchpad/doctor-offline-brief.md (authoritative).
Worktree: /home/ingmar/worktrees/ptest/cc-doctor-offline/ptest  branch feature/doctor-offline off local main 985d43e (Release 0.3.7).
Read AGENTS.md and docs/ptest-agent.md first. Older .pipeline records (<2026-09-22) are out of scope.

## Files this feature touches
- src/ptest/cli.py — `_doctor_static_output`, `_offline_assessment_parts` (calls `agent_assessment.build_packets(workspace, resolution)` with no deadline/progress), `_doctor_offline_assessment_json`, init's declined-review fallback to static doctor. Offline JSON carries `packet_sha256` (cli.py ~1714) and excerpt/inventory limitations (`_assessment_limitations`, ~1650) — light packet must keep them identical.
- src/ptest/agent_assessment.py — `build_packets` (already takes `deadline`, `progress`), `_build_one_packet` (calls `_candidate_text_pool` then `RE.rank_candidates`, `RE.item_source_chains`, admission), `_candidate_inventory_sha256`, `_packet_body`, `_review_checkpoint`.
- src/ptest/doctor.py — `inspect_workspace` (18% of offline time; per-child file counts for progress line).
- src/ptest/review_evidence.py — `rank_candidates`, `_item_rank`, `_item_signal`, `_relation_score`, `_source_role`, `_python_identifier_text`, `_executable_text`, `_strong_item_operation` (signal_cache).
- src/ptest/config.py — `init_project` / `_commit_paths` / `_existing_result` (commit_paths is () when config unchanged); src/ptest/worktree.py `uncommitted_config_files(root, include_agent_rules=True)` (read-only reuse); src/ptest/init_render.py renders "Commit these files" from `result.commit_paths`; cli.py ~2420 appends agent-rule guidance targets after init_project.

## Precedent
src/ptest/agent_assessment.py — `build_packets`/`_build_one_packet`: deadline+progress via `_review_checkpoint`, bounded reads, packet identity via `_packet_body`/`packet_hash`. Imitate its style (frozen dataclasses, `_fail` problems, explicit checkpoints).

## Test commands (ptest only; never raw pytest)
- scoped (TDD):  `ptest --workers 2 --queue-timeout 1800 tests/ng/<file>.py`
  e.g. `ptest --workers 2 --queue-timeout 1800 tests/ng/test_review_evidence.py tests/ng/test_doctor_grid.py`
- coverage: the same scoped command — `.ptest.toml` runner args already include `--cov=ptest --cov-report=term`, so each scoped run prints the coverage table. Do not add a separate coverage command.
- Expect queue waits: the host shares 8 ptest slots across worktrees; `waiting for N slots` is normal, do not start a second run.
- full suite (once, integrated, orchestrator only): `ptest --full`
- `tests/ng/support.py` `invoke()` rejects timeouts under 20 s unless `expect_timeout=True`. No wall-clock speed assertions.
- Existing offline fixtures: tests/ng/test_doctor_grid.py, test_doctor_smoke.py, test_agent_doctor_acceptance.py, test_cli.py, test_render.py, test_init.py; ranking: test_review_evidence.py, test_agent_assessment.py.

## Coverage gate
none — no CI config in repo (.github absent); pyproject `[tool.coverage.run]` sets only `data_file`, no `fail_under`.

## installCmd
`uv sync --locked --extra test` (the `.ptest.toml` [setup] argv; prewarmed in this worktree).

## Migrations
none (no database).

## After source changes
`graphify update .` in the changed checkout. Commit only in your own worktree.
