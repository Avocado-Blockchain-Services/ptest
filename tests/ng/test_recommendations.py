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


def _row(row_id, status="satisfied", rationale=None, evidence=None,
         label=None):
    if rationale is None:
        rationale = f"Row {row_id} judged {status} against packet excerpt."
    if evidence is None:
        evidence = [] if status == "unknown" else [_citation()]
    row = {"id": row_id, "status": status, "rationale": rationale,
           "evidence": evidence}
    if label is not None:
        row["label"] = label
    return row


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


LABELS = {
    "FIX-001": "Test data factories",
    "FIX-002": "Fixture state isolation",
    "DB-001": "Database setup reuse",
    "DB-002": "Database isolation",
    "CACHE-001": "Cache isolation",
    "RESOURCE-001": "Files and ports",
    "NETWORK-001": "Network isolation",
    "PROCESS-001": "Child processes",
    "TIME-001": "Deterministic time",
    "SELECT-001": "Test selection",
    "TIMING-001": "Test timing",
}


def _child(rows=None, findings=None, scope="child-a",
           packet_sha=PACKET_SHA, project_id=PROJECT_ID, execution=None,
           facts=None):
    rows = _mixed_rows() if rows is None else rows
    if findings is None:
        findings = [_finding("FIX-002")]
    child = {"project_id": project_id, "scope": scope,
             "packet_sha256": packet_sha, "rows": rows,
             "score": {"satisfied": 0, "applicable": 1, "percent": 0},
             "findings": findings, "limitations": []}
    if execution is not None:
        child["execution"] = execution
    if facts is not None:
        child["facts"] = facts
    return child


def _api_facts(**overrides):
    facts = {
        "project": "api", "runner": "pytest", "runs": True,
        "runs_reason": None, "runs_fix": None,
        "parallel": "4 workers (xdist, --dist loadgroup)",
        "parallel_short": "4 workers", "parallel_fix": None,
        "setup": "uv sync --locked",
        "full_suite": 'your pytest config: -m "not slow"',
        "full_blocked": None,
    }
    facts.update(overrides)
    return facts


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
    # 8 satisfied of 10 applicable; bogus model score ignored.
    assert "8 of 10 checks confirmed from evidence" in out
    assert '"percent": 0' not in out


def test_render_foregrounds_deterministic_initialization_limitation_once():
    import ptest.recommendations as recommendations

    limitation = {
        "code": "capability-unsupported",
        "message": (
            "initialization-required: standalone ptest configuration is "
            "missing; ptest is not execution-ready until you run `ptest init`."),
        "paths": [],
    }
    run = _run(limitations=[limitation])
    run.children[0]["limitations"] = [limitation]

    payload = recommendations.render_recommendations(run)
    marker_hash, body = recommendations._split_marker(payload)
    rendered = body.decode("utf-8")
    blocker = "- capability-unsupported: initialization-required:"

    assert hashlib.sha256(body).hexdigest() == marker_hash
    assert rendered.index(blocker) < rendered.index(
        "8 of 10 checks confirmed from evidence")
    assert rendered.count(blocker) == 1


def test_render_keeps_multiscope_limitation_paths_distinct_without_repeating_duplicates():
    import ptest.recommendations as recommendations

    api_limitation = {
        "code": "partial-evidence",
        "message": "Bounded evidence was omitted.",
        "paths": ["api/tests/test_api.py"],
    }
    web_limitation = {
        "code": "partial-evidence",
        "message": "Bounded evidence was omitted.",
        "paths": ["web/tests/test_web.py"],
    }
    api = _child(scope="api", project_id="aa" * 16)
    web = _child(scope="web", project_id="bb" * 16)
    api["limitations"] = [api_limitation, api_limitation]
    web["limitations"] = [web_limitation]
    run = _run(
        children=[api, web],
        limitations=[api_limitation, api_limitation, web_limitation],
    )

    payload = recommendations.render_recommendations(run)
    marker_hash, body = recommendations._split_marker(payload)
    rendered = body.decode("utf-8")

    assert hashlib.sha256(body).hexdigest() == marker_hash
    assert "`api/tests/test_api.py`" in rendered
    assert "`web/tests/test_web.py`" in rendered
    assert rendered.count("Bounded evidence was omitted.") == 1
    assert "## Scope api" in rendered and "## Scope web" in rendered


def test_render_all_na_child_has_no_score():
    from ptest.recommendations import render_recommendations
    rows = [_row(row_id, "not-applicable",
                 rationale="Affirmative packet evidence shows this "
                           "criterion cannot apply here.",
                 evidence=[_citation()]) for row_id in CHECKLIST_IDS]
    out = render_recommendations(_run(children=[_child(rows=rows,
                                                       findings=[])])).decode("utf-8")
    assert "no score" in out


def test_render_item_headings_show_id_and_label():
    from ptest.recommendations import render_recommendations
    rows = [_row(row_id, "satisfied", label=LABELS[row_id])
            for row_id in CHECKLIST_IDS]
    out = render_recommendations(
        _run(children=[_child(rows=rows, findings=[])])).decode("utf-8")
    for row_id in CHECKLIST_IDS:
        assert f"## {row_id} {LABELS[row_id]}" in out
    assert "## Finding" not in out


def test_render_gap_heading_carries_id_and_label():
    from ptest.recommendations import render_recommendations
    rows = [_row("FIX-002", "gap", label=LABELS["FIX-002"])]
    rows += [_row(row_id, "satisfied", label=LABELS[row_id])
             for row_id in CHECKLIST_IDS if row_id != "FIX-002"]
    out = render_recommendations(_run(children=[_child(rows=rows)])).decode(
        "utf-8")
    assert "## FIX-002 Fixture state isolation" in out


