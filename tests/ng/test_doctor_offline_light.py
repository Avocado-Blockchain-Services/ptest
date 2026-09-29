"""Offline static doctor light path (T1).

The offline static packet must never build the candidate text pool or run
ranking: ``doctor --offline``, offline ``--json`` and init's declined-review
fallback produce their document without calling ``_candidate_text_pool``,
``review_evidence.rank_candidates``, ``review_evidence.item_source_chains``
or ``_resolve_tier4``.
"""
from __future__ import annotations

import json
import re
import sys
import time
from types import SimpleNamespace

import pytest

from ptest import agent_assessment
from ptest import cli as cli_module
from ptest import config as config_api
from ptest import contracts as C
from ptest import doctor
from ptest.cli import main, parse_argv
from support import write_file, write_ptest_toml

from factories_agents import doctor_assessment_project

_STANDALONE_PID = "ab" * 16
_V2_PIDS = {"api": "ab" * 16, "web": "cd" * 16}


def _write_v2(root, *, runner="command"):
    root.mkdir(parents=True, exist_ok=True)
    (root / ".ptest.toml").write_text(
        'version = 2\n[monorepo]\nchildren = ["api", "web"]\n',
        encoding="utf-8",
    )
    for name, pid in (("api", _V2_PIDS["api"]), ("web", _V2_PIDS["web"])):
        doctor_assessment_project(root / name, pid, runner=runner)


def _write_pytest_child(root, project_id, files):
    write_ptest_toml(root, kind="pytest", launcher=("pytest",), args=[],
                     full_args=(), test_roots=("tests",), workers=1,
                     project_id=project_id)
    for rel, text in files.items():
        write_file(root / rel, text)


def _resolve(root, scope=None):
    resolution = config_api.resolve_config(root)
    domain = cli_module.platform.domain_paths(None)
    parsed = parse_argv(
        ("doctor", "--offline")
        + (("--scope", scope) if scope is not None else ()))
    workspace = doctor.inspect_workspace(
        domain, resolution, cli_module._doctor_limits(parsed), scope)
    return parsed, resolution, domain, workspace


def _reference_static(monkeypatch):
    """Rewire build_static_packets to today's full path (the reference)."""
    real_build = agent_assessment.build_packets

    def fake(workspace, resolution, *args, **kwargs):
        return agent_assessment.StaticPackets(
            real_build(workspace, resolution), ())

    monkeypatch.setattr(
        agent_assessment, "build_static_packets", fake)


def _fail_static(monkeypatch):
    """Forbid every ranking/pool function the static path must not call."""
    monkeypatch.setattr(
        "ptest.review_evidence.rank_candidates",
        lambda *args, **kwargs: pytest.fail("static path ranked candidates"))
    monkeypatch.setattr(
        "ptest.review_evidence.item_source_chains",
        lambda *args, **kwargs: pytest.fail(
            "static path built item source chains"))
    monkeypatch.setattr(
        agent_assessment, "_candidate_text_pool",
        lambda *args, **kwargs: pytest.fail("static path built text pool"))
    monkeypatch.setattr(
        agent_assessment, "_resolve_tier4",
        lambda *args, **kwargs: pytest.fail("static path resolved tier4"))


def _mask_duration(text):
    return re.sub(r"\b\d+m\d{2}s\b|\b\d+s\b", "DUR", text)


def _children_without_sha(document):
    children = []
    for child in document.data["children"]:
        child = dict(child)
        child["packet_sha256"] = "0" * 64
        children.append(child)
    return children


# --- negative contract: the static path never ranks -------------------------


def test_offline_grid_never_calls_ranking_pool(tmp_path, monkeypatch, capsys):
    root = tmp_path / "standalone"
    doctor_assessment_project(root, _STANDALONE_PID)
    monkeypatch.chdir(root)
    _fail_static(monkeypatch)
    assert main(("doctor", "--offline")) == 0
    out = capsys.readouterr()
    assert "offline · " in out.out and "0 calls" in out.out


def test_offline_json_never_calls_ranking_pool(tmp_path, monkeypatch, capsys):
    root = tmp_path / "standalone"
    doctor_assessment_project(root, _STANDALONE_PID)
    monkeypatch.chdir(root)
    _fail_static(monkeypatch)
    assert main(("doctor", "--offline", "--json")) == 0
    out = capsys.readouterr()
    public = C.decode_public_document(out.out.encode("utf-8"))
    assert public.error is None
    assert public.data["provider"]["name"] == "offline"


