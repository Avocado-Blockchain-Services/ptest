"""Shared contract codec and default tests (Task0 owned)."""
from __future__ import annotations

import json
import struct

import pytest

from ptest import contracts as C
from ptest.contracts import Problem

RUN_ID = "c" * 32
NONCE = "ab" * 32
WRONG_NONCE = "cd" * 32


def test_bootstrap_smoke():
    assert C.PTEST_VERSION == "0.1.0"
    assert C.SCHEMA_VERSION == 1
    assert C.MAX_PROMPT_BYTES == 65536
    assert C.ExecutionTier("advanced") is C.ExecutionTier.ADVANCED


def _run_data():
    return {
        "run_id": RUN_ID,
        "project_id": "ab" * 16,
        "checkout_id": "d" * 32,
        "mode": "full",
        "status": "passed",
        "phase": "complete",
        "started_at": "2026-09-17T00:00:00+00:00",
        "finished_at": "2026-09-17T00:00:01+00:00",
        "plan": {
            "mode": "full", "execution": "full", "files": [],
            "reasons": [], "input_digest": None, "compatibility": None,
            "baseline_run_id": None, "static_preview": False,
        },
        "command": {
            "kind": "pytest", "mode": "full", "argument_count": 6,
            "generated_options": [], "workers": 1,
            "provenance": ["test"],
        },
        "granted_workers": 1,
        "memory_estimate_mb": None,
        "reserved_memory_mb": None,
        "runner_exit_code": 0,
        "exit_code": 0,
        "exit_origin": "runner",
        "signal": None,
        "source_valid": True,
        "full_gate_eligible": True,
        "baseline_published": False,
        "counts": None,
        "timings": None,
        "attempts": [],
        "reasons": [],
        "limitations": [],
        "artifact_id": None,
    }


def test_unknown_major_version_rejected():
    raw = json.loads(C.encode_public_document("run", _run_data()))
    raw["schema_version"] = 2
    with pytest.raises(Problem, match="protocol-mismatch"):
        C.decode_public_document(json.dumps(raw).encode())


def test_malformed_envelope_rejected():
    with pytest.raises(Problem, match="report-invalid"):
        C.decode_public_document(b"{not json")
    with pytest.raises(Problem, match="report-invalid"):
        C.decode_public_document(json.dumps({"kind": "run"}).encode())


def test_command_summary_withholds_every_token_form():
    argv = [
        "runner-with-argv0-secret-token",
        "tests/positional-secret-token.py",
        "--password=--option-secret-token",
        "--unknown-flag-secret-token",
        "--env-file=dotenv-secret-token",
        "TOKEN=env-value-secret-token",
    ]
    summary = C.summarize_command(
        C.RunnerKind.PYTEST, C.Mode.SCOPED, argv,
        generated_options=("-q",), workers=1, provenance=("adapter",),
    )
    assert summary.argument_count == 6
    assert summary.generated_options == ("-q",)
    data = _run_data()
    data["command"] = {
        "kind": summary.kind.value,
        "mode": summary.mode.value,
        "argument_count": summary.argument_count,
        "generated_options": list(summary.generated_options),
        "workers": summary.workers,
        "provenance": list(summary.provenance),
    }
    rendered = C.encode_public_document("run", data).decode()
    for sentinel in (
        "argv0-secret-token",
        "positional-secret-token",
        "--option-secret-token",
        "--unknown-flag-secret-token",
        "dotenv-secret-token",
        "env-value-secret-token",
    ):
        assert sentinel not in rendered
    payload = json.loads(C.encode_public_document("run", data))["data"]
    assert "argv" not in payload["command"]
    assert "env" not in payload["command"]
    assert payload["command"]["argument_count"] == 6


