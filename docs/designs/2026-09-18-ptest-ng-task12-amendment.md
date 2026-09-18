# Task 12 amendment: authenticated compound execution

Created: 2026-09-18 UTC. Author: architect-agent (Astra).

Status: Opus reviewed with no HIGH blockers; requested documentation repairs are incorporated for controller verification before implementation. Documentation only; no implementation, test, or capability qualification is claimed.

Authority: the [approved product](../specs/2026-09-17-ptest-ng-product.md), especially T3–T5, D4, C3 and A5–A10, and the [implementation design](2026-09-17-ptest-ng-design.md). This amendment replaces Task 12's ownership and fills its compound-control and quarantine contracts. It supersedes the original design's guard protocol only where explicitly stated below. All local-first, no-remote, literal-argv, capacity, privacy, setup, and release boundaries remain applicable.

Evidence baseline: controller `71fb7399a49f34cd27c5737fe375a461988fc001`. Rejected candidate commits `479e739`, `5ebcbd9`, and `f699d63` were inspected read-only. Their command-profile comparison, cancellation after an ordinary selected failure, private history-marker calls, and 30-second shadow limit are not reusable product decisions. The candidate report contains no passing test run.

## 1. Question space and decomposition

For E(X,Q), X is an explicit selected/full comparison and a bounded serial/parallel probe using the existing local executor.

| Q | Frozen answer |
|---|---|
| Who decides whether another attempt is permitted? | Operations validates evidence and inputs; the authenticated guard alone consumes a one-use continue decision and spawns. |
| Is an ordinary selected failure a stop condition? | No. Full still executes; the first nonzero outcome survives. |
| Can an execution-only command stand in for a selector? | No. Only a qualified first-class advanced profile plus a genuine eligible selected plan can run shadow. |
| Who changes selection quarantine? | History, through one public typed publication API. Health/corruption markers have separate ownership and cannot be cleared by comparison. |
| Is rejection enough to complete probes? | No. Qualification and real serial/parallel observations are mandatory Task 12 work. |
| What task split is necessary? | Three ordered deliverables: T12a shared lifecycle/history contracts; T12b native capability qualification; T12c compound integration and acceptance. |

A single operations-only task cannot repair the protocol or history boundary. A single expanded task covering every adapter and orchestration change would combine independently rejectable qualification and lifecycle decisions. T12a → T12b → T12c is the smallest honest sequence; each has its own scoped tests, commit and Opus gate. T12b finishes the relevant unfulfilled T5/T6 advanced contracts, rather than adding a substitute runner. Task 12 and M3 remain incomplete until the required profiles and positive tests pass. An unavailable profile is reported explicitly, never counted as a completed deferred feature.

No parallel writer owns shared contracts, reports, operations, registry or CLI during this sequence. The orchestrator transfers ownership after preceding T11/native-profile work has stopped. Existing changes in other worktrees remain untouched. The implementation workflow uses secure-by-spec, scoped candidate `ptest` tests, and the existing review routing; this documentation amendment runs no tests.

## 2. Guard protocol and attempt decision gate

### Version and transport

Introduce `GUARD_PROTOCOL_VERSION = 2` for `ControlFrame` and `LaunchManifest` only. Public `schema_version`, native bridge protocol, scheduler/domain protocol and their existing `PROTOCOL_VERSION = 1` stay unchanged. The shared generated `runtime/protocol-v1.json` remains the version-1 contract catalog: its `control_frame.protocol` and `launch_manifest.protocol` explicitly become 2, while `bridge_event.protocol` stays 1. Do not change a global version constant to upgrade unrelated protocols. Both new control and manifest codecs reject protocol 1; an old guard rejects the protocol-2 manifest before registration or repository execution. There is no downgrade retry. CLI and guard still come from the same installation.

Keep four-byte big-endian byte-length framing, UTF-8 strict objects, 65,536-byte/depth-16 control bounds, 4-MiB/depth-32 manifest bounds, and two-second send/partial-frame receive deadlines. A complete frame must have exactly the known keys and types; reject booleans as numbers, non-finite numbers, duplicate JSON keys, trailing data, truncation and unknown fields. Private frames never use public additive-field projection. Raw commands/environment remain only in the inherited manifest pipe. Child processes inherit neither control nor manifest descriptors.

`LaunchManifest` retains its existing fields/signature, with protocol 2. Require distinct, contiguous ordered execution IDs `a001` through `a00N` (`a010` at N=10), matching the prepared attempts. Every execution attempt, including a single ordinary run and the first attempt after setup, requires a decision gate; there is no ungated compound mode. Setup remains separately authorized by the existing declaration and manifest, runs at most once, and a failed/incomplete setup closes the spawn path.

When setup exists, both its `phase` and `runner-facts` messages carry exactly `attempt_id="a001"` and `phase="setup"`. The first execution still uses `attempt_id="a001"`, `phase="execution"`; parsers distinguish the `(phase, attempt_id)` pair. Setup consumes no execution entry, and the first execution's `attempt-ready.previous_attempt_id` remains null after setup.

All control frames have exactly `{protocol:2, run_id:hex32, nonce:hex64, kind, payload}`. The nonce is the grant capability received over the inherited private channel. The guard has already authenticated the exact grant and live guard identity through `register_guard`; operations checks `registered.guard` against the process identity it launched. New gate fields bind the same grant generation and one position in this manifest, and are not independent authority to release or signal processes.

### Frozen messages

Existing messages retain their fields, direction, and bounds under protocol 2: caller → guard `cancel {signal:2|15}` and `parent-closing {}`; guard → caller `registered {guard:ProcessIdentity}`, `phase {phase,attempt_id}`, `runner-facts {attempt_id,phase,raw_exit_code,report_name,problem}`, and `draining {provisional_artifact_id}`.

