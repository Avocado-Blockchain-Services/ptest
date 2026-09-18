# ptest NG Task 11F: Pytest Full Basic-Serial Implementation Plan

> **For agentic workers:** Use `superpowers:executing-plans` or the already authorized isolated-worktree, test-first task workflow. Read `secure-by-spec` before implementation and `audit-spec` before auditing. Every test runs through `ptest`; the approved controller bootstrap is the development test harness described below.

**Created:** 2026-09-18 UTC

**Author:** architect-agent (Astra)

**Status:** Specification amended after Opus high rejection of `3e54bcd`; focused re-review required. Implementation and qualification not performed by this document.

**Inspected base:** `1dfea81d71417d1c6e07d7ff7b7477cd29829a55` in `/home/ingmar/worktrees/ptest/cx-ng-product/ptest`; original runtime inspection was at `f26dd56397f6f056c74247829d2641dcd9f76e0d`.

**Goal:** Extend accepted explicit scoped Pytest execution to explicit `ptest --full` under the same guarded, one-slot `basic_serial` contract, and make `ptest where` describe that capability and Vitest's unavailable foundation truthfully.

**Architecture:** Reuse `operations.execute`, the Pytest adapter, the native bridge, and the existing private terminal report lifecycle. Bind full mode and configured roots before importing project code, validate the effective native configuration at the existing execution boundaries, and retain conservative result claims. Extend the existing bounded source scanner with an explicitly opt-in Pytest-full generated-artifact policy and a distinct execution-only digest domain; default source/selection behavior and runtime compatibility remain unchanged. No second scanner, executor, report protocol, selector or compatibility layer is introduced.

**Tech Stack:** Existing Python ptest package, project CPython/Pytest, private JSON terminal reports, local scheduler and process-group guard; no new dependencies.

**Spec:** [Product specification](../../specs/2026-09-17-ptest-ng-product.md), especially T1/T3/C3; [accepted design](../../designs/2026-09-17-ptest-ng-design.md), especially the enumerated basic-serial tier; accepted evidence in `.pipeline/out/task-11d-opus-final.json` and `.pipeline/out/task-11e-opus-foundation-final.json`. This incremental slice supersedes the Task 11D full-refusal boundary only. Broader design goals do not authorize deferred features here.

## Global constraints and explicit non-goals

- Local first; remote execution/backends/cloud operations remain deferred. Product runtime requires no model API, hosted service, or coding TUI.
- Preserve Task 11D scoped behavior, literal argv, native stdout/stderr and exit status, configured assertions and user files. Full runs retain reporters/coverage only when their hooks satisfy the exact profile below; incompatible hooks cause refusal, never silent removal.
- Only Pytest explicit scoped and explicit full may execute. Bare/automatic/changed, selected plans, setup, shadow and probe remain refused. A setup declaration remains unsupported even with `--no-setup`; do not silently bypass it.
- Exactly one nonexclusive scheduler slot and one job; no xdist, native parallelism, retries, worker fan-out, or alternate command fallback. A request/configuration of eight workers still receives one slot.
- No Vitest activation, Go/Cargo activation, selection, history publication, recording or clearing of whole-gate failure obligations, baseline publication, runtime-identity promotion, verified inventory/count claims, or full-gate eligibility publication. Whole-gate failure obligations remain explicitly deferred even when the native full runner fails.
- No dependency/tuple installation, lockfile/manifest edits, bootstrap changes, packaging, live CLI replacement, publication, main merge, push or deployment. Missing prerequisites are reported, not installed by this task.
- Clean break: no legacy runtime import, legacy config discovery/translation, compatibility alias, remote fallback, or substitution of the installed legacy CLI for the candidate product under test.
- No public schema/config changes, new public flags, or new private terminal-report fields. Do not touch the provisional foundation checkout or unrelated dirty files.
- Specification-only work runs no tests, installs no dependencies, and needs no graph refresh. Implementation uses scoped `ptest` runs; the orchestrator owns one final integrated `ptest --full` at the approved final gate.

## Question space E(X, Q)

X is explicit Pytest full basic-serial execution plus truthful static capability inspection.

| Design question | Answer |
|---|---|
| What changes for users? | An initialized, already provisioned, compatible Pytest project may run its configured roots with prefix `--full`; `where` explains scoped/full basic-serial support and its limits. |
| What is a full run here? | The exact configured `runner.test_roots`, with `runner.args` and `runner.full_args`, under native parsing and verified serial/execution controls. It is not selected work or a published baseline. |
| Does automatic mode become a full fallback? | No. Explicit full admission does not authorize automatic mode, even though the wider design eventually calls for that fallback. |
| What can success establish? | A native execution outcome after authenticated terminal evidence and quiescent finalization. Source/runtime compatibility and full-gate publication stay unqualified. |
| What source evidence is required? | Comparable bounded Git content digests under the opt-in generated-artifact policy below. Root `.` is unsupported for full; non-Git/unreadable/over-budget evidence cannot complete full. Missing optional runtime compatibility alone does not invalidate equal content digests. |
| Can static inspection qualify a runtime? | No. `where` describes the implemented profile and required execution-time checks; it imports/runs nothing. |
| Where does implementation fit? | Five existing source modules, including a bounded extension of `source.py`, its focused tests, adapter/CLI/boundary tests and a new real full-subprocess test module. Shared report/scheduler contracts remain unchanged. |
| What unblocks support claims? | RED/GREEN through the capped controller, real provisioned tuple evidence, and Claude Opus high approval of the exact implementation range. |

## Baseline facts and prerequisites

