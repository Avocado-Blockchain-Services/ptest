# ptest — one test command for every project, every agent

A dispatcher that stops parallel test runners from eating a shared workstation,
and sends full-suite and heavy-scoped runs off-box when the project has a remote
backend to send them to.

```
ptest <paths / -k args>    scoped run — local and workers-capped, unless the path
                           covers most of the suite (see Three tiers below)
ptest --full               whole suite — remote when the project has a backend,
                           otherwise here, capped to `workers`
ptest --full --fresh       force a new remote execution (benchmarking only)
ptest status               what is running here and off-box, and what is wrong
ptest where                what resolved for this directory
ptest doctor               config + backend health
ptest register             print a config stanza for the repo you are in
ptest spot-status          Spot queue capacity (Spot is switched off; see below)
ptest spot-result <key>    inspect one completed Spot request
```

**Spot is retired, not removed.** `spot_queue.py`, `spot_worker.py`,
`spot_controller.py`, their Terraform and their tests all remain; the backend is
gated behind `spot_enabled` (default `false`) and its infrastructure is torn
down. Setting `spot_enabled = true` is all the code needs to come back — but the
MIG, controller and workers have to be redeployed first, or submitted work will
queue for a collector that does not exist.

**`ptest status` is for deciding how to run**, on a machine several agents
share. It reports load, in-flight ptest runs and their commands, test runners
nobody registered (found by walking the parent chain, since names and cwd lie),
today's remote budget, and `--full` commands naming paths that no longer exist.
It exits 1 when any of that needs attention.

**The problem it solves.** `pytest -n auto`, `vitest`, `jest` and friends each
grab roughly every core. Several concurrent sessions auto-detecting the same
machine is how load average reaches 40 while everything swaps. ptest caps workers
per run, so six agents at 2 workers each beat three agents at twelve — and routes
full-suite runs off-box entirely.

### Three tiers, not two

| invocation | where |
|---|---|
| `ptest tests/api/test_x.py`, `ptest -k foo` | local, capped to `workers` |
| `ptest tests/api` (≥ `remote_scoped_min_files` files) | remote, scoped command, no coverage gate |
| `ptest --full` | remote, using the project's `full` command |

"Remote" means the project's configured backend. A project with
`backend = "local"` — or no stanza at all — has nowhere to route to, so both
lower tiers collapse to a capped local run rather than failing.

The middle tier exists because a scoped run can be full-suite-sized: in persea-api,
`tests/api` alone is roughly two-thirds of the whole test suite. A `-k`/`-m`/`--lf`
filter always keeps a run local — a filter means you are hunting one failure and
want the fast loop. `ptest --local <narrow-path>` stays local; it cannot bypass heavy scoped or full work.

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
| `ptest` | The dispatcher. No dependencies beyond Python 3.11+ for local and Cloud Run use. |
| `spot_queue.py` | Companion module required by the opt-in Spot backend. |
| `install.sh` | Atomically installs `ptest` and its Spot companion together. |
| `config.example.toml` | Sanitised config; real one lives at `~/.config/ptest/config.toml` |
| `Dockerfile.pytest` | Runner image: python + postgres 15 + uv |
| `Dockerfile.vitest` | Runner image: node 20, no database |
| `entrypoint.sh` | Shared: fetch → boot DB → install deps → run → exit with its code |
| `cloudbuild.*.yaml` | Image build configs |
| `terraform/` | Bucket, Cloud Run jobs, and static opt-in Spot queue resources |
| `provision.sh` | Imperative equivalent of the Terraform, plus the image build |
| `provision-vitest.sh` | Adds the node image + job, reusing bucket and SA |
| `smoke.sh` | End-to-end runner check; opt-in live owner/joiner/cache proof |

## Install

```bash
./install.sh
cp config.example.toml ~/.config/ptest/config.toml   # then edit
ptest doctor
```

Pass a destination as the first argument to install somewhere other than
`~/.local/bin`. The installer builds a versioned `ptest`/`spot_queue.py` bundle
and atomically switches the executable symlink, so an interrupted upgrade keeps
the previous complete pair. `ptest doctor` rejects a configured Spot backend
when its companion is missing or ABI-incompatible. Local and Cloud Run backends
remain usable with the historical single-file `ptest` layout.

## Inspect a Spot result

`ptest status` reports queue capacity. To inspect one completed remote request,
use the request key printed at submission time:

```bash
ptest result <request-key>          # status, exit code, and completion time
ptest result <request-key> --output | less
```

