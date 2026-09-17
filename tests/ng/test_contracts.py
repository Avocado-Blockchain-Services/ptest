"""Shared contract codec and default tests (Task0 owned)."""
from __future__ import annotations

import json
import struct
from pathlib import Path

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
    sentinels = (
        "argv0-secret-token",
        "positional-secret-token",
        "--option-secret-token",
        "--unknown-flag-secret-token",
        "dotenv-secret-token",
        "env-value-secret-token",
    )
    for token in argv:
        assert token not in ("-q",)
    result = _secret_result(argv)
    payload = C.serialize_run_result(result)
    assert payload["command"]["argument_count"] == 6
    assert payload["command"]["generated_options"] == ["-q"]
    assert "argv" not in payload["command"]
    assert "env" not in payload["command"]
    for internal in ("sequence", "input_before", "input_after",
                     "policy_digest"):
        assert internal not in payload, internal
    assert payload["mode"] == "shadow"
    assert payload["plan"]["mode"] == "automatic"
    rendered = C.encode_public_document("run", payload).decode()
    for sentinel in sentinels:
        assert sentinel not in rendered
    revived = C.decode_public_document(
        C.encode_public_document("run", payload))
    assert revived.data["mode"] == "shadow"
    assert revived.data["command"]["argument_count"] == 6


def _secret_result(argv):
    summary = C.summarize_command(
        C.RunnerKind.PYTEST, C.Mode.SCOPED, argv,
        generated_options=("-q",), workers=1, provenance=("adapter",),
    )
    assert summary.argument_count == len(argv)
    assert summary.generated_options == ("-q",)
    plan = C.Plan(
        mode=C.Mode.AUTOMATIC, execution="full", files=(), reasons=(),
        input_digest=None, compatibility=None, baseline_run_id=None,
        static_preview=False,
    )
    snapshot = C.InputSnapshot(
        digest="e" * 64, compatibility="c", head="a" * 40, clean=True,
        changes=(), limitations=(), files=(),
    )
    return C.RunResult(
        run_id=RUN_ID, project_id="ab" * 16, checkout_id="d" * 32,
        mode=C.Mode.SHADOW, status="passed", phase="complete",
        started_at="2026-09-17T00:00:00+00:00",
        finished_at="2026-09-17T00:00:01+00:00",
        plan=plan, command=summary,
        counts=C.Counts(collected=6, executed=6, passed=6, failed=0,
                        skipped=0, unknown=None),
        timings=C.Timings(queue_s=0.1, setup_s=0.2, collection_s=0.3,
                          execution_s=0.4, finalization_s=0.5),
        attempts=(C.AttemptResult(
            attempt_id="a001", phase="execution", status="passed",
            raw_exit_code=0, final_exit_code=0, source_valid=True,
            inventory_complete=True, timings=None,
        ),),
        sequence=7, input_before=snapshot, input_after=snapshot,
        policy_digest="f" * 64,
    )


def _capability_data():
    capability = C.Capability(
        execution=C.ExecutionTier.ADVANCED, selection=True,
        lifecycle="cooperative-process-group", limitations=(),
    )
    return C._capability_dict(capability)


def _full_command_data():
    summary = C.summarize_command(
        C.RunnerKind.PYTEST, C.Mode.FULL, ["-q"],
        workers=1, provenance=("test",),
    )
    return C._command_dict(summary)


def _lease_data():
    lease = C.LeaseView(
        run_id=RUN_ID, checkout_id="d" * 32, state=C.LeaseState.QUEUED,
        sequence=0, requested_slots=1, slots=1, memory_estimate_mb=None,
        reserved_memory_mb=None, phase="execution", age_s=0.5,
        queue_wait_s=0.25, ownership="certain", fixture=True, reasons=(),
    )
    return C._lease_dict(lease)


def _obligation_data():
    obligation = C.Obligation(
        file="tests/test_a.py", test_id=None, sequence=0,
        source_digest="e" * 64, compatibility="c",
        reason="full-gate-obligation",
    )
    return C._obligation_dict(obligation)


def _readiness_data():
    readiness = C.Readiness(
        area="execution", state="ready-for-declared-capability", reasons=(),
    )
    return C._readiness_dict(readiness)


def _finding_data():
    finding = C.Finding(
        code="timing.slow-test", severity="medium", confidence="high",
        path="tests/test_a.py", line=12, evidence_type="static",
        consequence="slow suite", remediation="split the file",
        verification="timer",
    )
    return C._finding_dict(finding)