1. Task 11D is accepted for scoped native execution. Its recorded real tuple is Linux x86_64, CPython 3.13.11, Pytest 9.1.1, pluggy 1.6.0, xdist absent. That is scoped evidence, not existing full qualification.
2. Candidate versions remain Pytest `8.4.2`, `9.0.3`, `9.1.0`, `9.1.1` and existing candidate CPython `3.11` through `3.14`. A candidate/version match is not proof of support. No broader range or new tuple is authorized.
3. Existing subprocess tests use a preprovisioned interpreter or `PTEST_TEST_PYTHON_<version_with_underscores>`. Missing versions skip explicitly as unqualified. The supported controller tuple must execute the new full tests; an all-skipped suite cannot pass this gate.
4. Active pytest-cov currently fails the critical-hook gate. Its arguments and native behavior must not be stripped or disabled to obtain a green result. Coverage qualification is a separate prerequisite, not added by Task 11F; a project requiring an unqualified coverage plugin fails closed.
5. Vitest is prepared but unavailable: no real Node/Vitest/Vite tuple or executor lifecycle is qualified. API doubles and the prepared bridge do not authorize activation.
6. `NativeReportBinding` and `NativeTerminalReport` already accept execution mode `full`; `reports.py` needs no protocol change. The bridge currently accepts only scoped report identity and must be extended explicitly.
7. `source.snapshot` can produce a content digest without a native runtime identity. Its compatibility limitation must remain visible; do not fabricate a compatibility identity from a version string or delete that limitation.
8. The current scanner fingerprints untracked and undeclared ignored files, including ordinary Pytest `.pytest_cache` and assertion-rewritten `tests/__pycache__` outputs. `non_input_outputs` cannot exempt paths overlapping test roots. Therefore full pass acceptance requires the controlled scanner extension below; merely ignoring cache paths in Git is insufficient. Do not hide this prerequisite with `PYTHONDONTWRITEBYTECODE`, `-p no:cacheprovider`, altered assertions, or prewarmed fixtures.
9. Current selection-policy validation rejects `.` roots, and source evidence also fails for a non-Git/mismatched checkout or scan limits. Task 11F explicitly refuses full with `.` roots and retains the other evidence failures; it does not relax selection policy or add a filesystem-only snapshot fallback.
10. `scripts/ptest-bootstrap` is the existing pinned, local, one-worker development harness. It writes a private generated configuration under the controller's `.pipeline/local/` and invokes the frozen legacy dispatcher; those are acknowledged development-harness side effects, not product implementation or candidate qualification. It rejects an outer `--full`; do not alter it. The task's candidate `--full` runs inside isolated subprocess fixtures, using the candidate package and real guard/bridge. No bootstrap invocation occurs during this specification repair.

## Immutable user-facing contract

### Admission and argv

| Input | Required result |
|---|---|
| `ptest --full` on compatible Pytest with no setup | `mode=full`, `plan.execution=full`, one slot, exact configured full command and roots. |
| `ptest --workers 8 --full` | Same full execution with `granted_workers=1`; never `-n 8` or exclusive host reservation. |
| Existing explicit scope | Unchanged literal scope and Task 11D claim restrictions. Full-only arguments are not appended. |
| Bare `ptest`, `ptest changed`, `ptest --changed` | `unsupported-capability` before enqueue or project/dependency execution. |
| Full with any nonempty runner suffix, including `--full -- -k expr` or `--full tests/a.py` | Refuse before enqueue; never reinterpret as a full run or silently discard the suffix. An empty delimiter alone carries no narrowing token. |
| A trailing flag-looking scoped value, e.g. `ptest -k --full` or `ptest tests/a.py --full` | Prefix-only grammar remains unchanged: it is scoped literal native input, not a newly parsed ptest full flag. |
| Setup declaration, shadow/probe request, selected plan, or mismatched mode/execution pair | Typed refusal, with no test launch and no dependency lifecycle. |
| Vitest execution request | Remains unavailable before admission. |
| Full with any configured test root `.` | `unsupported-capability` before admission. Config/init may still legitimately produce `.`; scoped behavior remains available and `where` must explain that full is unavailable for this root in Task 11F. |
| Full with non-Git/mismatched root, unreadable content or exceeded scan budget | Native execution may occur, but missing content proof means `incomplete`; native zero becomes final 70, an observed nonzero is retained. `where` explicitly states this execution-time condition and does not claim it has checked it. |

The adapter accepts only `(Mode.SCOPED, "scoped")` and `(Mode.FULL, "full")`. A full plan has no `plan.files`. Grant and attempt run identities agree, both worker counts equal one, and no setup/shadow/probe request reaches the bridge. A direct call to `prepare` cannot resurrect the old multiworker preparation branch.

The native full argv is exactly:

```text
validated launcher + trusted bridge path + runner.args + runner.full_args + runner.test_roots
```

Every user token is retained once, in order. There is no shell expansion, package-manager fallback, automatic plugin disabling, coverage threshold override, root inference from the invocation directory, or selection-based rewrite. `CommandSummary` stays redacted, uses one worker, includes the full root tokens in its argument count, and uses truthful Pytest provenance rather than the generic exclusive-command provenance.

### Full roots and native controls