Both commands only read the durable result record; neither acknowledges a
delivery nor changes worker capacity.

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

### Compulsory Spot queue (manual deployment only)

Full and heavy-scoped work requires `backend = "spot_queue"`. Cloud Run is not
an active dispatcher choice, and no compulsory run falls back locally. The
dedicated-project live smoke remains a later gate; Cloud Run cleanup is deferred
until after it passes.

```bash
spot_project=YOUR_DEDICATED_TEST_PROJECT
spot_region=us-central1
spot_repo=YOUR_ARTIFACT_REPOSITORY
spot_tag=$(git rev-parse --short HEAD)

gcloud builds submit --project="$spot_project" --region="$spot_region" . \
  --config=cloudbuild.spot-worker.yaml \
  --substitutions="_IMAGE=$spot_region-docker.pkg.dev/$spot_project/$spot_repo/ptest-spot-worker:$spot_tag"
gcloud builds submit --project="$spot_project" --region="$spot_region" . \
  --config=cloudbuild.spot-controller.yaml \
  --substitutions="_IMAGE=$spot_region-docker.pkg.dev/$spot_project/$spot_repo/ptest-spot-controller:$spot_tag"

terraform -chdir=terraform init
terraform -chdir=terraform fmt -check
terraform -chdir=terraform validate
terraform -chdir=terraform plan \
  -var="project_id=$spot_project" -var="region=$spot_region" \
  -var="bucket_name=YOUR_GLOBALLY_UNIQUE_BUCKET" -var="ar_repo=$spot_repo" \
  -var="spot_overflow_reject_enabled=true" \
  -var="spot_worker_image=$spot_region-docker.pkg.dev/$spot_project/$spot_repo/ptest-spot-worker:$spot_tag" \
  -var="spot_controller_image=$spot_region-docker.pkg.dev/$spot_project/$spot_repo/ptest-spot-controller:$spot_tag"
```

After a separately approved apply, use an identity listed in
`operator_members`: Terraform grants it controller invocation, topic publish,
subscription pull/ack, source/result object access, and dedicated-project
Compute OS-admin login plus the narrow instance/MIG read role used below. The
`terraform -chdir=terraform output -raw spot_queue_config_stanza` output includes
the controller URL and the same overflow-rejection toggle value for the ptest
project stanza; do not enable only one side. The
following live smoke deliberately mutates only the dedicated test project. It
submits a real queue request through `ptest`, waits for a worker to run it,
then inspects the durable result and empty delivery:

```bash
set -euo pipefail
controller_url=$(gcloud run services describe ptest-spot-controller \
  --project="$spot_project" --region="$spot_region" --format='value(status.url)')
curl --fail --header "Authorization: Bearer $(gcloud auth print-identity-token)" \
  "$controller_url/healthz"
curl --fail --request POST \
  --header "Authorization: Bearer $(gcloud auth print-identity-token)" \
  "$controller_url/reconcile"
gcloud compute instance-groups managed list-instances ptest-spot-workers \
  --project="$spot_project" --region="$spot_region"

# With a registered project set to backend = "spot_queue", this writes the
# request record, publishes its request key, and waits for its durable result.
set +e
ptest --full --fresh 2>spot-request.log
spot_rc=$?
set -e
test "$spot_rc" -eq 0 || { echo "initial remote smoke failed with $spot_rc" >&2; exit "$spot_rc"; }
spot_key=$(sed -n 's/.*spot request: \([0-9a-f]\{64\}\).*/\1/p' spot-request.log | tail -1)
test -n "$spot_key"
gcloud storage cat "gs://YOUR_GLOBALLY_UNIQUE_BUCKET/spot/v1/states/$spot_key.json" | \
  python -c 'import json,sys; d=json.load(sys.stdin); assert d["schema"] == "ptest-spot-result-v1" and d["status"] == "passed" and d["exit_code"] == 0; print(json.dumps(d, indent=2))'
gcloud pubsub subscriptions pull ptest-spot-workers --project="$spot_project" \
  --limit=1 --format=json | python -c 'import json,sys; assert json.load(sys.stdin) == []'

# Bubblewrap must be permitted by the worker VM/container runtime. The normal
# queue run above proves it for the actual isolated test process; this checks it
# directly before running the preemption proof.
spot_vm=$(gcloud compute instance-groups managed list-instances ptest-spot-workers \
  --project="$spot_project" --region="$spot_region" --format='value(instance)' | head -1)
spot_name=${spot_vm##*/}
spot_zone=${spot_vm%/instances/*}; spot_zone=${spot_zone##*/}
gcloud compute ssh "$spot_name" --project="$spot_project" --zone="$spot_zone" \
  --command='sudo docker exec --user ptest ptest-spot-worker bwrap --die-with-parent --unshare-user --uid 65534 --gid 65534 --unshare-net --ro-bind / / /bin/true'

# Preemption/redelivery proof: configure a harmless slow smoke project (for
# example `full = "sh -c 'sleep 90; exit 0'"`) with backend = spot_queue.
# --fresh guarantees a new attempt. Every poll below has a deadline. The active
# lease must name the VM that is stopped; the MIG, not an assumed Docker restart,
# then restores capacity for redelivery.
ptest --full --fresh 2>spot-slow-request.log & smoke_pid=$!
slow_key=''
deadline=$((SECONDS + 60))
while [ -z "$slow_key" ] && [ "$SECONDS" -lt "$deadline" ]; do
  slow_key=$(sed -n 's/.*spot request: \([0-9a-f]\{64\}\).*/\1/p' spot-slow-request.log | tail -1 || true)
  sleep 1
done
test -n "$slow_key" || { echo 'timed out waiting for request key' >&2; kill "$smoke_pid"; exit 1; }

state_uri="gs://YOUR_GLOBALLY_UNIQUE_BUCKET/spot/v1/states/$slow_key.json"
state_json=$(mktemp)
deadline=$((SECONDS + 180))
active_lease=''
while [ "$SECONDS" -lt "$deadline" ]; do
  if gcloud storage cat "$state_uri" >"$state_json" 2>/dev/null &&
     python -c 'import datetime,json,sys; d=json.load(open(sys.argv[1])); now=datetime.datetime.now(datetime.timezone.utc); expiry=datetime.datetime.fromisoformat(d["expires_at"].replace("Z", "+00:00")); assert d["schema"] == "ptest-spot-lease-v1" and d["request_key"] == sys.argv[2] and expiry > now' "$state_json" "$slow_key"; then
    active_lease=1
    break
  fi
  sleep 2
done
test -n "$active_lease" || { echo 'timed out waiting for an active lease' >&2; kill "$smoke_pid"; exit 1; }

spot_name=$(python -c 'import json,sys; print(json.load(open(sys.argv[1]))["worker_id"])' "$state_json")
lease_acquired_at=$(python -c 'import json,sys; print(json.load(open(sys.argv[1]))["acquired_at"])' "$state_json")
spot_vm=$(gcloud compute instance-groups managed list-instances ptest-spot-workers \
  --project="$spot_project" --region="$spot_region" --format='value(instance)' | \
  grep "/instances/$spot_name$" | head -1)
test -n "$spot_vm" || { echo 'active lease does not name a managed worker VM' >&2; kill "$smoke_pid"; exit 1; }
spot_zone=${spot_vm%/instances/*}; spot_zone=${spot_zone##*/}

# Stopping the leased VM exercises the lost-worker boundary: no result or ack
# can be emitted, its lease expires, and the regional MIG replaces it.
gcloud compute instances stop "$spot_name" --project="$spot_project" \
  --zone="$spot_zone" --quiet
post_stop_generation=$(gcloud storage objects describe "$state_uri" --format='value(generation)')

deadline=$((SECONDS + 600))
replacement_lease=''
while [ "$SECONDS" -lt "$deadline" ]; do
  if gcloud storage cat "$state_uri" >"$state_json" 2>/dev/null &&
     python -c 'import datetime,json,sys; d=json.load(open(sys.argv[1])); now=datetime.datetime.now(datetime.timezone.utc); expiry=datetime.datetime.fromisoformat(d["expires_at"].replace("Z", "+00:00")); acquired=datetime.datetime.fromisoformat(d["acquired_at"].replace("Z", "+00:00")); prior=datetime.datetime.fromisoformat(sys.argv[2].replace("Z", "+00:00")); assert d["schema"] == "ptest-spot-lease-v1" and acquired > prior and expiry > now' "$state_json" "$lease_acquired_at"; then
    new_generation=$(gcloud storage objects describe "$state_uri" --format='value(generation)')
    if [ "$new_generation" -gt "$post_stop_generation" ]; then replacement_lease=1; break; fi
  fi
  sleep 3
done
test -n "$replacement_lease" || { echo 'timed out waiting for replacement lease' >&2; kill "$smoke_pid"; exit 1; }

deadline=$((SECONDS + 900))
while kill -0 "$smoke_pid" 2>/dev/null && [ "$SECONDS" -lt "$deadline" ]; do sleep 2; done
if kill -0 "$smoke_pid" 2>/dev/null; then
  echo 'timed out waiting for redelivered smoke result' >&2; kill "$smoke_pid"; exit 1
fi
set +e
wait "$smoke_pid"
slow_rc=$?
set -e
test "$slow_rc" -eq 0 || { echo "redelivered remote smoke failed with $slow_rc" >&2; exit "$slow_rc"; }
gcloud storage cat "$state_uri" | python -c 'import json,sys; d=json.load(sys.stdin); assert d["schema"] == "ptest-spot-result-v1" and d["status"] == "passed" and d["exit_code"] == 0; print(json.dumps(d, indent=2))'
gcloud pubsub subscriptions pull ptest-spot-workers --project="$spot_project" --limit=1 --format=json | \
  python -c 'import json,sys; assert json.load(sys.stdin) == []'
rm -f "$state_json"
```