def test_eight_public_documents_parse():
    payloads = {
        "run": _run_data(),
        "plan": {
            "mode": "automatic", "execution": "selected",
            "files": ["tests/test_a.py"], "reasons": [],
            "input_digest": "e" * 64, "compatibility": "c",
            "baseline_run_id": "f" * 32, "static_preview": False,
        },
        "where": {
            "root": "/repo", "config_path": "/repo/.ptest.toml",
            "initialized": True, "runner_kind": "pytest",
            "capability": "advanced", "commands": [],
            "effective_limits": {"workers": 1},
            "provenance": ["config"], "warnings": [],
        },
        "status": {
            "effective_limits": {"workers": 1},
            "queued": [], "active": [],
        },
        "history": {"summaries": [], "obligations": []},
        "init": {
            "action": "created", "target": "/repo/.ptest.toml",
            "exists": False, "warnings": [], "config": None,
        },
        "doctor": {
            "scope": ["tests"], "readiness": [], "findings": [],
            "limits": {"entries": 1}, "usage": {"entries": 0},
            "limitations": [],
        },
        "register": {
            "root": "/repo", "initialized": False,
            "legacy_present": True, "legacy_local": None,
            "proposed_runner": "pytest", "commands": [],
            "legacy_alias_count": 0,
            "required_actions": ["initialize"], "warnings": [],
        },
    }
    for kind, data in payloads.items():
        doc = C.decode_public_document(C.encode_public_document(kind, data))
        assert doc.kind == kind
        assert doc.error is None
        assert doc.ptest_version == "0.1.0"
    run_doc = C.decode_public_document(C.encode_public_document("run", _run_data()))
    assert run_doc.data["run_id"] == RUN_ID


def test_register_error_matches_schema():
    problem = Problem(code="invalid-config", message="bad", phase="init")
    doc = C.decode_public_document(
        C.encode_public_document("register", None, error=problem)
    )
    assert doc.kind == "register"
    assert doc.data is None
    assert doc.error.code == "invalid-config"
    assert doc.error.message == "bad"
    assert doc.error.phase == "init"
    assert doc.error.retryable is False


def test_encode_rejects_data_with_error():
    problem = Problem(code="x", message="y", phase="z")
    with pytest.raises(ValueError):
        C.encode_public_document("run", _run_data(), error=problem)


def _frame(kind="cancel", payload=None):
    return C.ControlFrame(
        protocol=1, run_id=RUN_ID, nonce=NONCE,
        kind=kind, payload=dict(payload or {"signal": 2}),
    )


def test_control_frame_roundtrip():
    frame = C.decode_control_frame(C.encode_control_frame(_frame()))
    assert frame.kind == "cancel"
    assert frame.payload == {"signal": 2}
    assert frame.nonce == NONCE


def test_frame_truncation_rejected():
    raw = C.encode_control_frame(_frame())
    with pytest.raises(Problem, match="protocol-mismatch"):
        C.decode_control_frame(raw[:-1])
    with pytest.raises(Problem, match="protocol-mismatch"):
        C.decode_control_frame(raw[:2])


def test_frame_wrong_nonce_rejected():
    raw = C.encode_control_frame(_frame())
    with pytest.raises(Problem, match="protocol-mismatch"):
        C.decode_control_frame(raw, expected_nonce=WRONG_NONCE)
    C.decode_control_frame(raw, expected_nonce=NONCE)


def test_frame_oversize_rejected():
    length = struct.pack(">I", C.CONTROL_FRAME_MAX_BYTES + 1)
    with pytest.raises(Problem, match="protocol-mismatch"):
        C.decode_control_frame(length + b'{"protocol":1}')
    oversized = C.ControlFrame(
        protocol=1, run_id=RUN_ID, nonce=NONCE, kind="phase",
        payload={"phase": "execution", "attempt_id": "a001",
                 "blob": "x" * 70000},
    )
    with pytest.raises(Problem, match="protocol-mismatch"):
        C.encode_control_frame(oversized)


def test_frame_wrong_protocol_rejected():
    body = json.dumps({
        "protocol": 2, "run_id": RUN_ID, "nonce": NONCE,
        "kind": "cancel", "payload": {"signal": 2},
    }).encode()
    raw = struct.pack(">I", len(body)) + body
    with pytest.raises(Problem, match="protocol-mismatch"):
        C.decode_control_frame(raw)


def test_frame_wrong_kind_shape_rejected():
    with pytest.raises(ValueError, match="requires"):
        C.ControlFrame(
            protocol=1, run_id=RUN_ID, nonce=NONCE,
            kind="cancel", payload={"blob": "x"},
        )


def test_boundary_integers_and_ids(case):
    case.config(workers=64)
    with pytest.raises(ValueError):
        case.config(workers=65)
    with pytest.raises(ValueError):
        case.config(workers=0)
    with pytest.raises(ValueError):
        case.config(project_id="ab" * 15 + "a")
    with pytest.raises(ValueError):
        case.config(project_id="ab" * 16 + "ab")
    good = case.config()
    assert good.project_id == "ab" * 16
    assert good.runner.workers == 1