| Message | Direction | Exact payload |
|---|---|---|
| `attempt-ready` | Guard → operations | `{attempt_id:str, previous_attempt_id:str|null, generation:int, gate_token:hex32, deadline_monotonic:float}` |
| `attempt-decision` | Operations → guard | `{attempt_id:str, generation:int, gate_token:hex32, action:"continue"|"stop", reason:str|null}` |

`generation` must equal `manifest.grant.generation`. `attempt_id` must equal the next unstarted manifest entry. `previous_attempt_id` is null for the first execution attempt and otherwise the immediately preceding execution ID; setup does not consume an execution ID. Each ready event gets a fresh cryptographically random 128-bit `gate_token`. A continue has `reason=null`. A stop requires exactly one of `changed-during-run`, `unknown-input`, `report-invalid`, `incomplete-inventory`, `unsupported-capability`, `execution-timeout`, or `state-unavailable`. User cancellation uses `cancel`, not a fabricated stop reason or synthetic runner failure. Constants and reason enums belong to contracts.

`DEFAULT_ATTEMPT_DECISION_TIMEOUT_S = 30.0` is a controller-decision watchdog, **not** an attempt execution timeout. The guard creates the deadline as `min(now + 30, compound_deadline)` and includes it in `attempt-ready`; the controller cannot extend it. Monotonic time is shared only by these local processes within the current boot. Frame transfer still has its own two-second bound, and partial frames cannot reset either deadline. Operations must finish the existing bounded source capture, report validation and decision within that deadline; if it cannot, it stops. No-data waits remain interruptible by cancellation and the applicable watchdog.

### Ordering and failures

```text
registered
  [setup phase -> setup runner-facts; successful setup only]
  attempt-ready(a001) <- attempt-decision(a001, continue)
  phase(a001) -> runner-facts(a001)
  attempt-ready(a002) <- source/report check -> attempt-decision(a002, continue|stop)
  [phase(a002) -> runner-facts(a002) only after continue]
  ... -> close spawn path -> authenticated mark_draining -> draining -> guard exit
  -> reap/group-and-escape absence proof -> final source check/publication -> release
```

Before emitting terminal facts for setup/an execution and the next ready event, the guard reaps its direct child and performs a bounded observation of its anchored group and recorded escaped descendants. It must observe no live non-guard member or live/unknown recorded escape from the preceding phase. Live survivors, inaccessible identities or an exhausted observation bound close the spawn path: annotate the preceding facts with `ownership-uncertain`, start the existing owned-group cleanup when needed, and emit no next ready event. Do not delegate this guard-side prerequisite solely to operations or trust a bridge's terminal flag as proof. Supported bridge lifecycle also joins/reaps attempt-owned services before terminal completion, and operations independently rejects missing lifecycle/report evidence. This between-attempt observation is not the final group-absence `QuiescenceProof`; the live guard still anchors the group. No capacity is released between attempts.

Operations validates each ready event against the expected phase/facts order and matches the preceding report to its allocated run/nonce/attempt/report binding. For shadow/probe it rechecks current config, source and relevant environment identity against the comparison input immediately before every continue; it also checks signal state immediately before sending. Ordinary single-run callbacks preserve their existing source-evidence requirements: an execution-only command is not newly required to supply selection compatibility, and an initial advanced full run captures inputs before native runtime qualification. The first gate covers queue/setup invalidation.

**First-gate rule:** if source/config/relevant environment identity has changed or become unavailable at `a001`, send stop with `changed-during-run` for observed change or `unknown-input` for unavailable previously required identity. Do not rebuild the plan, replace prepared commands, widen scope, alter resource claims or continue under that grant. Complete the existing drain/absence/finalization/release path, with zero test launches. The exact outcomes are:

| Request at `a001` | Result after orderly stop |
|---|---|
| Ordinary automatic, full or explicit scoped execution | `status=incomplete`, exit `70`, origin `ptest`, reason `changed-during-run` or `unknown-input` as above. |
| Shadow | `status=not-run`, exit `2`, origin `ptest`, primary reason `unsupported-capability` because the admitted selected/full pair is no longer valid; also retain `changed-during-run` or `unknown-input`. |
| Probe | `status=incomplete`, exit `70`, origin `ptest`, reason `changed-during-run` or `unknown-input`; no serial or parallel attempt runs. |

These paths have `runner_exit_code=null`, source_valid=false, full_gate_eligible=false, baseline_published=false and unstarted execution entries marked not-run. Successful setup remains separately recorded; a failed setup never reaches this gate and preserves its own nonzero. A failure to establish guard quiescence retains the existing charged-lease/incomplete rules. Missing selection compatibility that was never required by an execution-only profile is not an invalidation by itself. A replan needs fresh admission with newly resolved config/resources in a new caller invocation; automatic readmission/replanning is outside Task 12. Planning before admission can still resolve the current tree. After any execution attempt has run, changed/unknown inputs stop the compound operation as incomplete `70` unless an earlier nonzero must be preserved.

The guard accepts exactly one correctly authenticated decision while that gate is pending. Continue consumes the token; it never authorizes later attempts. It drains already pending control/signal state and checks the deadline immediately before spawning. Stop permanently closes the spawn path, retains provisional facts and proceeds through ordinary draining; it does not manufacture a SIGTERM runner exit. `draining` without all attempted entries is valid only on a tracked stop/cancellation/timeout branch, never a complete comparison. No synthetic runner-facts frame is invented for an unstarted attempt.

An ordinary nonzero test result does not close the gate. A setup failure, missing executable, unsupported native profile, missing/malformed required report, incomplete inventory, attempt/compound timeout, invalid control frame, source invalidation or cancellation does. Distinguish native test failure from an adapter rejecting the capability before tests.

