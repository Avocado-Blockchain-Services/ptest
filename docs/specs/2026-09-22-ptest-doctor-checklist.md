# ptest doctor readiness and assessment checklist

Status: implementation-ready specification, 2026-09-22; baseline `f9637065a3b1305a269efa6e9107dcdafb118d17` on `cx-doctor-checklist`.

## Outcome

`ptest doctor` run from a standalone repository or configured v2 monorepo root first renders a
per-repository execution/parallelism/selection/timing table, then one authoritative worksheet, grouped
findings, and coverage limits. `--prompt` requests an evidenced filled worksheet. ptest stays local,
bounded, and model-independent.

This is an extension of doctor, guide, and prompt behavior. It is not an agent runner, report-ingest
protocol, repair command, or new public document version.

## Baseline facts and preserved interfaces

- `doctor.inspect()` performs bounded regular UTF-8 reads, imports no repository code, and currently
  emits static-pattern hypotheses plus four `unknown` readiness entries.
- Human output groups findings; prompt evidence stays JSON escaped, delimiter-contained, and capped by
  `MAX_PROMPT_BYTES`.
- `doctor --json` keeps the strict shared v1 codec and `docs/schemas/v1/doctor.json`; no public field,
  enum, required key, schema version, or envelope shape changes.
- Preserve `ptest doctor`, `--scope`, `--json`, `--prompt`, `--max-entries`, `--max-files`,
  `--max-file-bytes`, `--max-total-bytes`, explicit `--probe`, and `ptest guide`.
- `--json` and `--prompt` remain mutually exclusive. Probe-only options and incompatibilities remain
  exactly as parsed today. `--probe` still calls the existing operation path, executes only after the
  user's explicit flag, emits no checklist, and supplies no inferred or cached static readiness.
- `ptest guide --write` retains exclusive creation and never replaces a user file.
- Static doctor never runs tests/services, installs dependencies, imports app code, invokes a model or
  agent, contacts the network, writes the inspected repository, or treats an LLM worksheet as proof.
- Requesting doctor, guide, or a prompt grants assessment authority only. Source repair requires a
  separate user instruction granting repair authority.

## Readiness contract

The displayed vocabulary is `ready`, `not ready`, and `unknown`.

| Display | Existing v1 JSON state | Meaning and setter |
|---|---|---|
| ready | `ready-for-declared-capability` | ptest only, from direct trusted evidence sufficient for that area; static pattern absence is never sufficient. This slice produces no `ready` state. |
| not ready | `blocked` | ptest only, from a conclusive blocker such as missing/invalid/unsafe required configuration or explicitly disabled selection. A hypothesis alone cannot set it. |
| unknown | `unknown` | ptest when evidence is absent, indirect, incomplete, truncated, or only a static hypothesis. |

The four rows are ordered `execution`, `parallel`, `selection`, `timing`; human output labels the second
`Parallelism`, while JSON retains `parallel`. For a valid config, execution and parallelism remain
unknown because static inspection ran nothing and proved no worker isolation. Selection is `not ready`
when disabled or invalid, otherwise unknown. Timing is unknown without measured per-test data. A
missing standalone config, or missing/invalid/unsafe child config, blocks execution and selection;
the other areas stay unknown. Invalid existing root configs instead follow the fatal-error rule below.

Aggregate JSON readiness is the area-wise worst evidenced state in repository declaration order:
`blocked` if any included repository is blocked, otherwise `unknown`; `ready-for-declared-capability`
would require every included repository to be ready. Reasons carry the root-relative repository path.
Findings are retained regardless of every readiness state.
Readiness is per capability: disabled selection does not prevent serial/full testing. Do not enable
selection or declare isolation merely to turn a row green; configuration assertions need real evidence.

## Reviewed worksheet contract

Worksheet statuses are `pass`, `needs work`, `unknown`, and `not applicable`.

- ptest emits every row as `unknown`; ptest static patterns never fill or upgrade a row.
- A human or invoking LLM may set `pass` only after inspecting direct source/config evidence and any
  required runtime evidence; runtime claims must name the ptest result or command.
