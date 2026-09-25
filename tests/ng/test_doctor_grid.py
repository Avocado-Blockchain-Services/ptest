"""Doctor grid output twins (spec 2026-09-25-doctor-grid, section G).

Presentation only: rows = checklist items grouped (general, parallel
safety, Parallel execution), columns = projects. Covers the plain
2-project snapshot, TTY-only color, ASCII fallback, narrow-terminal
stacking, unknown-reason grouping, never-truncated gaps and hostile
label/path escaping.
"""
from __future__ import annotations

from types import SimpleNamespace

from ptest.render import render_agent_assessment


def _workspace(*declarations, runner="pytest"):
    repos = tuple(
        SimpleNamespace(
            declaration=declaration,
            config=SimpleNamespace(
                runner=SimpleNamespace(kind=SimpleNamespace(value=runner)),
                selection=SimpleNamespace(enabled=True)),
            config_problem=None)
        for declaration in declarations)
    return SimpleNamespace(repositories=repos)


def _citation(path="api/tests/test_example.py", start=3, end=9):
    return {"path": path, "start_line": start, "end_line": end,
            "sha256": "ef" * 32}


def _row(row_id, status, label=None, rationale=None, evidence="default"):
    if rationale is None:
        rationale = f"Row {row_id} judged {status} against packet excerpts."
    if evidence == "default":
        evidence = ([] if status in ("unknown", "not-applicable")
                    else [_citation()])
    row = {"id": row_id, "status": status, "rationale": rationale,
           "evidence": evidence}
    if label is not None:
        row["label"] = label
    return row


def _facts(**overrides):
    facts = {
        "project": "api", "runner": "pytest", "runs": True,
        "runs_reason": None, "runs_fix": None,
        "parallel": "4 workers (xdist, --dist loadgroup)",
        "parallel_short": "4 workers", "parallel_fix": None,
        "setup": "uv sync --locked",
        "full_suite": None, "full_blocked": None,
    }
    facts.update(overrides)
    return facts


_SHARED_UNKNOWN = ("offline static run: model review unavailable")


def _two_projects():
    api = {
        "scope": "api",
        "facts": _facts(),
        "rows": [
            _row("FIX-001", "satisfied", label="Test data factories"),
            _row("SELECT-001", "satisfied", label="Test selection"),
            _row("TIMING-001", "unknown", label="Test timing",
                 rationale="No timing history yet: run ptest --full once."),
            _row("FIX-002", "gap", label="Fixture state isolation",
                 rationale="Shares module-global state between tests."),
            _row("DB-001", "unknown", label="Database setup reuse",
                 rationale=_SHARED_UNKNOWN),
            _row("CACHE-001", "unknown", label="Cache isolation",
                 rationale=_SHARED_UNKNOWN),
            _row("PARALLEL-001", "gap", label="Parallel execution",
                 rationale="Runs serially on one worker."),
        ],
        "findings": [
            {"id": "FIX-002",
             "summary": "Share one module-global fixture.",
             "suggested_change": "Build a per-test factory.",
             "recipe_id": "factories", "evidence": [_citation()]},
            {"id": "PARALLEL-001", "summary": "Runs serially.",
             "suggested_change": "Enable xdist.",
             "recipe_id": None, "evidence": [_citation()]},
        ],
        "limitations": [],
    }
    web = {
        "scope": "web",
        "facts": _facts(project="web"),
        "rows": [
            _row("FIX-001", "satisfied", label="Test data factories"),
            _row("SELECT-001", "unknown", label="Test selection",
                 rationale="Could not trace the vitest invocation."),
            _row("TIMING-001", "satisfied", label="Test timing"),
            _row("FIX-002", "satisfied", label="Fixture state isolation"),
            _row("DB-001", "unknown", label="Database setup reuse",
                 rationale=_SHARED_UNKNOWN),
            _row("CACHE-001", "satisfied", label="Cache isolation"),
            _row("PARALLEL-001", "satisfied", label="Parallel execution"),
        ],
        "findings": [],
        "limitations": [],
    }
    return [api, web]


