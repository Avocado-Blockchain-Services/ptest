# `ptest status` design

## Purpose

Provide one read-only operator command for the Spot full-suite backend.  It
answers the three live questions needed during normal operation: how many jobs
are waiting, how many jobs are currently being worked, and how many worker VMs
are on.

## Command and output

`ptest status` resolves the project configuration from the current directory.
For a configured `spot_queue` backend it prints a compact, stable summary:

```text
Spot queue
  unacknowledged deliveries   <integer>
  working jobs   <integer>
  servers on     <integer>
```

The command is observational only.  It must never pull, acknowledge, publish,
create, cancel, or modify a queue object or VM.

## Sources and meaning

- **unacknowledged deliveries** is the current Pub/Sub subscription backlog,
  obtained using the same authenticated monitoring query used by the capacity
  controller. It can overlap with active jobs until the worker acknowledges
  their deliveries.
- **working jobs** is the number of unexpired durable Spot leases.  This is the
  authoritative in-flight-job measure, rather than a count inferred from VMs.
- **servers on** is the number of RUNNING instances in the configured Spot MIG.
  Instances booting or terminating are not counted as on.

The command reuses the controller's authenticated GCP adapter so status and
autoscaling have identical definitions for backlog, worker liveness, and
leases.  It requires the Spot project, region, MIG, subscription, and bucket
configuration plus `gcloud` credentials.

## Errors and tests

If the current project has no `spot_queue` backend, `ptest status` exits
non-zero and explains that Spot is not configured.  If a live metric cannot be
read, it exits non-zero with a concise error; it does not invent a zero.

Unit tests will cover command dispatch, the output contract, the configured and
unconfigured cases, and error propagation.  A live check will run `ptest
status` against the deployed isolated Spot queue and must not change backlog,
leases, or MIG target.
