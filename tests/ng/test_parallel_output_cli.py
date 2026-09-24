"""Wave-2 CLI wiring: parallel facts, init/doctor renderers, disclosure.

Integration through ``cli.main`` / ``case.invoke`` with no monkeypatching
of ptest modules. The only fakes are provider executables on a temporary
``PATH`` (the ``test_doctor_init_integration`` pattern) plus
``stdin.isatty``/``input`` where a TTY is needed. No network, no real
provider, no real claude/codex/opencode.

Seam note: T5 is the wave-2 barrier task. Tests that need names T1-T4
create (``executability.facts``/``parallel_request``, ``render_init_footer``,
``deterministic_items``, bridge qualification constants) are guarded by
explicit ``pytest.mark.skipif`` naming the missing merge piece, so the file
is green on the T5 worktree and asserts the full contract after the merge.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from ptest import contracts as C
from ptest import executability as exec_check
from ptest import init_render
from ptest.cli import main


def _t2_present() -> bool:
    return hasattr(exec_check, "FACT_KEYS") and hasattr(
        exec_check, "parallel_request")


def _t3_present() -> bool:
    return hasattr(init_render, "render_init_footer")


def _t4_present() -> bool:
    try:
        __import__("ptest.deterministic_items")
    except ImportError:
        return False
    import ptest.agent_assessment as assessment

    try:
        import inspect

        return "answers" in inspect.signature(
            assessment.plan_item_reviews).parameters
    except (TypeError, ValueError):
        return False


def _t1_present() -> bool:
    try:
        from ptest.runtime import pytest_bridge as bridge
    except ImportError:
        return False
    return hasattr(bridge, "QUALIFIED_XDIST_VERSIONS") and hasattr(
        bridge, "PARALLEL_DIST_MODES")


needs_t1_t2 = pytest.mark.skipif(
    not (_t1_present() and _t2_present()),
    reason="needs T1 bridge constants + T2 admission merge",
)
needs_t2_t3 = pytest.mark.skipif(
    not (_t2_present() and _t3_present()),
    reason="needs T2 facts + T3 renderers merge",
)
needs_t4 = pytest.mark.skipif(not _t4_present(), reason="needs T4 merge")


# ---- project fixtures -----------------------------------------------------

def _write_pytest_project(root: Path, *, addopts: str) -> None:
    """Standalone pytest project with real xdist from the test venv."""
    tests = root / "tests"
    tests.mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\n"
        f"addopts = '{addopts}'\n",
        encoding="utf-8",
    )
    (tests / "conftest.py").write_text(
        "import os\n"
        "\n"
        "\n"
        "def pytest_configure_node(node):\n"
        "    return None\n"
        "\n"
        "\n"
        "def pytest_collection_modifyitems(items):\n"
        "    return None\n",
        encoding="utf-8",
    )
    (tests / "test_groups.py").write_text(
        "import os\n"
        "\n"
        "import pytest\n"
        "\n"
        "MARKERS = os.environ.get('PTEST_T5_MARKERS', '')\n"
        "\n"
        "\n"
        "def _record(name):\n"
        "    if MARKERS:\n"
        "        with open(os.path.join(MARKERS, 'workers.log'), 'a',\n"
        "                  encoding='utf-8') as handle:\n"
        "            handle.write(\n"
        "                os.environ.get('PYTEST_XDIST_WORKER', '-') + ':'\n"
        "                + os.environ.get('PTEST_WORKER_ID', '-') + ':'\n"
        "                + name + '\\n')\n"
        "\n"
        "\n"
        "@pytest.mark.xdist_group('alpha')\n"
        "def test_alpha_one():\n"
        "    _record('alpha_one')\n"
        "    assert True\n"
        "\n"
        "\n"
        "@pytest.mark.xdist_group('alpha')\n"
        "def test_alpha_two():\n"
        "    _record('alpha_two')\n"
        "    assert True\n"
        "\n"
        "\n"
        "def test_plain_one():\n"
        "    _record('plain_one')\n"
        "    assert True\n"
        "\n"
        "\n"
        "def test_plain_two():\n"
        "    _record('plain_two')\n"
        "    assert True\n",
        encoding="utf-8",
    )


def _commit(root: Path) -> None:
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("GIT_")}
    env.update(
        GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
        GIT_AUTHOR_NAME="Fixture", GIT_COMMITTER_NAME="Fixture",
        GIT_AUTHOR_EMAIL="fixture@example.test",
        GIT_COMMITTER_EMAIL="fixture@example.test",
    )
    for args in (("init",),
                 ("config", "user.email", "fixture@example.test"),
                 ("config", "user.name", "Fixture"),
                 ("add", "."), ("commit", "-m", "initial")):
        subprocess.run(
            ("git", "-c", "core.hooksPath=" + os.devnull,
             "-c", "commit.gpgsign=false", "-C", str(root), *args),
            env=env, check=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def _write_persea_shaped_monorepo(root: Path) -> None:
    """Uv-style api (stub-qualified xdist, ``-n 0``) plus vitest web."""
    api = root / "api"
    web = root / "web"
    (api / "tests").mkdir(parents=True)
    (web / "tests").mkdir(parents=True)
    (root / ".ptest.toml").write_text(
        "version = 2\n\n[monorepo]\nchildren = [\"api\", \"web\"]\n",
        encoding="utf-8",
    )
    (api / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\n"
        "addopts = '-n 4 --dist=loadgroup -m \"not extended_migration\"'\n",
        encoding="utf-8",
    )
    (api / "tests" / "conftest.py").write_text(
        "def pytest_sessionfinish(session, exitstatus):\n    return None\n",
        encoding="utf-8",
    )
    (api / "tests" / "test_example.py").write_text(
        "def test_example():\n    assert True\n", encoding="utf-8")
    venv_packages = api / ".venv" / "lib" / "python3.12" / "site-packages"
    dist_info = venv_packages / "pytest_xdist-3.8.0.dist-info"
    dist_info.mkdir(parents=True)
    (dist_info / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: pytest-xdist\nVersion: 3.8.0\n",
        encoding="utf-8",
    )
    (api / ".venv" / "pyvenv.cfg").write_text(
        "home = /usr/bin\ninclude-system-site-packages = false\nversion = 3.12\n",
        encoding="utf-8",
    )
    (api / ".ptest.toml").write_text(
        'version = 1\nproject_id = "abababababababababababababababab"\n'
        "[runner]\n"
        'kind = "pytest"\n'
        'launcher = ["uv", "run", "--locked", "--no-sync", "python"]\n'
        'args = ["-n", "0"]\n'
        "full_args = []\n"
        'test_roots = ["tests"]\n'
        "workers = 1\n"
        'lifecycle = "cooperative-process-group"\n'
        "[selection]\n"
        "enabled = false\n",
        encoding="utf-8",
    )
    (web / "tests" / "a.test.ts").write_text("export {};\n",
                                             encoding="utf-8")
    (web / ".ptest.toml").write_text(
        'version = 1\nproject_id = "cdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcd"\n'
        "[runner]\n"
        'kind = "vitest"\n'
        'launcher = ["node"]\n'
        "args = []\n"
        "full_args = []\n"
        'test_roots = ["tests"]\n'
        "workers = 1\n"
        'lifecycle = "cooperative-process-group"\n'
        "[setup]\n"
        'argv = ["npm", "ci"]\n'
        'required_paths = ["node_modules"]\n'
        "network = true\nlifecycle_scripts = true\n",
        encoding="utf-8",
    )


def _install_fake_claude(bindir: Path) -> None:
    """Claude-shaped fake answering one one-row reply per item request."""
    script = "\n".join([
        "#!" + sys.executable,
        "import json, os, sys",
        "here = os.path.dirname(os.path.abspath(sys.argv[0]))",
        "if len(sys.argv) > 1 and sys.argv[1] == '--version':",
        "    sys.stdout.write('claude-test 1.0\\n')",
        "    sys.exit(0)",
        "request = json.load(sys.stdin)",
        "item_id = request['policy']['item']['id']",
        "with open(os.path.join(here, 'argv.log'), 'a',",
        "          encoding='utf-8') as handle:",
        "    handle.write(json.dumps({'argv': sys.argv[1:],",
        "                              'item': item_id}) + '\\n')",
        "excerpts = request['excerpts']",
        "if excerpts:",
        "    first = excerpts[0]",
        "    reply = {'status': 'satisfied',",
        "             'rationale': ('Reviewed ' + item_id + ' against '",
        "                         'the cited excerpt lines.'),",
        "             'evidence': [{'path': first['path'],",
        "                           'start_line': first['start_line'],",
        "                           'end_line': first['end_line'],",
        "                           'sha256': first['sha256']}],",
        "             'finding': None}",
        "else:",
        "    reply = {'status': 'unknown',",
        "             'rationale': ('The bounded source evidence does not '",
        "                         'establish this row.'),",
        "             'evidence': [], 'finding': None}",
        "envelope = {'type': 'result', 'subtype': 'success',",
        "            'is_error': False, 'num_turns': 1,",
        "            'permission_denials': [],",
        "            'result': json.dumps(reply)}",
        "sys.stdout.write(json.dumps(envelope))",
        "",
    ])
    bindir.mkdir(exist_ok=True)
    executable = bindir / "claude"
    executable.write_text(script, encoding="utf-8")
    executable.chmod(0o755)


def _prepare_review(monkeypatch, root: Path, bindir: Path) -> None:
    monkeypatch.chdir(root)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.delenv("CI", raising=False)
    _install_fake_claude(bindir)
    monkeypatch.setenv(
        "PATH", str(bindir) + os.pathsep + os.environ.get("PATH", ""))


def _read_launches(bindir: Path) -> list:
    log = bindir / "argv.log"
    if not log.exists():
        return []
    return [json.loads(line) for line in
            log.read_text(encoding="utf-8").splitlines() if line.strip()]


# ---- disclosure (T5-only, runs at base) ------------------------------------

def test_review_disclosure_is_at_most_three_lines_before_prompt(
        tmp_path, monkeypatch, capsys):
    """The short disclosure is three lines, then the consent prompt."""
    import sys
    from types import SimpleNamespace

    from ptest import cli as cli_module

    adapter = SimpleNamespace(name="claude", qualified=True)
    resolution = SimpleNamespace(root=tmp_path)
    monkeypatch.setattr(sys.stderr, "isatty", lambda: False)
    monkeypatch.setattr("builtins.input", lambda: "no")
    assert cli_module._render_review_disclosure(
        adapter, resolution, calls=9, concurrency=4,
        model="haiku") is False
    err = capsys.readouterr().err
    assert "Run this review once? [y/N]:" in err
    head, _, _ = err.partition("Run this review once?")
    lines = [line for line in head.splitlines() if line.strip()]
    assert len(lines) <= 3
    assert lines[0].startswith("Model review disclosure: claude ")
    assert "9 calls" in lines[0]
    assert "4 at a time" in lines[0]
    assert "model haiku" in lines[0]
    assert "--offline" in "\n".join(lines[1:])


def test_review_disclosure_unknown_model_and_missing_count_shapes(
        tmp_path, monkeypatch, capsys):
    import sys
    from types import SimpleNamespace

    from ptest import cli as cli_module

    adapter = SimpleNamespace(name="codex", qualified=True)
    resolution = SimpleNamespace(root=tmp_path)
    monkeypatch.setattr(sys.stderr, "isatty", lambda: False)
    assert cli_module._render_review_disclosure(
        adapter, resolution, ask=False, calls=5, concurrency=2,
        model=None) is True
    err = capsys.readouterr().err
    assert err.splitlines()[0].startswith("Model review disclosure: codex ")
    assert "model chosen from the provider list after consent" in err
    assert cli_module._render_review_disclosure(
        adapter, resolution, ask=False, calls=None) is True
    short = capsys.readouterr().err.splitlines()[0]
    assert short.startswith("Model review disclosure: codex ")
    assert short.rstrip().endswith(".")


def test_doctor_decline_prints_short_disclosure_not_legal_text(
        tmp_path, monkeypatch, capsys):
    """Through main(): decline path shows the short disclosure only."""
    root = tmp_path / "decline"
    root.mkdir()
    (root / ".ptest.toml").write_text(
        'version = 1\nproject_id = "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"\n'
        "[runner]\n"
        'kind = "pytest"\n'
        f'launcher = ["{sys.executable}"]\n'
        "args = []\n"
        "full_args = []\n"
        'test_roots = ["tests"]\n'
        "workers = 1\n"
        'lifecycle = "cooperative-process-group"\n',
        encoding="utf-8",
    )
    (root / "tests").mkdir()
    (root / "tests" / "test_example.py").write_text(
        "def test_example():\n    assert True\n", encoding="utf-8")
    bindir = tmp_path / "bin"
    _prepare_review(monkeypatch, root, bindir)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda: "no")
    monkeypatch.setenv("PTEST_RECOMMENDATIONS_LOCK_DIR",
                       str(tmp_path / "locks"))

    assert main(("doctor", "--reviewer", "claude",
                 "--allow-model-review")) == 0
    captured = capsys.readouterr()
    head, _, _ = captured.err.partition("Run this review once?")
    lines = [line for line in head.splitlines() if line.strip()]
    # The spinner newline plus at most three disclosure lines.
    assert len(lines) <= 4
    assert any(line.startswith("Model review disclosure: claude ")
               for line in lines)
    assert "cannot perfectly detect secrets" not in captured.err
    assert "review not yet performed" in captured.out


def test_child_assessment_data_carries_facts_and_json_drops_them():
    """``facts=`` lands on the child; validator tolerates, projection drops."""
    from types import SimpleNamespace

    from ptest import checklist as checklist_module
    from ptest import cli as cli_module

    rows = [
        SimpleNamespace(id=entry.id, status="unknown",
                        rationale="The bounded source evidence does not "
                                  "establish this row.",
                        label=entry.label, dropped_citations=0, evidence=[])
        for entry in checklist_module.CATALOG
    ]
    assessment = SimpleNamespace(
        project_id="a" * 32, scope=".", packet_sha256="b" * 64,
        rows=rows,
        score=SimpleNamespace(satisfied=0, applicable=len(rows),
                              percent=0),
        findings=[],
    )
    facts = {
        "project": "api", "runner": "pytest", "runs": True,
        "runs_reason": None, "runs_fix": None,
        "parallel": "4 workers (xdist, --dist loadgroup)",
        "parallel_short": "4 workers", "parallel_fix": None,
        "setup": None, "full_suite": None, "full_blocked": None,
    }
    child = cli_module._child_assessment_data(
        SimpleNamespace(scope=".", declaration="."), assessment, [],
        execution={"status": "executable", "detail": "ready",
                   "fix": None},
        facts=facts)
    assert child["facts"] == facts
    # The public validator tolerates the additive key ...
    C._validate_agent_assessment_payload({
        "schema": C.AGENT_ASSESSMENT_SCHEMA,
        "provider": {"name": "claude", "cli_version": "t",
                     "profile": "p"},
        "children": [child],
        "limitations": [],
        "publication": {"status": "created", "path": "recommendations.md",
                        "sha256": "0" * 64},
    })
    # ... and projection drops it, so --assessment-json is unchanged.
    projected = C._project_agent_assessment_payload({
        "schema": C.AGENT_ASSESSMENT_SCHEMA,
        "provider": {"name": "claude", "cli_version": "t",
                     "profile": "p"},
        "children": [child],
        "limitations": [],
        "publication": {"status": "created", "path": "recommendations.md",
                        "sha256": "c" * 64},
    })
    assert "facts" not in projected["children"][0]


# ---- (a) parallel end to end -----------------------------------------------

@needs_t1_t2
def test_parallel_end_to_end_four_workers_then_partial_grant(
        case, tmp_path, monkeypatch, capsys):
    root = tmp_path / "parallel"
    root.mkdir()
    _write_pytest_project(
        root, addopts='-n 4 --dist=loadgroup -m "not slow"')
    markers = root / "markers"
    markers.mkdir()

    monkeypatch.chdir(root)
    assert main(("init", "--no-doctor", "--agents", "none")) == 0
    out = capsys.readouterr().out
    assert "parallel: 4 workers" in out
    for banned in ("┌", "ready with caveats", "expected:", "fingerprint"):
        assert banned not in out
    import tomllib

    generated = tomllib.loads(
        (root / ".ptest.toml").read_text(encoding="utf-8"))["runner"]["args"]
    assert generated == []
    _commit(root)

    env = {"PTEST_T5_MARKERS": str(markers)}
    full = case.invoke(case.domain(slots=4), root, "--full",
                       env=env, timeout=60)
    assert full.code == 0, full.stderr.decode()
    assert full.result is not None
    assert full.result["data"]["granted_workers"] == 4
    assert any(
        reason["code"] == "project-filtered"
        and "-m not slow" in reason["message"]
        for reason in full.result["data"]["reasons"])
    workers = set()
    for line in (markers / "workers.log").read_text(
            encoding="utf-8").splitlines():
        controller, worker, _ = line.split(":")
        workers.add((controller, worker))
    assert len(workers) == 4

    partial = case.invoke(case.domain(slots=2), root, "--full",
                          env=env, timeout=60)
    assert partial.result is not None
    assert partial.result["data"]["granted_workers"] == 2
    assert ("parallel-workers: 2 xdist workers (4 requested, 2 granted)"
            in partial.stderr.decode())


# ---- (b) fallback -----------------------------------------------------------

@needs_t1_t2
def test_dist_each_falls_back_to_serial(case, tmp_path, monkeypatch,
                                        capsys):
    root = tmp_path / "fallback"
    root.mkdir()
    _write_pytest_project(root, addopts="--dist each")

    monkeypatch.chdir(root)
    assert main(("init", "--no-doctor", "--agents", "none")) == 0
    out = capsys.readouterr().out
    import tomllib

    generated = tomllib.loads(
        (root / ".ptest.toml").read_text(encoding="utf-8"))["runner"]["args"]
    assert generated == ["-n", "0"]
    assert ("parallel: no — --dist each is not supported; "
            "ptest runs serially") in out
    _commit(root)

    full = case.invoke(case.domain(slots=4), root, "--full", timeout=60)
    assert full.code == 0, full.stderr.decode()


# ---- (c) Ctrl-C through the guard -------------------------------------------

@needs_t1_t2
def test_ctrl_c_kills_parallel_workers(case, tmp_path):
    import psutil

    root = tmp_path / "interrupt"
    root.mkdir()
    _write_pytest_project(
        root, addopts="-n 4 --dist=loadgroup")
    sleeper = root / "tests" / "test_sleep.py"
    sleeper.write_text(
        "import os, time\n"
        "\n"
        "MARKERS = os.environ.get('PTEST_T5_MARKERS', '')\n"
        "\n"
        "\n"
        "def _mark(name):\n"
        "    with open(os.path.join(MARKERS, name), 'w',"
        "              encoding='utf-8') as handle:\n"
        "        handle.write('started\\n')\n"
        "\n"
        "\n"
        "def test_sleep_a():\n"
        "    _mark('a')\n"
        "    time.sleep(30)\n"
        "\n"
        "\n"
        "def test_sleep_b():\n"
        "    _mark('b')\n"
        "    time.sleep(30)\n"
        "\n"
        "\n"
        "def test_sleep_c():\n"
        "    _mark('c')\n"
        "    time.sleep(30)\n"
        "\n"
        "\n"
        "def test_sleep_d():\n"
        "    _mark('d')\n"
        "    time.sleep(30)\n",
        encoding="utf-8",
    )
    markers = root / "markers"
    markers.mkdir()

    # Init through cli.main in-process (no result-json injection issue).
    monkeypatch_cwd = os.getcwd()
    os.chdir(root)
    try:
        assert main(("init", "--no-doctor", "--agents", "none")) == 0
    finally:
        os.chdir(monkeypatch_cwd)
    _commit(root)

    domain = case.domain(slots=4)
    child_env = {key: value for key, value in os.environ.items()}
    child_env["PTEST_T5_MARKERS"] = str(markers)
    proc = subprocess.Popen(
        [sys.executable, "-m", "ptest", "--fixture-domain",
         str(domain.root), "--full"],
        cwd=str(root), env=child_env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 55
        while time.monotonic() < deadline:
            if all((markers / name).exists() for name in "abcd"):
                break
            if proc.poll() is not None:
                break
            time.sleep(0.2)
        assert all((markers / name).exists() for name in "abcd"), \
            "workers never started"
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, subprocess.signal.SIGINT)
        code = proc.wait(timeout=20)
        assert code != 0
        # No process of that session survives: poll the group, then scan.
        gone_by = time.monotonic() + 10
        while time.monotonic() < gone_by:
            try:
                os.killpg(pgid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.2)
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            pass
        else:
            leftovers = [
                proc_info.info for proc_info in psutil.process_iter(
                    ["pid", "cmdline"])
                if proc_info.info["cmdline"]
                and any(str(root) in part
                        for part in proc_info.info["cmdline"])]
            assert leftovers == [], leftovers
    finally:
        if proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid),
                          subprocess.signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass
            proc.wait(timeout=10)
        proc.stdout.close()
        proc.stderr.close()


# ---- (d) doctor on a persea-shaped monorepo ----------------------------------

@needs_t2_t3
@needs_t4
def test_doctor_persea_shaped_monorepo(tmp_path, monkeypatch, capsys):
    from ptest.checklist import CATALOG

    root = tmp_path / "monorepo"
    root.mkdir()
    _write_persea_shaped_monorepo(root)
    bindir = tmp_path / "bin"
    _prepare_review(monkeypatch, root, bindir)
    monkeypatch.setenv("PTEST_RECOMMENDATIONS_LOCK_DIR",
                       str(tmp_path / "locks"))

    assert main(("doctor", "--reviewer", "claude",
                 "--allow-model-review")) == 0
    human = capsys.readouterr()
    assert "api  pytest · " in human.out
    assert "web  vitest · " in human.out
    assert "runs: yes · parallel: no · setup:" in human.out
    assert "parallel: inside vitest" in human.out
    assert ("? Test timing  no timing history yet: "
            "run ptest --full once") in human.out
    assert "✗ Test selection" in human.out
    unknown_lines = [line for line in human.out.splitlines()
                     if line.lstrip().startswith("? ")]
    assert unknown_lines
    for line in unknown_lines:
        assert len(line.split("  ")) >= 2

    launches = _read_launches(bindir)
    items = [entry["item"] for entry in launches]
    assert "TIMING-001" not in items
    assert "SELECT-001" not in items
    assert "PARALLEL-001" not in items
    catalog_ids = {entry.id for entry in CATALOG}
    for item_id in catalog_ids - {"TIMING-001", "SELECT-001",
                                  "PARALLEL-001"}:
        assert items.count(item_id) == 2

    disclosure_head, _, _ = human.err.partition("Run this review once?")
    assert ("Model review disclosure: claude " in disclosure_head)
    assert len([line for line in disclosure_head.splitlines()
                if line.strip()]) <= 4

    report = (root / "recommendations.md").read_text(encoding="utf-8")
    assert "parallel" in report
    assert "Reason:" in report

    assert main(("doctor", "--reviewer", "claude", "--allow-model-review",
                 "--assessment-json")) == 0
    document = C.decode_public_document(
        capsys.readouterr().out.encode("utf-8"))
    assert document.kind == "agent-assessment" and document.error is None
    for child in document.data["children"]:
        assert "facts" not in child


# ---- (e) init on the same monorepo -------------------------------------------

@needs_t2_t3
def test_init_persea_shaped_monorepo_grouped_actions_and_restart(
        tmp_path, monkeypatch, capsys):
    root = tmp_path / "init-mono"
    root.mkdir()
    _write_persea_shaped_monorepo(root)
    monkeypatch.chdir(root)

    assert main(("init", "--no-doctor", "--agents", "claude")) == 0
    first = capsys.readouterr().out
    assert "api  parallel off → " in first
    assert ('remove "-n", "0" from [runner] args in api/.ptest.toml '
            "to run 4 workers") in first
    assert first.count("Restart your coding agents") == 1

    assert main(("init", "--no-doctor", "--agents", "claude")) == 0
    second = capsys.readouterr().out
    assert "Restart your coding agents" not in second
    assert "config     unchanged" in second


# ---- (f) mirror equality ------------------------------------------------------

def test_mirror_equality_with_wave1_names():
    t1 = pytest.importorskip(
        "ptest.runtime.pytest_bridge",
        reason="needs T1 bridge merge",
    )
    if not _t2_present():
        pytest.skip("needs T2 executability merge")
    if not _t3_present():
        pytest.skip("needs T3 project_facts merge")
    if not _t4_present():
        pytest.skip("needs T4 deterministic merge")
    import ptest.agent_assessment as assessment
    import ptest.project_facts as project_facts
    import ptest.render as render

    assert (t1.QUALIFIED_XDIST_VERSIONS
            == exec_check.XDIST_QUALIFIED_VERSIONS)
    assert t1.PARALLEL_DIST_MODES == exec_check.XDIST_DIST_MODES
    assert project_facts.FACT_KEYS == exec_check.FACT_KEYS
    item = exec_check.Executability(
        project=".", runner="pytest", status="executable", caveats=(),
        reason=None, fix=None, full=False, example=None)
    assert tuple(item.facts()) == project_facts.FACT_KEYS
    assert (render.PTEST_ANSWER_PREFIX
            == assessment.PTEST_ANSWER_PREFIX == "Answered by ptest: ")
