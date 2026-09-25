"""Wave-2 CLI wiring: parallel facts, init/doctor renderers, disclosure.

Integration through ``cli.main`` / ``case.invoke`` with no monkeypatching
of ptest modules. The only fakes are provider executables on a temporary
``PATH`` (the ``test_doctor_init_integration`` pattern) plus
``stdin.isatty``/``input`` where a TTY is needed. No network, no real
provider, no real claude/codex/opencode.

Seam note: the T1-T4 merges are all in, so every test runs unguarded
(``executability.facts``/``parallel_request``, ``render_init_footer``,
``deterministic_items``, T1's bridge). Run-phase checkouts live under
their fixture domain (the scheduler refuses escaping checkouts), and run
phases point the config launcher at the test venv's own interpreter,
which ships real xdist 3.8.0.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from ptest import agent_assessment as assessment
from ptest import contracts as C
from ptest import executability as exec_check
from ptest import project_facts
from ptest import render
from ptest.cli import main
from ptest.runtime import pytest_bridge as t1


# ---- project fixtures -----------------------------------------------------

def _write_pytest_project(root: Path, *, addopts: str,
                          launcher: list[str] | None = None,
                          args: list[str] | None = None) -> None:
    """Standalone pytest project with real xdist from the test venv.

    With ``launcher``/``args`` given, pre-writes ``.ptest.toml`` (init
    then treats it as already configured and never rewrites it, per M5);
    without them init writes the config fresh. A fresh init derives a
    bare ``("python",)`` launcher, which the parallel tier cannot verify
    (M1), so run phases that need real xdist use an absolute launcher
    pointing at the test venv's own interpreter.
    """
    tests = root / "tests"
    tests.mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\n"
        f"addopts = '{addopts}'\n",
        encoding="utf-8",
    )
    if launcher is not None:
        import json as _json

        (root / ".ptest.toml").write_text(
            'version = 1\nproject_id = "abababababababababababababababab"\n'
            "[runner]\n"
            'kind = "pytest"\n'
            f"launcher = {_json.dumps(launcher)}\n"
            f"args = {_json.dumps(args if args is not None else [])}\n"
            "full_args = []\n"
            'test_roots = ["tests"]\n'
            "workers = 1\n"
            'lifecycle = "cooperative-process-group"\n',
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
        "MARKERS = os.environ.get('T5_WORKER_MARKERS', '')\n"
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
        "    assert True\n"
        "\n"
        "\n"
        "def test_plain_three():\n"
        "    _record('plain_three')\n"
        "    assert True\n",
        encoding="utf-8",
    )


def _point_launcher_at_test_venv(root: Path) -> None:
    """Point a fresh-written config at the test venv for run phases.

    Fresh init derives a bare ``("python",)`` launcher, which has no
    pytest on PATH here; run phases that execute real pytest/xdist need
    the absolute test-venv interpreter. The parallel-tier shape under
    test (addopts, args) is left untouched.
    """
    import json as _json

    config = root / ".ptest.toml"
    lines = []
    for line in config.read_text(encoding="utf-8").splitlines(
            keepends=True):
        if line.strip().startswith("launcher = "):
            line = f"launcher = {_json.dumps([sys.executable])}\n"
        lines.append(line)
    config.write_text("".join(lines), encoding="utf-8")


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


def _write_fresh_xdist_project(root: Path) -> None:
    """Fresh xdist+uv project: no `.ptest.toml`, stub-qualified xdist.

    `uv.lock` makes fresh init derive the uv launcher, so the stub
    ``.venv`` (qualified 3.8.0) verifies and init writes empty args.
    """
    (root / "tests").mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\n"
        "addopts = '-n 4 --dist=loadgroup -m \"not extended_migration\"'\n",
        encoding="utf-8",
    )
    (root / "tests" / "conftest.py").write_text(
        "def pytest_sessionfinish(session, exitstatus):\n    return None\n",
        encoding="utf-8",
    )
    (root / "tests" / "test_example.py").write_text(
        "def test_example():\n    assert True\n", encoding="utf-8")
    (root / "uv.lock").write_text("", encoding="utf-8")
    venv_packages = root / ".venv" / "lib" / "python3.12" / "site-packages"
    dist_info = venv_packages / "pytest_xdist-3.8.0.dist-info"
    dist_info.mkdir(parents=True)
    (dist_info / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: pytest-xdist\nVersion: 3.8.0\n",
        encoding="utf-8",
    )
    (root / ".venv" / "pyvenv.cfg").write_text(
        "home = /usr/bin\ninclude-system-site-packages = false\nversion = 3.12\n",
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
    # ... and projection drops it, so --json is unchanged.
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

def test_parallel_init_writes_no_serial_opt_out(case, tmp_path, monkeypatch,
                                                capsys):
    """Init half of (a): fresh init writes no ``-n 0`` for xdist shape."""
    import tomllib

    domain = case.domain(slots=4)
    root = domain.root / "parallel"
    root.mkdir()
    _write_pytest_project(
        root, addopts='-n 4 --dist=loadgroup -m "not slow"')

    monkeypatch.chdir(root)
    assert main(("init", "--no-doctor", "--agents", "none")) == 0
    capsys.readouterr()
    generated = tomllib.loads(
        (root / ".ptest.toml").read_text(encoding="utf-8"))["runner"]["args"]
    assert generated == []


def test_parallel_init_reports_four_workers(case, tmp_path, monkeypatch,
                                            capsys):
    """Init half of (a): the facts line once the launcher is verifiable.

    A fresh init derives a bare ``("python",)`` launcher, which M1 leaves
    unverifiable, so the ``parallel: 4 workers`` line is asserted after
    pointing the config at the test venv's own interpreter (which ships
    real xdist 3.8.0).
    """
    import tomllib

    domain = case.domain(slots=4)
    root = domain.root / "parallel"
    root.mkdir()
    _write_pytest_project(
        root, addopts='-n 4 --dist=loadgroup -m "not slow"',
        launcher=[sys.executable], args=[])
    markers = root / "markers"
    markers.mkdir()

    monkeypatch.chdir(root)
    assert main(("init", "--no-doctor", "--agents", "none")) == 0
    out = capsys.readouterr().out
    assert "parallel: 4 workers" in out
    for banned in ("┌", "ready with caveats", "expected:", "fingerprint"):
        assert banned not in out
    generated = tomllib.loads(
        (root / ".ptest.toml").read_text(encoding="utf-8"))["runner"]["args"]
    assert generated == []


def test_parallel_end_to_end_four_workers_then_partial_grant(
        case, tmp_path, monkeypatch, capsys):
    import shutil
    import tomllib

    domain_full = case.domain(slots=4)
    root = domain_full.root / "parallel"
    root.mkdir()
    _write_pytest_project(
        root, addopts='-n 4 --dist=loadgroup -m "not slow"',
        launcher=[sys.executable], args=[])
    # Markers live outside the checkout: test output inside it would trip
    # ptest's changed-during-run detection.
    markers = tmp_path / "full-markers"
    markers.mkdir()

    monkeypatch.chdir(root)
    assert main(("init", "--no-doctor", "--agents", "none")) == 0
    capsys.readouterr()
    _commit(root)

    env = {"T5_WORKER_MARKERS": str(markers)}
    full = case.invoke(domain_full, root, "--full",
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

    # The partial-grant checkout must live under the 2-slot domain.
    domain_partial = case.domain(slots=2)
    root2 = domain_partial.root / "parallel"
    shutil.copytree(
        root, root2,
        ignore=shutil.ignore_patterns(
            "markers", "__pycache__", ".pytest_cache"))
    markers2 = tmp_path / "partial-markers"
    markers2.mkdir()
    partial = case.invoke(
        domain_partial, root2, "--full",
        env={"T5_WORKER_MARKERS": str(markers2)}, timeout=60)
    assert partial.result is not None
    assert partial.result["data"]["granted_workers"] == 2
    assert ("parallel-workers: 2 xdist workers (4 requested, 2 granted)"
            in partial.stderr.decode())


# ---- (b) fallback -----------------------------------------------------------

def test_dist_each_init_writes_serial_opt_out(case, tmp_path, monkeypatch,
                                              capsys):
    """Init half of (b): fresh init writes ``-n 0`` for `--dist each`."""
    import tomllib

    domain = case.domain(slots=4)
    root = domain.root / "fallback"
    root.mkdir()
    _write_pytest_project(root, addopts="--dist each")

    monkeypatch.chdir(root)
    assert main(("init", "--no-doctor", "--agents", "none")) == 0
    out = capsys.readouterr().out

    generated = tomllib.loads(
        (root / ".ptest.toml").read_text(encoding="utf-8"))["runner"]["args"]
    assert generated == ["-n", "0"]
    # Terminal project lines carry the short form (§3.6); the long
    # fallback reason lives in the facts dict and the doctor report.
    assert "parallel: no" in out


def test_dist_each_full_passes_serially(case, tmp_path, monkeypatch,
                                        capsys):
    """Run half of (b): the serial fallback passes `--full` serially."""
    domain = case.domain(slots=4)
    root = domain.root / "fallback"
    root.mkdir()
    _write_pytest_project(root, addopts="--dist each")

    monkeypatch.chdir(root)
    assert main(("init", "--no-doctor", "--agents", "none")) == 0
    capsys.readouterr()
    _point_launcher_at_test_venv(root)
    _commit(root)

    full = case.invoke(domain, root, "--full", timeout=60)
    assert full.code == 0, full.stderr.decode()


# ---- (c) Ctrl-C through the guard -------------------------------------------

def test_ctrl_c_kills_parallel_workers(case, tmp_path):
    import select

    # The checkout must live under the fixture domain, and the run phase
    # needs real xdist through the test venv's interpreter.
    domain = case.domain(slots=4)
    root = domain.root / "interrupt"
    root.mkdir()
    _write_pytest_project(
        root, addopts="-n 4 --dist=loadgroup",
        launcher=[sys.executable], args=[])
    sleeper = root / "tests" / "test_sleep.py"
    sleeper.write_text(
        "import os, time\n"
        "\n"
        "MARKERS = os.environ.get('T5_WORKER_MARKERS', '')\n"
        "_HOLD = None\n"
        "try:\n"
        "    _HOLD = os.open(os.path.join(MARKERS, 'interrupt.fifo'),\n"
        "                   os.O_WRONLY)\n"
        "except OSError:\n"
        "    _HOLD = None\n"
        "\n"
        "MARKERS = os.environ.get('T5_WORKER_MARKERS', '')\n"
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
    # Descendant-held fifo: every xdist worker holds the write end from
    # test-module import until death; EOF proves no worker survives.
    fifo = markers / "interrupt.fifo"
    os.mkfifo(fifo)

    # Init through cli.main in-process (no result-json injection issue).
    monkeypatch_cwd = os.getcwd()
    os.chdir(root)
    try:
        assert main(("init", "--no-doctor", "--agents", "none")) == 0
    finally:
        os.chdir(monkeypatch_cwd)
    _commit(root)

    child_env = {key: value for key, value in os.environ.items()}
    child_env["T5_WORKER_MARKERS"] = str(markers)
    reader = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
    # Same process group as the caller: a detached session made the
    # scheduler count twin descendants as escaped under a concurrent ptest
    # admission and ended that run incomplete (exit 70).
    proc = subprocess.Popen(
        [sys.executable, "-m", "ptest", "--fixture-domain",
         str(domain.root), "--full"],
        cwd=str(root), env=child_env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
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
        os.kill(proc.pid, subprocess.signal.SIGINT)
        code = proc.wait(timeout=20)
        assert code != 0
        # Every descendant held the fifo write end; EOF means none survive.
        gone_by = time.monotonic() + 10
        eof = False
        while time.monotonic() < gone_by:
            ready, _, _ = select.select(
                [reader], [], [],
                max(0.0, gone_by - time.monotonic()))
            if ready and os.read(reader, 65536) == b"":
                eof = True
                break
        assert eof, "parallel workers survived SIGINT"
    finally:
        if proc.poll() is None:
            try:
                os.kill(proc.pid, subprocess.signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass
            proc.wait(timeout=10)
        proc.stdout.close()
        proc.stderr.close()
        os.close(reader)


# ---- (d) doctor on a persea-shaped monorepo ----------------------------------

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
    assert "runs: yes · parallel: no" in human.out
    assert "parallel: inside vitest" in human.out
    assert ("? Test timing  no timing history yet: "
            "run ptest --full once") in human.out
    assert "✗ Test selection" in human.out
    # PARALLEL-001: api configures xdist but opts out with -n 0, so the
    # deterministic answer is a gap carrying the fallback reason and fix.
    assert "✗ Parallel execution" in human.out
    assert "parallel safety" in human.out
    assert human.out.index("parallel safety") < human.out.index(
        "✗ Parallel execution")
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
    from collections import Counter

    counts = Counter(items)
    # Only planned provider requests take a call: deterministic answers
    # (TIMING/SELECT/PARALLEL) and deterministic skips never reach the
    # provider.
    assert set(counts) <= catalog_ids - {
        "TIMING-001", "SELECT-001", "PARALLEL-001"}
    # Web reviews every non-deterministic item (9); api skips the three
    # pure-library items without a model call (6). At most one call per
    # child.
    assert sum(counts.values()) == 15
    assert all(count <= 2 for count in counts.values())

    disclosure_head, _, _ = human.err.partition("Run this review once?")
    assert ("Model review disclosure: claude " in disclosure_head)
    disclosure_lines = disclosure_head.splitlines()
    start = next(index for index, line in enumerate(disclosure_lines)
                 if line.startswith("Model review disclosure:"))
    disclosure_only = [
        line for line in disclosure_lines[start:]
        if line.strip() and not line.startswith("doctor review:")]
    assert len(disclosure_only) <= 3

    report = (root / "recommendations.md").read_text(encoding="utf-8")
    assert "parallel" in report
    assert "Reason:" in report
    assert "## PARALLEL-001" in report
    assert ("pytest configures xdist but api/.ptest.toml sets -n 0"
            in report)
    assert ('remove "-n", "0" from [runner] args in api/.ptest.toml '
            "to run 4 workers" in report)

    assert main(("doctor", "--reviewer", "claude", "--allow-model-review",
                 "--json")) == 0
    document = C.decode_public_document(
        capsys.readouterr().out.encode("utf-8"))
    assert document.kind == "agent-assessment" and document.error is None
    for child in document.data["children"]:
        assert "facts" not in child
    by_scope = {child["scope"]: child
                for child in document.data["children"]}
    assert [row["id"] for row in by_scope["api"]["rows"]][-1] == (
        "PARALLEL-001")
    api_parallel = next(row for row in by_scope["api"]["rows"]
                        if row["id"] == "PARALLEL-001")
    assert api_parallel["status"] == "gap"
    assert api_parallel["rationale"].startswith("Answered by ptest: ")
    web_parallel = next(row for row in by_scope["web"]["rows"]
                        if row["id"] == "PARALLEL-001")
    assert web_parallel["status"] == "satisfied"


def test_doctor_fresh_xdist_project_parallel_satisfied(
        tmp_path, monkeypatch, capsys):
    """Doctor on a qualified 4-worker project: satisfied, no model call."""
    root = tmp_path / "fresh-doctor"
    root.mkdir()
    _write_fresh_xdist_project(root)
    monkeypatch.chdir(root)
    assert main(("init", "--no-doctor", "--agents", "none")) == 0
    capsys.readouterr()
    bindir = tmp_path / "bin"
    _prepare_review(monkeypatch, root, bindir)
    monkeypatch.setenv("PTEST_RECOMMENDATIONS_LOCK_DIR",
                       str(tmp_path / "locks"))

    assert main(("doctor", "--reviewer", "claude",
                 "--allow-model-review")) == 0
    human = capsys.readouterr()
    assert "Parallel execution" in human.out
    assert "✗ Parallel execution" not in human.out
    assert "? Parallel execution" not in human.out

    launches = _read_launches(bindir)
    assert "PARALLEL-001" not in [entry["item"] for entry in launches]

    report = (root / "recommendations.md").read_text(encoding="utf-8")
    assert "## PARALLEL-001" in report


# ---- (d2) one safety gap keeps the safety-first parallel fix ------------------

def _write_unconfigured_db_project(root: Path) -> None:
    """Standalone pytest project: no xdist, database usage, one passing test.

    Empty addopts means no parallel runner is configured, so PARALLEL-001
    plans the safety-first provisional fix. The ``DATABASE_URL`` line
    defeats the ``no-database`` skip, so DB-001/DB-002 go to the provider.
    """
    tests = root / "tests"
    tests.mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\n"
        "addopts = ''\n",
        encoding="utf-8",
    )
    (tests / "test_example.py").write_text(
        "import os\n"
        "\n"
        "DATABASE_URL = os.environ.get(\n"
        "    'DATABASE_URL', 'sqlite:///fallback.db')\n"
        "\n"
        "\n"
        "def test_example():\n"
        "    assert DATABASE_URL\n",
        encoding="utf-8",
    )


def _install_fake_claude_with_db_gap(bindir: Path) -> None:
    """Claude-shaped fake answering a DB-002 gap, satisfied elsewhere."""
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
        "    citation = {'path': first['path'],",
        "                'start_line': first['start_line'],",
        "                'end_line': first['end_line'],",
        "                'sha256': first['sha256']}",
        "    if item_id == 'DB-002':",
        "        reply = {'status': 'gap',",
        "                 'rationale': ('DB-002 shows the tests share one '",
        "                               'database without isolation.'),",
        "                 'evidence': [citation],",
        "                 'finding': {",
        "                     'summary': ('Tests share one database '",
        "                                 'without isolation.'),",
        "                     'suggested_change': ('Give each test its own '",
        "                                          'isolated database.'),",
        "                     'evidence': [citation]}}",
        "    else:",
        "        reply = {'status': 'satisfied',",
        "                 'rationale': ('Reviewed ' + item_id + ' against '",
        "                             'the cited excerpt lines.'),",
        "                 'evidence': [citation],",
        "                 'finding': None}",
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


def test_doctor_unconfigured_project_with_db_gap_keeps_safety_first(
        tmp_path, monkeypatch, capsys):
    """One safety gap (DB-002) keeps the safety-first parallel fix."""
    root = tmp_path / "db-gap-doctor"
    root.mkdir()
    _write_unconfigured_db_project(root)
    monkeypatch.chdir(root)
    assert main(("init", "--no-doctor", "--agents", "none")) == 0
    capsys.readouterr()
    bindir = tmp_path / "bin"
    monkeypatch.chdir(root)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.delenv("CI", raising=False)
    _install_fake_claude_with_db_gap(bindir)
    monkeypatch.setenv(
        "PATH", str(bindir) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setenv("PTEST_RECOMMENDATIONS_LOCK_DIR",
                       str(tmp_path / "locks"))

    assert main(("doctor", "--reviewer", "claude",
                 "--allow-model-review")) == 0
    human = capsys.readouterr()
    assert "✗ Database isolation" in human.out
    assert "✗ Parallel execution" in human.out
    assert "Resolve the parallel-safety gaps first." in human.out
    assert "request workers with -n auto" not in human.out

    launches = _read_launches(bindir)
    items = [entry["item"] for entry in launches]
    assert "DB-002" in items
    assert "PARALLEL-001" not in items

    report = (root / "recommendations.md").read_text(encoding="utf-8")
    assert "## PARALLEL-001" in report
    assert "Resolve the parallel-safety gaps first." in report


# ---- (e) init on the same monorepo -------------------------------------------

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
    assert "parallel: inside vitest" in first
    assert "config     unchanged" in first
    assert "guidance   created" in first
    assert first.count("Restart your coding agents") == 1

    assert main(("init", "--no-doctor", "--agents", "claude")) == 0
    second = capsys.readouterr().out
    assert "Restart your coding agents" not in second
    assert "config     unchanged" in second


def test_init_fresh_xdist_project_writes_no_serial_opt_out(
        tmp_path, monkeypatch, capsys):
    """Init on a fresh xdist project: no `-n 0`, `4 workers` line.

    The project-level counterpart of the monorepo coverage above: the
    config init writes carries empty args, and the project line reports
    the qualified worker count end to end through ``cli.main``.
    """
    import tomllib

    root = tmp_path / "fresh-api"
    root.mkdir()
    _write_fresh_xdist_project(root)
    monkeypatch.chdir(root)

    assert main(("init", "--no-doctor", "--agents", "claude")) == 0
    out = capsys.readouterr().out
    generated = tomllib.loads(
        (root / ".ptest.toml").read_text(encoding="utf-8"))["runner"]["args"]
    assert generated == []
    assert "parallel: 4 workers" in out
    assert "parallel off → " not in out
    assert "config     created" in out
    assert "guidance   created" in out
    assert out.count("Restart your coding agents") == 1


def test_init_smoke_block_prints_once(tmp_path, monkeypatch, capsys):
    """`ptest init --smoke` prints the smoke block exactly once.

    The footer owns the smoke block (§3.6 steps 3–4); the init flow must
    not also write a separate ``format_smoke`` block.
    """
    root = tmp_path / "smoke-api"
    root.mkdir()
    _write_fresh_xdist_project(root)
    monkeypatch.chdir(root)

    assert main(("init", "--smoke", "--no-doctor",
                 "--agents", "none")) == 0
    out = capsys.readouterr().out
    assert out.count("  smoke      ") == 1


def test_init_footer_separated_by_one_blank_line(
        tmp_path, monkeypatch, capsys):
    """The init footer starts after exactly one blank line.

    The file block ends, one blank line, then the footer (smoke /
    next steps / restart line) — no jamming, no double gap.
    """
    root = tmp_path / "smoke-gap"
    root.mkdir()
    _write_fresh_xdist_project(root)
    monkeypatch.chdir(root)

    assert main(("init", "--smoke", "--no-doctor",
                 "--agents", "none")) == 0
    out = capsys.readouterr().out
    assert out.count("  smoke      ") == 1
    assert "\n\n  smoke      " in out
    assert "\n\n\n  smoke      " not in out


# ---- (f) mirror equality ------------------------------------------------------

def test_mirror_equality_with_wave1_names():
    assert project_facts.FACT_KEYS == exec_check.FACT_KEYS
    item = exec_check.Executability(
        project=".", runner="pytest", status="executable", caveats=(),
        reason=None, fix=None, full=False, example=None)
    assert tuple(item.facts()) == project_facts.FACT_KEYS
    assert (render.PTEST_ANSWER_PREFIX
            == assessment.PTEST_ANSWER_PREFIX == "Answered by ptest: ")

    assert (t1.QUALIFIED_XDIST_VERSIONS
            == exec_check.XDIST_QUALIFIED_VERSIONS)
    assert t1.PARALLEL_DIST_MODES == exec_check.XDIST_DIST_MODES


# ---- DET1: deterministic rows cite the child .ptest.toml, never downgrade ---

def _write_det1_shaped_monorepo(root: Path) -> None:
    """Persea-shaped api (4 xdist workers) plus vitest web (DET1).

    Mirrors ``_write_persea_shaped_monorepo`` but the api runner requests
    real workers (empty ``args`` so ``-n 4`` from ``addopts`` applies) and
    the api child holds private ``.ptest/`` runtime state that must never
    reach the review evidence.
    """
    _write_persea_shaped_monorepo(root)
    (root / "api" / ".ptest.toml").write_text(
        'version = 1\nproject_id = "abababababababababababababababab"\n'
        "[runner]\n"
        'kind = "pytest"\n'
        'launcher = ["uv", "run", "--locked", "--no-sync", "python"]\n'
        "args = []\n"
        "full_args = []\n"
        'test_roots = ["tests"]\n'
        "workers = 1\n"
        'lifecycle = "cooperative-process-group"\n'
        "[selection]\n"
        "enabled = false\n",
        encoding="utf-8",
    )
    private = root / "api" / ".ptest" / "ledger"
    private.mkdir(parents=True)
    (private / "SENTINEL").write_text(
        "PRIVATE_STATE_SENTINEL_DET1\n", encoding="utf-8")


def test_doctor_det1_deterministic_rows_cite_child_config(
        tmp_path, monkeypatch, capsys):
    """DET1 twin: api parallel ✓ (4 workers), selection ✗ gap, timing ?.

    The child ``.ptest.toml`` files are tier-0 evidence, so no deterministic
    satisfied/gap row downgrades for a missing citation; citations carry the
    exact ``[selection]``/``[runner]`` line ranges with valid identities;
    private ``.ptest/`` state never reaches any output.
    """
    root = tmp_path / "det1-mono"
    root.mkdir()
    _write_det1_shaped_monorepo(root)
    bindir = tmp_path / "bin"
    _prepare_review(monkeypatch, root, bindir)
    monkeypatch.setenv("PTEST_RECOMMENDATIONS_LOCK_DIR",
                       str(tmp_path / "locks"))

    assert main(("doctor", "--reviewer", "claude",
                 "--allow-model-review")) == 0
    human = capsys.readouterr()
    assert "✓ Parallel execution" in human.out
    assert "? Parallel execution" not in human.out
    assert "✗ Test selection" in human.out
    assert ("? Test timing  no timing history yet: "
            "run ptest --full once") in human.out
    assert "(the ptest config is not in the review evidence)" not in human.out
    assert "PRIVATE_STATE_SENTINEL_DET1" not in human.out

    launches = _read_launches(bindir)
    assert "PARALLEL-001" not in [entry["item"] for entry in launches]

    report = (root / "recommendations.md").read_text(encoding="utf-8")
    assert "PRIVATE_STATE_SENTINEL_DET1" not in report

    assert main(("doctor", "--reviewer", "claude", "--allow-model-review",
                 "--json")) == 0
    out = capsys.readouterr().out
    assert "PRIVATE_STATE_SENTINEL_DET1" not in out
    document = C.decode_public_document(out.encode("utf-8"))
    assert document.kind == "agent-assessment" and document.error is None
    by_scope = {child["scope"]: child
                for child in document.data["children"]}
    api_rows = {row["id"]: row for row in by_scope["api"]["rows"]}
    parallel = api_rows["PARALLEL-001"]
    assert parallel["status"] == "satisfied"
    assert "4 workers" in parallel["rationale"]
    assert len(parallel["evidence"]) == 2
    assert parallel["evidence"][0]["path"] == "api/.ptest.toml"
    runner_lines = (
        parallel["evidence"][0]["start_line"],
        parallel["evidence"][0]["end_line"])
    assert runner_lines == (3, 10)
    assert parallel["evidence"][1]["path"] == "api/pyproject.toml"
    assert (parallel["evidence"][1]["start_line"],
            parallel["evidence"][1]["end_line"]) == (2, 2)
    selection = api_rows["SELECT-001"]
    assert selection["status"] == "gap"
    assert len(selection["evidence"]) == 1
    assert selection["evidence"][0]["path"] == "api/.ptest.toml"
    assert (selection["evidence"][0]["start_line"],
            selection["evidence"][0]["end_line"]) == (11, 12)
    findings = {finding["id"]: finding
                for finding in by_scope["api"]["findings"]}
    assert "SELECT-001" in findings
    assert "PARALLEL-001" not in findings
    assert api_rows["TIMING-001"]["status"] == "unknown"
    assert "no timing history yet" in api_rows["TIMING-001"]["rationale"]
    # Web (vitest): no automatic selection, so n/a by design, still cited.
    # The n/a is justified by kind = "vitest" in [runner], not [selection].
    web_rows = {row["id"]: row for row in by_scope["web"]["rows"]}
    assert web_rows["SELECT-001"]["status"] == "not-applicable"
    assert len(web_rows["SELECT-001"]["evidence"]) == 1
    assert web_rows["SELECT-001"]["evidence"][0]["path"] == "web/.ptest.toml"
    assert (web_rows["SELECT-001"]["evidence"][0]["start_line"],
            web_rows["SELECT-001"]["evidence"][0]["end_line"]) == (3, 10)
    assert web_rows["PARALLEL-001"]["status"] == "satisfied"