- A reviewer may set `needs work` after verifying direct source/config/runtime evidence. A doctor
  pattern may direct the review, but is labelled `static hypothesis` and is not the conclusion.
- `unknown` requires a reason such as an unread file, missing runtime observation, ambiguity, or
  truncated scan. A truncated scan can never support a claim that review is complete.
- `not applicable` requires affirmative evidence that the boundary is unused or the criterion truly
  does not apply. A clean scan is never pass or N/A.
- No timing data means timing remains unknown; source naming, sleep duration, or a clean scan cannot
  establish a timing pass.
- Factories are guidance, not a mechanical mandate for pure tests. The review must not prescribe a
  particular ORM, require Redis/Valkey where none is used, or force a rewrite without a demonstrated
  isolation, determinism, selection, or performance benefit.

Each filled row has repository, stable ID, criterion, status, evidence, recommendation/example,
verification, and reason for `unknown`/`not applicable`. Evidence is labelled `static hypothesis`,
`runtime evidence`, or `reviewer conclusion`.
The completed LLM report starts with its own per-repo table explicitly labelled `Reviewer assessment`.
A reviewer may recommend ready there only with cited evidence for the stated checkout, scope, and
capability; this does not update the CLI table. Parallel evidence must show workers actually granted
and exercised, not merely a `--workers 2` request. A full-suite pass alone proves neither cleanup
ownership nor complete selection inputs.

## Authoritative checklist catalog

`src/ptest/checklist.py` owns one immutable ordered catalog consumed by renderers and guide content.
Each entry stores ID, criterion, evidence request, recommendation, example, verification, and the
packaged recipe name where applicable; no renderer keeps a second list.

| ID | Criterion | Required evidence and safe recommendation/example | Verification |
|---|---|---|---|
| `FIX-001` | Fixtures/factories create fresh test records without weakening assertions. | Cite fixture/factory definitions and callers. Prefer a small factory when records vary; pure tests need none. Reuse `recipes/factories.md`. | Scoped ptest proves original assertions and inventory remain. |
| `FIX-002` | Mutable fixture state is isolated or reset for every test. | Cite fixture lifetime, mutation, and reset boundaries. Replace shared mutable state or prove deterministic reset. Reuse `recipes/factories.md`. | Run scoped order/worker permutations through ptest. |
| `DB-001` | Expensive database/server/schema setup is reused per run or worker, not repeated per test. | Cite setup scope and cost. Prefer one owned database/schema template per run/worker; no ORM is mandated. Reuse `recipes/databases.md`. | Measured scoped ptest run shows setup reuse without semantic loss. |
| `DB-002` | Database identities, records, and cleanup have explicit run/worker ownership. | Cite name derivation and teardown. Remove only owned records/namespaces; never infer ownership from a test-like name. Reuse `recipes/databases.md`. | Neighbor database/schema sentinel survives concurrent teardown. |
| `CACHE-001` | Redis, Valkey, and other mutable caches are namespaced and cleaned by owner. | Cite key prefix and deletion path. Prefer checkout/run/worker prefixes and owned-key deletion; verify any claimed disposable-service exclusivity before judging a global-flush hypothesis. Never recommend blanket flush as a repair. Reuse `recipes/cache.md`. | Neighbor key survives concurrent cleanup. |
| `RESOURCE-001` | Writable files and listening ports are uniquely owned and released. | Cite temp-root and port allocation. Use run/worker temp roots and OS-assigned ports. Reuse `recipes/files-ports.md`. | Concurrent scoped runs use distinct paths/ports and preserve a neighbor sentinel. |
| `NETWORK-001` | External network is denied or replaced by a declared isolated fake. | Cite clients, targets, and denial/fake boundary. Do not require a live service for ordinary tests. Reuse `recipes/time-network.md`. | Scoped ptest succeeds under network denial or against the declared local fake. |
| `PROCESS-001` | Child processes remain owned, joined, cancelled, and reaped. | Cite spawn and teardown paths. Keep descendants in the owned foreground process group; do not detach. Reuse `recipes/processes.md`. | Cancellation leaves no owned descendant and does not affect a neighbor process. |
| `TIME-001` | Clocks and synchronization are deterministic. | Cite clock injection and barriers. Prefer fake clocks/events over wall-clock sleeps. Reuse `recipes/time-network.md`. | Scoped ptest repeats without wall-clock waiting or race-dependent outcome. |
| `SELECT-001` | Test selection has declared closed inputs and a conservative full fallback. | Cite policy, source-to-test mapping, environment, generated outputs, and full triggers. Dynamic unknown input widens to full. | Exercise input changes through authorized ptest execution and inspect actual selection/fallback artifacts; static plan previews are insufficient. |
| `TIMING-001` | Per-test timings are measured and interpreted without hiding integration work. | Cite ptest result/history timing. `<0.5s` healthy, `0.5–<2s` inspect, `2–3s` optimize, `>3s` investigate or justify; these are guidance, not failures. | Repeat the scoped ptest measurement; absence of timings stays unknown. |

