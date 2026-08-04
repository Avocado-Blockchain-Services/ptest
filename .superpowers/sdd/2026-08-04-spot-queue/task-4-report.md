# Task 4 report — Whole-branch verification and Scheduler route fix

## Status

Complete. Corrected the Spot controller Scheduler target from the Cloud Run
service root to its implemented `POST /reconcile` route. No Terraform plan or
apply, deployment, user configuration mutation, default-backend change, push,
or merge was performed.

## Remediation

- Changed the Scheduler URI to
  `${google_cloud_run_v2_service.spot_controller.uri}/reconcile`, matching the
  controller HTTP contract.
- Added a focused Terraform regression that extracts the actual
  `google_cloud_scheduler_job.spot_controller` resource block and fails if its
  URI omits `/reconcile`.

## Evidence

- RED: `ptest tests/test_spot_terraform.py` failed 1 test because the Scheduler
  URI was the bare Cloud Run service URI.
- GREEN: `ptest tests/test_spot_terraform.py` passed 1 test after the route fix.
- GREEN: `ptest tests/test_spot_terraform.py tests/test_spot_controller.py`
  passed 17 tests in 0.89s.
- GREEN: `ptest --full` passed 209 tests in 4.62s.
- GREEN: `terraform -chdir=terraform fmt -check` and
  `terraform -chdir=terraform validate` passed; Python bytecode compilation and
  `git diff --check` also passed.
- Scoped audit confirmed validated queue record parsing, result publication
  before acknowledgement, per-request fresh worker directories, capped
  capacity/backlog behavior, and no change to the default backend.

## Commit

- `d46f1ff fix: target spot controller reconcile route`

## Remaining concern

The live dedicated-project smoke remains the deployment gate for Scheduler OIDC,
Cloud Run routing, IAM, Monitoring, MIG replacement, and Pub/Sub behavior. This
task made no remote mutation and did not claim live GCP verification.

---

## Final whole-branch remediation wave

### Status

Complete. Closed the five final audit findings and the two rollout/install
issues found by the follow-up review. No Terraform plan or apply, deployment,
user configuration mutation, default-backend change, push, or merge was
performed.

### Remediation

- Scaling with live leases now caps
  `max(backlog, active_workers, active_leases)`, so a first lease cannot suppress
  scale-out for the remaining backlog.
- Scheduler still posts to `/reconcile`, while its OIDC audience is explicitly
  the base Cloud Run service URI.
- `install.sh` builds a complete versioned `ptest`/`spot_queue.py` bundle and
  atomically switches one executable symlink. Runtime dispatch and `doctor`
  enforce a companion ABI; `doctor` fails configured Spot installs when the
  companion is missing or incompatible.
- Worker storage IAM is split into conditional read-only access for immutable
  `sources/` and `spot/v1/requests/`, and conditional `objectUser` access only
  under mutable `spot/v1/states/` and `spot/v1/workers/` prefixes.
- Workers generation-CAS a bounded shared worker index before lease claim and
  renewal. The controller reads that single object instead of every retained
  terminal state. During mixed rollout, running MIG identities absent from the
  index are conservatively counted as leases; unidentified instances are too.

### Evidence

- RED: the initial focused run failed 12 tests across scale policy, Scheduler
  audience, IAM, installed layout/doctor, lease-expiry records, claim/renew
  ordering, and terminal-history reads.
- RED: review regressions failed for non-atomic installation, incompatible
  companion detection, and unindexed rollout accounting; the equal-count,
  different-worker identity regression then failed before identity matching.
- GREEN: final focused Spot/install/Terraform run passed 83 tests in 2.75s.
- GREEN: final `ptest --full` passed 222 tests in 3.99s.
- GREEN: `terraform -chdir=terraform fmt -check` and
  `terraform -chdir=terraform validate` passed.
- GREEN: Python bytecode compilation, `bash -n install.sh`, and
  `git diff --check` passed.
- Follow-up internal review cleared the identity-based rollout accounting and
  atomic bundle/ABI remediation.

### Commit

- `7fa6774 fix: close spot queue rollout gaps`

### Remaining concern

Live dedicated-project smoke remains required to prove Cloud Scheduler OIDC,
conditional Cloud Storage IAM, MIG identity shape, worker-index CAS behavior,
preemption, and Pub/Sub behavior against GCP. This wave made no remote mutation
and does not claim that deployment gate.
