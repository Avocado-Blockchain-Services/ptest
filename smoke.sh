#!/usr/bin/env bash
# End-to-end smoke test of the remote runner.
#
# Packs this repo exactly the way ptest does, uploads it, and runs ONE
# DB-backed test file remotely. The point is to prove the whole chain
# (fetch → unpack → Postgres → uv sync → pytest) on a few minutes of wall
# clock instead of discovering a broken image during a 20-minute full suite.

set -Eeuo pipefail

die() {
  echo "smoke: $*" >&2
  return 1
}

one_request_key() {
  local log=$1 keys count
  keys=$(sed -nE 's/.*\[ptest\] request: ([0-9a-f]{64}).*/\1/p' "$log" | sort -u)
  count=$(printf '%s\n' "$keys" | sed '/^$/d' | wc -l)
  [[ $count -eq 1 ]] || die "expected exactly one request key in $log, found $count"
  printf '%s\n' "$keys"
}

assert_dedup_fixtures() {
  local owner_log=$1 joiner_log=$2 cached_log=$3
  local owner_key joiner_key cached_key joined_execution cached_execution
  owner_key=$(one_request_key "$owner_log")
  joiner_key=$(one_request_key "$joiner_log")
  cached_key=$(one_request_key "$cached_log")
  [[ $owner_key == "$joiner_key" && $owner_key == "$cached_key" ]] ||
    die "request keys differ across owner, joiner, and cache-hit logs"

  joined_execution=$(sed -nE 's/.*joining identical remote execution ([a-z0-9-]+).*/\1/p' \
    "$owner_log" "$joiner_log" | tail -1)
  cached_execution=$(sed -nE 's/.*reused passing result from ([a-z0-9-]+).*/\1/p' "$cached_log" | tail -1)
  [[ -n $joined_execution ]] || die "joiner log has no shared execution"
  [[ $joined_execution == "$cached_execution" ]] ||
    die "cache hit did not reuse joined execution $joined_execution"

  echo "dedup fixtures: request $owner_key joined and reused $joined_execution"
}

run_live_dedup_smoke() {
  local repo=${PTEST_LIVE_REPO:-/home/ingmar/code/persea_content_maker/persea_content_maker_api}
  local ptest_bin=${PTEST_BIN:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/ptest}
  local job=${PTEST_JOB:-ptest-persea-api}
  local tmpdir caller_one caller_two cached before after new_executions new_count rc_one rc_two
  tmpdir=$(mktemp -d /tmp/ptest-live-dedup-XXXXXX)
  caller_one=$tmpdir/caller-one.log
  caller_two=$tmpdir/caller-two.log
  cached=$tmpdir/cached.log
  before=$tmpdir/before.txt
  after=$tmpdir/after.txt

  "${G[@]}" run jobs executions list --job="$job" --region="$REGION" \
    --format='value(metadata.name)' | sort -u > "$before"

  (cd "$repo" && "$ptest_bin" --full) > "$caller_one" 2>&1 &
  local pid_one=$!
  (cd "$repo" && "$ptest_bin" --full) > "$caller_two" 2>&1 &
  local pid_two=$!
  if wait "$pid_one"; then rc_one=0; else rc_one=$?; fi
  if wait "$pid_two"; then rc_two=0; else rc_two=$?; fi
  [[ $rc_one -eq 0 && $rc_two -eq 0 ]] ||
    die "concurrent callers failed (caller-one=$rc_one caller-two=$rc_two); logs: $tmpdir"

  (cd "$repo" && "$ptest_bin" --full) > "$cached" 2>&1 ||
    die "cache-hit caller failed; log: $cached"
  assert_dedup_fixtures "$caller_one" "$caller_two" "$cached"

  "${G[@]}" run jobs executions list --job="$job" --region="$REGION" \
    --format='value(metadata.name)' | sort -u > "$after"
  new_executions=$(comm -13 "$before" "$after")
  new_count=$(printf '%s\n' "$new_executions" | sed '/^$/d' | wc -l)
  [[ $new_count -eq 1 ]] ||
    die "expected one new Cloud Run execution, found $new_count; logs: $tmpdir"
  echo "live dedup smoke: one execution (${new_executions}) for two callers plus one cache hit"
  rm -rf "$tmpdir"
}

if [[ ${1:-} == --assert-dedup-fixtures ]]; then
  [[ $# -eq 4 ]] || die "usage: smoke.sh --assert-dedup-fixtures OWNER JOINER CACHED"
  assert_dedup_fixtures "$2" "$3" "$4"
  exit
fi

REPO="${1:-/home/ingmar/code/persea_content_maker/persea_content_maker_api}"
CMD="${2:-uv run pytest tests/api/test_approver_resolution.py -q --no-cov}"

PROJECT=seed-staging-b9508c89
REGION=us-central1
ACCOUNT=ingmar@avocadoblock.com
BUCKET="${PROJECT}-ptest"
JOB="${PTEST_JOB:-ptest-persea-api}"

G=(gcloud --project="$PROJECT" --account="$ACCOUNT" --billing-project="$PROJECT")

if [[ ${PTEST_LIVE_DEDUP_SMOKE:-0} == 1 ]]; then
  run_live_dedup_smoke
  exit
fi

TAR=$(mktemp -u /tmp/ptest-smoke-XXXX.tar.gz)
# Same git-driven packing as ptest's pack_tree — keep these in step, or the
# smoke test stops being a faithful rehearsal of a real run.
python3 - "$REPO" "$TAR" <<'PY'
import subprocess, sys, tarfile
from pathlib import Path
root, out = Path(sys.argv[1]), sys.argv[2]
res = subprocess.run(["git", "ls-files", "-c", "-o", "--exclude-standard", "-z"],
                     cwd=str(root), capture_output=True, check=True)
files = [f for f in res.stdout.decode().split("\0") if f]
# Lockfiles are build input; this repo gitignores uv.lock, so git omits it.
for lock in ("uv.lock", "poetry.lock", "package-lock.json", "pnpm-lock.yaml"):
    if lock not in files and (root / lock).is_file():
        files.append(lock)
n = 0
with tarfile.open(out, "w:gz") as tf:
    for rel in files:
        p = root / rel
        if not p.exists() or p.is_dir() or Path(rel).name.startswith(".env"):
            continue
        tf.add(str(p), arcname="./" + rel)
        n += 1
print(f"packed {n} files, {Path(out).stat().st_size/1e6:.1f}MB")
PY

OBJ="gs://${BUCKET}/smoke-$(date +%s).tar.gz"
echo "uploading → $OBJ"
"${G[@]}" storage cp "$TAR" "$OBJ"
rm -f "$TAR"

echo "executing $JOB …"
"${G[@]}" run jobs execute "$JOB" \
  --region="$REGION" --wait \
  --update-env-vars="PTEST_SRC=${OBJ},PTEST_CMD=${CMD}"
rc=$?
echo "execute exit: $rc"
exit $rc
