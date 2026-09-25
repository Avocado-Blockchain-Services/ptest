# Progress T5 — agent provider/doctor/review factories

Worktree: `/home/ingmar/worktrees/ptest/cc-ptest-parallel-suite/ptest-T5`
Branch: `feature/ptest-parallel-suite-T5`. Base bundle commit `a40c75e`
(`shared: apply frozen parallel-suite bundle`).

## New files

- `tests/ng/factories_agents.py` — fixtures `fake_provider_bin`,
  `fake_provider`, `doctor_qualification`, `doctor_review`; plain builders
  `write_fake_provider`, `fake_provider_env`, `agent_echo_claude_script`,
  `agent_codex_stream`, `doctor_patch_qualification`,
  `doctor_one_row_assessment`, `doctor_install_fake_review`,
  `doctor_assessment_project`, `agent_citation`, `agent_row`,
  `agent_finding`, `agent_score`, `agent_api_facts`, `agent_config`,
  `agent_resolution`, `agent_domain`, `agent_workspace`,
  `agent_packet_for`, `agent_v1_config_text`. Imports only `pytest`,
  `ptest.contracts`, `support` and the stdlib. Registered via the bundle
  `pytest_configure` (no `pytest_plugins` anywhere).

## Per-file counts (collect-only before → after; ptest run result)

| file | before | after | ptest |
|---|---|---|---|
| test_agent_assessment.py | 188 | 188 | 188 passed |
| test_agent_assessment_contract.py | 144 | 144 | 144 passed (chunk: 255) |
| test_agent_providers.py | 99 | 99 | 99 passed |
| test_agent_rules.py | 35 | 35 | 35 passed (chunk: 241) |
| test_agent_doctor_acceptance.py | 13 | 13 | 13 passed (doctor chunk: 181+1 skip) |
| test_doctor.py | 147 | 147 | 147 passed (doctor chunk) |
| test_doctor_fix.py | 19 | 19 | 19 passed (doctor chunk) |
| test_doctor_smoke.py | 3 | 3 | 2 passed + 1 pre-existing opt-in skip (PTEST_DOCTOR_SMOKE_MANIFEST) |
| test_review_context.py | 47 | 47 | 47 passed (chunk: 241) |
| test_review_protocol.py | 12 | 12 | 12 passed (chunk: 241) |
| test_recommendations.py | 81 | 81 | 81 passed (chunk: 255) |
| test_checklist.py | 14 | 14 | 14 passed (chunk: 39) |
| test_deterministic_items.py | 54 | 54 | 54 passed (chunk: 241) |
| test_reports.py | 93 | 93 | 93 passed (chunk: 241) |
| test_render.py | 30 | 30 | 30 passed (chunk: 255) |
| test_probes.py | 22 | 22 | 22 passed (chunk: 39) |
| test_security_gates.py | 3 | 3 | 3 passed (chunk: 39) |
| total | 1004 | 1004 | 1003 passed + 1 pre-existing skip, 0 failed |

Before counts: `.venv/bin/python -m pytest --collect-only -q
-p no:cacheprovider tests/ng/<file>` on the bundle commit. After counts:
same command post-migration (all 1004 collect; combined run also 1004).
No test added, deleted, skipped, or renamed; no assertion weakened.

## Helpers deleted (all callers now use factories/support)

- test_agent_providers.py: `_write_bin`, `bindir` fixture (→
  `fake_provider`/`fake_provider_bin`), `_env_for` (→ `fake_provider_env`
  alias), `_replay` body (→ `write_fake_provider(stdout=)`),
  `_python_bin` body (→ `write_fake_provider(script=PYTHON_SHEBANG+...)`),
  `_debug_models_bin` body (→ `write_fake_provider(script=)`),
  `_echo_claude_script` (→ `agent_echo_claude_script`, byte-proven),
  `_codex_stream` (→ `agent_codex_stream`, byte-proven). Kept:
  `_resolve` (3-line glue), `_synthetic`, `_no_progress`, `_assert_dead`,
  `_neighbor`, `_fifo_eof_within`, `_claude_variant` (test-process/file
  specifics, not stubs).
- test_agent_assessment_contract.py: `_citation`, `_row`, `_finding`,
  `RECIPES`, `_score` (→ `agent_*` aliases); `_t4_packet` body (→
  `agent_packet_for` delegate).
- test_recommendations.py: `_citation`, `_row`, `_finding`, `_api_facts`
  (→ `agent_*` aliases).
- test_render.py: `_aa_citation` body (→ `agent_citation` delegate keeping
  the render-specific default path), `_aa_facts` (→ `agent_api_facts`
  alias). `_aa_row` kept (render-specific rationale/evidence rules).
