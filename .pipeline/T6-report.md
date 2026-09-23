# T6 report — review runtime, cheap-model discovery, CLI wiring, LOW test fixes

- status: DONE
- taskWorktree: /home/ingmar/worktrees/ptest/cc-doctor-init-v2/ptest-T6
- taskBranch: feature/doctor-init-v2-T6
- commit: 68f39b9 "T6: review runtime, cheap-model discovery, CLI wiring, LOW test fixes"
- base: 884506f (chain branch feature/doctor-init-v2 at session start)

## Scope kept

- No reference to `executability`, `plan_item_reviews`, `assemble_child`, or
  `ItemReview` anywhere in `src/ptest/cli.py`, `agent_providers.py`, `help.py`,
  `contracts.py` (grep exits 1). Doctor-flow wiring is left for T7.
- `tests/ng/test_init.py` untouched (T1 owns it in wave 1; LOW fix (b) is T7's).
- No migration, no push, no deploy. Fakes only; no real provider launched.
- Diff touches only T6-owned paths: `src/ptest/{agent_providers,cli,help,contracts}.py`,
  `docs/schemas/v1/agent-assessment.json`, `tests/ng/test_{agent_providers,cli,help,contracts,agent_doctor_acceptance}.py`,
  `tests/ng/fixtures/agent_providers/codex-debug-models.json` (new).

## Built (criterion -> evidence)

1. Contracts additive fields (`src/ptest/contracts.py`): `rows[i]["label"]`
   optional, validated 1..64 bytes plain text when present
   (`_check_aa_row`); `child["execution"]` optional, exactly
   `{"status","detail","fix"}` (`_check_aa_execution`); projection keeps both;
   JSON-schema descriptor extended (both optional); legacy docs still decode;
   `docs/schemas/v1/agent-assessment.json` regenerated with
   `scripts/export-schemas.py` (only that file changed, no drift).
2. `launch_review` keeps its positional shape, gains keyword-only
   `cancel=None` (`src/ptest/agent_providers.py`); pre-set event returns
   `cancelled` without starting a child; set mid-run stops the owned group.
3. `launch_reviews(adapter, requests, timeout_s, *, concurrency=4, on_done=None)`
   (`src/ptest/agent_providers.py`): aligned tuple, concurrency validated
   1..8, per-item failure stays per-item (Problem -> failed result), KI in the
   waiting thread sets the shared cancel event, joins workers via executor
   shutdown, raises `review-cancelled`. Waiting uses `concurrent.futures.wait`
   (attribute lookup, patchable).
4. `with_model` appends only `--model m` (claude) / `-m m` (codex), regex
   `^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$`, others raise provider-unqualified.
5. `discover_models` runs `[executable, "debug", "models"]` (sanitized env, no
   shell, 20 s, 4 MiB bound, stderr discarded), keeps `visibility == "list"`
   slugs in catalog order, `()` on any failure; non-codex returns `()`
   without launching. `cli_version` returns first `--version` line (<=128) or
   None. Private `_discover_model_entries` strips entries to
   slug/display_name/description for the pick request.
6. `cli._declared_review_model` (flag > `PTEST_REVIEW_MODEL` > claude `haiku`,
   else None; pure, subprocess-free) and `cli._resolve_review_model`
   (declared wins uncached; claude `haiku`; codex cache-per-(provider, CLI
   version) then one `launch_review` pick requiring an exact listed slug, else
   provider default). Pick request carries only listed
   slug/display_name/description triples.
7. Flags `--review-model MODEL`, `--review-concurrency 1..8` (default 4) on
   doctor and init+`--doctor`, same validity matrix as `--reviewer`
   (rejected with `--offline`/`--json`/`--prompt`/`--probe`/`--assessment-json`,
   init `--json`/`--dry-run`, and without `--doctor` on init).
8. Disclosure keeps its first sentence byte-identical; optional
   `calls/concurrency/model` kwargs append `This review makes <N> model calls
   (<C> at a time) with model <model>.` or the after-consent pick sentence.
9. `cli._where_payload` VITEST branch is now `exclusive_command` with the
   verbatim §3.4 message.
10. `help.py` doctor/init topics document the flags, per-item review, cheap
    model selection, the canary note
    ("tool-denial qualification must be re-run when the chosen model changes"),
    and "Citations are in recommendations.md".
11. LOW (a): new `test_tty_init_doctor_menu_selects_codex_without_consent_prompt`;
    `("doctor", "--reviewer", "auto")` added to the two-reviewer menu test.
    LOW (c): `test_non_tty_auto_never_shows_menu` deleted, its input ban
    folded into the parametrized non-TTY consent test.
12. Render-tolerant asserts: `created:?`/`unchanged:?` via
    `re.search(r"(created|unchanged):?\s+\.ptest\.toml")`; banner test creates
    `tests/test_example.py` before init (keeps `ptest --full` under T3);
    review-output checks assert scope names + `recommendations.md`, never the
    `Project | Execution` header.
13. New file `tests/ng/fixtures/agent_providers/codex-debug-models.json` holds
    the exact §6 capture; `discover_models` test pins the 5 `list` slugs.

## Verification (all via `ptest`, never pytest directly)

- `ptest tests/ng/test_agent_providers.py tests/ng/test_cli.py
  tests/ng/test_help.py tests/ng/test_contracts.py
  tests/ng/test_agent_doctor_acceptance.py tests/ng/test_doctor_smoke.py`
  -> **517 passed, 1 skipped** (skip is the opt-in external-manifest smoke).
- Adjacent-file regression check (read-only, files not owned):
  `ptest tests/ng/test_agent_assessment_contract.py` -> 142 passed;
  `ptest tests/ng/test_init.py` -> 57 passed.
- TDD: new tests were run before implementation and failed (28 provider +
  12 contract failures observed), then passed after.
- `graphify update .` run in the task worktree -> "Code graph updated."

## Self-audit (dan-jefferies passes 1-3 + audit-spec sweep)

- Re-read the full diff; two findings fixed before commit: discovery/version
  helpers consumed unbounded stderr via PIPE (now DEVNULL; stdout already
  bounded), and `_render_review_disclosure` accepted unvalidated
  calls/concurrency (now TypeError on non-int/negative).
- Verdict: APPROVE (self). No BLOCKER/HIGH. One NOTE: `launch_reviews`
  converts a systemic containment `Problem` (pidfd) into a per-item
  `provider-failed` result to preserve alignment; T7 maps it to
  "provider exited with an error", which is accurate.
- Tests are non-vacuous: concurrency peak asserted == 2 via fake timestamps,
  alignment via echoed ids, exact-slug pick (trailing-dot and hidden slugs
  rejected), over-long label / bad execution status rejected.

## Integration notes for T7 / controller

- Seam: `cli._resolve_review_model` calls the private
  `agent_providers._discover_model_entries` (one subprocess) rather than the
  public `discover_models`, so the pick request carries display
  names/descriptions. Both are T6-owned; patch either in tests.
- Seam: `cli._render_review_disclosure(..., calls=None, concurrency=4,
  model=None)`; pass `calls` + `concurrency=parsed.review_concurrency` +
  resolved model from the flow for the §3.9 sentence. `calls=None` preserves
  legacy text.
- Seam: model cache directory constant `cli._REVIEW_MODEL_CACHE_DIR =
  "review-models"`; T7 builds the root with
  `files.ensure_private_dir(domain.root, "review-models")` and passes it as
  `cache_root`.
- `provider.profile` format (`ptest-item-review-v1 model=...`, 128-byte
  fallback) and `provider.cli_version`/`"unreported"` are T7's to construct;
  contracts validation already accepts them.
- Known: `test_agent_doctor_acceptance.py` still pins base capability-row
  strings (`basic-serial; reviewed isolation unverified`,
  `not execution-verified`); those strings belong to the base renderer
  that T5 replaces, and T7 owns post-merge failures there.

## Fix report — audit findings (2026-09-23, task worktree ptest-T6)

Two CONFIRMED findings fixed; nothing else touched.

### [MEDIUM] KI fan-out test now proves every owned group stops
- File: `tests/ng/test_agent_providers.py::test_launch_reviews_keyboard_interrupt_raises_review_cancelled`
- Fake is now a pid-logging shell body (`echo $$ >> pids.log`, then hang 30 s)
  instead of bare `HANG`.
- The patched `wait` (`_boom`) waits until both pids are logged (bounded 10 s)
  before raising `KeyboardInterrupt`, so both children are provably alive at
  cancel time — no start-up race.
- After the `review-cancelled` raise the test asserts `elapsed < 10`
  (`timeout_s=30`; a broken cancel path would take ~30 s via executor
  shutdown) and calls `_assert_dead` on each of the 2 logged pids.
- Observed: test takes 0.23 s with the fix in place
  (`ptest tests/ng/test_agent_providers.py -k keyboard_interrupt`).

### [LOW] `launch_review` cancel is now keyword-only
- File: `src/ptest/agent_providers.py` — signature is now
  `def launch_review(adapter, packet, schema, timeout_s, progress, *, cancel=None)`,
  matching the frozen §3.9 form and the docstring.
- Caller audit: `src/ptest/cli.py` calls it positionally with 5 args at L970
  and L1371 (unaffected); no in-repo caller passes a sixth positional arg.
- Tests: `test_exact_function_signatures` now also pins
  `cancel.kind is KEYWORD_ONLY`; new `test_launch_review_cancel_is_keyword_only`
  asserts a positional sixth argument raises `TypeError`.

### Verification (all via `ptest`, in ptest-T6 worktree)
- `ptest tests/ng/test_agent_providers.py` -> **91 passed** (15.03 s).
- `ptest tests/ng/test_init_render.py tests/ng/test_agent_rules.py` ->
  **47 passed** (0.36 s).
- No push, no merge, no deploy; fakes only.
