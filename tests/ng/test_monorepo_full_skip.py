"""Monorepo child-scoped full-gate skip (real git monorepo, command children).

Fixture: root v2 manifest plus api and web command-runner children
(``launcher = ["true"]``: fast, no frozen pytest-cov needed), root files
``uv.lock``, ``README.md`` and ``shared/openapi.json``. ``web`` declares
``[selection] full_triggers = ["shared/openapi.json"]``; both children
declare ``non_input_outputs = ["ptest-result-"]`` and the root
``.gitignore`` lists ``ptest-result-*``.

Rules pinned here: a declared child skips its full gate when its
child-scoped inputs match its last green full run even after commits
elsewhere (no HEAD equality); every negative in the matrix still runs;
a skip records no new green, baseline, history, or ledger entry.
"""
from __future__ import annotations

import json
import os
import re
import secrets
from dataclasses import replace
from pathlib import Path

import pytest

from support import git, init_git_repo

from ptest import contracts as C
from ptest import history as history_api


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


def _child_toml(project_id: str, *, triggers: tuple[str, ...] = ()) -> str:
    lines = [
        "version = 1",
        f'project_id = "{project_id}"',
        "[runner]",
        'kind = "command"',
        'launcher = ["true"]',
        "args = []",
        "full_args = []",
        "workers = 1",
        'lifecycle = "cooperative-process-group"',
        "[selection]",
        "enabled = true",
        "closed_inputs = true",
        'input_roots = ["input.txt"]',
        'non_input_outputs = ["ptest-result-"]',
    ]
    if triggers:
        rendered = ", ".join(f'"{item}"' for item in triggers)
        lines.append(f"full_triggers = [{rendered}]")
    return "\n".join(lines) + "\n"


