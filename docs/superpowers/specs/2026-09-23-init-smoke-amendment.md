# Amendment: init smoke run (requirements section E)

Status: implemented 2026-09-23 in worktree `ptest-smoke`
(branch `feature/doctor-init-v2-smoke`); controller decision (b) on setup
gating implemented 2026-09-23 in worktree `ptest`
(branch `feature/doctor-init-v2`, round 3). Section E was added after the
`.pipeline/design.md` wave design, so it is recorded here instead of in
that document. E is authoritative; this note records the minimal decisions.

## Decisions

1. **In-process scoped execution, never a nested ptest.** Each smoke run
   calls `operations.execute(domain, config, RunRequest(mode=SCOPED,
   argv=(candidate,)))` directly — the same path `cli.main` uses for
   `ptest <path>` (isolation, timeouts, exit codes). No subprocess of the
   ptest binary, no provider, no model.
2. **Deterministic candidate only; model pick deferred.** `choose_candidate`
   returns the smallest test file under the project's test roots whose
   static scan is clean: `doctor.match_rules` codes with `db.`/`network.`/
   `process.` prefixes plus a conservative DB/network/service library-name
   regex. Files defining a test sort before files without one. The
   cheap-model pick (E paragraph 2) is deferred: it needs the C.4 consent
   plumbing wired into init, which does not exist yet.
3. **Consent.** `--smoke` / `--no-smoke` (combined or repeated use is
   `invalid-config`). TTY asks once per init —
   `Run a quick smoke test to confirm ptest works? [Y/n]` — listing each
   runnable `ptest <project>/<file>` command; empty/`y`/`yes` runs,
   anything else and EOF decline. Non-interactive runs only with `--smoke`.
   `--dry-run` and `--json` never run and never prompt (flag combinations
   stay silent no-ops, not errors). No runnable candidate anywhere means no
   prompt and no section.
4. **Setup gating (controller decision b).** A project whose setup would
   run (`operations.setup_blocker`, a read-only wrapper over the runner's
   own `_required_setup_state`) stays runnable in the plan with its setup
   argv attached, so the consent prompt still names its file. On a TTY,
   after the smoke consent, init asks explicitly per setup-owed project
   (sanitized): `ptest needs to run setup (<setup argv>) once before the
   smoke test; run it? [Y/n]`. On yes, the setup runs through ptest's own
   setup path — one scoped `operations.execute` call, which records the
   setup fingerprint — then the smoke runs with `no_setup=True`. On
   no/EOF/Ctrl-C the project skips. Non-TTY with `--smoke` never installs:
   the project skips with the working advice `setup baseline not recorded;
   run: ptest <candidate> (runs <setup argv> first)`. The smoke
   `RunRequest` ALWAYS has `no_setup=True` (anything still owed refuses
   and becomes a skip) and a short `queue_timeout_s` (a timeout is a
   skip). The executability verdict cannot contradict this: the setup
   caveat is shown whenever `[setup]` is declared (fix1 finding 1), so a
   setup-owed project reads `ready with caveats`, never `ready`.
5. **Output is human-only, after the banner.** One `Smoke` section, one
   line per project: `passed: <cmd> (<s>)`, `failed: <cmd> (exit <n>)`
   plus up to 5 useful reason lines, `skipped: <cmd-or-project> (reason)`.
   Stdout is flushed and `Smoke: running <cmd>` prints before each
   execution, so live output reads banner, then Smoke running, then runner
   output. A real failing test with no machine reasons says `see runner
   output above` instead of `no detail`. Every field passes through
   `render.terminal_text`, including the consent prompt. `InitResult.details`
   and `serialize_init_result` are untouched, so `init --json` keeps its
   exact drift-guarded key set (smoke is confirmation for humans, not
   machine contract).
6. **Failure containment.** A failing smoke keeps the written config and
   init's exit status stays configuration-based. Runner problems become
   `failed`/`skipped` rows; smoke planning or infrastructure trouble is
   swallowed to `""` so init can never fail because of smoke.
7. Not-executable projects (per `executability.check_config`) are skipped
   with the executability reason; only `executable`/`caveat` projects run.
8. **Monorepo planning uses the preflight path.** Children resolve through
   `monorepo.preflight_children` (as `ptest <path>` does); a preflight
   Problem skips every child. Candidate choice walks with the
   executability walker and reads through `files.read_regular`.
9. **Interrupts decline.** Ctrl-C at either prompt is a decline (skip),
   never a traceback. The smoke prompt answers at most once per init.

## Owned files

- `src/ptest/init_smoke.py` (new): candidate scan, plan, in-process run,
  `SmokeResult`/`SmokePlan`, `format_smoke`, consent parse, plus round 3:
  `setup_advice`/`skip_result`/`run_setup`, `SMOKE_QUEUE_TIMEOUT_S`,
  `SETUP_QUESTION`, preflight planning, shared walker/reader.
- `src/ptest/cli.py`: `--smoke`/`--no-smoke` grammar, TTY prompts
  (`_ask_init_smoke` sanitized, `_ask_init_setup`), smoke phase between
  the init banner and the review offer.
- `src/ptest/operations.py`: additive `setup_blocker` only.
- `src/ptest/help.py`: init topic documents the flags and setup gating.
- `tests/ng/test_init_smoke.py` (new): 17 twins (see below) plus 13
  round-3 twins.
- `tests/ng/test_init.py`: split the no-prompt TTY test (`--json`/
  `--dry-run` stay silent; `--no-doctor` gets its own smoke-prompt test);
  the no-qualified-reviewer test now declines the smoke prompt and fails
  on a second prompt.

## Twins (tests first; RED observed, then GREEN)

Consent yes/no/EOF; non-TTY without `--smoke` never runs; `--smoke`
runs; `--no-smoke`; `--dry-run`; `--smoke`+`--no-smoke` conflicts;
`--json` keeps its exact document and never runs; setup-missing skips
with `npm ci`; stubbed failure keeps config and exit 0; hostile names
sanitized; monorepo fixture yields one smoke line per project (api
xdist-blocked, web setup-missing, zero executions); two real end-to-end
runs (pass and fail) through the real scoped runner in-process, proving
config preservation and exit 0 on failure.

Round-3 twins (controller decision b and audit LOWs; RED observed, then
GREEN): runnable uv-locked pytest fixture with `[setup]` where TTY
yes/yes runs setup through ptest then passes; npm-locked vitest where
TTY setup-no and setup Ctrl-C skip with zero executions; non-TTY
`--smoke` skips with `setup baseline not recorded; run: ptest
<candidate> (runs <setup argv> first)`; the smoke `RunRequest` always
carries `no_setup=True` with the short queue bound; executability
`caveat` and the smoke skip agree on a setup-owed project; a real
failing smoke says `see runner output above` with banner, Smoke running,
then runner output in order; the consent prompt sanitizes ESC/newline
names (and the old vacuous `"\nforged"` assertion now forges); Ctrl-C at
the smoke prompt declines with exit 0 and no traceback; a monorepo child
executes via the `preflight_children` path and a preflight Problem skips
all; a queue timeout skips; the candidate scan uses the shared walker
and bounded reader.

## Deferred / controller-owned

- Cheap-model candidate pick (needs C.4 consent wiring in init).
- Persea validation (`api` serial, `web` vitest) and real-provider
  canaries: no persea access from this worktree by machine rule.
