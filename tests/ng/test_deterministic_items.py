"""Deterministic doctor answers for SELECT-001 and TIMING-001 (T4 owned).

TDD contract for ``ptest.deterministic_items``: SELECT-001 answered from the
child's runner kind and ``[selection]`` policy, TIMING-001 answered from
ptest's own run/timing history. No model call, no scheduler initialization,
read-only history access, recorded fixtures only.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ptest import contracts as C
from ptest import doctor

CHILD_PID = "ab" * 16


def _config(pid=CHILD_PID, runner_kind=C.RunnerKind.PYTEST, **selection):
    policy = {"enabled": False, "closed_inputs": False}
    policy.update(selection)
    return C.Config(
        runner=C.RunnerConfig(
            kind=runner_kind, launcher=("uv",),
            test_roots=("tests",),
        ),
        setup=None,
        resources=C.ResourceConfig(),
        selection=C.SelectionPolicy(**policy),
        project_id=pid,
    )


def _resolution(root: Path, config=None):
    return C.ConfigResolution(
        root=root, path=None,
        config=config if config is not None else _config(),
        monorepo=None, provenance=(), warnings=(), problem=None,
    )


def _domain(root: Path):
    return C.DomainPaths(
        root=root, machine_config=root / ".ptest" / "config.toml",
        ledger=root / ".ptest" / "ledger",
        marker=root / ".ptest" / "marker",
        fixture=True, domain_id=None,
    )


def _workspace(root: Path, config=None):
    resolution = _resolution(root, config)
    return doctor.inspect_workspace(
        _domain(root), resolution, C.DEFAULT_SCAN_LIMITS, None), resolution


def _packet_for(root: Path, files: dict[str, str] | None = None):
    """Build one standalone packet over a tmp project."""
    from ptest import agent_assessment as AA

    for rel, text in (files or {}).items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    workspace, _ = _workspace(root)
    packets = AA.build_packets(workspace, _resolution(root),
                               AA.EvidenceLimits())
    assert len(packets) == 1
    return packets[0]


def _v1_config_text(project_id: str, runner_kind: str) -> str:
    return (
        "version = 1\n"
        f'project_id = "{project_id}"\n'
        "[runner]\n"
        f'kind = "{runner_kind}"\n'
        'launcher = ["true"]\n'
        "args = []\n"
        "full_args = []\n"
        'test_roots = ["tests"]\n'
        "workers = 1\n"
        'lifecycle = "cooperative-process-group"\n'
    )


def _answers_for(root: Path, config=None,
                 files: dict[str, str] | None = None):
    from ptest import deterministic_items as DI

    packet = _packet_for(root, files)
    return DI.answers_for(_domain(root), _resolution(root, config), packet)


def _baseline(call_durations):
    tests = tuple(
        C.TestRecord(id=f"tests/test_t.py::test_{index}", file="tests/test_t.py",
                     outcome=C.Outcome.PASSED, call_s=duration)
        for index, duration in enumerate(call_durations)
    )
    inventory = C.Inventory(adapter="pytest", version="9.1.1", complete=True,
                            tests=tests, digest="00" * 32)
    return C.Baseline(run_id="ab" * 16, head="head", input_digest="11" * 32,
                      compatibility="", inventory=inventory,
                      policy_digest="22" * 32, created_at="2026-09-24")


def _view(baseline=None, disabled=False):
    return C.HistoryView(baseline=baseline, obligations=(),
                         selection_disabled=disabled, limitations=())


def _full_run_summary(queue_s=1.0, setup_s=2.0, collection_s=3.0,
                      execution_s=12.34, finalization_s=0.5):
    """A summary with the real public history shape via serialize_run_result.

    The timings keys must stay the public payload names (``queue`` etc.,
    see ``contracts._timings_dict``); a hand-made dict here would hide a
    key-shape mismatch between the reader and real history.
    """
    command = C.summarize_command(
        C.RunnerKind.PYTEST, C.Mode.FULL, ["-q"],
        workers=1, provenance=("test",),
    )
    plan = C.Plan(
        mode=C.Mode.AUTOMATIC, execution="full", files=(), reasons=(),
        input_digest=None, compatibility=None, baseline_run_id=None,
        static_preview=False,
    )
    result = C.RunResult(
        run_id="ab" * 16, project_id="ab" * 16, checkout_id="cd" * 16,
        mode=C.Mode.FULL, status="passed", phase="complete",
        started_at="2026-09-24T00:00:00+00:00",
        finished_at="2026-09-24T00:00:19+00:00",
        plan=plan, command=command,
        timings=C.Timings(queue_s=queue_s, setup_s=setup_s,
                          collection_s=collection_s, execution_s=execution_s,
                          finalization_s=finalization_s),
    )
    summary = C.serialize_run_result(result)
    assert set(summary["timings"]) == {
        "queue", "setup", "collection", "execution", "finalization"}
    return summary


def test_ids_are_frozen():
    from ptest import deterministic_items as DI

    assert DI.DETERMINISTIC_ITEM_IDS == (
        "SELECT-001", "TIMING-001", "PARALLEL-001")


def test_selection_disabled_pytest_is_gap_with_cfg(tmp_path):
    answers = _answers_for(tmp_path, _config(),
                           {".ptest.toml": "[selection]\nenabled = false\n"})
    answer = answers["SELECT-001"]
    assert answer.item_id == "SELECT-001"
    assert answer.status == "gap"
    assert answer.reason == "selection is disabled in .ptest.toml"
    assert answer.evidence_paths == (".ptest.toml",)
    assert answer.finding_summary is not None
    assert answer.finding_change is not None
    assert ".ptest.toml" in answer.finding_change


def test_selection_open_inputs_is_gap(tmp_path):
    config = _config(enabled=True, closed_inputs=False,
                     input_roots=("src",))
    answers = _answers_for(tmp_path, config,
                           {".ptest.toml": "[selection]\nenabled = true\n"})
    answer = answers["SELECT-001"]
    assert answer.status == "gap"
    assert answer.reason == (
        "selection inputs are not declared closed in .ptest.toml")


def test_selection_enabled_without_input_roots_is_gap(tmp_path):
    config = _config(enabled=True, closed_inputs=True)
    answers = _answers_for(tmp_path, config,
                           {".ptest.toml": "[selection]\nenabled = true\n"})
    answer = answers["SELECT-001"]
    assert answer.status == "gap"
    assert answer.reason == (
        "selection inputs are not declared closed in .ptest.toml")


def test_selection_closed_is_satisfied_with_counts(tmp_path):
    config = _config(enabled=True, closed_inputs=True,
                     input_roots=("src", "lib"),
                     full_triggers=("pyproject.toml",))
    answers = _answers_for(tmp_path, config,
                           {".ptest.toml": "[selection]\nenabled = true\n"})
    answer = answers["SELECT-001"]
    assert answer.status == "satisfied"
    assert answer.reason == (
        "selection is enabled with closed inputs (2 input roots, "
        "1 full triggers); unknown input widens to the full suite")
    assert answer.finding_summary is None
    assert answer.finding_change is None


@pytest.mark.parametrize("runner_kind", ["vitest", "command"])
def test_selection_non_pytest_runners_are_not_applicable(tmp_path,
                                                         runner_kind):
    from ptest import deterministic_items as DI

    config = _config(
        runner_kind=C.RunnerKind(runner_kind))
    domain = _domain(tmp_path)
    packet = _packet_for(tmp_path, {".ptest.toml": "# cfg\n"})
    answers = DI.answers_for(domain, _resolution(tmp_path, config), packet)
    answer = answers["SELECT-001"]
    assert answer.status == "not-applicable"
    assert answer.reason == (
        f"ptest has no automatic test selection for {runner_kind}; "
        "every run is scoped or full")


def test_selection_gap_names_monorepo_cfg(tmp_path):
    from ptest import deterministic_items as DI
    from ptest import agent_assessment as AA
    from ptest import config as config_api

    config_text = _v1_config_text(CHILD_PID, "pytest")
    (tmp_path / ".ptest.toml").write_text(
        'version = 2\n[monorepo]\nchildren = ["api"]\n', encoding="utf-8")
    (tmp_path / "api").mkdir()
    (tmp_path / "api" / ".ptest.toml").write_text(
        config_text + "[selection]\nenabled = false\n", encoding="utf-8")
    resolution = config_api.resolve_config(tmp_path)
    workspace = doctor.inspect_workspace(
        _domain(tmp_path), resolution, C.DEFAULT_SCAN_LIMITS, None)
    packets = AA.build_packets(workspace, resolution, AA.EvidenceLimits())
    assert [packet.declaration for packet in packets] == ["api"]
    answers = DI.answers_for(_domain(tmp_path), resolution, packets[0])
    assert answers["SELECT-001"].reason == (
        "selection is disabled in api/.ptest.toml")


def test_select_gap_finding_prose_is_trusted_plain_text(tmp_path):
    answers = _answers_for(tmp_path, _config(),
                           {".ptest.toml": "[selection]\nenabled = false\n"})
    answer = answers["SELECT-001"]
    assert C.aa_prose_is_untrusted(answer.finding_summary) is False
    assert C.aa_prose_is_untrusted(answer.finding_change) is False
    assert "closed_inputs" in answer.finding_change
    assert "input_roots" in answer.finding_change
    assert "full_triggers" in answer.finding_change
    assert "`" not in answer.finding_summary + answer.finding_change


def test_select_fix_states_serial_tradeoff_when_parallel_active(tmp_path):
    """SELECT-001 fix on a parallel-active project: tradeoff, no --cov order.

    Following the fix must not break PARALLEL-001: under ptest --cov
    forces serial runs, so the fix states the tradeoff plainly instead
    of instructing the user to just add --cov.
    """
    from ptest import deterministic_items as DI

    _stub_qualified_venv(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\naddopts = '-n 4'\n", encoding="utf-8")
    packet = _packet_for(tmp_path, {".ptest.toml": "[selection]"})
    answers = DI.answers_for(
        _domain(tmp_path),
        _resolution(tmp_path, _parallel_config(tmp_path)), packet)
    answer = answers["SELECT-001"]
    assert answer.status == "gap"
    assert "--cov" not in answer.finding_change
    assert "runs serially under ptest" in answer.finding_change
    assert "keep parallel runs" in answer.finding_change
    assert "accept serial runs" in answer.finding_change


def test_select_fix_keeps_cov_guidance_when_not_parallel(tmp_path):
    """SELECT-001 fix without parallel still names the coverage first step."""
    answers = _answers_for(tmp_path, _config(),
                           {".ptest.toml": "[selection]\\nenabled = false\\n"})
    answer = answers["SELECT-001"]
    assert answer.status == "gap"
    assert "--cov" in answer.finding_change


def test_select_fix_states_tradeoff_when_parallel_active_and_qualified(
        tmp_path):
    """Qualified xdist + --cov in runner args still states the tradeoff.

    The coverage profile qualifies, yet xdist is active and ptest runs
    coverage serially: the SELECT-001 fix must state the serial tradeoff
    instead of staying silent, or it contradicts the PARALLEL-001 fix.
    """
    config = _parallel_config(
        tmp_path, args=("--cov", "pkg", "--cov-report", "term"))
    answers = _parallel_answers_for(tmp_path, addopts="-n 4", config=config)
    answer = answers["SELECT-001"]
    assert answer.status == "gap"
    assert "runs serially under ptest" in answer.finding_change
    assert "keep parallel runs" in answer.finding_change
    assert "accept serial runs" in answer.finding_change
    assert "add --cov" not in answer.finding_change


def test_parallel_coverage_fix_names_runner_args_and_selection_cost(
        tmp_path):
    """PARALLEL-001 R3 with --cov in runner args names it, not addopts.

    Removing --cov turns off test selection, so the fix states that cost
    plainly and never says pytest configuration for a runner args setting.
    """
    config = _parallel_config(
        tmp_path, args=("--cov", "pkg", "--cov-report", "term"))
    answer = _parallel_answers_for(
        tmp_path, addopts="-n 4", config=config)["PARALLEL-001"]
    assert answer.status == "gap"
    assert answer.finding_change == (
        "Remove --cov from [runner] args in .ptest.toml to run with xdist "
        "workers; removing it turns off ptest's test selection.")
    assert "pytest configuration" not in answer.finding_change


def test_parallel_coverage_fix_names_pytest_addopts_twin(tmp_path):
    """Twin: --cov in pytest addopts names the addopts, same cost."""
    answer = _parallel_answers_for(
        tmp_path, addopts="-n 4 --cov")["PARALLEL-001"]
    assert answer.status == "gap"
    assert answer.finding_change == (
        "Remove --cov from the pytest addopts to run with xdist workers; "
        "removing it turns off ptest's test selection.")


def test_timing_without_history_is_unknown(tmp_path):
    answers = _answers_for(
        tmp_path, _config(), {".ptest.toml": "[selection]\nenabled = false\n"})
    answer = answers["TIMING-001"]
    assert answer.status == "unknown"
    assert answer.reason == ("no timing history yet: run ptest --full once "
                             "for whole-run timing; per-test timings need "
                             "pytest coverage and selection set up in "
                             ".ptest.toml")


def test_timing_without_history_vitest_keeps_short_reason(tmp_path):
    """Twin: vitest keeps the short reason, no per-test clause."""
    answers = _answers_for(
        tmp_path, _config(runner_kind=C.RunnerKind.VITEST),
        {".ptest.toml": "[selection]"})
    answer = answers["TIMING-001"]
    assert answer.status == "unknown"
    assert answer.reason == "no timing history yet: run ptest --full once"


def test_timing_unknown_names_what_changes_the_answer(tmp_path):
    """TIMING-001 unknown advice stays honest for basic pytest projects.

    A bare "run ptest --full once" pretends one run settles timing;
    per-test timings need pytest coverage and selection set up.
    """
    answers = _answers_for(
        tmp_path, _config(), {".ptest.toml": "[selection]\nenabled = false\n"})
    answer = answers["TIMING-001"]
    assert answer.status == "unknown"
    assert "run ptest --full once" in answer.reason
    assert "per-test timings need pytest coverage and selection" in (
        answer.reason)


def test_timing_unreadable_history_is_unknown(tmp_path, monkeypatch):
    from ptest import deterministic_items as DI
    from ptest import history as history_api

    def _boom(*_args, **_kwargs):
        raise C.Problem(code="state-unavailable",
                        message="history state is unavailable",
                        phase="evidence", retryable=False)

    monkeypatch.setattr(history_api, "read_history", _boom)
    monkeypatch.setattr(history_api, "read_history_summaries", _boom)
    packet = _packet_for(tmp_path, {".ptest.toml": "# cfg\n"})
    answers = DI.answers_for(_domain(tmp_path), _resolution(tmp_path),
                             packet)
    assert answers["TIMING-001"].status == "unknown"
    assert answers["TIMING-001"].reason == (
        "ptest history is unavailable, so timing cannot be read")


def test_timing_satisfied_from_baseline_call_timings(tmp_path, monkeypatch):
    from ptest import deterministic_items as DI
    from ptest import history as history_api

    monkeypatch.setattr(history_api, "read_history",
                        lambda *_a, **_k: _view(_baseline([0.5, 1.0, 4.2, 5.0])))
    monkeypatch.setattr(history_api, "read_history_summaries",
                        lambda *_a, **_k: ())
    packet = _packet_for(tmp_path, {".ptest.toml": "# cfg\n"})
    answers = DI.answers_for(_domain(tmp_path), _resolution(tmp_path),
                             packet)
    answer = answers["TIMING-001"]
    assert answer.status == "satisfied"
    assert answer.reason == (
        "ptest recorded per-test timings for 4 tests in the last clean "
        "full run; 2 take over 3 s (slowest 5.0 s)")


def test_timing_whole_run_only_without_per_test_timings(tmp_path,
                                                       monkeypatch):
    from ptest import deterministic_items as DI
    from ptest import history as history_api

    summary = _full_run_summary()
    monkeypatch.setattr(history_api, "read_history",
                        lambda *_a, **_k: _view(_baseline([None, None])))
    monkeypatch.setattr(history_api, "read_history_summaries",
                        lambda *_a, **_k: (summary,))
    packet = _packet_for(tmp_path, {".ptest.toml": "# cfg\n"})
    answers = DI.answers_for(_domain(tmp_path), _resolution(tmp_path),
                             packet)
    answer = answers["TIMING-001"]
    assert answer.status == "unknown"
    assert answer.reason == (
        "ptest has whole-run timing only (last full run 18.8 s); "
        "per-test timings are not recorded for this runner")


def test_timing_prefers_baseline_over_whole_run(tmp_path, monkeypatch):
    from ptest import deterministic_items as DI
    from ptest import history as history_api

    summary = _full_run_summary()
    monkeypatch.setattr(history_api, "read_history",
                        lambda *_a, **_k: _view(_baseline([0.2])))
    monkeypatch.setattr(history_api, "read_history_summaries",
                        lambda *_a, **_k: (summary,))
    packet = _packet_for(tmp_path, {".ptest.toml": "# cfg\n"})
    answers = DI.answers_for(_domain(tmp_path), _resolution(tmp_path),
                             packet)
    assert answers["TIMING-001"].status == "satisfied"


def test_answers_never_raise_and_skip_unresolved_child(tmp_path):
    from ptest import deterministic_items as DI
    from ptest import agent_assessment as AA

    assert DI.answers_for(None, None, None) == {}
    packet = _packet_for(tmp_path, {".ptest.toml": "# cfg\n"})
    assert isinstance(packet, AA.EvidencePacket)
    bad_resolution = C.ConfigResolution(
        root=tmp_path, path=None, config=None, monorepo=None,
        provenance=(), warnings=(), problem=None)
    assert DI.answers_for(_domain(tmp_path), bad_resolution, packet) == {}


def test_answers_never_initialize_the_scheduler(tmp_path, monkeypatch):
    from ptest import deterministic_items as DI
    from ptest import scheduler

    def _forbidden(*_args, **_kwargs):
        raise AssertionError("scheduler must stay untouched")

    monkeypatch.setattr(scheduler, "initialize", _forbidden)
    answers = _answers_for(tmp_path)
    assert set(answers) == {"SELECT-001", "TIMING-001", "PARALLEL-001"}


def test_answer_validation_rejects_bad_shapes():
    from ptest import deterministic_items as DI

    with pytest.raises((TypeError, ValueError)):
        DI.DeterministicAnswer(item_id="FIX-001", status="gap",
                               reason="no", finding_summary="s",
                               finding_change="c")
    with pytest.raises((TypeError, ValueError)):
        DI.DeterministicAnswer(item_id="SELECT-001", status="gap",
                               reason="no")
    with pytest.raises((TypeError, ValueError)):
        DI.DeterministicAnswer(item_id="SELECT-001", status="satisfied",
                               reason="yes", finding_summary="s",
                               finding_change="c")
    ok = DI.DeterministicAnswer(item_id="SELECT-001", status="gap",
                                reason="selection is disabled in .ptest.toml",
                                evidence_paths=(".ptest.toml",),
                                finding_summary="Enable selection.",
                                finding_change="Enable it.")
    assert ok.evidence_paths == (".ptest.toml",)


def test_finding_prose_must_be_trusted():
    from ptest import deterministic_items as DI

    with pytest.raises((TypeError, ValueError)):
        DI.DeterministicAnswer(
            item_id="SELECT-001", status="gap", reason="r",
            finding_summary="Run `ptest --full` now.",
            finding_change="Enable selection.")


def test_answer_reason_rejects_owned_prefix():
    from ptest import deterministic_items as DI

    with pytest.raises((TypeError, ValueError)):
        DI.DeterministicAnswer(item_id="TIMING-001", status="unknown",
                               reason="Answered by ptest: late.")


# ---- PARALLEL-001 (T4b): answered from executability facts, no model call --

_UV_LAUNCHER = ("uv", "run", "--locked", "--no-sync", "python")


def _parallel_config(root=None, runner_kind=C.RunnerKind.PYTEST,
                     launcher=_UV_LAUNCHER, args=(), **selection):
    policy = {"enabled": False, "closed_inputs": False}
    policy.update(selection)
    checkout = None
    if root is not None:
        checkout = C.CheckoutIdentity(
            project_id=CHILD_PID, checkout_id="cd" * 16, root=root)
    return C.Config(
        runner=C.RunnerConfig(
            kind=runner_kind, launcher=launcher, args=args,
            test_roots=("tests",),
        ),
        setup=None,
        resources=C.ResourceConfig(),
        selection=C.SelectionPolicy(**policy),
        project_id=CHILD_PID,
        checkout=checkout,
    )


def _stub_qualified_venv(root: Path) -> None:
    packages = root / ".venv" / "lib" / "python3.12" / "site-packages"
    dist_info = packages / "pytest_xdist-3.8.0.dist-info"
    dist_info.mkdir(parents=True)
    (dist_info / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: pytest-xdist\nVersion: 3.8.0\n",
        encoding="utf-8",
    )
    (root / ".venv" / "pyvenv.cfg").write_text(
        "home = /usr/bin\ninclude-system-site-packages = false\n"
        "version = 3.12\n",
        encoding="utf-8",
    )


def _parallel_answers_for(root: Path, *, addopts: str | None,
                          config=None, files: dict[str, str] | None = None):
    """answers_for over a tmp pytest project with real executability facts."""
    from ptest import deterministic_items as DI

    if addopts is not None:
        (root / "pyproject.toml").write_text(
            "[tool.pytest.ini_options]\n"
            f"addopts = '{addopts}'\n",
            encoding="utf-8",
        )
    if config is None:
        config = _parallel_config(root)
    packet = _packet_for(root, files if files is not None else {
        ".ptest.toml": "[selection]\\nenabled = false\\n"})
    return DI.answers_for(_domain(root), _resolution(root, config), packet)


def test_parallel_four_workers_is_satisfied(tmp_path):
    _stub_qualified_venv(tmp_path)
    answers = _parallel_answers_for(
        tmp_path, addopts="-n 4 --dist=loadgroup")
    answer = answers["PARALLEL-001"]
    assert answer.status == "satisfied"
    assert answer.reason == (
        "pytest runs in parallel with 4 workers (xdist, --dist loadgroup)")
    assert answer.evidence_paths == (".ptest.toml", "pyproject.toml")
    assert answer.finding_summary is None
    assert answer.finding_change is None


def test_parallel_serial_fallback_is_gap_with_reason(tmp_path):
    _stub_qualified_venv(tmp_path)
    answers = _parallel_answers_for(
        tmp_path, addopts="-n 4 --dist=each")
    answer = answers["PARALLEL-001"]
    assert answer.status == "gap"
    assert answer.reason == (
        "pytest configures xdist but --dist each is not supported; "
        "ptest runs serially")
    assert answer.evidence_paths == (".ptest.toml",)
    assert answer.finding_summary is not None
    assert answer.finding_change is not None
    assert "--dist each is not supported" in answer.finding_summary


def test_parallel_n0_opt_out_is_gap_with_actionable_fix(tmp_path):
    _stub_qualified_venv(tmp_path)
    config = _parallel_config(tmp_path, args=("-n", "0"))
    answers = _parallel_answers_for(
        tmp_path, addopts="-n 4 --dist=loadgroup", config=config)
    answer = answers["PARALLEL-001"]
    assert answer.status == "gap"
    assert answer.reason == (
        "pytest configures xdist but .ptest.toml sets -n 0")
    assert answer.finding_change == (
        'remove "-n", "0" from [runner] args in .ptest.toml to run 4 workers')


def test_parallel_not_configured_provisional_fix_is_safety_first(tmp_path):
    answers = _parallel_answers_for(tmp_path, addopts=None)
    answer = answers["PARALLEL-001"]
    assert answer.status == "gap"
    assert answer.reason == "no parallel runner is configured for this project"
    assert answer.evidence_paths == (".ptest.toml",)
    assert answer.finding_change == "Resolve the parallel-safety gaps first."


def test_parallel_suggestion_gated_on_safety_gaps(tmp_path):
    """The safety gating lives in finalize_parallel, not the planned answer."""
    from ptest import deterministic_items as DI

    planned = _parallel_answers_for(tmp_path, addopts=None)["PARALLEL-001"]
    assert planned.status == "gap"
    assert planned.finding_change == (
        "Resolve the parallel-safety gaps first.")
    clean = DI.finalize_parallel(planned, False)
    assert clean.status == "gap"
    assert clean.finding_change == DI.parallel_suggestion()
    assert DI.finalize_parallel(planned, True) == planned
    assert DI.finalize_parallel(planned, None) == planned


def test_parallel_vitest_is_satisfied(tmp_path):
    from ptest import deterministic_items as DI

    (tmp_path / "node_modules" / "vitest").mkdir(parents=True)
    (tmp_path / "node_modules" / "vitest" / "vitest.mjs").write_text(
        "export {};\n", encoding="utf-8")
    config = _parallel_config(tmp_path, runner_kind=C.RunnerKind.VITEST,
                             launcher=("node",))
    packet = _packet_for(tmp_path, {
        ".ptest.toml": "[runner]\\nkind = \"vitest\"\\n"})
    answers = DI.answers_for(_domain(tmp_path), _resolution(tmp_path, config),
                             packet)
    answer = answers["PARALLEL-001"]
    assert answer.status == "satisfied"
    assert answer.reason == "tests run inside vitest with its own workers"


def test_parallel_command_runner_is_unknown_with_reason(tmp_path):
    from ptest import deterministic_items as DI

    config = _parallel_config(tmp_path, runner_kind=C.RunnerKind.COMMAND,
                             launcher=("sh",))
    packet = _packet_for(tmp_path, {
        ".ptest.toml": "[runner]\\nkind = \"command\"\\n"})
    answers = DI.answers_for(_domain(tmp_path), _resolution(tmp_path, config),
                             packet)
    answer = answers["PARALLEL-001"]
    assert answer.status == "unknown"
    assert answer.reason == (
        "ptest cannot tell whether this command runner parallelizes tests")


def test_parallel_gap_finding_prose_is_trusted_plain_text(tmp_path):
    _stub_qualified_venv(tmp_path)
    fallback = _parallel_answers_for(
        tmp_path, addopts="-n 4 --dist=each")["PARALLEL-001"]
    missing = _parallel_answers_for(tmp_path, addopts=None)["PARALLEL-001"]
    for answer in (fallback, missing):
        assert C.aa_prose_is_untrusted(answer.finding_summary) is False
        assert C.aa_prose_is_untrusted(answer.finding_change) is False
        assert "`" not in answer.finding_summary + answer.finding_change
        assert "|" not in answer.finding_summary + answer.finding_change


def test_parallel_missing_xdist_is_gap_with_environment_fix(tmp_path):
    """Twin: no .venv plus ``-n 4`` is an environment gap, never a config fix."""
    answers = _parallel_answers_for(tmp_path, addopts="-n 4")
    answer = answers["PARALLEL-001"]
    assert answer.status == "gap"
    assert answer.reason == (
        "pytest configures xdist but pytest-xdist is not installed in "
        "the project environment yet; ptest runs serially until setup "
        "installs it")
    assert answer.finding_change == "install pytest-xdist 3.8.0"
    assert "Clear the serial fallback" not in answer.finding_change
    assert answer.finding_summary.count("ptest runs serially") == 1


def test_parallel_missing_xdist_with_setup_names_setup_fix(tmp_path):
    """With a setup argv, the environment fix names the project setup."""
    from dataclasses import replace

    setup = C.SetupConfig(
        argv=("uv", "sync", "--locked"), required_paths=(".venv",),
        network=False, lifecycle_scripts=False)
    config = replace(_parallel_config(tmp_path), setup=setup)
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\naddopts = '-n 4'\n",
        encoding="utf-8",
    )
    packet = _packet_for(tmp_path, {
        ".ptest.toml": "[selection]\\nenabled = false\\n"})
    from ptest import deterministic_items as DI

    answer = DI.answers_for(_domain(tmp_path),
                            _resolution(tmp_path, config),
                            packet)["PARALLEL-001"]
    assert answer.status == "gap"
    assert answer.finding_change == (
        "run the project setup (uv sync --locked) "
        "so ptest can use pytest-xdist")
    assert answer.finding_summary.count("ptest runs serially") == 1


def test_parallel_unverifiable_launcher_is_gap_with_environment_fix(tmp_path):
    """A bare launcher cannot verify xdist: launcher fix, no dup phrase."""
    config = _parallel_config(tmp_path, launcher=("python",))
    answers = _parallel_answers_for(
        tmp_path, addopts="-n 4", config=config)
    answer = answers["PARALLEL-001"]
    assert answer.status == "gap"
    assert "cannot verify pytest-xdist" in answer.reason
    assert answer.finding_change == (
        'set [runner] launcher to an absolute interpreter or '
        '["uv", "run", "--locked", "--no-sync", "python"] in .ptest.toml')
    assert "Clear the serial fallback" not in answer.finding_change
    assert answer.finding_summary.count("ptest runs serially") == 1


def test_parallel_unqualified_xdist_is_gap_with_install_fix(tmp_path):
    """Twin: unqualified xdist + setup present still names the install fix."""
    from dataclasses import replace

    packages = tmp_path / ".venv" / "lib" / "python3.12" / "site-packages"
    dist_info = packages / "pytest_xdist-3.7.0.dist-info"
    dist_info.mkdir(parents=True)
    (dist_info / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: pytest-xdist\nVersion: 3.7.0\n",
        encoding="utf-8",
    )
    (tmp_path / ".venv" / "pyvenv.cfg").write_text(
        "home = /usr/bin\ninclude-system-site-packages = false\n"
        "version = 3.12\n",
        encoding="utf-8",
    )
    setup = C.SetupConfig(
        argv=("uv", "sync", "--locked"), required_paths=(".venv",),
        network=False, lifecycle_scripts=False)
    config = replace(_parallel_config(tmp_path), setup=setup)
    answers = _parallel_answers_for(
        tmp_path, addopts="-n 4", config=config)
    answer = answers["PARALLEL-001"]
    assert answer.status == "gap"
    assert "is not qualified" in answer.reason
    assert answer.finding_change == "install pytest-xdist 3.8.0"
    assert "run the project setup" not in answer.finding_change
    assert "Clear the serial fallback" not in answer.finding_change
    assert answer.finding_summary.count("ptest runs serially") == 1


def test_finalize_parallel_swaps_safety_first_when_clear(tmp_path):
    from ptest import deterministic_items as DI

    answer = _parallel_answers_for(tmp_path, addopts=None)["PARALLEL-001"]
    assert answer.finding_change == "Resolve the parallel-safety gaps first."
    final = DI.finalize_parallel(answer, False)
    assert final.status == "gap"
    assert final.reason == answer.reason
    assert final.finding_change == (
        "Add pytest-xdist to the project environment and request workers "
        "with -n auto in the pytest configuration.")


def test_finalize_parallel_keeps_safety_first_when_blocked(tmp_path):
    from ptest import deterministic_items as DI

    answer = _parallel_answers_for(tmp_path, addopts=None)["PARALLEL-001"]
    assert DI.finalize_parallel(answer, True) == answer
    assert DI.finalize_parallel(answer, None) == answer


def test_finalize_parallel_leaves_other_fixes_alone(tmp_path):
    from ptest import deterministic_items as DI

    _stub_qualified_venv(tmp_path)
    config = _parallel_config(tmp_path, args=("-n", "0"))
    answer = _parallel_answers_for(
        tmp_path, addopts="-n 4 --dist=loadgroup",
        config=config)["PARALLEL-001"]
    assert DI.finalize_parallel(answer, False) == answer


def test_parallel_unreadable_facts_is_unknown_with_reason(tmp_path,
                                                         monkeypatch):
    from ptest import deterministic_items as DI
    from ptest import executability as executability_api

    def _boom(config, *, project="."):
        raise RuntimeError("facts unavailable")

    monkeypatch.setattr(executability_api, "check_config", _boom)
    packet = _packet_for(tmp_path, {
        ".ptest.toml": "[selection]\\nenabled = false\\n"})
    answers = DI.answers_for(_domain(tmp_path),
                             _resolution(tmp_path, _parallel_config(tmp_path)),
                             packet)
    answer = answers["PARALLEL-001"]
    assert answer.status == "unknown"
    assert answer.reason == ("ptest cannot read the parallel configuration "
                             "for this project")


def test_parallel_degrades_without_cfg_excerpt(tmp_path):
    from ptest import agent_assessment as AA
    from ptest import deterministic_items as DI

    _stub_qualified_venv(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\naddopts = '-n 4 --dist=loadgroup'\n",
        encoding="utf-8",
    )
    packet = _packet_for(tmp_path, {
        "tests/test_x.py": "def test_x():\n    assert True\n"})
    assert ".ptest.toml" not in {excerpt.path for excerpt in packet.excerpts}
    answers = DI.answers_for(_domain(tmp_path),
                             _resolution(tmp_path,
                                         _parallel_config(tmp_path)),
                             packet)
    assert answers["PARALLEL-001"].status == "satisfied"
    assert answers["PARALLEL-001"].evidence_paths == ()
    reviews = AA.plan_item_reviews(packet, answers=answers)
    parallel = next(review for review in reviews
                    if review.item_id == "PARALLEL-001")
    assert parallel.request is None
    assert parallel.answer is not None
    replies = tuple(
        None if review.request is None else "synthetic provider failure"
        for review in reviews)
    child = AA.assemble_child(packet, reviews, replies)
    row = next(row for row in child.rows if row.id == "PARALLEL-001")
    assert row.status == "unknown"
    assert "(the ptest config is not in the review evidence)" in row.rationale


def test_parallel_answer_takes_no_model_call(tmp_path):
    from ptest import agent_assessment as AA

    _stub_qualified_venv(tmp_path)
    answers = _parallel_answers_for(
        tmp_path, addopts="-n 4 --dist=loadgroup")
    packet = _packet_for(tmp_path, {
        ".ptest.toml": "[selection]\\nenabled = false\\n"})
    reviews = AA.plan_item_reviews(packet, answers=answers)
    parallel = next(review for review in reviews
                    if review.item_id == "PARALLEL-001")
    assert parallel.request is None
    assert parallel.skip_reason is None
    assert parallel.answer is answers["PARALLEL-001"]
    replies = tuple(
        None if review.request is None else "synthetic provider failure"
        for review in reviews)
    child = AA.assemble_child(packet, reviews, replies)
    row = next(row for row in child.rows if row.id == "PARALLEL-001")
    assert row.status == "satisfied"
    assert row.rationale.startswith("Answered by ptest: ")
    assert [finding.id for finding in child.findings
            if finding.id == "PARALLEL-001"] == []


# --- DET1: child .ptest.toml is tier-0 evidence with exact citations ---------

_DET1_CONFIG_TEXT = (
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
    "enabled = false\n"
)
# 1-based spans inside _DET1_CONFIG_TEXT.
_DET1_RUNNER_SPAN = (3, 10)
_DET1_SELECTION_SPAN = (11, 12)
# 1-based span of the addopts entry in the helper-written pyproject.toml
# ("[tool.pytest.ini_options]\naddopts = '...'\n").
_DET1_ADDOPTS_SPAN = (2, 2)


def test_det1_ptest_toml_is_tier_zero_like_manifests():
    from ptest import agent_assessment as AA

    assert AA._admission_tier(".ptest.toml") == 0
    assert AA._admission_tier("api/.ptest.toml") == 5


def test_det1_ptest_toml_survives_packet_pressure(tmp_path):
    """70 test files must not starve the child .ptest.toml (DET1)."""
    from ptest import agent_assessment as AA

    files = {
        ".ptest.toml": _DET1_CONFIG_TEXT,
        "pyproject.toml": "[project]\nname = 'demo'\n",
    }
    for index in range(70):
        files[f"tests/test_{index:02d}.py"] = (
            f"def test_{index:02d}():\n    assert True\n")
    packet = _packet_for(tmp_path, files)
    assert ".ptest.toml" in {excerpt.path for excerpt in packet.excerpts}


def test_det1_private_ptest_state_is_never_admitted(tmp_path):
    from ptest import agent_assessment as AA

    packet = _packet_for(tmp_path, {
        ".ptest.toml": _DET1_CONFIG_TEXT,
        "tests/test_x.py": "def test_x():\n    assert True\n",
        ".ptest/ledger/SENTINEL": "PRIVATE_STATE_SENTINEL_DET1\n",
        ".ptest/history.json": "PRIVATE_STATE_SENTINEL_DET1\n",
    })
    paths = {excerpt.path for excerpt in packet.excerpts}
    assert not any(path.startswith(".ptest/") for path in paths)
    assert all("PRIVATE_STATE_SENTINEL_DET1" not in excerpt.text
               for excerpt in packet.excerpts)
    assert ".ptest.toml" in paths


def test_det1_packet_budget_accounting_holds(tmp_path):
    from ptest import agent_assessment as AA

    packet = _packet_for(tmp_path, {
        ".ptest.toml": _DET1_CONFIG_TEXT,
        "pyproject.toml": "[project]\nname = 'demo'\n",
        "tests/test_x.py": "def test_x():\n    assert True\n",
    })
    assert packet.file_count == len(packet.excerpts)
    assert packet.byte_count == sum(
        len(excerpt.text.encode("utf-8")) for excerpt in packet.excerpts)
    assert packet.file_count <= AA.MAX_FILES_PER_CHILD
    assert packet.byte_count <= AA.MAX_BYTES_PER_CHILD
    assert packet.excluded_count >= 0 and packet.truncated_count >= 0


def test_det1_rows_cite_exact_config_ranges_with_valid_identities(tmp_path):
    """Parallel ✓ cites [runner] + addopts; selection gap cites [selection]."""
    from ptest import agent_assessment as AA

    _stub_qualified_venv(tmp_path)
    answers = _parallel_answers_for(
        tmp_path, addopts="-n 4 --dist=loadgroup",
        config=_parallel_config(tmp_path),
        files={".ptest.toml": _DET1_CONFIG_TEXT,
               "tests/test_x.py": "def test_x():\n    assert True\n"})
    assert answers["SELECT-001"].status == "gap"
    assert answers["PARALLEL-001"].status == "satisfied"
    packet = _packet_for(tmp_path, {
        ".ptest.toml": _DET1_CONFIG_TEXT,
        "tests/test_x.py": "def test_x():\n    assert True\n"})
    # Re-answer against the assembled packet so rows bind real excerpts.
    from ptest import deterministic_items as DI
    answers = DI.answers_for(_domain(tmp_path),
                             _resolution(tmp_path, _parallel_config(tmp_path)),
                             packet)
    reviews = AA.plan_item_reviews(packet, answers=answers)
    replies = tuple(
        None if review.request is None else "synthetic provider failure"
        for review in reviews)
    child = AA.assemble_child(packet, reviews, replies)
    excerpt = next(e for e in packet.excerpts if e.path == ".ptest.toml")
    by_id = {row.id: row for row in child.rows}
    parallel = by_id["PARALLEL-001"]
    assert parallel.status == "satisfied"
    assert "(the ptest config is not in the review evidence)" not in (
        parallel.rationale)
    assert len(parallel.evidence) == 2
    assert parallel.evidence[0].path == ".ptest.toml"
    assert (parallel.evidence[0].start_line,
            parallel.evidence[0].end_line) == _DET1_RUNNER_SPAN
    assert parallel.evidence[0].sha256 == excerpt.sha256
    addopts_excerpt = next(
        e for e in packet.excerpts if e.path == "pyproject.toml")
    assert parallel.evidence[1].path == "pyproject.toml"
    assert (parallel.evidence[1].start_line,
            parallel.evidence[1].end_line) == _DET1_ADDOPTS_SPAN
    assert parallel.evidence[1].sha256 == addopts_excerpt.sha256
    selection = by_id["SELECT-001"]
    assert selection.status == "gap"
    assert "(the ptest config is not in the review evidence)" not in (
        selection.rationale)
    assert len(selection.evidence) == 1
    assert (selection.evidence[0].start_line,
            selection.evidence[0].end_line) == _DET1_SELECTION_SPAN
    assert selection.evidence[0].sha256 == excerpt.sha256
    assert [finding.id for finding in child.findings] == ["SELECT-001"]
    timing = by_id["TIMING-001"]
    assert timing.status == "unknown"
    assert "no timing history yet" in timing.rationale


@pytest.mark.parametrize("name,text,span", [
    ("pytest.ini",
     "[pytest]\naddopts = -n 4  # (see docs\ntestpaths = tests\n",
     (2, 2)),
    ("tox.ini",
     "[tox:tox]\nskipsdist = true\n[pytest]\n"
     "addopts = -n 4  # (see docs\ntestpaths = tests\n",
     (4, 4)),
    ("setup.cfg",
     "[metadata]\nname = demo\n[tool:pytest]\n"
     "addopts = -n 4  # (see docs\ntestpaths = tests\n",
     (4, 4)),
])
def test_det3_parallel_citation_reaches_ini_family_addopts(
        tmp_path, name, text, span):
    """The satisfied PARALLEL-001 citation narrows to the deciding file's
    addopts entry; a bracket in a trailing comment must not extend it."""
    from ptest import agent_assessment as AA
    from ptest import deterministic_items as DI
    from ptest import executability as E

    _stub_qualified_venv(tmp_path)
    assert E.addopts_source(tmp_path) is None
    (tmp_path / name).write_text(text, encoding="utf-8")
    assert E.addopts_source(tmp_path) == name
    config = _parallel_config(tmp_path)
    packet = _packet_for(tmp_path, {
        ".ptest.toml": "[selection]\nenabled = false\n",
        name: text,
        "tests/test_x.py": "def test_x():\n    assert True\n",
    })
    answers = DI.answers_for(_domain(tmp_path),
                             _resolution(tmp_path, config), packet)
    assert answers["PARALLEL-001"].status == "satisfied"
    assert answers["PARALLEL-001"].evidence_paths == (".ptest.toml", name)
    reviews = AA.plan_item_reviews(packet, answers=answers)
    replies = tuple(
        None if review.request is None else "synthetic provider failure"
        for review in reviews)
    child = AA.assemble_child(packet, reviews, replies)
    row = next(r for r in child.rows if r.id == "PARALLEL-001")
    assert row.status == "satisfied"
    assert row.evidence[1].path == name
    assert (row.evidence[1].start_line,
            row.evidence[1].end_line) == span