def test_render_project_section_starts_with_plain_language_facts():
    from ptest.recommendations import render_recommendations
    out = render_recommendations(
        _run(children=[_child(facts=_api_facts())])).decode("utf-8")
    scope_at = out.index("## Scope child-a")
    runs_at = out.index("- runs: yes")
    packet_at = out.index("Packet:")
    assert scope_at < runs_at < packet_at
    assert "- parallel: 4 workers (xdist, --dist loadgroup)" in out
    assert "- setup: uv sync --locked (ptest runs it when needed)" in out
    assert '- full suite = your pytest config: -m "not slow"' in out
    assert "Execution:" not in out

    blocked = _child(
        execution={
            "status": "not-executable",
            "detail": "no tests found",
            "fix": "add a test file",
        },
        facts={"project": "api", "runner": "pytest", "runs": "yes",
               "bogus": True},
    )
    blocked_out = render_recommendations(
        _run(children=[blocked])).decode("utf-8")
    assert "- runs: no — no tests found → add a test file" in blocked_out

    fallback = _child(execution={
        "status": "not-executable",
        "detail": "no tests found",
        "fix": "add a test file",
    })
    fallback_out = render_recommendations(
        _run(children=[fallback])).decode("utf-8")
    assert "- runs: no — no tests found → add a test file" in fallback_out

    unrecorded = render_recommendations(_run()).decode("utf-8")
    assert "- runs: not recorded" in unrecorded


def test_render_skip_and_failed_rows_use_report_wording():
    from ptest.recommendations import render_recommendations
    rows = [_mixed_rows()[0]]
    rows.append(_row("DB-001", "unknown",
                     rationale="Review failed: timed out", evidence=[]))
    rows.append(_row(
        "DB-002", "not-applicable",
        rationale=("Skipped without a model call: no database library in "
                   "pyproject.toml and no database configuration or usage "
                   "in the admitted evidence."),
        evidence=[_citation()], label=LABELS["DB-002"]))
    rows.extend(row for row in _mixed_rows() if row["id"] not in
                ("FIX-001", "DB-001", "DB-002"))
    out = render_recommendations(
        _run(children=[_child(rows=rows)])).decode("utf-8")
    assert "Status: unknown.\n\nReason: review failed: timed out" in out
    assert "not applicable (skipped without a model call)" in out


def test_render_unknown_rows_report_reason_and_ptest_note():
    from ptest.recommendations import render_recommendations
    rows = [_row("DB-001", "unknown",
                 rationale=("Answered by ptest: no timing history yet: "
                            "run ptest --full once"), evidence=[])]
    rows += [_row(row_id, "satisfied") for row_id in CHECKLIST_IDS
             if row_id != "DB-001"]
    out = render_recommendations(
        _run(children=[_child(rows=rows, findings=[])])).decode("utf-8")
    assert ("Reason: no timing history yet: run ptest --full once") in out
    assert "Answered by ptest" not in out
    assert "(answered by ptest without a model call)" in out


def test_render_parallel_safety_group_before_parallel_item():
    from ptest.recommendations import render_recommendations
    rows = [_row("FIX-001", "satisfied")]
    rows += [_row("PARALLEL-001", "gap", rationale="Serial fallback.",
                  evidence=[_citation()])]
    findings = [_finding("PARALLEL-001", recipe=None)]
    out = render_recommendations(
        _run(children=[_child(rows=rows, findings=findings)])).decode("utf-8")
    assert "## Parallel safety" in out
    assert out.index("## Parallel safety") < out.index("## PARALLEL-001")


def test_render_rejects_bad_label_and_execution_shapes():
    from ptest.recommendations import render_recommendations
    from ptest.contracts import Problem
    with pytest.raises(Problem):
        render_recommendations(_run(children=[_child(execution={
            "status": "bogus", "detail": "ready", "fix": None})]))
    with pytest.raises(Problem):
        render_recommendations(_run(children=[_child(execution={
            "status": "executable", "detail": "ready", "fix": None,
            "extra": 1})]))
    with pytest.raises(Problem):
        render_recommendations(_run(children=[_child(rows=[
            _row("FIX-001", "satisfied", label="x" * 65)])]))
    with pytest.raises(Problem):
        render_recommendations(_run(children=[_child(rows=[
            _row("FIX-001", "satisfied", label="")])]))


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
    result = publish_recommendations(root, payload, None, source_proof=[])
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
    publish_recommendations(root, first, None, source_proof=[])
    previous = _identity_of(root)
    other = _child(scope="child-b")
    second = render_recommendations(_run(children=[other]))
    assert second != first
    result = publish_recommendations(root, second, previous, source_proof=[])
    assert result.status == "replaced"
    assert (root / "recommendations.md").read_bytes() == second


def test_publish_identical_bytes_is_unchanged(tmp_path, monkeypatch):
    from ptest.recommendations import render_recommendations
    from ptest.recommendations import publish_recommendations
    monkeypatch.setenv("PTEST_RECOMMENDATIONS_LOCK_DIR", str(tmp_path))
    root = tmp_path / "proj"
    root.mkdir()
    payload = render_recommendations(_run())
    publish_recommendations(root, payload, None, source_proof=[])
    previous = _identity_of(root)
    result = publish_recommendations(root, payload, previous, source_proof=[])
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
        publish_recommendations(
            root, render_recommendations(_run()), None, source_proof=[])
    assert (root / "recommendations.md").read_bytes() == b"# my own notes\n"