def _grid(**overrides):
    params = {"report_path": "recommendations.md",
              "publication_status": "created", "width": 100,
              "color": False, "repo": "shop",
              "provider": "codex/gpt-5", "duration_s": 45,
              "calls": 9}
    params.update(overrides)
    return render_agent_assessment(
        _two_projects(), _workspace("api", "web"), **params)


def test_grid_header_facts_and_single_table_at_width_100():
    text = _grid()

    assert text.splitlines()[0] == (
        "shop · codex/gpt-5 · 14 checks · 9 calls · 45s")
    assert ("api  pytest · runs ✓ · 4 workers · "
            "setup: uv sync --locked") in text
    assert ("web  pytest · runs ✓ · 4 workers · "
            "setup: uv sync --locked") in text
    # One table: exactly one top border and one header row.
    assert text.count("╭") == 1
    assert text.count("│ check") == 1
    assert ("│ Test data factories     │ ✓ ok          │ "
            "✓ ok      │") in text
    assert ("│ Fixture state isolation │ ✗ gap         │ "
            "✓ ok      │") in text
    assert "├─ parallel safety ─" in text
    assert "│ Parallel execution" in text
    assert text.index("parallel safety") < text.index(
        "│ Parallel execution")
    # No duplicate header row for the single-item Parallel execution
    # group: only the divider separates it from parallel safety.
    assert "Parallel execution" not in text.split("Cache isolation")[0]
    # One compact footer tally per project, separated by a rule.
    assert "│ 2 ✓  2 ✗  3 ? │" in text
    assert "│ 5 ✓  2 ?  │" in text
    assert text.index("Test data factories") < text.index(
        "parallel safety")
    for line in text.splitlines():
        assert len(line) <= 100, line


def test_grid_header_offline_provider_and_singular_counts():
    child = {
        "scope": "api", "facts": _facts(),
        "rows": [_row("FIX-001", "satisfied",
                      label="Test data factories")],
        "findings": [], "limitations": [],
    }
    text = render_agent_assessment(
        [child], _workspace("api"), report_path="recommendations.md",
        publication_status="skipped", width=100, color=False,
        repo="shop", provider="offline", duration_s=75, calls=1)

    assert text.splitlines()[0] == "shop · offline · 1 check · 1 call · 1m15s"


def test_grid_gaps_list_project_label_finding_and_fix():
    text = _grid()

    gaps = text[text.index("Gaps"):text.index("Unknowns")]
    assert "api · Fixture state isolation" in gaps
    assert "Share one module-global fixture." in gaps
    assert "→ Build a per-test factory." in gaps
    assert "api · Parallel execution" in gaps
    assert "→ Enable xdist." in gaps
    assert gaps.index("api · Fixture state isolation") < gaps.index(
        "api · Parallel execution")


def test_grid_gaps_never_truncate_long_findings():
    # Past the old 512-byte finding-line cap, inside the field bound.
    words = " ".join(f"word{i:03d}" for i in range(100))
    child = {
        "scope": "api", "facts": _facts(),
        "rows": [_row("FIX-002", "gap", label="Fixture state isolation",
                      rationale="Shares state.")],
        "findings": [{"id": "FIX-002", "summary": words,
                      "suggested_change": "Apply the packaged recipe.",
                      "recipe_id": "factories",
                      "evidence": [_citation()]}],
        "limitations": [],
    }
    text = render_agent_assessment(
        [child], _workspace("api"), report_path="recommendations.md",
        publication_status="created", width=90, color=False,
        repo="shop", provider="codex/gpt-5", duration_s=5, calls=1)

    assert "…" not in text
    assert "[truncated]" not in text
    for index in range(100):
        assert f"word{index:03d}" in text
    assert "Apply the packaged recipe." in text


