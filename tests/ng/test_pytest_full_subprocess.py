"""Real candidate subprocess smoke for explicit Pytest full mode."""
from __future__ import annotations

import json
import sys
import subprocess


def test_non_git_pytest_full_preserves_native_zero_but_is_incomplete(case):
    domain = case.domain(slots=1, jobs=1)
    root = case.project(domain, kind="pytest")
    project_id = (root / ".ptest.toml").read_text().split('project_id = "', 1)[1].split('"', 1)[0]
    (root / "tests").mkdir()
    (root / "tests" / "test_full.py").write_text("def test_native_full():\n    assert True\n")
    (root / ".ptest.toml").write_text(
        "version = 1\nproject_id = \"" + project_id + "\"\n[runner]\n"
        "kind = \"pytest\"\nlauncher = " + json.dumps([sys.executable]) + "\n"
        "args = [\"-q\"]\nfull_args = []\ntest_roots = [\"tests\"]\nworkers = 8\n"
        "lifecycle = \"cooperative-process-group\"\n")
    completed = case.invoke(domain, root, "--full", timeout=20)
    assert completed.code == 70
    assert completed.result is not None
    data = completed.result["data"]
    assert data["plan"]["execution"] == "full"
    assert data["granted_workers"] == 1
    assert data["counts"] is None
    assert data["full_gate_eligible"] is False
    assert data["status"] == "incomplete"


def test_git_pytest_full_allows_root_cache_and_assertion_bytecode(case):
    domain = case.domain(slots=1, jobs=1)
    root = case.project(domain, kind="pytest")
    project_id = (root / ".ptest.toml").read_text().split('project_id = "', 1)[1].split('"', 1)[0]
    (root / "tests").mkdir()
    (root / "tests" / "test_full.py").write_text("def test_native_full():\n    assert True\n")
    (root / "pytest.ini").write_text("[pytest]\ncache_dir = .pytest_cache\n")
    (root / ".ptest.toml").write_text(
        "version = 1\nproject_id = \"" + project_id + "\"\n[runner]\n"
        "kind = \"pytest\"\nlauncher = " + json.dumps([sys.executable]) + "\n"
        "args = [\"-q\"]\nfull_args = []\ntest_roots = [\"tests\"]\nworkers = 1\n"
        "lifecycle = \"cooperative-process-group\"\n")
    for args in (("init",), ("config", "user.email", "fixture@example.test"),
                 ("config", "user.name", "Fixture"), ("add", "."), ("commit", "-m", "initial")):
        subprocess.run(("git", "-C", str(root), *args), check=True, stdout=subprocess.PIPE)
    first = case.invoke(domain, root, "--full", timeout=20)
    second = case.invoke(domain, root, "--full", timeout=20)
    assert first.code == second.code == 0
    assert (root / ".pytest_cache" / "CACHEDIR.TAG").is_file()
    assert any((root / "tests" / "__pycache__").glob("test_full.cpython-*.pyc"))


def test_nested_native_cache_remains_input_and_makes_full_incomplete(case):
    domain = case.domain(slots=1, jobs=1)
    root = case.project(domain, kind="pytest")
    project_id = (root / ".ptest.toml").read_text().split('project_id = "', 1)[1].split('"', 1)[0]
    nested = root / "nested"; (nested / "tests").mkdir(parents=True)
    (nested / "pytest.ini").write_text("[pytest]\n")
    (nested / "tests" / "test_native.py").write_text("def test_nested():\n    assert True\n")
    (root / ".ptest.toml").write_text(
        "version = 1\nproject_id = \"" + project_id + "\"\n[runner]\n"
        "kind = \"pytest\"\nlauncher = " + json.dumps([sys.executable]) + "\n"
        "args = [\"-q\"]\nfull_args = []\ntest_roots = [\"nested/tests\"]\nworkers = 1\n"
        "lifecycle = \"cooperative-process-group\"\n")
    for args in (("init",), ("config", "user.email", "fixture@example.test"),
                 ("config", "user.name", "Fixture"), ("add", "."), ("commit", "-m", "initial")):
        subprocess.run(("git", "-C", str(root), *args), check=True, stdout=subprocess.PIPE)
    completed = case.invoke(domain, root, "--full", timeout=20)
    assert completed.code == 70
    assert (nested / ".pytest_cache").is_dir()
    assert completed.result["data"]["status"] == "incomplete"


