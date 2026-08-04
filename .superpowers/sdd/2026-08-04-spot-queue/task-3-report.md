# Task 3 report — Spot worker, controller, and static infrastructure

## Status

Complete. Added a one-suite worker primitive that publishes durable results
before acknowledgement and leaves preempted messages unacknowledged, a pure
controller scaling policy/reconcile adapter, Spot worker/controller images,
and opt-in static Terraform resources for Pub/Sub, IAM, a regional Spot MIG,
health check, controller service, and scheduler. No Terraform apply, plan, or
deployment action was run.

## Test evidence

- RED: `ptest tests/test_spot_worker.py tests/test_spot_controller.py`
  initially failed collection because the worker/controller modules were absent.
- GREEN: `ptest tests/test_spot_worker.py tests/test_spot_controller.py
  tests/test_spot_queue.py` passed 27 tests in 0.34s.
- GREEN: `ptest --full` passed 163 tests in 1.74s.
- GREEN: `terraform -chdir=terraform fmt -check` and
  `terraform -chdir=terraform validate` passed after backend-free provider
  initialization; no Terraform plan or apply was run.
- GREEN: `python -m py_compile ptest spot_queue.py spot_worker.py
  spot_controller.py` succeeded.

## Files

- `spot_worker.py`, `spot_controller.py`
- `Dockerfile.spot-worker`, `Dockerfile.spot-controller`
- `terraform/spot_queue.tf`, `terraform/variables.tf`, `terraform/outputs.tf`
- `tests/test_spot_worker.py`, `tests/test_spot_controller.py`
- `README.md`

## Commit

- `1f36324 feat: add spot queue worker infrastructure`

## Concerns

Terraform is intentionally static and opt-in; the manual-only procedure is in
the README. No deployed or user configuration was changed.

## Review fix round 1

### Remediation

- Replaced the primitive-only worker with a runnable `main()` and authenticated
  Gcloud adapter: pull one Pub/Sub message, read terminal state, take a
  generation-safe durable lease, renew its generation while running, publish a
  durable result before acknowledgement, and leave unfinished work unacked on
  SIGTERM. Its health endpoint serves on port 8080.
- Added a runnable Cloud Run controller `main()` and authenticated `/reconcile`
  endpoint. Cloud Run IAM validates the OIDC caller; the handler additionally
  rejects absent bearer tokens. The controller reads queue/MIG metrics, treats
  active leases as a non-negotiable floor, and separates observed idle age from
  the configured idle timeout. Overflow is explicit/observable only when the
  opt-in flag is set.
- Added generation-safe lease renewal tests and worker/controller policy tests.
  Child suites run through Bubblewrap as UID/GID 65534 with a new network
  namespace, stripped queue/GCP environment, an 1800-second default timeout,
  and process-group cleanup. The worker image includes Python, Node, Vitest,
  gcloud, Bubblewrap, and Tini.
- Tightened Terraform to subscription/topic-level Pub/Sub grants, repository
  image-reader access, a narrow custom MIG controller role, lease-read and
  monitoring-view access, explicit operator publisher grant, controller
  invoker grant, worker health firewall, Docker installation/registry auth,
  and complete queue configuration injection. Terraform ignores controller-
  owned MIG `target_size` drift.
- Rewrote the README’s Spot section with exact manual build, static validation,
  reviewed-plan, and post-approval smoke commands. It still never applies.

### Evidence

- RED: `ptest tests/test_spot_worker.py tests/test_spot_controller.py` — 2
  expected policy failures before implementing lease-floor and separate-timeout
  behavior.
- GREEN: `ptest tests/test_spot_queue.py tests/test_spot_worker.py
  tests/test_spot_controller.py` — 31 passed in 0.39s.
- GREEN: `terraform -chdir=terraform fmt -check` and
  `terraform -chdir=terraform validate` — valid; no plan/apply/deploy ran.
- GREEN: `ptest --full` — 167 passed in 1.66s.

### Remaining operational concern

This repository cannot prove GCP IAM, Bubblewrap kernel support, Artifact
Registry image pulls, or live Cloud Monitoring metric shape without a dedicated
project. The README’s manual smoke procedure is the required pre-enable gate;
no remote mutation was performed here.

## Review fix round 2

### Remediation