_SUMMARY_SENTINELS = (
    "launcher-secret-7f1",
    "args-secret-7f1",
    "full-secret-7f1",
    "setup-secret-7f1",
    "env-secret-7f1",
    "positional-secret-7f1",
)


def _summary_case(with_setup=True):
    runner = C.RunnerConfig(
        kind=C.RunnerKind.PYTEST,
        launcher=("python", "launcher-secret-7f1"),
        args=("--args-secret-7f1",),
        full_args=("--full-secret-7f1",),
        test_roots=("tests",),
        workers=4,
        lifecycle="cooperative-process-group",
    )
    setup = None
    if with_setup:
        setup = C.SetupConfig(
            argv=("setup-secret-7f1",),
            required_paths=("pyproject.toml",),
            network=True,
            lifecycle_scripts=True,
        )
    config = C.Config(
        runner=runner,
        setup=setup,
        resources=C.ResourceConfig(
            locks=(), memory_mb_per_worker=0,
            probe_isolation="undeclared",
        ),
        selection=C.SelectionPolicy(
            enabled=True, closed_inputs=True, input_roots=("src",),
            ignored_inputs=(), environment=("ENV-secret=env-secret-7f1",),
            full_triggers=(), always=(), no_tests=(),
            non_input_outputs=(), full_ratio=0.70, groups=(),
        ),
        project_id="ab" * 16,
    )
    scoped = C.summarize_command(
        C.RunnerKind.PYTEST, C.Mode.SCOPED,
        ["-q", "positional-secret-7f1"],
        generated_options=("-q",), workers=4, provenance=("adapter",),
    )
    full = C.summarize_command(
        C.RunnerKind.PYTEST, C.Mode.FULL, ["-q"],
        generated_options=(), workers=4, provenance=("adapter",),
    )
    return config, scoped, full


def _limits_data():
    return C._effective_limits_dict(C.EffectiveLimits(
        max_slots=8, max_jobs=4, memory_mb=4096, repo_workers=2))


def _init_result():
    config, scoped, full = _summary_case()
    summary = C.summarize_config(config, scoped=scoped, full=full)
    return C.InitResult(
        action="created", target=Path("/repo/.ptest.toml"), exists=True,
        config=summary, warnings=(),
    )


def _full_payloads():
    return {
        "run": C.serialize_run_result(_secret_result(["-q"])),
        "plan": {
            "mode": "automatic", "execution": "selected",
            "files": ["tests/test_a.py"], "reasons": [],
            "input_digest": "e" * 64, "compatibility": "c",
            "baseline_run_id": "f" * 32, "static_preview": False,
        },
        "where": {
            "root": "/repo", "config_path": "/repo/.ptest.toml",
            "initialized": True, "runner_kind": "pytest",
            "capability": _capability_data(),
            "commands": [_full_command_data()],
            "effective_limits": _limits_data(),
            "provenance": ["config"], "warnings": [],
        },
        "status": {
            "effective_limits": _limits_data(),
            "queued": [_lease_data()], "active": [],
        },
        "history": {
            "summaries": [C.serialize_run_result(_secret_result(["-q"]))],
            "obligations": [_obligation_data()],
        },
        "init": C.serialize_init_result(_init_result()),
        "doctor": {
            "scope": ["tests"], "readiness": [_readiness_data()],
            "findings": [_finding_data()],
            "limits": C._scan_limits_dict(C.DEFAULT_SCAN_LIMITS),
            "usage": C._scan_usage_dict(C.ScanUsage(
                entries=2, files=1, file_bytes=128, total_bytes=256,
                findings=1, output_bytes=512, elapsed_s=0.5, skipped=0,
                truncated=False,
            )),
            "limitations": [],
        },
        "register": {
            "root": "/repo", "initialized": False,
            "legacy_present": True, "legacy_local": None,
            "proposed_runner": "pytest", "commands": [_full_command_data()],
            "legacy_alias_count": 0,
            "required_actions": ["initialize"], "warnings": [],
        },
    }


