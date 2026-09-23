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
import os
import re
import shutil
import stat
import sys
import tempfile
import tomllib
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Sequence

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
from benchmark import (_lexical_no_symlinks, _write_private,
                       run_command, environment_metadata)  # noqa: E402

SCHEMA_VERSION = 1
MAX_ATTEMPTS = 64
VERSION_RE = re.compile(r"\A\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?\Z")
WORKLOAD = {
    "schema_version": 1,
    "name": "local-miniature-v1",
    "warmup_samples": 20,
    "doctor_samples": 20,
    "alternating_pairs": 5,
    "s2_pairs_default_unknown": 10,
    "s2_pairs_known_budget": 10,
    "s2_pairs_unknown_opt_in": 10,
    "thresholds": {
        "wrapper_p95_seconds": 0.250,
        "plan_p95_seconds": 2.0,
        "doctor_completed_p95_seconds": 5.0,
        "doctor_safety_cap_seconds": 8.0,
        "minimum_benefit_ratio": 0.20,
    },
    "required_fields": [
        "setup_seconds", "queue_wait_seconds", "execution_seconds", "rss_bytes",
        "completed", "exit_code", "coverage_digest", "inventory_digest",
    ],
}
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


@dataclass(frozen=True)
class CandidateIdentity:
    path: str
    sha256: str
    version: str


