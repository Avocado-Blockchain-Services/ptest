"""Real candidate subprocess smoke for explicit Pytest full mode."""
from __future__ import annotations

import json
import os
import sys
import subprocess

import pytest


@pytest.fixture(autouse=True)
def _clear_native_pytest_environment(monkeypatch):
    """Keep committed-Git child runs independent of an outer pytest run."""
    for name in (
        "PYTEST_ADDOPTS", "PYTHONDONTWRITEBYTECODE", "PTEST_EXECUTION",
        "PTEST_RUN_ID", "PTEST_GRANT_NONCE",
        "PTEST_PYTEST_REPORT_PATH", "PTEST_PYTEST_ATTEMPT",
        "PTEST_PYTEST_EXECUTION", "PTEST_PYTEST_CHECKOUT_ROOT",
        "PTEST_PYTEST_CONFIG_PATH",
    ):
        monkeypatch.delenv(name, raising=False)


def _commit_fixture(root):
    """Make lifecycle evidence real: these tests must run from committed Git."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(
        GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
        GIT_AUTHOR_NAME="Fixture", GIT_COMMITTER_NAME="Fixture",
        GIT_AUTHOR_EMAIL="fixture@example.test", GIT_COMMITTER_EMAIL="fixture@example.test",
    )
    for args in (("init",), ("config", "user.email", "fixture@example.test"),
                 ("config", "user.name", "Fixture"), ("add", "."),
                 ("commit", "-m", "initial")):
        subprocess.run(("git", "-c", "core.hooksPath=" + os.devnull,
                        "-c", "commit.gpgsign=false", "-C", str(root), *args),
                       env=env, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def test_non_git_pytest_full_preserves_native_zero_but_is_incomplete(case):
    domain = case.domain(slots=1, jobs=1)
    root = case.project(domain, kind="pytest")
    project_id = (root / ".ptest.toml").read_text().split('project_id = "', 1)[1].split('"', 1)[0]
    (root / "tests").mkdir()
    (root / "tests" / "test_full.py").write_text("def test_native_full():\n    assert True\n")
    (root / ".ptest.toml").write_text(
        "version = 1\nproject_id = \"" + project_id + "\"\n[runner]\n"
        "kind = \"pytest\"\nlauncher = " + json.dumps([sys.executable]) + "\n"
        "args = [\"-q\", \"-p\", \"no:xdist\"]\nfull_args = []\ntest_roots = [\"tests\"]\nworkers = 8\n"
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


@pytest.mark.parametrize("source", ["env", "ini", "env-combined", "ini-combined"])
def test_git_full_addopts_controls_are_ptest_refusals(case, source):
    domain = case.domain(slots=1, jobs=1)
    root = case.project(domain, kind="pytest")
    project_id = (root / ".ptest.toml").read_text().split('project_id = "', 1)[1].split('"', 1)[0]
    (root / "tests").mkdir()
    (root / "tests" / "test_native.py").write_text(
        "from pathlib import Path\n"
        "def test_body():\n    Path('body.marker').write_text('ran')\n")
    (root / "pytest.ini").write_text("[pytest]\ncache_dir = .pytest_cache\n")
    env = None
    if source in {"env", "env-combined"}:
        env = {"PYTEST_ADDOPTS": "-k hidden" if source == "env" else "-qc alt.ini"}
    else:
        addopts = ("--deselect=tests/test_native.py::test_body"
                   if source == "ini" else "-qc alt.ini")
        (root / "pytest.ini").write_text(
            "[pytest]\ncache_dir = .pytest_cache\naddopts = " + addopts + "\n")
    (root / ".ptest.toml").write_text(
        "version = 1\nproject_id = \"" + project_id + "\"\n[runner]\n"
        "kind = \"pytest\"\nlauncher = " + json.dumps([sys.executable]) + "\n"
        "args = [\"-q\", \"-p\", \"no:xdist\"]\nfull_args = []\ntest_roots = [\"tests\"]\nworkers = 1\n"
        "lifecycle = \"cooperative-process-group\"\n"
        "[selection]\nnon_input_outputs = [\"body.marker\"]\n")
    _commit_fixture(root)

    completed = case.invoke(domain, root, "--full", env=env, timeout=20)

    assert completed.code == 4
    assert b"ptest-bridge-refusal" in completed.stderr
    assert completed.result is not None
    assert completed.result["data"]["status"] == "incomplete"
    assert completed.result["data"]["exit_origin"] == "ptest"
    assert not (root / "body.marker").exists()


def test_git_pytest_full_allows_root_cache_and_assertion_bytecode(case):
    domain = case.domain(slots=1, jobs=1)
    root = case.project(domain, kind="pytest")
    project_id = (root / ".ptest.toml").read_text().split('project_id = "', 1)[1].split('"', 1)[0]
    (root / "tests").mkdir()
    (root / "src").mkdir()
    (root / "src" / "__init__.py").write_text("")
    (root / "src" / "helper.py").write_text("VALUE = 7\n")
    (root / "conftest.py").write_text("from src.helper import VALUE\n")
    (root / "tests" / "test_full.py").write_text(
        "from src.helper import VALUE\n"
        "def test_native_full():\n    assert VALUE == 7\n")
    (root / "pytest.ini").write_text("[pytest]\ncache_dir = .pytest_cache\n")
    (root / ".ptest.toml").write_text(
        "version = 1\nproject_id = \"" + project_id + "\"\n[runner]\n"
        "kind = \"pytest\"\nlauncher = " + json.dumps([sys.executable]) + "\n"
        "args = [\"-q\", \"-p\", \"no:xdist\"]\nfull_args = []\ntest_roots = [\"tests\"]\nworkers = 1\n"
        "lifecycle = \"cooperative-process-group\"\n")
    _commit_fixture(root)
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
        "args = [\"-q\", \"-p\", \"no:xdist\"]\nfull_args = []\ntest_roots = [\"nested/tests\"]\nworkers = 1\n"
        "lifecycle = \"cooperative-process-group\"\n")
    _commit_fixture(root)
    completed = case.invoke(domain, root, "--full", timeout=20)
    assert completed.code == 70
    assert (nested / ".pytest_cache").is_dir()
    assert completed.result["data"]["status"] == "incomplete"


@pytest.mark.parametrize("hook_source", [
    "def pytest_collection_modifyitems(session, config, items):\n    pass\n",
    (
        "import pytest\n"
        "@pytest.hookimpl(wrapper=True)\n"
        "def pytest_collection_modifyitems(session, config, items):\n"
        "    yield\n"
    ),
    "def pytest_runtest_makereport(item, call):\n    pass\n",
    (
        "import pytest\n"
        "@pytest.hookimpl(wrapper=True)\n"
        "def pytest_runtest_makereport(item, call):\n"
        "    yield\n"
    ),
    "def pytest_report_teststatus(report):\n    pass\n",
    (
        "import pytest\n"
        "@pytest.hookimpl(wrapper=True)\n"
        "def pytest_report_teststatus(report):\n"
        "    yield\n"
    ),
    "def pytest_sessionfinish(session, exitstatus):\n    pass\n",
    (
        "import pytest\n"
        "@pytest.hookimpl(wrapper=True)\n"
        "def pytest_sessionfinish(session, exitstatus):\n"
        "    yield\n"
    ),
    # Only pytest_-prefixed names take effect as specname aliases: pytest's
    # plugin manager ignores marked non-pytest_ attributes before consulting
    # the marker, so this alias must keep its pytest_ prefix to be live.
    (
        "import pytest\n"
        "@pytest.hookimpl(specname='pytest_sessionfinish')\n"
        "def pytest_aliased_observer(session, exitstatus):\n"
        "    pass\n"
    ),
    (
        "import pytest\n"
        "class Late:\n"
        "    @pytest.hookimpl\n"
        "    def pytest_sessionfinish(self, session, exitstatus):\n"
        "        pass\n"
        "def pytest_collection_finish(session):\n"
        "    session.config.pluginmanager.register(Late(), 'late')\n"
    ),
    (
        "import pytest\n"
        "class Alias:\n"
        "    @pytest.hookimpl\n"
        "    def pytest_sessionfinish(self, session, exitstatus):\n"
        "        pass\n"
        "def pytest_configure(config):\n"
        "    config.pluginmanager.register(Alias(), 'alias')\n"
    ),
])
def test_full_forbidden_hook_forms_refuse_from_git(case, hook_source):
    domain = case.domain(slots=1, jobs=1)
    root = case.project(domain, kind="pytest")
    project_id = (root / ".ptest.toml").read_text().split('project_id = "', 1)[1].split('"', 1)[0]
    (root / "tests").mkdir()
    (root / "tests" / "conftest.py").write_text(hook_source)
    (root / "tests" / "test_native.py").write_text(
        "from pathlib import Path\n"
        "def test_body():\n    Path('body.marker').write_text('ran')\n")
    (root / ".ptest.toml").write_text(
        "version = 1\nproject_id = \"" + project_id + "\"\n[runner]\n"
        "kind = \"pytest\"\nlauncher = " + json.dumps([sys.executable]) + "\n"
        "args = [\"-q\", \"-p\", \"no:xdist\"]\nfull_args = []\ntest_roots = [\"tests\"]\nworkers = 1\n"
        "lifecycle = \"cooperative-process-group\"\n")
    _commit_fixture(root)

    completed = case.invoke(domain, root, "--full", timeout=20)

    assert completed.code == 4
    assert b"ptest-bridge-refusal" in completed.stderr
    assert completed.result is not None
    assert completed.result["data"]["status"] == "incomplete"
    assert completed.result["data"]["exit_origin"] == "ptest"
    assert not (root / "body.marker").exists()


def test_full_collection_finish_mutation_keeps_cooperative_claim_limits(case):
    domain = case.domain(slots=1, jobs=1)
    root = case.project(domain, kind="pytest")
    project_id = (root / ".ptest.toml").read_text().split('project_id = "', 1)[1].split('"', 1)[0]
    (root / "tests").mkdir()
    (root / "tests" / "conftest.py").write_text(
        "def pytest_collection_finish(session):\n    session.items.pop(0)\n"
    )
    (root / "tests" / "test_native.py").write_text(
        "from pathlib import Path\n"
        "def test_removed():\n    Path('removed.marker').write_text('ran')\n"
        "def test_body():\n    Path('body.marker').write_text('ran')\n"
    )
    (root / ".ptest.toml").write_text(
        "version = 1\nproject_id = \"" + project_id + "\"\n[runner]\n"
        "kind = \"pytest\"\nlauncher = " + json.dumps([sys.executable]) + "\n"
        "args = [\"-q\", \"-p\", \"no:xdist\"]\nfull_args = []\ntest_roots = [\"tests\"]\nworkers = 1\n"
        "lifecycle = \"cooperative-process-group\"\n"
        "[selection]\nnon_input_outputs = [\"body.marker\", \"removed.marker\"]\n")
    _commit_fixture(root)

    completed = case.invoke(domain, root, "--full", timeout=20)

    assert completed.code == 0
    data = completed.result["data"]
    assert data["status"] == "passed"
    assert data["counts"] is None
    assert data["full_gate_eligible"] is False
    assert data["attempts"][0]["inventory_complete"] is False
    assert any("collection-finish" in item["message"] for item in data["limitations"])
    assert not (root / "removed.marker").exists()
    assert (root / "body.marker").read_text() == "ran"


def test_full_setup_skip_keeps_cooperative_claim_limits(case):
    domain = case.domain(slots=1, jobs=1)
    root = case.project(domain, kind="pytest")
    project_id = (root / ".ptest.toml").read_text().split('project_id = "', 1)[1].split('"', 1)[0]
    (root / "tests").mkdir()
    (root / "tests" / "conftest.py").write_text(
        "import pytest\n"
        "@pytest.fixture(autouse=True)\n"
        "def skip_everything():\n    pytest.skip('cooperative fixture skip')\n"
    )
    (root / "tests" / "test_native.py").write_text(
        "from pathlib import Path\n"
        "def test_body():\n    Path('body.marker').write_text('ran')\n"
    )
    (root / ".ptest.toml").write_text(
        "version = 1\nproject_id = \"" + project_id + "\"\n[runner]\n"
        "kind = \"pytest\"\nlauncher = " + json.dumps([sys.executable]) + "\n"
        "args = [\"-q\", \"-p\", \"no:xdist\"]\nfull_args = []\ntest_roots = [\"tests\"]\nworkers = 1\n"
        "lifecycle = \"cooperative-process-group\"\n")
    _commit_fixture(root)

    completed = case.invoke(domain, root, "--full", timeout=20)

    assert completed.code == 0
    data = completed.result["data"]
    assert data["status"] == "passed"
    assert data["counts"] is None
    assert data["full_gate_eligible"] is False
    assert data["attempts"][0]["inventory_complete"] is False
    assert any("setup skips" in item["message"] for item in data["limitations"])
    assert not (root / "body.marker").exists()


def test_full_allows_terminal_summary_and_unconfigure_observation(case):
    domain = case.domain(slots=1, jobs=1)
    root = case.project(domain, kind="pytest")
    project_id = (root / ".ptest.toml").read_text().split('project_id = "', 1)[1].split('"', 1)[0]
    (root / "tests").mkdir()
    (root / "tests" / "conftest.py").write_text(
        "from pathlib import Path\n"
        "def pytest_terminal_summary(terminalreporter, exitstatus, config):\n    print('terminal-observed')\n"
        "def pytest_unconfigure(config):\n    Path('cleanup.marker').write_text('done')\n")
    (root / "tests" / "test_native.py").write_text(
        "from pathlib import Path\n"
        "def test_body():\n    Path('body.marker').write_text('ran')\n")
    (root / ".ptest.toml").write_text(
        "version = 1\nproject_id = \"" + project_id + "\"\n[runner]\n"
        "kind = \"pytest\"\nlauncher = " + json.dumps([sys.executable]) + "\n"
        "args = [\"-q\", \"-p\", \"no:xdist\"]\nfull_args = []\ntest_roots = [\"tests\"]\nworkers = 1\n"
        "lifecycle = \"cooperative-process-group\"\n"
        "[selection]\nnon_input_outputs = [\"body.marker\", \"cleanup.marker\"]\n")
    _commit_fixture(root)
    completed = case.invoke(domain, root, "--full", timeout=20)
    assert b"terminal-observed" in completed.stdout
    assert (root / "cleanup.marker").read_text() == "done"
    assert (root / "body.marker").read_text() == "ran"
    assert completed.code == 0
    assert completed.result["data"]["status"] == "passed"


def test_full_unknown_input_refuses_before_launch(case):
    domain = case.domain(slots=1, jobs=1)
    root = case.project(domain, kind="pytest")
    project_id = (root / ".ptest.toml").read_text().split('project_id = "', 1)[1].split('"', 1)[0]
    (root / "tests").mkdir(); (root / "tests" / "test_fail.py").write_text("def test_failure():\n    assert False\n")
    (root / ".ptest.toml").write_text(
        "version = 1\nproject_id = \"" + project_id + "\"\n[runner]\n"
        "kind = \"pytest\"\nlauncher = " + json.dumps([sys.executable]) + "\n"
        "args = [\"-q\", \"-p\", \"no:xdist\"]\nfull_args = []\ntest_roots = [\"tests\"]\nworkers = 1\n"
        "lifecycle = \"cooperative-process-group\"\n")
    completed = case.invoke(domain, root, "--full", timeout=20)
    assert completed.code == 70
    assert completed.result["data"]["runner_exit_code"] is None
    assert completed.result["data"]["status"] == "incomplete"


def test_no_selection_full_preserves_native_failure_after_known_input(case):
    domain = case.domain(slots=1, jobs=1)
    root = case.project(domain, kind="pytest")
    project_id = (root / ".ptest.toml").read_text().split('project_id = "', 1)[1].split('"', 1)[0]
    (root / "tests").mkdir()
    (root / "tests" / "test_fail.py").write_text("def test_failure():\n    assert False\n")
    (root / ".ptest.toml").write_text(
        "version = 1\nproject_id = \"" + project_id + "\"\n[runner]\n"
        "kind = \"pytest\"\nlauncher = " + json.dumps([sys.executable]) + "\n"
        "args = [\"-q\", \"-p\", \"no:xdist\"]\nfull_args = []\ntest_roots = [\"tests\"]\nworkers = 1\n"
        "lifecycle = \"cooperative-process-group\"\n"
        "[selection]\nenabled = false\nclosed_inputs = false\n")
    _commit_fixture(root)
    completed = case.invoke(domain, root, "--full", timeout=20)
    assert completed.code == 1
    assert completed.result["data"]["runner_exit_code"] == 1
    assert completed.result["data"]["status"] == "failed"
    assert completed.result["data"]["full_gate_eligible"] is False


def test_git_pytest_full_preserves_safe_strict_controls(case):
    domain = case.domain(slots=1, jobs=1)
    root = case.project(domain, kind="pytest")
    project_id = (root / ".ptest.toml").read_text().split('project_id = "', 1)[1].split('"', 1)[0]
    (root / "tests").mkdir()
    (root / "tests" / "test_native.py").write_text(
        "from pathlib import Path\n"
        "def test_body():\n    Path('body.marker').write_text('ran')\n")
    (root / "pytest.ini").write_text("[pytest]\ncache_dir = .pytest_cache\n")
    (root / ".ptest.toml").write_text(
        "version = 1\nproject_id = \"" + project_id + "\"\n[runner]\n"
        "kind = \"pytest\"\nlauncher = " + json.dumps([sys.executable]) + "\n"
        "args = [\"-q\", \"-p\", \"no:xdist\"]\nfull_args = [\"--strict-markers\", \"--strict-config\", \"--strict\"]\n"
        "test_roots = [\"tests\"]\nworkers = 1\n"
        "lifecycle = \"cooperative-process-group\"\n"
        "[selection]\nnon_input_outputs = [\"body.marker\"]\n")
    _commit_fixture(root)
    completed = case.invoke(domain, root, "--full", timeout=20)
    assert completed.code == 0
    assert completed.result is not None
    data = completed.result["data"]
    assert data["status"] == "passed"
    assert data["exit_origin"] == "runner"
    assert (root / "body.marker").read_text() == "ran"


@pytest.mark.parametrize("source", ["env", "ini"])
def test_git_full_arbitrary_override_ini_remains_refused(case, source):
    domain = case.domain(slots=1, jobs=1)
    root = case.project(domain, kind="pytest")
    project_id = (root / ".ptest.toml").read_text().split('project_id = "', 1)[1].split('"', 1)[0]
    (root / "tests").mkdir()
    (root / "tests" / "test_native.py").write_text(
        "from pathlib import Path\n"
        "def test_body():\n    Path('body.marker').write_text('ran')\n")
    (root / "pytest.ini").write_text("[pytest]\ncache_dir = .pytest_cache\n")
    env = None
    if source == "env":
        env = {"PYTEST_ADDOPTS": "-o cache_dir=/tmp/elsewhere"}
    else:
        (root / "pytest.ini").write_text(
            "[pytest]\ncache_dir = .pytest_cache\naddopts = -o cache_dir=/tmp/elsewhere\n")
    (root / ".ptest.toml").write_text(
        "version = 1\nproject_id = \"" + project_id + "\"\n[runner]\n"
        "kind = \"pytest\"\nlauncher = " + json.dumps([sys.executable]) + "\n"
        "args = [\"-q\", \"-p\", \"no:xdist\"]\nfull_args = []\ntest_roots = [\"tests\"]\nworkers = 1\n"
        "lifecycle = \"cooperative-process-group\"\n"
        "[selection]\nnon_input_outputs = [\"body.marker\"]\n")
    _commit_fixture(root)
    completed = case.invoke(domain, root, "--full", env=env, timeout=20)
    assert completed.code == 4
    assert b"ptest-bridge-refusal" in completed.stderr
    assert completed.result is not None
    assert completed.result["data"]["status"] == "incomplete"
    assert completed.result["data"]["exit_origin"] == "ptest"
    assert not (root / "body.marker").exists()


def test_git_full_over_budget_fixture_is_incomplete_with_scan_limit(case):
    domain = case.domain(slots=1, jobs=1)
    root = case.project(domain, kind="pytest")
    project_id = (root / ".ptest.toml").read_text().split('project_id = "', 1)[1].split('"', 1)[0]
    (root / "tests").mkdir()
    (root / "tests" / "test_native.py").write_text(
        "from pathlib import Path\n"
        "def test_body():\n    Path('body.marker').write_text('ran')\n")
    (root / "pytest.ini").write_text("[pytest]\ncache_dir = .pytest_cache\n")
    (root / ".ptest.toml").write_text(
        "version = 1\nproject_id = \"" + project_id + "\"\n[runner]\n"
        "kind = \"pytest\"\nlauncher = " + json.dumps([sys.executable]) + "\n"
        "args = [\"-q\", \"-p\", \"no:xdist\"]\nfull_args = []\ntest_roots = [\"tests\"]\nworkers = 1\n"
        "lifecycle = \"cooperative-process-group\"\n"
        "[selection]\nnon_input_outputs = [\"body.marker\"]\n")
    _commit_fixture(root)
    with (root / "payload.bin").open("wb") as stream:
        stream.truncate(17 * 1024 * 1024)
    completed = case.invoke(domain, root, "--full", timeout=20)
    assert completed.code == 70
    assert completed.result is not None
    data = completed.result["data"]
    assert data["status"] == "incomplete"
    assert any(item["code"] == "scan-limit" for item in data["limitations"])
    # The unchanged unknown-input first gate refuses before launching the
    # child, so the test body cannot create its marker.
    assert not (root / "body.marker").exists()
