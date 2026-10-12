# ptest 0.6.3: full mode without coverage (`[runner] reuse`)

Status: approved by the user on 2026-10-12. They chose the opt-in first ("go ahead" on
the four-step suggestion: opt-in setting, doctor suggests it, trial in the user's
repos, default later).

## Problem

A pytest project enters ptest's full mode only by declaring coverage (`--cov` plus
`--cov-report` across `args`, `full_args` and `coverage_args`; 0.6 C5). The full mode
records a baseline after a clean `ptest --full`. That baseline is what lets ptest skip:
"already verified", "verified elsewhere", the monorepo "unchanged since green" skip,
and joining a running full run. Projects without coverage run the normal mode, which
publishes no history at all (`operations.execute` publishes only when `advanced`). So
their repeat `ptest --full` on identical code reruns everything. Measured on specvm
(0.6.2, clean tree, no change between runs): 1m31s, then 1m30s.

Since 0.6, declaring coverage no longer measures it, so the coverage declaration is an
accidental entry ticket. Making the full mode the default for every project is unsafe:
the full-mode bridge refuses things the normal mode accepts. Switching it on for every
project broke 117 of ptest's own normal-mode tests on 2026-10-11.

## Contract

R1. **Config.** `[runner] reuse` is a TOML boolean, default `false`, for pytest only.
A non-boolean value, or the key on any other runner, is `invalid-config` (exit 2)
before anything runs. The config writer emits `reuse = true` only when it is set.
`doctor --fix` never adds or removes it. A config with the key fails on ptest 0.6.2
and older, which reject unknown `[runner]` keys. The changelog says so.

R2. **Entry.** `reuse = true` qualifies a pytest config for the full mode exactly as
declaring coverage does. The same launcher and ptest-owned-controls checks still
apply: a config whose `args`/`full_args` carry unowned controls stays in the normal
mode. With the key absent or `false`, behaviour is byte-identical to 0.6.2.

R3. **Proof.** Nothing changes in what counts as proof. A run made before the opt-in
(normal mode) published nothing, so it can never satisfy a full-mode skip, elsewhere,
child-unchanged or join. The first opted-in clean `ptest --full` records the baseline.
A repeat `ptest --full` on the same clean tree reports "already verified". A failing,
refused or incomplete opted-in run records no proof.

R4. **Identity.** `reuse` is not part of the compatibility fingerprint (it is
`repr=False`, like `coverage_args`). Toggling it on a project that already declares
coverage changes nothing except the config file's own bytes, which are an input like
any other.

R5. **Narration.** The end line's `next:` hint is unchanged: `next: ptest --full
--cov before handoff` only when `coverage_args` is declared, otherwise `next: ptest
--full before handoff`.

R6. **Refusal names the fix.** When the full-mode bridge refuses a project that is in
the full mode only because of `reuse = true` (no coverage declared), the end line adds:
`· this project needs the normal mode: remove reuse = true from <cfg>`. The guide's
"rerun once alone" advice would otherwise loop. A project that also declares coverage
gets no such line, because removing the key would not change its mode.

R7. **Doctor.** Doctor's selection advice for a pytest project outside the full mode
names `reuse = true` as the way in, with coverage as the other way. `doctor --fix`
does not set it (it never runs tests, so it cannot know whether the project qualifies).

R8. **Docs.** README, help and changelog describe the key. The agent guide is not
changed: agents never set it.

## Negative contracts (abuse cases)

| Axis | Must not happen | Evidence |
|---|---|---|
| Input | `reuse = "true"`, `1`, `[]`, `{}` accepted | config table test, exit 2 |
| Input | `reuse` on vitest, go, cargo or command accepted or ignored | config test, exit 2 |
| Integrity | a normal-mode green satisfies a full-mode skip after opting in | test: green, opt in, `--full` runs |
| Integrity | an opted-in run whose conftest rewrites the exit status passes or records proof | test: refused, no baseline, fix line |
| Integrity | an opted-in failing run records a baseline | test |
| State | the key changes the compatibility fingerprint | identity test |
| State | an opted-in project's repeat `--full` reruns on an identical clean tree | test: second run is "already verified" |
| Regression | a config without the key changes tier, argv or narration | existing suite unchanged (6237 tests) |
| Regression | `doctor --fix` drops the key | test |

Identity, authorization and tenancy have no new surface (local CLI, same user, same
per-checkout stores).

## Out of scope

Making the full mode the default (0.7.0, after the trial); changing what the full-mode
bridge refuses; Vitest (its full reuse is already separate).
