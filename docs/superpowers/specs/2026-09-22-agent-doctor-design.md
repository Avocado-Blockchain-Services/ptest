# Agent-backed doctor and init diagnostic

Status: implementation in progress under the user's prior authorization;
model review remains disabled pending the all-three provider qualification
gate. This document is the design and implementation specification.

Provider process ownership and its release gate are amended by
[`2026-09-23-agent-review-containment-design.md`](2026-09-23-agent-review-containment-design.md).
The amendment supersedes process-group cleanup claims below; it records
unresolved host prerequisites and does not authorize provider launch.

## Outcome and boundaries

`ptest doctor` becomes an evidence-bounded review performed by an installed
Claude, Codex, or OpenCode CLI for every configured project. The same review is
offered in-process after a successful `ptest init`, including when the config
already existed. The review answers a deliberately narrow question: how much of
ptest's canonical test-quality checklist is supported by the supplied source
evidence, and what changes and later verification would close the gaps?

The review does **not** run tests, import project code, install packages, inspect
services, repair files, or grant an agent repository access. It is not coverage,
a performance prediction, a safety certificate, or proof that the suite runs.
Normal test execution remains offline and model-independent. `doctor --probe`
remains the separate, explicit live-test authority, and the implementation must
never invoke a nested `ptest` process.

Guidance-agent selection (`init --agents`) remains separate from model-review
consent. Finding Claude/Codex/OpenCode on `PATH`, installing a repository skill,
or repeating `init` never authorizes source sharing. Advice in
`recommendations.md` is not repair authority.

## User contract

### Command matrix

The parser adds these literal options while retaining every current scan-limit
and probe option:

```text
ptest doctor [--reviewer auto|claude|codex|opencode]
             [--allow-model-review] [--assessment-json]
             [--review-timeout SECONDS] [--scope PATH]
ptest doctor --offline [--scope PATH] [existing scan-limit options]
ptest doctor --json [--scope PATH] [existing scan-limit options]
ptest doctor --prompt [--scope PATH] [existing scan-limit options]
ptest doctor --probe --scope PATH [existing probe options]

ptest init [existing options] [--doctor | --no-doctor]
           [--reviewer auto|claude|codex|opencode]
           [--allow-model-review] [--review-timeout SECONDS]
```

The modes are closed and are rejected before scanning, writing, or launching:

| Invocation | Contract |
|---|---|
| `doctor` on a TTY | Resolve one qualified installed reviewer, show the disclosure, ask once for this invocation, then launch only after an affirmative answer. Decline renders the offline result and says optimization review is disabled, not that ptest is unusable. |
| `doctor` without a TTY or with `CI` set | Never prompt or launch. Return `consent-required` unless both `--reviewer PROVIDER` (not `auto`) and `--allow-model-review` were explicit. |
| `doctor --reviewer …` | Select review mode; it does not itself express consent in automation. `auto` uses the stable order `claude`, `codex`, `opencode`, considering qualified installed adapters only. |
| `doctor --offline` | Current bounded human static output, with no model call or report write. |
| `doctor --json` | Preserve the current `PublicDocument(kind="doctor")` static schema and bytes semantics; always offline. |
| `doctor --prompt` | Preserve the current bounded offline prompt; never launches a provider. |
| `doctor --assessment-json` | Agent review with the new assessment-v1 document on stdout; in automation it still requires explicit provider and consent. |
| `doctor --probe` | Existing explicit test/setup execution only. It rejects all reviewer, assessment, timeout, offline, and scan-output modes as applicable today. |
| ordinary TTY `init` | Finish normal init first, render its result, then disclose and ask whether to run the same in-process review. Existing config is still offered. A decline is successful init. |
| `init --doctor` | Request the offer/run. TTY still asks after disclosure unless `--allow-model-review` was also explicit. Non-TTY requires explicit provider plus consent. |
| `init --no-doctor` | Never offer or launch. |
| `init --dry-run` | Preview configuration/guidance only; never prompt for review, launch a reviewer, or write reports. Reject explicit review flags before mutation. |
| `init --json` | Preserve one coherent init JSON document and never prompt/launch. Reject combination with `--doctor`, reviewer, consent, or review-timeout; automation runs `ptest doctor` separately. |