def test_publish_conflict_on_edited_report(tmp_path, monkeypatch):
    from ptest.recommendations import render_recommendations
    from ptest.recommendations import publish_recommendations
    from ptest.contracts import Problem
    monkeypatch.setenv("PTEST_RECOMMENDATIONS_LOCK_DIR", str(tmp_path))
    root = tmp_path / "proj"
    root.mkdir()
    payload = render_recommendations(_run())
    publish_recommendations(root, payload, None, source_proof=[])
    with (root / "recommendations.md").open("ab") as handle:
        handle.write(b"\n<!-- edited by hand -->\n")
    with pytest.raises(Problem, match="report-conflict"):
        publish_recommendations(root, payload, _identity_of(root), source_proof=[])
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
        publish_recommendations(root, payload, None, source_proof=[])
    assert outside.read_bytes() == b"external\n"
    (root / "recommendations.md").unlink()
    os.mkfifo(root / "recommendations.md")
    with pytest.raises(Problem, match="report-conflict"):
        publish_recommendations(root, payload, None, source_proof=[])


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
    publish_recommendations(root, first, None, source_proof=[])
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
        publish_recommendations(root, second, previous, source_proof=[])
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
    publish_recommendations(root, first, None, source_proof=[])
    stale = _identity_of(root)
    rival = render_recommendations(_run(children=[_child(scope="r")]))
    (root / "recommendations.md").write_bytes(rival)
    second = render_recommendations(_run(children=[_child(scope="b")]))
    with pytest.raises(Problem, match="stale-evidence"):
        publish_recommendations(root, second, stale, source_proof=[])
    assert (root / "recommendations.md").read_bytes() == rival


def test_publish_cancel_preserves_old_report_and_cleans_temp(tmp_path,
                                                             monkeypatch):
    from ptest.recommendations import publish_recommendations
    from ptest.recommendations import render_recommendations
    monkeypatch.setenv("PTEST_RECOMMENDATIONS_LOCK_DIR", str(tmp_path))
    root = tmp_path / "proj"
    root.mkdir()
    first = render_recommendations(_run())
    publish_recommendations(root, first, None, source_proof=[])
    previous = _identity_of(root)
    second = render_recommendations(_run(children=[_child(scope="b")]))

    def _boom(*args, **kwargs):
        raise KeyboardInterrupt()

    monkeypatch.setattr(os, "rename", _boom)
    with pytest.raises(KeyboardInterrupt):
        publish_recommendations(root, second, previous, source_proof=[])
    assert (root / "recommendations.md").read_bytes() == first
    assert list(root.glob("*.tmp.*")) == []


def test_publish_rejects_payload_with_bad_marker(tmp_path, monkeypatch):
    from ptest.recommendations import publish_recommendations
    from ptest.contracts import Problem
    monkeypatch.setenv("PTEST_RECOMMENDATIONS_LOCK_DIR", str(tmp_path))
    root = tmp_path / "proj"
    root.mkdir()
    with pytest.raises(Problem):
        publish_recommendations(root, b"no marker here\n", None, source_proof=[])
    with pytest.raises(TypeError):
        publish_recommendations(  # type: ignore[arg-type]
            root, "not-bytes", None, source_proof=[])
    assert not (root / "recommendations.md").exists()


def test_publish_sequential_writers_do_not_clobber(tmp_path, monkeypatch):
    from ptest.recommendations import publish_recommendations
    from ptest.recommendations import render_recommendations
    monkeypatch.setenv("PTEST_RECOMMENDATIONS_LOCK_DIR", str(tmp_path))
    root = tmp_path / "proj"
    root.mkdir()
    first = render_recommendations(_run())
    assert publish_recommendations(
        root, first, None, source_proof=[]).status == "created"
    prev = _identity_of(root)
    assert publish_recommendations(
        root, first, prev, source_proof=[]).status == "unchanged"
    lock_files = list(tmp_path.glob("rec-*.lock"))
    assert len(lock_files) == 1


# ---- audit blockers (dated report-audit + controller lock) ----

def _source_file(root: Path, rel="src/a.py", lines=10):
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(("x = 1\n" * lines).encode())
    return target


def _proof_for(target: Path, rel="src/a.py", start=1, end=3):
    raw = target.read_bytes()
    return [{"path": rel, "sha256": hashlib.sha256(raw).hexdigest(),
             "byte_count": len(raw),
             "start_line": start, "end_line": end}]


def test_publish_stale_source_proof_fails_closed_and_preserves(
        tmp_path, monkeypatch):
    from ptest.recommendations import publish_recommendations
    from ptest.recommendations import render_recommendations
    from ptest.contracts import Problem
    monkeypatch.setenv("PTEST_RECOMMENDATIONS_LOCK_DIR", str(tmp_path))
    root = tmp_path / "proj"
    root.mkdir()
    target = _source_file(root)
    proof = _proof_for(target)
    first = render_recommendations(_run())
    assert publish_recommendations(
        root, first, None, source_proof=proof).status == "created"
    previous = _identity_of(root)
    target.write_bytes(b"x = 2\n" * 10)  # source drift after collection
    second = render_recommendations(_run(children=[_child(scope="child-b")]))
    with pytest.raises(Problem, match="stale-evidence"):
        publish_recommendations(root, second, previous, source_proof=proof)
    assert (root / "recommendations.md").read_bytes() == first
    assert list(root.glob("*.tmp.*")) == []


