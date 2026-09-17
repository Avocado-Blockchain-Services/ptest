# ptest NG product specification

Created: 2026-09-17 UTC

Author: architect-agent (Astra)

Status: proposed product contract; pending independent specification review and user approval

## 1. Product and decision boundary

ptest NG is a local test command for developers and coding agents sharing a workstation. Install it once, initialize each repository, then use `ptest` for an explained automatic run, an explicit scope for a focused run, or `ptest --full` for the repository's full gate. Independent invocations cooperate on local capacity; uncertainty in automatic selection widens execution instead of creating a misleading green result.

Doctor helps a developer or their existing coding agent prepare suites for efficient, isolated execution. It supplies evidence and practical repair instructions. It does not repair code, launch an agent, certify arbitrary tests safe, or require a model API.

This document specifies observable behavior, boundaries, acceptance criteria, and delivery gates. It does **not** approve a package layout, scheduler transport, database format, lock algorithm, parser implementation, dependency set, or detailed implementation plan. Those belong to the next, separately reviewed design. The existing foundation is candidate reuse only.

### Question space

For X = local-first ptest NG, these are the product questions Q and proposed answers:

| Question | Answer |
|---|---|
| Who benefits first? | Several developers/agents running independent local commands, including different repositories and Git worktrees, under one operating-system user. |
| What happens after install and init? | A portable repository config resolves scoped/full commands. Bare `ptest` selects conservatively when evidence permits and otherwise runs full. New repositories begin with one worker per run. |
| What is shared? | Admission capacity for cooperating invocations on the same host/user; isolated state for each checkout and execution. No hosted coordinator. |
| What is trusted? | Running tests executes the explicitly adopted repository and its dependencies. Inspection does not execute that code. Local metadata is evidence, not permission to execute commands supplied by a report. |
| How is speed evaluated? | Separate planning, queue, setup, execution, and recording measurements; unchanged assertions and full coverage gates; replayable comparisons. No universal speedup promise. |
| What is the first support boundary? | Linux and macOS; first-class pytest/Vitest; bounded legacy Go/Cargo execution; generic CLI use from coding TUIs. Exact tested versions are frozen in the design/release matrix. |
| What is deferred? | Remote runs, service deployment, native Windows support, cross-repository impact graphs, automatic code repair, agent launchers, and a broad adapter marketplace. |
| What may happen next? | Independent specification review, correction, and user approval. Detailed design and implementation cannot begin on the strength of this draft alone. |

### Traceability to user intent

Requirement groups below cite these intent anchors from `.pipeline/context.md`:

| Anchor | Requested outcome |
|---|---|
| I1 | Install once, initialize per repository; local-first and open-source-ready. |
| I2 | Coordinate simultaneous independent invocations, repositories, and worktrees. |
| I3 | Conservative, explainable partial selection; preserve failures and full fallback. |
| I4 | Diagnose readiness/parallelization blockers and produce useful local repair prompts. |
| I5 | Generic resource ownership, reusable setup, clean test state, factories, and timing. |
| I6 | Broad reasonable TUI compatibility without a model/API/provider dependency. |
| I7 | Preserve literal arguments, runner outcomes, coverage, user files, and bounded resources. |
| I8 | Finite reviewed milestones, realistic support claims, independent adoption evidence, and explicit release approval. |

The requirements are either direct expressions of these anchors or minimum correctness needed to honor them. In particular, state identities, invalidation, cancellation, and versioned outputs support I2/I3/I7; they are not a new distributed platform.

## 2. Scope and support tiers

### Included in the complete v1 product

1. Portable initialization/configuration, installation, and explicit legacy migration.
2. Local admission shared across independent invocations, with worker caps, fairness, status, and recovery.
3. Conservative automatic selection for supported pytest/Vitest configurations, explanations, outcome history, full fallback, and explicit comparison with a full run.
4. Static doctor findings, timing/resource guidance, printable repair prompts, and bounded explicitly requested probes.
5. Versioned machine-readable interfaces and a generic coding-agent guide.
6. Documentation, regression evidence, benchmarks, and release gates described here.

Intermediate milestones must identify absent capabilities. Shipping the foundation alone must not be described as the completed smart scheduler/selector.

### Support promises

| Surface | v1 promise | Limits |
|---|---|---|
| Linux and macOS | Supported once each passes the published subprocess, scheduler, cancellation, installation, and runner matrix. | No statement of support based only on source portability. |
| Native Windows | Explicit unsupported-platform response before execution or mutation. | Later milestone; no partially working scheduler advertised as supported. |
| WSL/container environments | Capability-limited local environments; support only a tested configuration listed in release notes. | A container normally has its own coordination domain. It is not evidence of native Windows or host-wide container coordination. |
| pytest and Vitest | Execution, tested worker bounding, machine results, timing, and conservative selection for the documented version/configuration matrix. | Unsupported plugins, launchers, versions, or discovery behavior lose the affected capability explicitly; they do not silently inherit it. |
| Go and Cargo | Preserve explicit scoped/full execution through small, tested adapters and documented concurrency controls. | No impact selection or language-specific static doctor certification in v1; bare `ptest` uses full fallback. |
| Other npm scripts / arbitrary commands | Explicit executable-wrapper configuration remains available as a compatibility escape hatch. | No inferred runner, no asserted inner worker bound, no automatic selection. Admission reserves the entire configured ptest capacity exclusively, and the user must supply a bounded wrapper. This is best-effort execution, not first-class scheduler enforcement. |
| Coding TUIs | Ordinary command execution, exit status, text, JSON, and repair prompts are the required interoperability surface. | Native rule loading and model behavior have separate evidence levels; neither is necessary to use ptest. |

A supported runner must not require an optional parallelization plugin merely to execute serially if a documented/tested serial path is available. Optional instrumentation or parallelization requirements must be reported separately from basic execution readiness. The design must verify actual runner capabilities and resolve launchers/settings precedence; this specification does not assert that a particular runner flag solves every version.