- Full roots are a nonempty ordered tuple of unique literal project-relative paths from the existing configuration, excluding `.` in this slice. The config/init acceptance of `.` is unchanged, but it cannot complete the current source policy and therefore full refuses before admission. Files and multiple roots are allowed. Empty roots, options beginning `-`, argument files beginning `@`, node IDs containing `::`, traversal, absolute roots, or paths crossing symlinks are not valid full roots. Preserve existing config path validation; do not relax it or add a new root-discovery algorithm.
- The adapter rejects recognizable parallel/remote/narrowing controls from configured common/full argv and roots. Full `plan.files` and request tails cannot supplement or replace roots. The real native parser is the authority for option values and combined native settings; no universal argv arity parser is added.
- Before pytest/project import, capture the report identity, granted worker count, execution mode and expected roots into immutable bridge-owned values. Require execution mode to agree with the report binding. Subsequent checks use those captured values, not reread `PTEST_EXECUTION`/`PTEST_TEST_ROOTS`. A project hook changing environment variables cannot downgrade full to scoped or redefine the expected roots.
- Effective `config.args` must equal the configured roots in order, including cardinality; missing/additional/duplicate/rewritten paths fail closed. Native ini/TOML `testpaths` cannot replace the explicitly configured ptest roots. Redirected native root/config/collection controls may not suppress config/conftest input or change this contract: reject unsupported `--rootdir`, `-c/--config-file`, `--confcutdir`, `--noconftest`, `--pyargs`, and equivalent hook-induced changes rather than guessing equivalence.
- Effective full options must contain no `-k`, `-m`, node selector, deselect, last-failed/failed-first, stepwise variants, testmon, ignore/ignore-glob, positive maxfail/`-x`, collect-only, setup-only/setup-plan, fixtures/listing, help/version or other nonexecuting/narrowing mode. Cover equals forms and grouped short flags. Argfiles remain unsupported, including controls introduced through `PYTEST_ADDOPTS` or native `addopts`.
- Pytest's ordinary configured discovery conventions such as builtin filename/class/function patterns remain native project semantics. Full mode does not invent a larger inventory than the configured project gate. Project `collect_ignore`/`collect_ignore_glob` and observed deselection cannot certify this full profile. Hook restrictions are the exact sets below, including aliases and late registration; scoped restrictions do not acquire the additional full-only set.
- Revalidate after configuration, after collection-finish implementations, immediately before the test loop, before each item protocol/call, and after `pytest.main`. Refusal discovered after imports/setup/teardown is incomplete, never proof that no project code ran. Preserve ordinary fixtures, builtin skip/xfail behavior and only profile-compatible observation/cleanup hooks as defined below.
- Retain native output and error behavior. Unknown ordinary native options produce their genuine nonzero usage outcome; never rewrite them into success. A valid native no-tests exit remains nonzero. A supported custom reporter or coverage requirement is not removed merely because it fails.

These are checks on cooperative local execution, not a sandbox or a proof against hostile project code forging Python objects or calling arbitrary processes. That residual limitation must be carried into qualification evidence.

#### Exact hook ownership policy

The current builtin `_pytest.*` hook owners and the ptest-owned plugin are permitted. Any other registered owner of the following hooks is refused, whether its implementation is plain, `wrapper=True`, or `hookwrapper=True`, regardless of `tryfirst`/`trylast`, plugin alias or `specname`. An allegedly observation-only implementation of a forbidden hook is still refused; the bridge does not inspect function bodies to infer harmlessness.

| Applies to | Exact forbidden external hook names |
|---|---|
| Scoped and full (existing Task 11D set) | `pytest_cmdline_main`, `pytest_collection`, `pytest_runtestloop`, `pytest_runtest_protocol`, `pytest_runtest_call`, `pytest_pyfunc_call` |
| Full only (Task 11F addition) | `pytest_collection_modifyitems`, `pytest_ignore_collect`, `pytest_runtest_makereport`, `pytest_report_teststatus`, `pytest_sessionfinish` |

The reporter/cleanup compatibility promise is specifically for `pytest_terminal_summary`, `pytest_runtest_logstart`, `pytest_runtest_logreport`, `pytest_runtest_logfinish`, and `pytest_unconfigure`, in plain or wrapper form, provided the same plugin registers none of the forbidden hooks and satisfies the other profile checks. Fixtures, configuration and collection-finish hooks remain trusted project code subject to repeated gate checks; they are not certified observation-only. Other custom reporters are unqualified unless their actual registered hooks meet this policy. In particular, even a logging-only external `pytest_sessionfinish` is refused for full, and active pytest-cov remains unqualified. Refuse an incompatible plugin with a safe diagnostic; never unload it or strip its arguments.

Required real tests: a custom `pytest_terminal_summary` prints a sentinel and `pytest_unconfigure` writes a declared non-input cleanup marker during a successful full run; both survive. Separate plain and wrapper cases for `pytest_collection_modifyitems`, `pytest_runtest_makereport`, `pytest_report_teststatus` and `pytest_sessionfinish` are refused, including an observational session-finish hook. Existing scoped cases keep their previous behavior. At least one aliased and one late full-only hook registration must invalidate full completion.

#### Full-only generated-artifact source policy

Add an opt-in policy to the existing scanner, used only by Pytest full basic-serial operations. It is not a public selection exclusion or general ignore mechanism. Default `snapshot` calls, scoped Pytest, command execution and future selection consumers retain today's behavior. A separate domain tag, `ptest-pytest-full-content-v1`, prevents these execution-only digests from being confused with `ptest-source-v2`; the opt-in call requires `baseline=None` and `runtime_identity=None` and always returns `compatibility=None` with an execution-only limitation.

The policy removes only these generated regular files from the untracked/undeclared-ignored content set:

1. A file directly under `__pycache__` matching a CPython cache basename `<module>.cpython-<digits>[.opt-<digits>].pyc` or Pytest's assertion-rewrite basename `<module>.cpython-<digits>-pytest-<enumerated-version>.pyc`, where the corresponding sibling `<module>.py` is a regular file included in the same content snapshot. Match the complete basename, not any `.pyc` suffix. Never exempt sourceless bytecode, a malformed/cache-like user filename, or a path without the included `.py` input.
2. Only the checkout-root default cache files `.pytest_cache/.gitignore`, `.pytest_cache/CACHEDIR.TAG`, `.pytest_cache/README.md`, `.pytest_cache/v/cache/nodeids`, `.pytest_cache/v/cache/lastfailed`, and `.pytest_cache/v/cache/stepwise`. A custom `cache_dir`, arbitrary plugin cache key, nested `.pytest_cache`, or other file under that directory is not automatically exempt. Existing safe user `non_input_outputs` declarations remain available for genuine output paths outside protected inputs.