def test_declined_review_fallback_never_calls_ranking_pool(
        tmp_path, monkeypatch, capsys):
    root = tmp_path / "standalone"
    doctor_assessment_project(root, _STANDALONE_PID)
    monkeypatch.chdir(root)
    _fail_static(monkeypatch)
    parsed, resolution, domain, _workspace = _resolve(root)
    assert cli_module._declined_review_output(
        parsed, resolution, domain) is True
    out = capsys.readouterr()
    assert "offline · " in out.out


# --- identity: static output matches the full path --------------------------

def _run_json(args):
    assert main(args) == 0


def _json_children_and_limitations(out):
    public = C.decode_public_document(out.encode("utf-8"))
    assert public.error is None
    return public, _children_without_sha(public)


def _identity_case(root, monkeypatch, capsys, scope=None):
    """Reference (full-path) vs static JSON and grid must match."""
    monkeypatch.chdir(root)
    extra = () if scope is None else ("--scope", scope)
    _run_json(("doctor", "--offline", "--json") + extra)
    static = capsys.readouterr()
    _run_json(("doctor", "--offline") + extra)
    static_grid = capsys.readouterr()
    with monkeypatch.context() as patch:
        _reference_static(patch)
        _run_json(("doctor", "--offline", "--json") + extra)
        reference = capsys.readouterr()
        _run_json(("doctor", "--offline") + extra)
        reference_grid = capsys.readouterr()
    ref_public, ref_children = _json_children_and_limitations(reference.out)
    static_public, static_children = _json_children_and_limitations(static.out)
    assert static_children == ref_children
    assert static_public.data["limitations"] == ref_public.data["limitations"]
    assert (static_public.data["publication"]
            == ref_public.data["publication"])
    assert static_grid.err == reference_grid.err == ""
    assert (_mask_duration(static_grid.out)
            == _mask_duration(reference_grid.out))


def test_identity_standalone(tmp_path, monkeypatch, capsys):
    root = tmp_path / "standalone"
    doctor_assessment_project(root, _STANDALONE_PID)
    _identity_case(root, monkeypatch, capsys)


def test_identity_v2_monorepo(tmp_path, monkeypatch, capsys):
    root = tmp_path / "mono"
    _write_v2(root)
    _identity_case(root, monkeypatch, capsys)


def test_identity_pytest_with_empty_inits_and_oversize_file(
        tmp_path, monkeypatch, capsys):
    root = tmp_path / "pytest-big"
    big = "x = 1\n" * 20000
    assert len(big.encode("utf-8")) > agent_assessment.MAX_BYTES_PER_FILE
    _write_pytest_child(root, _STANDALONE_PID, {
        "tests/__init__.py": "",
        "src/__init__.py": "",
        "tests/test_app.py": "def test_app():\n    assert True\n",
        "src/big.py": big,
    })
    _identity_case(root, monkeypatch, capsys)


def test_identity_setup_py_marker(tmp_path, monkeypatch, capsys):
    root = tmp_path / "setup-marker"
    _write_pytest_child(root, _STANDALONE_PID, {
        "setup.py": "from setuptools import setup\nsetup(name='demo')\n",
        "src/app.py": "VALUE = 1\n",
        "tests/test_app.py": "def test_app():\n    assert True\n",
    })
    _identity_case(root, monkeypatch, capsys)


def test_identity_node_package_json(tmp_path, monkeypatch, capsys):
    root = tmp_path / "node"
    write_ptest_toml(root, kind="command", launcher=("echo",), args=["hi"],
                     full_args=(), test_roots=(), workers=1,
                     project_id=_STANDALONE_PID)
    write_file(root / "package.json",
               json.dumps({"name": "demo", "version": "1.0.0"}) + "\n")
    write_file(root / "src" / "index.js",
               "export function value() { return 1; }\n")
    _identity_case(root, monkeypatch, capsys)


