#!/usr/bin/env bash
# Provision the ptest remote backend in seed-staging-b9508c89.
#
# Idempotent: every step either creates the resource or reports it already
# exists. Safe to re-run after a partial failure.
#
# Deliberately NOT piped anywhere by default. `gcloud builds submit | tail`
# hides a failed build behind a zero exit status from the pipe — that has
# already caused one bad deploy in this project. If you pipe this, check
# PIPESTATUS.

set -Eeuo pipefail

PROJECT="${PTEST_GCP_PROJECT:-seed-staging-b9508c89}"
REGION="${PTEST_REGION:-us-central1}"
ACCOUNT="${PTEST_ACCOUNT:-ingmar@avocadoblock.com}"
BUCKET="${PTEST_BUCKET:-${PROJECT}-ptest}"
SA_NAME="ptest-runner"
SA="${SA_NAME}@${PROJECT}.iam.gserviceaccount.com"
AR_REPO="${PTEST_AR_REPO:-persea}"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/${AR_REPO}/ptest-runner:latest"
JOB="${PTEST_JOB:-ptest-persea-api}"

# Extra job-level environment, as gcloud's comma-separated KEY=VALUE list.
# ptest sends PTEST_SRC/PTEST_CMD per execution with --update-env-vars, which
# merges rather than replaces, so anything set here survives every run. This is
# where a project whose suite reads discrete DB_*/REDIS_* variables (fullon2)
# rather than a single DATABASE_URL (persea) declares them.
JOB_ENV="${PTEST_JOB_ENV:-}"

# Runaway controls. These are the whole reason this file is not a one-liner.
TASK_TIMEOUT="${PTEST_TASK_TIMEOUT:-1800s}"   # hard server-side wall clock
MAX_RETRIES=0                                  # Cloud Run defaults to 3 (!)
CPU="${PTEST_CPU:-8}"
MEMORY="${PTEST_MEMORY:-16Gi}"
SRC_RETENTION_DAYS="${PTEST_SRC_RETENTION_DAYS:-3}"

G=(gcloud --project="$PROJECT" --account="$ACCOUNT" --billing-project="$PROJECT")

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
have() { "$@" >/dev/null 2>&1; }

# IAM is eventually consistent: a service account can be created successfully
# and still be invisible to add-iam-policy-binding for tens of seconds. Without
# this, a first run always fails at step 5 and looks like a permissions problem.
retry() {
  local tries="$1"; shift
  local n=1
  until "$@"; do
    if [ "$n" -ge "$tries" ]; then
      echo "gave up after $tries attempts: $*" >&2
      return 1
    fi
    echo "  … attempt $n failed, retrying in 10s" >&2
    sleep 10
    n=$((n + 1))
  done
}

say "0. Preflight"
"${G[@]}" projects describe "$PROJECT" --format='value(projectId)' \
  || { echo "cannot reach $PROJECT as $ACCOUNT"; exit 1; }

say "1. Enable APIs"
"${G[@]}" services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  storage.googleapis.com

say "2. Source-drop bucket gs://$BUCKET"
if have "${G[@]}" storage buckets describe "gs://$BUCKET"; then
  echo "bucket already exists"
else
  "${G[@]}" storage buckets create "gs://$BUCKET" \
    --location="$REGION" \
    --uniform-bucket-level-access \
    --public-access-prevention
fi

# Tarballs are worthless the moment the run finishes. Without this they
# accumulate forever and quietly become the most expensive part of the setup.
say "3. Lifecycle: delete source tarballs after ${SRC_RETENTION_DAYS}d"
_lc="$(mktemp)"; trap 'rm -f "$_lc"' EXIT
cat >"$_lc" <<JSON
{"rule":[{"action":{"type":"Delete"},"condition":{"age":${SRC_RETENTION_DAYS}}}]}
JSON
"${G[@]}" storage buckets update "gs://$BUCKET" --lifecycle-file="$_lc"

say "4. Runner service account"
if have "${G[@]}" iam service-accounts describe "$SA"; then
  echo "service account already exists"
else
  "${G[@]}" iam service-accounts create "$SA_NAME" \
    --display-name="ptest remote test runner"
fi

# Least privilege: read the source drop, write logs. Nothing else. This SA must
# never be able to touch persea's real buckets, secrets, or Cloud SQL.
say "5. Grant the runner read access to the source bucket only"
echo "waiting for the service account to propagate…"
retry 12 "${G[@]}" iam service-accounts describe "$SA" >/dev/null

retry 6 "${G[@]}" storage buckets add-iam-policy-binding "gs://$BUCKET" \
  --member="serviceAccount:${SA}" \
  --role="roles/storage.objectViewer" >/dev/null
retry 6 "${G[@]}" projects add-iam-policy-binding "$PROJECT" \
  --member="serviceAccount:${SA}" \
  --role="roles/logging.logWriter" \
  --condition=None >/dev/null

say "6. Build the runner image"
echo "target: $IMAGE"
_here="$(cd "$(dirname "$0")" && pwd)"
"${G[@]}" builds submit "$_here" \
  --config="$_here/cloudbuild.pytest.yaml" \
  --substitutions=_IMAGE="$IMAGE"
# NOTE: no pipe above, on purpose — see the header comment.

say "7. Cloud Run job $JOB"
_env="PTEST_SRC=unset,PTEST_CMD=unset"
[ -n "$JOB_ENV" ] && _env="${_env},${JOB_ENV}"
JOB_ARGS=(
  --image="$IMAGE"
  --region="$REGION"
  --service-account="$SA"
  --cpu="$CPU"
  --memory="$MEMORY"
  --max-retries="$MAX_RETRIES"
  --task-timeout="$TASK_TIMEOUT"
  --parallelism=1
  --tasks=1
  --set-env-vars="$_env"
)
if have "${G[@]}" run jobs describe "$JOB" --region="$REGION"; then
  "${G[@]}" run jobs update "$JOB" "${JOB_ARGS[@]}"
else
  "${G[@]}" run jobs create "$JOB" "${JOB_ARGS[@]}"
fi

say "8. Let your account push source and execute the job"
"${G[@]}" storage buckets add-iam-policy-binding "gs://$BUCKET" \
  --member="user:${ACCOUNT}" --role="roles/storage.objectAdmin" >/dev/null

cat <<SUMMARY

Provisioned:
  bucket   gs://$BUCKET           (objects auto-delete after ${SRC_RETENTION_DAYS}d)
  runner   $SA
  image    $IMAGE
  job      $JOB  ($CPU vCPU / $MEMORY, timeout $TASK_TIMEOUT, retries $MAX_RETRIES)

Runaway controls in place:
  - max-retries 0     a failed run is NOT silently retried 3x
  - task-timeout      $TASK_TIMEOUT hard server-side kill
  - lifecycle         source tarballs expire after ${SRC_RETENTION_DAYS}d
  - client guards     daily cap + concurrent-execution refusal (see ~/.local/bin/ptest)

Now flip the stanza in ~/.config/ptest/config.toml:

  [projects.<project>]
  backend     = "cloudrun"
  job         = "$JOB"
  gcp_project = "$PROJECT"
  bucket      = "$BUCKET"

SUMMARY
