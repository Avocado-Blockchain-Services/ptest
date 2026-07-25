#!/usr/bin/env bash
# ptest remote runner entrypoint.
#
# Fetch source → boot an in-RAM Postgres → run the suite → exit with its code.
#
# Every failure before the test command runs exits non-zero with a distinct
# message. That matters: an agent reading the output must be able to tell
# "the suite is red" from "the runner broke", because those demand opposite
# reactions and the second one must never be reported as a passing build.

set -Eeuo pipefail

log() { printf '[ptest-runner] %s\n' "$*" >&2; }
die() { printf '[ptest-runner] FATAL: %s\n' "$*" >&2; exit 2; }

: "${PTEST_SRC:?PTEST_SRC (gs://bucket/object.tar.gz) is required}"
: "${PTEST_CMD:?PTEST_CMD (the test command) is required}"

# ── 1. Fetch the packed repo from GCS ────────────────────────────────────────
# One authenticated GET via the metadata-server token; no gcloud SDK in the image.
case "$PTEST_SRC" in
  gs://*) ;;
  *) die "PTEST_SRC must be a gs:// URI, got: $PTEST_SRC" ;;
esac

_path="${PTEST_SRC#gs://}"
_bucket="${_path%%/*}"
_object="${_path#*/}"
# The object name goes in a URL path segment, so slashes must be percent-encoded.
_object_enc="$(printf '%s' "$_object" | sed 's|/|%2F|g')"

log "fetching $PTEST_SRC"
_token="$(curl -sf -H 'Metadata-Flavor: Google' \
  'http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token' \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["access_token"])')" \
  || die "could not obtain a metadata-server access token"

_http=$(curl -s -w '%{http_code}' -o /tmp/src.tar.gz \
  -H "Authorization: Bearer $_token" \
  "https://storage.googleapis.com/storage/v1/b/${_bucket}/o/${_object_enc}?alt=media")
[ "$_http" = "200" ] || die "GCS download failed (HTTP $_http) for $PTEST_SRC"

mkdir -p /work
tar -xzf /tmp/src.tar.gz -C /work || die "could not unpack the source tarball"
rm -f /tmp/src.tar.gz
log "unpacked $(du -sh /work | cut -f1) into /work"

# PTEST_KIND selects the toolchain path. It is baked into each image rather
# than passed per execution, so a job can never be pointed at an image that
# lacks the tools it is about to invoke. Defaults to pytest for images built
# before this variable existed.
PTEST_KIND="${PTEST_KIND:-pytest}"

# Find the repo root: the tarball may unpack into a single top-level directory.
cd /work
if [ ! -f pyproject.toml ] && [ ! -f package.json ] \
   && [ "$(find . -maxdepth 1 -mindepth 1 -type d | wc -l)" = "1" ]; then
  cd "$(find . -maxdepth 1 -mindepth 1 -type d)"
fi
log "workdir $(pwd) (kind=$PTEST_KIND)"

if [ "$PTEST_KIND" = "vitest" ]; then
  # ── Node path: no database, no Postgres. ──────────────────────────────────
  [ -f package.json ] || die "no package.json at $(pwd)"
  if [ -f package-lock.json ]; then
    log "npm ci"
    npm ci --no-audit --no-fund 2>&1 || die "npm ci failed"
  else
    log "WARNING: no package-lock.json shipped — falling back to npm install"
    npm install --no-audit --no-fund 2>&1 || die "npm install failed"
  fi

  log "running: $PTEST_CMD"
  set +e
  bash -lc "$PTEST_CMD"
  _rc=$?
  set -e
  log "test command exited $_rc"
  exit "$_rc"
fi

# ── 2. Boot Postgres in RAM (pytest path only) ───────────────────────────────
# On Cloud Run the container filesystem is memory-backed, so $PGDATA is already
# a tmpfs. The durability settings below are safe *only* because this database
# is destroyed with the execution — never copy this config anywhere real.
log "initdb ($PGDATA)"
su postgres -c "$PGBIN/initdb -D $PGDATA -U $PGUSER --auth=trust --encoding=UTF8" >/dev/null \
  || die "initdb failed"

cat >>"$PGDATA/postgresql.conf" <<'CONF'
# Test-only durability tradeoffs — the volume dies with the container.
fsync = off
synchronous_commit = off
full_page_writes = off
# The suite creates one database per xdist worker and opens a pool per worker.
max_connections = 300
shared_buffers = 512MB
work_mem = 16MB
listen_addresses = 'localhost'
CONF

su postgres -c "$PGBIN/pg_ctl -D $PGDATA -o '-p $PGPORT' -w -t 60 start" \
  || { cat "$PGDATA"/log/* 2>/dev/null >&2; die "postgres failed to start"; }

# The test rig connects as a superuser to the `postgres` admin database and
# issues CREATE/DROP DATABASE, so $PGUSER must be a real superuser. initdb -U
# already made it the bootstrap superuser; assert it rather than assume.
psql -U "$PGUSER" -d postgres -tAc \
  "SELECT usesuper FROM pg_user WHERE usename = '$PGUSER'" | grep -qx 't' \
  || die "role $PGUSER is not a superuser — the temp-DB rig cannot work"
log "postgres up on :$PGPORT as superuser $PGUSER"

# ── 3. Run the suite ─────────────────────────────────────────────────────────
# DATABASE_URL matches the conftest default so an unregistered repo still works.
export DATABASE_URL="${DATABASE_URL:-postgresql+asyncpg://${PGUSER}@localhost:${PGPORT}/persea_content_maker_test}"
export ENVIRONMENT=test
export PYTHONDONTWRITEBYTECODE=1

if [ -f pyproject.toml ]; then
  if [ -f uv.lock ]; then
    log "uv sync --frozen"
    uv sync --frozen 2>&1 || die "uv sync --frozen failed — is uv.lock stale?"
  else
    # Do NOT quietly fall back to a resolving sync. Locking walks the entire
    # dependency space, including private optional extras that a normal run
    # never installs; the clone then fails for lack of credentials and the
    # error blames GitHub auth instead of the missing lockfile. Say so plainly.
    log "WARNING: no uv.lock in the source tree — resolving from scratch."
    log "         Private optional extras may fail here with a git auth error."
    uv sync 2>&1 || die "dependency resolution failed and no uv.lock was shipped"
  fi
fi

log "running: $PTEST_CMD"
set +e
bash -lc "$PTEST_CMD"
_rc=$?
set -e

log "test command exited $_rc"
# Stop Postgres politely so a crash in shutdown can't mask the real exit code.
su postgres -c "$PGBIN/pg_ctl -D $PGDATA -m immediate stop" >/dev/null 2>&1 || true

# Exit code 2 is reserved above for runner failures. pytest also uses 2 for
# "interrupted", which is a genuine test-run outcome, so remap it to 1 to keep
# the runner/suite distinction unambiguous for whoever reads this output.
[ "$_rc" = "2" ] && _rc=1
exit "$_rc"