def test_grid_unknowns_group_identical_reasons_per_project():
    text = _grid()

    unknowns = text[text.index("Unknowns"):text.index("Next:")]
    # The wide prefix does not fit a third of the line: the shared
    # reason drops to the next line, indented.
    assert "? api: Database setup reuse, Cache isolation\n" in unknowns
    assert ("    offline static run: model review unavailable") in unknowns
    # Same reason under another project stays a separate line.
    assert ("? web: Database setup reuse — "
            "offline static run: model review unavailable") in unknowns
    # Distinct reasons list each check on its own line.
    assert "? api: Test timing — No timing history yet" in unknowns
    assert "? web: Test selection — Could not trace" in unknowns
    assert unknowns.count(
        "offline static run: model review unavailable") == 2


def test_grid_unknown_lines_wrap_with_hanging_indent():
    rationale = " ".join(f"token{i:03d}" for i in range(30))
    child = {
        "scope": "api", "facts": _facts(),
        "rows": [_row("DB-001", "unknown", label="Database setup reuse",
                      rationale=rationale, evidence=[])],
        "findings": [], "limitations": [],
    }
    text = render_agent_assessment(
        [child], _workspace("api"), report_path="recommendations.md",
        publication_status="created", width=60, color=False,
        repo="shop", provider="offline", duration_s=3, calls=0)

    unknowns = text[text.index("Unknowns"):]
    for line in unknowns.splitlines()[1:]:
        if line.startswith("Next:") or line.startswith("Report:"):
            break
        assert len(line) <= 60, line
    for index in range(30):
        assert f"token{index:03d}" in unknowns


def test_grid_next_fix_with_gaps_and_full_without():
    assert "Next: ptest doctor --fix" in _grid()

    children = _two_projects()
    for child in children:
        for row in child["rows"]:
            if row["status"] == "gap":
                row["status"] = "satisfied"
        child["findings"] = []
    text = render_agent_assessment(
        children, _workspace("api", "web"),
        report_path="recommendations.md", publication_status="created",
        width=100, color=False, repo="shop", provider="codex/gpt-5",
        duration_s=45, calls=9)

    assert "Next: ptest --full" in text
    assert "Gaps" not in text.split("Unknowns")[0].split("┘")[-1]


def test_grid_report_trailer_closes_output():
    text = _grid()

    assert text.rstrip().endswith(
        "Report: recommendations.md (created) — citations, fixes and "
        "verification steps.")
    assert text.endswith("\n") and not text.endswith("\n\n")


def test_grid_colors_icons_on_tty_only(monkeypatch):
    tty = _grid(color=True)
    assert "\x1b[32m✓ ok\x1b[0m" in tty
    assert "\x1b[31m✗ gap\x1b[0m" in tty
    assert "\x1b[33m? unknown\x1b[0m" in tty
    assert "\x1b[2m" in tty  # dim borders
    assert "\x1b[1m" in tty  # bold headers

    assert "\x1b" not in _grid(color=False)

    monkeypatch.setenv("NO_COLOR", "1")
    dimmed = _grid(color=True)
    assert "\x1b" not in dimmed
    assert "[ok]" in dimmed and "[gap]" in dimmed


def test_grid_ascii_fallback_without_utf8_stdout():
    text = _grid(encoding="ascii")

    text.encode("ascii")  # raises on any non-ASCII leak
    assert "╭" not in text and "│" not in text and "✓" not in text
    assert "+-" in text
    assert "[ok]" in text and "[gap]" in text


def test_grid_narrow_terminal_stacks_one_table_per_project():
    import copy

    children = _two_projects()
    third = copy.deepcopy(children[0])
    third["scope"] = "cli"
    third["facts"] = _facts(project="cli")
    children.append(third)
    text = render_agent_assessment(
        children, _workspace("api", "web", "cli"),
        report_path="recommendations.md", publication_status="created",
        width=60, color=False, repo="shop", provider="codex/gpt-5",
        duration_s=45, calls=9)

    # The three-column table cannot fit, so each project gets its own.
    assert text.count("╭") == 3
    first, second, third_table = text.split("╭")[1:]
    assert "api" in first and "web" not in first and "cli" not in first
    assert "web" in second and "cli" not in second
    assert "cli" in third_table
    for line in text.splitlines():
        if line[:1] in ("╭", "│", "├", "╰"):
            assert len(line) <= 60, line


