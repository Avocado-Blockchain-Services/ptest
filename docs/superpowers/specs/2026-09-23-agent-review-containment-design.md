# Agent-review process containment amendment

Status: proposed design, **not approved for implementation**. This document
supersedes the process-group ownership contract in the
[agent-backed doctor design](2026-09-22-agent-doctor-design.md). Provider review
remains disabled. Normal `ptest` execution, `doctor --offline`, and legacy
doctor modes do not create cgroups and remain usable.

## Boundary and threat

For an optional, separately consented doctor review, each provider and every
descendant must remain in one task-owned Linux cgroup-v2 subtree from before
provider `exec` until final teardown. PID/PGID ancestry, `/proc` descendant
enumeration, subreapers, and process-group signals are not containment. A
provider may call `setsid`, double-fork, or let its direct leader exit while a
child remains. Its bounded output is not a completion signal.

Suppose ptest owns delegated parent **D** and creates review leaf **A**. A
same-UID provider in A may be able to write `D/cgroup.procs` and move into a
sibling, so `A/cgroup.kill` alone cannot prove per-run isolation. Before
provider launch, qualification must establish one of these boundaries:

1. A cgroup namespace rooted at A, paired with a mount namespace that exposes
   only A's cgroup view, under kernel delegation semantics that reject migration
   beyond A; or
2. A host-managed ownership separation that denies the provider write access
   to D and every outside destination/common-ancestor `cgroup.procs` while
   retaining ptest's ability to manage A.

The provider must have no route to regain host cgroup privileges. The precise
namespace/UID provisioning mechanism is **not yet qualified**. Mount flags,
permissions, namespace support, and attempted escape must be checked on the
actual host, not inferred from Linux or systemd presence. A cooperative canary
inside A must attempt migration only into a task-owned scratch sibling and
report/exit; success disqualifies the host. The canary must be reaped without
signalling a numeric PID that might be reused. No other run's leaf is a probe
target. A setup-time canary alone cannot prove permissions remain unchanged
during review; the implementation plan must address that residual before launch
is enabled.

## Required lifecycle

The review path fails closed **before provider exec** unless it can verify a
delegated cgroup-v2 parent, create a unique task-owned child without following
symlinks or adopting an existing name, open a stable handle to that child,
access `cgroup.kill` and `cgroup.events`, establish the escape boundary, and
provide a qualified hard lifetime after ptest dies. The provider must be born
in A (for example through qualified `clone3(CLONE_INTO_CGROUP)`) or held in a
trusted pre-exec bootstrap that joins A before any provider code can execute or
fork. The chosen primitive and required kernel/privilege matrix need a separate
qualification record; a post-exec migration is forbidden.

All cgroup operations use the verified task-owned directory/handle and never
the parent, user slice, another run's leaf, process name, or a stale path. Each
child review is sequential as in the base spec; concurrent ptest runs use
separate leaves. The implementation must specify how a held handle and identity
check prevent name replacement, and fail closed if ownership cannot be
re-established. It must not rely on a bare inode number alone as an eternal
identity.

On normal leader exit **as well as** timeout, Ctrl-C, output exhaustion,
invalid response, and launch failure after containment, ptest writes `1` to
the owned `cgroup.kill`, waits a bounded time for `cgroup.events` to report
`populated 0`, reaps the direct child, and removes only the empty owned leaf.
Cleanup failure is reported as incomplete and never converted to success or
retargeted to a broader cgroup. Preserve the base command's exit-code contract:
review errors exit 2, timeout 124, and Ctrl-C 130, with a cleanup-failed
diagnostic when relevant; the exact precedence when timeout/interrupt and
cleanup failure coincide belongs in the implementation plan. Progress remains
phase/elapsed based, with no fake percentage.

The kernel does **not** automatically kill a populated cgroup when ptest
crashes. Launch therefore requires an independently enforced, bounded host
lifetime that kills only A if the controller vanishes; otherwise review is
unavailable. A systemd transient unit may be investigated as an optional
existing host manager, but its exact scope, kill semantics, lifetime guarantee,
and no-cross-run identity proof are unqualified. No ptest daemon, sudo,
automatic package install, hidden host provisioning, or process-group fallback
is added. Unsupported Linux hosts and non-Linux hosts return an actionable
`provider-unavailable`/`provider-unqualified` review error (exit 2) before
launch; offline doctor and normal test execution remain unaffected.

## Qualification and negative twins

Before implementation can enable review, deterministic tests and reference-host
evidence must cover: `setsid`; double-fork; leader exit 0 with a live child;
concurrent fork during kill; attempted migration from A to a task-owned sibling;
canary success/failure; normal completion; timeout; Ctrl-C; output cap;
permission loss; cgroup path replacement; `cgroup.kill` or event read failure;
`populated` never reaching zero; controller crash with a surviving child; and
two concurrent ptest runs whose leaves cannot kill or absorb each other's
processes. No test may rely only on a process-group signal or `/proc` snapshot
to establish quiescence. Tests must also prove an unsupported host launches no
provider. The all-three Claude/Codex/OpenCode qualification gate in the base
spec remains unchanged, including its existing account limitation.

Open prerequisites before approval: a concrete host-backed delegation and
parent-death lifetime mechanism; a race-safe launch primitive available from
ptest's supported Python/package contract; a namespace or ownership boundary
that stays effective for the whole run; bounded cleanup timing and exit-code
precedence; and a CI/reference host that can execute the adversarial cases.
Until these are resolved and audited, **provider review must stay disabled**.
Child v1 `.ptest.toml` files remain authoritative and unchanged.

References: [Linux kernel cgroup v2 documentation](https://www.kernel.org/doc/html/latest/admin-guide/cgroup-v2.html)
for delegation, migration, `cgroup.kill`, and `cgroup.events`;
[`proc_tid_children(5)`](https://man7.org/linux/man-pages/man5/proc_tid_children.5.html)
for why an unfrozen `/proc` child list is not a reliable ownership proof.
