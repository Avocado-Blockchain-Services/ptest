# Init onboarding — final verification (2026-09-22)

Worktree: `/home/ingmar/worktrees/ptest/cx-init-onboarding/ptest`
Branch: `cx-init-onboarding`. Main and the globally installed CLI were not changed.
Runtime/test development used Muse; specification review used the current Astra
session; implementation audits and repair verification used Terra.

## Result

- Human init now shows a bounded boxed configuration/guidance summary, including
  actual file actions, unchanged files, selected-agent discovery hints, preview
  verbs, and attention warnings for unusable existing child configurations.
- Codex skills use `.agents/skills/ptest/SKILL.md`; Claude skills use
  `.claude/skills/ptest/SKILL.md`. Both have native-discoverable YAML metadata.
- Exact released generated skills upgrade; edited skills conflict safely; the
  legacy Codex artifact is preserved. Ordinary guidance-write failures roll back
  owned changes without clobbering detected concurrent edits.
- `--agents all` works; TTY JSON does not prompt; the public JSON document shape
  is unchanged. No new dependencies, runtime model calls, or global integration.

## Gates

- `.venv/bin/ptest --full`: **exit 0; 2291 passed, 31 skipped in 147.78s**.
  No input-change warning in the final full run.
- Changed-test timing check, also through ptest:
  `tests/ng/test_agent_rules.py tests/ng/test_init.py
  tests/ng/test_init_render.py tests/ng/test_cli.py --durations=10
  --durations-min=0.005`: **exit 0; 204 passed in 0.61s**; slowest call 0.02s.
- Terra closed the final code finding in `terra-closure-audit.md` (PASS).
  Evidence-only closure is recorded separately in `terra-evidence-closure.md`.
- `git diff --check`: exit 0.
- `graphify update .`: exit 0, AST-only, no semantic/model extraction.
  Final graph: 4469 nodes, 10222 edges. Eight schema/data files yielded zero
  AST nodes; graphify reported that limitation explicitly.

## Installed package and live skill smoke

Evidence directory (all task-owned copies/fixtures, outside source checkouts):
`/home/ingmar/worktrees/ptest/cx-init-onboarding/smoke-5Vh6ai`.

- Built `verified-dist/ptest_ng-0.1.5-py3-none-any.whl`, installed with uv into
  the evidence directory's own venv, and compared installed `ptest` files with
  `src/ptest` using `diff -rq --exclude=__pycache__`: exit 0.
- Independent standalone/monorepo init, dry-run/no-write, repeat/no-change, JSON
  shape and native Codex catalog checks passed. Repeat monorepo output includes
  both `api/.ptest.toml` and `web/.ptest.toml` as already present.
- Final installed CLI upgraded an exact legacy Claude template. Its generated
  skill and guide bytes match those loaded by the live smoke agents; root config
  bytes were unchanged. Missing-child fixture correctly reports needs attention.
- Codex 0.155.1 / explicit Terra **low**: native skills catalog lists enabled
  repo-scoped ptest; transcript records reading the actual skill and root guide.
- Claude Code 2.1.278 / Sonnet **low**: native startup catalog lists ptest;
  transcript records an actual `Skill` invocation for ptest.
- Both agents returned `ptest api/tests/test_example.py` and `ptest --full`;
  both exited 0. Before/after file-hash inventories match.
- The model sessions used the earlier installed candidate. Final rebuilt output
  was compared byte-for-byte with the exact skill/guide files they loaded;
  no repeated model calls were needed after filesystem-only repairs.

Read `2026-09-22-native-smoke.md` for transcript paths and flags. Raw model logs,
wheel/build products, and fixtures are preserved in the external evidence
directory rather than mixed into product source.

## Limits and unsuccessful probes

- 31 full-suite skips: opt-in external doctor smoke and unprovisioned frozen
  pytest/coverage/xdist tuples, including shadow cases. No skip was added here.
- Live native discovery covered Claude and Codex, not OpenCode or Gemini.
- An extra `umask 077` init probe failed at the unchanged root-config writer
  (`unsafe-path: file '.ptest.toml' failed creation checks`). Restrictive-umask
  support is not added by this feature; this probe is not counted as passing.
- Initial/repair audits blocked real defects before the final PASS. Headless
  continuation attempts that produced no visible tool progress were terminated;
  fresh focused workers completed the repairs. Their logs were retained.
- No crash-atomic multi-file transaction is promised. No release, push, merge to
  main, or global installation was performed. Feature changes remain local.