Tracked/indexed paths and explicit `selection.ignored_inputs` always win over generated-artifact classification and remain fingerprinted. Do not exempt a whole directory or use Git ignore status alone. Generated candidates must pass the existing no-follow regular-file/path checks; a symlink, FIFO, inaccessible path or raced replacement cannot be silently discarded. Apply existing total listing/count/deadline budgets before filtering, and retain all existing content, Git-state and no-network checks. Both snapshots apply the identical policy. Do not amend `_invalid_policy`, reduce the source input roots, suppress cache generation or delete generated/user files.

This is a declared execution-only treatment of known tool output, not proof that repository code cannot read or maliciously repurpose a cache. That residual trust limit is why source-validity, compatibility, inventory and full-gate publication remain unavailable. If evidence shows another ordinary generated format, record the actual format and seek a focused policy amendment; do not add a broad wildcard to obtain green tests.

Native acceptance must start from a fresh committed Git fixture with explicit `tests` roots and normal cache/bytecode settings, run candidate `--full` twice, observe actual assertion-rewrite `.pyc` and pytest cache creation, and obtain complete zero outcomes with equal execution-only content digests. Also mutate a tracked file under a cache-like name, a declared ignored cache input, ordinary source/expectation content, a sourceless `.pyc`, and an unknown pytest cache key: each remains input evidence and invalidates zero-exit full. Unit tests must prove the default scanner still fingerprints all these generated outputs.

### Results, source and lifecycle

The ordering remains:

```text
pre-admission refusal checks
  -> one-slot local grant -> re-resolve config after queue
  -> allocate private full report -> adapter preparation
  -> source snapshot -> guard/bridge/native execution
  -> authenticated DRAINING + guard reap -> final source snapshot
  -> scheduler quiescence/finalization proof -> consume report
  -> exclusive result export -> release lease -> owned report cleanup
```

- Re-resolved config must equal the admitted config, including roots, full args, setup, project identity and resources. A queued edit cancels/refuses without launching stale work or leaving an unregistered granted lease.
- Authenticate the existing report's run ID, nonce, attempt, runner, `execution_mode=full`, `effective_profile=basic_serial`, observed runtime, report basename and native/bridge/guard exit agreement. Scoped reports cannot satisfy full bindings or vice versa. No translation of old event/profile/status/files records.
- A complete native zero with equal full-policy content digests and a satisfied runtime/hook profile returns `status=passed`, `runner_exit_code=0`, `exit_code=0`, `exit_origin=runner`. This excludes `.` roots and unavailable source evidence as specified above. Native failure retains its actual nonzero code and `failed` status unless later evidence makes the run incomplete. Native collection/setup/teardown/usage errors retain their codes; do not call them bridge refusals unless authenticated bridge evidence says so.
- Missing/malformed/replayed/mismatched/colliding reports, uncertain quiescence, invalid control frames, failed required export/finalization, or a bridge refusal never certify completion. Preserve existing nonzero/signal precedence. A previously zero child becomes final 70 for missing required evidence; authenticated bridge refusal retains its existing nonzero bridge code with ptest origin.
- Full-policy content snapshots are taken after admission and after drain while the checkout remains reserved. Different digests or lost final digest produce `changed-during-run` or `unknown-input`, `incomplete`, and zero-to-70 conversion. If either digest is unavailable, including non-Git roots or exceeded scan budgets, full is incomplete with `unknown-input`; retain the underlying `policy-invalid`, `scan-limit`, unsafe-path or other available reason in limitations. An existing nonzero child is retained. Do not apply this stricter full content rule to scoped runs.
- Missing `InputSnapshot.compatibility` solely because runtime identity is unqualified is an explicit limitation, not a reason to turn every basic-serial full run incomplete when content digests are present and unchanged. `source.py` must implement the bounded opt-in artifact policy above; its default/selection compatibility contract is unchanged. Only that exact artifact set and existing valid non-input outputs are exempt; changed tracked source/expectations remain inputs even if a test wrote them intentionally.
- Every scoped/full Pytest result in this slice has `source_valid=false`, `full_gate_eligible=false`, `baseline_published=false`, `counts=null`; attempts have `source_valid=false` and `inventory_complete=false`. A passed full result reports the native execution, not a verified final-tree gate or an assertion that every possible test body ran. Keep explanatory limitations truthful.
- No history database/outcome/baseline is created or updated; preexisting history/obligations remain untouched. Recording and clearing whole-gate failure obligations are both deferred, so a failed full run supplies its native result but no persisted future-selection obligation. No run may claim to clear unresolved failures. Report cleanup is confined to the owned attempt and existing authenticated cleanup rules.

### `ptest where`

Retain the versioned `where` schema and safe summary fields. Static inspection never invokes a runner, imports project code, allocates scheduler/report/history state, installs dependencies, probes versions, reads legacy config or makes a network request.

| Resolved runner/config | Capability and required explanation |
|---|---|
| Pytest without setup, valid static launcher/control prerequisites, roots other than `.` | `execution="basic_serial"`, `selection=false`, existing cooperative lifecycle. This is a conditional implemented tier, not qualification of this runtime/project. Explain explicit scoped/full, one worker, execution-time tuple and exact hook/profile checks, and comparable bounded Git content evidence required for full completion. Explain unavailable automatic/setup/shadow/probe/inventory/history/whole-gate-obligation/baseline/full-gate publication. |
| Pytest with test root `.` (including config produced by init) | `execution="basic_serial"` describes scoped execution only. State explicitly: full unavailable for `.` roots in this slice, and execution-time tuple/profile checks still apply to scoped. Do not label the whole runner unavailable or advertise full readiness. |
| Pytest where Git/source readability or scan budget has not been verified | Same conditional tier; state that static inspection has not checked source evidence. Non-Git/mismatched roots or missing/over-budget content proof produce incomplete full, zero-to-70 conversion, and retained native failures. `where` must not launch a source scan or claim these preconditions passed. This includes non-Git and oversized-project fixtures. |
| Pytest with unsupported setup or statically provable invalid launcher/shared controls preventing both modes | `execution="unavailable"` with the applicable safe limitation; no broad assertion that the configured project can run. A defect confined to full-only controls yields scoped-only `basic_serial` with an explicit full-unavailable limitation instead. |
| Vitest | `execution="unavailable"`, `selection=false`; explicitly prepared only, with executor integration and real native tuple qualification unavailable. Never `basic_serial`. |
| Uninitialized project | Existing `capability=null`, no command summaries, no created state. |

