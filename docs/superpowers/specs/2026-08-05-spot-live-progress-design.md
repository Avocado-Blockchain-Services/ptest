# Spot live progress

## Purpose

Expose honest progress for a running compulsory Spot test without SSH access and
without submitting, cancelling, or otherwise changing the request.

## Durable record

Each worker writes `spot/v1/progress/<request-key>.json` while it owns a live
lease.  The record contains the request key, worker ID, observation timestamp,
elapsed seconds, parsed completed and total test counts when available, and a
bounded trailing output excerpt.  A progress record is advisory: the durable
lease remains the authority for queued versus working, and a terminal result
remains the authority for completion and exit code.

Workers update it at a bounded interval while the subprocess runs and remove it
only after successfully publishing a terminal result.  A preempted worker may
leave a stale record; readers show it only while its timestamp is fresh and the
durable lease is live.

## CLI

`ptest follow <request-key>` polls the durable progress record and prints a
single continuously refreshed status line.  It shows `N / M tests` and a
percentage only when both values are known; otherwise it shows the elapsed
time and latest output excerpt.  On terminal result it prints the normal
terminal output and exits with that result's exit code.

The command never publishes a request, starts a worker, acknowledges a queue
message, or changes a lease.  Interrupting it has no effect on the remote run;
it can be run again from any configured machine.

## Errors

If progress is not yet present, `follow` says that the job is queued or starting
and continues polling.  If a progress record is stale but the terminal result
does not exist, it reports the lease state rather than inventing a percentage.
Storage read failures use the existing Spot error reporting conventions.

## Verification

Tests cover progress serialization, stale-record handling, test-count parsing,
and `follow` rendering/terminal exit behavior.  Focused tests use `ptest`; the
worker and CLI images are rebuilt and deployed only after those tests pass.