def test_full_forbidden_plain_hook_cannot_certify_a_result(case):
    domain = case.domain(slots=1, jobs=1)
    root = case.project(domain, kind="pytest")
    project_id = (root / ".ptest.toml").read_text().split('project_id = "', 1)[1].split('"', 1)[0]
    (root / "tests").mkdir()
    (root / "tests" / "conftest.py").write_text("def pytest_sessionfinish(session, exitstatus):\n    pass\n")
    (root / "tests" / "test_native.py").write_text("def test_body():\n    assert True\n")
    (root / ".ptest.toml").write_text(
        "version = 1\nproject_id = \"" + project_id + "\"\n[runner]\n"
        "kind = \"pytest\"\nlauncher = " + json.dumps([sys.executable]) + "\n"
        "args = [\"-q\"]\nfull_args = []\ntest_roots = [\"tests\"]\nworkers = 1\n"
        "lifecycle = \"cooperative-process-group\"\n")
    completed = case.invoke(domain, root, "--full", timeout=20)
    assert completed.code != 0
    assert completed.result is not None
    assert completed.result["data"]["status"] == "incomplete"


def test_full_allows_terminal_summary_and_unconfigure_observation(case):
    domain = case.domain(slots=1, jobs=1)
    root = case.project(domain, kind="pytest")
    project_id = (root / ".ptest.toml").read_text().split('project_id = "', 1)[1].split('"', 1)[0]
    (root / "tests").mkdir()
    (root / "tests" / "conftest.py").write_text(
        "from pathlib import Path\n"
        "def pytest_terminal_summary(terminalreporter, exitstatus, config):\n    print('terminal-observed')\n"
        "def pytest_unconfigure(config):\n    Path('cleanup.marker').write_text('done')\n")
    (root / "tests" / "test_native.py").write_text("def test_body():\n    assert True\n")
    (root / ".ptest.toml").write_text(
        "version = 1\nproject_id = \"" + project_id + "\"\n[runner]\n"
        "kind = \"pytest\"\nlauncher = " + json.dumps([sys.executable]) + "\n"
        "args = [\"-q\"]\nfull_args = []\ntest_roots = [\"tests\"]\nworkers = 1\n"
        "lifecycle = \"cooperative-process-group\"\n")
    completed = case.invoke(domain, root, "--full", timeout=20)
    assert b"terminal-observed" in completed.stdout
    assert (root / "cleanup.marker").read_text() == "done"
    assert completed.result["data"]["status"] == "incomplete"


def test_full_preserves_native_failure_exit(case):
    domain = case.domain(slots=1, jobs=1)
    root = case.project(domain, kind="pytest")
    project_id = (root / ".ptest.toml").read_text().split('project_id = "', 1)[1].split('"', 1)[0]
    (root / "tests").mkdir(); (root / "tests" / "test_fail.py").write_text("def test_failure():\n    assert False\n")
    (root / ".ptest.toml").write_text(
        "version = 1\nproject_id = \"" + project_id + "\"\n[runner]\n"
        "kind = \"pytest\"\nlauncher = " + json.dumps([sys.executable]) + "\n"
        "args = [\"-q\"]\nfull_args = []\ntest_roots = [\"tests\"]\nworkers = 1\n"
        "lifecycle = \"cooperative-process-group\"\n")
    completed = case.invoke(domain, root, "--full", timeout=20)
    assert completed.code == 1
    assert completed.result["data"]["runner_exit_code"] == 1
    assert completed.result["data"]["status"] == "incomplete"
