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


@pytest.mark.parametrize("source", ["env", "env-combined", "ini-combined"])
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
        (root / "pytest.ini").write_text(
            "[pytest]\ncache_dir = .pytest_cache\naddopts = -qc alt.ini\n")
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


def test_git_full_checked_in_deselect_is_project_filtered(case):
    """Section F flips the ini twin: checked-in narrowing runs labelled."""
    domain = case.domain(slots=1, jobs=1)
    root = case.project(domain, kind="pytest")
    project_id = (root / ".ptest.toml").read_text().split('project_id = "', 1)[1].split('"', 1)[0]
    (root / "tests").mkdir()
    (root / "tests" / "test_native.py").write_text(
        "from pathlib import Path\n"
        "def test_body():\n    Path('body.marker').write_text('ran')\n")
    (root / "pytest.ini").write_text(
        "[pytest]\ncache_dir = .pytest_cache\n"
        "addopts = --deselect=tests/test_native.py::test_body\n")
    (root / ".ptest.toml").write_text(
        "version = 1\nproject_id = \"" + project_id + "\"\n[runner]\n"
        "kind = \"pytest\"\nlauncher = " + json.dumps([sys.executable]) + "\n"
        "args = [\"-q\", \"-p\", \"no:xdist\"]\nfull_args = []\ntest_roots = [\"tests\"]\nworkers = 1\n"
        "lifecycle = \"cooperative-process-group\"\n"
        "[selection]\nnon_input_outputs = [\"body.marker\"]\n")
    _commit_fixture(root)

    completed = case.invoke(domain, root, "--full", timeout=20)

    label = ("full (project-filtered: "
             "--deselect=tests/test_native.py::test_body)")
    assert b"ptest-bridge-refusal" not in completed.stderr
    assert label in completed.stderr.decode()
    assert not (root / "body.marker").exists()
    assert completed.result is not None
    data = completed.result["data"]
    assert any(reason["code"] == "project-filtered" and reason["message"] == label
               for reason in data["reasons"])


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
    # Collection-time hooks that can silently narrow a full run are
    # checked exactly like modifyitems: accepted from a checkout
    # conftest and recorded in the run label.
    "def pytest_pycollect_makeitem():\n    pass\n",
    "def pytest_collect_file():\n    pass\n",
    "def pytest_collect_directory():\n    pass\n",
    "def pytest_make_collect_report():\n    pass\n",
])
def test_full_project_conftest_collection_hooks_run_labelled(case, hook_source):
    """Section F flips these twins: conftest collection hooks run labelled."""
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
        "lifecycle = \"cooperative-process-group\"\n"
        "[selection]\nnon_input_outputs = [\"body.marker\"]\n")
    _commit_fixture(root)

    completed = case.invoke(domain, root, "--full", timeout=20)

    label = "full (project-filtered: conftest collection hook)"
    assert completed.code == 0, completed.stderr.decode()
    assert b"ptest-bridge-refusal" not in completed.stderr
    assert label in completed.stderr.decode()
    assert (root / "body.marker").read_text() == "ran"
    assert completed.result is not None
    data = completed.result["data"]
    assert any(reason["code"] == "project-filtered" and reason["message"] == label
               for reason in data["reasons"])


