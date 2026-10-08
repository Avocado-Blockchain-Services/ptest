"""T2 doctor Test policy facts: gate/branch/omit/pragma/vitest/instruction facts.

Strict TDD: this file was written before ``src/ptest/policy_facts.py`` and
failed at collection (ImportError) until the module existed.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from ptest import agent_rules
from ptest import config as config_api
from ptest import contracts as C
from support import write_file, write_ptest_toml

PID = "ab" * 16

HAS_BARRIER = hasattr(agent_rules, "coverage_instruction_lines")
needs_barrier = pytest.mark.skipif(
    not HAS_BARRIER,
    reason="barrier not yet merged; orchestrator re-runs post-merge",
)


def _repo(root: Path, *, kind="pytest", args=(), full_args=(),
          test_roots=("tests",), files=None):
    root.mkdir(parents=True, exist_ok=True)
    write_ptest_toml(root, kind=kind, launcher=("pytest",), args=list(args),
                     full_args=list(full_args), test_roots=test_roots,
                     workers=1, project_id=PID)
    for rel, text in (files or {}).items():
        write_file(root / rel, text)
    return root


def _resolution(root: Path):
    resolution = config_api.resolve_config(root)
    assert resolution.problem is None, resolution.problem
    assert resolution.config is not None
    return resolution


def _collect(root, **kwargs):
    from ptest import policy_facts
    return policy_facts.collect(_resolution(root))


def _project(report, name="."):
    matches = [item for item in report.projects if item.project == name]
    assert len(matches) == 1, [item.project for item in report.projects]
    return matches[0]


def _gate_map(project):
    return {gate.source: gate.value for gate in project.gates}


# ---- frozen shapes ----

def test_frozen_dataclass_shapes():
    from ptest import policy_facts
    gate = policy_facts.CoverageGate(source="s", value="85")
    assert (gate.source, gate.value) == ("s", "85")
    scan = policy_facts.InstructionScan()
    facts = policy_facts.ProjectPolicyFacts(project=".")
    assert facts.gates == () and facts.branch is False
    assert facts.omit == () and facts.omit_total == 0
    assert facts.pragma_complete is True
    assert facts.vitest_not_inspected is False
    report = policy_facts.PolicyReport(projects=(facts,))
    assert report.root_instructions is None
    assert report.policy_installed is False
    with pytest.raises(Exception):
        gate.source = "other"


# ---- gate detection, table-driven over every D12 source ----

@pytest.mark.parametrize("config_file,section", [
    ("pyproject.toml", "[tool.coverage.report]\nfail_under = 85\n"),
    ("pyproject.toml", "[tool.coverage.report]\nfail_under = 85.5\n"),
    (".coveragerc", "[report]\nfail_under = 80\n"),
    ("setup.cfg", "[coverage:report]\nfail_under = 75\n"),
    ("tox.ini", "[coverage:report]\nfail_under = 70\n"),
])
def test_coverage_py_gate_sources(tmp_path, config_file, section):
    files = {}
    if config_file == "pyproject.toml":
        files[config_file] = section
    else:
        files[config_file] = section
    _repo(tmp_path, files=files)
    project = _project(_collect(tmp_path))
    assert len(project.gates) == 1
    assert "fail_under" in project.gates[0].source
    assert config_file in project.gates[0].source


@pytest.mark.parametrize("argv,expected", [
    (["--cov-fail-under=85"], "85"),
    (["--cov-fail-under", "85"], "85"),
    (["--cov-fail-under=84.5"], "84.5"),
])
def test_runner_args_gate_forms(tmp_path, argv, expected):
    _repo(tmp_path, args=argv)
    project = _project(_collect(tmp_path))
    assert _gate_map(project) == {
        "[runner] args --cov-fail-under": expected}


def test_runner_full_args_gate(tmp_path):
    _repo(tmp_path, full_args=["--cov-fail-under=77"])
    project = _project(_collect(tmp_path))
    assert _gate_map(project) == {
        "[runner] full_args --cov-fail-under": "77"}


@pytest.mark.parametrize("addopts_file,addopts", [
    ("pyproject.toml", "[tool.pytest.ini_options]\naddopts = \"--cov-fail-under=66\"\n"),
    ("pytest.ini", "[pytest]\naddopts = --cov-fail-under 66\n"),
    ("setup.cfg", "[tool:pytest]\naddopts = --cov-fail-under=65\n"),
    ("tox.ini", "[pytest]\naddopts = --cov-fail-under=64\n"),
])
def test_pytest_addopts_string_gate(tmp_path, addopts_file, addopts):
    _repo(tmp_path, files={addopts_file: addopts})
    project = _project(_collect(tmp_path))
    assert len(project.gates) == 1
    value = next(iter(_gate_map(project).values()))
    assert value in {"66", "65", "64"}


def test_pytest_addopts_list_gate(tmp_path):
    _repo(tmp_path, files={
        "pyproject.toml":
        "[tool.pytest.ini_options]\naddopts = [\"--cov-fail-under=63\"]\n"})
    project = _project(_collect(tmp_path))
    assert "63" in _gate_map(project).values()


def test_no_gate_found(tmp_path):
    _repo(tmp_path)
    project = _project(_collect(tmp_path))
    assert project.gates == ()
    assert project.branch is False


def test_multiple_sources_get_precedence_note(tmp_path):
    _repo(tmp_path, args=["--cov-fail-under=85"], files={
        "pyproject.toml": "[tool.coverage.report]\nfail_under = 80\n"})
    project = _project(_collect(tmp_path))
    assert len(project.gates) == 2
    assert any("reads only the first" in note for note in project.notes)


def test_single_source_has_no_precedence_note(tmp_path):
    _repo(tmp_path, files={
        "pyproject.toml": "[tool.coverage.report]\nfail_under = 80\n"})
    project = _project(_collect(tmp_path))
    assert not any("reads only the first" in note
                   for note in project.notes)


# ---- branch detection ----

@pytest.mark.parametrize("branch_value,expected", [
    ('"true"', True), ('"True"', True), ('"1"', True), ('"yes"', True),
    ('"on"', True), ('"false"', False), ('"off"', False), ('"0"', False),
    ('"no"', False),
])
def test_coverage_py_branch_spellings(tmp_path, branch_value, expected):
    # Quoted: TOML bare words other than true/false do not parse, so the
    # word spellings only reach coverage.py as strings.
    _repo(tmp_path, files={
        "pyproject.toml": f"[tool.coverage.run]\nbranch = {branch_value}\n"})
    project = _project(_collect(tmp_path))
    assert project.branch is expected


def test_toml_native_bool_branch(tmp_path):
    _repo(tmp_path, files={
        "pyproject.toml":
        "[tool.coverage.report]\nfail_under = 85\n"
        "[tool.coverage.run]\nbranch = true\n"})
    project = _project(_collect(tmp_path))
    assert project.branch is True
    assert project.branch_sources == (
        "pyproject.toml [tool.coverage.run] branch",)


def test_cov_branch_flag(tmp_path):
    _repo(tmp_path, args=["--cov-branch"])
    project = _project(_collect(tmp_path))
    assert project.branch is True
    assert "[runner] args --cov-branch" in project.branch_sources


# ---- omit patterns and bounds ----

def test_omit_patterns_reported(tmp_path):
    _repo(tmp_path, files={
        "pyproject.toml":
        "[tool.coverage.run]\nomit = [\"src/generated/*\", \"tests/*\"]\n"})
    project = _project(_collect(tmp_path))
    assert project.omit == ("src/generated/*", "tests/*")
    assert project.omit_total == 2


def test_omit_bounds(tmp_path):
    patterns = [f"p{i:03d}/" + "x" * 200 for i in range(25)]
    body = "omit = [\n" + "".join(f'  "{item}",\n' for item in patterns) + "]\n"
    _repo(tmp_path, files={"pyproject.toml": "[tool.coverage.run]\n" + body})
    project = _project(_collect(tmp_path))
    assert project.omit_total == 25
    assert len(project.omit) == 20
    assert all(len(item) <= 120 for item in project.omit)


# ---- pragma counts ----

def _src(root: Path, rel: str, text: str):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_pragma_count_and_test_root_exclusion(tmp_path):
    _repo(tmp_path)
    _src(tmp_path, "src/a.py", "x = 1  # pragma: no cover\ny = 2\n")
    _src(tmp_path, "src/b.py", "# PRAGMA:  NO   COVER\n")
    _src(tmp_path, "tests/test_a.py", "z = 3  # pragma: no cover\n")
    project = _project(_collect(tmp_path))
    assert project.pragma_count == 2
    assert project.pragma_complete is True


def test_pragma_nested_test_root_excludes_only_its_subtree(tmp_path):
    _repo(tmp_path, test_roots=("app/tests",))
    _src(tmp_path, "app/core.py", "x = 1  # pragma: no cover\n")
    _src(tmp_path, "app/tests/test_a.py", "z = 3  # pragma: no cover\n")
    _src(tmp_path, "tests2/b.py", "w = 4  # pragma: no cover\n")
    project = _project(_collect(tmp_path))
    assert project.pragma_count == 2
    assert project.pragma_complete is True


def test_pragma_skips_vendored_dirs_and_symlinks(tmp_path):
    _repo(tmp_path)
    _src(tmp_path, "src/a.py", "x = 1  # pragma: no cover\n")
    _src(tmp_path, ".venv/lib.py", "x = 1  # pragma: no cover\n")
    _src(tmp_path, "node_modules/x.py", "x = 1  # pragma: no cover\n")
    _src(tmp_path, "build/x.py", "x = 1  # pragma: no cover\n")
    target = tmp_path / "src" / "a.py"
    try:
        (tmp_path / "src" / "linked.py").symlink_to(target)
    except OSError:
        pass
    project = _project(_collect(tmp_path))
    assert project.pragma_count == 1


def test_pragma_fifo_never_blocks_scan(tmp_path):
    _repo(tmp_path)
    _src(tmp_path, "src/a.py", "x = 1  # pragma: no cover\n")
    fifo = tmp_path / "src" / "hung.py"
    try:
        os.mkfifo(fifo)
    except (AttributeError, OSError):
        pytest.skip("FIFO not supported here")
    project = _project(_collect(tmp_path))
    assert project.pragma_count == 1


def test_pragma_oversized_file_makes_count_partial(tmp_path):
    _repo(tmp_path)
    big = tmp_path / "src" / "big.py"
    big.parent.mkdir(parents=True, exist_ok=True)
    big.write_bytes(b"# pragma: no cover\n" + b"x" * (512 * 1024 + 8))
    project = _project(_collect(tmp_path))
    assert project.pragma_complete is False


# ---- vitest: never opened ----

def test_vitest_runner_reports_not_inspected(tmp_path):
    _repo(tmp_path, kind="vitest")
    project = _project(_collect(tmp_path))
    assert project.vitest_not_inspected is True


def test_vitest_config_never_opened(tmp_path):
    _repo(tmp_path)
    fifo = tmp_path / "vitest.config.ts"
    try:
        os.mkfifo(fifo)
    except (AttributeError, OSError):
        fifo.write_bytes(b"")
        os.chmod(fifo, 0o000)
    project = _project(_collect(tmp_path))
    assert project.vitest_not_inspected is True


def test_no_vitest_config_no_line(tmp_path):
    _repo(tmp_path)
    project = _project(_collect(tmp_path))
    assert project.vitest_not_inspected is False


# ---- instruction lines: barrier scanner wiring ----

def _fake_scan(lines):
    def _scan(directory):
        from ptest import policy_facts
        items = tuple(SimpleNamespace(path=name, line=number, text=text)
                      for name, number, text in lines)
        return policy_facts.InstructionScan(lines=items)
    return _scan


def test_instruction_scan_wiring(monkeypatch, tmp_path):
    from ptest import policy_facts
    _repo(tmp_path)
    monkeypatch.setattr(policy_facts, "coverage_instruction_lines",
                        _fake_scan(
                            [("AGENTS.md", 12,
                              "Keep coverage above 90%.")]))
    project = _project(_collect(tmp_path))
    assert [(item.path, item.line, item.text) for item in
            project.instructions.lines] == [
        ("AGENTS.md", 12, "Keep coverage above 90%.")]


def test_raising_scanner_degrades_to_note(monkeypatch, tmp_path):
    from ptest import policy_facts
    _repo(tmp_path)

    def _boom(directory):
        raise OSError("denied")

    monkeypatch.setattr(policy_facts, "coverage_instruction_lines", _boom)
    project = _project(_collect(tmp_path))
    assert project.instructions.lines == ()
    assert any("not inspected" in note for note in project.notes)


@needs_barrier
def test_real_barrier_scan_finds_percent_and_ignores_managed_block(tmp_path):
    _repo(tmp_path, files={
        "AGENTS.md":
        "Keep coverage above 90%.\n"
        "<!-- ptest-agent-rules:start -->\n"
        "Before running or changing tests, read `docs/ptest-agent.md`.\n"
        "Keep coverage above 90%.\n"
        "<!-- ptest-agent-rules:end -->\n"})
    project = _project(_collect(tmp_path))
    assert [(item.path, item.line) for item in
            project.instructions.lines] == [("AGENTS.md", 1)]


# ---- never raises, writes nothing ----

def test_unreadable_configs_become_notes_not_exceptions(tmp_path):
    _repo(tmp_path, files={
        "pyproject.toml": "[tool.coverage.report\nfail_under = \n",
        ".coveragerc": "[report]\nfail_under = 80\n"})
    (tmp_path / ".coveragerc").write_bytes(b"\xff\xfe\x00bad")
    project = _project(_collect(tmp_path))
    assert project.gates == ()
    assert any("not inspected" in note or "could not be parsed" in note
               for note in project.notes)


def test_symlink_and_directory_configs_do_not_raise(tmp_path):
    _repo(tmp_path)
    target = tmp_path / "real-coveragerc"
    target.write_text("[report]\nfail_under = 80\n", encoding="utf-8")
    (tmp_path / ".coveragerc").symlink_to(target)
    (tmp_path / "tox.ini").mkdir()
    project = _project(_collect(tmp_path))
    assert project.gates == ()


def test_percent_in_ini_value_keeps_other_facts(tmp_path):
    from ptest import policy_render
    _repo(tmp_path, files={
        "pytest.ini": "[pytest]\naddopts = --cov-fail-under=90 "
                      "--log-cli-format=\"%(asctime)s %(message)s\"\n",
        ".coveragerc": "[report]\nfail_under = 80\n"})
    _src(tmp_path, "src/a.py", "x = 1  # pragma: no cover\n")
    report = _collect(tmp_path)
    project = _project(report)
    assert _gate_map(project) == {
        "pytest.ini addopts --cov-fail-under": "90",
        ".coveragerc [report] fail_under": "80",
    }
    assert project.pragma_count == 1
    # Doctor-facing symptom: the block must show the gates, never
    # "coverage gate: none found".
    text = policy_render.render_terminal(report)
    assert "coverage gate: 90" in text
    assert "coverage gate: 80" in text
    assert "none found" not in text


def test_bad_shlex_addopts_becomes_note(tmp_path):
    _repo(tmp_path, files={"pytest.ini": "[pytest]\naddopts = '--cov  \n"})
    project = _project(_collect(tmp_path))
    assert project.gates == ()
    assert any("addopts" in note for note in project.notes)


def test_broken_tree_still_returns_facts(monkeypatch, tmp_path):
    _repo(tmp_path)

    def _denied(*args, **kwargs):
        raise OSError("denied")

    # scandir failure must degrade, never raise:
    monkeypatch.setattr("os.scandir", _denied)
    project = _project(_collect(tmp_path))
    assert project.project == "."


def test_collect_writes_nothing(tmp_path):
    _repo(tmp_path, files={
        "pyproject.toml": "[tool.coverage.report]\nfail_under = 85\n"})
    before = {path: (path.stat().st_mtime_ns, path.read_bytes())
              for path in tmp_path.rglob("*") if path.is_file()}
    _collect(tmp_path)
    after = {path: (path.stat().st_mtime_ns, path.read_bytes())
             for path in tmp_path.rglob("*") if path.is_file()}
    assert before == after


# ---- policy installed flag ----

def test_policy_installed_regular_file(tmp_path):
    _repo(tmp_path)
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "ptest-test-policy.md").write_text("policy\n", encoding="utf-8")
    assert _collect(tmp_path).policy_installed is True


def test_policy_installed_absent_or_symlink(tmp_path):
    _repo(tmp_path)
    assert _collect(tmp_path).policy_installed is False
    target = tmp_path / "real-policy.md"
    target.write_text("policy\n", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "ptest-test-policy.md").symlink_to(target)
    assert _collect(tmp_path).policy_installed is False


# ---- standalone vs monorepo ----

def test_standalone_has_dot_project_and_no_root_scan(tmp_path):
    _repo(tmp_path)
    report = _collect(tmp_path)
    assert [item.project for item in report.projects] == ["."]
    assert report.root_instructions is None


def test_monorepo_children_and_root_scan(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / ".ptest.toml").write_text(
        'version = 2\n[monorepo]\nchildren = ["api", "web"]\n',
        encoding="utf-8")
    for name in ("api", "web"):
        child = tmp_path / name
        write_ptest_toml(child, kind="command", launcher=("true",),
                         args=(), full_args=(), test_roots=(),
                         workers=1, project_id=PID)
    (tmp_path / "api" / "pyproject.toml").write_text(
        "[tool.coverage.report]\nfail_under = 88\n", encoding="utf-8")
    from ptest import config as config_api, policy_facts
    resolution = config_api.resolve_config(tmp_path)
    report = policy_facts.collect(resolution)
    assert [item.project for item in report.projects] == ["api", "web"]
    assert report.root_instructions is not None
    assert _project(report, "api").gates[0].value == "88"
    assert _project(report, "web").gates == ()


def test_config_none_still_collects_file_facts(tmp_path):
    from ptest import policy_facts
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "pyproject.toml").write_text(
        "[tool.coverage.report]\nfail_under = 85\n", encoding="utf-8")
    resolution = C.ConfigResolution(root=tmp_path, path=None, config=None,
                                    provenance=(), warnings=(), problem=None)
    report = policy_facts.collect(resolution)
    assert [item.project for item in report.projects] == ["."]
    assert _gate_map(_project(report)) != {}


def test_symlinked_pytest_ini_is_noted_not_silent(tmp_path):
    _repo(tmp_path)
    target = tmp_path / "real.ini"
    target.write_text("[pytest]\naddopts = --cov-fail-under=90\n",
                      encoding="utf-8")
    (tmp_path / "pytest.ini").symlink_to(target)
    project = _project(_collect(tmp_path))
    assert project.gates == ()
    assert any("pytest.ini" in note for note in project.notes)


def test_unparseable_pytest_ini_is_noted_not_silent(tmp_path):
    _repo(tmp_path, files={
        "pytest.ini": "no section header\naddopts = --cov-fail-under=90\n"})
    project = _project(_collect(tmp_path))
    assert project.gates == ()
    assert any("pytest.ini" in note for note in project.notes)


def test_pytest_ini_takes_precedence_over_pyproject_addopts(tmp_path):
    _repo(tmp_path, files={
        "pytest.ini": "[pytest]\nminversion = 7\n",
        "pyproject.toml": "[tool.pytest.ini_options]\n"
                          "addopts = \"--cov-fail-under=66\"\n"})
    project = _project(_collect(tmp_path))
    assert project.gates == ()
