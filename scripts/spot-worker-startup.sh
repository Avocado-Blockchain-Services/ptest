#!/usr/bin/env bash
# Boot a stock Debian 12 Spot VM.  This script is injected as instance metadata
# so its ordering can be exercised without creating a VM.
set -Eeuo pipefail

log() { printf '[ptest-spot-bootstrap] %s\n' "$*" >&2; }
die() { log "FATAL: $*"; exit 2; }
trap 'die "bootstrap failed at line $LINENO"' ERR

: "${SPOT_WORKER_IMAGE:?SPOT_WORKER_IMAGE is required}"
: "${SPOT_PROJECT:?SPOT_PROJECT is required}"
: "${SPOT_REGION:?SPOT_REGION is required}"
: "${SPOT_TOPIC:?SPOT_TOPIC is required}"
: "${SPOT_SUBSCRIPTION:?SPOT_SUBSCRIPTION is required}"
: "${SPOT_BUCKET:?SPOT_BUCKET is required}"

apt_root="${SPOT_APT_ROOT:-/etc/apt}"
keyring_dir="$apt_root/keyrings"
sources_dir="$apt_root/sources.list.d"
mkdir -p "$keyring_dir" "$sources_dir"

log 'installing Docker bootstrap prerequisites'
apt-get update
apt-get install -y --no-install-recommends ca-certificates curl gnupg docker.io

log 'configuring the Google Cloud apt repository'
curl -fsSL https://packages.cloud.google.com/apt/doc/apt-key.gpg |
  gpg --dearmor -o "$keyring_dir/google-cloud.gpg"
printf '%s\n' 'deb [signed-by=/etc/apt/keyrings/google-cloud.gpg] https://packages.cloud.google.com/apt cloud-sdk main' \
  > "$sources_dir/google-cloud-sdk.list"
apt-get update
apt-get install -y --no-install-recommends google-cloud-cli

systemctl enable --now docker
gcloud auth configure-docker "${SPOT_REGION}-docker.pkg.dev" --quiet
docker rm -f ptest-spot-worker >/dev/null 2>&1 || true
docker run --detach --restart=always --name ptest-spot-worker --hostname "$(hostname)" --publish 8080:8080 \
  --security-opt=no-new-privileges:true --security-opt=seccomp=unconfined \
  --env SPOT_PROJECT --env SPOT_REGION --env SPOT_TOPIC --env SPOT_SUBSCRIPTION --env SPOT_BUCKET \
  --env SPOT_LEASE_SECONDS="${SPOT_LEASE_SECONDS:-120}" \
  --env SPOT_TASK_TIMEOUT_SECONDS="${SPOT_TASK_TIMEOUT_SECONDS:-1800}" \
  "$SPOT_WORKER_IMAGE"
log 'Spot worker container started'