The worker pulls one Pub/Sub request, checks terminal state, and takes a
generation-safe lease in one state object. It heartbeats while its
unprivileged/no-network child runs, CAS-transitions that lease to the durable
result, and only then acknowledges. Cancellation uses the competing CAS
transition, so result and cancellation cannot both win. Before local fallback,
ptest waits for a post-cancel idle heartbeat or the captured lease expiry;
ambiguous publish, wait, or cancellation outcomes return 75. A cancelled
deterministic key retries locally, while `--fresh` creates a new remote attempt.
SIGTERM stops the heartbeat, terminates the child, publishes no result, and
leaves unfinished work unacknowledged for redelivery. Node dependencies are
installed from `package-lock.json` before the isolated child starts; dependency
install is script-free, while Vitest itself runs in Bubblewrap with no network.
The worker service account has conditional `roles/storage.objectViewer` access
only to immutable `sources/` and `spot/v1/requests/` objects. Its conditional
`roles/storage.objectUser` grant is limited to mutable `spot/v1/states/` and
`spot/v1/workers/` objects, so a compromised worker cannot overwrite source
archives or request records. The conditions use Cloud Storage's documented
[resource-name prefix model](https://cloud.google.com/storage/docs/access-control/iam-conditions#resource-attributes).
Workers generation-CAS their current lease expiry into the bounded
`spot/v1/workers/index/current.json` snapshot before claiming or renewing. The
controller reads that one snapshot for the lease floor and idle age; retained
terminal state objects do not add controller reads.
With `spot_overflow_reject_enabled = true` in both Terraform and the matching
ptest stanza, a new request first calls the IAM-protected controller `/admit`
endpoint. When the controller observes either Pub/Sub's unacknowledged backlog
or the active-lease floor at `spot_max_workers`, ptest returns 75 before
uploading source or creating durable request state. This is an advisory overflow
brake, not a hard admission cap: the
[Pub/Sub metric](https://docs.cloud.google.com/monitoring/api/metrics_gcp_p_z)
is sampled and delayed, so concurrent callers can pass before saturation is
visible. The controller response carries its enabled state, and an enabled
ptest client fails closed if the controller toggle does not match.
Overflow is never dispatched to a malformed Cloud Run job, and existing durable
requests bypass admission so retries can rejoin them. Backlog already accepted
above the cap remains in Pub/Sub. Configure `backend = "spot_queue"` only after
this smoke proves worker/controller IAM, Bubblewrap, terminal transition,
acknowledgement, cancellation, and retry.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | tests passed |
| 1 | tests failed |
| 2 | compulsory Spot installation/configuration is invalid, or ptest itself could not run |
| **75** | **temporary Spot capacity/coordination outcome; no tests ran locally.** Not a test failure. |

75 is `EX_TEMPFAIL`. For Cloud Run it can mean the remote concurrency ceiling;
for the Spot queue it can mean queued/running work or an ambiguous terminal
coordination outcome. The attempt deliberately does **not** start a local full
fallback, because remote work may already exist. A heavy scoped run competes
for the same Cloud Run slots as `--full` and can return 75 too, so "use a scoped
run" only helps if it's small enough to stay local (a narrower path, or a
`-k`/`-m`/`--lf` filter). Retry later, use `--fresh` when instructed for an
immutable Spot terminal key, scope it tighter, or pass `--local` to force this
run onto the machine.

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