`--doctor` and `--no-doctor` are mutually exclusive. `--reviewer`,
`--allow-model-review`, and `--review-timeout` require review mode and are
invalid with `--offline`, `--json`, `--prompt`, or `--probe`.
`--assessment-json` is incompatible with legacy `--json` and human init.
`review-timeout` is an integer from 10 through 900 seconds per child; the
default is 300. All conflicts fail before any provider process starts.

The disclosure names the provider and project, says that bounded source text is
sent to that provider using the user's existing account, that account/provider
costs may apply, and that ptest cannot perfectly detect secrets. It identifies
the excluded classes and offers `--offline`. Consent is invocation-local and is
not persisted.

Exit policy: a completed valid assessment exits 0 even when it contains quality
gaps; quality scores are not test failures. Invalid arguments, absent consent in
automation, unavailable/unqualified provider, invalid response, stale evidence,
or report conflict exit 2; review timeout exits 124; Ctrl-C exits 130. A requested
review failure after init preserves all successfully initialized files, clearly
reports `initialization succeeded; review incomplete`, and returns the review
exit code. Declining an interactive offer exits 0. No provider fallback or retry
silently spends additional tokens; the user explicitly reruns with another choice.

### Help and shared registration

`src/ptest/help.py` remains the single help source. Its overview changes the
doctor line to:

```text
  ptest doctor                    # consented CLI review; use --offline for static-only
```

Its machine-output line becomes:

```text
  Legacy doctor --json stays static; --assessment-json is the versioned agent result.
```

The doctor topic includes the exact syntax and matrix above, explicitly states
that review may share bounded source and incur provider cost, and retains the
probe execution warning. The init topic lists `--doctor | --no-doctor` and says
`--agents` installs guidance only. The agents topic replaces “no model APIs”
with “normal test execution requires no model; doctor review is separately
consented.”

Reviewer registration is a new shared constant, not an extension of guidance
skills:

```python
SUPPORTED_REVIEWERS = ("claude", "codex", "opencode")
```

It lives in `src/ptest/agent_providers.py` and is used by parsing, resolution,
help tests, and adapters. Gemini remains valid for `init --agents` but is not a
doctor reviewer. No other module keeps a second provider list.

## Assessment architecture

The current static scanner remains the admission layer: configured root/child
routing, safe relative scopes, typed problems, exclusions, and finite limits are
reused. Regex findings remain labelled hypotheses and can never satisfy a row.
Code absence is `unknown`, not `not-applicable`.

Three new small modules own the new boundary:

```python
# agent_assessment.py: bounded packet collection; pure validation and scoring
build_packets(workspace: doctor.WorkspaceInspection,
              resolution: C.ConfigResolution,
              limits: EvidenceLimits) -> tuple[EvidencePacket, ...]
parse_assessment(payload: bytes, packet: EvidencePacket) -> ChildAssessment
score(rows: tuple[AssessmentRow, ...]) -> Score | None

# agent_providers.py: qualification records and owned subprocesses
resolve_reviewer(name: str, env: Mapping[str, str]) -> ReviewerAdapter
launch_review(adapter: ReviewerAdapter, packet: bytes,
              schema: bytes, timeout_s: int,
              progress: Callable[[ProgressEvent], None]) -> ProviderResult

# recommendations.py: pure Markdown plus guarded publication
render_recommendations(run: AssessmentRun) -> bytes
publish_recommendations(root: Path, payload: bytes,
                        previous: PublishedIdentity | None) -> PublishResult

# cli.py: single shared entry for doctor and post-init review
_run_doctor_review(parsed: ParsedArgs, resolution: C.ConfigResolution,
                  domain: C.DomainPaths) -> int

# init_render.py: remain filesystem-free; caller supplies display context
render_init(result: C.InitResult, rules: object = None, *,
            dry_run: bool = False, agents: tuple[str, ...] = (),
            repo_name: str = "", color: bool = False) -> str
```

