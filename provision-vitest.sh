#!/usr/bin/env bash
# Provision the vitest remote runner (job: ptest-persea-front).
#
# Reuses the bucket and runner service account created by provision.sh — those
# are shared infrastructure, not per-toolchain. This script only adds the node
# image and its own Cloud Run job.
#
# A SEPARATE job per project, not one shared job, on purpose: the concurrency
# guard keys on the job, so a single shared job would make a fullon2 run block
# a persea run for no reason.

set -Eeuo pipefail

PROJECT="${PTEST_GCP_PROJECT:-seed-staging-b9508c89}"
REGION="${PTEST_REGION:-us-central1}"
ACCOUNT="${PTEST_ACCOUNT:-ingmar@avocadoblock.com}"
BUCKET="${PTEST_BUCKET:-${PROJECT}-ptest}"
SA="ptest-runner@${PROJECT}.iam.gserviceaccount.com"
AR_REPO="${PTEST_AR_REPO:-persea}"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/${AR_REPO}/ptest-runner-node:latest"
JOB="${PTEST_JOB:-ptest-persea-front}"

# vitest workers are ~350MB each and the suite is CPU-bound, not IO-bound.
TASK_TIMEOUT="${PTEST_TASK_TIMEOUT:-1800s}"
CPU="${PTEST_CPU:-8}"
MEMORY="${PTEST_MEMORY:-16Gi}"

G=(gcloud --project="$PROJECT" --account="$ACCOUNT" --billing-project="$PROJECT")

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
have() { "$@" >/dev/null 2>&1; }

say "0. Preflight — shared bucket + SA must already exist (run provision.sh first)"
have "${G[@]}" storage buckets describe "gs://$BUCKET" \
  || { echo "bucket gs://$BUCKET missing — run provision.sh first"; exit 1; }
have "${G[@]}" iam service-accounts describe "$SA" \
  || { echo "service account $SA missing — run provision.sh first"; exit 1; }

say "1. Build the node runner image"
echo "target: $IMAGE"
_here="$(cd "$(dirname "$0")" && pwd)"
"${G[@]}" builds submit "$_here" \
  --config="$_here/cloudbuild.vitest.yaml" \
  --substitutions=_IMAGE="$IMAGE"

say "2. Cloud Run job $JOB"
JOB_ARGS=(
  --image="$IMAGE"
  --region="$REGION"
  --service-account="$SA"
  --cpu="$CPU"
  --memory="$MEMORY"
  --max-retries=0
  --task-timeout="$TASK_TIMEOUT"
  --parallelism=1
  --tasks=1
  --set-env-vars="PTEST_SRC=unset,PTEST_CMD=unset"
)
if have "${G[@]}" run jobs describe "$JOB" --region="$REGION"; then
  "${G[@]}" run jobs update "$JOB" "${JOB_ARGS[@]}"
else
  "${G[@]}" run jobs create "$JOB" "${JOB_ARGS[@]}"
fi

cat <<SUMMARY

Provisioned:
  image  $IMAGE
  job    $JOB  ($CPU vCPU / $MEMORY, timeout $TASK_TIMEOUT, retries 0)

Then in ~/.config/ptest/config.toml:

  [projects.persea-front]
  backend = "cloudrun"
  job     = "$JOB"

SUMMARY