def test_identity_scoped_monorepo_tests_dir(tmp_path, monkeypatch, capsys):
    root = tmp_path / "scoped-mono"
    _write_v2(root, runner="pytest")
    for child in ("api", "web"):
        write_file(root / child / "tests" / "__init__.py", "")
        write_file(root / child / "tests" / "conftest.py",
                   "import pytest\n\n@pytest.fixture\n"
                   "def thing():\n    return 1\n")
        write_file(root / child / "tests" / "test_thing.py",
                   "def test_thing(thing):\n    assert thing == 1\n")
    _identity_case(root, monkeypatch, capsys, scope="api/tests")
    parsed, resolution, _domain, workspace = _resolve(root, scope="api/tests")
    result = agent_assessment.build_static_packets(workspace, resolution)
    api = next(packet for packet in result.packets
               if packet.scope == "api/tests")
    assert api.excerpts == ()
    assert api._projected_admitted > 0


def test_identity_scoped_standalone_src_pkg(tmp_path, monkeypatch, capsys):
    root = tmp_path / "scoped-standalone"
    _write_pytest_child(root, _STANDALONE_PID, {
        "src/pkg/__init__.py": "",
        "src/pkg/mod.py": "VALUE = 1\n",
    })
    _identity_case(root, monkeypatch, capsys, scope="src/pkg")
    _parsed, resolution, _domain, workspace = _resolve(root, scope="src/pkg")
    result = agent_assessment.build_static_packets(workspace, resolution)
    assert len(result.packets) == 1
    assert result.packets[0].excerpts == ()
    assert result.packets[0]._projected_admitted > 0


def test_identity_uninitialized_repo(tmp_path, monkeypatch, capsys):
    root = tmp_path / "uninit"
    write_file(root / "src" / "app.py", "VALUE = 1\n")
    write_file(root / "tests" / "test_app.py",
               "def test_app():\n    assert True\n")
    _identity_case(root, monkeypatch, capsys)
    _parsed, resolution, _domain, workspace = _resolve(root)
    result = agent_assessment.build_static_packets(workspace, resolution)
    assert len(result.packets) == 1
    assert result.packets[0].excerpts == ()
    assert result.packets[0]._projected_admitted > 0


# --- documented differences -------------------------------------------------

def _run_pair(root, monkeypatch, capsys, scope=None):
    """Return (static_public, reference_public) JSON documents."""
    monkeypatch.chdir(root)
    extra = () if scope is None else ("--scope", scope)
    _run_json(("doctor", "--offline", "--json") + extra)
    static_out = capsys.readouterr().out
    with monkeypatch.context() as patch:
        _reference_static(patch)
        _run_json(("doctor", "--offline", "--json") + extra)
        reference_out = capsys.readouterr().out
    static_public = C.decode_public_document(static_out.encode("utf-8"))
    reference_public = C.decode_public_document(
        reference_out.encode("utf-8"))
    assert static_public.error is None and reference_public.error is None
    return static_public, reference_public


def _partial_evidence_for(limitations, scope):
    return [entry for entry in limitations
            if entry["code"] == "partial-evidence"
            and entry["paths"] == [scope]]


def test_cap_binding_only_changes_counts_and_sha(
        tmp_path, monkeypatch, capsys):
    root = tmp_path / "capped"
    files = {"tests/__init__.py": ""}
    for index in range(70):
        files[f"src/mod_{index:02d}.py"] = f"VALUE_{index} = {index}\n"
    _write_pytest_child(root, _STANDALONE_PID, files)
    static_public, reference_public = _run_pair(root, monkeypatch, capsys)
    assert len(static_public.data["children"]) == 1
    static_child = static_public.data["children"][0]
    reference_child = reference_public.data["children"][0]
    assert static_child["rows"] == reference_child["rows"]
    static_limits = _partial_evidence_for(
        static_child["limitations"], static_child["scope"])
    reference_limits = _partial_evidence_for(
        reference_child["limitations"], reference_child["scope"])
    assert len(static_limits) == len(reference_limits) == 1
    assert static_limits[0]["message"].startswith("Evidence limits: ")
    assert reference_limits[0]["message"].startswith("Evidence limits: ")
    static_rest = dict(static_child)
    reference_rest = dict(reference_child)
    for rest in (static_rest, reference_rest):
        rest.pop("packet_sha256")
        rest.pop("limitations")
    assert static_rest == reference_rest


