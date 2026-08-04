# ptest snapshot cache and Spot queue design

## Goal

Reduce ptest submission time and add an opt-in persistent Spot-VM backend. No
infrastructure is applied, no user configuration is changed, and this branch
is not merged.

## Snapshot cache

Source artifacts remain immutable GCS objects at `sources/<tree-digest>.tar.gz`.
ptest must check for that exact object *before* it builds a temporary archive.
A hit skips both archive creation and upload; a miss creates one archive and
uses `--if-generation-match=0`. The source digest still includes every shipped
byte, so dirty worktrees are exact. Timings for digest, probe, packaging, and
upload are logged separately.

## Spot queue

`backend = "spot_queue"` retains the existing request digest, one-hour passing
result reuse, source artifact, exit-code, and output contracts. It writes a
validated request record to GCS and publishes its key to a Pub/Sub pull
subscription. Workers claim records with generation preconditions, heartbeat
leases, run one suite in a fresh temporary directory, publish a durable
terminal result, and only then acknowledge Pub/Sub. Delivery is at-least-once;
duplicate delivery/preemption cannot produce two authoritative results.

## Control plane

A small authenticated Cloud Run controller calculates a regional Spot MIG
target from queue backlog and fresh worker heartbeats. It keeps one worker for
a configurable idle window, scales zero through a configurable maximum, and
never scales in an active lease. Backlog above the cap remains in Pub/Sub; no
marker or toggle claims to dispatch it elsewhere. The MIG replaces preempted
VMs. The worker image bakes Node/Vitest dependencies but no application source.

## Safety and verification

Terraform describes Pub/Sub, controller, least-privilege identities, the
regional Spot MIG, and scheduler trigger. `terraform apply` is forbidden.
Unit tests use fake storage/Pub/Sub/process adapters and cover cache behavior,
claims, leases, results, routing, worker ordering, and scale decisions. The
full ptest suite is run through `ptest --full` once at the end.
