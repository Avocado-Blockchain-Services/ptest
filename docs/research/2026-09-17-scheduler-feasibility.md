# ptest NG scheduler feasibility

Created: 2026-09-17 UTC

Author: architect-agent (Astra); bounded research input to M1, not the final design

Authority: the complete approved [product specification](../specs/2026-09-17-ptest-ng-product.md), especially S1–S4, C1–C3, A2–A4 and A14. No implementation, test execution, dependency installation, or platform support claim accompanies this document.

## Recommendation and question space

For X = a shared Linux/macOS local scheduler, the critical questions are:

| Question | Recommended answer |
|---|---|
| How do independent installations find one domain? | Fixed paths derived from the OS account database, independent of environment, on a supported local filesystem. |
| What serializes admission? | One small SQLite ledger; `BEGIN IMMEDIATE` atomically changes capacity, FIFO state, checkout exclusivity, and named-resource ownership. |
| Is a persistent daemon necessary? | No. Waiting clients drive reconciliation; one short-lived guard per admitted job owns its execution and finalization. |
| How is process identity kept safe during cancellation? | The live guard is its own session/group leader and signals its own group on a private-channel request. A recovery client never sends a terminating signal to a stored PID or PGID. |
| What proves ordinary managed work is gone? | Under the explicit cooperative-group contract, the guard has stopped and a kernel process-group existence probe reports absence. A process-tree snapshot alone does not prove absence. |
| What about daemonized/escaped descendants? | They are outside the supported cooperative lifecycle. Observed escape or uncertain ownership holds capacity and reports a limitation; arbitrary escape detection cannot be promised. |
| What makes nested invocation safe? | Live ancestry/group checks against active domain records, with environment IDs used only as hints. A same-domain nested execution fails before enqueue. |
| How is this tested without recursive deadlock? | The outer project tests run through installed `ptest`; child commands invoke the exact candidate CLI with an explicit private fixture domain and bounded synthetic workloads. |

This is a feasible minimal design for cooperative foreground workloads. It is not a way to contain arbitrary descendants of arbitrary tests. Neither psutil, repeated `/proc` scans, nor a longer timeout changes that boundary.

## Evidence and reusable material

Read-only local inspection was performed at task base `1ca3223eac562af1adf54111b54f223ab1a5b1de`. This checkout has no `graphify-out/graph.json`; no graph was generated. The machine reports Linux, account UID 1000, account-database home `/home/ingmar`, and a local ext-family home filesystem. The installed command resolves to `/home/ingmar/.local/bin/ptest`. These are inspection observations, not tested portability evidence.

The legacy `ptest:1226` function `coalesce_remote_run` uses per-key flock and completed-result replay. Its semantics must not become the NG scheduler: NG does not deduplicate runs, and releasing a caller's file lock does not establish that its children have stopped. The provisional `src/ptest/execution.py:10` is likewise not an ownership implementation to reuse unchanged: it waits/reaps its direct child and then sends a further group signal, without a shared lease protocol or a pinned group identity. This is a design reuse finding, not an implemented correction.

Primary sources establish these primitive facts:

