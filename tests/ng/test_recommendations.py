"""Focused TDD contract for report rendering/publication (task 4 owned).

Covers custom edits, symlink/race, interruption, stale source,
prior-report preservation, injection, footer, and sentinel guidance.
All tests run via ``.venv/bin/ptest tests/ng/test_recommendations.py``.
"""
from __future__ import annotations

import hashlib
import os
import stat
import types
from pathlib import Path

import pytest

CITATION_SHA = "ef" * 32
PACKET_SHA = "cd" * 32
PROJECT_ID = "ab" * 16

CHECKLIST_IDS = (
    "FIX-001", "FIX-002", "DB-001", "DB-002", "CACHE-001",
    "RESOURCE-001", "NETWORK-001", "PROCESS-001", "TIME-001",
    "SELECT-001", "TIMING-001",
)

FOOTER_LINES = (
    "> Ask your LLM to read this file, verify every cited claim against the current",
    "> source, implement only recommendations you approve, record the actual ptest",
    "> command/cwd/exit/output for each verification, and finish with `ptest --full`.",
)

FORBIDDEN_COMMANDS = (
    "rm ", "curl", "wget", "bash", "sh -c", "sudo", "chmod",
    "docker", "npm install", "pip install", "--workers", "--parallel",
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


def _finding(row_id, summary=None, change=None, recipe="__catalog__",
             evidence=None):
    recipes = {
        "FIX-001": "factories", "FIX-002": "factories",
        "DB-001": "databases", "DB-002": "databases",
        "CACHE-001": "cache", "RESOURCE-001": "files-ports",
        "NETWORK-001": "time-network", "PROCESS-001": "processes",
        "TIME-001": "time-network", "SELECT-001": None,
        "TIMING-001": None,
    }
    if recipe == "__catalog__":
        recipe = recipes[row_id]
    return {"id": row_id,
            "summary": summary or f"Close gap {row_id} with owned setup.",
            "suggested_change": change or f"Apply packaged recipe for {row_id}.",
            "recipe_id": recipe,
            "evidence": evidence if evidence is not None else [_citation()]}


def _mixed_rows():
    rows = []
    for row_id in CHECKLIST_IDS:
        if row_id == "FIX-002":
            rows.append(_row(row_id, "gap"))
        elif row_id == "DB-001":
            rows.append(_row(row_id, "unknown"))
        elif row_id == "DB-002":
            rows.append(_row(
                row_id, "not-applicable",
                rationale="Affirmative packet evidence shows this child "
                          "uses no database at all.",
                evidence=[_citation()]))
        else:
            rows.append(_row(row_id, "satisfied"))
    return rows


def _child(rows=None, findings=None, scope="child-a",
           packet_sha=PACKET_SHA, project_id=PROJECT_ID):
    rows = _mixed_rows() if rows is None else rows
    if findings is None:
        findings = [_finding("FIX-002")]
    return {"project_id": project_id, "scope": scope,
            "packet_sha256": packet_sha, "rows": rows,
            "score": {"satisfied": 0, "applicable": 1, "percent": 0},
            "findings": findings, "limitations": []}


def _run(children=None, provider=None, limitations=None):
    return types.SimpleNamespace(
        provider=provider or {"name": "claude", "cli_version": "1.2.3",
                              "profile": "default"},
        children=[_child()] if children is None else children,
        limitations=[] if limitations is None else limitations,
    )


def _identity_of(root: Path):
    from ptest.recommendations import PublishedIdentity
    target = root / "recommendations.md"
    raw = target.read_bytes()
    stamp = os.lstat(target)
    return PublishedIdentity(
        sha256=hashlib.sha256(raw).hexdigest(),
        st_dev=stamp.st_dev, st_ino=stamp.st_ino, size=len(raw))


# ---- render: evidence identity per item ----

def test_render_includes_file_line_sha_evidence_per_item():
    from ptest.recommendations import render_recommendations
    out = render_recommendations(_run()).decode("utf-8")
    assert "src/example.py" in out
    assert "3" in out and "9" in out
    assert CITATION_SHA in out
    assert "FIX-002" in out


def test_render_recomputes_score_and_ignores_model_score():
    from ptest.recommendations import render_recommendations
    out = render_recommendations(_run()).decode("utf-8")
    # 8 satisfied of 10 applicable -> floor 80; bogus model score ignored.
    assert "8/10 (80%), agent-reviewed" in out
    assert '"percent": 0' not in out


def test_render_all_na_child_has_no_score():
    from ptest.recommendations import render_recommendations
    rows = [_row(row_id, "not-applicable",
                 rationale="Affirmative packet evidence shows this "
                           "criterion cannot apply here.",
                 evidence=[_citation()]) for row_id in CHECKLIST_IDS]
    out = render_recommendations(_run(children=[_child(rows=rows,
                                                       findings=[])])).decode("utf-8")
    assert "no score" in out


def test_render_has_blank_observed_fields_and_unverified():
    from ptest.recommendations import render_recommendations
    out = render_recommendations(_run()).decode("utf-8")
    for line in ("Observed command: (blank)", "Observed cwd: (blank)",
                 "Observed exit status: (blank)", "Observed output: (blank)"):
        assert line in out
    assert "Status: unverified" in out


def test_render_uses_only_deterministic_ptest_commands():
    from ptest.recommendations import render_recommendations
    out = render_recommendations(_run()).decode("utf-8")
    assert "ptest child-a" in out
    assert "ptest --full" in out
    for forbidden in FORBIDDEN_COMMANDS:
        assert forbidden not in out


def test_render_final_gate_and_fixed_footer():
    from ptest.recommendations import render_recommendations
    raw = render_recommendations(_run())
    out = raw.decode("utf-8")
    assert "ptest --full" in out
    for line in FOOTER_LINES:
        assert line in out
    assert raw.endswith(b"\n")


def test_render_preserves_assertions_coverage_inventory_clause():
    from ptest.recommendations import render_recommendations
    out = render_recommendations(_run()).decode("utf-8").lower()
    assert "assertion" in out
    assert "coverage" in out
    assert "inventory" in out


def test_render_regression_must_fail_before_fix():
    from ptest.recommendations import render_recommendations
    out = render_recommendations(_run()).decode("utf-8").lower()
    assert "must fail before" in out


def test_render_unresolved_gap_stays_unverified_with_next_step():
    from ptest.recommendations import render_recommendations
    rows = _mixed_rows()
    # FIX-002 gap has no finding: unresolved.
    out = render_recommendations(
        _run(children=[_child(rows=rows, findings=[])])).decode("utf-8")
    assert "FIX-002" in out
    assert "unverified" in out.lower()
    assert "next step" in out.lower()


def test_render_sanitizes_markdown_injection():
    from ptest.recommendations import render_recommendations
    hostile = ("[click](http://evil.example) <script>alert(1)</script> "
               "a|b `rm -rf /` \u202eRTL\x00\x1b\n# Not a heading\n"
               "<https://evil.example/x>")
    finding = _finding("FIX-002", summary=hostile, change=hostile)
    out = render_recommendations(
        _run(children=[_child(findings=[finding])])).decode("utf-8")
    assert "http://evil.example" not in out
    assert "https://evil.example/x" not in out
    assert "<script" not in out
    assert "\u202e" not in out
    assert "\x00" not in out
    assert "\x1b" not in out
    assert "|b" not in out
    assert "\n# Not a heading" not in out
    assert "`rm -rf /`" not in out
    assert "alert(1)" in out  # inner text survives, tag does not


def test_render_sanitizes_provider_and_paths_in_tables():
    from ptest.recommendations import render_recommendations
    run = _run(provider={"name": "claude|evil", "cli_version": "1`2",
                         "profile": "d\nefault"})
    out = render_recommendations(run).decode("utf-8")
    assert "claude|evil" not in out
    assert "1`2" not in out


def test_render_sentinel_read_overwrite_delete_guidance():
    from ptest.recommendations import render_recommendations
    rows = [_row(row["id"], "gap") if row["id"] in ("DB-002", "CACHE-001")
            else row for row in _mixed_rows()]
    findings = [_finding("FIX-002"),
                _finding("DB-002", summary="Own the teardown.",
                         change="Scope teardown to owned names."),
                _finding("CACHE-001", summary="Namespace the cache.",
                         change="Prefix keys per run and worker.")]
    out = render_recommendations(
        _run(children=[_child(rows=rows, findings=findings)])).decode("utf-8")
    lowered = out.lower()
    assert "sentinel" in lowered
    assert "read" in lowered and "overwrite" in lowered and "delete" in lowered
    assert "neighbor" in lowered
    assert "worker" in lowered


def test_render_no_sentinel_demand_without_storage_gaps():
    from ptest.recommendations import render_recommendations
    out = render_recommendations(_run()).decode("utf-8")
    assert "no sentinel proof required" in out.lower()


def test_render_unsupported_parallel_says_unsupported():
    from ptest.recommendations import render_recommendations
    out = render_recommendations(_run()).decode("utf-8")
    section = out.lower()
    assert "unsupported" in section
    assert "-n " not in out
    assert "xdist" not in section


def test_render_accepts_mapping_and_document_forms():
    from ptest.recommendations import render_recommendations
    run = _run()
    as_dict = {"provider": run.provider, "children": run.children,
               "limitations": run.limitations}
    assert (render_recommendations(as_dict)
            == render_recommendations(run))
    doc = types.SimpleNamespace(data={"schema": "ptest.agent-assessment/v1",
                                      "provider": run.provider,
                                      "children": run.children,
                                      "limitations": run.limitations})
    assert b"FIX-002" in render_recommendations(doc)


def test_render_rejects_bad_input_fail_closed():
    from ptest.recommendations import render_recommendations
    from ptest.contracts import Problem
    with pytest.raises(TypeError):
        render_recommendations(None)
    with pytest.raises(Problem):
        render_recommendations(_run(children=[_child(scope="/abs")]))
    with pytest.raises(Problem):
        render_recommendations(_run(children=[_child(scope="../up")]))
    too_many = [_child(scope=f"c{i}") for i in range(257)]
    with pytest.raises(Problem):
        render_recommendations(_run(children=too_many))


def test_render_truncates_oversize_prose_visibly():
    from ptest.recommendations import render_recommendations
    finding = _finding("FIX-002", summary="s" * 3000)
    out = render_recommendations(
        _run(children=[_child(findings=[finding])])).decode("utf-8")
    assert "[truncated" in out


# ---- publish ----

def test_publish_creates_missing_target_with_valid_marker(tmp_path,
                                                           monkeypatch):
    from ptest.recommendations import render_recommendations
    from ptest.recommendations import publish_recommendations
    monkeypatch.setenv("PTEST_RECOMMENDATIONS_LOCK_DIR", str(tmp_path))
    root = tmp_path / "proj"
    root.mkdir()
    payload = render_recommendations(_run())
    result = publish_recommendations(root, payload, None)
    assert result.status == "created"
    assert result.path == "recommendations.md"
    raw = (root / "recommendations.md").read_bytes()
    first, rest = raw.split(b"\n", 1)
    assert first.startswith(b"<!-- ptest-recommendations v1 sha256=")
    assert hashlib.sha256(rest).hexdigest().encode() in first
    assert result.sha256 == hashlib.sha256(raw).hexdigest()


def test_publish_replaces_matching_managed_report(tmp_path, monkeypatch):
    from ptest.recommendations import render_recommendations
    from ptest.recommendations import publish_recommendations
    monkeypatch.setenv("PTEST_RECOMMENDATIONS_LOCK_DIR", str(tmp_path))
    root = tmp_path / "proj"
    root.mkdir()
    first = render_recommendations(_run())
    publish_recommendations(root, first, None)
    previous = _identity_of(root)
    other = _child(scope="child-b")
    second = render_recommendations(_run(children=[other]))
    assert second != first
    result = publish_recommendations(root, second, previous)
    assert result.status == "replaced"
    assert (root / "recommendations.md").read_bytes() == second


def test_publish_identical_bytes_is_unchanged(tmp_path, monkeypatch):
    from ptest.recommendations import render_recommendations
    from ptest.recommendations import publish_recommendations
    monkeypatch.setenv("PTEST_RECOMMENDATIONS_LOCK_DIR", str(tmp_path))
    root = tmp_path / "proj"
    root.mkdir()
    payload = render_recommendations(_run())
    publish_recommendations(root, payload, None)
    previous = _identity_of(root)
    result = publish_recommendations(root, payload, previous)
    assert result.status == "unchanged"
    assert (root / "recommendations.md").read_bytes() == payload


def test_publish_conflict_on_custom_report(tmp_path, monkeypatch):
    from ptest.recommendations import render_recommendations
    from ptest.recommendations import publish_recommendations
    from ptest.contracts import Problem
    monkeypatch.setenv("PTEST_RECOMMENDATIONS_LOCK_DIR", str(tmp_path))
    root = tmp_path / "proj"
    root.mkdir()
    (root / "recommendations.md").write_bytes(b"# my own notes\n")
    with pytest.raises(Problem, match="report-conflict"):
        publish_recommendations(root, render_recommendations(_run()), None)
    assert (root / "recommendations.md").read_bytes() == b"# my own notes\n"


def test_publish_conflict_on_edited_report(tmp_path, monkeypatch):
    from ptest.recommendations import render_recommendations
    from ptest.recommendations import publish_recommendations
    from ptest.contracts import Problem
    monkeypatch.setenv("PTEST_RECOMMENDATIONS_LOCK_DIR", str(tmp_path))
    root = tmp_path / "proj"
    root.mkdir()
    payload = render_recommendations(_run())
    publish_recommendations(root, payload, None)
    with (root / "recommendations.md").open("ab") as handle:
        handle.write(b"\n<!-- edited by hand -->\n")
    with pytest.raises(Problem, match="report-conflict"):
        publish_recommendations(root, payload, _identity_of(root))
    assert b"edited by hand" in (root / "recommendations.md").read_bytes()


def test_publish_conflict_on_symlink_and_nonregular(tmp_path, monkeypatch):
    from ptest.recommendations import render_recommendations
    from ptest.recommendations import publish_recommendations
    from ptest.contracts import Problem
    monkeypatch.setenv("PTEST_RECOMMENDATIONS_LOCK_DIR", str(tmp_path))
    payload = render_recommendations(_run())
    root = tmp_path / "proj"
    root.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_bytes(b"external\n")
    (root / "recommendations.md").symlink_to(outside)
    with pytest.raises(Problem, match="report-conflict"):
        publish_recommendations(root, payload, None)
    assert outside.read_bytes() == b"external\n"
    (root / "recommendations.md").unlink()
    os.mkfifo(root / "recommendations.md")
    with pytest.raises(Problem, match="report-conflict"):
        publish_recommendations(root, payload, None)


def test_publish_intervening_edit_returns_conflict_and_preserves(tmp_path,
                                                                 monkeypatch):
    from ptest.recommendations import publish_recommendations
    from ptest.recommendations import render_recommendations
    from ptest.contracts import Problem
    import ptest.recommendations as rec
    monkeypatch.setenv("PTEST_RECOMMENDATIONS_LOCK_DIR", str(tmp_path))
    root = tmp_path / "proj"
    root.mkdir()
    first = render_recommendations(_run())
    publish_recommendations(root, first, None)
    previous = _identity_of(root)
    second = render_recommendations(_run(children=[_child(scope="b")]))
    real_fsync = os.fsync
    fired = []

    def _rivalrous(fd):
        if not fired:
            fired.append(True)
            rival = render_recommendations(_run(children=[_child(scope="r")]))
            (root / "recommendations.md").write_bytes(rival)
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", _rivalrous)
    with pytest.raises(Problem, match="report-conflict"):
        publish_recommendations(root, second, previous)
    current = (root / "recommendations.md").read_bytes()
    assert b"## Scope r" in current
    assert current != second
    assert list(root.glob("*.tmp.*")) == []


def test_publish_stale_previous_identity_fails_closed(tmp_path, monkeypatch):
    from ptest.recommendations import publish_recommendations
    from ptest.recommendations import render_recommendations
    from ptest.contracts import Problem
    monkeypatch.setenv("PTEST_RECOMMENDATIONS_LOCK_DIR", str(tmp_path))
    root = tmp_path / "proj"
    root.mkdir()
    first = render_recommendations(_run())
    publish_recommendations(root, first, None)
    stale = _identity_of(root)
    rival = render_recommendations(_run(children=[_child(scope="r")]))
    (root / "recommendations.md").write_bytes(rival)
    second = render_recommendations(_run(children=[_child(scope="b")]))
    with pytest.raises(Problem, match="stale-evidence"):
        publish_recommendations(root, second, stale)
    assert (root / "recommendations.md").read_bytes() == rival


def test_publish_cancel_preserves_old_report_and_cleans_temp(tmp_path,
                                                             monkeypatch):
    from ptest.recommendations import publish_recommendations
    from ptest.recommendations import render_recommendations
    monkeypatch.setenv("PTEST_RECOMMENDATIONS_LOCK_DIR", str(tmp_path))
    root = tmp_path / "proj"
    root.mkdir()
    first = render_recommendations(_run())
    publish_recommendations(root, first, None)
    previous = _identity_of(root)
    second = render_recommendations(_run(children=[_child(scope="b")]))

    def _boom(*args, **kwargs):
        raise KeyboardInterrupt()

    monkeypatch.setattr(os, "rename", _boom)
    with pytest.raises(KeyboardInterrupt):
        publish_recommendations(root, second, previous)
    assert (root / "recommendations.md").read_bytes() == first
    assert list(root.glob("*.tmp.*")) == []


def test_publish_rejects_payload_with_bad_marker(tmp_path, monkeypatch):
    from ptest.recommendations import publish_recommendations
    from ptest.contracts import Problem
    monkeypatch.setenv("PTEST_RECOMMENDATIONS_LOCK_DIR", str(tmp_path))
    root = tmp_path / "proj"
    root.mkdir()
    with pytest.raises(Problem):
        publish_recommendations(root, b"no marker here\n", None)
    with pytest.raises(TypeError):
        publish_recommendations(root, "not-bytes", None)  # type: ignore[arg-type]
    assert not (root / "recommendations.md").exists()


def test_publish_sequential_writers_do_not_clobber(tmp_path, monkeypatch):
    from ptest.recommendations import publish_recommendations
    from ptest.recommendations import render_recommendations
    monkeypatch.setenv("PTEST_RECOMMENDATIONS_LOCK_DIR", str(tmp_path))
    root = tmp_path / "proj"
    root.mkdir()
    first = render_recommendations(_run())
    assert publish_recommendations(root, first, None).status == "created"
    prev = _identity_of(root)
    assert publish_recommendations(root, first, prev).status == "unchanged"
    lock_files = list(tmp_path.glob("rec-*.lock"))
    assert len(lock_files) == 1