### Explicit exclusions

No cloud execution, cloud credentials, model calls, telemetry upload, remote cache, source upload, web dashboard, IDE extension fleet, MCP server, native agent launcher, test-result reuse as a substitute for execution, flake quarantine, automatic assertion/coverage reduction, universal database provisioning framework, mandatory container runtime, or cross-machine/multi-user administrator service. ptest does not sandbox intentionally hostile repository code or impose an operating-system CPU/RAM limit on arbitrary descendants.

The local scheduler does not deduplicate independent runs. Each command obtains its own result; scheduler correctness must not depend on replaying a cached pass. Cross-repository *capacity* coordination is included; cross-repository *test dependency* inference is deferred.

## 3. Adoption, configuration, and migration [I1, I7, I8]

**P1 — Install once.** A documented isolated installation provides `ptest` without modifying the system Python, requiring a cloud account, or installing application dependencies globally. A candidate version can be installed to a temporary destination and removed without replacing the live CLI. Upgrade/uninstall preserve repository configuration and user history unless removal is explicitly requested. An interrupted upgrade must leave either the prior complete installation or the new complete installation usable.

**P2 — Initialize safely.** `ptest init` creates a small, versioned, repository-local TOML configuration, proposed as `.ptest.toml`. It detects only configurations it can explain, shows scoped/full commands and setup/network implications, and refuses ambiguity rather than guessing a runner from a language manifest alone. A dry run prints the proposed file and decision without writing. Repeated initialization does not overwrite config, instruction files, lockfiles, tests, or coverage settings. Existing compatible configuration yields an unchanged result; conflicting configuration yields a clear nonzero response.

The committed file uses repository-relative paths and argv arrays, not machine-specific absolute checkout paths or shell command strings. It owns runner choice, scoped/full commands, selection policy, and repository-specific limits. Machine-local settings own the shared host budget, private state location, and optional local overrides. `ptest where` explains the resolved root, value provenance, capabilities, commands, and effective limits. Repository or invocation settings may lower limits; they cannot raise the machine ceiling. Changing a budget while runs are active prevents new grants above the new budget without killing existing work. The initialization message explains that committing reviewed config/policy is necessary before a clean reusable baseline can be recorded.

Git worktrees inherit the repository's committed policy while resolving the current checkout's files and environment. Nested configurations must resolve to an explicit nearest project boundary; ambiguity must be reported. Non-Git directories can run explicit/full commands but have no Git-based automatic selection. Moving/cloning a repository must not require editing absolute paths in the committed config.

**P3 — Setup is an execution boundary.** `init`, static `doctor`, `where`, and static `plan` do not install dependencies, import project configuration, execute hooks, start services, or call the network. During a requested run, a declared setup step may use the repository's package manager; its possible network/lifecycle-script execution is visible before use. Locked setup must not rewrite the lockfile. There is a no-setup mode that reports missing dependencies instead of installing them. Missing or ambiguous lock/package-manager information requires an explicit setup recipe or prior manual setup. Setup failure prevents tests from starting and remains distinguishable from a test failure.

Python environments and Node dependencies belong to their checkout. ptest never copies or shares `.venv`/`node_modules` across worktrees. Two commands in the same checkout must not race dependency installation or mutate runner artifacts concurrently. Declared setup consumes admission capacity too; it is not an unaccounted parallel workload.

**P4 — Migrate explicitly.** Preserve the existing repository/history and recovery tag. A migration preview maps existing local registrations, scoped/full commands, worker settings, and coverage gates to portable config, including worktree resolution. Simple legacy strings may be translated to literal argv only when lossless; compound shell behavior requires an explicit executable wrapper. Do not evaluate legacy strings to discover their meaning.

Legacy remote registrations and remote-only commands return an actionable migration/unsupported-mode response. They must not silently launch a newly local full suite. Explicit `init` adoption records local execution after showing that change. Existing Go/Cargo and generic command users receive the support limitations in section 2; unsupported cases are named, not discarded. Legacy `register` provides the migration/init preview, and `--local` remains an accepted local-mode alias. `--fresh` is accepted for a forced real execution; v1 never reuses passing executions in the first place. Detailed option parsing and deprecation messages are frozen in the design.

Removing obsolete cloud source/tests from the active implementation must be a reviewed scope change with verified callers and the recovery reference recorded. It must not delete cloud resources, alter credentials, run migrations, or deploy anything.

## 4. Shared local scheduling [I2, I5, I7]

**S1 — One admission domain.** Every normal run, explicit scope, full fallback, setup step, runner discovery that executes project code, and dynamic doctor probe cooperates with other invocations under the same host/OS-user and resolved scheduler configuration. Coordination spans repositories, Git worktrees, shells, and TUIs. A repo config cannot create a private domain to evade the shared limit. Separate OS users or isolated container namespaces are outside the v1 guarantee and must be described as such.

**S2 — Explicit resource accounting.** At minimum, enforce a shared ceiling for granted runner worker slots and active jobs. Initial repository worker count is one. The machine default reserves workstation headroom and is printed with its derivation; the exact default formula and supported CPU/quota detection are frozen in the design. A worker slot is an admission unit, not proof that the kernel will observe exactly one CPU thread.

For supported adapters, the effective runner command/settings cannot request more than the grant through command-line flags, inherited runner options, config defaults, or an unrecognized automatic-worker setting. Unsupported combinations fail or take a documented serial/exclusive path before launch. Native library thread pools are bounded where the support matrix claims control. Unknown nested executors are reported; ptest does not pretend it controls them.

Memory guidance includes a configured per-run estimate/budget and admission accounting when supplied. Missing estimates are shown as unknown; the default is conservative. Available-memory observations may reduce admission, never increase the configured ceiling. v1 does not claim hard RAM enforcement, immunity from swapping, or measurement of arbitrary external load. The design must specify the unknown-estimate policy and benchmark it on simultaneous Python/Node fixtures.