EOF or `parent-closing` permanently prevents further spawn. An already executing attempt may finish within its deadline after orderly EOF; only provisional facts remain, and no new attempt begins. EOF during a partial frame is a protocol error. Wrong nonce/run/generation/token/attempt, duplicate/replayed/early decisions, wrong-direction events, and out-of-order facts are `protocol-mismatch`: close spawning, cancel active owned work using the existing guard grace path, and publish no passing comparison. A ready gate with no decision by its deadline is `attempt-decision-timeout`; record incomplete, close spawning and drain. A broken send has the same no-next guarantee. Do not accept an unsolicited continue sent before ready.

Once cancellation has been observed by the guard, it wins over any queued continue. A later signal or input edit after a valid decision cannot be made atomic with an operating-system spawn; existing active-work cancellation and final-source verification cover that interval. The required regression is that a completed attempt cannot race ahead of its controller's still-pending validation, or start after an explicit stop. No polling-based assumption replaces the gate.

Keep the existing 3-second cancellation grace, 10-second controlled cancellation target and 30-second proven-gone recovery target. `mark_draining` must succeed and its matching in-band event must be received for normal finalization. Forced group SIGKILL, missing handoff, uncertain descendants and parent death retain the existing incomplete/charged-lease rules. An authenticated decision never supplies a `QuiescenceProof`.

## 3. Shared records, public seams and ownership

New records below are frozen keyword-only internal contracts, not new public JSON payloads. Validate bounded strings, existing hex/ID patterns, exact enums and actual types. Inventories retain the existing record/byte quotas. Public results continue to use the existing allowlisted RunResult/AttemptResult/Reason fields.

```python
# contracts.py
CompoundSupport(selection: bool, parallel_identity: bool,
                profile: str | None, limitations: tuple[Reason, ...])
AttemptEvidence(attempt_id: str, result: AttemptResult,
                inventory: Inventory | None, terminal_complete: bool,
                parallel_identity: bool, runtime_identity: str | None)
SelectionQuarantine(code: Literal["selection-shadow-quarantine"],
                    run_id: str, sequence: int, policy_digest: str,
                    compatibility: str, input_digest: str,
                    verdict: Literal["suspected-miss", "unclassified-divergence"])
ShadowPlans(selected: Plan, full: Plan,
            quarantine: SelectionQuarantine | None)
ShadowComparison(selected: AttemptEvidence | None,
                 full: AttemptEvidence | None,
                 verdict: Literal["matched", "suspected-miss",
                                  "unclassified-divergence", "incomplete"],
                 expected_quarantine: SelectionQuarantine | None)

# Append an internal defaulted field to HistoryView; keep existing fields.
HistoryView.selection_quarantine: SelectionQuarantine | None = None
# Retain the verified native digest used to derive source compatibility.
Baseline.runtime_identity: str | None = None
RunResult.runtime_identity: str | None = None  # internal; never public JSON

# runners.RunnerAdapter; preparation remains unchanged and nonexecuting.
compound_support(config: Config) -> CompoundSupport
prepare(config: Config, plan: Plan, grant: Grant,
        attempt: AttemptIdentity) -> PreparedRun

# reports.py; the existing basic-terminal API remains supported.
consume_attempt_report(binding: NativeReportBinding) -> AttemptEvidence

# selection.py; both functions are pure, never launch/discover/write.
choose_plan(config: Config, snapshot: InputSnapshot,
            history: HistoryView, request: RunRequest) -> Plan
choose_shadow_plans(config: Config, snapshot: InputSnapshot,
                    history: HistoryView, request: RunRequest,
                    support: CompoundSupport) -> ShadowPlans

# history.py: the public, code-specific selection-quarantine publication API.
publish_shadow_outcome(domain: DomainPaths, checkout: CheckoutIdentity,
                       result: RunResult,
                       comparison: ShadowComparison) -> PublishResult
publish_probe_outcome(domain: DomainPaths, checkout: CheckoutIdentity,
                      result: RunResult,
                      evidence: tuple[AttemptEvidence, ...]) -> PublishResult
# Existing read_history/publish_outcome signatures stay unchanged.

# guard.py / operations.py / cli.py: public entrypoints stay unchanged.
run_guard(control_fd: int, manifest_fd: int) -> int
execute(domain: DomainPaths, config: Config, request: RunRequest) -> RunResult
main(argv: Sequence[str] | None = None) -> int

# source.py: consume the existing verified-runtime keyword, not a guessed tuple.
snapshot(domain: DomainPaths, config: Config, baseline: Baseline | None,
         base: str | None, *, runtime_identity: str | None = None,
         pytest_full_outputs: bool = False) -> InputSnapshot
```

`CompoundSupport` is a declaration from the closed adapter registry and qualified profile catalog, not evidence supplied by repository configuration. `profile` names the exact approved tuple/configuration class; null/false is unavailable. Static support never executes an interpreter or native configuration. The bridge must revalidate actual installed versions, effective controls, inventory and worker identities after admission; a catalog declaration alone cannot certify an attempt. `parallel_identity=true` requires tested distinct worker identity in addition to bounded concurrency. Neither `execution=advanced` nor runner kind alone implies it. Ordinary public `Capability.selection` must also require advanced qualification.

`consume_attempt_report` binds native terminal, complete inventory/outcomes and identity evidence to one `NativeReportBinding`; it rejects another attempt's report, partial events, duplicates with conflicting identities, unsafe report names and quota violations. It extends the existing reports module and generated bridge descriptors for the already-approved inventory/test-outcome/terminal event contract. It does not infer IDs from stdout or require operations to parse private event dictionaries. The existing basic-serial terminal path remains execution-only.

