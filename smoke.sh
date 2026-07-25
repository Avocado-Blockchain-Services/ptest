#!/usr/bin/env bash
# End-to-end smoke test of the remote runner.
#
# Packs this repo exactly the way ptest does, uploads it, and runs ONE
# DB-backed test file remotely. The point is to prove the whole chain
# (fetch → unpack → Postgres → uv sync → pytest) on a few minutes of wall
# clock instead of discovering a broken image during a 20-minute full suite.

set -Eeuo pipefail

REPO="${1:-/home/ingmar/code/persea_content_maker/persea_content_maker_api}"
CMD="${2:-uv run pytest tests/api/test_approver_resolution.py -q --no-cov}"

PROJECT=seed-staging-b9508c89
REGION=us-central1
ACCOUNT=ingmar@avocadoblock.com
BUCKET="${PROJECT}-ptest"
JOB="${PTEST_JOB:-ptest-persea-api}"

G=(gcloud --project="$PROJECT" --account="$ACCOUNT" --billing-project="$PROJECT")

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