## Human and prompt presentation

Human output order is fixed:

1. `ptest doctor`, the static-only caveat, and a Markdown readiness table with one row per included
   repository in declaration order and four displayed states.
2. Scan coverage, including incomplete/truncated status.
3. The worksheet catalog once, marked `Reviewer fills one copy per repository`; all initial statuses
   are unknown and every unknown-reason cell says `review not yet performed`.
4. `Static hypotheses`, grouped first by repository, then severity and finding code, with count and
   one root-relative example. `none found` retains the non-certification caveat.
5. Configuration diagnostics and scan limitations, followed by the existing next-command hint.

`--prompt` changes the leading instruction from repair to assessment. It asks the LLM to inspect named
repositories, return readiness recommendations and all worksheet fields, distinguish evidence classes,
cite file/line evidence, give examples, and provide ptest-only verification commands. It may recommend
repairs but says edits require authorization and never claims its text updates ptest readiness.

Guidance must distinguish a proposed command from an observed result. From a monorepo root,
recommend supported scoped execution such as `ptest api/tests/<chosen-test>.py`; label placeholders
as examples requiring a real scope. Do not recommend root `doctor --probe`: that route is unsupported
today and unchanged here. Standalone probe suggestions must explain their existing isolation/config
prerequisites. Existing authority to run tests or repair remains valid; do not ask for it again.
For nested child declarations, current scoped execution also lacks routing support: report that
limitation and use the supported root full gate when execution is authorized, not invented commands.

Trusted instructions, catalog, and caveats precede
`BEGIN UNTRUSTED DOCTOR EVIDENCE`. Findings, limitations, and readiness reasons remain one complete
JSON record per line inside delimiters. Prompt bytes stay at or below `MAX_PROMPT_BYTES`; the existing
marker identifies evidence truncation, never a partial valid-looking record. `ptest guide` includes the
same rules/catalog plus existing packaged guide and recipe examples; missing resources fail closed.
Repository labels and scan context are untrusted metadata even when placed before the delimiter:
encode them as single-line escaped JSON, never raw Markdown or interpolated instructions/commands.
Reserve space for the complete fixed worksheet and all numbered repository rows; bound labels and
record any shortening explicitly. Full labels may appear as evidence only when the bound permits.

## v2 monorepo behavior

Only children explicitly listed by the resolved root v2 manifest are considered; doctor never discovers
sibling projects, Terraform runners, or nested ptest processes. Declaration order governs display,
scan, aggregation, and prompt. Children never borrow config, test roots, evidence, or budgets.

Scope behavior is frozen:

| Input | Behavior |
|---|---|
| no `--scope` | Include every declared child, including missing/invalid/unsafe entries. Do not scan the monorepo root as an implicit project. |
| `--scope CHILD` | Include that whole declared child and use its configured test roots as scan priorities. |
| `--scope CHILD/PATH` | Include only that child; validate the root-relative path, remove the child prefix for scanning, and retain the full root-relative scope in display/JSON. |
| safe but undeclared scope | Exit 2 with `invalid-config` before scanning; do not fall back to discovery. |
| absolute, empty-segment, dot-segment, control, oversized, symlink-escaping, or otherwise unsafe scope | Exit 2 with `unsafe-path`, do not echo hostile input, and produce no partial success report. |