def test_grid_facts_show_not_runnable_project():
    children = _two_projects()
    children[1]["facts"] = _facts(
        project="web", runner="pytest", runs=False,
        runs_reason="no tests found", runs_fix=None,
        parallel=None, parallel_short=None, setup=None)
    text = render_agent_assessment(
        children, _workspace("api", "web"),
        report_path="recommendations.md", publication_status="created",
        width=100, color=False, repo="shop", provider="codex/gpt-5",
        duration_s=45, calls=9)

    assert "web  pytest · runs ✗ — no tests found" in text


def test_grid_hostile_labels_and_scopes_stay_inert():
    child = {
        "scope": "api|web",
        "facts": _facts(),
        "rows": [_row("FIX-001", "satisfied",
                      label="Fixture | `isolation` \u202eRTL &#40;",
                      rationale="ok")],
        "findings": [], "limitations": [],
    }
    text = render_agent_assessment(
        [child], _workspace("api|web"),
        report_path="recommendations.md", publication_status="created",
        width=90, color=False, repo="sh|op", provider="codex/gpt-5",
        duration_s=5, calls=1)

    assert "\u202e" not in text
    assert "&#" not in text
    assert "|" not in text.splitlines()[0]  # header cannot gain cells
    table = text[text.index("╭"):text.index("╯")]
    # The escaped label keeps every border on its own line.
    counts = {line.count("│") for line in table.splitlines()
              if line.startswith("│")}
    assert counts == {3}
    assert "Fixture / 'isolation'" in table


def test_grid_long_labels_truncate_to_fit_single_table():
    child = {
        "scope": "api", "facts": _facts(),
        "rows": [_row(
            "FIX-001", "satisfied",
            label=("A very long checklist label that must shrink to fit "
                   "the terminal width ok"))],
        "findings": [], "limitations": [],
    }
    text = render_agent_assessment(
        [child], _workspace("api"), report_path="recommendations.md",
        publication_status="created", width=60, color=False,
        repo="shop", provider="offline", duration_s=3, calls=0)

    assert text.count("╭") == 1
    assert "…" in text
    for line in text.splitlines():
        if line[:1] in ("╭", "│", "├", "╰"):
            assert len(line) <= 60, line


def test_grid_empty_children_render_header_next_and_trailer():
    text = render_agent_assessment(
        [], _workspace("api"), report_path="recommendations.md",
        publication_status="created", width=90, color=False,
        repo="shop", provider="offline", duration_s=3, calls=0)

    assert text.splitlines()[0] == "shop · offline · 0 checks · 0 calls · 3s"
    assert "╭" not in text
    assert "Next: ptest --full" in text
    assert text.rstrip().endswith("and verification steps.")


def test_grid_facts_show_full_suite_detail_on_second_line():
    children = _two_projects()
    children[0]["facts"] = _facts(
        full_suite='your pytest config: -m "not slow"')
    text = render_agent_assessment(
        children, _workspace("api", "web"),
        report_path="recommendations.md", publication_status="created",
        width=100, color=False, repo="shop", provider="codex/gpt-5",
        duration_s=45, calls=9)

    lines = text.splitlines()
    facts_at = next(index for index, line in enumerate(lines)
                    if line.startswith("api  pytest"))
    assert lines[facts_at + 1] == (
        '     full suite = your pytest config: -m "not slow"')
    # The sibling project without detail keeps a single facts line.
    web_at = next(index for index, line in enumerate(lines)
                  if line.startswith("web  pytest"))
    assert lines[web_at + 1] == ""
    assert lines[web_at + 2].startswith("╭")

    dimmed = render_agent_assessment(
        children, _workspace("api", "web"),
        report_path="recommendations.md", publication_status="created",
        width=100, color=True, repo="shop", provider="codex/gpt-5",
        duration_s=45, calls=9)
    assert ('\x1b[2m     full suite = your pytest config: -m "not slow"'
            '\x1b[0m') in dimmed


