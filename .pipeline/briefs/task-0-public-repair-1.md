# Muse / Dan public-boundary correction — implement, not preflight

Same muse-spark-1.3 medium, no delegation. Task0 worktree:
/home/ingmar/worktrees/ptest/cx-ng-product/task-0; reviewed HEADdbac887.
Own ONLY src/ptest/contracts.py, tests/ng/test_contracts.py, generated
docs/schemas/v1/*.json and src/ptest/runtime/protocol-v1.json when affected,
and .pipeline/out/task-0.json. Other branches own helpers. No shared-file edits
outside this ownership, no merge/integration, no production CLI stub.

Read COMPLETE .pipeline/local/public-review-1.json: Opus43turns rejected with
T0-8/HIGH producer privacy, T0-9/HIGH known-field validation holes,
T0-10/MEDIUM unhashable enum crashes, T0-11/MEDIUM private unknown fields accepted.
The original T0-1..T0-6 repairs were confirmed fixed; don't regress them.
Root independent78passed/0.13s and schema check0 do NOT cover these new failures.

Read AGENTS.md, .pipeline/context.md, .pipeline/briefs/muse-dan-common.md and
task-0-launch.md (exact paths; no broad personal config/history searches), approved
design public addendum, planT0 and current report including addendum_round.
Read full filesystem instructions, not unsupported skill aliases:
/home/ingmar/.codex/agents/dan-jefferies-agent.toml
/home/ingmar/.codex/agent-memory/dan-jefferies-agent/reference-muse-code-cli.md
/home/ingmar/.agents/skills/secure-by-spec/SKILL.md
/home/ingmar/.codex/plugins/cache/openai-curated-remote/superpowers/6.3.0/skills/receiving-code-review/SKILL.md
/home/ingmar/.codex/plugins/cache/openai-curated-remote/superpowers/6.3.0/skills/systematic-debugging/SKILL.md
/home/ingmar/.codex/plugins/cache/openai-curated-remote/superpowers/6.3.0/skills/test-driven-development/SKILL.md
/home/ingmar/.codex/plugins/cache/openai-curated-remote/superpowers/6.3.0/skills/test-driven-development/writing-good-tests.md
/home/ingmar/.codex/skills/graphify/SKILL.md

All owned writes are authorized through SHELL apply_patch. --disable-write disables
only non-shell writers. Use bare @@ for Update File, +lines without @@ for Add.
Pipe the patch straight into apply_patch; do NOT cat it into /tmp/patch files or
use Python/source rewrites. Logs/generated schemas are mechanical exceptions.

Resolve the four findings thoroughly with durable RED-first behavioral tests:
- T0-8: enforce recursive validate+allowlist at FIRST encode for data AND domain,
  not only after decode/re-encode. Reuse the authority/projectors, no divergent
  six-kind serializer family. Test FIRST emitted bytes for every kind and nested
  injection, domain and errors. Keep consumer tests independent: construct hostile
  JSON using test json.dumps, NOT the now-sanitizing producer under test, otherwise
  your decoder unknown-field tests become vacuous. No caller-liberal deviation.
- T0-9: no arbitrary objects through known string/list fields provenance/scope/
  artifact_id. Match identifier/bool/integer/null/bounds with frozen records and
  descriptors; history routes same RunResult checks. Audit the related known-field
  catalog, not only listed examples. Remove concrete schema/validator drift;
  derive shared field sets where practical, no speculative general JSON framework
  or dependency. Do not call four manually copied lists one mechanical authority.
- T0-10: all malformed closed-enum values (dict/list/bool/null where forbidden)
  produce typed safe Problems, public AND private. No broad catch that hides
  programmer defects. Give fixed messages, never repr input/exception/sentinels.
- T0-11: PRIVATE frame, per-kind payload, manifest, domain, grant, prepared-run
  and nested authority records reject unknown keys. Test each level and frame kind
  using independent malformed JSON. Keep version/length/nonce bounds and known
  defaults; no new control protocol fields or weakening existing strictness.

Close these directly related notes too:
- Remove special public run.command argv/env rejection. The author's already-
  approved addendum explicitly says all same-major additive unknowns, including
  argv/env-like fields, are dropped. It is NOT an unresolved author question:
  the producer allowlist will drop them and the consumer must do the same. Keep
  private unknown-field rejection separate. Update the conflicting old test.
- Replace input-interpolating public/private decoder error messages with fixed
  bounded machine messages; synthetic sentinel negatives verify no echo.
- Deduplicate the report's duplicate implemented JSON key without losing earlier
  evidence, and correct superseded producer-liberal/private-strict overclaims.

Use own venv and local UV_CACHE_DIR, supplied frozen PTEST_BOOTSTRAP. All tests
through scoped scripts/ptest-bootstrap, no raw runners/full/main push/live install.
Capture FULL raw RED/GREEN logs + immediate numeric exits in .pipeline/local.
Do not overwrite previous logs or mistake missing-symbol failures for behavioral
RED. Show assertion sensitivity for FIRST-encode/privacy and strict-private tests.
Run schema regeneration/check, scoped test_contracts plus sibling file/storage
tests read-only, durations, all three Dan passes, graphify update . AST-only and
git diff --check. Commit explicit owned files, return SHA/evidence/remaininggaps.
Root owns independent checks/re-review and combined gate; no self-launched audit.