- Replaced unsupported Pub/Sub invocations with the installed, supported
  `subscriptions ack` and `subscriptions modify-message-ack-deadline` commands.
  Replaced the unsupported Monitoring gcloud alias with the documented Cloud
  Monitoring v3 REST `timeSeries` API authenticated by `gcloud auth
  print-access-token`.
- SIGTERM now stops the heartbeat and process group; an interrupted run returns
  no terminal result and performs no acknowledgement, so the durable lease and
  Pub/Sub delivery can redeliver it. Preemption can no longer be reported as an
  infrastructure failure.
- Replaced raw `tar -xzf` with controlled validation/extraction: traversal,
  absolute names, special files, hard links, and escaping symlinks are rejected;
  extracted source and installed dependencies are mode-normalized for the
  unprivileged Bubblewrap child. Lockfile-pinned Node dependencies are
  provisioned script-free before Vitest runs with no network.
- Defined exact safe overflow behavior instead of logging: because an existing
  Cloud Run Job lacks a queue-request source/command dispatch contract, excess
  work remains durable in Pub/Sub and ptest uses its configured timeout fallback.
  It never launches a malformed job.
- Removed the controller’s unnecessary publisher grant and granted every
  documented `operator_members` principal `run.invoker` on the controller.
  Added explicit Cloud Build configs and corrected README commands/smoke steps.

### Evidence

- RED: `ptest tests/test_spot_worker.py tests/test_spot_controller.py` exposed
  absent archive/Monitoring boundaries and the old overflow handler.
- GREEN: `ptest tests/test_spot_queue.py tests/test_spot_worker.py
  tests/test_spot_controller.py` — 36 passed in 0.45s.
- GREEN: installed command help confirmed `gcloud pubsub subscriptions ack`,
  `modify-message-ack-deadline`, `gcloud auth print-access-token`, and `gcloud
  builds submit`; no cloud command was executed against a project.
- GREEN: `ptest --full` — 172 passed in 1.80s; Terraform `fmt -check` and
  `validate` passed. No plan/apply/deploy/config mutation ran.

### Remaining operational gate

Docker is not installed in this checkout, and a local process cannot verify the
dedicated project’s IAM, Artifact Registry pulls, Cloud Monitoring data shape,
or Docker/Bubblewrap kernel policy. The README now makes queue submission,
result/ack inspection, preemption/redelivery, and Bubblewrap validation explicit
post-approval smoke requirements before enabling the backend.

## Review fix round 3

### Remediation

- Heartbeat renewal/deadline exceptions now transition the worker to stopping,
  terminate the child process group, suppress terminal publication and ack, and
  leave its lease/message retryable. A durable cancellation is checked both
  before claiming and on heartbeats, so timeout fallback stops a still-running
  remote child rather than racing a later local execution.
- Added generation-aware completed-lease release plus durable worker heartbeat
  records containing actual `idle_since`, heartbeat time, and active request.
  The controller consumes worker state for idle age rather than VM uptime. Its
  target is capped even under inconsistent lease observations; an over-cap
  lease floor records a fault and deliberately issues no downsize.
- Overflow now writes the observable durable
  `spot/v1/overflow/current.json` state only when the toggle is enabled. ptest
  writes a durable cancellation before local timeout fallback, and workers
  acknowledge cancelled deliveries without running them.
- Removed project-wide Pub/Sub viewer. Dedicated `operator_members` receive
  subscription subscriber, controller invoker, and narrowly documented
  dedicated-project Compute/OS-admin smoke permissions. README smoke commands
  now derive the worker zone, poll for a lease, SIGTERM the Docker supervisor,
  assert no result before redelivery, verify restart health, then await retry.

### Evidence

- RED: `ptest tests/test_spot_queue.py tests/test_spot_worker.py
  tests/test_spot_controller.py` exposed missing cancellation, heartbeat-loss,
  and cap-bound behavior.
- GREEN: focused queue/worker/controller/wiring tests — 60 passed in 1.33s.
- GREEN: `ptest --full` — 181 passed in 2.49s; Terraform `fmt -check` and
  `validate` passed. No Terraform plan/apply, deployment, config mutation,
  push, or merge ran.

### Remaining operational gate

The documented dedicated-project smoke remains required for live Docker restart
policy, Bubblewrap kernel policy, Artifact Registry, IAM, and Monitoring data.
Those checks cannot be truthfully performed from this checkout without an
authorized project mutation.