The unsafe-scope rule also rejects backslashes and drive-qualified paths. Validate an explicit scope
before any source scan; an explicitly selected child symlink is an unsafe scope. Without an explicit
scope, unsafe child declarations instead receive diagnostic rows. Missing directories or configs are
accounted for as unavailable rows, not silently omitted. Successful diagnostic reports exit 0 even
when readiness is blocked; fatal root/scope errors exit 2, using the existing JSON error envelope.

Nested declarations are matched by exact declaration or `declaration + "/"`; manifest overlap is
already forbidden. Every displayed finding path and reason path is prefixed with the declaration.
Whole-child scope displays the declaration itself, never an empty path.

A missing/unsafe child or missing/invalid child config gets a visible row: execution and selection are
not ready with a bounded diagnostic; parallelism and timing are unknown. A safely reachable child is
still scanned without config priorities; an absent/unsafe directory is not. Valid siblings continue
and keep findings. This is doctor-only; execution `monorepo.preflight_children()` stays fail-fast.

A malformed, unsupported, or unsafe existing root config (v1 or v2), including an invalid/overlapping
declaration, exits 2 through the existing public error envelope; never guess the project topology.
An unconfigured
single repository is scanned as repository `.`, with execution and selection not ready and the
initialization-required reason visible. It is never auto-discovered as a monorepo by doctor.

## Aggregate budget and bounded reporting

The user-selected `ScanLimits` are one workspace-wide budget, never multiplied by child count.
Entries, files, total bytes, findings, elapsed time, and serialized JSON output are global counters.
Per-file bytes, AST nodes, and depth retain their present fixed limits.

Before each child, let `R` be the remaining entries/files/source-bytes/findings allowance and `K` the
remaining child slots. The child gets `ceil(R / K)`, capped by `R`; unused quota returns to the pool. Its
deadline is the earlier of the global deadline and an equal remaining-time share. A capped child stays
visible with unknown affected readiness and a root-relative `scan-limit`; later children are not
omitted. An unscannable slot consumes reporting space only; a reachable child scanned without a valid
config debits its actual source work. Zero allowances mean no further work of that kind, not invalid
configuration. A config not inspected before deadline expiry is unknown, never inferred invalid.

Do not divide `output_bytes` into per-child serialized envelopes: the standalone scanner enforces a
4096-byte minimum, which would fail with many children. Use the same underlying scan collector for
standalone and workspace inspection, with global payload admission and one final aggregate envelope.
Internal child report views are not separately emitted; metadata and rebased evidence count against
the final JSON cap. Bound/no-follow child config reads using the existing configuration byte limit;
start the global deadline before child diagnostics and account for their read work.

All declared children receive a numbered readiness row. Repository labels are terminal-safe and
bounded; if labels cannot fit, each row remains present by number, truncated labels are explicit, and
a limitation records label truncation. The checklist catalog is rendered once. Human findings show
at most one example per repository/code group. Human and prompt output are each capped at
`MAX_PROMPT_BYTES`; overflow is explicit and never presented as complete.

## JSON v1 preservation and internal model

The public JSON is one aggregate `DoctorReport`:

- `scope` is `[]`, `[CHILD]`, `[CHILD/PATH]`, or the standalone relative scope exactly as inspected;
- findings and reason paths are root-rebased and ordered by repository then current scan order;
- readiness uses the aggregate rule above;
- `limits` is the single requested workspace budget;
- `usage` sums actual counters and measures elapsed time across the whole inspection; `truncated`
  records scan or aggregate JSON evidence truncation, and `output_bytes` is the final compact public
  document size under the existing accounting;
- limitations identify affected children through existing `paths`, without adding repository fields.

For root monorepo scope `[]`, include an ordinary bounded limitation explaining that only declared
children were inspected and root content was excluded; preserve affected paths where the cap permits.

