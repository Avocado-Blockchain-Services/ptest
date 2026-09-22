"""Public agent-assessment document contract (task 1B owned).

TDD contract for ``PublicDocument(kind="agent-assessment")`` against the
frozen Sol-medium contract: strict key/value/status/row/evidence/score shape,
projection of additive unknowns, error shape, and generated schema descriptor.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from ptest import contracts as C
from ptest.checklist import CATALOG
from ptest.contracts import Problem

CHILD_PID = "ab" * 16
PACKET_SHA = "cd" * 32
CITATION_SHA = "ef" * 32
PUB_SHA = "12" * 32

EXPECTED_IDS = (
    "FIX-001", "FIX-002", "DB-001", "DB-002", "CACHE-001",
    "RESOURCE-001", "NETWORK-001", "PROCESS-001", "TIME-001",
    "SELECT-001", "TIMING-001",
)

# Golden bytes recorded before the registry extension (RED baseline).
DOCTOR_GOLDEN_SHA256 = (
    "aee2a07e98e5c6b4cf6862236b71a316cc0b1fe3d0d66b06320cb929b3583a16"
)
DOCTOR_ERROR_GOLDEN_SHA256 = (
    "3e7c8d5f9e5731a4808cc37a39304aca561138fd707e2b74f05c8dd5aa2bc180"
)


def _citation(path="src/example.py", start=3, end=9, sha=CITATION_SHA):
    return {"path": path, "start_line": start, "end_line": end,
            "sha256": sha}


def _row(row_id, status="satisfied", rationale=None, evidence=None):
    if rationale is None:
        rationale = f"Row {row_id} judged {status} against packet excerpt."
    if evidence is None:
        evidence = [] if status == "unknown" else [_citation()]
    return {"id": row_id, "status": status, "rationale": rationale,
            "evidence": evidence}


RECIPES = {
    "FIX-001": "factories", "FIX-002": "factories",
    "DB-001": "databases", "DB-002": "databases",
    "CACHE-001": "cache", "RESOURCE-001": "files-ports",
    "NETWORK-001": "time-network", "PROCESS-001": "processes",
    "TIME-001": "time-network", "SELECT-001": None,
    "TIMING-001": None,
}


def _finding(row_id, recipe="__catalog__"):
    if recipe == "__catalog__":
        recipe = RECIPES[row_id]
    return {"id": row_id, "summary": f"Close gap {row_id} with owned setup.",
            "suggested_change": f"Apply packaged recipe for {row_id}.",
            "recipe_id": recipe, "evidence": [_citation()]}


def _score(satisfied, applicable):
    return {"satisfied": satisfied, "applicable": applicable,
            "percent": (100 * satisfied) // applicable}


def _mixed_rows():
    """One gap (FIX-002), one unknown (DB-001), one N/A (DB-002)."""
    rows = []
    for row_id in EXPECTED_IDS:
        if row_id == "FIX-002":
            rows.append(_row(row_id, "gap"))
        elif row_id == "DB-001":
            rows.append(_row(row_id, "unknown", evidence=[]))
        elif row_id == "DB-002":
            rows.append(_row(row_id, "not-applicable",
                             rationale="Affirmative: child is a pure doc "
                                       "package with no database use, so no "
                                       "ownership rule applies."))
        else:
            rows.append(_row(row_id, "satisfied"))
    return rows


def _child(rows=None, findings=None, score="compute", limitations=(),
           project_id=CHILD_PID, scope="child-a",
           packet_sha256=PACKET_SHA):
    if rows is None:
        rows = _mixed_rows()
    if findings is None:
        findings = [_finding("FIX-002")]
    if score == "compute":
        na_count = sum(1 for row in rows
                       if row["status"] == "not-applicable")
        satisfied = sum(1 for row in rows if row["status"] == "satisfied")
        score = _score(satisfied, 11 - na_count)
    return {"project_id": project_id, "scope": scope,
            "packet_sha256": packet_sha256, "rows": rows, "score": score,
            "findings": findings, "limitations": list(limitations)}


def _na_child():
    rows = [_row(row_id, "not-applicable",
                 rationale=f"Affirmative: scope contains no artifact for "
                           f"{row_id}, confirmed by packet manifest.")
            for row_id in EXPECTED_IDS]
    return _child(rows=rows, findings=[], score=None)


def _payload(children=None, limitations=(), publication=None):
    if children is None:
        children = [_child(), _na_child()]
    if publication is None:
        publication = {"status": "created", "path": "recommendations.md",
                       "sha256": PUB_SHA}
    return {"schema": "ptest.agent-assessment/v1",
            "provider": {"name": "claude", "cli_version": "1.2.3",
                         "profile": "stable"},
            "children": children, "limitations": list(limitations),
            "publication": publication}


def _hostile(data):
    return json.dumps({"schema_version": 1, "kind": "agent-assessment",
                       "ptest_version": "0.1.5", "domain": None,
                       "data": data, "error": None}).encode()


def test_kind_registered_without_disturbing_existing_kinds():
    assert "agent-assessment" in C.PUBLIC_KINDS
    assert C.PUBLIC_KINDS[:8] == (
        "init", "register", "plan", "where",
        "status", "history", "doctor", "run",
    )


def test_catalog_ids_come_from_canonical_checklist():
    assert C.AGENT_ASSESSMENT_CHECKLIST_IDS == EXPECTED_IDS
    assert C.AGENT_ASSESSMENT_CHECKLIST_IDS == tuple(
        entry.id for entry in CATALOG)
    assert C.AGENT_ASSESSMENT_RECIPES == {
        entry.id: entry.recipe for entry in CATALOG}


def test_success_round_trip_projects_exact_keys():
    raw = C.encode_public_document("agent-assessment", _payload())
    assert raw.endswith(b"\n")
    envelope = json.loads(raw.decode())
    assert set(envelope) == {
        "schema_version", "kind", "ptest_version", "domain", "data",
        "error",
    }
    assert envelope["error"] is None
    assert C.encode_public_document("agent-assessment", _payload()) == raw
    doc = C.decode_public_document(raw)
    assert doc.kind == "agent-assessment"
    assert doc.error is None
    assert set(doc.data) == {
        "schema", "provider", "children", "limitations", "publication",
    }
    assert doc.data["schema"] == "ptest.agent-assessment/v1"
    assert set(doc.data["provider"]) == {"name", "cli_version", "profile"}
    child = doc.data["children"][0]
    assert set(child) == {
        "project_id", "scope", "packet_sha256", "rows", "score",
        "findings", "limitations",
    }
    assert [row["id"] for row in child["rows"]] == list(EXPECTED_IDS)
    assert child["score"] == {"satisfied": 8, "applicable": 10,
                              "percent": 80}
    assert [finding["id"] for finding in child["findings"]] == ["FIX-002"]
    assert doc.data["children"][1]["score"] is None
    assert doc.data["children"][1]["findings"] == []
    assert set(doc.data["publication"]) == {"status", "path", "sha256"}


def test_percent_uses_integer_floor():
    rows = [_row("FIX-001", "satisfied")]
    rows += [_row(row_id, "unknown", evidence=[]) for row_id in
             ("FIX-002", "DB-001")]
    rows += [_row(row_id, "gap") for row_id in EXPECTED_IDS[3:]]
    findings = [_finding(row_id) for row_id in EXPECTED_IDS[3:]]
    child = _child(rows=rows, findings=findings,
                   score={"satisfied": 1, "applicable": 11,
                          "percent": 9})
    doc = C.decode_public_document(
        C.encode_public_document("agent-assessment", _payload(
            children=[child])))
    assert doc.data["children"][0]["score"] == {
        "satisfied": 1, "applicable": 11, "percent": 9}


def test_error_documents_carry_null_payload():
    problem = Problem(code="invalid-assessment",
                      message="assessment failed validation",
                      phase="validation")
    doc = C.decode_public_document(
        C.encode_public_document("agent-assessment", None, error=problem))
    assert doc.kind == "agent-assessment"
    assert doc.data is None
    assert doc.error.code == "invalid-assessment"
    assert doc.error.phase == "validation"
    assert doc.error.retryable is False
    for code, phase in (("consent-required", "consent"),
                        ("provider-unavailable", "provider"),
                        ("stale-evidence", "publication")):
        again = C.decode_public_document(C.encode_public_document(
            "agent-assessment", None,
            error=Problem(code=code, message="m", phase=phase)))
        assert again.data is None
        assert again.error.code == code
    with pytest.raises(ValueError):
        C.encode_public_document("agent-assessment", _payload(),
                                 error=problem)
    hostile = json.dumps({"schema_version": 1, "kind": "agent-assessment",
                          "ptest_version": "0.1.5", "domain": None,
                          "data": _payload(),
                          "error": {"code": "x", "message": "y",
                                    "phase": "z", "retryable": False},
                          }).encode()
    with pytest.raises(Problem, match="report-invalid"):
        C.decode_public_document(hostile)


@pytest.mark.parametrize("mutate", [
    lambda rows: rows[1:],
    lambda rows: rows + [dict(rows[0])],
    lambda rows: [dict(rows[0], id="NOPE-000")] + rows[1:],
    lambda rows: [rows[1], rows[0]] + rows[2:],
    lambda rows: [dict(rows[1], id="FIX-001")] + rows[1:],
])
def test_reject_missing_extra_unknown_duplicate_reordered_ids(mutate):
    payload = _payload(children=[_child(rows=mutate(_mixed_rows()))])
    with pytest.raises(Problem, match="report-invalid"):
        C.decode_public_document(_hostile(payload))
    with pytest.raises(Problem, match="report-invalid"):
        C.encode_public_document("agent-assessment", payload)


def test_reject_wrong_score_math():
    base = _mixed_rows()
    for bad_score in ({"satisfied": 7, "applicable": 10, "percent": 70},
                      {"satisfied": 8, "applicable": 9, "percent": 88},
                      {"satisfied": 8, "applicable": 10, "percent": 81},
                      {"satisfied": 11, "applicable": 11, "percent": 100},
                      None):
        payload = _payload(children=[_child(rows=base, score=bad_score)])
        with pytest.raises(Problem, match="report-invalid"):
            C.decode_public_document(_hostile(payload))
    na_rows = _na_child()["rows"]
    payload = _payload(children=[_child(
        rows=na_rows, findings=[],
        score={"satisfied": 0, "applicable": 0, "percent": 0})])
    with pytest.raises(Problem, match="report-invalid"):
        C.decode_public_document(_hostile(payload))


def test_reject_bad_citations():
    for bad_evidence in ([{**_citation(), "path": "/abs/path.py"}],
                         [{**_citation(), "path": "../escape.py"}],
                         [{**_citation(), "start_line": 9,
                            "end_line": 3}],
                         [{**_citation(), "start_line": 0}],
                         [{**_citation(), "sha256": "xyz"}],
                         ([_citation()] * 2),
                         ([_citation(path=f"src/f{i}.py",
                                     sha=f"{i:064x}") for i in range(17)]),
                         ([])):
        rows = _mixed_rows()
        rows[0] = _row("FIX-001", "satisfied", evidence=bad_evidence)
        payload = _payload(children=[_child(rows=rows)])
        with pytest.raises(Problem, match="report-invalid"):
            C.decode_public_document(_hostile(payload))


def test_reject_unjustified_not_applicable():
    rows = _mixed_rows()
    rows[3] = _row("DB-002", "not-applicable",
                   rationale="Affirmative but uncited.", evidence=[])
    payload = _payload(children=[_child(rows=rows)])
    with pytest.raises(Problem, match="report-invalid"):
        C.decode_public_document(_hostile(payload))


def test_reject_finding_shape_mismatch():
    base = _mixed_rows()
    cases = [
        [],
        [_finding("FIX-002"), _finding("FIX-002")],
        [_finding("FIX-001")],
        [_finding("FIX-002", recipe="cache")],
        [dict(_finding("FIX-002"), evidence=[])],
        [dict(_finding("FIX-002"), summary="")],
    ]
    for findings in cases:
        payload = _payload(children=[_child(rows=base,
                                            findings=findings)])
        with pytest.raises(Problem, match="report-invalid"):
            C.decode_public_document(_hostile(payload))


@pytest.mark.parametrize("field,value", [
    ("summary", "See [the fix](https://example.com) for details."),
    ("summary", "Contact <https://example.com> for help."),
    ("summary", "Use <b>bold</b> markup here."),
    ("summary", "First | second column layout."),
    ("summary", "Run `rm -rf /tmp/x` to clean."),
    ("summary", "# Finding headline here"),
    ("summary", "This scores 80% on quality."),
    ("summary", "Verified by observed pytest output exit code 0."),
    ("suggested_change", "Apply <script>alert(1)</script> now."),
    ("suggested_change", "Autolink <a@example.com> follows."),
])
def test_reject_untrusted_finding_content(field, value):
    finding = dict(_finding("FIX-002"), **{field: value})
    payload = _payload(children=[_child(findings=[finding])])
    with pytest.raises(Problem, match="report-invalid"):
        C.decode_public_document(_hostile(payload))


def test_reject_unsupported_publication():
    for bad_pub in ({"status": "published", "path": "recommendations.md",
                     "sha256": PUB_SHA},
                    {"status": "created", "path": "report.md",
                     "sha256": PUB_SHA},
                    {"status": "created", "path": "recommendations.md",
                     "sha256": "short"}):
        with pytest.raises(Problem, match="report-invalid"):
            C.decode_public_document(_hostile(_payload(
                publication=bad_pub)))


def test_reject_bounds_and_malformed_values():
    payload = _payload(children=[])
    with pytest.raises(Problem, match="report-invalid"):
        C.decode_public_document(_hostile(payload))
    payload = _payload(children=[_child() for _ in range(257)])
    with pytest.raises(Problem, match="report-invalid"):
        C.decode_public_document(_hostile(payload))
    rows = _mixed_rows()
    rows[0] = _row("FIX-001", evidence=[_citation()])
    rows[0]["rationale"] = "x" * 2049
    with pytest.raises(Problem, match="report-invalid"):
        C.decode_public_document(_hostile(_payload(
            children=[_child(rows=rows)])))
    payload = _payload()
    payload["provider"] = {"name": "gemini", "cli_version": "1",
                           "profile": "p"}
    with pytest.raises(Problem, match="report-invalid"):
        C.decode_public_document(_hostile(payload))
    payload = _payload()
    payload["provider"] = {"name": "claude", "cli_version": "",
                           "profile": "p"}
    with pytest.raises(Problem, match="report-invalid"):
        C.decode_public_document(_hostile(payload))
    payload = _payload(limitations=[
        {"code": "nope", "message": "m", "paths": []}])
    with pytest.raises(Problem, match="report-invalid"):
        C.decode_public_document(_hostile(payload))
    payload = _payload(limitations=[
        {"code": "partial-evidence", "message": "m", "paths": []}
        for _ in range(65)])
    with pytest.raises(Problem, match="report-invalid"):
        C.decode_public_document(_hostile(payload))
    rows = _mixed_rows()
    rows[0] = dict(rows[0], status="bogus")
    with pytest.raises(Problem, match="report-invalid"):
        C.decode_public_document(_hostile(_payload(
            children=[_child(rows=rows)])))


def test_projection_drops_additive_unknowns():
    sentinel = "smuggled-assessment-5b1"
    payload = _payload()
    payload["future_top"] = {"argv": [sentinel], "env": {"K": sentinel}}
    payload["provider"] = dict(payload["provider"],
                               argv=[sentinel], session_id=sentinel)
    payload["children"][0] = dict(payload["children"][0],
                                  raw_output=sentinel,
                                  score_override={"satisfied": 11,
                                                  "applicable": 11,
                                                  "percent": 100})
    payload["children"][0]["rows"][0] = dict(
        payload["children"][0]["rows"][0], future_row=sentinel)
    payload["children"][0]["findings"][0] = dict(
        payload["children"][0]["findings"][0], headline=sentinel)
    doc = C.decode_public_document(_hostile(payload))
    assert doc.error is None
    assert "future_top" not in doc.data
    assert "argv" not in doc.data["provider"]
    assert "session_id" not in doc.data["provider"]
    assert "raw_output" not in doc.data["children"][0]
    assert "score_override" not in doc.data["children"][0]
    assert "future_row" not in doc.data["children"][0]["rows"][0]
    assert "headline" not in doc.data["children"][0]["findings"][0]
    rendered = C.encode_public_document("agent-assessment", doc.data)
    assert sentinel.encode() not in rendered
    assert doc.data["children"][0]["score"] == {
        "satisfied": 8, "applicable": 10, "percent": 80}


def test_schema_descriptor_matches_generated_file():
    assert "agent-assessment" in C.PUBLIC_SCHEMAS
    root = Path(__file__).resolve().parents[2]
    path = root / "docs" / "schemas" / "v1" / "agent-assessment.json"
    shipped = json.loads(path.read_text(encoding="utf-8"))
    assert shipped == C.PUBLIC_SCHEMAS["agent-assessment"]
    data = shipped["properties"]["data"]
    assert set(data["required"]) == {
        "schema", "provider", "children", "limitations", "publication",
    }
    assert set(data["properties"]) == {
        "schema", "provider", "children", "limitations", "publication",
    }
    assert data["properties"]["schema"] == {
        "type": "string", "const": "ptest.agent-assessment/v1"}


def test_doctor_bytes_unchanged_by_registry_extension():
    payload = {
        "scope": ["tests"],
        "readiness": [{"area": "execution", "state": "unknown",
                       "reasons": []}],
        "findings": [],
        "limits": {"entries": 1, "files": 1, "file_bytes": 8,
                   "total_bytes": 8, "findings": 0, "output_bytes": 16,
                   "elapsed_s": 0.5, "depth": 2, "ast_nodes": 10},
        "usage": {"entries": 1, "files": 1, "file_bytes": 8,
                  "total_bytes": 8, "findings": 0, "output_bytes": 16,
                  "elapsed_s": 0.5, "skipped": 0, "truncated": False},
        "limitations": [],
    }
    raw = C.encode_public_document("doctor", payload)
    assert hashlib.sha256(raw).hexdigest() == DOCTOR_GOLDEN_SHA256
    problem = Problem(code="state-unavailable", message="scan failed",
                      phase="doctor")
    err = C.encode_public_document("doctor", None, error=problem)
    assert hashlib.sha256(err).hexdigest() == DOCTOR_ERROR_GOLDEN_SHA256
    doc = C.decode_public_document(raw)
    assert set(doc.data) == {
        "scope", "readiness", "findings", "limits", "usage",
        "limitations",
    }