`cli.py` orchestrates these functions directly after one `inspect_workspace`
call; `init` calls the same in-process orchestration function. The review never
calls the CLI recursively. Public dataclasses are frozen, slot-based, validate
all strings and bounds in `__post_init__`, and use existing `Problem` codes plus
`consent-required`, `provider-unavailable`, `provider-unqualified`,
`invalid-assessment`, `stale-evidence`, and `report-conflict`.

### Bounded evidence packet

There is one packet and one provider invocation per declared selected child, in
manifest order. Fanout is sequential: at most 256 declared children, 300 seconds
per child by default, 1,800 seconds for the whole review, 64 source files and
512 KiB source bytes per child, 64 KiB per file, 1 MiB prompt input, and 512 KiB
combined provider stdout/stderr. Source collection bounds mean **partial evidence
coverage**, not a failed review: a valid completed assessment of that bounded
packet may publish a clearly labelled partial-coverage report. Checklist conclusions
must name their assessed scope; repository-wide claims lacking sufficient evidence
remain unknown. Lifecycle deadlines, output exhaustion, invalid responses, or
unreviewed children mean **review incomplete** and do not replace an old report.
These two states must not be conflated: ordinary large repos need useful reports.

Discovery admits regular files beneath the selected child without following
symlinks. It excludes `.git`, `.pipeline`, `graphify-out`, all agent instruction
and configuration locations (`AGENTS.md`, `CLAUDE.md`, `GEMINI.md`, `.claude`,
`.agents`, `.codex`, `.opencode`, `.gemini`), secrets/private names already
recognized by doctor, dependency environments, caches, coverage/build outputs,
generated/minified files, sockets/devices, and content that fails bounded UTF-8
text validation. The packet lists excluded/truncated counts so coverage limits
are visible. It never includes `.ptest` private runtime state.

Each admitted excerpt has a root-relative normalized path, inclusive line
bounds, SHA-256 content identity, and JSON-escaped text. The packet also contains
the authoritative child runner capability and statically parsed dependency
facts. Dependency inspection distinguishes:

- project declarations and authoritative locks;
- actual local environment metadata that can be read without imports or project
  execution (for example installed-distribution or package-manager metadata);
- `missing`, `unsupported`, and `uninspectable` observations.

It never treats ptest's own environment as the project environment, runs an
arbitrary configured launcher as inspection, installs anything, or changes a
lockfile. Advice must preserve the detected authoritative lockfile and use the
detected package manager (`uv`, npm, and so on). If provenance cannot be proven,
the prerequisite is unknown.

The reviewer receives the packet through stdin in a fresh task-owned scratch
directory, not as a repository path. Its fixed system request forbids tools,
file reads/writes, shell, browsing/network beyond the selected provider request,
MCP, hooks, plugins, skills, repository instructions, and custom models. No
repository-provided argv, prompt, hook, agent name, or configuration is used.
Normal CLI authentication is allowed, but ptest never reads, copies, logs, or
prints credential files.

### Mandatory provider qualification

No adapter is enabled merely because its executable or flags exist. Before
implementation consumes an adapter, the public-contract task freezes its exact
argv/environment and records adversarial qualification showing that hostile
packet text cannot cause file reads, file writes, commands, MCP/tools, hooks,
plugins, persistent sessions, or repository/user instruction loading. It also
proves bounded structured output and normal subscription authentication without
credential access by ptest.

Current evidence makes this a real implementation blocker. Claude's
`--safe-mode`, empty tools, strict empty MCP, disabled slash commands, no session
persistence, JSON schema, and print mode are promising but not yet proof.
Codex's read-only sandbox, ignored user config/rules, ephemeral execution,
disabled features/plugins/apps/browser/shell, output schema, and scratch `-C`
must be qualified as one fixed combination. OpenCode `--pure` only disables
external plugins and merged configuration may retain permissions/MCP; a fixed
owned agent with every permission denied and isolated config must be proven.
`--bare` is not an acceptable Claude fallback because it disables normal auth.

