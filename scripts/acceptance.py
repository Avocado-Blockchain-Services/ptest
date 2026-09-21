#!/usr/bin/env python3
"""Run bounded local ptest acceptance attempts and write honest evidence.

The default mode is a non-executing matrix inventory.  Execution is opt-in and
all commands are literal argv.  This script never clones, installs, publishes,
or contacts a service; a root operator may supply an already prepared checkout.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Sequence

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
from benchmark import run_command, environment_metadata  # noqa: E402

SCHEMA_VERSION = 1
MAX_ATTEMPTS = 64
ADOPTION_CANDIDATES = {
    "pytest": {
        "repository": "marshmallow-code/apispec",
        "commit": "bfac55c9bfbc4edbdde505d29d8c20c62133fc10",
        "profile": "basic_serial9.1.1",
        "status": "blocked-unverified",
        "reason": "independent checkout and dependency setup were not authorized by the operator",
    },
    "vitest": {
        "repository": "dcastil/tailwind-merge",
        "commit": "71218a58edc9e5ad36e5a3233b51c893782a411d",
        "profile": "basic_serial3.1.4",
        "status": "blocked-unverified",
        "reason": "independent checkout and Yarn setup were not authorized by the operator",
    },
}


@dataclass(frozen=True)
class Attempt:
    name: str
    command: tuple[str, ...]
    cwd: str
    status: str
    exit_code: int | None
    seconds: float | None
    artifact: str | None
    setup: str
    network: str
    notes: tuple[str, ...] = ()


def _sha256(path: Path) -> str | None:
    if not path.is_file() or path.is_symlink():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fixture_domain(parent: Path) -> Path:
    """Create an owned, explicit domain containing bounded nested children."""
    parent = parent.resolve()
    parent.mkdir(parents=True, exist_ok=True)
    if parent.is_symlink():
        raise ValueError("fixture parent may not be a symlink")
    domain = Path(tempfile.mkdtemp(prefix="ptest-acceptance-domain-", dir=parent))
    marker = domain / "fixture-domain.json"
    marker.write_text(
        json.dumps({"schema_version": 1, "fixture": True, "children": ["child-a", "child-b"]}) + "\n",
        encoding="utf-8",
    )
    for child in ("child-a", "child-b"):
        child_root = domain / child
        (child_root / "tests" / "nested").mkdir(parents=True)
        (child_root / "tests" / "nested" / "test_smoke.py").write_text(
            "def test_nested_fixture():\n    assert True\n", encoding="utf-8"
        )
    return domain


def _validate_fixture_domain(domain: Path) -> None:
    """Reject a fixture path unless its owned marker and children are intact."""
    domain = domain.resolve()
    marker = domain / "fixture-domain.json"
    if not domain.is_dir() or domain.is_symlink() or marker.is_symlink() or not marker.is_file():
        raise ValueError("fixture domain must be an owned regular directory with a marker")
    try:
        metadata = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("fixture domain marker is invalid") from exc
    if metadata != {"schema_version": 1, "fixture": True, "children": ["child-a", "child-b"]}:
        raise ValueError("fixture domain marker has unexpected fields")
    for child in metadata["children"]:
        child_root = domain / child
        test_file = child_root / "tests" / "nested" / "test_smoke.py"
        if child_root.is_symlink() or not test_file.is_file() or test_file.is_symlink():
            raise ValueError("fixture domain child is missing or linked")


def _validate_output(output: Path, root: Path) -> Path:
    """Keep evidence outside the checkout and refuse symlink destinations."""
    if output.exists() and output.is_symlink():
        raise ValueError("evidence output may not be a symlink")
    resolved = output.resolve()
    if resolved == root or root in resolved.parents:
        raise ValueError("evidence output must be outside the checkout")
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def _attempt_from_sample(name: str, command: Sequence[str], cwd: Path, sample, *, setup: str, network: str) -> Attempt:
    return Attempt(
        name=name,
        command=tuple(command),
        cwd=str(cwd),
        status="passed" if sample.exit_code == 0 and not sample.capped else ("capped" if sample.capped else "failed"),
        exit_code=sample.exit_code,
        seconds=sample.seconds,
        artifact=sample.artifact,
        setup=setup,
        network=network,
        notes=("captured stdout/stderr retained",) if sample.artifact else (),
    )


def run_acceptance(
    *,
    root: Path,
    ptest: Path,
    output: Path,
    execute: bool = False,
    timeout: float = 30.0,
) -> dict[str, object]:
    """Build the Task14 matrix; execute only the local candidate smoke when asked."""
    root = root.resolve()
    ptest = ptest.resolve()
    output = output.resolve()
    if not root.is_dir() or not ptest.is_file():
        raise ValueError("root and candidate ptest must be existing files/directories")
    output = _validate_output(output, root)
    attempts: list[Attempt] = []
    if execute:
        with tempfile.TemporaryDirectory(prefix="ptest-acceptance-", dir=str(output)) as temp:
            domain = _fixture_domain(Path(temp))
            _validate_fixture_domain(domain)
            command = (str(ptest), "--fixture-domain", str(domain), "--version")
            sample = run_command(
                command,
                cwd=root,
                timeout=timeout,
                artifact_dir=output / "attempts",
                label="local-fixture-version",
            )
            attempts.append(
                _attempt_from_sample(
                    "local-fixture-version", command, root, sample,
                    setup="temporary validated fixture domain with two nested children",
                    network="none",
                )
            )
    else:
        attempts.append(Attempt(
            name="local-fixture-version",
            command=(str(ptest), "--fixture-domain", "<temporary-domain>", "--version"),
            cwd=str(root),
            status="not-run",
            exit_code=None,
            seconds=None,
            artifact=None,
            setup="not run; pass --execute to create temporary fixture domain",
            network="none",
            notes=("plan-only inventory; no claim of runtime support",),
        ))
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "acceptance",
        "status": "observed" if execute else "inventory-only",
        "root": str(root),
        "candidate": str(ptest),
        "environment": environment_metadata(root),
        "source": {"commit": environment_metadata(root).get("commit"), "config_sha256": _sha256(root / ".ptest.toml")},
        "attempts": [asdict(item) for item in attempts],
        "adoption": ADOPTION_CANDIDATES,
        "limitations": [
            "No independent adoption checkout was fetched or executed.",
            "No macOS evidence was available in this Linux run.",
            "No advanced selection, baseline, or performance claim is promoted.",
            "Substantial suites require direct operator invocation outside ptest collection.",
        ],
        "promotable": bool(execute and attempts and all(item.status == "passed" for item in attempts)),
        "artifacts": str(output),
    }


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--ptest", type=Path, default=Path(shutil.which("ptest") or "ptest"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--execute", action="store_true", help="run the bounded local fixture smoke")
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args(list(argv))
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    output = args.output.resolve() if args.output else Path(tempfile.mkdtemp(prefix="ptest-acceptance-"))
    try:
        evidence = run_acceptance(
            root=args.root,
            ptest=args.ptest,
            output=output,
            execute=args.execute,
            timeout=args.timeout,
        )
    except (OSError, ValueError) as exc:
        print(f"acceptance refused: {exc}", file=sys.stderr)
        return 2
    path = output / "acceptance.json"
    path.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(evidence, indent=2, sort_keys=True))
    return 0 if evidence["promotable"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