**S3 — Fair, observable waiting.** New work queues when capacity is unavailable. A continuously waiting eligible request cannot be overtaken indefinitely by newer small requests. The minimum acceptable v1 policy is FIFO admission with bounded grants; sophisticated priority prediction is not required. An impossible request is rejected or reduced within its declared allowed range before entering the queue. A configured queue deadline returns a scheduling error, never a test pass. `ptest status` exposes queued/running/cancelling/uncertain states, requested/granted capacity, age, and reason for waiting without publishing environment values or raw secret-bearing commands.

Two invocations in the same checkout are serialized for their entire setup/execution/artifact lifetime by default. Different checkouts may run concurrently within the shared budget. A repository that explicitly names a shared exclusive resource is serialized against other runs declaring the same resource identity. Distinct worktrees do not by themselves prove that an external database/cache/service is isolated.

**S4 — Cancel and recover without harming neighbors.** Cancelling a queued command starts no test process and releases its queue entry. Cancelling active work signals only its owned process group, waits a bounded grace period, escalates if needed, and retains capacity until owned descendants are confirmed stopped. Normal cancellation should complete within 10 seconds in the controlled subprocess fixture, including the documented grace period. Runner termination and shell signal exit conventions remain visible.

A crashed caller must not permanently retain capacity. After proving the owner and its managed work are gone, stale capacity becomes reusable within 30 seconds in the controlled recovery fixture. PID reuse, an old timestamp, or a missing parent alone is not sufficient ownership proof. Live or ambiguous descendants keep their capacity charged and receive an actionable status, rather than allowing duplicate work or killing an unrelated process. Scheduler corruption/unavailability stops execution with a clear operational error; it must not fall back to unlimited independent launches. Mixed incompatible ptest versions in the same domain refuse unsafe coordination.

Named database/cache cleanup is **not** a scheduler responsibility. Process/lease recovery may remove only ptest-owned state whose identity is validated; it must never drop application databases, flush caches, or delete arbitrary checkout paths.

## 5. Automatic selection and evidence [I3, I7]

**T1 — Modes stay honest.** Bare `ptest` and `ptest changed` request the same automatic policy. Explicit paths/filters run the user's scope without silently expanding it; they report `scoped` and disclose outstanding failures outside that scope. `ptest --full` executes the entire configured gate, including its existing assertions and coverage thresholds. An option that narrows collection cannot be combined with a claim of a full gate; reject that combination or identify it as scoped before execution. No selected/scoped run satisfies a global coverage or release gate.

Every automatic execution emits a plan before running its tests. It includes the resolved checkout/tree identity, mode, base/baseline if relevant, runner/config/environment compatibility, changed paths, selected scope, mandatory additions, and widening reasons. Every selected test/file has at least one stable reason code with supporting evidence. Human output and JSON describe the same decision. Estimates and prior timing can order work or promote it to full, but cannot remove a required test. A repository switch disables narrowing immediately without deleting evidence; automatic commands then run full. Narrowing starts only when validated policy and compatible complete evidence meet the documented adapter requirements, never merely because initialization created a file.

**T2 — Conservative inputs.** Selection combines current changes, supported runner evidence, declared safety policy, changed/new tests, and unresolved prior failures. Pytest/Vitest adapters may use supported runner-native dependency or execution evidence; historical coverage by itself is not proof that a changed program cannot introduce a new dependency. Dynamic imports, generated resources, runtime discovery, shared fixtures, and opaque plugin behavior need declared broader groups or a full fallback. Do not build an LLM selector or a second universal language/module resolver.

Changed/new test files run all currently collected cases, including parametrization. Deleted/renamed source uses both the old and new path where needed. Test helpers and setup/teardown dependencies count. Code/configuration executed globally or outside an attributable test context is broad-impact evidence. A runtime-source change with no explainable affected tests widens to a declared conservative group or full. New/unclassified roots cannot be silently excluded.

A repository policy may add always-run tests, broad source-to-test groups, full triggers, and explicitly justified paths that need no tests. Ignore/no-test rules cannot overlap test/runtime/global-trigger paths or override failures. Policies are validated and their own changes trigger full. Runner/dependency/coverage/plugin/global-fixture/build configuration changes trigger full unless a narrower behavior has a separately tested explicit contract. Defaults err on broad execution.

**T3 — Baselines and dirty trees.** First use, missing evidence, or incompatible evidence produces full. Eligible baseline evidence comes only from a complete, successful full run on an unchanged clean source tree, with compatible runner, instrumentation, policy, dependency, relevant environment, platform, and configuration identities. Failed, partial, interrupted, dirty, mixed-source, or malformed runs cannot replace it. Baseline capture is local and visible; an instrumentation failure leaves the earlier usable baseline intact and cannot manufacture success.

The change set covers committed changes relative to compatible evidence, staged/unstaged edits, untracked candidate source/tests, deletes, renames, and relevant mode/symlink changes. The effective range must cover all changes since the evidence used, even when the requested base is newer. A configured/explicit base resolves locally to an immutable commit; no guessing `main`/`dev`, fetching, or switching branches. An available compatible ancestor baseline can supply the local default; otherwise use full. Missing/shallow history, conflicting ancestry, unresolved conflicts, unsupported submodules, or unreadable inputs widen to full.

Ignored content is not automatically irrelevant: generated/runtime inputs and local configuration are fingerprinted or declared as external influence. Relevant environment values are never stored verbatim. If an influence cannot be bounded by the declared policy/fingerprint, automatic narrowing is unavailable. A dirty tree may be tested and may yield a selected plan when all its changes are accounted for; it never becomes a reusable clean baseline.

Revalidate the source/config identity after queue waiting and before execution. If it changed, rebuild the plan or run full. If relevant files change during execution, label the result as a changed-during-run observation, retain its actual failures, and publish no baseline/full-gate evidence for the final tree. ptest does not claim to prevent a user from editing a checkout during tests.

