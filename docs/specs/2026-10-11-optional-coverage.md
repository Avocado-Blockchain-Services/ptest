# ptest 0.6: coverage on request

Status: approved direction from the user on 2026-10-11 ("make it optional, not an
intrinsic part of running ptest"; "explicit flag only"; the agent rules and skill say
when to pass it).

## Problem (measured on persea api, 8 xdist workers, 8 cores)

| subset | raw `pytest -n auto` | raw + `--cov` | `ptest --full <folder>` |
|---|---|---|---|
| tests/llm (379) | 10.4s | 33.3s | 53.1s |
| tests/crawler (702) | 22.1s | 46.6s | 67.0s |

Coverage is the largest cost, and ptest makes it intrinsic:

- `adapters/pytest._qualified_profile_catalog` qualifies a pytest config only when
  `args + full_args` contain `--cov` and `--cov-report`; `operations.execute` refuses
  bare `ptest` (automatic selection) without that profile, and doctor's SELECTION
  advice says "add --cov and --cov-report to runner args".
- The bridge requires the pytest-cov/coverage pair for the advanced profile
  (`_coverage_tuple`), and `reports.AdvancedAttemptReport` rejects a report without
  complete coverage evidence.
- So every iteration run pays for coverage, although partial runs already neutralise
  the gate (`--cov-fail-under=0`). Coverage was the selection input before 0.5; 0.5's
  dependency recorder (`sys.monitoring`) no longer reads it.

## Contract

C1. **Config.** `[runner] coverage_args` (pytest only, default empty) holds the
coverage options. Accepted tokens (closed allowlist): `--cov`, `--cov=<src>`,
`--cov-report <type>` / `--cov-report=<type>`, `--cov-config <path>` /
`--cov-config=<path>`, `--cov-branch`, `--cov-fail-under <n>` /
`--cov-fail-under=<n>`, `--cov-context <ctx>` / `--cov-context=<ctx>`. `--cov` may take
one following value that does not start with `-`. It must contain at least one `--cov`
or `--cov=`. Anything else (another pytest option, a positional, `@file`, `--no-cov`, a
missing or dash-leading value, a non-pytest runner) is `invalid-config`, exit 2,
before anything runs. The config writer round-trips the key.

C2. **CLI.** `--cov` requests coverage for one run. It combines with bare `ptest`,
`--changed`, `ptest <path>`, `--full`, `--full <folder>` and `--full --again`. With
`--shadow`, `--setup-only` or probe options it is a usage error (exit 2).

C3. **Execution.** With `--cov`, a pytest project whose `coverage_args` is non-empty
runs `launcher bridge args coverage_args …` (coverage options right after the
configured args, before ptest-owned controls, `full_args`, roots and files). Without
`--cov`, `coverage_args` never reaches argv. `args` and `full_args` stay literal: a
config that still has `--cov` in `args` keeps measuring coverage on every run
(legacy; doctor flags it, C11).

C4. **Partial runs.** The existing rule holds with coverage from either source: when
the run's argv measures coverage and the plan is scoped or selected, ptest appends
`--cov-fail-under=0` unless a fail-under is set explicitly.

C5. **Declaring is not measuring.** The advanced tier (baselines, history, verified
reuse, parallel identity, automatic selection) is still chosen by declaring coverage:
`--cov` plus `--cov-report` across `args`, `full_args` and `coverage_args`, as before.
So every existing project keeps its tier, and `doctor --fix` copies tokens verbatim.
Whether a run actually measures coverage follows that run's argv alone. The
pytest-cov/coverage version pair is checked only when the run measures coverage. A
run with `--cov` and one without resolve to the same tier. Decoupling the tier from
coverage entirely is a follow-up. It would move projects without coverage onto the
stricter advanced bridge, which needs git source identity and refuses some conftest
hooks.

C6. **Evidence.** Complete coverage evidence (controller data plus every worker's
contribution) is required exactly when the run's ptest-owned argv measures coverage
(`args`, `full_args` or requested `coverage_args`); otherwise it is not consumed. The
expectation comes from the ptest-written report binding, never from the report
itself. The verdict never depends on it (unchanged).

C7. **Runtime identity.** Coverage-family facts (pytest-cov plugins and hooks, `cov*`
effective options, the `coverage` fact) are excluded from the identity hash, but
still reported, because the runtime-facts key set is fixed. `command_variants` stay the
configured `args` and `full_args` only, never `coverage_args`. So a baseline from a
run with coverage authorises a selected run without it, and the reverse. Upgrading:
`source._IDENTITY_PROTOCOL` moves to `ptest-source-v3`. A stored 0.5 baseline then
fails compatibility at planning time (`incompatible-baseline` → one full run) instead
of failing the identity check after the run (exit 70).

