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