## Review fix round 4

### Remediation

- Replaced independent lease/result/cancellation objects with one
  `spot/v1/states/<request-key>.json` state object. A worker can CAS-transition
  only its current lease generation to a result; timeout cancellation competes
  for that same generation. Concurrent and ordered interleavings now prove that
  exactly one terminal outcome wins and a stale lease cannot publish.
- Cancellation records capture the active worker and lease expiry. ptest starts
  local fallback only after a post-cancellation idle heartbeat proves the child
  has returned, or after that captured lease expires. Failure to prove the
  barrier within the configured bound returns 75. Workers record idle before
  acknowledging an actively cancelled delivery.
- Split pre-publication failure handling from ambiguous Pub/Sub/result waiting.
  Once publish may have occurred, publish/wait errors return 75 instead of
  triggering local execution. If result publication wins the timeout CAS race,
  ptest returns that result. Existing durable requests are rejoined before
  budget/local-fallback decisions.
- Made cancellation idempotent by stable key and reason rather than timestamp.
  A cancelled deterministic key is an explicit safe local retry; `--fresh`
  creates a new remote key/attempt.
- Worker heartbeat observations now expire after 120 seconds. Stale active or
  idle records are ignored, and a controller with no fresh heartbeats can reach
  zero rather than being held up by a preempted VM's last record.
- Removed the non-dispatching overflow toggle, environment variable, Terraform
  variable, storage write method, and durable marker. Excess work simply stays
  in Pub/Sub. The controller's bucket grant is now read-only
  `roles/storage.objectViewer`, rather than `objectUser`; no Terraform-managed
  or local overflow state remained to clear, and no cloud deletion was run.
- Rewrote the manual preemption smoke with `--fresh`, bounded polling, an
  unexpired active-lease assertion tied to the exact managed VM, a stopped VM
  followed by a later-acquired lease whose generation advances beyond a
  post-stop baseline, and bounded terminal waiting. It accepts MIG recreation
  under the same VM name and cannot mistake the stopped worker's final renewal
  for redelivery. Worker containers receive the VM hostname so the initial
  live lease/VM assertion is meaningful.

### Evidence

- Baseline: focused queue/worker/controller/wiring/smoke tests passed 65 tests.
- RED: `ptest tests/test_spot_queue.py tests/test_spot_worker.py
  tests/test_spot_controller.py` failed 15 new assertions (39 passed), exposing
  the split terminal race, absent quiescence barrier, unsafe ambiguous fallback,
  timestamp-conflicting retry, stale heartbeat retention, overflow marker, and
  smoke/worker ordering gaps.
- Follow-up RED: the cancelled-deterministic-key retry test failed because a
  later caller did not re-check the prior cancellation's quiescence barrier.
  The paired not-yet-quiesced case now proves that retry returns 75.
- GREEN: focused queue/worker/controller tests passed 57 tests before that
  follow-up. The first queue/worker/controller/main-wiring/smoke selection
  passed 78 tests in 2.38s.
- Internal-review RED: the empty-prefix bootstrap, post-CAS claimant identity,
  and stale/failed immutable-terminal regressions failed all 4 focused cases.
  The fixes treat only GCS's explicit empty-listing diagnostics as empty,
  validate that the persisted lease is the exact candidate acquisition, and
  return 75 with `--fresh` guidance for non-reusable terminal keys.
- Internal-review GREEN: the queue/worker/controller/main-wiring/smoke
  selection passed 82 tests in 2.38s. The README preemption proof now accepts a
  MIG recreation with the same VM name and instead requires both a higher GCS
  generation and later `acquired_at`; exit-75 documentation covers Spot
  coordination without claiming no remote work ran.
- Follow-up review RED: a static smoke-ordering test failed because the first
  generation baseline was still captured before VM stop, so a last renewal
  could masquerade as reacquisition. GREEN: the baseline now occurs only after
  the stop operation completes; a subsequent active lease must have both a
  higher generation and later acquisition. The final focused selection passed
  83 tests in 2.34s, and the smoke block passed `bash -n`. The follow-up
  internal review found no remaining Critical, Important, or Minor issue.
- GREEN: the concurrent result/cancellation race passed five additional repeated
  scoped runs; the documented live-smoke shell block passed `bash -n`.
