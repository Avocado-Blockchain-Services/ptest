"""Evidence admission, strict validation, and scoring (task 2 owned).

TDD contract for ``ptest.agent_assessment``: bounded packet collection with
explicit coverage counts and SHA-256 line identities, single-packet strict
validation reusing the frozen ``PublicDocument`` contract, and floor scoring.

Abuse twins (secure-by-spec): symlinked/private/secret-bearing/instruction/
generated/dependency content is never admitted; hostile packet text cannot
become citations; stale packets, duplicate/reordered IDs, unjustified N/A,
and model-supplied commands/scores are rejected, never projected away.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from ptest import contracts as C
from ptest import doctor

CHILD_PID = "ab" * 16
EXPECTED_IDS = (
    "FIX-001", "FIX-002", "DB-001", "DB-002", "CACHE-001",
    "RESOURCE-001", "NETWORK-001", "PROCESS-001", "TIME-001",
    "SELECT-001", "TIMING-001",
)


def _config(pid=CHILD_PID):
    return C.Config(
        runner=C.RunnerConfig(
            kind=C.RunnerKind.PYTEST, launcher=("uv",),
            test_roots=("tests",),
        ),
        setup=None,
        resources=C.ResourceConfig(),
        selection=C.SelectionPolicy(enabled=False, closed_inputs=False),
        project_id=pid,
    )


def _resolution(root: Path, config=None):
    return C.ConfigResolution(
        root=root, path=None,
        config=config if config is not None else _config(),
        monorepo=None, provenance=(), warnings=(), problem=None,
    )


def _domain(root: Path):
    return C.DomainPaths(
        root=root, machine_config=root / ".ptest" / "config.toml",
        ledger=root / ".ptest" / "ledger",
        marker=root / ".ptest" / "marker",
        fixture=True, domain_id=None,
    )


def _workspace(root: Path, config=None):
    resolution = _resolution(root, config)
    return doctor.inspect_workspace(
        _domain(root), resolution, C.DEFAULT_SCAN_LIMITS, None), resolution


def _packet_for(root: Path, files: dict[str, str] | None = None):
    """Build one standalone packet over a tmp project."""
    from ptest import agent_assessment as AA

    for rel, text in (files or {}).items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    workspace, resolution = _workspace(root)
    packets = AA.build_packets(workspace, resolution, AA.EvidenceLimits())
    assert len(packets) == 1
    return packets[0]


def _citation_for(packet, start=1, end=None):
    excerpt = packet.excerpts[0]
    return {"path": excerpt.path, "start_line": start,
            "end_line": end if end is not None else excerpt.end_line,
            "sha256": excerpt.sha256}


def _row(packet, row_id, status="satisfied", rationale=None, evidence=None):
    if rationale is None:
        rationale = (
            f"Row {row_id} judged {status} against packet excerpt "
            f"{packet.excerpts[0].path} lines 1-{packet.excerpts[0].end_line}."
        )
    if evidence is None:
        evidence = [] if status == "unknown" else [_citation_for(packet)]
    return {"id": row_id, "status": status, "rationale": rationale,
            "evidence": evidence}


def _finding(packet, row_id, recipe="__catalog__"):
    from ptest import agent_assessment as AA

    if recipe == "__catalog__":
        recipe = C.AGENT_ASSESSMENT_RECIPES[row_id]
    return {"id": row_id,
            "summary": f"Close gap {row_id} with owned setup.",
            "suggested_change": f"Apply packaged recipe for {row_id}.",
            "recipe_id": recipe, "evidence": [_citation_for(packet)]}


def _score(satisfied, applicable):
    return {"satisfied": satisfied, "applicable": applicable,
            "percent": (100 * satisfied) // applicable}


def _payload_for(packet, rows=None, findings="auto", score="omit"):
    """Raw model-response payload. The model schema omits ``score``: ptest
    computes it after validation, so ``score="omit"`` (the default) builds a
    child with no ``score`` key. ``score="compute"`` includes a correct score
    dict (a model-supplied score, which the raw boundary must reject).
    Any other ``score`` value is included verbatim (including None)."""
    if rows is None:
        rows = [_row(packet, row_id) for row_id in EXPECTED_IDS]
    if findings == "auto":
        findings = [_finding(packet, row["id"]) for row in rows
                    if row["status"] == "gap"]
    child = {"project_id": packet.project_id,
             "scope": packet.scope,
             "packet_sha256": packet.packet_sha256,
             "rows": rows, "findings": findings, "limitations": []}
    if score == "compute":
        na_count = sum(1 for row in rows
                       if row["status"] == "not-applicable")
        satisfied = sum(1 for row in rows if row["status"] == "satisfied")
        child["score"] = (None if na_count == len(rows) else _score(
            satisfied, len(rows) - na_count))
    elif score != "omit":
        child["score"] = score
    return {"schema": "ptest.agent-assessment/v1",
            "provider": {"name": "claude", "cli_version": "1.2.3",
                         "profile": "stable"},
            "children": [child],
            "limitations": [],
            "publication": {"status": "created",
                            "path": "recommendations.md",
                            "sha256": "12" * 32}}


def _envelope_bytes(payload: dict) -> bytes:
    return (json.dumps({"schema_version": 1, "kind": "agent-assessment",
                        "ptest_version": "0.1.5", "domain": None,
                        "data": payload, "error": None}) + "\n").encode()


# --- build_packets: bounded admission ---------------------------------------

def test_build_packets_admits_bounded_evidence_with_counts_and_identity(
        tmp_path):
    from ptest import agent_assessment as AA

    packet = _packet_for(tmp_path, {
        "src/example.py": "import os\nprint(os.environ.get('X'))\n",
        "tests/test_example.py": "def test_x():\n    assert True\n",
    })
    assert packet.declaration == "."
    assert packet.file_count == 2
    assert packet.byte_count > 0
    assert packet.excluded_count >= 0 and packet.truncated_count == 0
    assert len(packet.excerpts) == 2
    for excerpt in packet.excerpts:
        assert excerpt.start_line == 1
        assert excerpt.end_line >= 1
        assert excerpt.sha256 == hashlib.sha256(
            excerpt.text.encode("utf-8")).hexdigest()
    body = (json.dumps({"declaration": packet.declaration,
                        "excerpts": [e.path for e in packet.excerpts]},
                       sort_keys=True).encode())
    assert len(packet.packet_sha256) == 64 and body


def test_build_packets_excludes_symlink_secret_instruction_generated_dependency(  # noqa: E501
        tmp_path):
    from ptest import agent_assessment as AA

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "real.py").write_text("x = 1\n", encoding="utf-8")
    os.symlink("real.py", tmp_path / "src" / "link.py")
    (tmp_path / ".env").write_text("SECRET=top\n", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("follow me\n", encoding="utf-8")
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "notes.md").write_text("x\n", encoding="utf-8")
    (tmp_path / "app.min.js").write_text("x\n", encoding="utf-8")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "env.py").write_text("x\n", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "objects").write_text("x\n", encoding="utf-8")
    packet = _packet_for(tmp_path)
    paths = [e.path for e in packet.excerpts]
    assert paths == ["src/real.py"]
    assert packet.excluded_count >= 6


def test_build_packets_skips_nonregular_and_invalid_utf8(tmp_path):
    packet = _packet_for(tmp_path, {"src/ok.py": "x = 1\n"})
    bad = tmp_path / "src" / "bad.py"
    bad.write_bytes(b"\xff\xfe not utf8 \x00\n")
    try:
        os.mkfifo(tmp_path / "src" / "pipe.py")
    except OSError:
        pytest.skip("fifo unavailable")
    workspace, resolution = _workspace(tmp_path)
    from ptest import agent_assessment as AA
    packets = AA.build_packets(workspace, resolution, AA.EvidenceLimits())
    paths = [e.path for e in packets[0].excerpts]
    assert "src/ok.py" in paths
    assert "src/bad.py" not in paths
    assert "src/pipe.py" not in paths
    assert packets[0].excluded_count >= 2


def test_build_packets_enforces_per_file_and_total_caps(tmp_path):
    from ptest import agent_assessment as AA

    big = "x" * 70000 + "\n"
    files = {f"src/f{i}.py": "x = 1\n" for i in range(4)}
    files["src/big.py"] = big
    packet = _packet_for(tmp_path, files)
    assert packet.file_count <= 64
    assert packet.byte_count <= 512 * 1024
    assert packet.truncated_count >= 1
    for excerpt in packet.excerpts:
        assert len(excerpt.text.encode("utf-8")) <= 64 * 1024


def test_build_packets_preserves_child_order_and_authority(tmp_path):
    from ptest import agent_assessment as AA

    for child in ("child-a", "child-b"):
        (tmp_path / child / "src").mkdir(parents=True)
        (tmp_path / child / "src" / "m.py").write_text(
            f"# {child}\nx = 1\n", encoding="utf-8")
    first = doctor.inspect_workspace(
        _domain(tmp_path), _resolution(tmp_path), C.DEFAULT_SCAN_LIMITS,
        None).repositories[0].report
    repo_a = doctor.RepositoryInspection(
        declaration="child-a", local_scope=None, report=first,
        config_problem=None)
    repo_b = doctor.RepositoryInspection(
        declaration="child-b", local_scope=None, report=first,
        config_problem=None)
    workspace = doctor.WorkspaceInspection(
        scope=(), repositories=(repo_a, repo_b), aggregate=first)
    packets = AA.build_packets(
        workspace, _resolution(tmp_path), AA.EvidenceLimits())
    assert [p.declaration for p in packets] == ["child-a", "child-b"]
    assert packets[0].packet_sha256 != packets[1].packet_sha256
    assert all(p.scope == p.declaration for p in packets)


def test_build_packets_dependency_provenance_is_static_and_unknown_where_unprovable(  # noqa: E501
        tmp_path):
    packet = _packet_for(tmp_path, {
        "pyproject.toml": "[project]\nname = 'demo'\n",
        "uv.lock": "version = 1\n",
        "src/m.py": "x = 1\n",
    })
    kinds = {(d.ecosystem, d.status) for d in packet.dependencies}
    assert ("python", "declared") in kinds
    assert ("python-lock", "locked") in kinds
    env = [d for d in packet.dependencies
           if d.ecosystem == "environment"]
    assert env and env[0].status == "uninspectable"
    assert all(".venv" not in e.path for e in packet.excerpts)
    import pathlib
    source = pathlib.Path("src/ptest/agent_assessment.py").read_text(
        encoding="utf-8")
    assert "import_module" not in source
    assert "sys.path" not in source


# --- parse_assessment: strict single-packet validation -----------------------

def test_parse_assessment_accepts_valid_single_child(tmp_path):
    from ptest import agent_assessment as AA

    packet = _packet_for(tmp_path, {"src/m.py": "x = 1\n"})
    rows = [_row(packet, row_id) for row_id in EXPECTED_IDS]
    raw = _envelope_bytes(_payload_for(packet, rows=rows))
    child = AA.parse_assessment(raw, packet)
    assert child.packet_sha256 == packet.packet_sha256
    assert [r.id for r in child.rows] == list(EXPECTED_IDS)
    assert child.score is not None
    assert (child.score.satisfied, child.score.applicable,
            child.score.percent) == (11, 11, 100)


def test_parse_assessment_rejects_stale_packet_identity(tmp_path):
    from ptest import agent_assessment as AA

    packet = _packet_for(tmp_path, {"src/m.py": "x = 1\n"})
    payload = _payload_for(packet)
    payload["children"][0]["packet_sha256"] = "00" * 32
    with pytest.raises(C.Problem) as exc:
        AA.parse_assessment(_envelope_bytes(payload), packet)
    assert exc.value.code == "stale-evidence"


def test_parse_assessment_rejects_citation_outside_packet(tmp_path):
    from ptest import agent_assessment as AA

    packet = _packet_for(tmp_path, {"src/m.py": "x = 1\n"})
    good = _citation_for(packet)
    for bad in (dict(good, path="../escape.py"),
                dict(good, path="src/other.py"),
                dict(good, sha256="00" * 32),
                dict(good, start_line=good["end_line"] + 1,
                     end_line=good["end_line"] + 5)):
        rows = [_row(packet, row_id) for row_id in EXPECTED_IDS]
        rows[0] = _row(packet, "FIX-001", evidence=[bad])
        with pytest.raises(C.Problem):
            AA.parse_assessment(
                _envelope_bytes(_payload_for(packet, rows=rows)), packet)


def test_parse_assessment_rejects_duplicate_reordered_missing_ids(tmp_path):
    from ptest import agent_assessment as AA

    packet = _packet_for(tmp_path, {"src/m.py": "x = 1\n"})
    base = [_row(packet, row_id) for row_id in EXPECTED_IDS]
    variants = [
        base[1:],
        base + [dict(base[0])],
        [dict(base[0], id="NOPE-000")] + base[1:],
        [base[1], base[0]] + base[2:],
        [base[0], dict(base[1], id="FIX-001")] + base[2:],
    ]
    for rows in variants:
        with pytest.raises(C.Problem):
            AA.parse_assessment(
                _envelope_bytes(_payload_for(
                    packet, rows=rows, findings=[],
                    score="omit")),
                packet)


def test_parse_assessment_rejects_unjustified_not_applicable(tmp_path):
    from ptest import agent_assessment as AA

    packet = _packet_for(tmp_path, {"src/m.py": "x = 1\n"})
    rows = [_row(packet, row_id) for row_id in EXPECTED_IDS]
    rows[3] = _row(packet, "DB-002", "not-applicable",
                   rationale="No evidence found anywhere.",
                   evidence=[_citation_for(packet)])
    with pytest.raises(C.Problem):
        AA.parse_assessment(
            _envelope_bytes(_payload_for(packet, rows=rows)), packet)


def test_parse_assessment_accepts_affirmative_not_applicable(tmp_path):
    from ptest import agent_assessment as AA

    packet = _packet_for(tmp_path, {"src/m.py": "x = 1\n"})
    excerpt = packet.excerpts[0]
    rows = [_row(packet, row_id) for row_id in EXPECTED_IDS]
    rows[3] = _row(
        packet, "DB-002", "not-applicable",
        rationale=(f"Affirmative: {excerpt.path} holds only x = 1, a bare "
                   "constant, so no database ownership rule applies."),
        evidence=[_citation_for(packet)])
    raw = _envelope_bytes(_payload_for(packet, rows=rows))
    child = AA.parse_assessment(raw, packet)
    assert child.rows[3].status == "not-applicable"
    assert child.score is not None
    assert (child.score.satisfied, child.score.applicable) == (10, 10)


def test_parse_assessment_rejects_model_supplied_score_and_command(tmp_path):
    from ptest import agent_assessment as AA

    packet = _packet_for(tmp_path, {"src/m.py": "x = 1\n"})
    payload = _payload_for(packet)
    payload["children"][0]["score_override"] = {"satisfied": 11,
                                                "applicable": 11,
                                                "percent": 100}
    with pytest.raises(C.Problem):
        AA.parse_assessment(_envelope_bytes(payload), packet)
    payload = _payload_for(packet)
    payload["children"][0]["observed_command"] = ["pytest", "-q"]
    with pytest.raises(C.Problem):
        AA.parse_assessment(_envelope_bytes(payload), packet)
    payload = _payload_for(packet)
    payload["children"][0]["headline"] = "ptest will work great"
    with pytest.raises(C.Problem):
        AA.parse_assessment(_envelope_bytes(payload), packet)


def test_parse_assessment_rejects_hostile_prose_and_stale_bytes(tmp_path):
    from ptest import agent_assessment as AA

    packet = _packet_for(tmp_path, {"src/m.py": "x = 1\n"})
    rows = [_row(packet, row_id) for row_id in EXPECTED_IDS]
    rows[0] = _row(packet, "FIX-001",
                   rationale="See [the fix](https://example.com) for it.")
    with pytest.raises(C.Problem):
        AA.parse_assessment(
            _envelope_bytes(_payload_for(packet, rows=rows)), packet)
    with pytest.raises(C.Problem):
        AA.parse_assessment(b"\xff\xfe invalid utf8", packet)


# --- score: floor math, unknown in denominator, null on zero applicable ------

def test_score_floor_unknown_in_denominator_and_null(tmp_path):
    from ptest import agent_assessment as AA

    packet = _packet_for(tmp_path, {"src/m.py": "x = 1\n"})
    excerpt = packet.excerpts[0]

    def row(row_id, status):
        rationale = (f"Row {row_id} judged {status} against "
                     f"{excerpt.path} excerpt.")
        evidence = [] if status == "unknown" else [_citation_for(packet)]
        if status == "not-applicable":
            rationale = (f"Affirmative: {excerpt.path} shows no artifact "
                         f"for {row_id}, confirmed by manifest.")
        return AA.AssessmentRow(id=row_id, status=status,
                                rationale=rationale,
                                evidence=tuple(
                                    AA.Citation(**c) for c in evidence))

    rows = ((row("FIX-001", "satisfied"),)
            + tuple(row(i, "unknown") for i in ("FIX-002", "DB-001"))
            + tuple(row(i, "gap") for i in EXPECTED_IDS[3:]))
    result = AA.score(rows)
    assert (result.satisfied, result.applicable, result.percent) == (1, 11, 9)
    na_rows = tuple(row(i, "not-applicable") for i in EXPECTED_IDS)
    assert AA.score(na_rows) is None
    assert AA.score(()) is None


# --- trust-boundary repair: exact keys, grounded N/A, no model score --------

def _with_extra(payload: dict, where: str) -> dict:
    """Return a copy of ``payload`` with one unknown field at ``where``."""
    import copy

    payload = copy.deepcopy(payload)
    child = payload["children"][0]
    if where == "assessment":
        payload["trace_id"] = "smuggled"
    elif where == "provider":
        payload["provider"]["region"] = "smuggled"
    elif where == "child":
        child["priority"] = "smuggled"
    elif where == "row":
        child["rows"][0]["comment"] = "smuggled"
    elif where == "citation":
        child["rows"][0]["evidence"][0]["confidence"] = 0.99
    elif where == "finding":
        child["rows"][2]["status"] = "gap"
        payload["children"][0]["findings"] = [_finding_for_gap(payload)]
        child["findings"][0]["owner"] = "smuggled"
    elif where == "limitation":
        child["limitations"] = [{"code": "execution-not-run",
                                 "message": "Review only.",
                                 "paths": [],
                                 "extra": "smuggled"}]
    elif where == "publication":
        payload["publication"]["uri"] = "smuggled"
    else:
        raise AssertionError(f"unknown injection site {where!r}")
    return payload


def _finding_for_gap(payload: dict) -> dict:
    row = payload["children"][0]["rows"][2]
    return {"id": row["id"],
            "summary": f"Close gap {row['id']} with owned setup.",
            "suggested_change": f"Apply packaged recipe for {row['id']}.",
            "recipe_id": C.AGENT_ASSESSMENT_RECIPES[row["id"]],
            "evidence": [dict(row["evidence"][0])]}


def test_parse_assessment_rejects_nested_extra_properties(tmp_path):
    """Unknown fields anywhere in the raw response are rejected, never
    projected away (exact allowlist on the raw boundary).

    ``score="compute"`` isolates the extra-keys control: pre-repair the
    payload is otherwise fully valid, so acceptance proves silent
    projection. Scoreless variants are regression cover for the repaired
    boundary (pre-repair they fail on the missing score instead).
    """
    from ptest import agent_assessment as AA

    packet = _packet_for(tmp_path, {"src/m.py": "x = 1\n"})
    sites = ("assessment", "provider", "child", "row", "citation",
             "limitation", "publication")
    for site in sites:
        with pytest.raises(C.Problem):
            AA.parse_assessment(
                _envelope_bytes(_with_extra(
                    _payload_for(packet, score="compute"), site)),
                packet)
        with pytest.raises(C.Problem):
            AA.parse_assessment(
                _envelope_bytes(_with_extra(
                    _payload_for(packet, score="omit"), site)),
                packet)
    rows = [_row(packet, row_id) for row_id in EXPECTED_IDS]
    rows[2] = _row(packet, "DB-001", "gap")
    findings = [_finding(packet, "DB-001")]
    with pytest.raises(C.Problem):
        AA.parse_assessment(
            _envelope_bytes(_with_extra(
                _payload_for(packet, rows=rows, findings=findings,
                             score="compute"),
                "finding")),
            packet)


def _filename_only_na_rows(packet):
    rows = [_row(packet, row_id) for row_id in EXPECTED_IDS]
    rows[3] = _row(packet, "DB-002", "not-applicable",
                   rationale=("Affirmative: src/db.py shows "
                               "no database applies here."),
                   evidence=[_citation_for(packet)])
    return rows


def test_parse_assessment_rejects_filename_only_not_applicable(tmp_path):
    """A filename mention without cited-line evidence never justifies N/A.

    ``score="compute"`` isolates the N/A control: pre-repair the payload is
    otherwise fully valid, so acceptance proves the basename-only hole. The
    scoreless variant is regression cover for the repaired boundary
    (pre-repair it fails on the missing score instead).
    """
    from ptest import agent_assessment as AA

    packet = _packet_for(tmp_path, {
        "src/db.py": ("DATABASE_URL = 'postgresql://localhost/demo'\n"
                      "import psycopg2\n"),
    })
    for score_mode in ("compute", "omit"):
        rows = _filename_only_na_rows(packet)
        with pytest.raises(C.Problem):
            AA.parse_assessment(
                _envelope_bytes(_payload_for(packet, rows=rows,
                                             score=score_mode)),
                packet)


def test_parse_assessment_rejects_model_supplied_score(tmp_path):
    """Any ``score`` key in the raw child is a model-supplied field."""
    from ptest import agent_assessment as AA

    packet = _packet_for(tmp_path, {"src/m.py": "x = 1\n"})
    with pytest.raises(C.Problem):
        AA.parse_assessment(
            _envelope_bytes(_payload_for(packet, score="compute")),
            packet)
    payload = _payload_for(packet, score="omit")
    payload["children"][0]["score"] = None
    with pytest.raises(C.Problem):
        AA.parse_assessment(_envelope_bytes(payload), packet)


def test_parse_assessment_accepts_scoreless_payload_with_computed_score(
        tmp_path):
    """The model schema omits ``score``; ptest computes it after validation."""
    from ptest import agent_assessment as AA

    packet = _packet_for(tmp_path, {"src/m.py": "x = 1\n"})
    payload = _payload_for(packet, score="omit")
    assert "score" not in payload["children"][0]
    child = AA.parse_assessment(_envelope_bytes(payload), packet)
    assert child.score is not None
    assert (child.score.satisfied, child.score.applicable,
            child.score.percent) == (11, 11, 100)