def test_eight_public_documents_parse():
    for kind, data in _full_payloads().items():
        doc = C.decode_public_document(C.encode_public_document(kind, data))
        assert doc.kind == kind
        assert doc.error is None
        assert doc.ptest_version == "0.1.0"
    run_doc = C.decode_public_document(C.encode_public_document("run", _run_data()))
    assert run_doc.data["run_id"] == RUN_ID
    full = C.decode_public_document(
        C.encode_public_document("run", _full_payloads()["run"]))
    assert full.data["mode"] == "shadow"
    assert full.data["counts"]["collected"] == 6
    assert full.data["attempts"][0]["attempt_id"] == "a001"
    assert full.data["attempts"][0]["timings"] is None
    where_doc = C.decode_public_document(
        C.encode_public_document("where", _full_payloads()["where"]))
    assert where_doc.data["capability"]["selection"] is True
    assert where_doc.data["capability"]["execution"] == "advanced"
    status_doc = C.decode_public_document(
        C.encode_public_document("status", _full_payloads()["status"]))
    assert status_doc.data["queued"][0]["state"] == "QUEUED"
    doctor_doc = C.decode_public_document(
        C.encode_public_document("doctor", _full_payloads()["doctor"]))
    assert doctor_doc.data["findings"][0]["code"] == "timing.slow-test"
    assert doctor_doc.data["limits"]["ast_nodes"] == 50000
    init_doc = C.decode_public_document(
        C.encode_public_document("init", _full_payloads()["init"]))
    assert init_doc.data["action"] == "created"
    assert init_doc.data["exists"] is True
    assert init_doc.data["config"]["project_id"] == "ab" * 16
    assert [c["mode"] for c in init_doc.data["config"]["commands"]] == [
        "scoped", "full"]
    history_doc = C.decode_public_document(
        C.encode_public_document("history", _full_payloads()["history"]))
    assert history_doc.data["summaries"][0]["run_id"] == RUN_ID
    assert history_doc.data["summaries"][0]["mode"] == "shadow"
    for internal in ("sequence", "input_before", "input_after",
                     "policy_digest"):
        assert internal not in history_doc.data["summaries"][0]


def test_generated_schema_files_match_frozen_shapes():
    root = Path(__file__).resolve().parents[2]
    schemas = {}
    for kind in ("run", "plan", "where", "status", "history", "init",
                 "doctor", "register"):
        path = root / "docs" / "schemas" / "v1" / f"{kind}.json"
        schemas[kind] = json.loads(path.read_text(encoding="utf-8"))
    capability = schemas["where"]["properties"]["data"]["properties"][
        "capability"]
    assert capability["type"] == ["object", "null"]
    assert sorted(capability["required"]) == [
        "execution", "lifecycle", "limitations", "selection"]
    attempts = schemas["run"]["properties"]["data"]["properties"][
        "attempts"]["items"]
    assert "timings" in attempts["required"]
    assert sorted(attempts["properties"]["timings"]["required"]) == [
        "collection", "execution", "finalization", "queue", "setup"]
    counts = schemas["run"]["properties"]["data"]["properties"]["counts"]
    assert sorted(counts["required"]) == [
        "collected", "executed", "failed", "passed", "skipped", "unknown"]
    queued = schemas["status"]["properties"]["data"]["properties"][
        "queued"]["items"]
    assert "ownership" in queued["required"]
    obligations = schemas["history"]["properties"]["data"]["properties"][
        "obligations"]["items"]
    assert "source_digest" in obligations["required"]
    limits = schemas["doctor"]["properties"]["data"]["properties"]["limits"]
    assert "ast_nodes" in limits["required"]
    findings = schemas["doctor"]["properties"]["data"]["properties"][
        "findings"]["items"]
    assert findings["properties"]["code"]["enum"] == sorted(
        C.FINDING_CODES)


def test_frozen_subrecord_negatives_rejected():
    where = _full_payloads()["where"]
    where["capability"] = "advanced"
    with pytest.raises(Problem, match="report-invalid"):
        C.decode_public_document(C.encode_public_document("where", where))
    status = _full_payloads()["status"]
    status["queued"] = [{}]
    with pytest.raises(Problem, match="report-invalid"):
        C.decode_public_document(C.encode_public_document("status", status))
    doctor = _full_payloads()["doctor"]
    doctor["findings"] = [dict(_finding_data(), severity="critical")]
    with pytest.raises(Problem, match="report-invalid"):
        C.decode_public_document(C.encode_public_document("doctor", doctor))
    run = _full_payloads()["run"]
    run["counts"] = dict(run["counts"], collected="many")
    with pytest.raises(Problem, match="report-invalid"):
        C.decode_public_document(C.encode_public_document("run", run))
    run = _full_payloads()["run"]
    entry = _attempt_dict_for_negative()
    entry.pop("timings")
    run["attempts"] = [entry]
    with pytest.raises(Problem, match="report-invalid"):
        C.decode_public_document(C.encode_public_document("run", run))
    run = _full_payloads()["run"]
    run["reasons"] = [{"code": "not-a-reason", "message": "x",
                       "paths": []}]
    with pytest.raises(Problem, match="report-invalid"):
        C.decode_public_document(C.encode_public_document("run", run))