def test_grid_gaps_separate_blocks_with_aligned_hanging_indent():
    text = _grid()

    gaps = text[text.index("Gaps"):text.index("Unknowns")]
    assert "  Share one module-global fixture." in gaps
    assert "      Share one module-global fixture." not in gaps
    assert "  → Build a per-test factory." in gaps
    assert ("→ Build a per-test factory.\n"
            "\n"
            "✗ api · Parallel execution") in gaps


def test_grid_unknowns_wide_prefix_drops_reason_to_next_line():
    rationale = " ".join(f"token{i:03d}" for i in range(30))
    child = {
        "scope": "api", "facts": _facts(),
        "rows": [_row("DB-001", "unknown", label="Database setup reuse",
                      rationale=rationale, evidence=[])],
        "findings": [], "limitations": [],
    }
    text = render_agent_assessment(
        [child], _workspace("api"), report_path="recommendations.md",
        publication_status="created", width=60, color=False,
        repo="shop", provider="offline", duration_s=3, calls=0)

    unknowns = text[text.index("Unknowns"):text.index("Next:")]
    body = [line for line in unknowns.splitlines()[1:] if line]
    # The `? api: Database setup reuse — ` prefix is wider than a
    # third of the line: no thin reason column, the reason starts on
    # the next line indented four spaces.
    assert body[0] == "? api: Database setup reuse"
    assert len(body) > 1
    for line in body[1:]:
        assert line.startswith("    "), line
        assert len(line) <= 60, line
    for index in range(30):
        assert f"token{index:03d}" in unknowns


def test_grid_unknowns_narrow_prefix_keeps_reason_on_head_line():
    child = {
        "scope": "api", "facts": _facts(),
        "rows": [_row("DB-001", "unknown", label="DB",
                      rationale="Brief reason.", evidence=[])],
        "findings": [], "limitations": [],
    }
    text = render_agent_assessment(
        [child], _workspace("api"), report_path="recommendations.md",
        publication_status="created", width=60, color=False,
        repo="shop", provider="offline", duration_s=3, calls=0)

    unknowns = text[text.index("Unknowns"):text.index("Next:")]
    assert "? api: DB — Brief reason." in unknowns.splitlines()


def test_grid_not_applicable_groups_reasons_dim():
    shared = "no legacy runner configured here"
    child = {
        "scope": "api", "facts": _facts(),
        "rows": [
            _row("SEL-1", "not-applicable", label="Check one",
                 rationale=f"Skipped without a model call: {shared}."),
            _row("SEL-2", "not-applicable", label="Check two",
                 rationale=f"{shared}."),
            _row("SEL-3", "not-applicable", label="Check three",
                 rationale="Another distinct reason."),
        ],
        "findings": [], "limitations": [],
    }
    text = render_agent_assessment(
        [child], _workspace("api"), report_path="recommendations.md",
        publication_status="created", width=100, color=False,
        repo="shop", provider="codex/gpt-5", duration_s=3, calls=1)

    assert "Not applicable" in text
    section = text[text.index("Not applicable"):text.index("Next:")]
    assert ("– api: Check one, Check two — "
            "no legacy runner configured here.") in section
    assert "– api: Check three — Another distinct reason." in section

    dimmed = render_agent_assessment(
        [child], _workspace("api"), report_path="recommendations.md",
        publication_status="created", width=100, color=True,
        repo="shop", provider="codex/gpt-5", duration_s=3, calls=1)
    assert "\x1b[2m– api: Check one, Check two — " in dimmed


def test_grid_header_styles_repo_bold_rest_dim():
    assert _grid().splitlines()[0] == (
        "shop · codex/gpt-5 · 14 checks · 9 calls · 45s")
    assert _grid(color=True).splitlines()[0] == (
        "\x1b[1mshop\x1b[0m"
        "\x1b[2m · codex/gpt-5 · 14 checks · 9 calls · 45s\x1b[0m")