@dataclass(frozen=True)
class WorkloadSpec:
    candidate: CandidateIdentity
    fixture_domain: str
    project: str
    lifecycle: tuple[str, ...]
    sample_counts: dict[str, int]
    thresholds: dict[str, float]


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
    parent = parent.absolute()
    if not parent.is_dir() or parent.is_symlink():
        raise ValueError("fixture parent may not be a symlink")
    domain = Path(tempfile.mkdtemp(prefix="ptest-acceptance-domain-", dir=parent))
    stamp = os.stat(domain)
    fixture_id = hashlib.sha256(os.fsencode(str(domain))).hexdigest()[:32]
    marker = domain / "fixture-domain.toml"
    marker_text = (
        "version = 1\nfixture = true\nmax_slots = 2\nmax_jobs = 2\n"
        f"uid = {os.getuid()}\n"
        f"directory_device = {stamp.st_dev}\n"
        f"directory_inode = {stamp.st_ino}\n"
        f'fixture_id = "{fixture_id}"\nworkload = "synthetic-or-miniature"\n'
    )
    _write_private(marker, marker_text.encode("utf-8"))
    for child in ("child-a", "child-b"):
        child_root = domain / child
        child_root.mkdir(mode=0o700)
        (child_root / "tests").mkdir(mode=0o700)
        (child_root / "tests" / "nested").mkdir(mode=0o700)
        _write_private(
            child_root / "tests" / "nested" / "test_smoke.py",
            b"def test_nested_fixture():\n    assert True\n",
        )
        runner = child_root / "tests" / "nested" / "runner.py"
        _write_private(runner, b"raise SystemExit(0)\n")
        config = (
            "version = 1\nproject_id = \"" + hashlib.sha256(os.fsencode(child)).hexdigest()[:32] + "\"\n"
            "[runner]\nkind = \"command\"\nlauncher = [\"python\"]\n"
            "args = [\"tests/nested/runner.py\"]\nfull_args = []\n"
            "test_roots = [\"tests\"]\nworkers = 1\n"
            "lifecycle = \"cooperative-process-group\"\n"
        )
        config_path = child_root / ".ptest.toml"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(config_path, flags, 0o644)
        with os.fdopen(fd, "wb") as stream:
            stream.write(config.encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
    return domain


def _validate_fixture_domain(domain: Path) -> None:
    """Reject a fixture path unless its owned marker and children are intact."""
    if domain.is_symlink():
        raise ValueError("fixture domain may not be a symlink")
    domain = domain.resolve()
    marker = domain / "fixture-domain.toml"
    if not domain.is_dir() or domain.is_symlink() or marker.is_symlink() or not marker.is_file():
        raise ValueError("fixture domain must be an owned regular directory with a marker")
    domain_stamp = os.stat(domain)
    marker_stamp = os.stat(marker)
    if (domain_stamp.st_uid != os.getuid() or marker_stamp.st_uid != os.getuid()
            or stat.S_IMODE(domain_stamp.st_mode) != 0o700
            or stat.S_IMODE(marker_stamp.st_mode) != 0o600):
        raise ValueError("fixture domain has unsafe mode")
    try:
        raw = marker.read_text(encoding="utf-8")
        metadata = tomllib.loads(raw)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError("fixture domain marker is invalid") from exc
    expected = {
        "version", "fixture", "max_slots", "max_jobs", "uid",
        "directory_device", "directory_inode", "fixture_id", "workload",
    }
    if set(metadata) != expected or metadata.get("version") != 1 or metadata.get("fixture") is not True:
        raise ValueError("fixture domain marker has unexpected fields")
    stamp = os.stat(domain)
    if metadata.get("uid") != os.getuid() or metadata.get("directory_device") != stamp.st_dev or metadata.get("directory_inode") != stamp.st_ino:
        raise ValueError("fixture domain marker identity is stale or foreign")
    if metadata.get("workload") != "synthetic-or-miniature":
        raise ValueError("fixture domain workload is unsupported")
    for child in ("child-a", "child-b"):
        child_root = domain / child
        test_file = child_root / "tests" / "nested" / "test_smoke.py"
        if child_root.is_symlink() or not test_file.is_file() or test_file.is_symlink() or not (child_root / ".ptest.toml").is_file():
            raise ValueError("fixture domain child is missing or linked")


def _new_evidence_root(requested: Path | None, root: Path) -> Path:
    """Exclusively create a fresh 0700 evidence root outside the checkout."""
    if requested is None:
        return Path(tempfile.mkdtemp(prefix="ptest-acceptance-", dir="/tmp"))
    requested = _lexical_no_symlinks(requested)
    if requested.exists() or requested.is_symlink():
        raise ValueError("evidence output must be a new directory")
    resolved = requested.resolve()
    resolved_root = root.resolve()
    if resolved == resolved_root or resolved_root in resolved.parents:
        raise ValueError("evidence output must be outside the checkout")
    requested.mkdir(mode=0o700)
    stamp = os.stat(requested)
    if stamp.st_uid != os.getuid() or (stamp.st_mode & 0o777) != 0o700:
        raise ValueError("evidence output has unsafe mode")
    return requested


def _candidate_identity(candidate: Path, root: Path, output: Path, timeout: float) -> tuple[CandidateIdentity | None, Attempt]:
    candidate = candidate.absolute()
    if not candidate.is_file() or not os.access(candidate, os.X_OK):
        raise ValueError("candidate ptest must be an executable regular file")
    candidate = candidate.resolve()
    digest = _sha256(candidate)
    assert digest is not None
    artifact = output / "attempts" / "candidate-version"
    sample = run_command((str(candidate), "--version"), cwd=root, timeout=timeout,
                         artifact_dir=artifact, label="candidate-version")
    output_path = artifact / "candidate-version.stdout"
    version = output_path.read_text(encoding="utf-8", errors="replace").strip()
    identity = CandidateIdentity(str(candidate), digest, version) if sample.exit_code == 0 and VERSION_RE.fullmatch(version) else None
    note = ("candidate version is not a semantic ptest version",) if identity is None else ()
    attempt = _attempt_from_sample(
        "candidate-version", (str(candidate), "--version"), root, sample,
        setup="candidate-bound version probe", network="none",
    )
    return identity, replace(attempt, notes=tuple(attempt.notes) + note)


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
    output: Path | None,
    execute: bool = False,
    timeout: float = 30.0,
) -> dict[str, object]:
    """Build the Task14 matrix; execute only the local candidate smoke when asked."""
    root = root.resolve()
    if not root.is_dir() or not ptest.exists():
        raise ValueError("root and candidate ptest must be existing files/directories")
    output = _new_evidence_root(output, root)
    attempts: list[Attempt] = []
    workload_spec: WorkloadSpec | None = None
    if execute:
        candidate, identity_attempt = _candidate_identity(ptest, root, output, timeout)
        attempts.append(identity_attempt)
        if candidate is None:
            return {
                "schema_version": SCHEMA_VERSION, "kind": "acceptance", "status": "failed",
                "root": str(root), "candidate": str(ptest.absolute()),
                "candidate_identity": None, "workload": WORKLOAD,
                "environment": environment_metadata(root),
                "source": {"commit": environment_metadata(root).get("commit"), "config_sha256": _sha256(root / ".ptest.toml")},
                "attempts": [asdict(item) for item in attempts], "adoption": ADOPTION_CANDIDATES,
                "limitations": ["Candidate identity/version validation failed; no workload was promoted."],
                "promotable": False, "artifacts": str(output),
            }
        with tempfile.TemporaryDirectory(prefix="fixture-", dir=str(output)) as temp:
            domain = _fixture_domain(Path(temp))
            _validate_fixture_domain(domain)
            project = domain / "child-a"
            lifecycle = (
                ("init", ("init", "--dry-run", "--runner", "command")),
                ("where", ("where",)),
                ("plan", ("plan",)),
                ("doctor", ("doctor", "--offline")),
                ("doctor-consent-required", ("doctor",)),
                ("status", ("status",)),
                ("full", ("--full", "--no-setup")),
                ("scoped", ("--no-setup", "tests/nested/test_smoke.py")),
                ("automatic", ("--no-setup",)),
            )
            workload_spec = WorkloadSpec(
                candidate=candidate,
                fixture_domain=str(domain),
                project=str(project),
                lifecycle=tuple(name for name, _ in lifecycle) + ("cancel",),
                sample_counts={
                    "warmup": WORKLOAD["warmup_samples"],
                    "doctor": WORKLOAD["doctor_samples"],
                    "alternating_pairs": WORKLOAD["alternating_pairs"],
                    "s2_default_unknown": WORKLOAD["s2_pairs_default_unknown"],
                    "s2_known_budget": WORKLOAD["s2_pairs_known_budget"],
                    "s2_unknown_opt_in": WORKLOAD["s2_pairs_unknown_opt_in"],
                },
                thresholds=WORKLOAD["thresholds"],
            )
            for name, tail in lifecycle:
                command = (candidate.path, "--fixture-domain", str(domain), *tail)
                sample = run_command(command, cwd=project, timeout=timeout,
                                     artifact_dir=output / "attempts" / name, label=name,
                                     env={"PATH": ""} if name == "doctor-consent-required" else None)
                attempt = _attempt_from_sample(
                    name, command, project, sample,
                    setup="validated TOML fixture domain and miniature command project",
                    network="none",
                )
                if name == "doctor":
                    attempt = replace(
                        attempt,
                        notes=tuple(attempt.notes) + (
                            "--offline static-only diagnostic; no reviewer qualification or launch occurred; not a model review",
                        ),
                    )
                elif name == "doctor-consent-required":
                    stdout = (Path(sample.artifact) / "doctor-consent-required.stdout").read_text(
                        encoding="utf-8", errors="replace")
                    stderr = (Path(sample.artifact) / "doctor-consent-required.stderr").read_text(
                        encoding="utf-8", errors="replace")
                    report = project / "recommendations.md"
                    no_report = not report.exists() and not report.is_symlink()
                    expected_rejection = (
                        not sample.capped and sample.exit_code == 2
                        and "consent-required" in stdout + stderr
                        and no_report
                    )
                    rejection_note = (
                        "expected non-TTY fail-closed rejection: consent-required; no model review was performed"
                        if expected_rejection else
                        "expected consent-required rejection was not fully observed; this is not a successful model review"
                    )
                    report_note = (
                        "no recommendations.md report was created"
                        if no_report else
                        "recommendations.md exists after rejection; report absence contract failed"
                    )
                    attempt = replace(
                        attempt,
                        status=("blocked-unverified" if expected_rejection
                                else "capped" if sample.capped else "failed"),
                        notes=tuple(attempt.notes) + (rejection_note, report_note),
                    )
                attempts.append(attempt)
            attempts.append(Attempt(
                name="cancel", command=(candidate.path, "--fixture-domain", str(domain), "cancel"),
                cwd=str(project), status="blocked-unverified", exit_code=None, seconds=None,
                artifact=None, setup="ptest has no public cancel subcommand in this candidate",
                network="none", notes=("cancellation requires an externally held run and is not fabricated",),
            ))
    else:
        attempts.append(Attempt(
            name="candidate-workload",
            command=(str(ptest.absolute()), "--version"),
            cwd=str(root),
            status="not-run",
            exit_code=None,
            seconds=None,
            artifact=None,
            setup="not run; pass --execute to validate candidate and create temporary TOML fixture domain",
            network="none",
            notes=("plan-only inventory; no claim of runtime support",),
        ))
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "acceptance",
        "status": "observed" if execute else "inventory-only",
        "root": str(root),
        "candidate": str(ptest),
        "candidate_identity": asdict(candidate) if execute and attempts and candidate is not None else None,
        "workload": asdict(workload_spec) if workload_spec is not None else WORKLOAD,
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
        "promotable": bool(execute and attempts and all(item.status == "passed" for item in attempts)
                           and any(item.name != "candidate-version" for item in attempts)),
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
    output = args.output
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
    path = Path(evidence["artifacts"]) / "acceptance.json"
    _write_private(path, (json.dumps(evidence, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    print(json.dumps(evidence, indent=2, sort_keys=True))
    return 0 if evidence["promotable"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
