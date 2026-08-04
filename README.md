# ptest — one test command for every project, every agent

A dispatcher that stops parallel test runners from eating a shared workstation,
and sends full-suite runs to Cloud Run Jobs with a database living in RAM.

```
ptest <paths / -k args>    scoped run — local and workers-capped, unless the path
                           covers most of the suite (see Three tiers below)
ptest --full               whole suite — remote if configured, else local (capped)
ptest --full --fresh       force a new remote execution (benchmarking only)
ptest where                what resolved for this directory
ptest doctor               config + backend health
ptest register             print a config stanza for the repo you are in
```

**The problem it solves.** `pytest -n auto`, `vitest`, `jest` and friends each
grab roughly every core. Several concurrent sessions auto-detecting the same
machine is how load average reaches 40 while everything swaps. ptest caps workers
per run, so six agents at 2 workers each beat three agents at twelve — and routes
full-suite runs off-box entirely.

### Three tiers, not two

| invocation | where |
|---|---|
| `ptest tests/api/test_x.py`, `ptest -k foo` | local, capped to `workers` |
| `ptest tests/api` (≥ `remote_scoped_min_files` files) | backend, scoped command, no coverage gate |
| `ptest --full` | the project's `full` command, on its configured backend — coverage gate only if that command sets one |

The middle tier exists because a scoped run can be full-suite-sized: in persea-api,
`tests/api` alone is roughly two-thirds of the whole test suite. A `-k`/`-m`/`--lf`
filter always keeps a run local — a filter means you are hunting one failure and
want the fast loop. `ptest --local <path>` forces local for anything.

A routed run's output is not identical to a local one: what comes back is the
summarized remote digest (counts, coverage, and — on a red run — the full FAILURES
section), not the streamed, verbatim output a local run gives you. It also uploads
the working tree to the bucket, the same packing and `.env`-stripping `--full` already
does. `ptest --local <path>` keeps both the run and its full output on this machine.

### Fast development workflow

Use the smallest scoped `ptest` path or filter for each red/green cycle, then run
one `ptest --full` after the final tree state is ready. Keep coverage in the
configured `full` command: batching makes that fixed cost one final gate instead
of charging it on every edit.

Identical remote requests coalesce automatically. The identity includes the
exact command, runner namespace, job configuration, and the bytes/permissions of
tracked files, dirty edits, untracked files, symlinks, and ignored lockfiles. A
caller joins an identical execution already in flight; a passing result is reused
for exactly 3600 seconds after Cloud Run reports completion. Failures and runner
errors are never cached. Cache hits and joiners do not consume the daily budget
or concurrency slots.

`runner_namespace = "v1"` is the explicit runner-image invalidation switch:
increment it whenever a mutable runner image or job behavior changes. `ptest
where` shows the namespace and TTL; `ptest doctor` verifies coordination access.
Use `--fresh` only for controlled remote benchmarks—it bypasses passing-result
reuse, gets a unique request identity, and still obeys budget/concurrency guards.

## → [`outsource_tests.md`](outsource_tests.md) ←

**The design document.** Parallel-safe per-worker test databases, the worktree
naming scheme that stops concurrent checkouts destroying each other's data, the
remote-execution architecture, Terraform, and a long list of gotchas with the
failure each one prevents. Written so a stranger — human or model — can
replicate the whole setup on a similar stack.

## Layout

| Path | What |
|---|---|
| `ptest` | The dispatcher. Single file, no dependencies beyond Python 3.11+. |
| `config.example.toml` | Sanitised config; real one lives at `~/.config/ptest/config.toml` |
| `Dockerfile.pytest` | Runner image: python + postgres 15 + uv |
| `Dockerfile.vitest` | Runner image: node 20, no database |
| `entrypoint.sh` | Shared: fetch → boot DB → install deps → run → exit with its code |
| `cloudbuild.*.yaml` | Image build configs |
| `terraform/` | Bucket, runner service account, both Cloud Run Jobs |
| `provision.sh` | Imperative equivalent of the Terraform, plus the image build |
| `provision-vitest.sh` | Adds the node image + job, reusing bucket and SA |
| `smoke.sh` | End-to-end runner check; opt-in live owner/joiner/cache proof |

## Install

```bash
cp ptest ~/.local/bin/ptest && chmod +x ~/.local/bin/ptest
cp config.example.toml ~/.config/ptest/config.toml   # then edit
ptest doctor
```

## Remote backend

Build the images, apply the Terraform, then flip a project to `backend = "cloudrun"`.
Full walkthrough in [`outsource_tests.md`](outsource_tests.md#part-4--terraform).

**Verify with `smoke.sh` before flipping.** A soft fallback to local means a
broken remote backend looks exactly like a working one — that failure mode kept
the backend dormant for months in the original setup.

After enabling coordination, run its live proof once from this repository:

```bash
PTEST_LIVE_DEDUP_SMOKE=1 ./smoke.sh
```

It launches two identical `ptest --full` callers, requires both to pass, checks
that exactly one new Cloud Run execution appeared, and makes a third call that
must reuse the joined passing execution. Override `PTEST_LIVE_REPO`, `PTEST_JOB`,
or `PTEST_BIN` for a different registered project or candidate dispatcher. The
mode is deliberately opt-in because it performs a real remote full command.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | tests passed |
| 1 | tests failed |
| 2 | ptest itself could not run (no command known, deps failed) |
| **75** | **remote job at its concurrency ceiling — NOTHING RAN.** Not a test failure. |

75 is `EX_TEMPFAIL`. It deliberately does **not** fall back to a local full run:
with several agents blocked at once that would be the exact meltdown this tool
exists to prevent. A heavy scoped run competes for the same slots as `--full`
and can return 75 too, so "use a scoped run" only helps if it's small enough
to stay local (a narrower path, or a `-k`/`-m`/`--lf` filter). Retry later,
scope it tighter, or pass `--local` to force this run onto the machine.

## Notes

- `provision*.sh` carry defaults from the environment they were written for.
  Override with `PTEST_GCP_PROJECT`, `PTEST_BUCKET`, `PTEST_ACCOUNT`, `PTEST_REGION`.
- ptest never inherits `gcloud config set account`; the account is explicit in
  config and passed on every CLI call. On a machine that touches several
  clients, inheriting it silently authenticates as the wrong identity.
- After editing `config.toml`, re-parse it. Invalid TOML makes ptest fall back to
  local defaults for **every** project, with only a warning line.
- This design does not keep compute warm. Cloud Run Job cold starts remain; a
  persistent or Spot-backed runner is a separate future backend choice.