# Additive same-major public fields are dropped on decode, never rejected;
# test_public_decode_drops_additive_unknowns covers that projection.


def _attempt_dict_for_negative():
    return C._attempt_dict(C.AttemptResult(
        attempt_id="a001", phase="execution", status="passed",
        raw_exit_code=0, final_exit_code=0, source_valid=True,
        inventory_complete=True, timings=None,
    ))


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


def _nested_frame_raw(depth):
    pad = "[" * depth + "]" * depth
    body = (
        '{"protocol":1,"run_id":"' + RUN_ID + '","nonce":"' + NONCE
        + '","kind":"cancel","payload":{"signal":2,"pad":' + pad + "}}"
    ).encode()
    assert len(body) <= C.CONTROL_FRAME_MAX_BYTES
    return struct.pack(">I", len(body)) + body


def test_frame_nesting_past_declared_bound_rejected():
    with pytest.raises(Problem, match="protocol-mismatch"):
        C.decode_control_frame(_nested_frame_raw(20))


def test_frame_pathological_nesting_is_typed_rejection():
    with pytest.raises(Problem, match="protocol-mismatch"):
        C.decode_control_frame(_nested_frame_raw(20000))


def test_document_pathological_nesting_is_typed_rejection():
    pad = "[" * 20000 + "]" * 20000
    raw = (
        '{"schema_version":1,"kind":"run","ptest_version":"0.1.0",'
        '"domain":null,"data":{"x":' + pad + '},"error":null}'
    ).encode()
    with pytest.raises(Problem, match="report-invalid"):
        C.decode_public_document(raw)


def test_manifest_pathological_nesting_is_typed_rejection(case):
    domain = case.domain()
    grant = C.Grant(
        run_id=RUN_ID, nonce=NONCE, slots=1,
        memory_estimate_mb=None, reserved_memory_mb=None,
        generation=1, domain_id="d" * 32,
    )
    prepared = C.PreparedRun(
        argv=("true",), cwd=domain.root, env_updates=(),
        report_path=domain.root / "report.json",
        capability=None, summary=None,
    )
    manifest = C.LaunchManifest(
        protocol=1, domain=domain, grant=grant, setup=None,
        attempts=(prepared,), attempt_ids=("a001",),
        setup_timeout_s=300.0, attempt_timeout_s=30.0,
        compound_timeout_s=600.0,
    )
    body = json.loads(C.encode_launch_manifest(manifest)[4:])
    base = json.dumps(body).encode()
    pad = ("[" * 20000 + "]" * 20000).encode()
    tampered = base.replace(b'"env_updates": []',
                            b'"env_updates": ' + pad, 1)
    assert tampered != base
    assert len(tampered) <= C.MANIFEST_MAX_BYTES
    with pytest.raises(Problem, match="protocol-mismatch"):
        C.decode_launch_manifest(struct.pack(">I", len(tampered)) + tampered)


def test_control_frame_values_validated_per_kind():
    guard = {"pid": 1, "birth": 0.0, "uid": 0, "pgid": 1}
    frame = C.ControlFrame(
        protocol=1, run_id=RUN_ID, nonce=NONCE,
        kind="registered", payload={"guard": guard},
    )
    assert C.decode_control_frame(
        C.encode_control_frame(frame)).payload == {"guard": guard}
    for kind, payload in (
        ("parent-closing", {"extra": 1}),
        ("phase", {"phase": "launch", "attempt_id": None}),
        ("phase", {"phase": "execution", "attempt_id": 7}),
        ("runner-facts", {"attempt_id": "a001", "phase": "finalization",
                          "raw_exit_code": None, "report_name": None,
                          "problem": None}),
        ("runner-facts", {"attempt_id": "a001", "phase": "execution",
                          "raw_exit_code": "0", "report_name": None,
                          "problem": None}),
        ("registered", {"guard": {"pid": 1}}),
        ("draining", {"provisional_artifact_id": 7}),
    ):
        with pytest.raises((TypeError, ValueError)):
            C.ControlFrame(
                protocol=1, run_id=RUN_ID, nonce=NONCE,
                kind=kind, payload=payload,
            )