All three adapters are a single release gate. If any cannot meet the boundary,
the default feature remains disabled and reports `provider-unqualified`; the
implementation must seek a design decision rather than weaken containment or
ship a partial provider set.

### Process ownership and progress

The process-group cleanup design is withdrawn. A group can lose descendants
after `setsid`, double-fork, or normal leader exit. The dedicated cgroup-v2
boundary, mandatory fail-closed preflight, cleanup protocol, and remaining
release blockers are specified in the linked containment amendment. No
process-group fallback is permitted for agent review. There is no ptest daemon
or fake percentage.

TTY stderr uses a spinner with phase (`collecting`, `reviewing`, `validating`,
`publishing`), elapsed time, sanitized provider, and sanitized project. Non-TTY
stderr emits readable phase-transition lines and a heartbeat every 15 seconds
while a phase remains active, with the same fields. Partial
provider output remains diagnostic data in task-owned bounded state, never a
successful assessment.

## Strict assessment and report contracts

Each provider adapter parses its own bounded native envelope/event stream and
extracts exactly one final assessment object. Claude structured output, Codex
last-message output, and OpenCode events are not interchangeable wire formats.
Missing/duplicate terminal results, provider error envelopes, tool attempts, or
nonzero exits are failures even if an embedded object looks valid. The normalized
assessment must match `ptest.agent-assessment/v1`. It contains the packet identity, one child identity,
and exactly the 11 canonical checklist IDs once each in catalog order. Unknown,
duplicate, missing, or reordered IDs; extra properties; bad enums; unbounded
strings; or evidence outside the packet are rejected. Row status is one of
`satisfied`, `gap`, `unknown`, or `not-applicable`. `not-applicable` requires a
specific reason based on affirmative evidence; absence is not sufficient.
Evidence references packet path, line bounds, and content identity and must lie
inside a collected excerpt.

The model supplies prose findings, rationale, suggested changes, and a canonical
recipe ID only. It cannot supply a score, headline, execution proof, observed
command, arbitrary shell command, Markdown link, or HTML. ptest computes the
score as `satisfied / (all rows - justified N/A)`; unknown stays in the
denominator, and zero applicable rows produce no score. Display percentages use
integer floor; show the numerator and denominator beside the percentage. Only
deterministic configuration/dependency observations can establish an actual execution
blocker; model concerns remain review findings, not proof of inability to run.
An actual blocker overrides percentage wording. The permitted headline is, for
example, “80% of applicable checklist satisfied by review; see
recommendations.md for the remaining items.” It never says “ptest will work
great” or equates selection-disabled with an unusable ptest installation.

Human output order is fixed:

1. per-child capability table;
2. checklist satisfied/gap/unknown/N/A table;
3. findings;
4. specific improvement guidance.

The capability table has `Project | Execution | Parallel | Selection | Timing |
Checklist` columns. Execution distinguishes declared capability from observed
dependency prerequisites and says `not execution-verified`. Parallel displays
the runner's actual supported mode separately from reviewed isolation. Selection
distinguishes `disabled` from invalid configuration and unverified correctness.
Timing remains `unmeasured` without qualified measurements. Checklist displays
`8/10 (80%), agent-reviewed` with scope/coverage limitations. A dependency detail
row lists missing/unsupported/uninspectable prerequisites. Execution verification
remains `not run` during audit, independent of all review conclusions.
Pytest's declared capability remains `basic-serial`; an installed `xdist` alone
does not become parallel support. Worker parallelism and monorepo-root probe
support are out of scope and remain explicit unsupported capabilities.