The advanced bridge records a 64-hex digest of the actually observed interpreter/runner, approved critical plugins/hooks, instrumentation, effective configuration/coverage/dependency identities and platform before executing tests, and verifies the same identity at terminal completion. `AttemptEvidence.runtime_identity` is present only when those bound observations agree. The tuple name or a caller-chosen constant is not that digest. Store the verified digest in the internal RunResult and successful full Baseline; history's bounded private serializer/schema validation must include it. Older baselines without it cannot qualify selection. The additional RunResult field is excluded from public serialization, including history summaries; the prior four internal fields remain excluded too.

Normalize only declared ptest-owned differences between attempts: selected/full scope, granted worker settings, generated report paths and run/attempt/worker identities. Bind those independently in each report. The runtime digest still includes the configured whole-gate coverage policy and both configured command variants; ordinary declared mode differences cannot make every shadow/probe incompatible. Never normalize changed user configuration, dependency/plugin versions or relevant environment inputs.

For an initial advanced full run, retain the original bounded input snapshot before execution. After native evidence is validated, recapture with the verified runtime digest; only an equal non-null input digest, unchanged config/environment and valid initial/terminal runtime observations allow operations to bind that compatibility to the saved pre-execution snapshot. Never replace its original contents/head/clean state with a later tree. A changed/unknown digest remains incomplete. A subsequent selected plan may use the baseline's previously verified runtime digest with current bounded fingerprints, but the bridge must compare the expected digest with current native observations before executing tests and reject a mismatch. Queue/setup changes invalidate that expectation. Shadow compares the observed runtime identity in both reports as well as input snapshots; a mismatch is incomplete, not a miss. This supplies the missing real baseline prerequisite without unscheduled native probing or a shadow-created baseline. Advanced source capture does not enable the execution-only `pytest_full_outputs` shortcut; normal approved output declarations must be valid.

| Owner/phase | Files and responsibilities |
|---|---|
| T12a contracts | `src/ptest/contracts.py`: protocol-2 codecs/descriptors, new records/constants/reason codes, HistoryView field. `src/ptest/runtime/protocol-v1.json`: generated descriptor only. No public schema change is needed for these internal types/reasons. |
| T12a guard/control | `src/ptest/guard.py`: gate state, authentication, deadlines and permanent stop. `src/ptest/operations.py`: minimally adapt the single shared transport/state machine and ordinary-run decision callback; preserve execution behavior. |
| T12a history | `src/ptest/history.py`: dedicated quarantine row, typed publication/read integration, atomic obligations and comparison receipt. `src/ptest/selection.py`: quarantine-aware pure diagnostic plan; ordinary fallback unchanged. |
| T12a tests | `tests/ng/test_contracts.py`, `tests/ng/test_guard.py`, `tests/ng/test_operations.py`, `tests/ng/test_history.py`, `tests/ng/test_selection.py`, `tests/ng/fixtures/processes/guard_client.py`, `guard_driver.py`, `guard_workload.py`: protocol callers, lifecycle barriers, quarantine and planning tests. |
| T12b qualification | `src/ptest/adapters/pytest.py`, `src/ptest/runtime/pytest_bridge.py`, `src/ptest/adapters/vitest.py`, `src/ptest/runtime/vitest_bridge.mjs`, `src/ptest/runners.py`, `src/ptest/reports.py`: implement advanced inventory/selection, report consumption and supported worker identity. Sequential ownership of contracts/generated bridge descriptor only for the frozen report/support records. |
| T12b single-attempt integration | `src/ptest/operations.py`, `src/ptest/cli.py` (capability summaries/ordinary routes only), `src/ptest/history.py`, `src/ptest/selection.py`: wire advanced explicit/full/automatic execution, observed runtime identity, genuine baseline publication and real selected plans before compound integration. Consume source.snapshot's existing keyword API; do not manufacture compatibility. |
| T12b tests/evidence | `tests/ng/test_pytest_adapter.py`, `test_vitest_adapter.py`, `test_reports.py`, `test_pytest_scoped_subprocess.py`, `test_pytest_full_subprocess.py`; new `tests/ng/test_compound_profiles.py`; existing `tests/ng/fixtures/pytest/` and `tests/ng/fixtures/vitest/` miniature projects and matrix documentation. Fixture dependency changes only under those directories; root dependency changes require a separate explicit owner transfer. |
| T12c integration | `src/ptest/operations.py`, `src/ptest/cli.py`: plan/admit/gates/evidence/result/publication through one executor, exact grammar and diagnostic output. `src/ptest/selection.py` and `src/ptest/history.py` only fixes within the frozen APIs, with their scoped regressions. |
| T12c tests | `tests/ng/test_probes.py`, `tests/ng/test_shadow.py`, `tests/ng/test_cli.py`, `tests/ng/test_operations.py`; new local fixture directories `tests/ng/fixtures/probes/` and `tests/ng/fixtures/shadow/`. No shared conftest/support edits or command-selector substitution. |
| Reports | `.pipeline/out/task-12a.json`, `task-12b.json`, `task-12c.json`; final `.pipeline/out/task-12.json` records every prerequisite and qualified/unqualified tuple. |

The scheduler API/ledger, config grammar, source snapshot API, shared filesystem primitives, public renderers, packaging and installed CLI are not assigned new behavior. Use `scheduler.cancel_pending(domain, ticket, owner)` for rejected QUEUED/GRANTED work. After guard registration, use authenticated control cancellation, guard drainage and the existing proof/finalization path. A failed cancel CAS is not release evidence; report `75`/`ownership-uncertain`, retain charge and do not launch more work. No direct lease deletion or early return abandoning a grant is allowed.

## 4. Genuine selectable plans and qualification gates

Shadow requires all of: an initialized pytest/Vitest project; a qualified advanced selection profile; enabled, valid closed-input policy; a compatible complete full baseline; trustworthy source/config/environment identities; accounted-for mandatory failures; and the ordinary selector yielding a nonempty `execution="selected"` set strictly smaller than full and below the existing full-ratio threshold. Selected files must be validated exact files from native inventory, with all current cases executed and stable reason evidence. `full` is the actual configured whole gate with coverage and supported reporters intact. `scoped`, `full`, `none`, arbitrary different argv and two identical full commands are not a selected/full comparison.