Unknown fields are not smuggled into evidence, domain, or messages. The codec, projector, generated
schema, and fixture remain the byte-shape authorities. Per-child UI uses human/prompt output in this
slice, not a fabricated v1 extension.
Human/prompt-only truncation has its own explicit marker and never mutates the typed report, JSON
usage, or scan-completeness claim. Missing/unsafe child diagnostics remain bounded even when evidence
must be dropped; the human/prompt numbered rows still account for every selected declaration.

Freeze these internal shapes in `src/ptest/doctor.py`:

```python
@dataclass(frozen=True, slots=True)
class RepositoryInspection:
    declaration: str       # "." or root-relative v2 declaration
    local_scope: str | None
    report: C.DoctorReport
    config_problem: C.Problem | None

@dataclass(frozen=True, slots=True)
class WorkspaceInspection:
    scope: tuple[str, ...]
    repositories: tuple[RepositoryInspection, ...]
    aggregate: C.DoctorReport

def inspect_workspace(domain: C.DomainPaths, resolution: C.ConfigResolution,
                      limits: C.ScanLimits, scope: str | None) -> WorkspaceInspection: ...
```

Keep `inspect(...) -> DoctorReport` for compatibility. Add a non-throwing doctor-only child diagnostic
helper in `monorepo.py` that reuses parsing without changing `preflight_children`. Preserve existing
renderer calls: `render_doctor(report: C.DoctorReport, *, workspace: WorkspaceInspection | None = None)`
and `repair_prompt(report: C.DoctorReport, *, workspace: WorkspaceInspection | None = None)`. CLI passes
the aggregate report and its workspace context; JSON receives only the aggregate. With no workspace,
direct report callers remain supported and prompt metadata copies their readiness entries in order,
including missing/repeated areas, rather than silently normalizing the public report.

## Owned implementation files

- `src/ptest/checklist.py`: catalog and bounded packaged-recipe loading.
- `src/ptest/doctor.py`: workspace orchestration, budget ledger, readiness, rebasing, aggregation.
- `src/ptest/monorepo.py`: doctor-only declared-child diagnostics; execution preflight unchanged.
- `src/ptest/render.py`: readiness table, worksheet, grouped per-repo findings, assessment prompt.
- `src/ptest/cli.py`: route static doctor through `inspect_workspace`; probe branch unchanged.
- `src/ptest/resources/agent-guide.md`, `repository-agent-guide.md`, and existing recipe Markdown:
  align assessment/authority wording and concrete examples with the catalog without duplicating it.
- `tests/ng/test_doctor.py`, `test_render.py`, `test_cli.py`, `test_contracts.py`,
  `test_agent_rules.py`, `test_install.py`: positive, negative, contract, resource, and wheel coverage.
- `tests/ng/test_doctor_smoke.py`: synthetic smoke-harness regression cases and an opt-in real-repo
  smoke test driven by a task-owned manifest. Ordinary suite runs do not read `/home/ingmar/code`.
- `docs/schemas/v1/doctor.json`, `pyproject.toml`, `.ptest.toml`, runtime protocol/config, and other
  repository files are not implementation targets unless a failing packaging assertion proves the
  existing recipe glob insufficient. The doctor JSON schema must not change.

## Security and negative contracts

| Axis | Applicability and negative contract |
|---|---|
| Identity | N/A: local doctor has no identity or credential decision. It must still exclude credential-like files. |
| Authorization | N/A: no multi-user object API. Assessment authority must not become repair/write authority. |
| Tenancy | N/A: no tenant data. Repository/child/run ownership is the analogous isolation boundary and may not be crossed. |
| Input | Hostile manifests, scopes, filenames, controls, source text, and prompt injection cannot escape roots, forge delimiters, become instructions, or enter terminal control flow. |
| State | Renames/symlink swaps cannot redirect a read; doctor writes no repo state; one child's failure cannot borrow or delete another child's state. |
| Exposure | Private/generated paths and raw source lines are not emitted; argv/secrets remain redacted; errors do not echo unsafe input. |
| Availability | One global finite budget bounds fan-out, bytes, AST work, time, findings, human output, JSON, and prompt output across up to 256 children. |
| Dependencies | Static doctor invokes no runner, nested ptest, shell, Terraform, model, network, database, cache, or service; packaged resource reads fail closed. |