Do not enumerate new JSON capability fields such as `supported_modes`; use existing limitations and summaries. Both Pytest summaries must use the effective serial worker count and distinguish scoped/full composition without exposing argv. Human `where` output must actually display the runner, capability and concise limitation, rather than printing only root/initialized while hiding the correction in JSON. Other runner execution contracts are unchanged.

Tests must assert the same conditional text/limitations for valid Git, non-Git and unscanned/over-budget fixtures without reading their source trees. `.` roots need their distinct scoped-only limitation; a configured full command summary describes configuration, not a capability promise. Executing fixtures separately assert full refusal for `.`, incomplete/70 for non-Git and injected real scanner budget exhaustion, and preservation of a nonzero native failure under the same source conditions.

## Ownership, interfaces and dependencies

Implement this as one bounded task in its own worktree. Do not spawn overlapping source writers.

| Owned file | Responsibility | Complexity/risk |
|---|---|---|
| `src/ptest/adapters/pytest.py` | Closed mode pairs, one-slot preparation, strict full root/control validation, immutable bridge binding inputs, truthful capability/summary. Reuse a small pure helper here if needed by CLI. | Medium; literal argv and direct-call bypasses. |
| `src/ptest/runtime/pytest_bridge.py` | Frozen full/scoped identity, full native policy checks, full terminal report. Retain additive reporter and late-hook defenses. | High; native parsing and hook order. |
| `src/ptest/operations.py` | Explicit full admission, full plan/summary, existing report/guard lifecycle, full content-invalidation semantics, no claim promotion. | Medium; lease and exit precedence. |
| `src/ptest/source.py` | Opt-in Pytest-full generated-artifact classification and digest domain using the existing bounded scanner; default source/selection semantics unchanged. No dot/non-Git fallback or scan-budget relaxation. | Medium; accidental input exclusion. |
| `src/ptest/cli.py` | Truthful static JSON/human `where` using current public schema; preserve prefix-only parsing. | Small; accidental execution or unsupported claims. |
| `tests/ng/test_task_11f_pytest.py` (new) | Deterministic admission, mode/grant/root/binding/full-result negatives. | Medium. |
| `tests/ng/test_pytest_full_subprocess.py` (new) | Real candidate full execution and adverse lifecycle/configuration/source cases, no installs. | High. |
| `tests/ng/test_pytest_adapter.py` | Update obsolete unavailable/multiworker expectations; add full parser/profile negatives. | Medium; distinguish native acceptance from setup refusal. |
| `tests/ng/test_task_11d_pytest.py` | Retire only obsolete full refusal assertions; retain scoped/automatic/setup safeguards. | Small. |
| `tests/ng/test_cli.py` | Static/human/JSON capability, privacy and no-execution proof. | Small. |
| `tests/ng/test_source.py` | Exact generated-file policy, tracked/declared-input precedence, no-follow checks, default-policy regression, digest-domain separation and unchanged scan limits. | Medium. |
| `tests/ng/fixtures/pytest/README.md` | Record exact full evidence and still-unqualified matrix rows. | Small. |
| `.pipeline/out/task-11f-pytest-full.json` (new, implementation author) | RED/GREEN commands, native tuples, skips, security dispositions and residual limits. | Small. |

Read-only dependencies: `contracts.py`, `reports.py`, `selection.py`, `scheduler.py`, `guard.py`, `config.py`, `tests/ng/support.py`, existing scoped subprocess tests and Vitest tests. No modification is currently required to those dependencies. The only ownership expansion from the first draft is `source.py` and `test_source.py` for F1's bounded full-only policy. A demonstrated prerequisite outside ownership requires a separately scoped decision and evidence; do not widen silently.

Public/internal entry points remain:

```python
prepare(config: C.Config, plan: C.Plan, grant: C.Grant,
        attempt: C.AttemptIdentity) -> C.PreparedRun
execute(domain: C.DomainPaths, config: C.Config,
        request: C.RunRequest) -> C.RunResult
pytest_bridge.run(argv: list[str] | tuple[str, ...] | None = None) -> int
```

The one additional private source API parameter is explicit and defaults off:

```python
snapshot(domain: C.DomainPaths, config: C.Config, baseline: C.Baseline | None,
         base: str | None, *, runtime_identity: str | None = None,
         pytest_full_outputs: bool = False) -> C.InputSnapshot
```

`pytest_full_outputs=True` is valid only for runner Pytest with `baseline=None` and `runtime_identity=None`; reject other combinations. `operations._capture_source` supplies it only for explicit Pytest full. Preserve the original validation/default path and fingerprint implementation; add the small bounded classifier before fingerprinting and the distinct digest tag, not a second scanner or selection-policy bypass.

If shared static capability inspection is necessary, put the pure Pytest-specific function in its adapter:

```python
def inspect_capability(config: C.Config) -> C.Capability:
    """Describe static scoped/full prerequisites without I/O or native imports."""
```

`prepare` consumes the same static validation before attaching the grant-dependent command. The CLI consumes the capability; it must not duplicate a separate rule table or call `prepare` with a fabricated grant. Bridge policy values are constructor state captured by `run`, not a new public contract or mutable environment source.

## Implementation phases and acceptance