`choose_shadow_plans` uses the same mapping/union/failure logic as `choose_plan`, factored once. It may ignore only the typed `selection-shadow-quarantine` block for an explicit diagnostic comparison. Every other history health flag/limitation, missing baseline, full-gate obligation, input uncertainty and ordinary full-trigger still applies. Read history exposes all concurrent disable reasons, not just the first marker. If a disable cannot be attributed exclusively to the returned quarantine record, refuse the diagnostic bypass. Configuration disabling selection is never bypassed.

Before T12b qualification, command, Go, Cargo, basic-serial pytest and deferred/basic-serial Vitest all return `2`/`unsupported-capability` for shadow. A structurally valid shadow request with no genuine selected plan also returns that code, with a safe explanation and `ptest --full` guidance. It launches no setup/test/guard when detectable before admission. It does not silently execute full in the name of shadow. Plain automatic fallback remains full for an executable profile with uncertain selection; this amendment does not claim previously unavailable native execution is already wired.

Grammar accepts `ptest --shadow`, `ptest --changed --shadow`, `ptest --shadow --changed`, and `ptest changed --shadow` as automatic shadow requests. Full or explicit runner tails combined with shadow return `2`/`invalid-config`; normal prefix-only forwarding remains unchanged. Grammar acceptance and runtime capability acceptance are separate tests. Until qualified, a valid combination reaches the unsupported-capability result, not a parser rejection.

Named qualification prerequisites are **Q-PY-SELECT**, **Q-PY-PROBE**, **Q-VT-SELECT**, and **Q-VT-PROBE**. T12b must implement and verify them using the exact advanced pytest/xdist/coverage and Vitest/coverage/Node tuple policies already frozen in design §3 and §8. SELECT means native complete inventory, exact file selection, preserved coverage/reporters, effective worker bounds, compatible baseline publication and adverse report/config cases pass. PROBE additionally means native parallel workers receive distinct supported run/attempt/worker identities and namespaced mutable resources in a real subprocess test. Pytest serial selection does not require xdist; its parallel probe qualification does. An unavailable Vitest worker-identity hook or unqualified version cannot be replaced by a shared `w000` fallback.

Qualification is per profile and platform evidence, not a flag inferred from installing a package. At baseline none of these four capabilities is qualified. T12b reports each distinctly. T12c can verify an available profile while another is being completed, but Task 12/M3 cannot be marked complete with an omitted first-class profile or absent positive probe evidence. Missing external test infrastructure is reported as an unmet gate; it does not change the exit contract or product scope.

## 5. History-owned selection quarantine

Add one bounded `selection_quarantine` table/row per checkout inside the existing history store, separate from health/corruption/capacity/publication markers and failure obligations. It carries exactly `SelectionQuarantine`; the code is always `selection-shadow-quarantine`. Retain it across policy/config changes until the recovery contract succeeds. The existing history writer lock and SQLite transaction protect its update. A recognized older NG history store may acquire this additive table only under that writer lock after existing schema validation; malformed/unknown stores remain disabled. Count the row and retained comparison evidence toward existing quotas. No second database, arbitrary marker path, or marker-clear API is introduced.

`publish_shadow_outcome` is the sole public quarantine mutation entrypoint for this task. It takes the real `Mode.SHADOW` result and typed comparison, validates their run/checkout/sequence/source/policy bindings, and atomically publishes the compound summary, per-attempt inventory evidence, union of failure obligations, comparison receipt and quarantine transition. Operations never calls `_write_disabled_marker`, `_remove_marker`, `_history_directory`, reads marker constants, converts shadow to `Mode.FULL`, or passes an unrestricted `reenable=True` flag. History rechecks comparison invariants; it does not accept a caller's verdict as permission by itself.

Failure obligations from **any** attempt in this run survive a later passing attempt in the same compound operation. Within one admitted sequence, insert the union of observed failures before applying permitted reconciliation of older obligations; no same-run pass erases those new failures. A required incomplete comparison records a whole-gate obligation when exact failures cannot be reconciled. A real full pass in a later independently admitted run follows the existing reconciliation rules. Store all observed attempts; unstarted attempts are `not-run` with null raw/final codes and no invented times or inventory.

`publish_probe_outcome` uses that same internal compound transaction for real `Mode.PROBE` results and ordered bound evidence, but has no quarantine transition or baseline authority. It is not implemented by merging all attempts into a fabricated single passing inventory. Both public APIs share history's existing writer/recovery/retention machinery; no second marker manager or outcome store is permitted.

`read_history` returns the typed quarantine and includes its stable reason in limitations, while preserving all unrelated health limitations. `selection_disabled` is the aggregate OR, never reassigned to false just because this row was removed. Plain automatic mode remains full while it exists. Ordinary passing runs, a new baseline, timeout, history age/pruning, or changing policy never remove it. Ordinary valid full publication may produce a compatible clean baseline while this row remains; it must not implicitly clear the row or promote automatic narrowing.

Clearing requires a newer explicit shadow whose two attempts conclusively pass on unchanged inputs, whose selected plan was genuinely eligible under §4, and whose valid compatible baseline was captured after a correction. Define correction concretely: current `policy_digest` **or** `compatibility` differs from the quarantined value, and a later clean full baseline matches both current values. An unrelated source edit or rerunning the same policy/environment does not count. The exact `expected_quarantine` tuple returned during planning must still equal the current row and the new sequence must be greater; stale/mismatched recovery cannot clear a newer quarantine. A fresh compatible baseline is normally obtained with explicit `--full` after the correction, then a real narrow change supplies the comparison. No synthetic baseline or diagnostic bypass of unrelated full obligations is allowed.