`--assessment-json` returns a `PublicDocument` whose `kind` is
`agent-assessment`, with `data.schema = "ptest.agent-assessment/v1"`, ordered
children, validated rows, computed score or null, findings, limitations,
provider metadata, packet identities, and report publication result. Terminal
and Markdown fields pass existing control sanitization plus escaping/removal of
Markdown links, autolinks, raw HTML, table delimiters, and bidi controls.

### `recommendations.md`

The root report distinguishes reviewer conclusions, static hypotheses, proposed
verification, and observed evidence. For every improvement it includes:

- exact collected file/line/content identity and why it matters;
- a specific change and applicable packaged canonical recipe;
- assertions, coverage behavior, and test inventory that must be preserved;
- a regression that must fail before repair;
- deterministic root-based `ptest` argv, cwd, prerequisites, and expected
  outcomes, never model-provided shell;
- blank observed-evidence fields requiring actual command, cwd, exit status,
  and output record; unresolved items say `unverified`;
- the final integrated `ptest --full` gate.

Isolation advice requires an executable sentinel design across both workers and
concurrent runs for databases, Redis/Valkey, and writable files. A naming
convention alone is not proof: attempt cross-owner reads, overwrites, and deletes
and assert the ownership boundary as well as preservation of the neighbor sentinel.
If evidence or a runnable target is unavailable, say so rather than inventing a
file citation or command; give a bounded evidence-gathering next step and keep
that item unverified. Where current ptest cannot execute a requested
parallel permutation, the report says `unsupported` and specifies a
test-harness regression that is itself run through supported ptest; it never
invents a ptest flag.

The fixed footer is:

> Ask your LLM to read this file, verify every cited claim against the current
> source, implement only recommendations you approve, record the actual ptest
> command/cwd/exit/output for each verification, and finish with `ptest --full`.

Publication uses one simple ownership strategy. A ptest-managed report begins
with a versioned HTML comment containing the SHA-256 of the remaining exact
bytes. A missing target is created with no-follow descriptor-relative writes.
An existing report is replaceable only when the marker is recognized and its
hash, inode identity, and bytes still match the verified read. A custom, edited,
symlinked, non-regular, unreadable, or concurrently changed target returns
`report-conflict` with an actionable message and is never clobbered. Publication
is temp-write, `fchmod`, `fsync`, identity/content recheck, atomic replace, and
parent sync. Cancellation, provider failure, validation failure, or source
identity drift preserves the prior complete report. Changes between collection
and publication produce `stale-evidence`; the new result stays incomplete.

Serialize concurrent ptest publishers using a root-identity-keyed lock in the
existing account-local state; re-read report identity and content after acquiring
it. This protects cooperating ptest writers and detects intervening edits, not
hostile same-user mutation in the final check/rename window: ordinary POSIX rename
is not a content compare-and-swap. The qualification record must state this
residual filesystem limit rather than claim an absolute concurrent-editor guarantee.

## Init presentation

Human init output begins with this real wordmark (ANSI color only on a capable
TTY; identical readable glyphs under `NO_COLOR` or non-TTY):

```text
██████╗ ████████╗███████╗███████╗████████╗
██╔══██╗╚══██╔══╝██╔════╝██╔════╝╚══██╔══╝
██████╔╝   ██║   █████╗  ███████╗   ██║
██╔═══╝    ██║   ██╔══╝  ╚════██║   ██║
██║        ██║   ███████╗███████║   ██║
╚═╝        ╚═╝   ╚══════╝╚══════╝   ╚═╝
```

Below it appear `ptest <version>`, the bounded terminal-escaped repository
name, and `https://github.com/Avocado-Blockchain-Services/ptest`. The existing
Configuration/Guidance/Warnings/Next steps sections remain. Every typed action
record is rendered as an exact root-relative path under one of the literal
labels `created`, `updated`, `unchanged`, or `conflicted` (`already present` is
the existing internal state normalized to `unchanged`). No banner or ANSI bytes
enter JSON. Names are capped at 80 display columns before box layout and cannot
inject controls.

## Numbered acceptance criteria and negative twins

