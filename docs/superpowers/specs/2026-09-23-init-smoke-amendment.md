# Amendment: init smoke run (requirements section E)

Status: implemented 2026-09-23 in worktree `ptest-smoke`
(branch `feature/doctor-init-v2-smoke`). Section E was added after the
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
4. **Setup means skip, never install.** A project whose setup would run
   (new `operations.setup_blocker`, a read-only wrapper over the runner's
   own `_required_setup_state`) is skipped with
   `setup not done; run: <exact setup argv>`.
5. **Output is human-only, after the banner.** One `Smoke` section, one
   line per project: `passed: <cmd> (<s>)`, `failed: <cmd> (exit <n>)`
   plus up to 5 useful reason lines, `skipped: <cmd-or-project> (reason)`.
   Every field passes through `render.terminal_text`. `InitResult.details`
   and `serialize_init_result` are untouched, so `init --json` keeps its
   exact drift-guarded key set (smoke is confirmation for humans, not
   machine contract).
6. **Failure containment.** A failing smoke keeps the written config and
   init's exit status stays configuration-based. Runner problems become
   `failed`/`skipped` rows; smoke planning or infrastructure trouble is
   swallowed to `""` so init can never fail because of smoke.
7. Not-executable projects (per `executability.check_config`) are skipped
   with the executability reason; only `executable`/`caveat` projects run.

## Owned files

- `src/ptest/init_smoke.py` (new): candidate scan, plan, in-process run,
  `SmokeResult`/`SmokePlan`, `format_smoke`, consent parse.
- `src/ptest/cli.py`: `--smoke`/`--no-smoke` grammar, TTY prompt, smoke
  phase between the init banner and the review offer.
- `src/ptest/operations.py`: additive `setup_blocker` only.
- `src/ptest/help.py`: init topic documents the flags.
- `tests/ng/test_init_smoke.py` (new): 17 twins (see below).
- `tests/ng/test_init.py`: split the no-prompt TTY test (`--json`/
  `--dry-run` stay silent; `--no-doctor` gets its own smoke-prompt test);
  the no-qualified-reviewer test now declines the smoke prompt.

## Twins (tests first; RED observed, then GREEN)

Consent yes/no/EOF; non-TTY without `--smoke` never runs; `--smoke`
runs; `--no-smoke`; `--dry-run`; `--smoke`+`--no-smoke` conflicts;
`--json` keeps its exact document and never runs; setup-missing skips
with `npm ci`; stubbed failure keeps config and exit 0; hostile names
sanitized; monorepo fixture yields one smoke line per project (api
xdist-blocked, web setup-missing, zero executions); two real end-to-end
runs (pass and fail) through the real scoped runner in-process, proving
config preservation and exit 0 on failure.

## Deferred / controller-owned

- Cheap-model candidate pick (needs C.4 consent wiring in init).
- Persea validation (`api` serial, `web` vitest) and real-provider
  canaries: no persea access from this worktree by machine rule.