Concrete abuse cases and required regressions:

- Escaping child symlink: retain an unsafe/not-ready row, read no target, and keep sibling findings.
- Early-child entry/finding flood: cap it at its fair share and scan later children from global remainder.
- Fake worksheet/delimiter/instruction source: keep escaped untrusted evidence; never change trusted state.
- Missing/malformed child config: retain its row and blocked aggregate while execution preflight stays unchanged.
- Clean or truncated scan: unsupported pass/N/A/ready remains unknown and truncation stays visible.
- Prompt mistaken for edit/test permission: trusted instructions prohibit both without later authority.
- Hostile paths, 256 long declarations, or multibyte exhaustion: bound output, account for every child, mark truncation.
- Attempted JSON migration: schema/projection tests prove exact v1 keys and legacy enums.

## Sequential implementation and verification plan

One Muse worker owns all implementation files; parallelism is one. Codex owns design, contract/security
verification, audit, integration, and smoke judgment. Both use this worktree, preserve unrelated edits,
and make only local commits. No merge to main, push, publish, deploy, migration, or global CLI replacement.

The generic feature-muse wrapper is not used: it rejects this non-Persea worktree root and emits the
unsupported `codex exec --prompt-file` form. The bounded fallback is:

```sh
muse exec --prompt-file <implementation-prompt> \
  --workspace /home/ingmar/worktrees/ptest/cx-doctor-checklist/ptest \
  --worktree existing \
  --worktree-existing /home/ingmar/worktrees/ptest/cx-doctor-checklist/ptest
```

Implementation order is test-first:

1. Add failing catalog/status and renderer tests, including clean/truncated/timing-absent cases.
2. Add failing standalone and v2 routing tests for every frozen scope/config/budget behavior and
   child isolation/path rebasing.
3. Add negative boundary tests from the abuse cases; observe and record genuine RED evidence.
4. Implement the catalog, workspace inspection, aggregation, renderer/prompt, CLI route, and guide
   wording minimally; leave probe and execution preflight unchanged.
5. Run focused GREEN commands only through ptest:

   ```sh
   ptest tests/ng/test_doctor.py tests/ng/test_render.py tests/ng/test_cli.py
   ptest tests/ng/test_contracts.py tests/ng/test_agent_rules.py tests/ng/test_install.py
   ```

6. Prove strict doctor v1 schema/key sets unchanged, packaged guide/recipes load from an installed
   wheel, prompts/human output are bounded, and no static path executes or writes.
7. Run `ptest --full`, then `graphify update .` in this checkout. Record exact commands, exits, and
   results; do not claim them before execution.
8. Codex runs the required `audit-spec` review against this specification, the diff, RED/GREEN
   evidence, negative contracts, full gate, and graph update. Muse performs any repairs sequentially,
   followed by affected ptest scopes and renewed audit. Any subsequent source repair requires a fresh
   final full gate and graph update before completion; do not repeat gates after documentation-only edits.
9. Run the actual-repository smoke gate below. Any smoke failure fails delivery even when
   `ptest --full` passed. Audit the retained smoke matrix before declaring completion.

## Required actual-repository smoke gate

Use these existing roots read-only:

1. `/home/ingmar/code/persea_content_maker_unified` (v2 target: api pytest + web Vitest; Terraform excluded).
2. `/home/ingmar/code/scammeter/persea_scam_meter` (standalone pytest).
3. `/home/ingmar/code/scammeter/persea_scam_meter_ui` (standalone Vitest).

Create a unique directory with `mktemp -d` beneath `/home/ingmar/worktrees/ptest/cx-doctor-checklist/`.
Record each source `HEAD`, porcelain status, and SHA-256 hashes of key manifests/configs before and
after; record paths/hashes, never private contents. Abort if a revision changes during capture.

Build each snapshot from committed blobs at the recorded revision: enumerate `git ls-tree -r -z
<recorded-HEAD>` and read each recorded blob ID with `git cat-file`, using argument arrays. Never use
the mutable index or checkout as the source. Copy only regular blob modes and reject absolute/dot-segment paths. Omit
and record symlinks, gitlinks, `.env*`, credential/key names, `.git`, `.ptest` state, `.venv`, `venv`,
`node_modules`, caches, coverage, build/dist output, and Terraform state/cache. Re-walk the snapshot
without following links and fail if any link or resolved path escapes. This preserves the working
repositories and avoids untracked secrets, dependencies, and state.

