# ptest Remote Result Deduplication and Full-Gate Batching

Date: 2026-08-03
Status: proposed — direction approved; written review pending

## 1. Problem

`ptest` correctly moves large suites away from a shared development machine, but
it treats every invocation as new work. On 2026-08-03 `ptest doctor` reported 26
remote API runs and 40 remote frontend runs. Several identical frontend full
suites overlapped. That wastes Cloud Run compute and makes every caller wait for
another cold start and another copy of the same tests.

The source of truth cannot be the Git commit alone. Agents routinely test dirty
worktrees containing tracked edits, untracked tests, and gitignored lockfiles.
Two worktrees at the same commit may therefore require different executions,
while two different worktrees may ship byte-identical inputs and should share
one.

## 2. Goals

1. Identical remote requests already in flight share one Cloud Run execution.
2. An identical successful result may be reused for exactly one hour after the
   execution completes.
3. Failures, infrastructure errors, interrupted executions, and exit 75 are
   never result-cached.
4. Only the owner of a newly claimed request consumes the daily-run budget or a
   remote concurrency slot. Joiners and cache hits do neither.
5. Existing local fallback, secret stripping, exit codes, summarized remote
   output, per-project commands, and worktree behavior remain intact.
6. Development uses scoped `ptest` runs; a final tree state receives one
   coverage-enabled `ptest --full` gate.

## 3. Non-goals

- No warm Cloud Run service, persistent VM, or Spot VM in this change. A
  preemption-safe Spot runner remains a future backend option.
- No test-result reuse across different commands, runner configurations, source
  trees, or runner-image namespaces.
- No caching of red results, even briefly.
- No distributed Vitest or pytest sharding in the first implementation. That is
  a benchmark-gated escalation described in section 11.
- No reliance on local wall-clock measurements while other agent workloads are
  active on the workstation.

## 4. Request identity

`ptest` computes a SHA-256 request key from a versioned canonical record:

```text
schema: ptest-remote-key-v1
project: configured project name
kind: pytest | vitest | npm | go | cargo
command: exact remote shell command
job: Cloud Run job name
region: configured region
gcp_project: configured GCP project
runner_namespace: configured cache namespace
tree_digest: SHA-256 of the exact source payload
```

One shared source-manifest function feeds both the tree digest and `pack_tree`,
so the two cannot drift. In a Git checkout it includes tracked files, untracked
non-ignored files, and re-added lockfiles while excluding secret dotenv files.
Tracked files remain included regardless of directory name, matching Git's
source-of-truth behavior. The non-Git fallback applies the existing static
build/cache exclusions. Records are ordered by normalized relative path and
hash `path`, file type, executable bit, length, and bytes.
Archive gzip timestamps and filesystem mtimes are deliberately absent, so two
byte-identical worktrees produce the same digest.

Symlinks hash their link target and are archived as symlinks. Regular files hash
their contents. A staged-but-deleted path contributes nothing, matching
`pack_tree`.

`runner_namespace` is mandatory for cache-enabled projects and defaults to
`v1` in generated configuration. Provisioning scripts increment it whenever a
runner image or job behavior changes. This explicit namespace avoids pretending
that a mutable `:latest` image has a stable digest. Lockfile changes already
invalidate the tree digest.

## 5. GCS coordination layout

The existing private source bucket also holds small coordination objects:

```text
coord/v1/<request-key>/claim.json
coord/v1/<request-key>/execution.json
coord/v1/<request-key>/pass.json
sources/<tree-digest>.tar.gz
```

- `claim.json` contains a random owner ID, creation time, and lease expiry.
- `execution.json` contains the Cloud Run execution resource name, owner ID,
  source object, command, and submission time.
- `pass.json` contains completion time, one-hour expiry, execution name, and the
  exact summarized stdout that a normal successful remote run would print.
- The content-addressed source object is uploaded once and may be reused by an
  identical owner. Existing bucket lifecycle deletion remains the final cleanup.

Coordination objects contain no source text, environment values, credentials,
or unhashed command line. The execution manifest stores the command hash, never
the command itself.

## 6. Atomic ownership

Clients claim a missing request with a GCS create-only precondition
(`ifGenerationMatch=0`). Exactly one client wins:

1. Every caller packages or inventories the tree and computes the request key.
2. A fresh, unexpired `pass.json` returns immediately with exit 0 and its stored
   digest, prefixed by a concise `[ptest] reused passing result ...` notice.
3. A live `execution.json` makes the caller a joiner. It waits for that execution
   and returns the same exit status and summarized output as the owner.
4. Otherwise the caller attempts the create-only claim.
5. A losing caller polls briefly for `execution.json`; it must not submit a
   second job during the claim-to-execution race.
6. The winner alone checks daily budget and concurrency, uploads a missing
   content-addressed source object, and submits the job.

The claim lease uses `remote_claim_ttl_seconds`, default 2100 seconds: the
current 30-minute task timeout plus five minutes. Configuration validation
requires the lease to exceed the Cloud Run task timeout.
A caller encountering an expired claim first checks whether its recorded
execution still exists or is running. It may steal the claim only when no live
execution exists. Claim replacement uses a generation-match precondition so an
old owner cannot delete or overwrite a newer lease.

