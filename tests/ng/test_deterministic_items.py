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

    assert DI.DETERMINISTIC_ITEM_IDS == ("SELECT-001", "TIMING-001")


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


def test_timing_without_history_is_unknown(tmp_path):
    answers = _answers_for(
        tmp_path, _config(), {".ptest.toml": "[selection]\nenabled = false\n"})
    answer = answers["TIMING-001"]
    assert answer.status == "unknown"
    assert answer.reason == "no timing history yet: run ptest --full once"


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
    assert set(answers) == {"SELECT-001", "TIMING-001"}


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