1. Default TTY doctor performs one disclosed, consented review per configured
   child and writes a validated report. **Negative:** CI, non-TTY, CLI presence,
   guidance selection, or repeated init never silently starts or uploads.
2. Init offers the identical in-process review after created or existing config.
   **Negative:** it never nests ptest, changes init JSON, or rolls back successful
   config because review was declined or failed.
3. All three qualified providers share one adapter contract. **Negative:** flags,
   `PATH` presence, prompt promises, read-only labels, or only two passing
   providers cannot satisfy the release gate.
4. Packets are finite, sanitized, identity-bound, and exclusion-aware, with
   partial evidence coverage separate from interrupted review.
   **Negative:** symlinks, recognized private/secret-bearing files, generated/dependency data,
   instructions, arbitrary configuration, or unrestricted workspace access are
   never admitted.
5. Assessment JSON has every canonical ID exactly once per child and only cited
   packet evidence. **Negative:** the model cannot add IDs, commands, scores,
   execution proof, or upgrade regex hypotheses.
6. Scores follow satisfied/applicable and justified N/A rules, with critical
   blockers dominant. **Negative:** unknown is not a pass, zero applicable has no
   score, and the percentage is not coverage/performance/safety/runtime proof.
7. Capability, dependencies, review, and execution evidence stay distinct.
   **Negative:** xdist presence does not imply pytest parallel support, and an
   offline review does not imply tests ran.
8. Recommendations are concrete and self-verifying with sentinel isolation
   proof and deterministic ptest-only commands. **Negative:** advice does not
   authorize repair, hide unsupported orchestration, weaken assertions/coverage/
   inventory, or claim proposed verification was observed.
9. Provider work is sequential, synchronous, visible, bounded, cancellable, and
   contained from launch in one task-owned cgroup under the linked amendment.
   **Negative:** no process-group fallback, cross-run kill, fake percentage,
   provider launch without qualified containment, or claim that a plain cgroup
   handles parent death is permitted.
10. Report writes are atomic, ownership-checked, serialize concurrent ptest
    writers, and preserve the last complete result on detected conflicts.
    **Negative:** detected custom/edited reports, stale source, and failures are
    not overwritten or labelled success; disclose the same-user rename limitation.
11. Legacy `doctor --json`, `--prompt`, and `--probe` retain their contracts and
    incompatible modes fail before launch. **Negative:** the new schema never
    appears under legacy `--json`, and probe authority never leaks into review.
12. Init shows the wordmark, version, repository, URL, action paths, plain mode,
    and `NO_COLOR` behavior. **Negative:** untrusted names cannot alter terminal
    structure and JSON has no banner.

## Ordered implementation ownership and verification

The secure-by-spec axes are covered explicitly: identity uses existing provider
auth and surfaces missing/expired sessions without revealing credentials;
authorization requires invocation-local upload consent and no agent tools;
tenancy means separate child packets, checkout identities, and qualified
task-owned cgroup containment as amended; input requires bounded schemas,
safe paths, and untrusted-content handling;
state covers stale source, repeat init, cancellation, and publication conflicts;
exposure excludes recognized sensitive material and discloses residual source
sharing; availability caps fanout, bytes, duration, and progress; dependencies
allow only qualified installed CLI adapters and static project-environment metadata.
No service migration is required.

Implementation starts only after approval and the capability qualification
barrier. Each sequential task owns only the listed files; later tasks adapt to
earlier committed interfaces and do not rewrite unrelated work.

