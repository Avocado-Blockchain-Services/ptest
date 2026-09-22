# Agent-backed doctor — 2026-09-22

Objective: agent-backed per-project assessment shared by init/doctor, evidence-based
checklist percentages, actionable recommendations with self-verification, honest
progress, dependency diagnostics, and branded init banner.

- Baseline verified clean: cx-init-onboarding at c8cccf9628a9bde8956abe7a50b8a8a13e962afd.
- Reuse existing isolated linked worktree; main is out of scope.
- Previous goal turn only restated requirements: no implementation progress.
- Current phase: current-source context, then one combined Sol-high specification.
- The user's existing `do this` authorization covers implementation after the
  written spec; no further consent to begin coding is needed. First-use consent
  remains part of the product behavior. Spec-only work ran no tests.
- Implementation/repairs: Muse with imported Dan Jefferies agent profile; controller
  verifies. No reading historical pipelines, git history, or agent memories.
- No push, release, deployment, main merge, global install, or real-repo cleanup.
- Graph queries omitted because historical pipeline ingestion is prohibited; inspect
  current source directly. AST graph update required after source changes.

## Plan

1. Terra-low bounded current-source context and security capability evidence.
2. Sol-high combined design/implementation specification; user review gate.
3. Sol-medium versioned public contract and drift guard.
4. Muse test-first implementation in declared owned-file tasks.
5. Controller mechanical gates, scoped audits, integrated full ptest and graph update.
6. Copied-repository smoke tests and requirement-by-requirement completion audit.

## Current evidence

- Terra-low context completed (exec session 39430, exit 0): existing static JSON,
  canonical 11-item checklist, serial pytest constraint and pure init renderer
  confirmed. No declared numeric coverage threshold found; .github absent.
- Local provider help inspected without model calls: Codex 0.155.1, Claude 2.1.280,
  OpenCode 1.18.32. Provider-evidence.md records exact observed capability limits.
- Sol-high combined-spec worker started (exec session 26783). Sole write ownership:
  docs/superpowers/specs/2026-09-22-agent-doctor-design.md. No product implementation.
- Sol-high spec worker finished exit 0; actual written document inspected in full.
- Controller audit-spec review corrected bounded-coverage semantics, provider wire
  normalization, contract ownership, dry-run/exits, requested table columns, progress
  heartbeat, shared init wiring, and documented publication race limitations.
  Findings and remaining gates: spec-review.md. No source/test changes.
- Smoke source directories verified: code/scammeter/persea_scam_meter,
  code/scammeter/persea_scam_meter_ui, code/persea_content_maker_unified.
- Current goal resumed under the user's existing authorization. Product remains
  unchanged at this checkpoint; contract, implementation, qualification and all
  test/smoke gates remain to be completed.
- Sol-medium bounded contract review dispatched (exec session 86017), read-only.
- Muse provider boundary task dispatched as sole coding owner for
  src/ptest/agent_providers.py and tests/ng/test_agent_providers.py. It must
  capture focused ptest RED/GREEN and cannot label fake-provider tests as actual
  provider qualification.
- Isolated `ptest-banner` linked worktree created from b1228f7 on branch
  `cx-agent-doctor-banner`; `uv sync --locked` created its own .venv (exit 0).
  Muse owns only init_render.py and test_init_render.py there, independent from
  provider task. Merge after scoped test and audit.
- Sol-medium contract result completed exit 0, frozen fields and five drift tests
  recorded in contract-result.json. Existing public envelope unchanged.
- Isolated `ptest-contract` linked worktree created from b1228f7 on branch
  `cx-agent-doctor-contract`; own uv locked environment. Muse owns only
  contracts.py, schema export/file and two contract test files.
- Contract task committed 466bbdf then audited five blockers; Muse repaired all
  five in 778683a. Controller independently ran focused `ptest`: 101 passed
  (0.19s), schema export `--check` passed, Sol-medium re-audit approved with
  zero blockers. Contract branch is not merged while provider worker uses chain.
- Banner task committed c4ac4bf in isolated worktree. Controller focused
  `ptest tests/ng/test_init_render.py`: 17 passed; scoped audit approved with
  deferred caller wiring noted. Not yet merged.
- Provider scaffold committed 87353ef and relative-PATH/scratch repair db939ae;
  focused provider test 34 passed. Audit rejected one owned-PGID descendant
  escape after direct child exit; Muse is adding a failing regression and repair
  in chain. Real provider profiles remain intentionally unqualified.