Removing that exact row, storing the successful comparison and publishing its outcome are one transaction. It never unlinks/resets `history-disabled.json`, capacity/publication markers, generic disabled metadata, unrelated obligations or another checkout's row. If unrelated health blocks publication, return `committed=false`, `selection_disabled=true` and its reasons; leave the quarantine untouched. A required persistence failure is visible: first runner failure remains the exit code, otherwise return incomplete `70`; never swallow it. Existing publication-uncertainty machinery must retain a fail-closed indication on an interrupted write. Failure to persist that indication is itself reported as unavailable state, never a successful quarantine or re-enable claim.

## 6. Shadow outcomes and time budget

One lease, one guard, one fixed comparison input identity, selected `a001` then full `a002`, separate report bindings and attempt resource prefixes. Snapshot after queue/setup, between attempts and after proven group absence; changed native output exemptions remain exactly the approved non-input-output contract. Setup, native discovery/collection, decision waits and both executions share the guard's 600-second compound bound. Shadow has `attempt_timeout_s=None`; the 30-second probe default does not apply. Queue time has its separate configured deadline. Cancellation/drain/final publication retain their existing bounded obligations and never authorize work beyond the compound deadline.

| Observed result | Next attempt and final meaning | Quarantine action |
|---|---|---|
| Selected pass; full pass; conclusive stable evidence | Execute both; exit 0, comparison matched. Full-gate eligibility additionally requires the complete actual full gate and final valid identity. | Retain existing row unless the exact correction/recovery conditions pass. |
| Selected ordinary test failure; full pass or fail | Full still executes; preserve the selected nonzero, both attempts and all failures. | A selected failure absent/different in full is unclassified divergence; matching failures alone are not a miss. |
| Conclusive full failing test absent from selected files, whether selected passed or failed | Suspected selector miss; preserve first nonzero and exact missed identities in retained evidence. | Atomically set/retain quarantine. |
| Both attempts conclusive but overlapping outcomes disagree, or a full-only gate failure lacks attributable test IDs | Unclassified divergence; do not call it a proven dependency miss. | Atomically set/retain quarantine. |
| Timeout, incomplete/invalid report, missing planned cases, missing handoff, or identity becoming unavailable after tests start | Incomplete; no next attempt. A timed-out full run is not a confirmed or suspected omitted-test miss without conclusive matching evidence. | No new divergence classification; retain existing quarantine and required failure/full-gate obligations. |
| Source/config/environment invalidated at first gate, before tests | Stop without same-grant replan; not-run, `2`/`unsupported-capability`, plus `changed-during-run` or `unknown-input`. | No comparison or clearing; both execution entries remain not-run. |
| Source/config/environment invalidated after an attempt | Stop at the gate, record `changed-during-run`, source_valid=false and incomplete. | No comparison promotion or clearing. |
| User cancellation | Stop/cancel active work; later entries remain not-run, result cancelled. | No clearing; retain already observed failures. |

Raw per-attempt exit codes are unmodified. Negative subprocess codes map to `128+signal` for final codes. Aggregate `runner_exit_code` is the raw code of the earliest failing runner, otherwise the last observed runner code, or null if none ran. The overall exit is the first nonzero setup/runner/attempt failure in execution order; later pass, invalidation or publication failure cannot replace it. With no earlier failure, user cancellation is `128+signal`; required incompleteness is `70`; pre-execution unsupported capability is `2`. A kill initiated solely to enforce a ptest timeout has incomplete status and ptest-origin `70` absent an earlier failure; retain the killed child's raw signal code separately. Failed comparison/incomplete/cancelled results have full_gate_eligible=false and baseline_published=false.

This paragraph explicitly supersedes design §9's focused-reproduction clause: Task 12 supplies no built-in replay/classifier or `confirmed-miss` transition. Explicit focused reproduction is a separate caller-requested ordinary scoped ptest run using the retained failing identity and input, with every attempt visible. Its recorded outcomes let the caller assess reproducibility; exit-code contrast alone never labels a confirmed miss. The bad-policy sensitivity fixture proves reproducibility in controlled acceptance evidence; product quarantine remains conservative even if the caller does not reproduce. Shadow itself has no hidden reproduction/retry phase. Shadow results do not seed baselines; the existing successful clean `Mode.FULL` publication path remains the sole baseline writer.

## 7. Probe prerequisites, sequence and results

Probe is executable only when Q-PY-PROBE or Q-VT-PROBE is qualified, the requested scope resolves without symlinks/traversal to a concrete test file or directory within declared test roots, and `resources.probe_isolation="run-worker-namespaced"`. The declaration identifies an owned local test target and fixture cleanup contract; it is not proof arbitrary tests use the namespace. No remote target discovery, service provisioning, credentials, migrations or shared destructive cleanup is added. Existing declared setup may execute once under the same lease with its displayed network/lifecycle implications and `--no-setup` behavior; setup is not blanket-forbidden to make the probe check pass.

A probe does not require enabled selection or a full baseline. Its first gate captures bounded input/config/environment identity; the first serial report establishes verified runtime identity using the same initial/terminal observation rule as an initial full run. Later attempts must match both. Unknown content identity or missing runtime observations stop the comparison; no synthetic baseline is created to enable a probe.

Grammar: `doctor --probe --scope REL_TEST [--workers N] [--repeat N] [--attempt-timeout SECONDS] [--no-setup] [--result-json REL_FILE]`. Scope is mandatory and positional scope is a usage error. Probe-only options without `--probe`, static scan-budget options with `--probe`, and `--json`/`--prompt` with execution are `2`/`invalid-config`; parse independent of option order. `--attempt-timeout` accepts finite **1–120** seconds inclusive, default **30**; repeat is **1–5**, default **2**; worker request is **1–64**, default **2**. Numeric invalidity returns `2`/`invalid-config` without echoing values.