def test_no_evidence_scope_only_differs_in_partial_evidence(
        tmp_path, monkeypatch, capsys):
    root = tmp_path / "no-evidence"
    _write_v2(root, runner="pytest")
    for child in ("api", "web"):
        write_file(root / child / "tests" / "__init__.py", "")
        write_file(root / child / "tests" / "test_only.py",
                   "def test_only():\n    assert True\n")
    static_public, reference_public = _run_pair(
        root, monkeypatch, capsys, scope="api/tests")
    static_child = next(child for child in static_public.data["children"]
                        if child["scope"] == "api/tests")
    static_limits = _partial_evidence_for(
        static_child["limitations"], "api/tests")
    assert len(static_limits) == 1
    assert static_limits[0]["message"].startswith(
        "No source files were admitted to this project packet. ")
    reference_child = next(
        child for child in reference_public.data["children"]
        if child["scope"] == "api/tests")
    assert static_child["rows"] == reference_child["rows"]

    def scrub(public):
        data = json.loads(json.dumps(public.data))
        for child in data["children"]:
            child["packet_sha256"] = "0" * 64
            child["limitations"] = [
                entry for entry in child["limitations"]
                if not (entry["code"] == "partial-evidence"
                        and entry["paths"] == ["api/tests"])]
        data["limitations"] = [
            entry for entry in data["limitations"]
            if not (entry["code"] == "partial-evidence"
                    and entry["paths"] == ["api/tests"])]
        return data

    assert scrub(static_public) == scrub(reference_public)


# --- read contract: admission reads nothing but core and markers ------------

def test_static_admission_reads_no_plain_sources(tmp_path, monkeypatch, capsys):
    root = tmp_path / "reads"
    _write_pytest_child(root, _STANDALONE_PID, {
        "setup.py": "from setuptools import setup\nsetup(name='demo')\n",
        "src/app/service.py": "VALUE = 1\n",
        "tests/test_app.py": "def test_app():\n    assert True\n",
    })
    monkeypatch.chdir(root)
    reads = []
    in_context = []
    real_collect = agent_assessment._collect_packet_context
    real_read = agent_assessment.read_regular

    def spy_collect(**kwargs):
        in_context.append(True)
        try:
            return real_collect(**kwargs)
        finally:
            in_context.pop()

    def spy_read(child_root, rel, limit):
        reads.append((str(rel), bool(in_context)))
        return real_read(child_root, rel, limit)

    monkeypatch.setattr(
        agent_assessment, "_collect_packet_context", spy_collect)
    monkeypatch.setattr(agent_assessment, "read_regular", spy_read)
    assert main(("doctor", "--offline")) == 0
    capsys.readouterr()
    admission_reads = {rel for rel, contextual in reads if not contextual}
    assert "src/app/service.py" not in admission_reads
    _parsed, resolution, _domain, workspace = _resolve(root)
    result = agent_assessment.build_static_packets(workspace, resolution)
    assert len(result.packets) == 1
    packet = result.packets[0]
    assert packet._inventory_sha256 is None
    assert packet._item_chains and all(
        chains == () for _digest, _item_id, chains in packet._item_chains)
    admitted = {excerpt.path for excerpt in packet.excerpts}
    assert admitted == {".ptest.toml", "setup.py"}


# --- deadline ----------------------------------------------------------------

def test_offline_deadline_zero_truncates_gracefully(
        tmp_path, monkeypatch, capsys):
    root = tmp_path / "deadline"
    _write_v2(root)
    monkeypatch.chdir(root)
    monkeypatch.setattr(cli_module, "_REVIEW_TOTAL_TIMEOUT_S", 0)
    assert main(("doctor", "--offline")) == 0
    grid = capsys.readouterr()
    assert "offline · " in grid.out
    assert main(("doctor", "--offline", "--json")) == 0
    out = capsys.readouterr()
    public = C.decode_public_document(out.out.encode("utf-8"))
    assert public.error is None
    message = cli_module._OFFLINE_DEADLINE_MESSAGE
    assert len(public.data["children"]) == 2
    for child in public.data["children"]:
        assert child["rows"] and all(
            row["status"] == "unknown" for row in child["rows"])
        assert child["score"] is None or child["score"]["percent"] == 0
        matches = [entry for entry in child["limitations"]
                   if entry["code"] == "partial-evidence"
                   and entry["message"] == message
                   and entry["paths"] == [child["scope"]]]
        assert len(matches) == 1
    top_matches = [entry for entry in public.data["limitations"]
                   if entry["code"] == "partial-evidence"
                   and entry["message"] == message]
    assert len(top_matches) == 2