def test_manifest_decoder_hides_offending_values(case):
    domain = case.domain()
    grant = C.Grant(
        run_id=RUN_ID, nonce=NONCE, slots=1,
        memory_estimate_mb=None, reserved_memory_mb=None,
        generation=1, domain_id="d" * 32,
    )
    prepared = C.PreparedRun(
        argv=("true",), cwd=domain.root, env_updates=(),
        report_path=domain.root / "report.json",
        capability=None, summary=None,
    )
    manifest = C.LaunchManifest(
        protocol=1, domain=domain, grant=grant, setup=None,
        attempts=(prepared,), attempt_ids=("a001",),
        setup_timeout_s=300.0, attempt_timeout_s=30.0,
        compound_timeout_s=600.0,
    )
    body = json.loads(C.encode_launch_manifest(manifest)[4:])
    sentinel = "SECRET-SENTINEL-9f2c"
    body["attempts"][0]["summary"] = {
        "kind": "command", "mode": "scoped", "argument_count": 1,
        "generated_options": ["not-an-option!! " + sentinel],
        "workers": 1, "provenance": [],
    }
    tampered = json.dumps(body).encode()
    raw = struct.pack(">I", len(tampered)) + tampered
    with pytest.raises(Problem) as caught:
        C.decode_launch_manifest(raw)
    assert caught.value.code == "protocol-mismatch"
    assert caught.value.message == "manifest attempt failed validation"
    assert sentinel not in str(caught.value)
    rendered = C.encode_public_document(
        "register", None, error=caught.value).decode()
    assert sentinel not in rendered


def test_control_frame_decoder_hides_offending_values():
    body = (
        '{"protocol":1,"run_id":"' + RUN_ID + '","nonce":"' + NONCE
        + '","kind":"cancel","payload":{"signal":999}}'
    ).encode()
    raw = struct.pack(">I", len(body)) + body
    with pytest.raises(Problem) as caught:
        C.decode_control_frame(raw)
    assert caught.value.code == "protocol-mismatch"
    assert caught.value.message == "control frame payload failed validation"
    assert "999" not in str(caught.value)