def test_grid_styled_cells_runs_marks_gaps_and_next():
    text = _grid(color=True)

    # Styling never changes the layout: one table, like the plain render.
    assert text.count("╭") == 1
    assert "runs \x1b[32m✓\x1b[0m" in text
    assert ("\x1b[31m✗\x1b[0m "
            "\x1b[1mapi · Fixture state isolation\x1b[0m") in text
    assert "\x1b[36m→\x1b[0m Build a per-test factory." in text
    assert ("\x1b[33m?\x1b[0m \x1b[1mapi: Test timing\x1b[0m"
            "\x1b[2m — \x1b[0m") in text
    assert "Next: \x1b[1mptest doctor --fix\x1b[0m" in text
    assert "\x1b[32m2 ✓\x1b[0m" in text
    assert "\x1b[31m2 ✗\x1b[0m" in text
    assert "\x1b[33m3 ?\x1b[0m" in text


def test_grid_two_projects_fit_in_60_columns_without_stacking():
    text = _grid(width=60)

    assert text.count("╭") == 1
    for line in text.splitlines():
        if line[:1] in ("╭", "│", "├", "╰"):
            assert len(line) <= 60, line


def test_grid_tally_omits_zeros_and_marks_na_dim():
    children = [
        {"scope": "api", "facts": _facts(),
         "rows": [_row("FIX-001", "satisfied", label="Ok check")],
         "findings": [], "limitations": []},
        {"scope": "web", "facts": _facts(project="web"),
         "rows": [_row("SEL-1", "not-applicable", label="Skipped check",
                       rationale="Skipped: no runner.")],
         "findings": [], "limitations": []},
        {"scope": "cli", "facts": _facts(project="cli"),
         "rows": [], "findings": [], "limitations": []},
    ]
    text = render_agent_assessment(
        children, _workspace("api", "web", "cli"),
        report_path="recommendations.md", publication_status="created",
        width=100, color=False, repo="shop", provider="codex/gpt-5",
        duration_s=3, calls=1)

    assert "1 ✓" in text
    assert "1 –" in text
    assert "0 ✓  0 ✗  0 ?" in text

    dimmed = render_agent_assessment(
        children, _workspace("api", "web", "cli"),
        report_path="recommendations.md", publication_status="created",
        width=100, color=True, repo="shop", provider="codex/gpt-5",
        duration_s=3, calls=1)
    assert "\x1b[32m1 ✓\x1b[0m" in dimmed
    assert "\x1b[2m1 –\x1b[0m" in dimmed


def test_grid_offline_renders_static_sections_in_grid_style():
    from ptest import contracts as C
    from ptest.doctor import RepositoryInspection, WorkspaceInspection

    def _report(path_prefix, code=None):
        findings = ()
        if code is not None:
            findings = (C.Finding(
                code=code, severity="high", confidence="medium",
                path=f"{path_prefix}/tests/cache_test.py", line=2,
                evidence_type="static-pattern", consequence="consequence",
                remediation="remediation", verification="verification"),)
        return C.DoctorReport(
            scope=(), readiness=(
                C.Readiness(area="execution", state="unknown", reasons=()),
                C.Readiness(area="parallel", state="unknown", reasons=()),
                C.Readiness(area="selection", state="blocked", reasons=()),
                C.Readiness(area="timing", state="unknown", reasons=()),
            ), findings=findings, limits=C.DEFAULT_SCAN_LIMITS,
            usage=C.ScanUsage(entries=3, files=1, file_bytes=10,
                              total_bytes=10, findings=len(findings),
                              output_bytes=10, elapsed_s=0.0, skipped=0,
                              truncated=False),
            limitations=(),
        )

    api_report = _report("api", "cache.global-flush")
    workspace = WorkspaceInspection(
        scope=(), repositories=(
            RepositoryInspection(declaration="api", local_scope=None,
                                 report=api_report, config_problem=None),
            RepositoryInspection(declaration="web", local_scope=None,
                                 report=_report("web"), config_problem=None),
        ), aggregate=C.DoctorReport(
            scope=(), readiness=(), findings=api_report.findings,
            limits=C.DEFAULT_SCAN_LIMITS,
            usage=C.ScanUsage(entries=6, files=2, file_bytes=20,
                              total_bytes=20, findings=1, output_bytes=20,
                              elapsed_s=0.0, skipped=0, truncated=False),
            limitations=(),
        ),
    )
    text = render_agent_assessment(
        _two_projects(), workspace, report_path="recommendations.md",
        publication_status="skipped", width=100, color=False,
        repo="shop", provider="offline", duration_s=3, calls=0)

    assert "Readiness" in text
    assert "Scan coverage: complete; 2 files inspected." in text
    assert "api: execution unknown · parallel unknown" in text
    assert "Static findings" in text
    assert "Findings: 1 total (1 high)" in text
    assert ("- [api] high cache.global-flush: 1 finding "
            "(e.g. api/tests/cache_test.py:2)") in text
    assert "Worksheet" in text
    assert "FIX-001" in text and "review not yet performed" in text
    assert (text.index("╯") < text.index("Readiness")
            < text.index("Static findings") < text.index("Worksheet")
            < text.index("Next:"))
    for line in text.splitlines():
        assert len(line) <= 100, line

    online = render_agent_assessment(
        _two_projects(), workspace, report_path="recommendations.md",
        publication_status="created", width=100, color=False,
        repo="shop", provider="codex/gpt-5", duration_s=3, calls=1)
    assert "Readiness" not in online
    assert "Static findings" not in online
    assert "Worksheet" not in online