Validation precedence: grammar/numeric bounds → safe in-root scope → declared isolation → qualified profile → feasible worker ceiling → admission and actual grant validation. Undeclared isolation returns `2`/`probe-isolation-required`. Unqualified profiles return `2`/`unsupported-capability` with the named missing identity capability. If the requested/repo/machine/memory-constrained worker ceiling or actual grant is below 2, return `2`/`capacity-exceeded` with the parallel portion explicitly blocked; run no serial-only success substitute. Busy capacity that can become available is queued under normal FIFO, not mistaken for impossible capacity. Workers are capped by the actual grant and repository/machine ceilings; a request above the ceiling is capped, not rejected merely for requesting more. If a grant is reduced below 2, cancel it through the authenticated prelaunch path. Queue expiry remains `75`/`queue-timeout`.

Each repetition is serial then parallel: `a001(serial=1)`, `a002(parallel=granted)`, `a003(serial=1)`, `a004(parallel=granted)`, up to ten attempts. Reserve the parallel maximum for the entire lease; serial execution must still observe native worker controls at 1. The same explicit scope and fixed input/config/environment identity apply throughout. Each attempt has its own authenticated report and `PTEST_ATTEMPT_ID`, with `PTEST_PROJECT_ID`, `PTEST_CHECKOUT_ID`, and `PTEST_RUN_ID` bound to this run. Serial uses `w000`; parallel workers receive distinct `w000`–`w063` and their own `pt_<checkout8>_<run32>_<attempt4>_<worker4>` prefixes through supported hooks. Stale inherited identities are overwritten. A shared fallback identity is a capability error, not successful isolation.

The manifest's attempt timeout is the validated caller value, and compound timeout is 600 seconds beginning at registration before setup. Every attempt gets the smaller of its own remaining deadline and the compound remainder; it cannot extend the compound bound. At either expiry cancel owned work, retain observed facts and mark remaining entries not-run. A first nonzero test failure does **not** end an otherwise complete, safe scheduled comparison: continue the explicitly requested finite repetitions and preserve that first failure. Cancellation, parent EOF/closing, gate/protocol failure, missing executable, setup/profile rejection, incomplete evidence, changed/unknown inputs and timeout prohibit every later attempt. Never increase repetitions until a pass appears.

A complete passing sample reports `probe-no-conflict-observed`, not certified safety. A serial pass followed by a reproducible parallel failure in the ownership fixture reports `probe-conflict-observed`; arbitrary failures without that differential evidence are failed/inconclusive, not automatically an isolation diagnosis. Timings are per attempt; do not assign total compound elapsed time to every attempt. Probe cannot publish a baseline, grant full-gate eligibility, clear selection quarantine, or use a later pass to erase a same-compound failure obligation. Required outcome history must preserve the compound union of failures.

## 8. Implementation sequence and acceptance

T12a is medium effort and independently reviewable: implement strict protocol-2 records/codecs and tests, the guard waiting state and normal-run controller compatibility, then atomic history/quarantine and the pure planning seam. T12b is the largest prerequisite: qualify the two existing first-class native profiles, wire their ordinary single-attempt execution/baseline path, and report each SELECT/PROBE capability separately. Its real parallel scoped fixtures establish probe eligibility without claiming that `doctor --probe` already executes. T12c composes those verified seams, parses the full existing grammar and records real outcomes. Each phase begins with the named negative behavior failing through scoped `ptest`, then its positive/regression cases; no phase invents tests that merely mirror its implementation.

| ID / owner | Positive acceptance | Required negative twin |
|---|---|---|
| G1 / T12a | Two guarded synthetic attempts use ordered ready/decision/facts and distinct tokens; custom exit 23 followed by 0/1 preserves 23 at the guard/controller layer. Setup facts use `(setup,a001)` before `(execution,a001)`. | Hold a decision with a pipe barrier: its child marker cannot appear. Invalidate at a001 and assert the exact ordinary/shadow/probe outcomes, no same-grant replan and zero launches. This custom-code case is not native selector qualification. |
| G2 / T12a | Continue with exact run/nonce/generation/token starts only the named attempt once. | Wrong/old protocol, nonce, run, generation, token, duplicate/early/replayed decision, unknown field, oversized/deep/truncated frame, wrong direction/order all prevent the next launch. |
| G3 / T12a | Cooperative cancellation drains and proven absence releases within existing fixture targets; next ready follows guard-side observation of no preceding-phase work. | A direct child that exits while a cooperative descendant remains live/unknown, observation-limit exhaustion, EOF, parent-only SIGKILL, SIGINT/SIGTERM while waiting, incomplete frame, controller stall and gate deadline never start the next entry or release live/uncertain work. Neighbor group sentinel survives. |
| H1 / T12a | A conclusive omitted-test comparison atomically stores both attempts, obligations and the specific quarantine row; ordinary auto becomes full. | Interrupted commit, disk/quota failure and malformed evidence cannot leave an acknowledged successful publication or silently clear the row. |
| H2 / T12a | Corrected compatibility/policy, newer clean full baseline and successful eligible shadow clear only the exact expected quarantine. | Older sequence, changed expected row, same policy+compatibility, ordinary pass, timeout, another checkout and simultaneous corruption/capacity/publication markers cannot clear it. Preserve all unrelated marker bytes. |
| Q1 / T12b | Each SELECT profile runs real full baseline then a genuine strict file subset; native IDs, parametrized/duplicate cases, coverage and reporters survive. | Basic serial/command/native non-selector, missing report/test, untested version/plugin, dynamic influence, coverage failure and incompatible evidence cannot become selectable. |
| Q2 / T12b | Each PROBE profile reports distinct parallel worker identities and namespaced local SQLite/file/socket or in-memory cache resources; reusable initialization and per-test reset are observed. | Shared identity, excessive config/env workers, inherited stale IDs, unsafe scope and neighbor-owned resources cannot gain execution/isolation authority. |
| S1 / T12c | A real selected native test failure has complete authenticated terminal/inventory evidence and exit 1; full `a002` then exits 0 or 1. Both execute and final remains 1. | No adapter/collection/refusal result substituted for the test failure, no stop solely on ordinary selected failure, no later green exit, and no same-run clearing of its failure obligation. |
| S2 / T12c | Real bad-policy fixture below passes selected and fails full, records suspected miss and disables narrowing. | No command-profile simulation, no deliberate child exit argument in lieu of a failing omitted test, no injected prebuilt selected plan in the end-to-end case. |
| S3 / T12c | Stable two-pass shadow records matching inventories and qualified gate outcome; fake clock can exceed 30 seconds within compound budget. | Source edit at gate, source edit during full, incomplete full, 600-second expiry, missing handoff and timeout never clear quarantine or become a confirmed miss. |
| P1 / T12c | Defaults yield four serial/parallel attempts; repeat 1 and 5 yield two and ten. Accept attempt limits 1, 30, 31 and 120 seconds. | Reject 0, negative, >120, NaN, infinity, missing scope and undeclared isolation; all before launch. |
| P2 / T12c | Barrier-controlled local bad ownership passes serial and fails parallel; corrected fixture shows no conflict and preserves neighboring resources. | Timeout/EOF/cancellation/invalidation prevents next attempt; ordinary test failure survives all remaining planned observations. No external services. |
| P3 / T12c | Busy adequate capacity queues and later grants; worker request is capped and serial controls stay at 1. | Effective/granted capacity below 2 is blocked, with zero launches and no leaked queued/granted lease. |
| C1 / T12c | All four automatic shadow spellings parse, then either execute a qualified pair or fail with the exact runtime code. | Shadow+full/scope is invalid; probe and static flags cannot cross modes. Every rejected post-admission preparation proves authenticated cancellation or retained uncertainty. |
| C2 / T12c | Result/history retain attempts, null unknown timings/counts, raw codes, redacted summaries and real reasons. | No private marker calls, raw argv/env disclosure, copied compound timings, fake baseline, swallowed publication error or passing not-run attempt. |