def test_build_static_packets_past_deadline_expires_every_child(
        tmp_path, monkeypatch):
    root = tmp_path / "deadline-direct"
    _write_v2(root)
    _parsed, resolution, _domain, workspace = _resolve(root)
    result = agent_assessment.build_static_packets(
        workspace, resolution, deadline=time.monotonic() - 1)
    assert result.deadline_expired == ("api", "web")
    assert [packet.scope for packet in result.packets] == ["api", "web"]
    for packet in result.packets:
        assert packet.excerpts == ()
        assert packet.dependencies == ()
        assert packet.context is not None


def test_build_static_packets_propagates_non_timeout_problem(tmp_path):
    root = tmp_path / "unsafe-scope"
    write_ptest_toml(root, kind="command", launcher=("echo",), args=["hi"],
                     full_args=(), test_roots=(), workers=1,
                     project_id=_STANDALONE_PID)
    write_file(root / "only.txt", "hello\n")
    _parsed, resolution, domain, _workspace = _resolve(root)
    workspace = doctor.inspect_workspace(
        domain, resolution,
        cli_module._doctor_limits(parse_argv(("doctor", "--offline"))),
        "only.txt")
    with pytest.raises(C.Problem) as excinfo:
        agent_assessment.build_static_packets(
            workspace, resolution, deadline=time.monotonic() + 60)
    assert excinfo.value.code == "unsafe-path"


# --- progress ----------------------------------------------------------------

def _tty(monkeypatch):
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True)


def test_offline_progress_one_line_per_child_on_tty(
        tmp_path, monkeypatch, capsys):
    root = tmp_path / "progress"
    _write_v2(root)
    monkeypatch.chdir(root)
    _tty(monkeypatch)
    assert main(("doctor", "--offline")) == 0
    out = capsys.readouterr()
    lines = [line for line in out.err.splitlines()
             if line.startswith("ptest: doctor: inspecting ")]
    assert len(lines) == 2
    assert lines[0].startswith("ptest: doctor: inspecting api · ")
    assert lines[0].endswith(" files") or lines[0].endswith(" file")
    assert lines[1].startswith("ptest: doctor: inspecting web · ")


def test_offline_progress_dot_uses_repo_name_and_singular(
        tmp_path, monkeypatch, capsys):
    root = tmp_path / "solo-proj"
    write_ptest_toml(root, kind="command", launcher=("echo",), args=["hi"],
                     full_args=(), test_roots=(), workers=1,
                     project_id=_STANDALONE_PID)
    monkeypatch.chdir(root)
    _tty(monkeypatch)
    assert main(("doctor", "--offline")) == 0
    out = capsys.readouterr()
    lines = [line for line in out.err.splitlines()
             if line.startswith("ptest: doctor: inspecting ")]
    assert lines == ["ptest: doctor: inspecting solo-proj · 1 file"]


def test_offline_progress_silent_without_tty(
        tmp_path, monkeypatch, capsys):
    root = tmp_path / "quiet-term"
    _write_v2(root)
    monkeypatch.chdir(root)
    monkeypatch.setattr(sys.stderr, "isatty", lambda: False)
    assert main(("doctor", "--offline")) == 0
    assert capsys.readouterr().err == ""


def test_offline_progress_never_on_json_stdout(
        tmp_path, monkeypatch, capsys):
    root = tmp_path / "json-progress"
    _write_v2(root)
    monkeypatch.chdir(root)
    _tty(monkeypatch)
    assert main(("doctor", "--offline", "--json")) == 0
    out = capsys.readouterr()
    assert "inspecting" not in out.out
    C.decode_public_document(out.out.encode("utf-8"))


def test_offline_progress_sanitizes_control_characters(
        tmp_path, monkeypatch, capsys):
    parsed = parse_argv(("doctor", "--offline"))
    resolution = SimpleNamespace(root=tmp_path / "proj\x01name")
    _tty(monkeypatch)
    emit = cli_module._offline_progress(parsed, resolution)
    assert emit is not None
    emit("api\x7fchild", 2)
    err = capsys.readouterr().err
    assert "\x01" not in err and "\x7f" not in err
    assert err.startswith("ptest: doctor: inspecting ")


