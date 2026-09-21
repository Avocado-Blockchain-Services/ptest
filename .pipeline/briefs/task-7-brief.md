# Task 7 — Guard lifecycle and quiescent finalization handoff

## Ownership

Own only `src/ptest/guard.py`, `tests/ng/test_guard.py`,
`tests/ng/fixtures/processes/`, and `.pipeline/out/task-7.json`. Do not edit
the scheduler, contracts, adapters, CLI, platform, or controller source.

## Frozen inputs

Read `AGENTS.md`, `docs/designs/2026-09-17-ptest-ng-design.md` sections 6–7,
`docs/plans/2026-09-17-ptest-ng.md` Task 7, the frozen contracts, and the
current scheduler before implementation. `ControlFrame` and `LaunchManifest`
are the sole private protocol; do not introduce alternate records, defaults,
wire formats, or persistence.

## Deliverable

Implement `run_guard(control_fd: int, manifest_fd: int) -> int` as the
registered, new-session guard. It validates bounded length-prefixed control and
manifest frames, validates run/nonce/domain/grant identity, registers its own
`ProcessIdentity` through `scheduler.register_guard` before repository work or
child creation, closes private descriptors from children, preserves literal
`PreparedRun.argv` and inherited stdout/stderr, and emits only bounded
provisional control facts. It never publishes an outcome/baseline and never
releases the scheduler lease.

Implement signal and parent-close handling with idempotent cancellation of the
guard's own anchored process group only: 3 s grace then SIGKILL, no stale
stored-PID signalling. EOF may permit the in-flight attempt to finish but must
forbid another compound attempt. Wait direct children before emitting the
`draining` provisional frame. Reject malformed, truncated, overlong,
wrong-protocol, wrong-run, or wrong-nonce input before execution.

## Lifecycle amendment

The reviewed design adds scheduler-owned `mark_draining(domain, grant,
guard)->bool`. Do not implement it: Task 4 owns its authenticated CAS and
tests. After it lands, call it only after permanently closing spawning and
waiting every direct child; emit `draining` only after a true result. A false
or failed handoff forbids any future spawn or success notification. Do not
claim full Task 7 until the amendment is present and exercised end-to-end.

If the three-second cancellation grace expires, the live guard may SIGKILL its
own anchored group, including itself. That is abnormal termination: it cannot
reap, mark DRAINING, or notify afterward, and it must never fabricate a
successful result. Capacity remains charged until independent absence proof.
Do not move force-signalling to a reconciler or signal a stored PID/PGID.

## Required tests and evidence

Start with `test_unregistered_guard_never_executes` using an IPC barrier.
Add deterministic owned-handle/barrier/watchdog tests for registration race,
protocol rejects, queued/active SIGINT/SIGTERM, repeated cancellation,
parent-only death around registration/fork/run/drain/finalize, cooperative
grandchild cleanup, and observed escaped descendants. No arbitrary sleeps,
PID/name killing, or host process impact. The tests must prove provisional
facts never imply final success, an ESRCH quiescence proof happens only after
the guard is reaped, group/escape uncertainty retains charge, cancellation
finishes fixture-owned descendants within 10 s, and recovery has an explicit
45 s watchdog for its 30 s target.

Use only `scripts/ptest-bootstrap` for tests. Run the focused red test first,
then the full task file, `git diff --check`, and `graphify update .`. Do not
commit; report exact commands/results and any deferred end-to-end lifecycle
coverage in `.pipeline/out/task-7.json`.