**T4 — Failures survive.** Failed and errored tests remain mandatory in automatic mode until a subsequent conclusive pass for the applicable checkout/configuration resolves them. A skip, xfail, interrupted run, missing reporter, empty collection, unrelated pass, or expired timing record does not clear them. A complete successful full inventory can establish that a deliberately deleted test is removed, recorded as removed rather than passed. Missing/renamed IDs without conclusive reconciliation force full.

Failures from concurrent runs cannot be erased by a late-arriving older pass. If outcomes overlap without a defensible ordering/source relationship, retain the failure or require full reconciliation. Branch/config/environment changes with unresolved incompatible history force full; they do not silently discard the obligation. Worktrees have separate mutable ledgers so one agent's passing branch cannot clear another's failing tree. They may share validated immutable evidence only if the exact compatibility rules hold.

No automatic retry converts an initial failure to green. An explicit reproduction or selected-versus-full comparison reports every attempt, including failures. Time-based history pruning preserves compact unresolved-failure obligations; loss/corruption of required history is uncertainty and forces full.

### Required fallback behavior

| Condition | Automatic execution | Negative contract |
|---|---|---|
| No compatible baseline, unsupported selection capability, unknown source influence | Full configured gate, with reason | Never an empty selected success. |
| Dependency/runner/global-fixture/selection-policy change | Full gate | No reuse based only on unchanged source filenames. |
| Unreadable/corrupt/oversized/incompatible selection metadata | Full gate if execution configuration is valid | Do not execute paths/commands taken from unvalidated metadata. |
| Invalid selection policy with separately valid runner/full configuration | Full gate and policy diagnostic | Do not use partially parsed narrowing rules. |
| Invalid runner/full command, conflicting config, unavailable scheduler | Operational failure before tests | Do not guess an executable or bypass admission. |
| Runtime changes select zero tests | Broader declared group or full | Do not say tests passed. |
| No relevant changes, or only validated no-test paths; no failures outstanding | `no-tests-needed`, exit 0, explicit zero-execution statement | Not `passed`, not a full gate, not a new baseline. |
| Selection is effectively full or exceeds a declared usefulness threshold | Actual full command with coverage | Do not run almost everything while accidentally omitting the gate. |
| Selected collection/report cannot validate planned work | Full widening if no tests have run; otherwise incomplete/error plus an actionable full rerun | Never silently treat missing tests as passed. |
| Confirmed selected/full miss | Disable narrowing for that checkout/policy, retain evidence, require correction and comparison before re-enabling | Do not hide the failure as a flaky warning. |

**T5 — Explain and compare.** `ptest plan` is a nonexecuting preview from configuration and existing local evidence. It does not import runners/config or collect tests. If accurate narrowing requires project execution, it says so and previews full/unknown rather than secretly running discovery. Actual automatic runs may perform declared supported discovery after admission, and must display any resulting plan change.

An explicit shadow option runs the selected work and full gate on the same unchanged tree/environment, reporting each result and the full outcome. A failure from either phase remains a nonzero outcome. Any full failure omitted from selection is a suspected miss; focused reproduction distinguishes a reproducible selector miss from nondeterminism. Unclassified divergences block selection promotion. Comparison is diagnostic evidence, not proof that future selections cannot miss behavior. Full remains mandatory at the project's existing integration/release boundary.

## 6. Doctor and repair prompts [I4, I5, I6, I7]

**D1 — Static means no execution.** Plain `ptest doctor` inspects bounded regular files under the resolved project root, validated ptest configuration, and already-recorded results. It does not import test/application code, invoke a package manager, load executable runner config, probe a network/service, start tests, or edit the repository. It skips secrets/dotenv files, generated/dependency directories, symlinks, external paths, special files, and unsupported content. A race that swaps a path to a symlink must not expose data outside the root.

The report distinguishes execution readiness, parallel-isolation concerns, selection readiness, and known timing. Each finding includes a stable code, severity, confidence, file/line where available, evidence type, consequence, practical remediation, and verification guidance. Readiness is `ready-for-declared-capability`, `blocked`, or `unknown`; absence of a detected pattern is never global certification. Findings are hypotheses unless supported by observed execution. Skipped/unreadable/truncated/unsupported input and missing timing data appear as limitations.

Initial static inspection budgets are at most 20,000 directory entries, 2,000 files, 256 KiB per file, and 16 MiB total input. Limits and early termination are visible. Deliberately crafted syntax depth, binary content, huge directories, or findings volume must remain bounded; the design freezes parser/output/time limits and proves them with fixtures. Reports/prompt generation do not include raw source snippets or environment values by default.

**D2 — Useful, bounded findings.** The initial catalog covers known patterns for per-test expensive initialization, destructive shared cache operations, fixed/shared resource names and ports, blocking wall-clock waits, external-network use, and insufficient cleanup/ownership declarations. Some concerns cannot be inferred reliably from source: these appear as explicit checks/questions, not fabricated detector results. Timing findings use actual runner reports, not static guesses. Factories/builders and resource recipes are guidance, not evidence that ptest rewrote or optimized a suite.

**D3 — Practical repair prompt.** `ptest doctor --prompt` emits a self-contained, bounded local prompt suitable for the user's current coding agent. It includes the exact inspection scope/limitations, structured findings, relevant recipes, and a verification request. It directs the agent to verify a suspected problem against callers and behavior, make the smallest maintainable correction, preserve assertions/test inventory/coverage, and use scoped `ptest` checks followed by the project's full gate. It forbids cleanup outside owned resources, blanket cache flushes, failure suppression, arbitrary sleeps as synchronization fixes, and optimizations that weaken coverage.

Repository filenames, comments, report messages, and captured data are delimited untrusted evidence; they cannot replace the repair instructions or grant permissions. The prompt never automatically discovers/invokes a model, forwards source, trusts a repo, edits agent config, or calls an API. The user decides whether to give it to an agent, and that agent remains responsible for its own permissions.