def test_selection_ratio_bounds(case):
    cfg = case.config(selection_enabled=True, closed_inputs=True)
    assert cfg.selection.full_ratio == 0.70
    with pytest.raises(ValueError):
        C.SelectionPolicy(
            enabled=True, closed_inputs=True, input_roots=("src",),
            ignored_inputs=(), environment=(), full_triggers=(),
            always=(), no_tests=(), non_input_outputs=(),
            full_ratio=0.09, groups=(),
        )
    with pytest.raises(ValueError):
        C.SelectionPolicy(
            enabled=True, closed_inputs=True, input_roots=("src",),
            ignored_inputs=(), environment=(), full_triggers=(),
            always=(), no_tests=(), non_input_outputs=(),
            full_ratio=1.1, groups=(),
        )


def test_default_constants_match_design():
    limits = C.DEFAULT_SCAN_LIMITS
    assert (limits.entries, limits.files) == (20000, 2000)
    assert (limits.file_bytes, limits.total_bytes) == (262144, 16777216)
    assert (limits.findings, limits.output_bytes) == (200, 262144)
    assert (limits.elapsed_s, limits.depth, limits.ast_nodes) == (8.0, 256, 50000)
    assert C.MAX_SCAN_LIMITS.entries == 100000
    assert C.MAX_SCAN_LIMITS.files == 10000
    assert C.MAX_SCAN_LIMITS.file_bytes == 1048576
    assert C.MAX_SCAN_LIMITS.total_bytes == 67108864
    assert C.DEFAULT_QUEUE_TIMEOUT_S == 1800.0
    assert C.MAX_QUEUE_TIMEOUT_S == 86400.0
    assert C.CANCEL_GRACE_S == 3.0
    assert C.SCHEDULER_POLL_S == 0.25


def test_execution_tier_is_closed():
    for value in (
        "advanced", "basic_serial", "bounded_native",
        "exclusive_command", "unavailable",
    ):
        assert C.ExecutionTier(value).value == value
    with pytest.raises(ValueError):
        C.Capability(
            execution="turbo", selection=False,
            lifecycle="cooperative-process-group", limitations=(),
        )


def test_problem_string_hides_details():
    problem = Problem(code="unsafe-path", message="escape", phase="files")
    assert str(problem) == "unsafe-path: escape"
    assert problem.retryable is False
    assert not hasattr(problem, "details")


def test_launch_manifest_rejects_bad_attempts(case):
    domain = case.domain()
    grant = C.Grant(
        run_id=RUN_ID, nonce=NONCE, slots=1,
        memory_estimate_mb=None, reserved_memory_mb=None,
        generation=1, domain_id="d" * 32,
    )
    prepared = C.PreparedRun(
        argv=("true",), cwd=domain.root, env_updates=(),
        report_path=domain.root / "report.json",
        capability=C.Capability(
            execution="exclusive_command", selection=False,
            lifecycle="cooperative-process-group", limitations=(),
        ),
        summary=C.summarize_command(
            C.RunnerKind.COMMAND, C.Mode.SCOPED, ["true"],
            workers=1, provenance=("test",),
        ),
    )
    good = C.LaunchManifest(
        protocol=1, domain=domain, grant=grant, setup=None,
        attempts=(prepared,), attempt_ids=("a001",),
        setup_timeout_s=300.0, attempt_timeout_s=30.0,
        compound_timeout_s=600.0,
    )
    decoded = C.decode_launch_manifest(C.encode_launch_manifest(good))
    assert decoded.grant.nonce == NONCE
    assert decoded.attempt_ids == ("a001",)
    with pytest.raises(ValueError):
        C.LaunchManifest(
            protocol=1, domain=domain, grant=grant, setup=None,
            attempts=(prepared,), attempt_ids=("a001", "a002"),
            setup_timeout_s=300.0, attempt_timeout_s=30.0,
            compound_timeout_s=600.0,
        )
    with pytest.raises(ValueError):
        C.LaunchManifest(
            protocol=1, domain=domain, grant=grant, setup=None,
            attempts=(), attempt_ids=(),
            setup_timeout_s=300.0, attempt_timeout_s=30.0,
            compound_timeout_s=600.0,
        )


def test_unknown_config_alias_raises(case):
    with pytest.raises(ValueError, match="unknown config override"):
        case.config(bogus_field=True)
    with pytest.raises(ValueError, match="unknown result override"):
        case.result(bogus_field=True)