C8. **Snapshot compatibility** is the same for a run with and without `--cov`. The
request travels on the private effective config only (`runner.coverage_requested`),
and the compatibility fingerprint normalises it away, the same way it already
normalises the worker grant.

C9. **Reuse.** A `--full --cov` request is satisfied by an earlier green only when that
green measured coverage with the same coverage tokens on the same inputs. The proof
digest covers the coverage tokens actually in argv (`args` + `full_args` + requested
`coverage_args`), so a legacy config with `--cov` in `args` earns proof too. This
applies to "already verified", "verified elsewhere", the monorepo "unchanged since
green" skip, and joining a running full run. A plain `--full` may reuse a green with
or without coverage. It joins only a running full run with the same coverage mode.
The proof is stored additively: an extra key in `verified.json` and the lease, plus a
per-checkout marker file. Every full failure on a tree drops that tree's marker, the
same rule `verified.json` follows. Older ptest versions ignore the extra key; if an
older version rewrites the file and drops it, the next `--full --cov` reruns.

C10. **Monorepo and other runners.** `--cov` applies to every pytest child with
`coverage_args`. Each project with nothing to add prints one line and runs
anyway, with its exit status unaffected:
`ptest: [<child> ·] coverage not configured — add [runner] coverage_args to <cfg>`
for pytest, or `coverage not supported for <runner>` for Vitest, Go, Cargo and command
runners (Vitest coverage is out of scope). That keeps the generic rule
`ptest --full --cov` safe in every repository.

C11. **Narration and doctor.** A run whose argv measures coverage shows `· coverage`
in its start line. On a project with `coverage_args`, a green changed-mode run, and a
green `--full` without `--cov`, end with `· next: ptest --full --cov before handoff`.
Doctor no longer asks for `--cov` to enable selection. `doctor --fix` moves allowlisted
`--cov*` tokens from `args`/`full_args` into `coverage_args`, unless the config already
declares `coverage_args`. Other tokens, including `--no-cov*`, stay where they are.
That plan also drives the existing once-per-version notice ("ptest 0.6.0 can improve
this config … run ptest doctor --fix"), so every affected project hears about it after
upgrading. No new checklist item is needed. Dependency drafting treats
`coverage_args` like `args`.

C12. **Guides and skill.** The loop table's handoff row becomes
`ptest --full --cov`. The text says `--cov` adds the project's coverage and its gate,
and is never passed while iterating. The README, help and changelog are updated. The
old guide text joins the previous-version list in `agent_rules.py`, so refresh still
recognises it.

C13. **No self-dogfooding in this branch.** `config.py` rejects unknown `[runner]` keys,
so `coverage_args` in this repo's `.ptest.toml` would break the installed 0.5.x CLI
that gates the branch. Moving ptest's own coverage, and persea's, is a follow-up
after the release is installed.

## Negative contracts (abuse cases)

| Axis | Must not happen | Evidence |
|---|---|---|
| Input | `coverage_args` smuggles a non-coverage control (`-k`, `-m`, `--deselect`, `-p`, `-n`, `@file`, `--no-cov`, positional path, dash-leading value, empty token) | table-driven config test, exit 2, no run |
| Input | `coverage_args` on a non-pytest runner is silently ignored | config error test |
| State/integrity | a `--full` green without coverage satisfies `--full --cov` via skip, elsewhere, child-unchanged or join | one test per reuse path |
| State/integrity | a coverage green with different `coverage_args` satisfies `--full --cov` | test |
| State/integrity | a failed coverage gate (fail-under) leaves reusable coverage proof | test |
| State | a selected run after a coverage baseline (or the reverse) ends `report-invalid`/exit 70 | bridge identity twin test |
| State | a stored pre-0.6 baseline makes the first run after upgrade exit 70 | upgrade test (written first) |
| Integrity | a report declares itself exempt from coverage evidence | binding test |
| State/integrity | a failed full run leaves a coverage marker for that tree | test |
| Exposure/argv | a run without `--cov` carries any `coverage_args` token | argv test |
| Integrity | a requested coverage run with a worker missing coverage data passes as complete | existing parallel-coverage twins stay green |
| Compatibility | a 0.5.3-shaped verified ledger or lease fails to read, or reads as coverage proof | ledger and lease tests |
| Regression | a config with `--cov` in `args` stops measuring coverage | test |

Identity, authorization and tenancy have no new surface (local CLI, same user). Run
ownership is unchanged: a coverage request never joins or releases another mode's
lease.

## Out of scope

Vitest coverage; changing a project's coverage settings (persea's `sysmon` change is
the project's own decision); ptest's fixed per-run history overhead (separate work).
