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
                     "start_line": 1, "end_line": 3}]
    full_proof = [{"path": "src/big.py",
                   "sha256": hashlib.sha256(chunk).hexdigest(),
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