def test_publish_parent_fsync_failure_fails_closed_and_restores(
        tmp_path, monkeypatch):
    from ptest.recommendations import publish_recommendations
    from ptest.recommendations import render_recommendations
    from ptest.contracts import Problem
    monkeypatch.setenv("PTEST_RECOMMENDATIONS_LOCK_DIR", str(tmp_path))
    root = tmp_path / "proj"
    root.mkdir()
    first = render_recommendations(_run())
    publish_recommendations(root, first, None, source_proof=[])
    previous = _identity_of(root)
    second = render_recommendations(_run(children=[_child(scope="child-b")]))
    real_fsync = os.fsync

    def _fail_on_dirs(fd):
        try:
            is_dir = stat.S_ISDIR(os.fstat(fd).st_mode)
        except OSError:
            is_dir = False
        if is_dir:
            raise OSError(5, "simulated parent sync failure")
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", _fail_on_dirs)
    with pytest.raises(Problem, match="state-unavailable"):
        publish_recommendations(root, second, previous, source_proof=[])
    assert (root / "recommendations.md").read_bytes() == first
    assert list(root.glob("*.tmp.*")) == []


def test_render_rejects_scope_command_markdown_injection():
    from ptest.recommendations import render_recommendations
    from ptest.contracts import Problem
    # Doctor-safe scopes render quoted/escaped instead of being rejected.
    out = render_recommendations(
        _run(children=[_child(scope="a b")])).decode("utf-8")
    assert "ptest 'a b'" in out
    out = render_recommendations(
        _run(children=[_child(scope="a|b")])).decode("utf-8")
    assert "a\\|b" in out
    # Actual controls and traversal are still rejected.
    for hostile in ("a\nptest --full", "a\x00b", "../up", "/abs", "a\\b"):
        with pytest.raises(Problem):
            render_recommendations(_run(children=[_child(scope=hostile)]))


# ---- TDD RED: four audited blockers (must fail before repair) ----

def test_publish_source_proof_omission_fails_closed_and_preserves(
        tmp_path, monkeypatch):
    from ptest.recommendations import publish_recommendations
    from ptest.recommendations import render_recommendations
    from ptest.contracts import Problem
    monkeypatch.setenv("PTEST_RECOMMENDATIONS_LOCK_DIR", str(tmp_path))
    root = tmp_path / "proj"
    root.mkdir()
    first = render_recommendations(_run())
    with pytest.raises(Problem, match="stale-evidence"):
        publish_recommendations(root, first, None)
    assert not (root / "recommendations.md").exists()
    # Omission must also fail when a prior report exists (no silent claim).
    publish_recommendations(root, first, None, source_proof=[])
    previous = _identity_of(root)
    second = render_recommendations(_run(children=[_child(scope="child-b")]))
    with pytest.raises(Problem, match="stale-evidence"):
        publish_recommendations(root, second, previous)
    assert (root / "recommendations.md").read_bytes() == first