def test_capability_limitations_require_reason_records():
    with pytest.raises(TypeError):
        C.Capability(
            execution="advanced", selection=False,
            lifecycle="cooperative-process-group",
            limitations=({"code": "scan-limit", "message": "x",
                         "paths": []},),
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


def test_init_action_is_closed_enum():
    assert [item.value for item in C.InitAction] == [
        "created", "preview", "existing"]
    good = C.InitResult(
        action=C.InitAction.CREATED, target=Path("/repo/.ptest.toml"),
        exists=True, config=None, warnings=())
    assert good.action is C.InitAction.CREATED
    coerced = C.InitResult(
        action="preview", target=Path("/repo/.ptest.toml"),
        exists=False, config=None, warnings=())
    assert coerced.action is C.InitAction.PREVIEW
    with pytest.raises(ValueError):
        C.InitResult(
            action="deleted", target=Path("/repo/.ptest.toml"),
            exists=False, config=None, warnings=())
    with pytest.raises(ValueError):
        C.InitResult(
            action="preview", target=Path("/repo/.ptest.toml"),
            exists=True, config=None, warnings=())
    with pytest.raises(ValueError):
        C.InitResult(
            action="created", target=Path("/repo/.ptest.toml"),
            exists=False, config=None, warnings=())
    with pytest.raises(ValueError):
        C.InitResult(
            action="existing", target=Path("/repo/.ptest.toml"),
            exists=False, config=None, warnings=())


def test_select_init_action_precedence():
    assert C.select_init_action(
        target_exists=True, dry_run=True, created=False) == (
        C.InitAction.EXISTING, True)
    assert C.select_init_action(
        target_exists=True, dry_run=False, created=False) == (
        C.InitAction.EXISTING, True)
    assert C.select_init_action(
        target_exists=False, dry_run=True, created=False) == (
        C.InitAction.PREVIEW, False)
    assert C.select_init_action(
        target_exists=False, dry_run=False, created=True) == (
        C.InitAction.CREATED, True)
    with pytest.raises(ValueError):
        C.select_init_action(
            target_exists=False, dry_run=False, created=False)


def test_config_summary_requires_scoped_then_full():
    config, scoped, full = _summary_case()
    summary = C.summarize_config(config, scoped=scoped, full=full)
    assert summary.project_id == "ab" * 16
    assert summary.runner_kind is C.RunnerKind.PYTEST
    assert summary.workers == 4
    assert [item.mode for item in summary.commands] == [
        C.Mode.SCOPED, C.Mode.FULL]
    assert (summary.setup_configured, summary.setup_network,
            summary.setup_lifecycle_scripts) == (True, True, True)
    assert (summary.selection_enabled, summary.closed_inputs) == (True, True)
    with pytest.raises(ValueError):
        C.summarize_config(config, scoped=full, full=scoped)
    with pytest.raises(ValueError):
        C.summarize_config(config, scoped=scoped, full=scoped)
    other = C.summarize_command(
        C.RunnerKind.GO, C.Mode.FULL, ["-q"],
        workers=1, provenance=("test",))
    with pytest.raises(ValueError):
        C.summarize_config(config, scoped=scoped, full=other)
    with pytest.raises(ValueError):
        C.ConfigSummary(
            project_id="zz", runner_kind=C.RunnerKind.PYTEST, workers=4,
            commands=(scoped, full), setup_configured=True,
            setup_network=True, setup_lifecycle_scripts=True,
            selection_enabled=True, closed_inputs=True)
    with pytest.raises(ValueError):
        C.ConfigSummary(
            project_id="ab" * 16, runner_kind=C.RunnerKind.PYTEST,
            workers=65, commands=(scoped, full), setup_configured=True,
            setup_network=True, setup_lifecycle_scripts=True,
            selection_enabled=True, closed_inputs=True)
    with pytest.raises(ValueError):
        C.ConfigSummary(
            project_id="ab" * 16, runner_kind=C.RunnerKind.PYTEST,
            workers=4, commands=(scoped,), setup_configured=True,
            setup_network=True, setup_lifecycle_scripts=True,
            selection_enabled=True, closed_inputs=True)


def test_config_summary_absent_setup_clears_setup_flags():
    config, scoped, full = _summary_case(with_setup=False)
    summary = C.summarize_config(config, scoped=scoped, full=full)
    assert (summary.setup_configured, summary.setup_network,
            summary.setup_lifecycle_scripts) == (False, False, False)


def test_effective_limits_pairing_and_bounds():
    ok = C.EffectiveLimits(
        max_slots=8, max_jobs=8, memory_mb=4096, repo_workers=4)
    assert ok.max_jobs <= ok.max_slots
    none_ok = C.EffectiveLimits(
        max_slots=None, max_jobs=None, memory_mb=None, repo_workers=None)
    assert none_ok.max_slots is None
    with pytest.raises(ValueError):
        C.EffectiveLimits(
            max_slots=8, max_jobs=None, memory_mb=None, repo_workers=None)
    with pytest.raises(ValueError):
        C.EffectiveLimits(
            max_slots=None, max_jobs=2, memory_mb=None, repo_workers=None)
    with pytest.raises(ValueError):
        C.EffectiveLimits(
            max_slots=4, max_jobs=8, memory_mb=None, repo_workers=None)
    with pytest.raises(ValueError):
        C.EffectiveLimits(
            max_slots=0, max_jobs=0, memory_mb=None, repo_workers=None)
    with pytest.raises(ValueError):
        C.EffectiveLimits(
            max_slots=8, max_jobs=8, memory_mb=32, repo_workers=None)
    with pytest.raises(ValueError):
        C.EffectiveLimits(
            max_slots=8, max_jobs=8, memory_mb=None, repo_workers=65)


def test_public_init_roundtrip_uses_config_summary():
    payload = C.serialize_init_result(_init_result())
    assert set(payload) == {
        "action", "target", "exists", "warnings", "config"}
    assert payload["action"] == "created"
    assert payload["target"] == "/repo/.ptest.toml"
    assert set(payload["config"]) == {
        "project_id", "runner_kind", "workers", "commands",
        "setup_configured", "setup_network", "setup_lifecycle_scripts",
        "selection_enabled", "closed_inputs"}
    assert [item["mode"] for item in payload["config"]["commands"]] == [
        "scoped", "full"]
    rendered = C.encode_public_document("init", payload).decode()
    for sentinel in _SUMMARY_SENTINELS:
        assert sentinel not in rendered
    revived = C.decode_public_document(
        C.encode_public_document("init", payload))
    assert revived.data["action"] == "created"
    assert revived.data["config"]["project_id"] == "ab" * 16
    assert revived.data["config"]["workers"] == 4


def test_init_payload_negatives_rejected():
    bad = {"action": "deleted", "target": "/repo/.ptest.toml",
           "exists": False, "warnings": [], "config": None}
    with pytest.raises(Problem, match="report-invalid"):
        C.decode_public_document(C.encode_public_document("init", bad))
    mismatch = {"action": "preview", "target": "/repo/.ptest.toml",
                "exists": True, "warnings": [], "config": None}
    with pytest.raises(Problem, match="report-invalid"):
        C.decode_public_document(
            C.encode_public_document("init", mismatch))
    partial = {"action": "created", "target": "/repo/.ptest.toml",
               "exists": True, "warnings": [],
               "config": {"project_id": "ab" * 16}}
    with pytest.raises(Problem, match="report-invalid"):
        C.decode_public_document(
            C.encode_public_document("init", partial))
    where = _full_payloads()["where"]
    where["effective_limits"] = {"max_slots": 8, "max_jobs": None,
                                 "memory_mb": None, "repo_workers": None}
    with pytest.raises(Problem, match="report-invalid"):
        C.decode_public_document(C.encode_public_document("where", where))
    status = _full_payloads()["status"]
    status["effective_limits"] = {"max_slots": 2, "max_jobs": 9,
                                  "memory_mb": None, "repo_workers": None}
    with pytest.raises(Problem, match="report-invalid"):
        C.decode_public_document(
            C.encode_public_document("status", status))


def test_history_summaries_reuse_run_payload():
    run_payload = C.serialize_run_result(_secret_result(["-q"]))
    history = {"summaries": [run_payload],
               "obligations": [_obligation_data()]}
    revived = C.decode_public_document(
        C.encode_public_document("history", history))
    assert revived.data["summaries"][0]["run_id"] == RUN_ID
    assert revived.data["summaries"][0]["counts"]["collected"] == 6
    assert revived.data["summaries"][0]["attempts"][0]["attempt_id"] == "a001"
    for internal in ("sequence", "input_before", "input_after",
                     "policy_digest"):
        assert internal not in revived.data["summaries"][0]
    tampered = {"summaries": [dict(run_payload, mode="turbo")],
                "obligations": []}
    with pytest.raises(Problem, match="report-invalid"):
        C.decode_public_document(
            C.encode_public_document("history", tampered))
    smuggled = {"summaries": [dict(run_payload, sequence=7)],
                "obligations": []}
    dropped = C.decode_public_document(
        C.encode_public_document("history", smuggled))
    assert "sequence" not in dropped.data["summaries"][0]
    empty = C.decode_public_document(
        C.encode_public_document(
            "history", {"summaries": [], "obligations": []}))
    assert empty.data == {"summaries": [], "obligations": []}


def test_public_decode_drops_additive_unknowns():
    sentinel = "smuggled-additive-4d7"
    payloads = _full_payloads()
    where = payloads["where"]
    where["capability"] = dict(
        where["capability"], future_note=sentinel)
    where["commands"][0] = dict(
        where["commands"][0], future_count=3)
    where["effective_limits"] = dict(
        where["effective_limits"], future_ceiling=9)
    where["future_top"] = {"argv": [sentinel], "env": {"K": sentinel}}
    run = payloads["run"]
    run["counts"] = dict(run["counts"], future_total=1)
    attempt = dict(run["attempts"][0])
    attempt["timings"] = dict(C._timings_dict(C.Timings(
        queue_s=0.1, setup_s=0.2, collection_s=0.3,
        execution_s=0.4, finalization_s=0.5)), future_phase=sentinel)
    attempt["future_attempt"] = sentinel
    run["attempts"] = [attempt]
    run["future_run"] = sentinel
    status = payloads["status"]
    status["queued"][0] = dict(status["queued"][0], future_lease=sentinel)
    status["effective_limits"] = dict(
        status["effective_limits"], future_ceiling=1)
    history = payloads["history"]
    history["summaries"][0] = dict(
        history["summaries"][0], future_summary=sentinel)
    history["obligations"][0] = dict(
        history["obligations"][0], future_obligation=sentinel)
    init = payloads["init"]
    init["config"] = dict(init["config"], future_config=sentinel)
    init["config"]["commands"][0] = dict(
        init["config"]["commands"][0], future_command=sentinel)
    init["future_init"] = sentinel
    doctor = payloads["doctor"]
    doctor["readiness"][0] = dict(
        doctor["readiness"][0], future_readiness=sentinel)
    doctor["findings"][0] = dict(
        doctor["findings"][0], future_finding=sentinel)
    doctor["limits"] = dict(doctor["limits"], future_limit=sentinel)
    doctor["usage"] = dict(doctor["usage"], future_usage=sentinel)
    register = payloads["register"]
    register["commands"][0] = dict(
        register["commands"][0], future_command=sentinel)
    payloads["plan"]["future_plan"] = sentinel
    for kind, data in payloads.items():
        doc = C.decode_public_document(
            C.encode_public_document(kind, data))
        assert doc.error is None, kind
    revived_where = C.decode_public_document(
        C.encode_public_document("where", payloads["where"])).data
    assert "future_top" not in revived_where
    assert "future_note" not in revived_where["capability"]
    assert "future_count" not in revived_where["commands"][0]
    assert "future_ceiling" not in revived_where["effective_limits"]
    assert revived_where["capability"]["execution"] == "advanced"
    revived_run = C.decode_public_document(
        C.encode_public_document("run", payloads["run"])).data
    assert "future_run" not in revived_run
    assert "future_total" not in revived_run["counts"]
    assert "future_attempt" not in revived_run["attempts"][0]
    assert "future_phase" not in revived_run["attempts"][0]["timings"]
    assert revived_run["attempts"][0]["timings"]["queue"] == 0.1
    revived_status = C.decode_public_document(
        C.encode_public_document("status", payloads["status"])).data
    assert "future_lease" not in revived_status["queued"][0]
    assert "future_ceiling" not in revived_status["effective_limits"]
    revived_history = C.decode_public_document(
        C.encode_public_document("history", payloads["history"])).data
    assert "future_summary" not in revived_history["summaries"][0]
    assert "future_obligation" not in revived_history["obligations"][0]
    assert revived_history["summaries"][0]["run_id"] == RUN_ID
    revived_init = C.decode_public_document(
        C.encode_public_document("init", payloads["init"])).data
    assert "future_init" not in revived_init
    assert "future_config" not in revived_init["config"]
    assert "future_command" not in revived_init["config"]["commands"][0]
    assert revived_init["config"]["commands"][1]["mode"] == "full"
    revived_doctor = C.decode_public_document(
        C.encode_public_document("doctor", payloads["doctor"])).data
    assert "future_readiness" not in revived_doctor["readiness"][0]
    assert "future_finding" not in revived_doctor["findings"][0]
    assert "future_limit" not in revived_doctor["limits"]
    assert "future_usage" not in revived_doctor["usage"]
    revived_register = C.decode_public_document(
        C.encode_public_document("register", payloads["register"])).data
    assert "future_command" not in revived_register["commands"][0]
    revived_plan = C.decode_public_document(
        C.encode_public_document("plan", payloads["plan"])).data
    assert "future_plan" not in revived_plan
    assert revived_plan["execution"] == "selected"
    for kind in ("where", "run", "history", "init"):
        rendered = C.encode_public_document(
            kind, C.decode_public_document(
                C.encode_public_document(
                    kind, payloads[kind])).data).decode()
        assert sentinel not in rendered, kind
    raw = json.loads(C.encode_public_document("where", payloads["where"]))
    raw["future_envelope"] = sentinel
    raw["domain"] = {"id": "ab" * 16, "fixture": False,
                     "future_domain": sentinel}
    envelope_doc = C.decode_public_document(json.dumps(raw).encode())
    assert envelope_doc.domain == {"id": "ab" * 16, "fixture": False}
    problem = Problem(code="x", message="y", phase="z")
    err_raw = json.loads(C.encode_public_document("run", None, error=problem))
    err_raw["error"]["future_error"] = sentinel
    err_doc = C.decode_public_document(json.dumps(err_raw).encode())
    assert set(json.loads(C.encode_public_document(
        "run", None, error=err_doc.error).decode())["error"]) == {
        "code", "message", "phase", "retryable"}


def test_addendum_schema_shapes():
    root = Path(__file__).resolve().parents[2]
    schemas = {}
    for kind in ("run", "plan", "where", "status", "history", "init",
                 "doctor", "register"):
        path = root / "docs" / "schemas" / "v1" / f"{kind}.json"
        schemas[kind] = json.loads(path.read_text(encoding="utf-8"))
    init_data = schemas["init"]["properties"]["data"]["properties"]
    assert init_data["action"] == {
        "type": "string", "enum": ["created", "preview", "existing"]}
    config = init_data["config"]
    assert config["type"] == ["object", "null"]
    assert sorted(config["required"]) == [
        "closed_inputs", "commands", "project_id", "runner_kind",
        "selection_enabled", "setup_configured", "setup_lifecycle_scripts",
        "setup_network", "workers"]
    commands = config["properties"]["commands"]
    assert commands["minItems"] == 2
    assert commands["maxItems"] == 2
    for kind in ("where", "status"):
        limits = schemas[kind]["properties"]["data"]["properties"][
            "effective_limits"]
        assert sorted(limits["required"]) == [
            "max_jobs", "max_slots", "memory_mb", "repo_workers"]
    summaries = schemas["history"]["properties"]["data"]["properties"][
        "summaries"]["items"]
    assert "run_id" in summaries["required"]
    assert "mode" in summaries["required"]
    assert "sequence" not in summaries["required"]
    assert "policy_digest" not in summaries.get("properties", {})