## 7. Asynchronous Cloud Run submission

In-flight sharing requires the execution name while the job is running. The
current synchronous `gcloud run jobs execute --wait` call therefore becomes:

1. Submit asynchronously and parse the exact execution name.
2. Write `execution.json` immediately.
3. Poll `gcloud run jobs executions describe` until terminal state.
4. Fetch Cloud Logging output with the existing retry behavior.
5. Summarize output with the existing `summarize_remote_logs` function.
6. On success, create/replace `pass.json` with an expiry exactly 3600 seconds
   after completion.
7. On test failure, print the failure digest and return 1 without `pass.json`.

Joiners execute steps 3–7 against the shared execution. Multiple successful
joiners may race to write the same passing record; conditional replacement makes
that harmless. If the original client exits after submission, another caller
can still discover and join the execution through GCS.

## 8. Failure behavior

| Condition | Required behavior |
|---|---|
| Fresh passing record | Print reuse notice + stored digest; return 0 |
| Identical execution running | Join it; return its real result |
| Shared execution fails tests | Every waiter returns 1; no result cache |
| Owner hits concurrency ceiling | Remove only its own claim; return 75 |
| Owner exhausts daily budget | Remove only its own claim; use existing local fallback |
| Upload/submission fails before execution exists | Remove own claim; use existing capped fallback |
| Polling temporarily fails | Retry; never launch a duplicate while lease is live |
| Claim is stale and execution absent | Atomically steal and submit once |

Cache lookup and joining happen before the concurrency and daily-budget checks.
This is essential: sharing existing work must remain possible when the backend is
otherwise full.

## 9. Security and permissions

Test code continues to run under a service account with read-only bucket access;
it cannot forge passing records. Only authenticated `ptest` operators create or
replace coordination objects. The bucket remains private with public-access
prevention.

All object names use SHA-256 keys and validated fixed prefixes. Project names,
paths, shell fragments, and branch names never become unvalidated object paths.
The existing dotenv exclusion contract is tested against both packing and tree
digest calculation so a secret can neither ship nor influence a visible key.

## 10. Full-gate batching

Deduplication is the enforcement mechanism; documentation supplies the workflow:

- During TDD, run the smallest relevant paths or filters through `ptest`.
- Run `ptest --full` once after the final tree state is ready.
- Keep `ptest --full` coverage-enabled. Batching makes its fixed coverage cost a
  one-time final gate rather than a repeated development tax.
- If several agents independently request that same final state, they join or
  reuse one execution automatically.

README, example configuration, and `ptest doctor` document the one-hour TTL,
cache namespace, and whether cache coordination is healthy. A cache failure is
an optimization failure: warn once and continue through the existing uncached
remote path, while retaining the concurrency safety check.

## 11. Benchmark-gated distributed sharding

The frontend first benchmarks two concurrent Vitest shards inside its existing
8-vCPU/16-GiB task. Only if the median container-reported Vitest duration remains
above 120 seconds after suite optimizations may a follow-up add two Cloud Run
tasks at 4 vCPU/8 GiB each.

That follow-up must preserve a single `ptest` result by:

- assigning stable `1/2` and `2/2` shards;
- persisting both blob and coverage artifacts;
- merging once and enforcing global coverage thresholds after the merge;
- returning failure if either shard or the merge fails;
- including shard topology in the request key.

The warm-runner question remains independent and out of scope.

## 12. Testing

All tests run through `ptest`.

Pure unit tests cover:

- deterministic tree digests across mtimes, worktree paths, and archive runs;
- dirty tracked files, untracked files, ignored lockfiles, deleted paths,
  symlinks, executable bits, excluded caches, and excluded dotenv secrets;
- request-key changes for every identity field;
- fresh/expired pass records and the exact one-hour boundary;
- cache hits bypassing budget, upload, concurrency checks, and submission;
- one atomic winner with multiple simulated callers;
- joiners during the claim-to-execution race;
- stale-claim recovery without stealing a live execution;
- success caching and the prohibition on caching all nonzero outcomes;
- generation-safe cleanup and fallback behavior;
- preserved exit 0/1/2/75 semantics and summarized output.

One manual smoke test after installation uses two processes to request the same
small remote command simultaneously. Cloud Run must show one execution, both
callers must return the same result, and a third call within one hour must reuse
the passing record without a new execution.

## 13. Acceptance criteria

1. Two identical concurrent remote calls create exactly one Cloud Run execution.
2. A successful identical call completed less than 3600 seconds ago creates no
   execution and returns 0 with the stored summary.
3. At 3600 seconds or later, a new execution is required.
4. Dirty-tree differences, command differences, runner namespace changes, and
   shard topology changes cannot collide.
5. Failures and infrastructure errors are never reused.
6. Cache coordination failure does not weaken the remote concurrency ceiling or
   cause an uncapped local full run.
7. Existing focused `ptest` tests and one final `ptest --full` are green.
8. No warm compute resource is created.
