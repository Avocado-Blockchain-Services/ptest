"""Post-merge integration: init notes plus the wired doctor v2 review flow.

Drives ``cli.main`` with the real config, executability, init_render,
agent_assessment, render, recommendations, contracts, and agent_providers
code. Nothing in those modules is monkeypatched. The only fakes are
provider executables on a temporary ``PATH`` (a ``claude``-shaped fake
that answers one one-row reply per item request, logs its argv beside
itself, and exits nonzero for item ids listed in a sibling control
file), plus ``stdin.isatty``/``input`` where a TTY is needed. No
network, no real provider; each fake finishes in well under a second.
"""
from __future__ import annotations

import json
import os
import sys

from ptest import contracts as C
from ptest.cli import main


def _write_child_config(path, kind, launcher, extra=""):
    (path / ".ptest.toml").write_text(
        'version = 1\nproject_id = "abababababababababababababababab"\n'
        "[runner]\n"
        f'kind = "{kind}"\n'
        f"launcher = {launcher}\n"
        "args = []\n"
        "full_args = []\n"
        'test_roots = ["tests"]\n'
        "workers = 1\n"
        'lifecycle = "cooperative-process-group"\n' + extra,
        encoding="utf-8",
    )


def _write_persea_shaped_monorepo(root):
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
        "addopts = '-n 4 --dist=loadgroup -m \"not slow\"'\n",
        encoding="utf-8",
    )
    (api / "tests" / "conftest.py").write_text(
        "def pytest_sessionfinish(session, exitstatus):\n    return None\n",
        encoding="utf-8",
    )
    _write_child_config(api, "pytest", '["python"]')
    (web / "tests" / "a.test.ts").write_text(
        "export {};\n", encoding="utf-8")
    _write_child_config(
        web, "vitest", '["node"]',
        extra="[setup]\n"
              'argv = ["npm", "ci"]\n'
              'required_paths = ["node_modules"]\n'
              "network = true\nlifecycle_scripts = true\n",
    )


def _write_db_standalone_repo(root):
    (root / ".ptest.toml").write_text(
        'version = 1\nproject_id = "cdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcd"\n'
        "[runner]\n"
        'kind = "pytest"\n'
        'launcher = ["python"]\n'
        "args = []\n"
        "full_args = []\n"
        'test_roots = ["tests"]\n'
        "workers = 1\n"
        'lifecycle = "cooperative-process-group"\n',
        encoding="utf-8",
    )
    tests = root / "tests"
    tests.mkdir()
    (tests / "test_example.py").write_text(
        "def test_example():\n    assert True\n", encoding="utf-8")
    (tests / "conftest.py").write_text(
        "import sqlalchemy\n"
        "\n"
        "\n"
        'ENGINE = sqlalchemy.create_engine("sqlite://")\n'
        "\n"
        "\n"
        "def uses_database():\n"
        "    return ENGINE\n",
        encoding="utf-8",
    )


def _install_fake_claude(bindir):
    """Claude-shaped fake answering one one-row reply per item request.

    Control and log files live beside the executable (``argv[0]``), so
    no environment variable must cross the sanitized provider boundary:
    ``fail-items.txt`` holds one item id per line (``*`` fails every
    item); every review launch appends ``{"argv", "item"}`` to
    ``argv.log``. ``--version`` answers without touching the log.
    """
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
        "fail = set()",
        "try:",
        "    with open(os.path.join(here, 'fail-items.txt'),",
        "              encoding='utf-8') as handle:",
        "        fail = {line.strip() for line in handle if line.strip()}",
        "except OSError:",
        "    pass",
        "if '*' in fail or item_id in fail:",
        "    sys.exit(3)",
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


def _prepare_review(monkeypatch, root, bindir):
    monkeypatch.chdir(root)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.delenv("CI", raising=False)
    _install_fake_claude(bindir)
    monkeypatch.setenv(
        "PATH",
        str(bindir) + os.pathsep + os.environ.get("PATH", ""))