### Phase 1: Foundation and RED

- [ ] Read the linked product/design, accepted 11D/11E evidence, `AGENTS.md`, `.pipeline/context.md`, and mandatory implementation skills in the assigned worktree.
- [ ] Add deterministic failing tests for explicit full capability/preparation, immutable binding, invalid mode pairs, multiworker direct grants, roots and full result claims. Add exact generated-artifact/default-policy tests and dot/non-Git/scan-limit contract tests. Add static `where` tests for both Pytest and Vitest, including conditional tier/source/hook limitations and fail-on-call sentinels at execution/network boundaries.
- [ ] Replace only contradictory assertions that full is always unavailable or that multiworker basic-serial preparation adds xdist. Retain automatic/setup negatives and historical scoped behavior. Existing fixture projects with setup continue to refuse full because setup is unavailable; they are not the new full qualification test.
- [ ] Run the exact focused RED scope below; record failing test names and the intended missing full/where behavior. An import, environment or dependency failure is not acceptable RED.

Acceptance: expected behavior is executable in test assertions before production edits; no source/profile behavior has yet been changed. Estimated effort: Small.

### Phase 2: Core admission and bridge

- [ ] Admit only explicit full in addition to scoped. Validate mode/execution pairs, full roots and one-slot grants; remove the obsolete active multiworker preparation path and update only its affected tests.
- [ ] Bind full report identity without changing the terminal schema. Capture mode/roots before import; add full-specific effective option/root/selection/result-hook validation at existing repeated gates.
- [ ] Add the opt-in bounded generated-artifact policy to the existing source scanner with tracked/explicit-input precedence and a separate execution-only digest tag; leave selection and default callers unchanged. Refuse full `.` roots without modifying init/config.
- [ ] Correct Pytest plan reasons and redacted command summaries so full execution is not described as an exclusive generic command or a selected fallback.
- [ ] Run the focused scope GREEN; retain meaningful bridge doubles for malformed identities and late mutation, without calling them native evidence.

Acceptance: full preparation and bridge binding are strict; scoped tests still pass; there is no route to selected/automatic/setup/Vitest/parallel execution. Estimated effort: Medium.

### Phase 3: Real full execution and lifecycle

- [ ] Add real subprocess full tests using `case.invoke` with explicit fixture domains and preprovisioned interpreters. Start fresh committed Git fixtures with normal bytecode/cache settings; prove generated `.pyc`/pytest cache creation and two unchanged full passes under the exact policy. Add tracked/explicit-input/cache-lookalike mutation negatives, plus dot/non-Git/scan-limit outcome cases. No bytecode/cache disabling or cache prewarming may mask the first-run behavior.
- [ ] Record a native RED proving the missing full path on the primary tuple before completing lifecycle changes. Cover pass/failure, literal full-only values, multiple roots, required report evidence, source changes, queue revalidation, serial bounds and release.
- [ ] Reuse existing guard/report/finalization code and add only the full content checks needed for truthful outcomes. Keep all publication/validity/count claims false/null.
- [ ] Add the real positive terminal-summary/cleanup case and negative plain/wrapper selection, result-rewrite and session-finish cases from the exact hook policy; include an observation-only session-finish refusal and scoped regression.
- [ ] Run the native scope GREEN. Every negative test observes the appropriate body marker, raw/final code, reason/origin, persisted result artifact and released-or-deliberately-retained lease; a nonzero code alone is insufficient.

Acceptance: the primary real tuple proves the complete candidate full path, unavailable candidates are explicitly unqualified, and lifecycle/report/source failures cannot become successful final-tree claims. Estimated effort: Medium to Large.

### Phase 4: Static integration and scoped regression gate

- [ ] Render truthful JSON and human `where` results using the current schema. Runtime compatibility remains unverified until an executing run; static commands retain their no-execution/no-state-write contract.
- [ ] Run the combined scoped regression command below, including old scoped Pytest, reports, generic operations, CLI and unavailable Vitest. Inspect durations; investigate new tests over three seconds and avoid arbitrary synchronization sleeps.
- [ ] Run `git diff --check` and `graphify update .` in the changed implementation checkout. Do not invoke semantic extraction or regenerate public schemas for unchanged contracts. Record existing security-gate availability and bounded missing checks honestly; do not install tools under this task.

Acceptance: scoped and generic behavior remains intact; no Vitest or public-contract activation; graph and whitespace checks recorded. Estimated effort: Small.

### Phase 5: Documentation and Opus high gate

- [ ] Update the fixture README and implementation evidence with every command/result, exact native tuple, skip, source/report limitation, and RED/GREEN provenance. Report specification/implementation/qualification separately.
- [ ] Request Claude Opus high read-only audit of the exact base-to-final range using the criteria below. No blocking findings may remain before orchestrator integration; record actual reviewer/model/effort and exact reviewed commit.
- [ ] Commit only owned implementation/test/evidence files inside the assigned task worktree when authorized by the orchestration workflow. The orchestrator integrates, refreshes its graph, verifies the integrated state, and owns the later final whole-suite gate.

Acceptance: evidence matches the shipped diff and the exact approved range, with no unsupported feature/support claims. Estimated effort: Small plus review.

## Required security and negative-test matrix