**D4 — Probes are explicit execution.** A separately requested doctor probe requires a concrete test scope and displayed execution plan. It lists commands, repetitions, concurrency, timeout, resource declarations, and potential setup/service/network side effects before starting; the execution flag/request is the user's authorization. A missing scope is an error, not permission to run the suite. Noninteractive use requires the same explicit options and never waits for a hidden confirmation prompt.

The v1 probe is a bounded serial/parallel/repeated-run comparison using existing supported runner commands and their result artifacts. It acquires scheduler capacity and uses per-attempt run/worker identities. It does not provision arbitrary database/cache servers, inspect production service contents, inject migrations, fuzz external services, or certify universal isolation. Without a declared isolated test target/resource policy it reports blocked/unknown and supplies repair guidance. A probe can show an observed conflict; successful finite samples mean only that those attempts showed no conflict. Results retain all attempts and incomplete/time-limited states. Probe failure cannot mutate selection policy into a more permissive state.

## 7. Resource and suite-quality contracts [I2, I4, I5, I7]

ptest supplies stable opaque project/checkout identity, unique run/attempt identity, and adapter-supported worker identity for test fixtures and diagnostics. They are namespacing inputs, not credentials or a claim that arbitrary tests already consume them. The design freezes the environment/API names and collision/length rules. A new run cannot inherit another active run's mutable resource ownership.

| Resource or practice | Required recipe/diagnostic guidance | Forbidden or unsupported shortcut |
|---|---|---|
| Databases, SQL or otherwise | Reuse expensive server/schema/template initialization once per run or worker as appropriate; keep independently concurrent workers isolated; reset each test's records/state. Explain transaction rollback versus truncation/namespace reset, including tests that commit and background connections. Identify target ownership before teardown. | Recreate/migrate the whole database per ordinary test without justification; share a mutable test DB across unrelated runs; drop an existing DB solely because its name looks like a test name. |
| SQLite/file-backed stores | Run/worker-specific path and intentional connection/transaction lifecycle; exercise concurrent access only in declared integration cases. | A fixed shared file, or assuming in-memory scope behaves identically across processes. |
| Redis, Valkey, and other caches | Namespace keys by checkout/run/worker; clean only owned keys or use an exclusively owned disposable instance; account for TTL, asynchronous jobs, and leftover clients. | `FLUSHALL`, shared `FLUSHDB`, or global `clear` as routine parallel cleanup. A DB number alone is not universal isolation. |
| Files, directories, coverage, snapshots | Unique mutable paths, close handles, merge supported reporting artifacts only after writers finish, and delete only validated owned paths. | Shared `.coverage`, temp filename collisions, writing into another checkout, or deleting a broad parent directory. |
| Ports, processes, services | OS-assigned ports when possible; communicate the allocated address; own process groups and wait for actual readiness; reap descendants. | Pick an unused port and assume it remains reserved; kill by process name; let another run terminate a service. |
| Time, randomness, order | Fake/inject clocks and deterministic seeds for ordinary tests; wait on observed conditions with deadlines; expose order-sensitive failures. | Sleep to hide a race, use unbounded retries, or rerun until green. |
| Network/external APIs | Mock the boundary for deterministic unit tests; declare explicit integration/live tests with separate prerequisites and owned resources. | Treat ptest's local runtime as proof that the suite cannot call external services. |
| Factories/builders | Small reusable deterministic builders with realistic defaults; explicit persistence/side effects; retain behavior-relevant variation. | Global mutable fixture data, universal mega-fixtures, duplicated setup everywhere, or replacing assertions with construction-only tests. |

These are framework-neutral contracts with concise pytest/Vitest examples where useful. v1 does not ship a database driver/plugin for every backend or enforce a universal fixture framework. The supported outcome is a useful diagnosis, implementable repair guidance, and a way to verify the repair locally.

Timing reports preserve test identity, outcome, attempt, wall time, and supported setup/call/teardown separation. Unknown attribution remains unknown. Default guidance: under 0.5 seconds is healthy; repeated 0.5–2 second ordinary tests merit inspection; 2–3 seconds merits optimization; over 3 seconds requires investigation or an explicit integration justification. These are configurable guidance thresholds, not automatic test failures or permission to weaken assertions. Initialization, queueing, collection, and teardown time must not vanish from end-to-end measurements. Unsupported result formats yield a limitation, not invented per-test durations.

## 8. CLI, JSON, and outcome semantics [I3, I6, I7]

The design will freeze exact grammar and schemas around this minimum surface:

| Command | Product contract |
|---|---|
| `ptest init [--dry-run]` | Safe config creation/preview and migration explanation. |
| `ptest` / `ptest changed` | Automatic local plan and execution, full fallback on uncertainty. |
| `ptest <paths / runner filters>` | Explicit scoped execution. A documented `--` boundary permits literal filenames/options that collide with ptest syntax. |
| `ptest --full` | Configured full gate; never narrowed implicitly. |
| `ptest plan [--json]` | Static nonexecuting selection preview and reasons. |
| `ptest where [--json]` | Resolution, provenance, capability, setup, and effective limits. |
| `ptest status [--json]` | This user's local queue/runs and coordination health. |
| `ptest doctor [--json / --prompt]` | Static readiness, findings, timing, and guidance. |
| `ptest doctor --probe <explicit scope>` | Bounded authorized execution as described in D4. |
| `ptest history [test-or-file] [--json]` | Local outcomes, unresolved failures, durations, and retention limitations. |
| `ptest --help` / `--version` | Public capability/version information without executing project code. |

An explicit shadow option belongs to automatic execution. Baseline capture/status and machine-result destination flags must be exposed without multiplying overlapping subcommands; the detailed design chooses their minimal grammar. No unused placeholder command is advertised as implemented.

**C1 — Literal execution and output.** Configured commands and user arguments are passed as argv, with tested handling of spaces, quotes, Unicode, leading dashes, metacharacters, and runner-valued options. ptest does not interpolate a shell. Only documented adapter-owned concurrency/instrumentation options may be added/normalized, and the effective command/capability is inspectable without leaking secret option values. A caller-supplied filter resembling a worker flag remains a filter value.