def _read_launches(bindir):
    log = bindir / "argv.log"
    if not log.exists():
        return []
    return [json.loads(line) for line in
            log.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_init_persea_shaped_monorepo_reports_projects_and_fix(
        tmp_path, monkeypatch, capsys):
    root = tmp_path / "monorepo"
    root.mkdir()
    _write_persea_shaped_monorepo(root)
    monkeypatch.chdir(root)

    assert main(("init", "--no-doctor", "--agents", "none")) == 0

    out = capsys.readouterr().out
    flat = " ".join(out.split())
    # The parallel tier deleted the old not-runnable rule: ptest itself
    # generates `-n 0` for serial xdist runs, so an xdist project stays
    # runnable and init never tells the user to write `-n 0`.
    assert "not runnable" not in flat
    assert '"-n", "0"' not in out
    assert "runs: yes · parallel: no" in out
    assert "parallel: inside vitest" in out


def test_doctor_review_runs_one_haiku_call_per_item(
        tmp_path, monkeypatch, capsys):
    from ptest.checklist import CATALOG

    root = tmp_path / "review"
    root.mkdir()
    _write_db_standalone_repo(root)
    bindir = tmp_path / "bin"
    _prepare_review(monkeypatch, root, bindir)
    monkeypatch.setenv("PTEST_RECOMMENDATIONS_LOCK_DIR",
                       str(tmp_path / "locks"))

    argv = ("doctor", "--reviewer", "claude", "--allow-model-review")
    assert main(argv) == 0

    launches = _read_launches(bindir)
    # Deterministic answers take no model call; every other item takes
    # exactly one provider request.
    launched = {item["item"] for item in launches}
    assert "TIMING-001" not in launched
    assert "SELECT-001" not in launched
    assert launched == {entry.id for entry in CATALOG} - {
        "TIMING-001", "SELECT-001", "PARALLEL-001"}
    assert len(launches) == len(launched) == 9
    assert all(item["argv"][-2:] == ["--model", "haiku"]
               for item in launches)

    human = capsys.readouterr()
    assert ".  pytest · 9 ok · 2 gap · 1 unknown" in human.out
    assert "runs: yes · parallel: no" in human.out
    for entry in CATALOG:
        assert entry.label in human.out
    assert "✗ Test selection" in human.out
    assert ("? Test timing  no timing history yet: "
            "run ptest --full once") in human.out
    assert "recommendations.md (created)" in human.out

    report = (root / "recommendations.md").read_text(encoding="utf-8")
    for entry in CATALOG:
        assert f"{entry.id} {entry.label}" in report
    # The not-configured PARALLEL-001 fix is finalized after the model
    # replies: no safety gap here, so the report carries the enabling
    # suggestion (a no-op finalizer would leave the safety-first text).
    assert ("Add pytest-xdist to the project environment and request "
            "workers with -n auto in the pytest configuration.") in report
    assert "Resolve the parallel-safety gaps first." not in report

    assert main((*argv, "--json")) == 0
    document = C.decode_public_document(
        capsys.readouterr().out.encode("utf-8"))
    assert document.kind == "agent-assessment" and document.error is None
    child = document.data["children"][0]
    assert child["execution"] == {
        "status": "caveat",
        "detail": "parallel: no — xdist is not enabled in your pytest config",
        "fix": None,
    }
    assert all(row["label"] for row in child["rows"])
    assert document.data["provider"]["profile"] == (
        "ptest-item-review-v1 model=haiku")


def test_doctor_review_contains_single_item_failure(tmp_path, monkeypatch,
                                                   capsys):
    root = tmp_path / "one-failure"
    root.mkdir()
    _write_db_standalone_repo(root)
    bindir = tmp_path / "bin"
    _prepare_review(monkeypatch, root, bindir)
    monkeypatch.setenv("PTEST_RECOMMENDATIONS_LOCK_DIR",
                       str(tmp_path / "locks"))
    (bindir / "fail-items.txt").write_text("FIX-001\n", encoding="utf-8")

    assert main(("doctor", "--reviewer", "claude",
                 "--allow-model-review")) == 0

    human = capsys.readouterr()
    assert "? Test data factories" in human.out
    assert "review failed: provider exited with an error" in human.out
    assert (root / "recommendations.md").is_file()


def test_doctor_review_fails_when_every_item_fails(tmp_path, monkeypatch,
                                                  capsys):
    root = tmp_path / "all-fail"
    root.mkdir()
    _write_db_standalone_repo(root)
    bindir = tmp_path / "bin"
    _prepare_review(monkeypatch, root, bindir)
    monkeypatch.setenv("PTEST_RECOMMENDATIONS_LOCK_DIR",
                       str(tmp_path / "locks"))
    (bindir / "fail-items.txt").write_text("*\n", encoding="utf-8")

    assert main(("doctor", "--reviewer", "claude", "--allow-model-review",
                 "--json")) == 2

    document = C.decode_public_document(
        capsys.readouterr().out.encode("utf-8"))
    assert document.data is None
    assert document.error.code == "provider-failed"
    assert not (root / "recommendations.md").exists()


def test_doctor_review_launches_nothing_before_consent(tmp_path, monkeypatch,
                                                      capsys):
    root = tmp_path / "declined"
    root.mkdir()
    _write_db_standalone_repo(root)
    bindir = tmp_path / "bin"
    _prepare_review(monkeypatch, root, bindir)
    monkeypatch.setenv("PTEST_RECOMMENDATIONS_LOCK_DIR",
                       str(tmp_path / "locks"))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda: "no")

    assert main(("doctor", "--reviewer", "claude",
                 "--allow-model-review")) == 0

    captured = capsys.readouterr()
    assert not (bindir / "argv.log").exists()
    assert "review not yet performed" in captured.out