@pytest.mark.parametrize("hook_source", [
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
    # Round 14: plain, wrapper, and specname-aliased sessionfinish forms
    # defined in the checkout conftest are project-owned and allowed (see
    # test_full_conftest_sessionfinish_cleanup_runs_labelled); only
    # sessionfinish on a registered instance stays refused below.
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

    # The hook itself is project-owned, but the dropped collected item can
    # never certify a pass: reconciliation makes the run incomplete.
    assert completed.code == 4
    assert b"ptest-bridge-refusal" in completed.stderr
    data = completed.result["data"]
    assert data["status"] == "incomplete"
    assert data["exit_origin"] == "ptest"
    assert data["counts"] is None
    assert data["full_gate_eligible"] is False
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


def _full_project_toml(root, project_id):
    (root / ".ptest.toml").write_text(
        "version = 1\nproject_id = \"" + project_id + "\"\n[runner]\n"
        "kind = \"pytest\"\nlauncher = " + json.dumps([sys.executable]) + "\n"
        "args = [\"-q\", \"-p\", \"no:xdist\"]\nfull_args = []\ntest_roots = [\"tests\"]\nworkers = 1\n"
        "lifecycle = \"cooperative-process-group\"\n"
        "[selection]\nnon_input_outputs = [\"a.marker\", \"b.marker\", \"legacy.marker\",\n"
        "\"mig.marker\", \"ignored.marker\", \"body.marker\", \"cleanup.marker\"]\n")


def test_full_deep_conftest_hook_runs_labelled(case):
    """Section F HIGH(a): a hook below the static scan depth still labels."""
    domain = case.domain(slots=1, jobs=1)
    root = case.project(domain, kind="pytest")
    project_id = (root / ".ptest.toml").read_text().split('project_id = "', 1)[1].split('"', 1)[0]
    deep = root / "tests" / "a" / "b" / "c" / "d"
    deep.mkdir(parents=True)
    (deep / "conftest.py").write_text(
        "def pytest_collection_modifyitems(items):\n"
        "    items[:] = [item for item in items if 'test_b' not in item.nodeid]\n")
    (deep / "test_deep.py").write_text(
        "from pathlib import Path\n"
        "def test_a():\n    Path('a.marker').write_text('ran')\n"
        "def test_b():\n    Path('b.marker').write_text('ran')\n")
    _full_project_toml(root, project_id)
    _commit_fixture(root)

    completed = case.invoke(domain, root, "--full", timeout=20)

    label = "full (project-filtered: conftest collection hook)"
    assert completed.code == 0, completed.stderr.decode()
    assert b"ptest-bridge-refusal" not in completed.stderr
    assert label in completed.stderr.decode()
    assert (root / "a.marker").read_text() == "ran"
    assert not (root / "b.marker").exists()
    assert completed.result is not None
    data = completed.result["data"]
    assert any(reason["code"] == "project-filtered" and reason["message"] == label
               for reason in data["reasons"])


def test_full_class_plugin_collection_hook_is_refused(case):
    """Section F HIGH(b): a hook on a registered instance is not project-owned."""
    domain = case.domain(slots=1, jobs=1)
    root = case.project(domain, kind="pytest")
    project_id = (root / ".ptest.toml").read_text().split('project_id = "', 1)[1].split('"', 1)[0]
    (root / "tests").mkdir()
    (root / "tests" / "conftest.py").write_text(
        "import pytest\n"
        "class DropB:\n"
        "    @pytest.hookimpl\n"
        "    def pytest_collection_modifyitems(self, items):\n"
        "        items[:] = [item for item in items if 'test_b' not in item.nodeid]\n"
        "def pytest_configure(config):\n"
        "    config.pluginmanager.register(DropB(), 'drop-b')\n")
    (root / "tests" / "test_native.py").write_text(
        "from pathlib import Path\n"
        "def test_a():\n    Path('a.marker').write_text('ran')\n"
        "def test_b():\n    Path('b.marker').write_text('ran')\n")
    _full_project_toml(root, project_id)
    _commit_fixture(root)

    completed = case.invoke(domain, root, "--full", timeout=20)

    assert completed.code == 4
    assert b"ptest-bridge-refusal" in completed.stderr
    assert completed.result is not None
    assert completed.result["data"]["status"] == "incomplete"
    assert completed.result["data"]["exit_origin"] == "ptest"
    assert not (root / "a.marker").exists()
    assert not (root / "b.marker").exists()


def test_full_non_conftest_makeitem_hook_is_refused(case):
    """A collection-time hook on a registered instance is not project-owned."""
    domain = case.domain(slots=1, jobs=1)
    root = case.project(domain, kind="pytest")
    project_id = (root / ".ptest.toml").read_text().split('project_id = "', 1)[1].split('"', 1)[0]
    (root / "tests").mkdir()
    (root / "tests" / "conftest.py").write_text(
        "import pytest\n"
        "class DropB:\n"
        "    @pytest.hookimpl\n"
        "    def pytest_pycollect_makeitem(self):\n"
        "        return []\n"
        "def pytest_configure(config):\n"
        "    config.pluginmanager.register(DropB(), 'drop-b')\n")
    (root / "tests" / "test_native.py").write_text(
        "from pathlib import Path\n"
        "def test_a():\n    Path('a.marker').write_text('ran')\n"
        "def test_b():\n    Path('b.marker').write_text('ran')\n")
    _full_project_toml(root, project_id)
    _commit_fixture(root)

    completed = case.invoke(domain, root, "--full", timeout=20)

    assert completed.code == 4
    assert b"ptest-bridge-refusal" in completed.stderr
    assert completed.result is not None
    assert completed.result["data"]["status"] == "incomplete"
    assert completed.result["data"]["exit_origin"] == "ptest"
    assert not (root / "a.marker").exists()
    assert not (root / "b.marker").exists()


def test_full_pytest_toml_addopts_runs_labelled(case):
    """Section F HIGH(c): pytest 9 reads pytest.toml first; its filters label."""
    domain = case.domain(slots=1, jobs=1)
    root = case.project(domain, kind="pytest")
    project_id = (root / ".ptest.toml").read_text().split('project_id = "', 1)[1].split('"', 1)[0]
    (root / "tests").mkdir()
    (root / "tests" / "test_native.py").write_text(
        "from pathlib import Path\n"
        "def test_fast():\n    Path('a.marker').write_text('ran')\n"
        "def test_slow():\n    Path('b.marker').write_text('ran')\n")
    (root / "pytest.toml").write_text("[pytest]\naddopts = [\"-k\", \"not slow\"]\n")
    _full_project_toml(root, project_id)
    _commit_fixture(root)

    completed = case.invoke(domain, root, "--full", timeout=20)

    label = "full (project-filtered: -k not slow)"
    assert completed.code == 0, completed.stderr.decode()
    assert b"ptest-bridge-refusal" not in completed.stderr
    assert label in completed.stderr.decode()
    assert (root / "a.marker").read_text() == "ran"
    assert not (root / "b.marker").exists()
    assert completed.result is not None
    data = completed.result["data"]
    assert any(reason["code"] == "project-filtered" and reason["message"] == label
               for reason in data["reasons"])


def test_full_reexported_collection_hook_is_refused(case):
    """Section F HIGH: a hook imported into conftest is not defined there."""
    domain = case.domain(slots=1, jobs=1)
    root = case.project(domain, kind="pytest")
    project_id = (root / ".ptest.toml").read_text().split('project_id = "', 1)[1].split('"', 1)[0]
    (root / "tests").mkdir()
    (root / "tests" / "hook_impl.py").write_text(
        "def pytest_collection_modifyitems(items):\n"
        "    items[:] = [item for item in items if 'test_b' not in item.nodeid]\n")
    (root / "tests" / "conftest.py").write_text(
        "from hook_impl import pytest_collection_modifyitems\n")
    (root / "tests" / "test_native.py").write_text(
        "from pathlib import Path\n"
        "def test_a():\n    Path('a.marker').write_text('ran')\n"
        "def test_b():\n    Path('b.marker').write_text('ran')\n")
    _full_project_toml(root, project_id)
    _commit_fixture(root)

    completed = case.invoke(domain, root, "--full", timeout=20)

    assert completed.code == 4
    assert b"ptest-bridge-refusal" in completed.stderr
    assert completed.result is not None
    assert completed.result["data"]["status"] == "incomplete"
    assert completed.result["data"]["exit_origin"] == "ptest"
    assert not (root / "a.marker").exists()
    assert not (root / "b.marker").exists()


def test_full_ini_collect_only_is_refused(case):
    """Section F MEDIUM: --co from ini runs nothing and must stay refused."""
    domain = case.domain(slots=1, jobs=1)
    root = case.project(domain, kind="pytest")
    project_id = (root / ".ptest.toml").read_text().split('project_id = "', 1)[1].split('"', 1)[0]
    (root / "tests").mkdir()
    (root / "tests" / "test_native.py").write_text(
        "from pathlib import Path\n"
        "def test_a():\n    Path('a.marker').write_text('ran')\n"
        "def test_b():\n    Path('b.marker').write_text('ran')\n")
    (root / "pytest.ini").write_text("[pytest]\naddopts = --co\n")
    _full_project_toml(root, project_id)
    _commit_fixture(root)

    completed = case.invoke(domain, root, "--full", timeout=20)

    assert completed.code == 4
    assert b"ptest-bridge-refusal" in completed.stderr
    assert completed.result is not None
    assert completed.result["data"]["status"] == "incomplete"
    assert completed.result["data"]["exit_origin"] == "ptest"
    assert not (root / "a.marker").exists()
    assert not (root / "b.marker").exists()


def test_full_ini_last_failed_is_refused(case):
    """Section F MEDIUM: --lf from ini is cache-state dependent, stays refused."""
    domain = case.domain(slots=1, jobs=1)
    root = case.project(domain, kind="pytest")
    project_id = (root / ".ptest.toml").read_text().split('project_id = "', 1)[1].split('"', 1)[0]
    (root / "tests").mkdir()
    (root / "tests" / "test_native.py").write_text(
        "from pathlib import Path\n"
        "def test_a():\n    Path('a.marker').write_text('ran')\n"
        "def test_b():\n    Path('b.marker').write_text('ran')\n")
    (root / "pytest.ini").write_text("[pytest]\naddopts = --lf\n")
    _full_project_toml(root, project_id)
    _commit_fixture(root)

    completed = case.invoke(domain, root, "--full", timeout=20)

    assert completed.code == 4
    assert b"ptest-bridge-refusal" in completed.stderr
    assert completed.result is not None
    assert completed.result["data"]["status"] == "incomplete"
    assert completed.result["data"]["exit_origin"] == "ptest"


def test_full_conftest_option_mutation_is_refused(case):
    """Section F LOW: mutating config.option.markexpr is not the ini value."""
    domain = case.domain(slots=1, jobs=1)
    root = case.project(domain, kind="pytest")
    project_id = (root / ".ptest.toml").read_text().split('project_id = "', 1)[1].split('"', 1)[0]
    (root / "tests").mkdir()
    (root / "tests" / "conftest.py").write_text(
        "def pytest_configure(config):\n"
        "    config.option.markexpr = 'slow'\n")
    (root / "tests" / "test_native.py").write_text(
        "import pytest\n"
        "from pathlib import Path\n"
        "def test_a():\n    Path('a.marker').write_text('ran')\n"
        "@pytest.mark.slow\n"
        "def test_b():\n    Path('b.marker').write_text('ran')\n")
    (root / "pytest.ini").write_text(
        "[pytest]\nmarkers = slow: a slow test\naddopts = -m 'not slow'\n")
    _full_project_toml(root, project_id)
    _commit_fixture(root)

    completed = case.invoke(domain, root, "--full", timeout=20)

    assert completed.code == 4
    assert b"ptest-bridge-refusal" in completed.stderr
    assert completed.result is not None
    assert completed.result["data"]["status"] == "incomplete"
    assert completed.result["data"]["exit_origin"] == "ptest"
    assert not (root / "a.marker").exists()
    assert not (root / "b.marker").exists()


def test_full_persea_shaped_suite_runs_with_combined_label(case):
    """Section F: ini -m plus a conftest hook label exactly like persea api."""
    domain = case.domain(slots=1, jobs=1)
    root = case.project(domain, kind="pytest")
    project_id = (root / ".ptest.toml").read_text().split('project_id = "', 1)[1].split('"', 1)[0]
    (root / "tests").mkdir()
    (root / "tests" / "conftest.py").write_text(
        "from pathlib import Path\n"
        "def pytest_collection_modifyitems(items):\n"
        "    items[:] = [item for item in items if 'test_legacy' not in item.nodeid]\n"
        "def pytest_sessionfinish(session, exitstatus):\n"
        "    Path('cleanup.marker').write_text('done')\n")
    (root / "tests" / "test_native.py").write_text(
        "import pytest\n"
        "from pathlib import Path\n"
        "def test_a():\n    Path('a.marker').write_text('ran')\n"
        "def test_legacy():\n    Path('legacy.marker').write_text('ran')\n"
        "@pytest.mark.extended_migration\n"
        "def test_mig():\n    Path('mig.marker').write_text('ran')\n")
    (root / "pytest.ini").write_text(
        "[pytest]\nmarkers = extended_migration: a migration test\n"
        "addopts = -m 'not extended_migration'\n")
    _full_project_toml(root, project_id)
    _commit_fixture(root)

    completed = case.invoke(domain, root, "--full", timeout=20)

    label = ("full (project-filtered: -m not extended_migration; "
             "conftest collection hook; conftest sessionfinish hook)")
    assert completed.code == 0, completed.stderr.decode()
    assert b"ptest-bridge-refusal" not in completed.stderr
    assert label in completed.stderr.decode()
    assert (root / "a.marker").read_text() == "ran"
    assert (root / "cleanup.marker").read_text() == "done"
    assert not (root / "legacy.marker").exists()
    assert not (root / "mig.marker").exists()
    assert completed.result is not None
    data = completed.result["data"]
    assert any(reason["code"] == "project-filtered" and reason["message"] == label
               for reason in data["reasons"])


def test_full_ini_python_files_runs_labelled(case):
    """Section F NOTE: non-default ini discovery narrowing is never silent."""
    domain = case.domain(slots=1, jobs=1)
    root = case.project(domain, kind="pytest")
    project_id = (root / ".ptest.toml").read_text().split('project_id = "', 1)[1].split('"', 1)[0]
    (root / "tests").mkdir()
    (root / "tests" / "check_a.py").write_text(
        "from pathlib import Path\n"
        "def test_a():\n    Path('a.marker').write_text('ran')\n")
    (root / "tests" / "test_unmatched.py").write_text(
        "from pathlib import Path\n"
        "def test_b():\n    Path('b.marker').write_text('ran')\n")
    (root / "pytest.ini").write_text("[pytest]\npython_files = check_*.py\n")
    _full_project_toml(root, project_id)
    _commit_fixture(root)

    completed = case.invoke(domain, root, "--full", timeout=20)

    assert completed.code == 0, completed.stderr.decode()
    assert b"ptest-bridge-refusal" not in completed.stderr
    assert "project-filtered" in completed.stderr.decode()
    assert "python_files=check_*.py" in completed.stderr.decode()
    assert (root / "a.marker").read_text() == "ran"
    assert not (root / "b.marker").exists()
    assert completed.result is not None
    data = completed.result["data"]
    assert any(reason["code"] == "project-filtered"
               and "python_files=check_*.py" in reason["message"]
               for reason in data["reasons"])


def test_full_conftest_collect_ignore_runs_labelled(case):
    """Section F NOTE: conftest collect_ignore narrowing is never silent."""
    domain = case.domain(slots=1, jobs=1)
    root = case.project(domain, kind="pytest")
    project_id = (root / ".ptest.toml").read_text().split('project_id = "', 1)[1].split('"', 1)[0]
    (root / "tests").mkdir()
    (root / "tests" / "conftest.py").write_text(
        "collect_ignore = ['test_ignored.py']\n")
    (root / "tests" / "test_native.py").write_text(
        "from pathlib import Path\n"
        "def test_a():\n    Path('a.marker').write_text('ran')\n")
    (root / "tests" / "test_ignored.py").write_text(
        "from pathlib import Path\n"
        "def test_ignored():\n    Path('ignored.marker').write_text('ran')\n")
    _full_project_toml(root, project_id)
    _commit_fixture(root)

    completed = case.invoke(domain, root, "--full", timeout=20)

    assert completed.code == 0, completed.stderr.decode()
    assert b"ptest-bridge-refusal" not in completed.stderr
    assert "project-filtered" in completed.stderr.decode()
    assert "collect_ignore in tests/conftest.py" in completed.stderr.decode()
    assert (root / "a.marker").read_text() == "ran"
    assert not (root / "ignored.marker").exists()
    assert completed.result is not None
    data = completed.result["data"]
    assert any(reason["code"] == "project-filtered"
               and "collect_ignore in tests/conftest.py" in reason["message"]
               for reason in data["reasons"])


def test_full_collection_finish_drop_is_incomplete(case):
    """HIGH twin `finish`: dropping after the final inventory is incomplete."""
    domain = case.domain(slots=1, jobs=1)
    root = case.project(domain, kind="pytest")
    project_id = (root / ".ptest.toml").read_text().split('project_id = "', 1)[1].split('"', 1)[0]
    (root / "tests").mkdir()
    (root / "tests" / "conftest.py").write_text(
        "def pytest_collection_finish(session):\n"
        "    session.items[:] = [i for i in session.items if 'test_b' not in i.nodeid]\n")
    (root / "tests" / "test_native.py").write_text(
        "from pathlib import Path\n"
        "def test_a():\n    Path('a.marker').write_text('ran')\n"
        "def test_b():\n    Path('b.marker').write_text('ran')\n")
    _full_project_toml(root, project_id)
    _commit_fixture(root)

    completed = case.invoke(domain, root, "--full", timeout=20)

    assert completed.code == 4
    assert b"ptest-bridge-refusal" in completed.stderr
    assert completed.result is not None
    data = completed.result["data"]
    assert data["status"] == "incomplete"
    assert data["exit_origin"] == "ptest"
    assert not (root / "b.marker").exists()


def test_full_fixture_drop_is_incomplete(case):
    """HIGH twin `fixturedrop`: a session fixture dropping items is incomplete."""
    domain = case.domain(slots=1, jobs=1)
    root = case.project(domain, kind="pytest")
    project_id = (root / ".ptest.toml").read_text().split('project_id = "', 1)[1].split('"', 1)[0]
    (root / "tests").mkdir()
    (root / "tests" / "conftest.py").write_text(
        "import pytest\n"
        "@pytest.fixture(autouse=True)\n"
        "def _drop(request):\n"
        "    request.session.items[:] = [\n"
        "        i for i in request.session.items if 'test_b' not in i.nodeid]\n"
        "    yield\n")
    (root / "tests" / "test_native.py").write_text(
        "from pathlib import Path\n"
        "def test_a():\n    Path('a.marker').write_text('ran')\n"
        "def test_b():\n    Path('b.marker').write_text('ran')\n")
    _full_project_toml(root, project_id)
    _commit_fixture(root)

    completed = case.invoke(domain, root, "--full", timeout=20)

    assert completed.code == 4
    assert b"ptest-bridge-refusal" in completed.stderr
    assert completed.result is not None
    data = completed.result["data"]
    assert data["status"] == "incomplete"
    assert data["exit_origin"] == "ptest"
    assert not (root / "b.marker").exists()


def test_full_deselect_all_exit_five_passes_through(case):
    """Reconciliation leaves exit 5 alone: nothing collected, nothing unrun."""
    domain = case.domain(slots=1, jobs=1)
    root = case.project(domain, kind="pytest")
    project_id = (root / ".ptest.toml").read_text().split('project_id = "', 1)[1].split('"', 1)[0]
    (root / "tests").mkdir()
    (root / "tests" / "test_native.py").write_text(
        "def test_a():\n    assert True\n"
        "def test_b():\n    assert True\n")
    (root / "pytest.ini").write_text(
        "[pytest]\naddopts = --deselect=tests/test_native.py::test_a "
        "--deselect=tests/test_native.py::test_b\n")
    _full_project_toml(root, project_id)
    _commit_fixture(root)

    completed = case.invoke(domain, root, "--full", timeout=20)

    assert b"ptest-bridge-refusal" not in completed.stderr
    assert completed.result is not None
    data = completed.result["data"]
    assert data["runner_exit_code"] == 5
    assert data["exit_origin"] == "runner"


def test_full_normal_pass_runs_every_collected_item(case):
    """Reconciliation keeps a clean full pass green: every item runs."""
    domain = case.domain(slots=1, jobs=1)
    root = case.project(domain, kind="pytest")
    project_id = (root / ".ptest.toml").read_text().split('project_id = "', 1)[1].split('"', 1)[0]
    (root / "tests").mkdir()
    (root / "tests" / "test_native.py").write_text(
        "from pathlib import Path\n"
        "def test_a():\n    Path('a.marker').write_text('ran')\n"
        "def test_b():\n    Path('b.marker').write_text('ran')\n")
    _full_project_toml(root, project_id)
    _commit_fixture(root)

    completed = case.invoke(domain, root, "--full", timeout=20)

    assert completed.code == 0, completed.stderr.decode()
    assert b"ptest-bridge-refusal" not in completed.stderr
    assert completed.result is not None
    assert completed.result["data"]["status"] == "passed"
    assert (root / "a.marker").read_text() == "ran"
    assert (root / "b.marker").read_text() == "ran"


def test_full_maxfail_stop_after_failure_stays_failure(case):
    """Reconciliation never masks a failure: -x stopping early stays failed."""
    domain = case.domain(slots=1, jobs=1)
    root = case.project(domain, kind="pytest")
    project_id = (root / ".ptest.toml").read_text().split('project_id = "', 1)[1].split('"', 1)[0]
    (root / "tests").mkdir()
    (root / "tests" / "test_native.py").write_text(
        "from pathlib import Path\n"
        "def test_a():\n    assert False\n"
        "def test_b():\n    Path('b.marker').write_text('ran')\n")
    (root / "pytest.ini").write_text("[pytest]\naddopts = -x\n")
    _full_project_toml(root, project_id)
    _commit_fixture(root)

    completed = case.invoke(domain, root, "--full", timeout=20)

    assert completed.code == 1, completed.stderr.decode()
    assert b"ptest-bridge-refusal" not in completed.stderr
    assert completed.result is not None
    data = completed.result["data"]
    assert data["status"] == "failed"
    assert data["runner_exit_code"] == 1
    assert data["exit_origin"] == "runner"
    assert not (root / "b.marker").exists()


def test_full_conftest_sessionfinish_cleanup_runs_labelled(case):
    """Round 14: a cleanup-only conftest sessionfinish runs labelled and passes."""
    domain = case.domain(slots=1, jobs=1)
    root = case.project(domain, kind="pytest")
    project_id = (root / ".ptest.toml").read_text().split('project_id = "', 1)[1].split('"', 1)[0]
    (root / "tests").mkdir()
    (root / "tests" / "conftest.py").write_text(
        "from pathlib import Path\n"
        "def pytest_sessionfinish(session, exitstatus):\n"
        "    Path('cleanup.marker').write_text('done')\n")
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

    label = "full (project-filtered: conftest sessionfinish hook)"
    assert completed.code == 0, completed.stderr.decode()
    assert b"ptest-bridge-refusal" not in completed.stderr
    assert label in completed.stderr.decode()
    assert (root / "body.marker").read_text() == "ran"
    assert (root / "cleanup.marker").read_text() == "done"
    assert completed.result is not None
    data = completed.result["data"]
    assert any(reason["code"] == "project-filtered" and reason["message"] == label
               for reason in data["reasons"])


def test_full_sessionfinish_exitstatus_rewrite_is_refused(case):
    """Round 14: a sessionfinish that clears a failure outcome is refused."""
    domain = case.domain(slots=1, jobs=1)
    root = case.project(domain, kind="pytest")
    project_id = (root / ".ptest.toml").read_text().split('project_id = "', 1)[1].split('"', 1)[0]
    (root / "tests").mkdir()
    (root / "tests" / "conftest.py").write_text(
        "def pytest_sessionfinish(session, exitstatus):\n"
        "    session.exitstatus = 0\n")
    (root / "tests" / "test_native.py").write_text(
        "from pathlib import Path\n"
        "def test_fails():\n    Path('fail.marker').write_text('ran')\n"
        "    assert False\n")
    (root / ".ptest.toml").write_text(
        "version = 1\nproject_id = \"" + project_id + "\"\n[runner]\n"
        "kind = \"pytest\"\nlauncher = " + json.dumps([sys.executable]) + "\n"
        "args = [\"-q\", \"-p\", \"no:xdist\"]\nfull_args = []\ntest_roots = [\"tests\"]\nworkers = 1\n"
        "lifecycle = \"cooperative-process-group\"\n"
        "[selection]\nnon_input_outputs = [\"fail.marker\"]\n")
    _commit_fixture(root)

    completed = case.invoke(domain, root, "--full", timeout=20)

    assert completed.code == 4
    assert b"ptest-bridge-refusal" in completed.stderr
    assert completed.result is not None
    assert completed.result["data"]["status"] == "incomplete"
    assert completed.result["data"]["exit_origin"] == "ptest"
    assert (root / "fail.marker").read_text() == "ran"


def test_full_non_conftest_plugin_sessionfinish_is_refused(case):
    """Round 14: a sessionfinish from a non-conftest plugin stays refused."""
    domain = case.domain(slots=1, jobs=1)
    root = case.project(domain, kind="pytest")
    project_id = (root / ".ptest.toml").read_text().split('project_id = "', 1)[1].split('"', 1)[0]
    (root / "tests").mkdir()
    (root / "tests" / "session_helper.py").write_text(
        "def pytest_sessionfinish(session, exitstatus):\n"
        "    return None\n")
    (root / "tests" / "conftest.py").write_text('pytest_plugins = "session_helper"\n')
    (root / "tests" / "test_native.py").write_text(
        "from pathlib import Path\n"
        "def test_body():\n    Path('body.marker').write_text('ran')\n")
    _full_project_toml(root, project_id)
    _commit_fixture(root)

    completed = case.invoke(domain, root, "--full", timeout=20)

    assert completed.code == 4
    assert b"ptest-bridge-refusal" in completed.stderr
    assert completed.result is not None
    assert completed.result["data"]["status"] == "incomplete"
    assert completed.result["data"]["exit_origin"] == "ptest"
    assert not (root / "body.marker").exists()
