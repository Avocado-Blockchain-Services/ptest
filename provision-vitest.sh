#!/usr/bin/env bash
# Provision a vitest remote runner.
#
#   ./provision-vitest.sh                                      persea-front, node 20
#   PTEST_JOB=ptest-fullon2-web PTEST_NODE_VERSION=22 ./provision-vitest.sh
#
# Reuses the bucket and runner service account created by provision.sh — those
# are shared infrastructure, not per-toolchain. This script only adds the node
# image and its own Cloud Run job.
#
# A SEPARATE job per project, not one shared job, on purpose: the concurrency
# guard keys on the job, so a single shared job would make a fullon2 run block
# a persea run for no reason.
#
# The image tag carries the Node major (:20, :22) and is NEVER `latest`. Node
# version must match the consuming project's CI, and the projects disagree, so a
# shared moving tag silently gives one of them the wrong runtime — which is
# exactly what happened to fullon2_web (engines >=22) on the node-20 image.

set -Eeuo pipefail

PROJECT="${PTEST_GCP_PROJECT:-seed-staging-b9508c89}"
REGION="${PTEST_REGION:-us-central1}"
ACCOUNT="${PTEST_ACCOUNT:-ingmar@avocadoblock.com}"
BUCKET="${PTEST_BUCKET:-${PROJECT}-ptest}"
SA="ptest-runner@${PROJECT}.iam.gserviceaccount.com"
AR_REPO="${PTEST_AR_REPO:-persea}"
NODE_VERSION="${PTEST_NODE_VERSION:-20}"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/${AR_REPO}/ptest-runner-node:${NODE_VERSION}"
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

say "1. Build the node runner image (node $NODE_VERSION)"
echo "target: $IMAGE"
_here="$(cd "$(dirname "$0")" && pwd)"
"${G[@]}" builds submit "$_here" \
  --config="$_here/cloudbuild.vitest.yaml" \
  --substitutions=_IMAGE="$IMAGE",_NODE_VERSION="$NODE_VERSION"

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
  image  $IMAGE  (node $NODE_VERSION)
  job    $JOB  ($CPU vCPU / $MEMORY, timeout $TASK_TIMEOUT, retries 0)

Then in ~/.config/ptest/config.toml:

  [projects.<name>]
  backend = "cloudrun"
  job     = "$JOB"

\`remote_workers\` defaults to $CPU in ptest and must track --cpu above; every run
logs the parallelism the container actually reports, so drift shows up there.

SUMMARY