def test_doctor_review_computes_executability_once(
        tmp_path, monkeypatch, capsys):
    """One executability pass per doctor run, shared by facts and answers.

    The review flow shares a single ``check_resolution`` across the
    execution/project facts and the deterministic answers (which reuse
    the facts instead of calling ``check_config`` again per packet).
    """
    from ptest import executability as exec_module

    root = tmp_path / "once"
    root.mkdir()
    _write_db_standalone_repo(root)
    bindir = tmp_path / "bin"
    _prepare_review(monkeypatch, root, bindir)
    monkeypatch.setenv("PTEST_RECOMMENDATIONS_LOCK_DIR",
                       str(tmp_path / "locks"))

    resolutions = []
    real_resolution = exec_module.check_resolution

    def counting_resolution(resolution):
        resolutions.append(1)
        return real_resolution(resolution)

    configs = []
    real_config = exec_module.check_config

    def counting_config(config, **kwargs):
        configs.append(1)
        return real_config(config, **kwargs)

    monkeypatch.setattr(exec_module, "check_resolution", counting_resolution)
    monkeypatch.setattr(exec_module, "check_config", counting_config)

    assert main(("doctor", "--reviewer", "claude",
                 "--allow-model-review")) == 0
    assert len(resolutions) == 1
    assert len(configs) == 1


def test_doctor_select_fix_orders_coverage_profile_on_parallel_project(
        tmp_path, monkeypatch, capsys):
    """Twin: cli.main doctor on an xdist project orders the coverage profile.

    SELECT-001's fix on a parallel-active project names --cov/--cov-report
    and a [selection] policy; coverage runs in parallel, so there is no
    serial tradeoff to state.
    """
    root = tmp_path / "parallel-select"
    root.mkdir()
    (root / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\naddopts = '-n 4'\n", encoding="utf-8")
    (root / ".ptest.toml").write_text(
        'version = 1\nproject_id = "' + "dd" * 16 + '"\n'
        "[runner]\n"
        'kind = "pytest"\n'
        f"launcher = {json.dumps([sys.executable])}\n"
        "args = []\n"
        "full_args = []\n"
        'test_roots = ["tests"]\n'
        "workers = 1\n"
        'lifecycle = "cooperative-process-group"\n',
        encoding="utf-8",
    )
    tests = root / "tests"
    tests.mkdir()
    (tests / "test_example.py").write_text(
        "def test_example():\n    assert True\n", encoding="utf-8")
    bindir = tmp_path / "bin"
    _prepare_review(monkeypatch, root, bindir)
    monkeypatch.setenv("PTEST_RECOMMENDATIONS_LOCK_DIR",
                       str(tmp_path / "locks"))

    assert main(("doctor", "--reviewer", "claude",
                 "--allow-model-review")) == 0

    flat = " ".join(capsys.readouterr().out.split())
    assert "add --cov" in flat
    assert "[selection]" in flat
    assert "runs serially under ptest" not in flat


def test_doctor_qualified_cov_states_no_tradeoff_both_items(
        tmp_path, monkeypatch, capsys):
    """Twin: qualified xdist + --cov in runner args stays consistent.

    SELECT-001 names the [selection] policy and PARALLEL-001 is satisfied;
    neither states a serial tradeoff nor says pytest configuration for the
    runner args setting.
    """
    root = tmp_path / "qualified-cov"
    root.mkdir()
    (root / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\naddopts = '-n 4'\n", encoding="utf-8")
    (root / ".ptest.toml").write_text(
        'version = 1\nproject_id = "' + "dd" * 16 + '"\n'
        "[runner]\n"
        'kind = "pytest"\n'
        f"launcher = {json.dumps([sys.executable])}\n"
        'args = ["--cov", "pkg", "--cov-report", "term"]\n'
        "full_args = []\n"
        'test_roots = ["tests"]\n'
        "workers = 1\n"
        'lifecycle = "cooperative-process-group"\n',
        encoding="utf-8",
    )
    tests = root / "tests"
    tests.mkdir()
    (tests / "test_example.py").write_text(
        "def test_example():\n    assert True\n", encoding="utf-8")
    bindir = tmp_path / "bin"
    _prepare_review(monkeypatch, root, bindir)
    monkeypatch.setenv("PTEST_RECOMMENDATIONS_LOCK_DIR",
                       str(tmp_path / "locks"))

    assert main(("doctor", "--reviewer", "claude",
                 "--allow-model-review")) == 0

    flat = " ".join(capsys.readouterr().out.split())
    assert "[selection]" in flat
    assert "runs serially under ptest" not in flat
    assert "accept serial runs" not in flat
    assert "turns off ptest's test selection" not in flat
    assert "pytest configuration" not in flat