| Axis | Required falsification cases and observable result |
|---|---|
| Identity and mode authority | `Mode.AUTOMATIC` plus execution full; Mode full plus scoped/selected; full `plan.files`; two-worker matching grant/attempt; wrong run/nonce/attempt/profile/mode; hook mutates mode/roots environment. Refuse or invalidate, no scope downgrade or report reuse. |
| Input/command boundaries | Every narrowing/control family through args, full_args, ini/TOML addopts and `PYTEST_ADDOPTS`; equals/grouped forms; argfile; root option/node ID/duplicates/traversal/symlink; config/root/conftest redirects; extra positional native paths. No complete full report for a narrowed gate; literal metacharacter option values have no shell side effect. |
| Native plugin and outcome authority | Active xdist or named executor; aliased/nested/late executor hooks; exact full-only selection/result/session-finish hook set in plain and wrapper forms, including harmless observers on forbidden hooks. Positive native terminal-summary plus cleanup hooks remain visible. Preserve existing scoped behavior. Before-known-unsafe collection/body markers remain absent; late refusal records that earlier side effects may exist. |
| Data/privacy boundaries | Default `where` and run JSON omit argv, environment values, secrets and source excerpts. Human diagnostics escape controls. Reveal remains explicit stderr-only. Inspections never execute native config or a package manager. |
| File/report ownership | Scoped report replayed as full; missing/truncated/oversize/duplicate-key/extra-key/mode-mismatched report; symlink/collision and swapped basename; bridge/native/guard exit disagreement. Preserve other files and the observed nonzero child; never publish success. Existing shared report tests supply generic codec proof; add full integration cases, not duplicate codecs. |
| Source/final tree | Fresh real full with normal pytest cache/assertion-bytecode writes, then a second unchanged full; exact known generated files excluded only under opt-in policy. Tracked/declared-input cache paths, unknown cache keys, sourceless bytecode, lookalikes, symlink/FIFO/raced candidates and ordinary source/expectations remain protected. Default scanner unchanged. Dot-root pre-admission refusal; non-Git/scan-limit/missing digest incomplete/70; failure code retained; compatibility unavailable with equal full-policy digests is only a limitation. |
| State and availability | Same-checkout full/scoped serialization, separate-checkout bounded overlap, queued config/root/full-arg mutation, cancellation, guard failure, export collision, quiescence failure and lease release. Shared tests may supply unchanged generic cases, but full mode requires real admission/report/cancel/release evidence. No cleanup outside the owned run. |
| Dependencies/supply chain | Missing interpreter/unsupported runtime, missing candidate environment, setup declaration, active unqualified coverage plugin. No install/sync/network/legacy fallback and no plugin stripping. Available tuple evidence is mandatory; skipped tuples stay unqualified. |
| Claims and regression | Full native failure/no tests/collection/setup/teardown/usage error retain nonzero status; counts stay unknown; no history/baseline or whole-gate-failure-obligation recording/clearing; automatic/Vitest remain unavailable; scoped full-looking suffix stays literal. Static JSON validates through existing decoders/schema and explicitly states tuple/hook/Git-source conditions, with dot-root scoped-only capability. |

## Exact RED/GREEN and regression scopes

The orchestrator assigns the implementation worktree `/home/ingmar/worktrees/ptest/cx-ng-product/task-11f-pytest-full`. If its slot differs, substitute that one absolute path consistently in all commands and record the substitution. Never share a virtualenv or `node_modules` between worktrees.

The existing controller owns a provisioned no-xdist development environment. These commands intentionally use its capped, pinned bootstrap, with candidate source and absolute test paths from the task worktree. The bootstrap writes the controller's private generated `.pipeline/local/ptest-bootstrap.toml` and invokes the pinned legacy bundle as the outer development harness; record these expected side effects. `case.invoke` must launch the candidate ptest package. Nothing installs/replaces the product, changes bootstrap, or activates legacy runtime behavior in the candidate, and no fixture invokes a raw runner.

Focused deterministic RED, then the identical GREEN:

```bash
env PYTHONPATH=/home/ingmar/worktrees/ptest/cx-ng-product/task-11f-pytest-full/src /home/ingmar/worktrees/ptest/cx-ng-product/ptest/scripts/ptest-bootstrap /home/ingmar/worktrees/ptest/cx-ng-product/task-11f-pytest-full/tests/ng/test_task_11f_pytest.py /home/ingmar/worktrees/ptest/cx-ng-product/task-11f-pytest-full/tests/ng/test_pytest_adapter.py /home/ingmar/worktrees/ptest/cx-ng-product/task-11f-pytest-full/tests/ng/test_task_11d_pytest.py /home/ingmar/worktrees/ptest/cx-ng-product/task-11f-pytest-full/tests/ng/test_cli.py /home/ingmar/worktrees/ptest/cx-ng-product/task-11f-pytest-full/tests/ng/test_source.py -q --tb=short --durations=20
```

Real full subprocess RED, then the identical GREEN:

```bash
env PYTHONPATH=/home/ingmar/worktrees/ptest/cx-ng-product/task-11f-pytest-full/src /home/ingmar/worktrees/ptest/cx-ng-product/ptest/scripts/ptest-bootstrap /home/ingmar/worktrees/ptest/cx-ng-product/task-11f-pytest-full/tests/ng/test_pytest_full_subprocess.py -q --tb=short --durations=20
```

One combined final scoped regression gate after the last substantive change:

```bash
env PYTHONPATH=/home/ingmar/worktrees/ptest/cx-ng-product/task-11f-pytest-full/src /home/ingmar/worktrees/ptest/cx-ng-product/ptest/scripts/ptest-bootstrap /home/ingmar/worktrees/ptest/cx-ng-product/task-11f-pytest-full/tests/ng/test_task_11f_pytest.py /home/ingmar/worktrees/ptest/cx-ng-product/task-11f-pytest-full/tests/ng/test_pytest_full_subprocess.py /home/ingmar/worktrees/ptest/cx-ng-product/task-11f-pytest-full/tests/ng/test_pytest_scoped_subprocess.py /home/ingmar/worktrees/ptest/cx-ng-product/task-11f-pytest-full/tests/ng/test_pytest_adapter.py /home/ingmar/worktrees/ptest/cx-ng-product/task-11f-pytest-full/tests/ng/test_task_11d_pytest.py /home/ingmar/worktrees/ptest/cx-ng-product/task-11f-pytest-full/tests/ng/test_operations.py /home/ingmar/worktrees/ptest/cx-ng-product/task-11f-pytest-full/tests/ng/test_reports.py /home/ingmar/worktrees/ptest/cx-ng-product/task-11f-pytest-full/tests/ng/test_cli.py /home/ingmar/worktrees/ptest/cx-ng-product/task-11f-pytest-full/tests/ng/test_vitest_adapter.py /home/ingmar/worktrees/ptest/cx-ng-product/task-11f-pytest-full/tests/ng/test_source.py /home/ingmar/worktrees/ptest/cx-ng-product/task-11f-pytest-full/tests/ng/test_selection.py -q --tb=short --durations=20
```