Record omitted paths/reasons and any dirty source files intentionally not included. The committed
monorepo snapshot will lack the original untracked ptest configs: run candidate `ptest init --agents
none` only inside that copy and require exactly api/web. On both standalone copies, capture an
unconfigured diagnostic first when applicable, then initialize the copy with `--runner pytest` or
`--runner vitest` and `--agents none`; capture configured diagnostics too. Record setup mutations and
preserve existing committed configs. Do not install app dependencies or edit original repos.

Build a fresh candidate wheel into a new empty smoke wheel directory with `uv build --wheel`. Create
a fresh CLI venv with `uv venv` and install the explicit wheel path with `uv pip install`; no `pip`,
editable install, downloaded ptest release, or global update is allowed. Clear `PYTHONPATH`, invoke
the venv's absolute `ptest`, and record wheel SHA-256, distribution version, `ptest.__file__`, and
hashes of installed guide/recipe resources. With isolated Python (`-I`), prove code/resources resolve
under that venv and match the candidate wheel, not source, global ptest, cache, or old build output.

For each snapshot, capture stdout, stderr, and exit code for absolute-binary `doctor`, `doctor --json`,
`doctor --prompt`, and `guide`. On the monorepo also run api/web whole-child scopes, a nested scope,
low-bound truncation, and undeclared/unsafe scope failures. Use an available syscall tracer to observe
process/network activity. `strace` is not installed on this host: the required fallback is a second
run using the candidate venv's isolated Python and a test-owned `sys.addaudithook` wrapper around its
installed console entry point. Block/log subprocess, fork/exec, system-command, and socket-connect
events before they occur; exercise harmless canaries to prove the hook is active. Run this audit first,
then compare its report shape with direct absolute-binary smoke after it passes. Record that hooks
observe Python events, not all
native syscalls; retain hermetic negative tests as the source-level backstop. Do not install a tracer
or claim kernel-level observation when none ran.

Validate and record:

- readiness tables precede the catalog and list the expected repository rows in order;
- all stable checklist IDs/fields and installed recipe examples are present;
- JSON parses through the unchanged v1 codec/schema and has only the frozen keys;
- paths are root-rebased, hypotheses remain distinct, unknown reasons and truncation are explicit;
- outputs satisfy bounds, invalid scopes fail, and Terraform is not discovered;
- no snapshot gains app dependencies, test/service artifacts, or doctor-caused source mutations;
- source statuses/key hashes before and after match exactly.

Feed one saved prompt to a focused read-only LLM pass. It fills at least three varied rows from real
file/line evidence, retains unknown without runtime/timing proof, recommends ptest verification, and
edits nothing. Its output is a review artifact, not imported readiness or runtime proof.

Retain under the smoke directory the command manifest, candidate provenance, redacted captures,
traces, source/snapshot integrity hashes, LLM sample, and a concise pass/fail matrix with limitations.
A failed assertion is a failed smoke gate. Any regression test added from smoke evidence is run via
ptest. No release download, push, publish, deploy, or global CLI replacement is part of this gate.

Codex writes a task-local manifest containing the three explicit source roots, snapshot destinations,
and candidate-wheel path. Run the opt-in smoke assertions from the ptest feature checkout using
`PTEST_DOCTOR_SMOKE_MANIFEST=<absolute-manifest> ptest tests/ng/test_doctor_smoke.py`. The harness invokes
the installed candidate's absolute binary for the application-copy commands; it never invokes their
native test runners. Keep the subsequent LLM worksheet review as a separately recorded artifact.

## Completion boundary

This specification claims no tests, Muse implementation, audit, graph update, wheel, smoke, or LLM
review ran. Completion requires the plan, full gate, security/audit evidence, and every smoke row to
pass; unknowns and limitations remain reported rather than converted to readiness.