def test_offline_quiet_suppresses_progress_but_keeps_stdout(
        tmp_path, monkeypatch, capsys):
    root = tmp_path / "quiet-flag"
    _write_v2(root)
    monkeypatch.chdir(root)
    _tty(monkeypatch)
    assert main(("doctor", "--offline")) == 0
    loud = capsys.readouterr()
    assert main(("doctor", "--offline", "-q")) == 0
    quiet_short = capsys.readouterr()
    assert main(("doctor", "--offline", "--quiet")) == 0
    quiet_long = capsys.readouterr()
    assert quiet_short.err == quiet_long.err == ""
    assert (_mask_duration(quiet_short.out)
            == _mask_duration(loud.out)
            == _mask_duration(quiet_long.out))
    assert main(("doctor", "--offline", "--json", "-q")) == 0
    quiet_json = capsys.readouterr()
    assert main(("doctor", "--offline", "--json")) == 0
    loud_json = capsys.readouterr()
    assert quiet_json.out == loud_json.out


def test_doctor_quiet_parser_contract():
    assert parse_argv(("doctor", "--offline", "-q")).quiet is True
    assert parse_argv(
        ("doctor", "--offline", "--json", "--quiet")).quiet is True
    assert parse_argv(("doctor", "--offline")).quiet is False
    with pytest.raises(C.Problem) as excinfo:
        parse_argv(("doctor", "-q"))
    assert (excinfo.value.code == "invalid-config"
            and "--quiet requires --offline" in excinfo.value.message)
    with pytest.raises(C.Problem) as excinfo:
        parse_argv(("doctor", "--offline", "-q", "--quiet"))
    assert (excinfo.value.code == "invalid-config"
            and "option cannot be repeated" in excinfo.value.message)
    with pytest.raises(C.Problem) as excinfo:
        parse_argv(("doctor", "--dry-run", "-q"))
    assert (excinfo.value.code == "invalid-config"
            and "--quiet requires --offline" in excinfo.value.message)
    with pytest.raises(C.Problem) as excinfo:
        parse_argv(("doctor", "--fix", "-q"))
    assert (excinfo.value.code == "invalid-config"
            and "--fix takes no output, scope, or scan-limit options"
            in excinfo.value.message)
    with pytest.raises(C.Problem) as excinfo:
        parse_argv(("doctor", "--probe", "--scope", "tests/x.py", "-q"))
    assert (excinfo.value.code == "invalid-config"
            and "doctor probe cannot combine output modes"
            in excinfo.value.message)
    with pytest.raises(C.Problem) as excinfo:
        parse_argv(("init", "-q"))
    assert (excinfo.value.code == "invalid-config"
            and "unknown inspection option" in excinfo.value.message)


# --- online path unchanged ---------------------------------------------------

def test_online_packets_carry_no_projection_and_own_identity(
        tmp_path, monkeypatch):
    root = tmp_path / "online"
    doctor_assessment_project(root, _STANDALONE_PID)
    _parsed, resolution, _domain, workspace = _resolve(root)
    online = agent_assessment.build_packets(workspace, resolution)
    static = agent_assessment.build_static_packets(workspace, resolution)
    assert len(online) == len(static.packets) == 1
    assert online[0]._projected_admitted == 0
    assert (agent_assessment.packet_hash(online[0])
            == online[0].packet_sha256)
    assert online[0].packet_sha256 != static.packets[0].packet_sha256
    from dataclasses import replace
    assert (agent_assessment.packet_hash(
                replace(online[0], _projected_admitted=5))
            == online[0].packet_sha256)


def test_projected_admitted_field_validation():
    packet = agent_assessment.EvidencePacket(
        declaration=".", project_id=_STANDALONE_PID, scope=".",
        packet_sha256="0" * 64, excerpts=(), dependencies=(),
        runner_kind="command", excluded_count=0, truncated_count=0,
        file_count=0, byte_count=0)
    assert packet._projected_admitted == 0
    assert agent_assessment.packet_has_evidence(packet) is False
    assert agent_assessment.packet_has_evidence(
        SimpleNamespace(excerpts=(), _projected_admitted=2)) is True
    with pytest.raises(TypeError):
        agent_assessment.replace(packet, _projected_admitted=True)
    with pytest.raises(ValueError):
        agent_assessment.replace(packet, _projected_admitted=-1)