If the controller environment is absent or its pinned bootstrap fails verification, record the prerequisite failure and stop that verification path. Do not call `pytest`, change bootstrap, install a tuple or use a different runtime silently. No `ptest --full` is run during this spec stage or this task's scoped iteration. The orchestrator's one later final integrated `ptest --full` is separate from testing the candidate's `--full` behavior inside these scoped fixtures.

## Claude Opus high audit criteria

The auditor follows `audit-spec`, reads the approved brief and exact diff, and checks the matrix above against actual assertions and real subprocess artifacts. Required questions:

1. Can any request/config/Plan combination turn automatic, selected, setup, shadow, probe or Vitest into executable work? Can direct preparation allocate/launch more than one worker?
2. Can a native parser, argfile, addopts, alternate config, root mismatch, environment mutation or ordinary selection/result hook narrow the configured full run while preserving a complete full report?
3. Are mode/roots frozen before repository execution, checked repeatedly, and bound to the same terminal identity consumed after quiescence? Does the exact forbidden full hook set reject plain/wrapper/aliased/late owners while real allowed terminal-summary/cleanup hooks survive? Are old scoped protections retained?
4. Does the primary native fixture create real ordinary pytest caches/bytecode and pass unchanged without disabling/prewarming them? Is the full-only generated policy exact, bounded and domain-separated, with tracked/explicit/cache-lookalike inputs protected and default source/selection behavior unchanged? Do dot/non-Git/scan-limit cases follow the documented refusal/incomplete outcomes without fake compatibility, and are nonzero/signal/refusal outcomes preserved?
5. Can missing/replayed/forged reports, export collisions, cancellation or queue mutation leak ownership, overwrite foreign files, erase failure obligations or certify a false pass?
6. Does `where` remain wholly static, distinguish conditional Pytest scoped/full from Vitest prepared/unavailable, show dot-root scoped-only support, and disclose unverified tuple/hook/Git-source/budget conditions and setup restrictions? Does it preserve privacy/schema compatibility without implying static qualification?
7. Do RED/GREEN and primary real tuple results exercise the candidate source rather than a different worktree or installed CLI? Are unavailable tuples, coverage and macOS evidence labeled unqualified rather than passing?
8. Is the diff limited to owned scope, with no dependencies, installation, public schemas, remote/legacy substitutions or speculative frameworks? Are obsolete full-refusal/multiworker assertions corrected without deleting meaningful negatives?

Approval requires no blocking findings and a disposition for every required matrix axis. A reviewer approval of this specification is not an implementation or runtime qualification verdict. Report the exact reviewed commit/range, model, effort, tests actually run, omissions and residual trust limitations.

## Success criteria and unresolved prerequisites

- [ ] Primary provisioned tuple completes real explicit full pass/failure tests with ordinary first-run pytest cache/bytecode generation and a second unchanged pass, preserving native output and configured roots under exactly one slot.
- [ ] Narrowing/mode/report/source/lifecycle negatives prove fail-closed behavior and keep every unsupported claim false or unknown.
- [ ] Existing scoped Pytest/generic execution tests remain green and Vitest remains prepared/unavailable.
- [ ] Static JSON and human `where` report conditional scoped/full capability, dot-root scoped-only support, and execution-time tuple/hook/Git-source conditions without execution or state creation.
- [ ] Task evidence and exact-range Opus high gate are recorded before integration.

Known prerequisites outside this slice are full support for `.` roots, non-Git/unreadable/over-budget source handling beyond the stated incomplete outcome, additional native tuple/macOS qualification, active coverage compatibility, real Vitest integration, setup support, runtime compatibility/inventory/history/selection and whole-gate failure-obligation publication, final integrated whole-suite verification, and packaging/release approval. None is silently promoted by Task 11F. This amendment proposes only the explicit `source.py`/`test_source.py` ownership expansion above for focused re-review; all other scope boundaries remain in force.

## Opus review dispositions for the amendment

| Finding | Specification correction and required evidence |
|---|---|
| F1 HIGH: ordinary generated files invalidate every fresh full | Own a bounded, opt-in source-scanner output policy with exact bytecode/default-cache paths, protected inputs and a distinct execution-only digest. Require actual fresh native cache generation and two unchanged passes; forbid bytecode/cache disabling or fixture prewarming. No claim that current `source.py` already supports this. |
| F2 HIGH: dot/non-Git/scan-limit evidence conflicts with advertised success | Dot roots remain valid configuration but are explicitly full-unsupported before admission and scoped-only in `where`. Non-Git/missing/over-budget content cannot complete full, with 70 for a zero child. Static tier text explicitly conditions full completion on unverified execution-time Git/content evidence and tuple/hook checks; add distinct execution and no-scan inspection tests. |
| F3 MEDIUM: reporter preservation contradicts hook refusals | Freeze the two exact forbidden external hook sets, apply them equally to plain/wrapper/alias owners, and name surviving reporter/cleanup hooks. Require real positive terminal-summary/cleanup and negative selection/result/session-finish cases, including a harmless session-finish observer. |
| Additional notes | Defer both recording and clearing whole-gate failure obligations; clarify static tier is conditional; acknowledge generated bootstrap configuration and frozen-dispatcher side effects without changing or executing the harness in the specification stage. |