The **S2 sensitivity fixture** is a small committed Git pytest/Vitest project with exactly four test files, alpha/beta/gamma/delta, and explicit `full_ratio=0.75`. A reviewed, syntactically valid but intentionally wrong mapping links `src/shared` only to alpha; beta also consumes it. Gamma/delta belong to independent source groups. Start from a real complete passing full baseline under that same bad mapping, then change only `src/shared`: alpha's assertions remain true, beta's real assertion fails. Assert the full inventory has four distinct files and selected has exactly alpha: `1/4 < 0.75`, with no always/full trigger making selection effectively full. Assert selected executes/passes, full executes/fails beta, `selection-shadow-quarantine` persists, and the next plain automatic plan widens to full. Repeat omitted beta through an explicit scoped ptest run to demonstrate fixture reproducibility. Never edit policy between baseline and failing comparison, since that must correctly force full.

For recovery, correct the mapping to include both alpha and beta, fix the failing source, commit, and obtain a new clean full baseline while quarantine remains. Make a benign narrow `src/shared` change and assert the selected set is exactly alpha+beta: `2/4 < 0.75`. Both shadow attempts pass; the exact old selector row clears and unrelated injected health markers in a separate negative case do not. The source fix alone without mapping/compatibility correction must fail the recovery predicate. A separate selector boundary test with those same four inventory files, two required selected files and `full_ratio=0.50` must choose actual full because equality widens; it is not the recovery case. Perform the sensitivity/recovery with each profile advertised as selectable.

Use deterministic pipe/socket barriers and fake monotonic clocks for 30/120/600-second policy bounds. Real small subprocess watchdogs are explicit and include the exercised cleanup interval; do not spend minutes sleeping. Invoke tests with the candidate `./.venv/bin/ptest` scoped to their owned files after the existing self-hosting gate. Do not call raw runners or fall back to the legacy bootstrap. Record every red/green attempt and its result, run `git diff --check`, refresh graphify only for source changes, and commit only owned files. The orchestrator owns the single final integrated `ptest --full` after all reviewed integration/repairs.

## 9. Risks, scope and completion truth

| Risk | Mitigation / evidence |
|---|---|
| Compound controller and guard disagree on state | One strict contract barrier, protocol separation, explicit pending-gate state and adverse frame tests. |
| Capability catalog outruns runtime evidence | Separate named qualification gates; observed bridge revalidation; exact exit-2 behavior until promoted. |
| Quarantine recovery removes broader safety state | Typed expected-row compare-and-swap, transactional publication and byte-preservation tests for unrelated markers. |
| Report/capability work expands T12 unnoticed | T12b owns it explicitly; original T5/T6 tuple constraints remain authoritative. Missing qualification blocks M3 instead of being hidden in the completion report. |
| Faults appear as selector misses | Classify only conclusive comparable evidence; keep timeout/invalidation incomplete and focused reproduction explicit. |

There is no unresolved product choice preventing this amendment. Opus reviewed the new protocol, watchdog, quarantine/API and ownership sequence with no HIGH blockers; the controller verifies these requested documentation repairs before builders consume them. Availability of the exact native profile/platform evidence is an implementation gate, not an ambiguity to paper over. No remote execution, model call, deployment, live installation, publication or source upload is authorized.

This amendment is complete when its three documents parse/read consistently and the isolated documentation commit is available. Task 12 implementation is complete only after T12a–T12c, all four named qualification prerequisites, the genuine bad-policy sensitivity cases, positive probe observations and the integrated review/test gates have evidence. A rejection-only implementation is never M3 completion.