- GREEN: `terraform -chdir=terraform fmt -check`,
  `terraform -chdir=terraform validate`, and `python -m py_compile ptest
  spot_queue.py spot_worker.py spot_controller.py` passed. No plan/apply/deploy
  command ran.
- GREEN: the final `ptest --full` passed 199 tests in 3.39s.

### Commit

- `e8d577c fix: serialize spot queue terminal transitions`
- `afb204f fix: recheck cancelled retry quiescence`
- `328a0c8 fix: harden spot queue bootstrap and retries`
- `c350333 docs: make preemption proof generation-safe`

### Remaining operational gate

The dedicated-project smoke is still required to prove live IAM, VM stop/MIG
replacement, Artifact Registry, Bubblewrap kernel policy, Pub/Sub redelivery,
and Cloud Monitoring behavior. A custom worker lease longer than the default
180-second cancellation-quiescence wait safely returns 75 unless the worker
publishes its idle proof first. No Terraform apply, deployment, user config
mutation, cloud deletion, push, or merge was performed.

## Review fix round 5

### Remediation

- Split source preparation from durable request creation. Source packaging can
  still fall back locally because it cannot schedule work, but once
  `create_request()` is attempted any GCS/process ambiguity returns 75. A lost
  response after a successful create-only CAS can no longer race a local suite.
- Restored the brief's configurable overflow toggle as a meaningful advisory
  brake. With matching Terraform and ptest toggles enabled, ptest calls the
  IAM-protected controller `/admit` endpoint before source upload or durable
  creation. An observed unacknowledged backlog or active-lease floor at the
  worker cap returns 75; disabled/mismatched or unavailable admission fails
  closed. Existing durable requests bypass the gate and rejoin normally.
- Kept controller IAM least-privilege: admission uses its existing Monitoring,
  MIG, and read-only bucket access and adds no marker, counter, storage write,
  malformed Cloud Run dispatch, or new grant. The controller returns its toggle
  state and ptest validates it. Terraform exposes the boolean and controller
  environment; its generated Spot config stanza and `config.example.toml`
  expose the matching client URL/toggle.
- Documented the truthful limit: delayed, sampled Cloud Monitoring data makes
  this an advisory overflow brake, not an atomic admission cap. It still changes
  behavior safely whenever saturation is observed, before any request exists.
- Hardened both live smoke runs with `--fresh` and explicit process exit checks.
  Each final durable state must be a passed result with exit code 0, and each
  subscription pull is parsed and asserted to be an empty JSON array. A passing
  local fallback can no longer mask a missing or failed remote execution.

### Evidence

- RED: the ambiguous-create regression returned local fallback after simulating
  a successful persistence followed by a lost response. GREEN: the focused
  creation/publish/retry selection passed 3 tests after the boundary split.
- RED: controller policy, real authenticated `/admit`, controller environment,
  ptest pre-upload rejection, identity-token client, and strict smoke-contract
  cases each failed before implementation. The corrected Pub/Sub occupancy
  test also failed the initial double-counting formula before changing it to the
  maximum of the unacknowledged backlog and active-lease floor.
- Internal-review RED: 3 focused cases exposed an undocumented hard-cap claim
  and a client/controller toggle mismatch. GREEN: `/admit` now reports enabled
  state, mismatches fail closed, and the Monitoring limit is explicit. Follow-up
  review found no remaining Critical, Important, or Minor issue.
- GREEN: final queue/worker/controller/main-wiring/smoke selection passed 92
  tests in 2.35s. `terraform fmt -check`, `terraform validate`, Python bytecode
  compilation, the extracted README smoke block's `bash -n`, and `git diff
  --check` all passed.
- GREEN: final `ptest --full` passed 208 tests in 3.91s.

### Commits

- `fe96d3d fix: fail closed after spot request creation`
- `bfd932f feat: gate spot queue overflow admission`
- `4271f32 fix: qualify spot overflow admission brake`

### Remaining operational gate

The overflow brake deliberately is not an atomic reservation: concurrent calls
can pass before the delayed Monitoring signal reflects them. It prevents new
durable work only when the controller observes saturation. The corrected live
dedicated-project smoke remains required for IAM, Monitoring shape/delay, MIG
replacement, Artifact Registry, Bubblewrap, and Pub/Sub redelivery. No
Terraform plan/apply, deployment, user config mutation, push, or merge ran.
