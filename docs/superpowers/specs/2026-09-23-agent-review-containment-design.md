# Agent-review process containment amendment

Status: proposed design, revision 2, **not approved for implementation**. This
document supersedes the process-group ownership contract in the
[agent-backed doctor design](2026-09-22-agent-doctor-design.md). Provider review
remains disabled. Normal `ptest` execution, `doctor --offline`, and legacy
doctor modes create no namespaces or cgroups and remain usable.

Revision 1 proposed a per-review cgroup-v2 leaf. A review of that design against
kernel semantics and the reference host rejected it as the release boundary
(see [Rejected: cgroup-v2 leaf](#rejected-cgroup-v2-leaf-as-the-boundary)).
Revision 2 uses a per-review user + PID + mount namespace owned by a trusted
ptest init.

## Claim and threat model

The containment claim is exactly this: **every process descending from a
provider launch has exited before ptest reports the review finished, before
ptest exits normally, and within a bounded time after ptest dies for any
reason.** A second ptest run's review is never signalled, absorbed, or waited
on by this one.

The provider is trusted as the user's own installed tool, run with no agent
tools as the base spec requires. The failure being contained is a leak: a
provider or helper calling `setsid`, double-forking, daemonizing, holding
output open, ignoring `SIGTERM`, or letting its direct child exit 0 while
descendants live. Bounded output and leader exit are not completion signals.

Non-claim: ptest does not sandbox a hostile same-UID provider. Such a process
can already read and write the user's files and signal the user's other
processes outside ptest. The namespace makes escaping the kill boundary
impossible, which is what the claim needs. It does not protect the host from
provider behaviour inside that boundary, and the report must not say it does.

## Kernel facts this design relies on

Each fact is to be re-proved by the qualification tests below, not assumed:

- A process cannot leave its PID namespace. `setsid`, double-fork, reparenting,
  and nested `unshare(CLONE_NEWPID)` keep every descendant inside the review
  namespace, because nested namespaces are descendants of it.
- When the namespace's init (PID 1) exits, the kernel sends `SIGKILL` to every
  process in the namespace, nested namespaces included. The init's parent can
  reap it only after the namespace has released all its PIDs, so reaping the
  init proves quiescence without polling `/proc` or cgroup files.
- PID 1 of a namespace ignores signals for which it has no handler when they
  come from inside the namespace. A provider therefore cannot kill the init
  early and orphan anything, because orphaning is impossible anyway.
- An open file descriptor's kernel checks use the credentials captured when it
  was opened. Any descriptor ptest leaks into the provider carries ptest's
  authority. Every ptest-side descriptor is `O_CLOEXEC`, and the launch closes
  everything except the provider's three stdio pipes.
- Creating the namespaces needs a single-threaded caller and unprivileged user
  namespaces. Hosts restrict these in several ways: Ubuntu's
  `kernel.apparmor_restrict_unprivileged_userns=1`, Debian's
  `kernel.unprivileged_userns_clone=0`, `user.max_user_namespaces=0`, default
  container seccomp profiles, and masked `/proc` (`mount_too_revealing`).
  All of these are detected by attempting the real bootstrap, never inferred.

Reference-host observation, 2026-09-23 (kernel 7.2, not a qualification
record): the interactive shell runs in the root-owned
`user.slice/user-1000.slice/session-9.scope`; `cgroup2` is mounted with
`nsdelegate`; unprivileged user namespaces are enabled; Yama `ptrace_scope=1`;
`unshare --user --map-current-user --pid --fork --mount --mount-proc` ran a
child as PID 1 with only its own processes visible.

## Required lifecycle

Processes: ptest (**P**) spawns bootstrap **B**, a fresh
`sys.executable -I -m ptest._review_init` that is single-threaded and ptest-owned.
B runs `unshare(CLONE_NEWUSER | CLONE_NEWPID | CLONE_NEWNS)`. Python 3.11 has
no `os.unshare`, so B uses the libc call on 3.11 and `os.unshare` on 3.12+.
B writes `setgroups deny` and maps exactly the caller's UID and GID. B then
forks **I**, which is PID 1 of the new namespace, and I forks and execs the
provider **R**. P holds a pidfd for B, taken with `os.pidfd_open` while B is an
unreaped child, so the PID cannot be reused. No numeric PID of anything else
is ever signalled.

1. **Preflight is the bootstrap.** Before R can exec, I does the following:
   makes `/` recursively private, mounts a fresh `/proc` for the new namespace,
   closes every descriptor except R's stdio and the two control pipes, and
   reports readiness to P over a status pipe. Any failure is reported over the
   status pipe as `provider-unavailable` with the failing step. The failing
   steps include unsupported OS, `EPERM`/`EINVAL`/`ENOSPC` from `unshare`, a
   refused map write, a refused `/proc` mount, or a missing readiness report
   within a short bound. In every such case R is never executed. Non-Linux hosts
   fail the same way before any spawn.
2. **Born contained.** R is forked by I after the namespaces exist, so no
   provider code ever runs outside them. Post-exec migration is impossible and
   is not used. R runs under the caller's own UID. When that UID is not root, R
   holds no capabilities after `execve`. Any user namespace R creates for itself
   inherits the mounts locked, so R cannot uncover the host `/proc`.
3. **Lifeline.** P holds the write end of a lifeline pipe, and B and I hold the
   read end. I runs a minimal init loop: it reaps children, watches the
   lifeline, and enforces an absolute hard deadline equal to the review timeout
   plus the teardown bound. P passes that deadline in, and I enforces it even if
   P is alive but stuck. When R exits, I relays R's status over the status pipe
   and keeps running, because descendants may outlive R. I exits only on
   lifeline EOF or at the deadline. A
   crashed, killed, or exec'd-away P closes the write end in the kernel, so I
   exits without ptest code running. I also sets `PR_SET_PDEATHSIG(SIGKILL)`
   against B as a secondary path. B is single-threaded, so the thread-exit
   pitfall of pdeathsig does not apply. The parent-alive race check uses the
   lifeline, not `getppid()`, which returns 0 inside the namespace.
4. **Teardown.** Normal leader exit, timeout, Ctrl-C, output cap, invalid
   response, and any post-bootstrap launch failure all take the same path. P
   closes the lifeline, which makes I exit and the kernel kill the namespace. P
   then waits a bounded 5 s to reap B. B exits only after reaping I, and I can
   be reaped only after the namespace is empty, so **a reaped B that reports
   "init reaped" is the quiescence proof.** If B is not reaped within the bound,
   P sends `SIGKILL` to B through its pidfd and waits a further bounded 5 s.
   A force-killed B does not prove quiescence. Pdeathsig and I's deadline still
   bound the namespace's lifetime, but the run is recorded as
   `cleanup-incomplete` and never as success. No broader target is ever
   signalled.
5. **Concurrency.** Each review gets its own B, I, and namespace set. Namespaces
   are not addressable by name, so two ptest runs cannot target each other's
   reviews. Child reviews remain sequential as in the base spec.

**Exit codes**, decided rather than deferred, applying the base contract's
precedence of Ctrl-C `130`, then timeout `124`, then review error `2`:
- An interrupt returns 130 and a timeout returns 124 even when cleanup was
  incomplete. The `cleanup-incomplete` diagnostic is still emitted.
- A review that would otherwise succeed but whose cleanup is incomplete exits 2
  with the diagnostic. Cleanup failure is never converted to success.

Progress stays phase- and elapsed-based, with no fake percentage.

No ptest daemon, sudo, setuid helper, systemd unit, package install, external
`unshare`/`bwrap` dependency, or process-group fallback is added. The
pidfd/session-group machinery in `src/ptest/agent_providers.py` and the
unmerged `/proc` descendant scan in the `ptest-provider-quiescence` task
worktree are superseded, not extended.

## Rejected: cgroup-v2 leaf as the boundary

- **No delegated parent is reachable unprivileged from a login session.**
  Migration needs write access to the destination's `cgroup.procs` and to that
  of the common ancestor. On the reference host the common ancestor of the
  session scope and the user's delegated `user@1000.service` tree is the
  root-owned `user-1000.slice`, so ptest can neither create a leaf beside
  itself nor move a process into the delegated tree. The only unprivileged
  route is a systemd transient unit, which this design excludes.
- **Same-UID ownership separation (revision 1, option 2) is unattainable.**
  The owner of a cgroupfs inode can `chmod` it back. Real separation needs a
  second UID, which means privilege.
- **A cgroup namespace (revision 1, option 1) confines migration only.** The
  kernel rejects `cgroup.procs`/`cgroup.threads` moves outside the writer's
  namespace root. It does not restrict writes to other interface files, such as
  an ancestor's or sibling's `cgroup.kill` or `cgroup.subtree_control`, through
  any still-visible host cgroupfs. `nsdelegate` protects only the namespace
  root's own non-delegatable files. Closing that gap needs the same user, PID,
  and mount namespaces as revision 2, at which point the cgroup adds nothing to
  the claim.
- **The kernel does not kill a populated cgroup when its manager dies,** and
  `clone3(CLONE_INTO_CGROUP)` has no safe path from multi-threaded CPython on
  the supported 3.11–3.14 range.
- A cgroup may be reconsidered later purely for resource accounting (for
  example `pids.max`). It is outside this release gate.

## Qualification and negative twins

Deterministic tests use fake provider executables inside real namespaces and
never launch a real provider. They must prove each of the following cases:

- **Leak shapes.** `setsid`; double-fork; a daemon that ignores
  `SIGTERM`/`SIGHUP`; leader exit 0 with a live descendant holding stdout open;
  a fork storm during teardown; a descendant in a nested user or PID namespace.
- **Lifetime.** P `SIGKILL`ed mid-review, where every descendant must be gone
  within the bound, observed from outside through a descendant-held pipe
  reaching EOF rather than a `/proc` scan. P alive but stalled past the hard
  deadline. B force-killed, which must record `cleanup-incomplete`.
- **Descriptor hygiene.** A fake provider enumerates its own open descriptors
  and finds only stdio. The lifeline and status pipes are absent.
- **Fail-closed preflight.** Each of the following yields
  `provider-unavailable` with no provider exec, proven by a sentinel that the
  fake provider would have written: an injected `unshare` failure, a map-write
  refusal, a `/proc` mount refusal, a missing readiness report, and a non-Linux
  platform.
- **Tenancy.** Two concurrent ptest runs with overlapping reviews. Tearing
  down one leaves the other's provider running and completing.
- **Paths.** Normal completion, timeout, Ctrl-C, output cap, and invalid
  response, including the exit-code precedence above.

No test may establish quiescence from a process-group signal or a `/proc`
snapshot. Suites that need real namespaces skip with an explicit reason on
hosts that refuse unprivileged user namespaces. They must run on the reference
host before review can be enabled.

The all-three Claude/Codex/OpenCode qualification gate in the base spec is
unchanged, including its existing account limitation. It additionally has to
show that each real provider authenticates and completes inside the namespace:
network, credential files, and session-bus keyring access unchanged, and
nested sandboxes such as a provider's own `bwrap` working.

## Open prerequisites before approval

1. User approval of this revised boundary and its non-claim.
2. A qualification record for the reference host covering the kernel facts
   above, including pdeathsig delivery to a namespace init from its parent.
3. A decision on hosts without unprivileged user namespaces, macOS included.
   The proposal returns `provider-unavailable` there, so review is Linux-only.
4. A CI or reference host that can run the namespace suites.

Until these are resolved and audited, **provider review stays disabled**. Child
v1 `.ptest.toml` files remain authoritative and unchanged.

References: [`pid_namespaces(7)`](https://man7.org/linux/man-pages/man7/pid_namespaces.7.html)
for init termination and the PID-1 signal rules;
[`user_namespaces(7)`](https://man7.org/linux/man-pages/man7/user_namespaces.7.html)
and [`mount_namespaces(7)`](https://man7.org/linux/man-pages/man7/mount_namespaces.7.html)
for maps, capabilities on `execve`, and locked mounts;
[`PR_SET_PDEATHSIG(2const)`](https://man7.org/linux/man-pages/man2/PR_SET_PDEATHSIG.2const.html);
[Linux cgroup v2 documentation](https://www.kernel.org/doc/html/latest/admin-guide/cgroup-v2.html)
for delegation containment, migration permission, and `nsdelegate`.