Normal runs stream the child stdout/stderr without removing failure detail, truncating them into a summary, or requiring a TTY. ptest diagnostics use their documented separate channel. Streaming output is not retained as selection metadata. Signal handling works in an ordinary shell and as a TUI child process.

**C2 — Versioned automation.** Pure inspection `--json` produces one valid versioned JSON document, including errors, on its documented output channel; human preambles/ANSI escapes must not corrupt it. Execution produces a separate versioned result artifact or explicitly selected event channel, leaving raw test output usable. The design freezes schema IDs, required fields, outcome/reason/error enumerations, additive-field policy, size bounds, and compatibility rules before coding. Unknown major schemas are rejected safely, not guessed.

Minimum result facts are command/mode, run identity, source/config identity or uncertainty, admission/phase timing, selected/full/no-work distinction, expected/observed counts where known, attempts, runner exit status, ptest operational errors, incompleteness, and full-gate eligibility. Reports distinguish `passed`, `failed`, `cancelled`, `incomplete`, `no-tests-needed`, and `not-run`; only actually observed passes are passed. A no-work result cannot supply full-gate evidence.

**C3 — Exit semantics.** Once a single runner fails, ptest preserves its nonzero exit code; telemetry/cleanup errors cannot hide it. A successful child may still produce a nonzero ptest operational status when ptest cannot validate required automatic-run/full-gate evidence, including a relevant source change during a full gate. Optional timing-recording failure alone does not change an otherwise valid explicit/full runner result, but is visible and cannot refresh a baseline. The result artifact always separates `runner_exit_code` from the final command status and error origin.

Proposed ptest-owned codes: `2` for usage/configuration/unsupported capability; `70` for an internal/incomplete required operation; `75` for temporary scheduling/coordinator unavailability or queue timeout; `127` for executable not found; `128 + signal` for termination by signal. Runner codes can overlap these numbers, so origin is explicit. Inspection exits `0` when it successfully reports findings/unknowns; that does not mean the inspected suite is ready. Failed inspection uses a ptest-owned nonzero status. Compound shadow/probe operations preserve every phase exit and return the first failing phase's status; a later pass cannot mask it.

## 9. Coding-TUI interoperability [I6, I8]

The universal contract is a local executable, noninteractive arguments, cwd/environment, stdout/stderr, exit status, and versioned artifacts. Required workflows need no PTY, editor, pager, clipboard, interactive confirmation, or stdin input; an unresolved required choice returns an actionable error. Ship one generic repository guide covering init, scoped/full/automatic use, doctor, repair prompts, cancellation, and verification. Native entry files are optional thin pointers to that guide, avoiding divergent copies of behavioral instructions.

Compatibility claims have three explicit levels: **generic CLI-compatible** (a documented shell/tool execution interface), **documented native convention** (official docs/local authoritative help identify a rule-loading mechanism), and **tested integration** (a recorded smoke against a named version/configuration). Unknown native integration is marked unverified. No list of recognizable product names counts as a tested matrix.

Initial documentation covers Codex, Claude Code, Gemini CLI, OpenCode, Aider, and Muse at the evidence levels established in the [interoperability research](../research/2026-09-17-agent-interoperability.md). Do not imply they read the same file: Codex documents `AGENTS.md` and an override convention; Claude Code documents `CLAUDE.md`; Gemini supports its memory-file/import mechanism; OpenCode documents its own rule precedence; Aider supports explicit read configuration. Muse rule loading depends on trust in the local verified tooling. One narrow Muse AGENTS-loading smoke was observed with explicit workspace trust; that is not a complete ptest integration test. Exact instructions and evidence links belong in the interoperability guide/release matrix and must be checked against the current official documentation or local CLI help.

`init` and doctor do not edit global TUI config, grant trust, launch agents, install MCP servers, select models, or make provider requests. Any optional repository instruction-file installation is explicit, non-overwriting, and previewable; printable snippets are sufficient for v1. Do not generate an override file that shadows user rules. A restricted/offline shell smoke proves the CLI contract without spending model tokens. Native-loader testing, when desired, is a separate explicit test with its own trust/provider requirements.

## 10. Security, privacy, and bounded state [I1–I8]

Actors are the local OS user, cooperating ptest processes, the explicitly executed repository/dependencies, and untrusted files/artifacts encountered during inspection. Protected resources are other runs/processes, checkout/configuration contents, private state, external test services, and the truth of reported results. ptest provides defensive boundaries for inspection, state handling, and ownership; it cannot isolate malicious test code already running with the user's privileges.

Every secure-by-spec axis has a positive contract and a required negative twin:

| Axis | Positive contract | Negative regression evidence |
|---|---|---|
| Identity | Validate host/user/checkout/run/worker and process ownership before claiming capacity or cleanup. No service credentials are required. | Missing/malformed/stale identities, PID reuse, and another user's state cannot authorize process termination, lease takeover, or resource cleanup. |
| Authorization | Only explicitly requested run/setup/probe actions execute repository code; init writes only its declared target; diagnostics remain nonexecuting. | Hostile config, a comment, an imported report, or doctor finding cannot execute commands, grant trust, overwrite a file, or start a probe. |
| Tenancy | Isolate mutable state/resources by checkout/run/worker; share only validated compatible immutable evidence. | Same repo basename, same branch name, different worktrees, overlapping runs, and another project's paths cannot collide, clear failures, read private results, or broaden cleanup. |
| Input | Validate versioned schemas, argv boundaries, paths, identifiers, bounds, encodings, and regular-file ownership. | Shell metacharacters remain literal; traversal/symlink races, hostile JSON/TOML, invalid types, giant/deep inputs, terminal controls, and malicious test IDs cannot escape roots, execute code, corrupt display, or allocate unbounded memory. |
| State | Admission, outcome publication, source identity, and cleanup are safe under retries, interruption, and concurrent callers. | Crash at every transition, repeated cancel/init, concurrent outcomes, stale plans, partial writes, and conflicting versions cannot overgrant capacity, erase failures, publish a false baseline, or reuse live ownership. |
| Exposure | ptest state is local/private by default; reports store minimal structured facts, with secret-bearing values excluded and unsafe terminal characters escaped. | Dotenv, credentials in env/URLs/argv, source snippets, and unrelated filesystem contents do not enter ptest diagnostics/history/prompts/JSON. Raw runner streams are forwarded and may contain what the tests print; ptest must not claim they are secret-filtered. |
| Availability | Bound scans, metadata, findings, queue depth, retries, probe attempts, and lifecycle waits; print limit hits. | A huge tree, stalled reporter, blocked probe, queue flood, corrupt artifact, or crashed caller cannot hang inspection indefinitely, grow ptest state without a cap, or start uncapped fallback work. |
| Dependencies | Use declared executable/package-manager boundaries and supported runner/report versions; verify installed artifacts and applicable security gates. | A missing runner, hostile report, legacy shell string, setup failure, unsupported plugin/version, or absent optional tool cannot trigger implicit shell execution, undeclared downloads, guessed capabilities, or test success. |

No axis is N/A: local ownership replaces hosted identity/tenancy concerns, but the corresponding boundaries remain relevant.

Retention and disk quotas are finite, configurable, and visible. v1 defaults must bound per-checkout history/state, report/artifact size, and total pending jobs; exact values and eviction behavior are frozen in design and included in acceptance fixtures. Eviction preserves an active run, its evidence, the usable current baseline, and compact unresolved failures. If those cannot fit, disable narrowing/report a capacity error rather than discard correctness-critical state. Garbage collection touches only validated ptest-owned entries; deleting an unavailable external checkout is never required.

## 11. Measurable acceptance and negative cases

These are implementation/release evidence requirements, not claims that they have already passed. Deterministic tests run through `ptest`; real-provider/live-service checks are separate explicit attempts. Specification-only changes run no tests and install no dependencies.

| ID | Acceptance evidence | Required negative case |
|---|---|---|
| A1 | Temporary-destination installation, upgrade interruption, fresh init, init dry run, repeated init, moved clone, and Git worktree smoke on supported platforms. Existing full commands retain coverage flags. | Existing config/guide files and live CLI remain byte-identical; ambiguous runner/compound legacy command/remote registration cannot silently execute. |
| A2 | Start six independent ptest subprocesses across two fixture repos and three worktrees with budget 4; recorded grants never exceed 4, and at most the configured active jobs run. Repeat with budget 1. | User flags/config/env requesting automatic or excessive workers cannot escape the grant on each supported adapter. |
| A3 | Queue a large eligible request before a stream of small ones; it starts after the finite set of already admitted predecessors finish. Queued cancellation launches nothing. Active cancellation reaps owned fixture descendants within 10 seconds. | No starvation, capacity release while owned work remains live, cross-run kill, or stale PID takeover. |
| A4 | Kill a caller at admission/start/run/result transitions. Once all owned fixture work is proven gone, capacity recovers within 30 seconds. Corruption and version conflict return structured errors. | A live/ambiguous orphan never causes speculative capacity reuse or unbounded launch fallback. |
| A5 | In isolated Git fixtures cover clean/dirty/staged/untracked/deleted/renamed changes, changed tests, setup/teardown/global fixtures, lock/config/policy changes, missing ancestors, and source changes while queued/running. | Every uncertainty row in section 5 broadens or errors as specified; zero tests cannot masquerade as a source-change pass or full gate. |
| A6 | Record a failing test, a skipped rerun, an unrelated pass, an older late pass, a branch change, and an explicitly removed test. Verify visible obligations and full reconciliation. | Failure obligations survive skips, incomplete reports, concurrent stale outcomes, pruning, and another worktree's pass. |
| A7 | Each first-class selector passes adversarial fixture changes, including an intentionally omitted dependency found by shadow comparison, and real supported runner subprocess checks. | A missing planned test/report, incompatible baseline, dynamic/opaque dependency, or confirmed miss cannot retain an apparently green narrowing path. |
| A8 | Static doctor fixtures exercise each claimed detector and all skip/limit categories. Filesystem/process/network sentinels confirm no source execution, dependency setup, service calls, or repo writes. | Secret/symlink/special-file/deep-input/prompt-injection fixtures do not escape inspection or gain authority. No-findings is never displayed as certified parallel safety. |
| A9 | Explicit probe fixtures show a serial pass plus an observed parallel ownership conflict, and an isolated corrected case; all attempts and timeouts remain visible. | No implicit full-suite probe, production target, hidden retry-to-green, or unscheduled test execution. Passing finite probes do not certify all future runs. |
| A10 | Two simultaneous fixture runs/workers use distinct database/cache/file/port identities; sentinel resources owned by the neighbor survive cleanup. Demonstrate reusable expensive setup and per-test clean state. | No shared destructive flush/drop, leaked record/key, cross-run teardown, fixed-port collision, or per-test migration disguised as an optimization. |
| A11 | Real subprocess fixtures preserve stdout/stderr and runner codes 0, 1, 2, a custom nonzero code, missing executable, and signals. JSON schemas parse independently and hostile argv remains literal. | No mixed JSON/preamble, rewritten filter values, hidden failure output, false `passed` status, or success after a failed compound phase. |
| A12 | Fresh adoption smoke in at least one independent non-Persea pytest repo and one independent non-Persea Vitest repo: install, init, full baseline, scoped run, automatic plan/run, doctor, cancellation. | No hard-coded Persea path/service/model configuration, cloud credential prerequisite, or mandatory native TUI integration. |
| A13 | Publish a supported OS/runner/launcher version matrix and generic noninteractive shell smoke. Any native TUI smoke names the exact version/trust state and observed behavior. | Unverified rule loading is never labeled tested; no tool configuration/trust/API change occurs as a side effect of init or doctor. |
| A14 | Fill state/queue/scan budgets with controlled fixtures and prune repeatedly. Active ownership, valid baseline, and unresolved failures remain intact or narrowing is disabled visibly. | Disk/queue growth is finite; corrupt/oversized state cannot block `--full` when its independent execution configuration and scheduler remain valid. |