def test_publish_source_prefix_proof_covers_truncated_input(
        tmp_path, monkeypatch):
    import ptest.recommendations as rec
    from ptest.recommendations import publish_recommendations
    from ptest.recommendations import render_recommendations
    from ptest.contracts import Problem
    monkeypatch.setenv("PTEST_RECOMMENDATIONS_LOCK_DIR", str(tmp_path))
    root = tmp_path / "proj"
    root.mkdir()
    bound = rec._MAX_SOURCE_BYTES
    chunk = b"x = 1\n" * (bound // 6 + 10)
    assert len(chunk) > bound
    target = root / "src" / "big.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(chunk)
    prefix = chunk[:bound]
    prefix_proof = [{"path": "src/big.py",
                     "sha256": hashlib.sha256(prefix).hexdigest(),
                     "byte_count": bound,
                     "start_line": 1, "end_line": 3}]
    full_proof = [{"path": "src/big.py",
                   "sha256": hashlib.sha256(chunk).hexdigest(),
                   "byte_count": bound,
                   "start_line": 1, "end_line": 3}]
    first = render_recommendations(_run())
    # Excerpt-chunk sha (what the assessment packet admits) must verify.
    assert publish_recommendations(
        root, first, None, source_proof=prefix_proof).status == "created"
    # An impossible full-file sha for truncated input must fail closed.
    (root / "recommendations.md").unlink()
    with pytest.raises(Problem, match="stale-evidence"):
        publish_recommendations(root, first, None, source_proof=full_proof)
    assert not (root / "recommendations.md").exists()


def test_publish_parent_symlink_swap_fails_closed_and_preserves(
        tmp_path, monkeypatch):
    from ptest.recommendations import publish_recommendations
    from ptest.recommendations import render_recommendations
    from ptest.contracts import Problem
    monkeypatch.setenv("PTEST_RECOMMENDATIONS_LOCK_DIR", str(tmp_path))
    root = tmp_path / "proj"
    root.mkdir()
    target = _source_file(root)
    proof = _proof_for(target)
    first = render_recommendations(_run())
    assert publish_recommendations(
        root, first, None, source_proof=proof).status == "created"
    previous = _identity_of(root)
    # Swap the parent component for a symlink to attacker content.
    outside = tmp_path / "outside"
    (outside / "src").mkdir(parents=True)
    (outside / "src" / "a.py").write_bytes(target.read_bytes())
    (root / "src").rename(root / "src-real")
    os.symlink(outside / "src", root / "src")
    try:
        second = render_recommendations(
            _run(children=[_child(scope="child-b")]))
        with pytest.raises(Problem, match="stale-evidence"):
            publish_recommendations(root, second, previous,
                                    source_proof=proof)
        assert (root / "recommendations.md").read_bytes() == first
    finally:
        os.unlink(root / "src")
        (root / "src-real").rename(root / "src")


def test_render_accepts_doctor_safe_scope_with_spaces():
    from ptest.recommendations import render_recommendations
    out = render_recommendations(
        _run(children=[_child(scope="web app")])).decode("utf-8")
    assert "web app" in out
    assert "ptest 'web app'" in out  # deterministic shell quoting
    assert "ptest --full" in out  # final gate preserved


def test_render_scope_shell_markdown_escaping_and_control_rejection():
    from ptest.recommendations import render_recommendations
    from ptest.contracts import Problem
    out = render_recommendations(
        _run(children=[_child(scope="a;b")])).decode("utf-8")
    assert "ptest 'a;b'" in out
    out = render_recommendations(
        _run(children=[_child(scope="a|b")])).decode("utf-8")
    assert "a\\|b" in out  # table cell stays one column
    # Actual controls and traversal are still rejected.
    for hostile in ("a\nptest --full", "a\x00b", "a\x1bb", "../up",
                    "/abs", "a\\b", "C:/x", "a/../b", ""):
        with pytest.raises(Problem):
            render_recommendations(_run(children=[_child(scope=hostile)]))


def test_publish_parent_fsync_failure_preserves_intervening_edit(
        tmp_path, monkeypatch):
    from ptest.recommendations import publish_recommendations
    from ptest.recommendations import render_recommendations
    from ptest.contracts import Problem
    monkeypatch.setenv("PTEST_RECOMMENDATIONS_LOCK_DIR", str(tmp_path))
    root = tmp_path / "proj"
    root.mkdir()
    first = render_recommendations(_run())
    publish_recommendations(root, first, None, source_proof=[])
    previous = _identity_of(root)
    second = render_recommendations(_run(children=[_child(scope="child-b")]))
    rival = render_recommendations(_run(children=[_child(scope="rival")]))
    real_fsync = os.fsync
    fired = []

    def _mutate_then_fail(fd):
        try:
            is_dir = stat.S_ISDIR(os.fstat(fd).st_mode)
        except OSError:
            is_dir = False
        if is_dir:
            if not fired:
                fired.append(True)
                # Intervening custom/newer report lands after rename,
                # before the failed parent sync's rollback decision.
                (root / "recommendations.md").write_bytes(rival)
            raise OSError(5, "simulated parent sync failure")
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", _mutate_then_fail)
    with pytest.raises(Problem, match="state-unavailable"):
        publish_recommendations(root, second, previous, source_proof=[])
    assert (root / "recommendations.md").read_bytes() == rival
    assert list(root.glob("*.tmp.*")) == []


def test_publish_lock_unavailable_fails_closed_without_publication(
        tmp_path, monkeypatch):
    from ptest.recommendations import publish_recommendations
    from ptest.recommendations import render_recommendations
    from ptest.contracts import Problem
    blocker = tmp_path / "not-a-dir"
    blocker.write_bytes(b"occupied")
    monkeypatch.setenv("PTEST_RECOMMENDATIONS_LOCK_DIR", str(blocker))
    root = tmp_path / "proj"
    root.mkdir()
    with pytest.raises(Problem, match="coordinator-unavailable|report-conflict"):
        publish_recommendations(
            root, render_recommendations(_run()), None, source_proof=[])
    assert not (root / "recommendations.md").exists()
    assert list(root.glob("*.tmp.*")) == []


# ---- audited repair: exact admitted byte_count + Markdown-safe command ----

def _proof_with_count(target_bytes: bytes, rel="src/a.py", start=1, end=1,
                      byte_count=None):
    import ptest.recommendations as rec
    if byte_count is None:
        byte_count = len(target_bytes)
        assert byte_count <= rec._MAX_SOURCE_BYTES
    prefix = target_bytes[:byte_count]
    return [{"path": rel, "sha256": hashlib.sha256(prefix).hexdigest(),
             "byte_count": byte_count,
             "start_line": start, "end_line": end}]


def test_proof_small_limit_prefix_verifies_without_false_stale(
        tmp_path, monkeypatch):
    from ptest.recommendations import publish_recommendations
    from ptest.recommendations import render_recommendations
    monkeypatch.setenv("PTEST_RECOMMENDATIONS_LOCK_DIR", str(tmp_path))
    root = tmp_path / "proj"
    root.mkdir()
    chunk = b"x = 1\n" * 4000  # ~24KiB: over 8KiB, under 64KiB
    assert 8 * 1024 < len(chunk) < 64 * 1024
    target = root / "src" / "a.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(chunk)
    proof = _proof_with_count(chunk, byte_count=8 * 1024,
                              start=1, end=3)
    first = render_recommendations(_run())
    assert publish_recommendations(
        root, first, None, source_proof=proof).status == "created"


def test_proof_utf8_boundary_shorter_prefix_verifies(tmp_path, monkeypatch):
    from ptest.recommendations import publish_recommendations
    from ptest.recommendations import render_recommendations
    monkeypatch.setenv("PTEST_RECOMMENDATIONS_LOCK_DIR", str(tmp_path))
    root = tmp_path / "proj"
    root.mkdir()
    body = b"a" * 8191 + "é".encode("utf-8") + b"\nline2\nline3\n" + b"z" * 1000
    assert len(body) > 8192
    # 8KiB cutoff lands inside the 2-byte é: caller admits UTF-8-safe 8191.
    assert body[8191:8193] == "é".encode("utf-8")
    byte_count = 8191
    body[:byte_count].decode("utf-8")
    target = root / "src" / "a.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(body)
    proof = _proof_with_count(body, byte_count=byte_count, start=1, end=1)
    first = render_recommendations(_run())
    assert publish_recommendations(
        root, first, None, source_proof=proof).status == "created"


def test_proof_huge_file_beyond_1mib_still_publishable(tmp_path, monkeypatch):
    from ptest.recommendations import publish_recommendations
    from ptest.recommendations import render_recommendations
    monkeypatch.setenv("PTEST_RECOMMENDATIONS_LOCK_DIR", str(tmp_path))
    root = tmp_path / "proj"
    root.mkdir()
    prefix = b"x = 1\n" * 1366  # 8196 bytes of valid UTF-8, 1366 lines
    prefix = prefix[:8192]
    assert len(prefix) == 8192
    rest = b"\xff\xfe-binary \x80 invalid utf8 beyond prefix\n" * 4000
    chunk = prefix + rest + b"z" * (1100 * 1024)
    assert len(chunk) > 1 << 20
    target = root / "src" / "a.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(chunk)
    proof = _proof_with_count(chunk, byte_count=8192, start=1, end=3)
    first = render_recommendations(_run())
    # Invalid UTF-8 beyond the admitted prefix is irrelevant.
    assert publish_recommendations(
        root, first, None, source_proof=proof).status == "created"


def test_proof_true_prefix_drift_rejected(tmp_path, monkeypatch):
    from ptest.recommendations import publish_recommendations
    from ptest.recommendations import render_recommendations
    from ptest.contracts import Problem
    monkeypatch.setenv("PTEST_RECOMMENDATIONS_LOCK_DIR", str(tmp_path))
    root = tmp_path / "proj"
    root.mkdir()
    chunk = b"x = 1\n" * 4000
    target = root / "src" / "a.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(chunk)
    proof = _proof_with_count(chunk, byte_count=8 * 1024, start=1, end=3)
    first = render_recommendations(_run())
    assert publish_recommendations(
        root, first, None, source_proof=proof).status == "created"
    previous = _identity_of(root)
    mutated = b"y = 2\n" + chunk[len(b"y = 2\n"):]
    assert mutated[:8192] != chunk[:8192]
    target.write_bytes(mutated)
    second = render_recommendations(_run(children=[_child(scope="child-b")]))
    with pytest.raises(Problem, match="stale-evidence"):
        publish_recommendations(root, second, previous, source_proof=proof)
    assert (root / "recommendations.md").read_bytes() == first


def test_proof_byte_count_bounds_rejected(tmp_path, monkeypatch):
    import ptest.recommendations as rec
    from ptest.recommendations import publish_recommendations
    from ptest.recommendations import render_recommendations
    from ptest.contracts import Problem
    monkeypatch.setenv("PTEST_RECOMMENDATIONS_LOCK_DIR", str(tmp_path))
    root = tmp_path / "proj"
    root.mkdir()
    target = _source_file(root)
    raw = target.read_bytes()
    good_sha = hashlib.sha256(raw).hexdigest()
    first = render_recommendations(_run())
    for bad in (-1, rec._MAX_SOURCE_BYTES + 1, 1 << 20, True, "8192", None,
                3.5):
        bad_proof = [{"path": "src/a.py", "sha256": good_sha,
                      "byte_count": bad, "start_line": 1, "end_line": 3}]
        with pytest.raises(Problem):
            publish_recommendations(root, first, None, source_proof=bad_proof)
    assert not (root / "recommendations.md").exists()


def test_proof_line_bound_beyond_admitted_prefix_rejected(
        tmp_path, monkeypatch):
    from ptest.recommendations import publish_recommendations
    from ptest.recommendations import render_recommendations
    from ptest.contracts import Problem
    monkeypatch.setenv("PTEST_RECOMMENDATIONS_LOCK_DIR", str(tmp_path))
    root = tmp_path / "proj"
    root.mkdir()
    body = b"line1\nline2\nline3\nline4\nline5\n" + b"z" * 9000
    target = root / "src" / "a.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(body)
    # Admitted prefix covers only the first line.
    byte_count = len(b"line1\n")
    prefix_proof = _proof_with_count(body, byte_count=byte_count,
                                     start=1, end=5)
    with pytest.raises(Problem, match="stale-evidence"):
        publish_recommendations(
            root, render_recommendations(_run()), None,
            source_proof=prefix_proof)
    assert not (root / "recommendations.md").exists()


def test_publish_accepts_more_than_256_source_proofs_and_rejects_above_total_bound(
        tmp_path, monkeypatch):
    import ptest.recommendations as rec
    from ptest.contracts import Problem
    from ptest.recommendations import publish_recommendations
    from ptest.recommendations import render_recommendations

    monkeypatch.setenv("PTEST_RECOMMENDATIONS_LOCK_DIR", str(tmp_path / "locks"))
    root = tmp_path / "proj"
    root.mkdir()
    source_root = root / "src"
    source_root.mkdir()
    proof = []
    for index in range(257):
        raw = f"VALUE = {index}\n".encode("ascii")
        relative = f"src/module_{index:03d}.py"
        (root / relative).write_bytes(raw)
        proof.append({"path": relative,
                      "sha256": hashlib.sha256(raw).hexdigest(),
                      "byte_count": len(raw),
                      "start_line": 1, "end_line": 1})

    payload = render_recommendations(_run())
    assert publish_recommendations(
        root, payload, None, source_proof=proof).status == "created"
    assert (root / "recommendations.md").read_bytes() == payload

    too_many = (proof * (16_385 // len(proof) + 1))[:16_385]
    assert len(too_many) == 16_385
    with pytest.raises(Problem, match="invalid-bound"):
        publish_recommendations(
            root, payload, _identity_of(root), source_proof=too_many)


def test_publish_restores_prior_report_when_cancelled_after_atomic_rename(
        tmp_path, monkeypatch):
    import ptest.recommendations as rec
    from ptest.recommendations import publish_recommendations
    from ptest.recommendations import render_recommendations

    monkeypatch.setenv("PTEST_RECOMMENDATIONS_LOCK_DIR", str(tmp_path / "locks"))
    root = tmp_path / "proj"
    root.mkdir()
    first = render_recommendations(_run())
    publish_recommendations(root, first, None, source_proof=[])
    previous = _identity_of(root)
    second = render_recommendations(_run(children=[_child(scope="child-b")]))
    real_rename = rec.os.rename
    interrupted = []

    def rename_then_interrupt(src, dst, *args, **kwargs):
        result = real_rename(src, dst, *args, **kwargs)
        if dst == "recommendations.md" and not interrupted:
            interrupted.append(True)
            raise KeyboardInterrupt
        return result

    monkeypatch.setattr(rec.os, "rename", rename_then_interrupt)
    with pytest.raises(KeyboardInterrupt):
        publish_recommendations(root, second, previous, source_proof=[])

    assert interrupted == [True]
    assert (root / "recommendations.md").read_bytes() == first


@pytest.mark.parametrize("stage_operation", ("write", "fsync"))
def test_publish_cleans_staging_resources_when_cancelled_during_stage(
        tmp_path, monkeypatch, stage_operation):
    import ptest.recommendations as rec
    from ptest.recommendations import publish_recommendations
    from ptest.recommendations import render_recommendations

    monkeypatch.setenv("PTEST_RECOMMENDATIONS_LOCK_DIR", str(tmp_path / "locks"))
    root = tmp_path / "proj"
    root.mkdir()
    first = render_recommendations(_run())
    publish_recommendations(root, first, None, source_proof=[])
    previous = _identity_of(root)
    second = render_recommendations(_run(children=[_child(scope="child-b")]))

    stage_fds = []
    real_open = rec.os.open

    def track_stage_fd(path, *args, **kwargs):
        fd = real_open(path, *args, **kwargs)
        if isinstance(path, str) and path.startswith("recommendations.md.tmp."):
            stage_fds.append(fd)
        return fd

    monkeypatch.setattr(rec.os, "open", track_stage_fd)
    if stage_operation == "write":
        real_write = rec.os.write

        def interrupt_stage_write(fd, data):
            if stage_fds and fd == stage_fds[0]:
                raise KeyboardInterrupt
            return real_write(fd, data)

        monkeypatch.setattr(rec.os, "write", interrupt_stage_write)
    else:
        real_fsync = rec.os.fsync

        def interrupt_stage_fsync(fd):
            if stage_fds and fd == stage_fds[0]:
                raise KeyboardInterrupt
            return real_fsync(fd)

        monkeypatch.setattr(rec.os, "fsync", interrupt_stage_fsync)

    with pytest.raises(KeyboardInterrupt):
        publish_recommendations(root, second, previous, source_proof=[])

    assert len(stage_fds) == 1
    with pytest.raises(OSError):
        os.fstat(stage_fds[0])
    assert list(root.glob("recommendations.md.tmp.*")) == []
    assert (root / "recommendations.md").read_bytes() == first


def test_publish_cleans_staging_resources_when_cancelled_during_final_close(
        tmp_path, monkeypatch):
    import ptest.recommendations as rec
    from ptest.recommendations import publish_recommendations
    from ptest.recommendations import render_recommendations

    monkeypatch.setenv("PTEST_RECOMMENDATIONS_LOCK_DIR", str(tmp_path / "locks"))
    root = tmp_path / "proj"
    root.mkdir()
    first = render_recommendations(_run())
    publish_recommendations(root, first, None, source_proof=[])
    previous = _identity_of(root)
    second = render_recommendations(_run(children=[_child(scope="child-b")]))

    stage_fds = []
    real_open = rec.os.open
    real_close = rec.os.close
    interrupted = []

    def track_stage_fd(path, *args, **kwargs):
        fd = real_open(path, *args, **kwargs)
        if isinstance(path, str) and path.startswith("recommendations.md.tmp."):
            stage_fds.append(fd)
        return fd

    def interrupt_final_close(fd):
        if stage_fds and fd == stage_fds[0] and not interrupted:
            interrupted.append(True)
            raise KeyboardInterrupt
        return real_close(fd)

    monkeypatch.setattr(rec.os, "open", track_stage_fd)
    monkeypatch.setattr(rec.os, "close", interrupt_final_close)

    with pytest.raises(KeyboardInterrupt):
        publish_recommendations(root, second, previous, source_proof=[])

    assert interrupted == [True]
    assert len(stage_fds) == 1
    with pytest.raises(OSError):
        os.fstat(stage_fds[0])
    assert list(root.glob("recommendations.md.tmp.*")) == []
    assert (root / "recommendations.md").read_bytes() == first


def test_publish_deadline_bounds_lock_wait_without_wall_clock_sleep(
        tmp_path, monkeypatch):
    import ptest.recommendations as rec
    from ptest.contracts import Problem
    from ptest.recommendations import publish_recommendations
    from ptest.recommendations import render_recommendations

    monkeypatch.setenv("PTEST_RECOMMENDATIONS_LOCK_DIR", str(tmp_path / "locks"))
    root = tmp_path / "proj"
    root.mkdir()
    clock = [0.0]

    def fake_flock(*args, **kwargs):
        raise BlockingIOError

    monkeypatch.setattr(rec.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        rec.time, "sleep", lambda delay: clock.__setitem__(0, clock[0] + delay))
    monkeypatch.setattr(rec.fcntl, "flock", fake_flock)

    with pytest.raises(Problem, match="review-timeout"):
        publish_recommendations(
            root, render_recommendations(_run()), None,
            source_proof=[], deadline=0.025)

    assert 0 < clock[0] <= 0.025
    assert not (root / "recommendations.md").exists()


def test_publish_rolls_back_if_deadline_expires_after_rename(
        tmp_path, monkeypatch):
    import ptest.recommendations as rec
    from ptest.contracts import Problem
    from ptest.recommendations import publish_recommendations
    from ptest.recommendations import render_recommendations

    monkeypatch.setenv("PTEST_RECOMMENDATIONS_LOCK_DIR", str(tmp_path / "locks"))
    root = tmp_path / "proj"
    root.mkdir()
    first = render_recommendations(_run())
    publish_recommendations(root, first, None, source_proof=[])
    previous = _identity_of(root)
    second = render_recommendations(_run(children=[_child(scope="child-b")]))
    clock = [0.0]
    real_rename = rec.os.rename

    def rename_then_expire(src, dst, *args, **kwargs):
        result = real_rename(src, dst, *args, **kwargs)
        if dst == "recommendations.md":
            clock[0] = 5.0
        return result

    monkeypatch.setattr(rec.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(rec.os, "rename", rename_then_expire)
    with pytest.raises(Problem, match="review-timeout"):
        publish_recommendations(
            root, second, previous, source_proof=[], deadline=5.0)

    assert (root / "recommendations.md").read_bytes() == first


def test_render_backtick_scope_markdown_safe_single_command():
    import shlex
    from ptest.recommendations import render_recommendations
    for scope in ("a`b", "a;b|c$d`e", "a'b\"c", "web app"):
        argv = f"ptest {shlex.quote(scope)}"
        out = render_recommendations(
            _run(children=[_child(scope=scope)])).decode("utf-8")
        # shlex-quoted argv stays visible as one command ...
        assert argv in out
        # ... but never inside a variable backtick span ...
        assert f"`{argv}`" not in out
        # ... and appears as its own indented code line.
        assert any(line.strip() == argv
                   for line in out.splitlines()), scope


def test_render_accepts_dot_limitation_path_for_root_project(
        tmp_path, monkeypatch):
    """Root-project partial evidence (paths ["."]) renders and publishes.

    Regression: ``cli._assessment_limitations`` emits ptest's own
    partial-evidence limitation with ``paths == [packet.scope]``, which is
    ``"."`` for a root project; the public contract accepts ``"."`` as the
    root, so the render path must too.
    """
    from ptest.recommendations import render_recommendations
    from ptest.recommendations import publish_recommendations
    monkeypatch.setenv("PTEST_RECOMMENDATIONS_LOCK_DIR", str(tmp_path))
    root = tmp_path / "proj"
    root.mkdir()
    limitation = {
        "code": "partial-evidence",
        "message": "Bounded evidence omitted 1 entries and truncated "
                   "0 files or excerpts.",
        "paths": ["."],
    }
    child = _child(scope=".")
    child["limitations"] = [limitation]
    payload = render_recommendations(
        _run(children=[child], limitations=[limitation]))
    out = payload.decode("utf-8")
    assert "partial-evidence" in out
    assert "` . `" not in out
    assert "`" + "." + "`" in out
    result = publish_recommendations(root, payload, None, source_proof=[])
    assert result.status == "created"
    assert (root / "recommendations.md").is_file()


@pytest.mark.parametrize("path", ["./x", "../x", "a//b", "`x`", "a|b",
                                   "/x", "C:/x", "~/x", "a\nb", "a\\b"])
def test_render_still_rejects_non_normalized_limitation_paths(path):
    """Every other limitation-path rejection stays exactly as before."""
    from ptest.recommendations import render_recommendations
    from ptest.contracts import Problem
    limitation = {"code": "partial-evidence",
                  "message": "Bounded evidence was partial.",
                  "paths": [path]}
    with pytest.raises(Problem, match="report-invalid"):
        render_recommendations(_run(limitations=[limitation]))


# --- Round 17 twins: dropped-citation counts surfaced per item --------------


def test_render_surfaces_dropped_citation_count_per_item():
    from ptest.recommendations import render_recommendations

    rows = _mixed_rows()
    rows[0] = dict(rows[0], dropped_citations=2)
    out = render_recommendations(_run(children=[_child(rows=rows)])).decode(
        "utf-8")
    assert "2 invalid citations dropped" in out


def test_render_singular_dropped_citation_count():
    from ptest.recommendations import render_recommendations

    rows = _mixed_rows()
    rows[0] = dict(rows[0], dropped_citations=1)
    out = render_recommendations(_run(children=[_child(rows=rows)])).decode(
        "utf-8")
    assert "1 invalid citation dropped" in out


def test_render_without_dropped_counts_names_no_drops():
    from ptest.recommendations import render_recommendations

    out = render_recommendations(_run()).decode("utf-8")
    assert "invalid citation" not in out