| Order | Task and exact owned files | Required ptest evidence |
|---|---|---|
| 1 | Public contract/qualification barrier: `src/ptest/contracts.py`, `src/ptest/agent_assessment.py`, `src/ptest/agent_providers.py`, `scripts/export-schemas.py`, `docs/schemas/v1/agent-assessment.json`, `tests/ng/test_contracts.py`, `tests/ng/test_agent_assessment_contract.py`, `tests/ng/test_agent_providers.py`, `docs/research/2026-09-22-agent-provider-qualification.md` | RED then GREEN: `ptest tests/ng/test_contracts.py tests/ng/test_agent_assessment_contract.py`; `ptest tests/ng/test_agent_providers.py`. Extend the existing public-kind/validator/projection/schema registries additively, preserving all legacy documents. Freeze exact qualified argv/env or stop as blocked. |
| 2 | Evidence admission, strict validation, scoring: `src/ptest/agent_assessment.py`, `tests/ng/test_agent_assessment.py`, `tests/ng/fixtures/agent_assessment/**` | `ptest tests/ng/test_agent_assessment.py`; include secret/symlink/instruction/bounds/duplicate-ID/stale-identity negatives. |
| 3 | Process lifecycle and progress: `src/ptest/agent_providers.py`, `tests/ng/test_agent_providers.py` | `ptest tests/ng/test_agent_providers.py`; prove the linked containment amendment's launch, escape, normal-exit, timeout/cancel/exhaustion, parent-death, and cross-run negatives; keep non-TTY progress stable. Do not enable review until its host prerequisites and all-three provider gate qualify. |
| 4 | Report rendering/publication: `src/ptest/recommendations.py`, `tests/ng/test_recommendations.py` | `ptest tests/ng/test_recommendations.py`; prove custom edits, symlink/race, interruption, stale source, prior-report preservation, injection, footer, and sentinel guidance. |
| 5 | Shared doctor/post-init orchestration and CLI/help integration: `src/ptest/cli.py`, `src/ptest/help.py`, `src/ptest/render.py`, `README.md`, `tests/ng/test_cli.py`, `tests/ng/test_help.py`, `tests/ng/test_doctor.py`, `tests/ng/test_doctor_smoke.py` | `ptest tests/ng/test_cli.py tests/ng/test_help.py tests/ng/test_doctor.py tests/ng/test_doctor_smoke.py`; preserve literal argv, legacy schemas, exit status, dry-run no-effects, and probe separation. README distinguishes optional consented review from model-independent normal runs. |
| 6 | Branded renderer and init-flow regression coverage (CLI wiring is task 5): `src/ptest/init_render.py`, `tests/ng/test_init.py`, `tests/ng/test_init_render.py` | `ptest tests/ng/test_init.py tests/ng/test_init_render.py`; created/existing/decline/non-TTY/JSON/repeated-init/NO_COLOR/hostile-name cases. |
| 7 | Copied-repository acceptance only: `tests/ng/test_agent_doctor_acceptance.py`, `scripts/acceptance.py` if its registered scenario list must change | `ptest tests/ng/test_agent_doctor_acceptance.py`; then the one integrated `ptest --full` after all source changes. |

For subsequent work, Claude Opus authors specifications; Luna handles coding and
audits under the user's current model-routing instruction. Controller checks
owned files and mechanical gates before scoped task audits.

Every focused task records the initial failing regression before repair and the
GREEN command, cwd, exit status, and raw output. After source changes run
`graphify update .` AST-only, followed by the repository's scoped audit,
secrets/SAST/dependency gates when applicable, and exactly one final integrated
`ptest --full`.

Acceptance smoke installs the built artifact into a task-local prefix/environment
and operates only on byte-preserving copies under a task-owned temporary root of
at least a standalone pytest repository, standalone Vitest repository, and v2
monorepo (relevant child snapshots are acceptable). Existing source candidates are
`/home/ingmar/code/scammeter/persea_scam_meter`,
`/home/ingmar/code/scammeter/persea_scam_meter_ui`, and
`/home/ingmar/code/persea_content_maker_unified`; verify their runners before
copying and never clean or initialize originals. It excludes `.git`, secrets,
envs, dependencies, and generated content and verifies originals remain
byte-identical. Mocked deterministic tests cover every negative path. Real
Claude, Codex, and OpenCode smoke covers init offer, repeated init, doctor,
capability-first table, report self-verification, assessment JSON, no-provider,
and cancellation without running real tests/services. An unavailable account is
recorded `unverified`, never complete; no provider failure is hidden by the
readiness UI.