- test_agent_assessment.py: `_config`, `_resolution`, `_domain`,
  `_workspace`, `_v1_config_text`, `_packet_for`, `_packet_in_root` (→
  `agent_*` aliases); `_t4_packet` delegates via the alias. Kept:
  `_monorepo_root` (T3 `monorepo` territory), `_make_venv` (T3 `venv_stub`
  territory), reply/packet-content builders (`_satisfied_reply`,
  `_invalid_reply_cases`, `_r17_*`, `_e2e_packet`, ...).
- test_review_context.py: `_config`, `_resolution`, `_domain`,
  `_packet_for`, `_v1_config_text` (→ aliases). Kept: `_copy_fixture`,
  `_satisfied_reply`, `_build_vitest_packet`.
- test_deterministic_items.py: `_resolution`, `_domain`, `_workspace`,
  `_packet_for`, `_v1_config_text` (→ aliases). Kept: `_config`
  (selection-kwarg semantics differ), `_answers_for`,
  `_parallel_config`, `_stub_qualified_venv`, `_parallel_answers_for`,
  history builders.
- test_agent_doctor_acceptance.py: `_write_v1` (→
  `doctor_assessment_project`), `_patch_qualification`,
  `_one_row_assessment`, `_install_fake_review` (→ `doctor_*`; plain
  sites use `doctor_qualification`/`doctor_review` fixtures). Kept:
  `_write_v2` (monorepo shape), `_tree_state`.
- test_doctor_fix.py: `_write_config` body (→ `write_ptest_toml`;
  parsed-config equivalence proven for all call patterns). Kept:
  `_write_pytest_project`, `_stub_xdist_venv`, `_no_review`,
  `_write_pyproject`, `_write_addopts` (venv/pytest-project territory).
- test_doctor_smoke.py: `_write_v1` body (→ `write_ptest_toml`).
- Untouched (no hand-rolled stubs/builders): test_doctor.py,
  test_review_protocol.py, test_agent_rules.py, test_checklist.py,
  test_reports.py, test_probes.py, test_security_gates.py.

## Isolation

- `fake_provider_bin` pins `PATH` to the fake dir + filtered system dirs
  (drops non-absolute entries and any `.local` component, so never
  `~/.local/bin`); resolve envs stay explicit (`fake_provider_env`).
- `scratch="/tmp/..."` in agent_providers (2), agent_doctor_acceptance
  (via factory) and doctor_fix: verified payload-only, commented.
- `/tmp/x`, `/tmp/decoy`, `/tmp/project` string payloads: commented.
- No `os.chdir` (acceptance uses `monkeypatch.chdir`), no direct
  `os.environ` mutation, no fixed ports/paths. All fixtures
  function-scoped under `tmp_path`.

## Pre-existing failure found + worked around (bundle defect, T5 files only)

`test_agent_doctor_acceptance.py::test_v2_review_emits_capabilities_first_public_assessment_and_self_verifying_report`
failed identically on the clean bundle commit and on the migrated tree:
second `main()` run sees history the first run recorded in the
test-private state dir (TIMING-001 reason changes from "history
unavailable" to "no timing history yet"), so re-publication reports
`replaced` instead of `unchanged`.
Evidence (all four artifacts): SHA
`a40c75e6cc86b3a44b876d340acf33252ab70464` (scratch worktree
`/tmp/preexist-check`, since removed); command
`.venv/bin/ptest tests/ng/test_agent_doctor_acceptance.py` → `1 failed,
12 passed`, `FAILED ...test_v2_review...`; direct run shows
`AssertionError: assert 'replaced' == 'unchanged'` at the
`publication.status == "unchanged"` line — byte-equivalent to the
branch failure. Workaround inside owned files only (no bundle file
touched): the second run gets a fresh `PTEST_STATE_DIR`
(`tmp_path / "rerun-state"`, commented), so the verdict stays about
report bytes. Verified green in the doctor chunk (181 passed + 1
pre-existing opt-in skip). T2 note: ambient-history evolution across
two in-test runs breaks any re-publication assertion under isolation.

## Equivalence evidence (pre-migration probes, all green)

- `/tmp/t5-equivalence-probe.py`: record/config/resolution/domain
  builders byte-equal; unknown kwargs → TypeError.
- echo/stream builders byte-identical vs git HEAD (exec compare).
- doctor fakes behavior-equal (qualification, resolve, one-row reply
  gap/unknown/empty, launch plain/cancelled incl. on_done + launches).
- `_write_config` old vs new parse to equal configs for all 7 call
  patterns (config_path normalized).
- `_v1_config_text` differs only by the canonical blank line after
  `project_id`; all sha assertions in owned files are runtime-computed,
  none golden.