- Assessment and recommendations Muse workers are active in their own worktrees
  (`ptest-assessment`, `ptest-report`) with independent uv environments. Neither
  is integrated or approved yet. CLI/init orchestration, actual provider
  qualification, copy smoke, final full suite, and final audit remain.
- Provider repair 7e0c184 closed the normal descendant-exit regression but
  audit rejected numeric PID reuse and unreadable identity. Pidfd repair
  2256ab9 passed 40 focused tests independently; audit rejected late-spawn
  survivors after SIGKILL enumeration and partial-validation pidfd leaks.
  Muse is repairing those two in chain. All real adapters still unqualified.
- Assessment task 6edc977 passed 15 focused tests independently; scoped audit
  rejected unknown-field projection, filename-only N/A, and model-supplied
  scores. Muse is repairing in its task worktree; no approval yet.
- Report task 2d26b1d passed 28 focused tests independently; scoped audit
  rejected missing source-drift recheck, swallowed parent-sync failure, and
  scope injection. Controller additionally found fail-open lock acquisition.
  Muse is repairing in its task worktree; no approval yet.
- Added .graphifyignore in active worktrees to exclude all historical
  .pipeline records from future AST refreshes. An initial update after the
  exclusion needed --force because the graph corpus intentionally shrank;
  subsequent AST updates no longer enumerate old pipeline files. Existing
  cached graph may still contain old nodes, so do not query it for context.
- Integration contract risk: assessment SourceExcerpt.sha256 hashes the admitted
  (possibly truncated) prefix, while report source_proof currently expects a
  full-file sha256. CLI must reconcile this explicitly and cover truncated
  excerpts with a stale-source negative; never claim source drift is checked
  by passing an incompatible digest. The report worker ran `ptest --full` in
  its task worktree through a shell pipe that masked ptest's guard errors;
  that is diagnostic only, not the final integrated gate.
- Provider task 48c5733 passed one 42-case focused run but controller
  immediately reproduced a real timing failure: 1 failed/41 passed when a
  pinned descendant exits before /proc start-time read; code reports
  unverifiable group rather than recognizing a dead pinned process. Another
  run returned exit 70 despite 42 pytest passes (guard protocol mismatch).
  Do not treat provider task as stable or approved; scoped audit/repair needed.
- Provider exit-race repair d4af53d passed three consecutive controller
  scoped runs (43 passed, exact exit 0), and its scoped audit approved.
  Provider qualification remains a separate all-three release gate; Muse is
  testing only synthetic scratch canaries and has not enabled adapters.
- Assessment final repair 2d19287 passed 21 controller focused tests and
  scoped audit approved: raw provider/publication removed, score ptest-owned,
  model-supplied N/A rejected in v1 (unknown stays in denominator).
- Contract 778683a, assessment 2d19287, and banner c4ac4bf merged serially
  into chain after provider task stopped; `graphify update .` completed with
  .pipeline excluded. Combined scoped `ptest` gate: 182 passed, exit 0.
- Report task 9a8be6f passed 38 controller focused tests but re-audit rejected
  fixed 64KiB proof prefix under smaller EvidenceLimits and inline-code
  backtick escape. Muse is adding explicit admitted-byte-count proofs and safe
  command display in isolated report worktree. No report merge yet.
- Report final repair 8a033b7 passed 45 controller focused tests, exit0, and
  scoped audit approved. Source proof now carries exact admitted byte_count,
  nofollow-walks parents/leaf, hashes only admitted prefix, and keeps command
  display outside inline backtick spans. Beyond-prefix drift still requires
  CLI packet recomputation. Do not merge while provider qualification worker
  owns chain HEAD.
- Synthetic OpenCode stdin probe of its currently selected default model
  returned HTTP 403 AccessDenied.Unpurchased from configured endpoint. This
  is not a qualification result or permission to pick a different paid model;
  worker is checking existing authorized profiles without uploading source.
- Qualification worker was terminated when it attempted to query local
  pre-today Codex thread history, outside task authority. Those results are
  unused. Scratch-only Claude and Codex canaries returned refusals but do not
  prove full containment (Codex tools not disabled); OpenCode repeated HTTP
  403. All adapters remain unqualified. Approved report branch 8a033b7 was
  merged after worker stop; graphify updated; combined scoped ptest gate:
  227 passed, exit 0. No final full suite because CLI and all-three release
  gate are incomplete. No push, main merge, or global install.
- CLI/init orchestration, all-three real provider qualification, copied-repo
  smoke, final full ptest, and final audit remain. Child-worker `ptest --full`
  counts are not accepted as the final gate; some shell pipelines masked ptest
  protocol errors.