def test_grid_offline_renders_diagnostics_and_limitations():
    from ptest import contracts as C
    from ptest.doctor import RepositoryInspection, WorkspaceInspection

    report = C.DoctorReport(
        scope=(), readiness=(), findings=(), limits=C.DEFAULT_SCAN_LIMITS,
        usage=C.ScanUsage(entries=6, files=2, file_bytes=20,
                          total_bytes=20, findings=0, output_bytes=20,
                          elapsed_s=0.0, skipped=1, truncated=True),
        limitations=(),
    )
    aggregate = C.DoctorReport(
        scope=(), readiness=(), findings=(), limits=C.DEFAULT_SCAN_LIMITS,
        usage=C.ScanUsage(entries=6, files=2, file_bytes=20,
                          total_bytes=20, findings=0, output_bytes=20,
                          elapsed_s=0.0, skipped=1, truncated=True),
        limitations=(
            C.Reason(code="static-evidence-insufficient",
                     message="Only declared children were inspected."),
            C.Reason(code="static-evidence-insufficient",
                     message="Only declared children were inspected."),
        ),
    )
    workspace = WorkspaceInspection(
        scope=(), repositories=(
            RepositoryInspection(
                declaration="api", local_scope=None, report=report,
                config_problem=C.Problem(
                    code="invalid-config", message="bad runner",
                    phase="config")),
            RepositoryInspection(declaration="web", local_scope=None,
                                 report=report, config_problem=None),
        ), aggregate=aggregate,
    )
    text = render_agent_assessment(
        _two_projects(), workspace, report_path="recommendations.md",
        publication_status="skipped", width=100, color=False,
        repo="shop", provider="offline", duration_s=3, calls=0)

    assert "Diagnostics" in text
    assert "- api: invalid-config: bad runner" in text
    assert "Limitations" in text
    assert "Scan coverage: incomplete; 2 files inspected, 1 entry skipped." in text
    assert ("Only declared children were inspected. "
            "(2 occurrences)") in " ".join(text.split())
    assert (text.index("Worksheet") < text.index("Diagnostics")
            < text.index("Limitations") < text.index("Next:"))

    online = render_agent_assessment(
        _two_projects(), workspace, report_path="recommendations.md",
        publication_status="created", width=100, color=False,
        repo="shop", provider="codex/gpt-5", duration_s=3, calls=1)
    assert "Diagnostics" not in online
    assert "Limitations" not in online


def test_grid_missing_cell_renders_blank_without_breaking_borders():
    children = _two_projects()
    del children[1]["rows"][0]  # web has no FIX-001 verdict
    text = render_agent_assessment(
        children, _workspace("api", "web"),
        report_path="recommendations.md", publication_status="created",
        width=100, color=False, repo="shop", provider="codex/gpt-5",
        duration_s=45, calls=9)

    line = next(item for item in text.splitlines()
                if "Test data factories" in item)
    assert line.count("│") == 4, line
    assert line.rstrip().endswith("│")