### Performance evidence

Publish hardware/OS/tool versions, fixture/repository commit, commands, worker budgets, sample count, and all measured phases. Compare equal assertions/test inventories/full coverage, including setup and queue time. Never report only the fastest attempt. Use at least 20 warm samples for wrapper/inspection microbenchmarks and at least five alternating full/selected pairs for each adoption benchmark; report median and p95 with sample counts and label estimates as estimates.

Initial release targets on the documented reference machines:

- Uncontended warm explicit-run ptest overhead: p95 at most 250 ms, excluding the child process, declared setup, and intentional waiting, with those exclusions measured separately.
- Static warm plan on a 10,000-tracked-file/10,000-test fixture: p95 at most 2 seconds; if useful evidence cannot be produced within its bound, report a reason/full preview.
- Static doctor at its 2,000-file/16 MiB inspection bound: p95 at most 5 seconds and process RSS at most 256 MiB; cap hits remain visible.
- On the curated narrow-change adoption workloads, automatic selection should reduce median end-to-end execution time by at least 20% versus the same full gate, with zero reproducible selector misses in those comparisons. If it does not, keep automatic mode choosing full for that workload and publish the result; scheduler/doctor remain useful independently.

These are measurable targets, not present-tense claims or guarantees for every repository. Adjusting them requires a recorded reason and product/design review. A speed target cannot justify dropping tests, assertions, required setup, coverage, or observed failures. Historical remote eight-minute suite targets and month-long cloud rollout rules are not v1 prerequisites.

## 12. Finite milestones and gates [I8]

| Milestone | Deliverable | Exit gate |
|---|---|---|
| M0 — Approved product | This spec, independent Fable review, and resolved findings. | User approves the reviewed specification. No detailed design/code before approval. |
| M1 — Reviewed design | Minimal architecture, frozen config/CLI/JSON/resource contracts, support versions, budgets/defaults, negative test strategy, and bounded owned tasks. | Opus design review resolved; explicit workflow gate permits implementation. Prototype reuse/removal decisions are justified against this spec. |
| M2 — Local execution and coordination | Install/init/migration, reliable execution adapters, shared scheduler, status, cancellation/recovery, and static doctor/repair guide. | A1–A4, A8, A11, A13, relevant A14 cases; advertised absent selection/probes remain absent. |
| M3 — Selection and diagnosis | First-class selection/history/fallback/shadow behavior, timing, explicit probes, and resource guidance. | A5–A10 and remaining bounded-state cases; adversarial fixtures plus genuine runner subprocess evidence. |
| M4 — Release candidate | Integrated support matrix, independent adoption, reproducible benchmarks, packaging/migration documentation, and security evidence. | All A1–A14, no unresolved correctness/security findings, integrated Opus audit and independent Astra audit, corrections reverified; all untested limits stated. |
| M5 — Public release decision | User-selected license, package/distribution name availability, changelog, installation/rollback guide, contribution/security-reporting documentation, and publication plan. | Separate explicit user approval to publish/install over the live CLI. Completion of M4 is not publication permission. |

Implementation follows the approved routing in `.pipeline/context.md`: Muse implements bounded tasks with the actual Dan Jefferies instructions; Opus reviews stages/integration; Astra independently reviews integration; Sol handles remaining corrections after that Astra review. Do not substitute a model silently. The workflow logs actual model/version, evidence, findings, and stage transitions.

For changed code, run scoped TDD checks through `ptest` and one final integrated `ptest --full`; refresh the graph only after source changes. Security release evidence inventories secret scanning, static analysis, dependency/advisory scanning, and property/fuzz-style boundary tests. The design selects/pins applicable tools and controlled negative fixtures; this spec does not mandate an unnecessary scanner fleet or source-upload service. A missing applicable gate is disclosed and resolved before a public release, not silently claimed green. The candidate must be verified from its own isolated environment, not a borrowed worktree's dependencies.

## 13. Open decisions and sources

No unanswered product question blocks review of this concrete proposal. The user owns the license choice and publication approval; these are M5 release decisions. Linux+macOS v1 is the proposed platform scope, subject to the user's pending platform preference. Detailed design must resolve implementation choices left deliberately open here, including storage/locking, parser/schema specifics, exact supported versions, machine defaults, retention limits, and minimal baseline/probe grammar. None authorizes weakening the behavioral contracts.

Sources of product authority: `.pipeline/context.md` and the task's user intent. Existing-code evidence inspected read-only:

- Legacy `README.md`, `ptest`, `config.example.toml`, and `install.sh` at recovery tag `legacy/pre-ng-2026-09-17` (commit `bae8863`): local caps, runner compatibility, remote history, installation and output contracts.
- Historical `/home/ingmar/code/persea_content_maker/ptest_ng.md`: useful selection/fallback/failure ideas; its cloud storage, remote routing, concrete architecture, and rollout gates are superseded by local-first intent.
- Provisional `/home/ingmar/worktrees/ptest/cx-ng-foundation/ptest` at `33656db` plus uncommitted candidate changes, inspected 2026-09-17: examples of portable config, literal execution, and bounded static doctor. Its implementation decisions are not approval evidence, and it is not the specification baseline.
- [Interoperability research](../research/2026-09-17-agent-interoperability.md), including primary documentation links and local CLI evidence: one narrowly observed Muse rule-loading smoke, other native conventions documented, complete ptest integrations not yet tested.

This document records proposed requirements only. It contains no executed test, benchmark, security, platform, or selector-correctness claim.