- Python exposes OS-account lookup through `pwd.getpwuid`, including the account home directory. [Python pwd](https://docs.python.org/3/library/pwd.html)
- SQLite permits one writer and `BEGIN IMMEDIATE` acquires a write transaction before reads that decide a grant. Its transaction mechanism is sufficient for the shared admission critical section. [SQLite transactions](https://www.sqlite.org/lang_transaction.html)
- SQLite rollback journals recover interrupted transactions. Filesystem locking failures and removing/replacing a live database or its journal can break assumptions; these are not reasons to recreate an empty scheduler silently. [Atomic commit](https://www.sqlite.org/atomiccommit.html), [SQLite corruption guidance](https://www.sqlite.org/howtocorrupt.html)
- `Popen(start_new_session=True)` calls `setsid`; `pass_fds` explicitly selects inherited control descriptors. Use these options rather than a threaded-process `preexec_fn`. [Python subprocess](https://docs.python.org/3/library/subprocess.html)
- Linux and macOS support a new session/group whose leader has its own PID. Nonleaders can create other sessions; groups do not contain descendants permanently. [Linux setsid](https://man7.org/linux/man-pages/man2/setsid.2.html), [Apple setsid](https://developer.apple.com/library/archive/documentation/System/Conceptual/ManPages_iPhoneOS/man2/setsid.2.html)
- Signal zero probes existence/permission without delivering a signal. A negative PID addresses a group; group membership, not ancestry, controls delivery. [Linux kill](https://man7.org/linux/man-pages/man2/kill.2.html), [Apple kill](https://developer.apple.com/library/archive/documentation/System/Conceptual/ManPages_iPhoneOS/man2/kill.2.html)
- Linux exposes process start ticks in `/proc/<pid>/stat`; Darwin exposes start seconds/microseconds and PGID in `proc_bsdinfo`. [Linux proc stat](https://man7.org/linux/man-pages/man5/proc_pid_stat.5.html), [Apple XNU proc_info.h](https://github.com/apple-oss-distributions/xnu/blob/main/bsd/sys/proc_info.h)
- psutil supports Linux/macOS birth time, ancestry, and process status. Its documentation explicitly warns that descendants disappear from recursive results when an intermediate parent disappears. Its creation time may reflect clock adjustments; its Unix signal method checks identity before calling `os.kill`, not an atomic birth-token-and-signal operation. [psutil documentation](https://psutil.readthedocs.io/stable/)
- Linux pidfds address a particular process without a numeric-PID signaling race; cgroup v2 offers hierarchy population and group kill primitives, subject to delegation. Neither supplies an unprivileged portable macOS solution. [pidfd signals](https://man7.org/linux/man-pages/man2/pidfd_send_signal.2.html), [kernel cgroup v2](https://www.kernel.org/doc/html/latest/admin-guide/cgroup-v2.html)

Apple's manual pages are archived API documentation. Current macOS behavior, Python availability, psutil wheels, filesystem behavior, and timing targets still require actual matrix runs.

## Domain and storage

Resolve `uid = os.getuid()` and require matching effective identity; reject unsupported set-ID execution. Obtain the home with `pwd.getpwuid(uid).pw_dir`, not `expanduser`, `HOME`, `USER`, XDG, repository settings, or an installation prefix.

Proposed fixed locations:

| Platform | Machine configuration | Scheduler state |
|---|---|---|
| Linux | `<account-home>/.config/ptest/machine.toml` | `<account-home>/.local/state/ptest/coordination/` |
| macOS | `<account-home>/Library/Application Support/ptest/machine.toml` | `<account-home>/Library/Application Support/ptest/coordination/` |

The final design owns the versioned `machine.toml` grammar. A small manually editable file with `max_slots`, `max_jobs`, and an optional memory budget avoids a separate settings command; repository/invocation overrides only lower those ceilings.

The supported local filesystem and OS account make this host/user scoped without adding a hardware-UUID dependency. Network/shared homes and other remotely shared scheduler storage are unsupported in v1; reject a detected unsupported filesystem, and fail visibly if locality/locking capability cannot be established. Do not redirect to `/tmp`, a per-install directory, or a newly generated fallback domain. Account-home relocation while work is active and deliberate replacement of live state by the same OS user are outside the operating contract. Do not claim protection against that privileged peer.

State directories are private (`0700`), files private (`0600`), owned by the expected UID. Validate the root and opened objects using descriptor-based checks; reject ptest state symlinks, nonregular database/marker objects, unexpected hard links, or ownership mismatch. The exact filesystem allowlist/capability check belongs in the final platform matrix. Environment changes are ignored for root selection; an explicitly unsupported ptest-specific location override produces an error. `where` reports the canonical path and provenance.

Use one protocol-independent root and database filename across NG versions. A permanent small bootstrap lock may serialize initial marker/schema creation; it is not the admission authority and must not be replaced/unlinked while a process may hold it. Existing domain marker plus missing/replaced database is corruption, not first use. Record a domain UUID, protocol version, expected database identity, and initialization completion; an interrupted initialization fails safely and starts no work. Do not create a `v2/` sibling domain when encountering an incompatible version.

The runtime ledger can use the standard-library SQLite binding: rollback `journal_mode=DELETE`, `synchronous=FULL`, foreign keys, a finite busy timeout, and verified pragma values. Consider `fullfsync=ON` on macOS for the declared durability target; benchmark its cost. Avoid WAL and a second runtime lock protocol unless measurements demonstrate a need. SQLite already supplies writer locking and crash rollback. [SQLite pragmas](https://www.sqlite.org/pragma.html)

Keep only scheduling data here: domain/policy generation; bounded queue/active rows; monotone enqueue sequence; opaque run/checkout/resource IDs; process identity evidence; requested/granted units; state/reason/timestamps. Commands, environment values, raw output, selection graphs, and test history do not belong in this ledger. Shared SQLite corruption must stop execution; unrelated selection/history corruption should not disable an otherwise valid full run.

## Admission transaction and bounds

The request contains validated worker minimum/preference/maximum, optional total-memory estimate, exclusive mode, checkout identity, and a bounded set of global named-resource IDs. Checkout identity should include the canonical filesystem identity of the actual worktree root, not repo basename or branch name. A symlink alias to the same checkout must collide; separate Git worktrees must not.

Enqueue and grant follow these rules:

1. Resolve initialized execution policy, normal/fixture domain, platform capability, and nesting before setup or queue entry. Reject impossible bounds, unsupported ownership settings, and oversized requests.
2. In a short write transaction, verify protocol/root identity and policy; clean only safely resolved finished/expired records; allocate a monotone FIFO sequence and insert `queued`. Commit before waiting.
3. At each attempt, evaluate the earliest live queued request. A request blocked on capacity, checkout, memory, or named resources blocks every newer request. This deliberately accepts head-of-line blocking; it is true FIFO.
4. In one write transaction, atomically reserve worker units, one active-job unit, memory/exclusivity, checkout, and every named resource, then change that request to `granted`. Acquire all or none. No process waits holding one resource while seeking another.
5. Only after durable grant may its guard start the registered execution lifecycle. Every acquisition, cancellation, release, and budget-change recheck uses the same ledger protocol. A stale snapshot never authorizes launch.

The first request's allowed range may permit a smaller grant; fix the grant at admission. Do not silently resize already-running commands or lend a grant to nested children. Full, selected, setup, executable discovery, and probe phases retain the job's reservation throughout their artifact lifetime. Admission cannot start a second same-checkout setup while the first run is still publishing files.

Recommended finite defaults for final-design evaluation:

| Setting | Proposal and rationale |
|---|---|
| Shared worker ceiling | `min(4, max(1, floor(C / 2)))`, with derivation shown. |
| Shared active jobs | `min(2, worker_ceiling)`; independent of requested worker count. |
| New repository workers | 1, as approved. |
| Pending requests | 256; overflow returns `75` before enqueue. |
| Poll interval | 250 ms with small bounded jitter; no long blocking lock wait. |
| Lock contention timeout | At most 2 s per attempt; queue deadline remains authoritative. |
| Queue deadline | Default 30 min, finite validated maximum 24 h. |
| Cancellation grace | 5 s; entire controlled cancellation target remains 10 s. |
| Scheduler ledger | Suggested 16 MiB database cap; separately budget rollback-journal overhead and bounded bootstrap metadata. Never evict active ownership to fit. |

`C` is the observed usable CPU count, not an environment-overridable Python CPU count. On Linux include affinity and applicable cgroup v2 ancestor `cpu.max`/cpuset constraints; unsupported or unreadable quota layouts produce a conservative one-slot capability fallback with a diagnostic, not guessed hardware capacity. macOS starts with the observed logical CPU count and the conservative ceiling; do not claim Linux-style quota detection there. Persist the initial derived machine ceiling under the domain transaction with provenance. Later observations and per-invocation limits may lower effective grants, never increase the persisted machine ceiling. A configured ceiling decrease blocks additional over-budget grants without killing current jobs. A now-impossible queued request is reduced within its original allowed range or rejected.

For memory, the smallest honest policy is: retain `estimate: unknown`, admit an unknown-estimate run exclusively with one worker, and allow known-estimate concurrency only within the configured total memory budget. If an explicit request's allowed range excludes one worker, require a declared estimate instead of silently shrinking it. If selecting an automatic budget, propose half observed physical/usable memory and print that derivation; a cgroup limit must reduce it where supported. Unknown exclusivity is an accounting policy, not an invented measured estimate. Available-memory observations may defer new work but must not increase configured capacity. Do not equate a large accounting reservation for unknown memory with an actual measurement or promise of avoiding OOM. Benchmark the conservative unknown policy and explicitly document its throughput cost.

## Guard lifecycle, signals, and recovery

Use one guard per admitted job, started by the exact installed candidate interpreter with `start_new_session=True`. It is a small implementation-owned process, not repository code, and stays foreground within its own session. The caller and guard exchange bounded frames over an inherited `socketpair` or pipe; possession of that private descriptor plus a one-run nonce binds requests. Do not put the nonce in a public report, argv, or the inherited test environment. Close the private descriptors in setup/runner children. The guard directly inherits output descriptors so stdout/stderr are not accumulated in a memory buffer.

The guard must register itself before it may launch any repository/setup/runner code:

```text
queued -> granted/unregistered -> registered guard -> running phases
                                                   -> provisional facts -> draining
                                                   -> caller finalizing -> released
                                                   -> cancelling -> draining/uncertain
```

The admission transaction records caller identity and a registration nonce. The guard's first transaction validates the same unrevoked grant and nonce, publishes its own PID/birth/PGID, and marks it registered. It then receives/validates the launch handoff. Reconciliation may revoke an unregistered grant after its caller is proved gone; because registration uses the same transaction, a late guard sees revocation and exits without repository execution. This closes the child-created-before-PID-recorded gap.

Once registered, the guard's group is the ownership anchor even in the interval before its runner PID is recorded. Setup and runners inherit that PGID, including all cooperating descendants. The guard owns setup, all attempts, and provisional runner evidence. The caller owns final publication after quiescence; both retain the same checkout/capacity lease throughout. The guard cannot launch more work after entering `draining`.

Cancellation:

- The invoking CLI's SIGINT/SIGTERM handler requests cancellation through its private channel. A process-group abort of the CLI reaches that handler; the guard's separate session avoids accidental duplicate delivery.
- The guard marks `cancelling`, then signals its own group. Because the sender itself is a live member/leader, that PGID cannot identify an unrelated newly recycled group. Its own signal handler is idempotent and continues cleanup; caught signal handlers reset appropriately on exec for runner children.
- After the five-second grace, the guard may send SIGKILL to its own group, including itself. The durable grant remains charged. The live CLI or another reconciler observes actual group disappearance before release. Repeated cancellation cannot create extra cleanup or launch phases.
- If the guard is already dead, do not signal its numeric PID or PGID from a fresh client. Keep charged while any matching group exists or ownership is ambiguous; report manual ownership-check guidance. A birth-time check followed by `kill` does not make that operation atomic.

On successful completion the guard waits its direct children and persists provisional runner facts. Observed surviving group members keep it alive to handle cancellation; a bounded group scan suggesting only the guard remains may allow it to commit `draining` and exit, but does not authorize success or release. Only then can a non-signalling `killpg(recorded_pgid, 0)` returning `ESRCH`, together with no unresolved observed escaped descendant, prove the cooperative group quiescent. A snapshot saying “only the guard remains” is insufficient: scanning can miss a concurrently forked grandchild. Group absence is the quiescence check, not recursive process enumeration.

After quiescence, the live caller enters `finalizing`, revalidates source/config inputs, publishes final artifacts and any eligible baseline, and releases the lease in that order. Reconcilers must retain the lease while this authenticated caller is still finalizing even though the guard group is gone. If the caller dies before final publication, a reconciler may mark the run incomplete and release after proving quiescence; it may not promote provisional facts to success or a baseline. No post-release write may touch checkout-shared runner artifacts.

This intentionally keeps execution and result truth separate. A runner result can be durably recorded before release; a crash cannot manufacture a passing result. If the runner's outcome is unavailable, report incomplete rather than guessing zero. If cleanup remains unresolved, the CLI may finish its bounded cancellation wait with an uncertainty diagnostic while the ledger continues charging capacity.

Recovery rules:

| Observed state | Action |
|---|---|
| Queued owner gone / queue deadline expired | Atomically remove/cancel; no repository process was allowed to start. |
| Granted but unregistered, caller proved gone | Revoke under the registration transaction; late guard must fail its CAS. |
| Caller killed, registered guard/work live | Keep all reservations; show `orphaned-caller` and live managed work. The guard may finish normally. |
| Guard gone, recorded group still exists | Keep charged. This includes a conservatively suspected reused PGID; never kill it to find out. |
| Guard/group absent, no unresolved escape observation, caller live | Retain lease through caller finalization; release only after final publication. |
| Guard/group absent, no unresolved escape observation, caller proved gone | Mark incomplete if needed and release on reconciliation; never promote provisional success. A waiter polling every 250 ms should satisfy the controlled 30-second recovery target. |
| Access denied, inconsistent birth evidence, incomplete ownership query, tracked escape | Keep charged and report `uncertain`. No timeout-based forced reclaim. |
| Database corrupt/missing after initialization, incompatible protocol, disk/lock unavailable | Structured scheduling error; no fallback launch and no empty-ledger replacement. |

With no waiter/status invocation and no surviving guard, nothing has to run continuously to clear old rows. “Recover within 30 seconds” is measured with a waiting/new command actively attempting admission. The next command reconciles before granting. Static `status` can report a computed state; if the final design keeps it strictly read-only, admission performs the actual release transaction.

Process identity observations should retain UID, PID, birth evidence, role, and recorded group/session association. A pinned psutil release is a reasonable single runtime dependency for portable ancestry/status. Use fresh observations and never infer death merely from an age/heartbeat or a changed clock-derived creation time. Linux raw start ticks can strengthen observations without a C extension. A direct live control channel establishes guard authority more strongly than stored timestamps. No process inspection needs to read argv or environment.

## Exact support boundary

Name the capability `cooperative-process-group` in the design. Supported setup/runner/service lifecycles remain foreground, keep managed descendants in the inherited group, and stop/join owned services. Reject declared daemon/detach launcher configurations before admission; do not advertise lifecycle support for unreviewed wrappers that detach work. Doctor may flag service-spawning patterns, but absence of a finding is not certification.

`setsid`, `setpgid`, double fork, closing inherited descriptors, namespace changes, or handing work to an external daemon can escape this model. An observed escape must retain uncertainty and its reservation, without automatically killing a PID found by name or dropping an application resource. If a tracked escaped process might have spawned untracked descendants, its later exit alone does not prove the escaped subtree empty; do not silently clear that uncertainty. Unobserved escapes cannot be exhaustively detected with these primitives. This limitation must be explicit; an environment marker is not containment.

Consequently, a requirement to prove quiescence or kill *every arbitrary descendant* on both OSes is not implementable by this recommendation. It would require a separate containment/platform decision and potentially additional authority; it is not solved by quietly requiring systemd/cgroups/root/containers. Under the product's hostile-repository exclusion and the explicitly narrowed cooperative lifecycle, S4 is implementable for the supported workload and acceptance fixtures. The final design must preserve both statements.

## Nesting and fixture-domain harness

Before enqueue, compare bounded live ancestry against active caller/guard identities in the resolved domain, and use current group membership as additional evidence for an orphaned cooperative child. Inspect actual process identity; a `PTEST_RUN_ID`-style environment value is only a lookup hint. Detect nesting even if the marker is removed. A stale marker with no matching live ancestry is not nesting. Ambiguous/inaccessible ancestry returns scheduling uncertainty rather than joining a queue that may deadlock. Read-only commands remain available.

For fixture domains, require the explicit CLI option on every participating invocation. A harness creates a private temporary directory, then initializes a versioned fixture marker recording UID, directory identity, random fixture ID, finite logical budgets, and permitted fixture roots. The first initializer rejects a nonempty unrelated directory; subsequent clients require the validated marker. Require executing fixture projects and mutable outputs to stay inside that fixture directory, with no symlink escape. Never accept an environment/repository-config switch to fixture mode. All plans, results, `where`, and `status` label `isolated-fixture-domain`; fixture history stays there and cannot seed normal evidence.

The real-subprocess harness should use the following arrangement:

1. Run the repository's test suite through the installed outer `ptest`, as required. Pin the candidate interpreter/console entrypoint by absolute path from this checkout's own environment; do not search `PATH` for a child command named `ptest`.
2. Child CLI invocations always include the explicit fixture-domain option. This remains correct when NG later becomes the outer runner: child fixture execution does not wait for its outer normal-domain parent. A same-domain nesting case deliberately passes the *same* fixture option in its nested call and must return `75` at budget 1.
3. Create miniature projects/two repositories/three worktrees under the fixture directory. Use installed tiny runners or synthetic pipe-controlled workers, never the user's application suite or live service. Each synthetic process does bounded work; logical requests of 1–4 slots do not imply four real CPU-bound workers.
4. Use pipes/socketpairs and recorded phase barriers, not arbitrary sleeps, to freeze enqueue, grant, registration, runner fork, execution, finalization, and release. A watchdog bounds every wait. Fixture fault hooks must require validated fixture mode and remain unavailable through normal environment variables.
5. Assert committed grant/release transitions through a bounded fixture observer, not only sampled `status` snapshots. A status sampler can miss an oversubscription interval. Keep event fields free of raw argv/env.
6. Teardown owns only recorded child handles and the validated fixture tree. Do not terminate by executable name or delete the normal scheduler state. Each failure leaves enough bounded event/identity evidence to explain cleanup uncertainty.

Required cases are six independent clients at logical budgets 4 and 1; job/slot/memory ceilings; a large FIFO head before new small requests; same-checkout and cross-checkout resource exclusivity; limit reduction; queue overflow/deadline; queued and active SIGINT/SIGTERM including CLI-group delivery; caller-only SIGKILL with surviving guard/work; guard death before registration and after runner fork; result/draining crash; reused PID/PGID and inaccessible identity fixtures; corruption/missing initialized DB/protocol mismatch; and stale/removed inherited nesting markers. Normal-root invariance under different HOME/XDG/cwd/installation paths is tested through read-only `where` resolution, not normal-domain workload launch.

Add an explicit escape fixture: a service creates another session and waits on a harness-owned control channel. Prove the documented limitation/uncertainty path and that its reservation is not released while the observed escape is live. Do not interpret this as proving complete arbitrary-escape detection. Actual PID reuse need not be forced by exhausting the machine's PID space; combine deterministic identity substitution with real unrelated process/group sentinels that must survive.

## Secure-by-spec negative contracts and remaining evidence

| Axis | Scheduler-specific negative contract |
|---|---|
| Identity | A PID, stale timestamp, inherited marker, or absent caller cannot authorize release or terminating another process. |
| Authorization | Status/where do not start guards, import repository code, or install dependencies; a guard without a valid durable grant cannot execute. |
| Tenancy | Symlink aliases serialize one checkout; different worktrees retain separate state; one run cannot release another run's named lease. |
| Input | Oversized frames/IDs/queue entries, unsafe roots, malformed schema, and protocol mismatch fail before execution. |
| State | Every crash/registration/cancel transition preserves charged work or uncertainty; checkout serialization extends through artifact publication. |
| Exposure | Ledger/status/control diagnostics omit arbitrary argv and environment; process observation does not collect them. |
| Availability | Queue, transaction waits, polls, metadata, inspection, and cancellation are bounded; uncertainty never enables uncapped fallback. |
| Dependencies | SQLite locking requires a supported filesystem; psutil observation is not containment; existing legacy ptest does not participate in NG admission. |

No tests, security scanners, installation checks, benchmarks, subprocess smokes, or macOS runs were executed during this specification-only research. The sources support feasibility and limits, not passing acceptance evidence. The guard startup protocol, filesystem capability validation, exact supported versions, signal timing, unknown-memory throughput cost, and all A2–A4 cases remain implementation/release gates.