def _monorepo(case, domain) -> Path:
    """Real git monorepo: root manifest plus api and web command children."""
    root = domain.root / "mono"
    root.mkdir(parents=True, exist_ok=True)
    (root / ".ptest.toml").write_text(
        'version = 2\n[monorepo]\nchildren = ["api", "web"]\n',
        encoding="utf-8")
    api = root / "api"
    api.mkdir(parents=True, exist_ok=True)
    (api / ".ptest.toml").write_text(
        _child_toml("ab" * 16), encoding="utf-8")
    (api / "input.txt").write_text("api v1\n", encoding="utf-8")
    web = root / "web"
    web.mkdir(parents=True, exist_ok=True)
    (web / ".ptest.toml").write_text(
        _child_toml("cd" * 16, triggers=("shared/openapi.json",)),
        encoding="utf-8")
    (web / "input.txt").write_text("web v1\n", encoding="utf-8")
    (root / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    (root / "README.md").write_text("# mono\n", encoding="utf-8")
    shared = root / "shared"
    shared.mkdir(parents=True, exist_ok=True)
    (shared / "openapi.json").write_text("{}\n", encoding="utf-8")
    (root / ".gitignore").write_text("ptest-result-*\n", encoding="utf-8")
    init_git_repo(root)
    assert (root / ".git").is_dir()
    return root


def _children(root: Path):
    from ptest import monorepo as monorepo_api

    manifest = monorepo_api.parse_monorepo_manifest(
        (root / ".ptest.toml").read_bytes(), root / ".ptest.toml")
    targets = monorepo_api.preflight_children(root, manifest)
    return {child.declaration: child for child in targets}


def _capture(domain, config):
    from ptest import operations
    from ptest.source import ensure_fingerprint_key

    ensure_fingerprint_key(domain)
    return operations._capture_source(
        domain, config, C.RunRequest(mode=C.Mode.FULL), ensure_key=True,
        execution_tier=C.ExecutionTier.ADVANCED, runtime_identity="a" * 64)


def _green(domain, child_config):
    """Publish a synthetic passed full run from the child's real capture."""
    from ptest import history as history_api
    from ptest import operations

    snapshot = _capture(domain, child_config)
    assert snapshot.digest is not None, snapshot.limitations
    assert snapshot.clean, [str(change) for change in snapshot.changes]
    checkout = operations._checkout(child_config)
    plan = C.Plan(mode=C.Mode.FULL, execution="full",
                  input_digest=snapshot.digest,
                  compatibility=snapshot.compatibility)
    command = C.summarize_command(C.RunnerKind.COMMAND, C.Mode.FULL,
                                  ("true",), workers=1, provenance=("test",))
    result = C.RunResult(
        run_id=secrets.token_hex(16), project_id=checkout.project_id,
        checkout_id=checkout.checkout_id, mode=C.Mode.FULL,
        status=C.Status.PASSED, phase="complete",
        started_at="2026-09-25T00:00:00+00:00",
        finished_at="2026-09-25T00:00:01+00:00",
        plan=plan, command=command, exit_code=0,
        exit_origin="runner", runner_exit_code=0,
        source_valid=True, full_gate_eligible=True,
        input_before=snapshot, input_after=snapshot,
        policy_digest=operations._policy_digest(child_config),
        runtime_identity="a" * 64,
    )
    inventory = C.Inventory(
        adapter="command", version="1", complete=True,
        tests=(C.TestRecord(id="tests/test_a.py::test_x",
                            file="tests/test_a.py",
                            outcome=C.Outcome("passed"),
                            setup_s=None, call_s=0.01, teardown_s=None),),
        digest="44" * 32,
    )
    from ptest import verified
    token = verified.begin_full(domain, checkout.project_id, snapshot.digest)
    published = history_api.publish_outcome(domain, checkout, result, inventory,
                                            verification_token=token)
    assert published.baseline_published is True
    return result


def _no_admission(monkeypatch):
    calls = []

    def _enqueue(domain, request):
        calls.append(request)
        raise AssertionError("full gate must not admit")

    monkeypatch.setattr("ptest.scheduler.enqueue", _enqueue)
    return calls


def _commit(root: Path, relative: str, content: str, message: str) -> str:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", message)
    return git(root, "rev-parse", "HEAD")


def _child_of(domain_config):
    from ptest import monorepo as monorepo_api

    return monorepo_api.declared_child(domain_config)


def _execute_web_after_green(case, domain, root, monkeypatch, capsys,
                             *, commit_rel=None, commit_content=None,
                             commit_message="change"):
    """Green both children, optionally commit, then execute web FULL."""
    from ptest import operations

    kids = _children(root)
    _green(domain, kids["api"].config)
    green = _green(domain, kids["web"].config)
    head = None
    if commit_rel is not None:
        head = _commit(root, commit_rel, commit_content, commit_message)
    calls = _no_admission(monkeypatch)
    result = operations.execute(
        domain, kids["web"].config, C.RunRequest(mode=C.Mode.FULL))
    err = capsys.readouterr().err
    return result, err, calls, green, head


def test_child_skips_after_a_commit_elsewhere(case, monkeypatch, capsys):
    from ptest import operations

    domain = case.domain()
    root = _monorepo(case, domain)
    result, err, calls, green, _ = _execute_web_after_green(
        case, domain, root, monkeypatch, capsys,
        commit_rel="api/input.txt", commit_content="api v2\n",
        commit_message="touch api")

    assert calls == []
    assert (result.status, result.exit_code) == (C.Status.PASSED, 0)
    assert operations.full_gate_skipped(result) is True
    assert f"ptest: web · unchanged since green at {green.input_after.head[:7]} (" in err
    assert "· skipped — ptest --full --again to rerun" in err


def test_child_skips_after_an_empty_commit(case, monkeypatch, capsys):
    from ptest import operations

    domain = case.domain()
    root = _monorepo(case, domain)
    kids = _children(root)
    _green(domain, kids["api"].config)
    green = _green(domain, kids["web"].config)
    git(root, "commit", "-q", "--allow-empty", "-m", "empty")
    calls = _no_admission(monkeypatch)
    result = operations.execute(
        domain, kids["web"].config, C.RunRequest(mode=C.Mode.FULL))
    err = capsys.readouterr().err

    assert calls == []
    assert (result.status, result.exit_code) == (C.Status.PASSED, 0)
    assert operations.full_gate_skipped(result) is True
    assert "ptest: web · unchanged since green at " in err
    assert green.input_after.head[:7] in err


def _mutate_own_file(root: Path) -> None:
    _commit(root, "web/input.txt", "web v2\n", "touch own file")


def _mutate_declared_trigger(root: Path) -> None:
    _commit(root, "shared/openapi.json", '{"v": 2}\n', "touch trigger")


def _mutate_root_manifest(root: Path) -> None:
    with (root / ".ptest.toml").open("a", encoding="utf-8") as handle:
        handle.write("# membership comment\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "touch manifest")


def _mutate_child_config(root: Path) -> None:
    with (root / "web" / ".ptest.toml").open("a", encoding="utf-8") as handle:
        handle.write("# child comment\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "touch child config")


def _mutate_shared_lockfile_committed(root: Path) -> None:
    _commit(root, "uv.lock", "version = 2\n", "bump lockfile")


def _mutate_shared_lockfile_uncommitted(root: Path) -> None:
    (root / "uv.lock").write_text("version = 2\n", encoding="utf-8")


@pytest.mark.parametrize("mutate", [
    _mutate_own_file,
    _mutate_declared_trigger,
    _mutate_root_manifest,
    _mutate_child_config,
    _mutate_shared_lockfile_committed,
    _mutate_shared_lockfile_uncommitted,
])
def test_child_negative_matrix_runs(case, mutate):
    from ptest import operations

    domain = case.domain()
    root = _monorepo(case, domain)
    kids = _children(root)
    _green(domain, kids["api"].config)
    _green(domain, kids["web"].config)
    mutate(root)
    # Re-resolve: a child config edit must be re-read to take effect.
    kids = _children(root)
    child = _child_of(operations._checkout(kids["web"].config).root)
    assert child is not None
    checkout = operations._checkout(kids["web"].config)
    request = C.RunRequest(mode=C.Mode.FULL)
    baseline = history_api.read_history(
        domain, checkout).baseline
    assert baseline is not None
    assert operations._full_skip_inputs(
        domain, kids["web"].config, checkout, request, baseline,
        child=child) is None


def test_child_compat_change_runs(case):
    from ptest import operations

    domain = case.domain()
    root = _monorepo(case, domain)
    kids = _children(root)
    web = kids["web"].config
    other = replace(web, runner=replace(web.runner, args=("other",)))
    snapshot = _capture(domain, other)
    assert snapshot.digest is not None
    # Same files, different runner identity: digest matches a same-file
    # capture under the real config, compatibility does not.
    plain = _capture(domain, web)
    assert snapshot.digest == plain.digest
    assert snapshot.compatibility != plain.compatibility
    _green(domain, other)
    child = _child_of(operations._checkout(web).root)
    assert child is not None
    checkout = operations._checkout(web)
    baseline = history_api.read_history(domain, checkout).baseline
    assert baseline is not None
    assert operations._full_skip_inputs(
        domain, web, checkout, C.RunRequest(mode=C.Mode.FULL), baseline,
        child=child) is None


def test_dirty_child_never_skips(case, monkeypatch):
    from ptest import operations

    domain = case.domain()
    root = _monorepo(case, domain)
    kids = _children(root)
    _green(domain, kids["api"].config)
    _green(domain, kids["web"].config)
    (root / "web" / "input.txt").write_text("dirty\n", encoding="utf-8")
    child = _child_of(operations._checkout(kids["web"].config).root)
    checkout = operations._checkout(kids["web"].config)
    request = C.RunRequest(mode=C.Mode.FULL)
    baseline = history_api.read_history(
        domain, checkout).baseline
    assert baseline is not None
    assert operations._full_skip_inputs(
        domain, kids["web"].config, checkout, request, baseline,
        child=child) is None


def test_dirty_sibling_or_root_readme_still_skips(case, monkeypatch, capsys):
    from ptest import operations

    domain = case.domain()
    root = _monorepo(case, domain)
    kids = _children(root)
    _green(domain, kids["api"].config)
    _green(domain, kids["web"].config)
    (root / "api" / "input.txt").write_text("dirty sibling\n", encoding="utf-8")
    calls = _no_admission(monkeypatch)
    result = operations.execute(
        domain, kids["web"].config, C.RunRequest(mode=C.Mode.FULL))
    assert calls == []
    assert operations.full_gate_skipped(result) is True
    assert "ptest: web · unchanged since green at " in capsys.readouterr().err
    git(root, "checkout", "--", "api/input.txt")
    (root / "README.md").write_text("# changed\n", encoding="utf-8")
    calls = _no_admission(monkeypatch)
    result = operations.execute(
        domain, kids["web"].config, C.RunRequest(mode=C.Mode.FULL))
    assert calls == []
    assert operations.full_gate_skipped(result) is True


def _publish_failed(domain, checkout, snapshot, policy, *, run_id=None,
                    status="failed", exit_code=1):
    from ptest import operations

    plan = C.Plan(mode=C.Mode.FULL, execution="full",
                  input_digest=snapshot.digest,
                  compatibility=snapshot.compatibility)
    command = C.summarize_command(C.RunnerKind.COMMAND, C.Mode.FULL,
                                  ("true",), workers=1, provenance=("test",))
    result = C.RunResult(
        run_id=run_id or secrets.token_hex(16),
        project_id=checkout.project_id, checkout_id=checkout.checkout_id,
        mode=C.Mode.FULL, status=C.Status(status), phase="complete",
        started_at="2026-09-25T00:00:00+00:00",
        finished_at="2026-09-25T00:00:01+00:00",
        plan=plan, command=command, exit_code=exit_code,
        exit_origin="runner", runner_exit_code=exit_code,
        source_valid=True, full_gate_eligible=True,
        input_before=snapshot, input_after=snapshot, policy_digest=policy,
        runtime_identity="a" * 64,
    )
    published = history_api.publish_outcome(domain, checkout, result, None)
    assert published.committed is True
    return result


def test_failed_or_incomplete_latest_full_runs(case):
    from ptest import operations

    domain = case.domain()
    root = _monorepo(case, domain)
    kids = _children(root)
    _green(domain, kids["api"].config)
    green = _green(domain, kids["web"].config)
    web = kids["web"].config
    checkout = operations._checkout(web)
    policy = operations._policy_digest(web)
    snapshot = _capture(domain, web)
    assert snapshot.digest == green.input_after.digest
    _publish_failed(domain, checkout, snapshot, policy)
    child = _child_of(operations._checkout(web).root)
    baseline = history_api.read_history(domain, checkout).baseline
    assert baseline is not None
    assert baseline.run_id == green.run_id
    assert operations._full_skip_inputs(
        domain, web, checkout, C.RunRequest(mode=C.Mode.FULL), baseline,
        child=child) is None
    incomplete = _publish_failed(domain, checkout, snapshot, policy,
                                 status="incomplete", exit_code=70)
    assert incomplete.status is C.Status.INCOMPLETE
    baseline = history_api.read_history(domain, checkout).baseline
    assert operations._full_skip_inputs(
        domain, web, checkout, C.RunRequest(mode=C.Mode.FULL), baseline,
        child=child) is None
    reason = operations.full_run_reason(
        baseline, snapshot, policy, last_failed=True, child=True,
        shared_change=())
    assert reason == "the last full run on this tree did not pass"


def test_single_project_new_commit_still_runs(case):
    from ptest import config as config_api
    from ptest import operations
    from support import write_ptest_toml

    domain = case.domain()
    root = domain.root / "repo"
    root.mkdir(parents=True, exist_ok=True)
    write_ptest_toml(root, kind="command", launcher=("true",), args=(),
                     full_args=(), project_id="ab" * 16)
    (root / "input.txt").write_text("v1\n", encoding="utf-8")
    init_git_repo(root, message="base")
    config = config_api.resolve_config(root).config
    assert config is not None
    checkout = operations._checkout(config)
    snapshot = _capture(domain, config)
    plan = C.Plan(mode=C.Mode.FULL, execution="full",
                  input_digest=snapshot.digest,
                  compatibility=snapshot.compatibility)
    command = C.summarize_command(C.RunnerKind.COMMAND, C.Mode.FULL,
                                  ("true",), workers=1, provenance=("test",))
    result = C.RunResult(
        run_id=secrets.token_hex(16), project_id=checkout.project_id,
        checkout_id=checkout.checkout_id, mode=C.Mode.FULL,
        status=C.Status.PASSED, phase="complete",
        started_at="2026-09-25T00:00:00+00:00",
        finished_at="2026-09-25T00:00:01+00:00",
        plan=plan, command=command, exit_code=0,
        exit_origin="runner", runner_exit_code=0,
        source_valid=True, full_gate_eligible=True,
        input_before=snapshot, input_after=snapshot,
        policy_digest=operations._policy_digest(config),
        runtime_identity="a" * 64,
    )
    inventory = C.Inventory(
        adapter="command", version="1", complete=True,
        tests=(C.TestRecord(id="tests/test_a.py::test_x",
                            file="tests/test_a.py",
                            outcome=C.Outcome("passed"),
                            setup_s=None, call_s=0.01, teardown_s=None),),
        digest="44" * 32,
    )
    from ptest import verified
    token = verified.begin_full(domain, checkout.project_id, snapshot.digest)
    published = history_api.publish_outcome(domain, checkout, result, inventory,
                                            verification_token=token)
    assert published.baseline_published is True
    git(root, "commit", "-q", "--allow-empty", "-m", "empty")
    baseline = history_api.read_history(domain, checkout).baseline
    assert baseline is not None
    assert operations._full_skip_inputs(
        domain, config, checkout, C.RunRequest(mode=C.Mode.FULL),
        baseline) is None
    fresh = _capture(domain, config)
    reason = operations.full_run_reason(
        baseline, fresh, operations._policy_digest(config))
    assert reason is not None and reason.startswith(
        "new commit since the last green (")


def test_again_runs_every_child(case):
    domain = case.domain()
    root = _monorepo(case, domain)
    kids = _children(root)
    _green(domain, kids["api"].config)
    _green(domain, kids["web"].config)
    _commit(root, "api/input.txt", "api v2\n", "touch api")
    completed = case.invoke(domain, root, "--full", "--again", timeout=60)
    assert completed.code == 0, completed.stderr.decode(errors="replace")
    err = completed.stderr.decode(errors="replace")
    assert "unchanged since green" not in err
    assert "skipped (unchanged)" not in err
    assert "ptest: api · command · full suite" in err
    assert "ptest: web · command · full suite" in err


def _state_files(base: Path) -> dict[str, bytes]:
    return {str(path.relative_to(base)): path.read_bytes()
            for path in sorted(base.rglob("*")) if path.is_file()}


def test_skipped_child_records_nothing(case):
    from ptest import lastgreen
    from ptest import operations
    from ptest import verified

    domain = case.domain()
    root = _monorepo(case, domain)
    kids = _children(root)
    _green(domain, kids["api"].config)
    _green(domain, kids["web"].config)
    web_checkout = operations._checkout(kids["web"].config)
    before_run_id = history_api.read_history(
        domain, web_checkout).baseline.run_id
    web_projects = (Path(domain.root) / "projects"
                    / kids["web"].config.project_id)
    before_verified = _state_files(web_projects)
    assert before_verified, "green runs must feed the verified ledger"
    ledger = json.loads(
        before_verified["verified.json"].decode("utf-8"))
    assert ledger["version"] == 2
    assert set(ledger["records"][0]) == set(verified.Record.__slots__)
    lastgreen_path = lastgreen.record_path(
        domain.root, os.path.realpath(root), "web")
    before_lastgreen = (lastgreen_path.read_bytes()
                        if lastgreen_path.exists() else None)
    _commit(root, "api/input.txt", "api v2\n", "touch api")
    completed = case.invoke(domain, root, "--full", timeout=60)
    assert completed.code == 0, completed.stderr.decode(errors="replace")
    err = completed.stderr.decode(errors="replace")
    assert "ptest: web · unchanged since green at " in err
    after = history_api.read_history(domain, web_checkout).baseline
    assert after.run_id == before_run_id
    assert _state_files(web_projects) == before_verified
    if lastgreen_path.exists() or before_lastgreen is not None:
        assert lastgreen_path.read_bytes() == before_lastgreen


def test_total_counts_skipped_children(case):
    domain = case.domain()
    root = _monorepo(case, domain)
    kids = _children(root)
    _green(domain, kids["api"].config)
    _green(domain, kids["web"].config)
    _commit(root, "api/input.txt", "api v2\n", "touch api")
    completed = case.invoke(domain, root, "--full", timeout=60)
    assert completed.code == 0, completed.stderr.decode(errors="replace")
    err = completed.stderr.decode(errors="replace")
    assert "ptest: web · unchanged since green at " in err
    assert "ptest: api · command · full suite" in err
    assert re.search(
        r"ptest: total · passed · 1 child skipped \(unchanged\) · "
        r"\d+(\.\d+)?s", err) is not None


def test_verbose_skip_lists_inputs(case):
    domain = case.domain()
    root = _monorepo(case, domain)
    kids = _children(root)
    _green(domain, kids["api"].config)
    _green(domain, kids["web"].config)
    _commit(root, "api/input.txt", "api v2\n", "touch api")
    completed = case.invoke(domain, root, "--full", "-v", timeout=60)
    assert completed.code == 0, completed.stderr.decode(errors="replace")
    err = completed.stderr.decode(errors="replace")
    assert ("ptest: -v web inputs: web/ + .ptest.toml, shared/openapi.json "
            "— declare other cross-child inputs in full_triggers") in err


def test_cross_checkout_child_skip(case, monkeypatch, capsys):
    from ptest import config as config_api
    from ptest import operations

    domain = case.domain()
    root = _monorepo(case, domain)
    kids = _children(root)
    green = _green(domain, kids["api"].config)
    green_head = green.input_after.head
    linked = domain.root / "linked"
    git(root, "worktree", "add", "--detach", str(linked))
    try:
        assert (linked / "api" / ".ptest.toml").is_file()
        linked_config = config_api.resolve_config(linked / "api").config
        assert linked_config is not None
        linked_checkout = operations._checkout(linked_config)
        assert linked_checkout.checkout_id != operations._checkout(
            kids["api"].config).checkout_id
        assert _capture(domain, linked_config).clean
        # An api-unrelated commit in main, then move the linked checkout
        # onto it: heads differ from the green head, inputs match.
        _commit(root, "web/input.txt", "web v2\n", "touch web")
        new_head = git(root, "rev-parse", "HEAD")
        assert new_head != green_head
        git(linked, "checkout", "-q", new_head)
        assert git(linked, "rev-parse", "HEAD") == new_head
        child = _child_of(linked_checkout.root)
        assert child is not None
        request = C.RunRequest(mode=C.Mode.FULL)
        verdict = operations._full_verified_elsewhere(
            domain, linked_config, linked_checkout, request, child=child)
        assert verdict is not None
        short_sha, _age, where = verdict
        assert short_sha == green_head[:7]
        assert where == str(operations._checkout(
            kids["api"].config).root)
        calls = _no_admission(monkeypatch)
        result = operations.execute(domain, linked_config, request)
        err = capsys.readouterr().err
        assert calls == []
        assert operations.full_gate_skipped(result) is True
        assert (f"ptest: api · unchanged since green at {short_sha} in "
                in err)
        assert "· skipped — ptest --full --again to rerun" in err
        # A local failed full run on that digest blocks reuse.
        _publish_failed(domain, linked_checkout,
                        _capture(domain, linked_config),
                        operations._policy_digest(linked_config))
        assert operations._full_verified_elsewhere(
            domain, linked_config, linked_checkout, request,
            child=child) is None
        # The single-project rule still requires HEAD equality.
        assert operations._full_verified_elsewhere(
            domain, linked_config, linked_checkout, request) is None
    finally:
        git(root, "worktree", "remove", "--force", str(linked))


def test_child_join_at_a_different_head(case, monkeypatch, capsys):
    import os as _os

    from ptest import history as history_api
    from ptest import operations

    domain = case.domain()
    root = _monorepo(case, domain)
    kids = _children(root)
    _green(domain, kids["api"].config)
    _green(domain, kids["web"].config)
    web = kids["web"].config
    checkout = operations._checkout(web)
    snapshot = _capture(domain, web)
    run_id = secrets.token_hex(16)
    # A passed summary under a new run id moves the latest-full point
    # without moving the baseline, so the skip path declines and the
    # live lease below is joined instead.
    _publish_failed(domain, checkout, snapshot,
                    operations._policy_digest(web), run_id=run_id,
                    status="passed", exit_code=0)
    lease = {"version": 1, "digest": snapshot.digest, "run_id": run_id,
             "pid": _os.getpid(), "started_at": operations._iso_now(),
             "sequence": 1}
    assert history_api.claim_full_lease(domain, checkout, lease) is True
    # A sibling-only commit moves HEAD without touching the digest.
    _commit(root, "api/input.txt", "api v2\n", "touch api")
    calls = _no_admission(monkeypatch)
    result = operations.execute(domain, web, C.RunRequest(mode=C.Mode.FULL))
    err = capsys.readouterr().err
    assert calls == []
    assert (result.status, result.exit_code) == (C.Status.PASSED, 0)
    assert operations.full_gate_skipped(result) is False
    assert f"ptest: joined the running full run (pid {_os.getpid()})" in err


def test_format_helpers_and_pure_units(tmp_path):
    from ptest import monorepo as monorepo_api
    from ptest import operations
    from ptest import progress

    line = progress.format_child_unchanged("web", "abc1234", 3.4)
    assert line == ("ptest: web · unchanged since green at abc1234 "
                    "(3.4s ago) · skipped — ptest --full --again to rerun")
    other = progress.format_child_unchanged(
        "web", "abc1234", 61.0, where="/home/u/work/mono/api")
    assert other == ("ptest: web · unchanged since green at abc1234 in "
                     "/home/u/work/mono/api (1m1s ago) · skipped — "
                     "ptest --full --again to rerun")
    home = str(Path.home())
    shortened = progress.format_child_unchanged(
        "web", "abc1234", 3.4, where=home + "/work/mono/api")
    assert f"in ~/work/mono/api (3.4s ago)" in shortened
    inputs = progress.format_child_skip_inputs(
        "web", (".ptest.toml", "shared/openapi.json"))
    assert inputs == ("ptest: -v web inputs: web/ + .ptest.toml, "
                      "shared/openapi.json — declare other cross-child "
                      "inputs in full_triggers")
    assert progress.format_skipped_children(1) == "1 child skipped (unchanged)"
    assert progress.format_skipped_children(2) == "2 children skipped (unchanged)"
    end_plain = progress.format_end(
        C.Status.PASSED, counts=C.Counts(collected=12, executed=12,
                                         passed=12, failed=0, skipped=0,
                                         unknown=0),
        duration_s=3.4, exit_code=0)
    end_none = progress.format_end(
        C.Status.PASSED, counts=C.Counts(collected=12, executed=12,
                                         passed=12, failed=0, skipped=0,
                                         unknown=0),
        duration_s=3.4, exit_code=0, note=None)
    assert end_none == end_plain
    noted = progress.format_end(
        C.Status.PASSED, counts=C.Counts(collected=12, executed=12,
                                         passed=12, failed=0, skipped=0,
                                         unknown=0),
        duration_s=3.4, exit_code=0,
        note=progress.format_skipped_children(1))
    assert noted == ("ptest: passed · 12 tests · 1 child skipped "
                     "(unchanged) · 3.4s")

    snapshot = C.InputSnapshot(digest="11" * 32, compatibility="c",
                               head="b" * 40, clean=True)
    inventory = C.Inventory(
        adapter="command", version="1", complete=True,
        tests=(C.TestRecord(id="tests/test_a.py::test_x",
                            file="tests/test_a.py",
                            outcome=C.Outcome("passed"),
                            setup_s=None, call_s=0.01, teardown_s=None),),
        digest="44" * 32,
    )
    baseline = C.Baseline(run_id="ef" * 16, head="a" * 40,
                          input_digest="11" * 32, compatibility="c",
                          inventory=inventory, policy_digest="22" * 32,
                          created_at="2026-09-25T00:00:00+00:00")
    assert operations.full_run_reason(
        baseline, snapshot, "22" * 32, child=True,
        shared_change=("uv.lock",)) == (
        "shared file uv.lock changed since the last green at "
        f"{'a' * 7}")
    assert operations.full_run_reason(
        baseline, snapshot, "22" * 32, child=True,
        shared_change=("a.lock", "b.lock", "c.lock")) == (
        "shared file a.lock changed since the last green at "
        f"{'a' * 7} (+2 more)")
    assert operations.full_run_reason(
        baseline, snapshot, "22" * 32, child=True,
        shared_change=None) == (
        "files outside the child cannot be compared with the last green at "
        f"{'a' * 7}")

    assert monorepo_api.declared_child(root := tmp_path) is None
    single = tmp_path / "single"
    single.mkdir()
    from support import init_git_repo as _init
    _init(single)
    assert monorepo_api.declared_child(single) is None
    assert monorepo_api.shared_dependency_changes(
        single, ("api",), "0" * 40) is None
    assert monorepo_api.shared_dependency_changes(
        single, ("api", "web"), "HEAD") == ()
    assert operations.full_gate_skipped(object()) is False
    assert operations.full_gate_skipped(
        type("R", (), {"status": C.Status.PASSED})()) is False


def test_declared_child_and_shared_guard_shapes(case):
    from ptest import monorepo as monorepo_api

    domain = case.domain()
    root = _monorepo(case, domain)
    kids = _children(root)
    web_root = operations_root(kids["web"])
    child = monorepo_api.declared_child(web_root)
    assert child is not None
    assert child.declaration == "web"
    assert child.children == ("api", "web")
    assert os.path.realpath(child.git_root) == os.path.realpath(root)
    assert monorepo_api.declared_child(root) is None
    (root / "nested").mkdir(exist_ok=True)
    assert monorepo_api.declared_child(root / "nested") is None
    head = git(root, "rev-parse", "HEAD")
    assert monorepo_api.shared_dependency_changes(
        root, ("api", "web"), head) == ()
    _commit(root, "package.json", "{}\n", "add package manifest")
    changed = monorepo_api.shared_dependency_changes(
        root, ("api", "web"), head)
    assert changed == ("package.json",)
    (root / "uv.lock").write_text("version = 9\n", encoding="utf-8")
    changed = monorepo_api.shared_dependency_changes(
        root, ("api", "web"), head)
    assert "uv.lock" in changed
    _commit(root, "api/extra.txt", "x\n", "child file")
    now = git(root, "rev-parse", "HEAD")
    assert monorepo_api.shared_dependency_changes(
        root, ("api", "web"), now) == ()


def operations_root(child_target):
    from ptest import operations

    return operations._checkout(child_target.config).root
