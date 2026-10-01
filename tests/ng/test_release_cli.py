"""ptest release: grammar, exact output lines, exit codes and guide row.

The scheduler is monkeypatched (``ptest.scheduler.release_run`` with
``raising=False``); outcomes are the local ``_FakeOutcome`` fake, never a
scheduler-owned name. Evidence values are string literals, never
``scheduler.EVIDENCE_*``.
"""
from __future__ import annotations

import collections
from pathlib import Path

import pytest

from ptest import contracts as C
from ptest.cli import main, parse_argv

_FakeOutcome = collections.namedtuple("_FakeOutcome", ("run_id", "released", "previous_state", "state", "slots", "checkout_id", "evidence", "forced"))

_RUN_ID = "a" * 32
_OTHER_ID = "b" * 32
_CHECKOUT = "c" * 32
_EVIDENCE_LOCK = "owner and guard are gone (lease lock is free)"
_EVIDENCE_ALREADY = "already finished"


def _released(run_id=_RUN_ID, slots=2, previous="QUEUED"):
    return _FakeOutcome(run_id=run_id, released=True, previous_state=previous,
                        state="RELEASED", slots=slots, checkout_id=_CHECKOUT,
                        evidence=_EVIDENCE_LOCK, forced=False)


def _already(run_id=_RUN_ID, state="RELEASED"):
    return _FakeOutcome(run_id=run_id, released=False, previous_state=state,
                        state=state, slots=1, checkout_id=_CHECKOUT,
                        evidence=_EVIDENCE_ALREADY, forced=False)


def _patch_release(monkeypatch, outcome=None, *, problem=None):
    calls = {}

    def fake(domain, run_id, *, force=False):
        calls["domain"] = domain
        calls["run_id"] = run_id
        calls["force"] = force
        if problem is not None:
            raise problem
        return outcome

    monkeypatch.setattr("ptest.scheduler.release_run", fake, raising=False)
    return calls


def _no_update_check(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("release must be exempt from the update check")

    monkeypatch.setattr("ptest.update.startup_check", forbidden)


def test_release_parser_accepts_run_id_and_force_positions():
    parsed = parse_argv(("release", _RUN_ID))
    assert parsed.command == "release"
    assert parsed.release_run_id == _RUN_ID
    assert parsed.release_force is False
    assert parse_argv(("release", _RUN_ID, "--force")).release_force is True
    assert parse_argv(("release", "--force", _RUN_ID)).release_force is True
    assert parse_argv(("release", "--force", _RUN_ID)).release_run_id == _RUN_ID
    with pytest.raises(C.Problem) as excinfo:
        parse_argv(("release",))
    assert excinfo.value.code == "invalid-config"


def test_released_line_exact_singular_and_plural(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _no_update_check(monkeypatch)
    _patch_release(monkeypatch, _released(slots=1))
    assert main(("release", _RUN_ID)) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == (
        f"released {_RUN_ID} · was QUEUED · 1 slot freed "
        f"· checkout {_CHECKOUT} · {_EVIDENCE_LOCK}\n")
    _patch_release(monkeypatch, _released(slots=3))
    assert main(("release", _RUN_ID)) == 0
    captured = capsys.readouterr()
    assert captured.out == (
        f"released {_RUN_ID} · was QUEUED · 3 slots freed "
        f"· checkout {_CHECKOUT} · {_EVIDENCE_LOCK}\n")


def test_already_terminal_line_exit_zero(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _no_update_check(monkeypatch)
    _patch_release(monkeypatch, _already())
    assert main(("release", _RUN_ID)) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == f"{_RUN_ID} is already RELEASED; nothing released\n"


def test_refusal_exact_stderr_exit_one_stdout_empty(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _no_update_check(monkeypatch)
    message = "owner pid 123 is still running"
    _patch_release(monkeypatch, problem=C.Problem(
        code="ownership-uncertain", message=message, phase="scheduler"))
    assert main(("release", _RUN_ID)) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip() == f"ptest: release refused for {_RUN_ID}: {message}"


def test_state_unavailable_exits_two(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _no_update_check(monkeypatch)
    _patch_release(monkeypatch, problem=C.Problem(
        code="state-unavailable", message="no retained lease has this run id",
        phase="scheduler"))
    assert main(("release", _OTHER_ID)) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "no retained lease has this run id" in captured.err


def test_coordinator_unavailable_exits_75(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _no_update_check(monkeypatch)
    _patch_release(monkeypatch, problem=C.Problem(
        code="coordinator-unavailable", message="locked", phase="scheduler"))
    assert main(("release", _RUN_ID)) == 75


def test_force_flag_reaches_scheduler_before_or_after_id(case, monkeypatch):
    domain = case.domain()
    for argv in (("release", _RUN_ID, "--force"),
                 ("--fixture-domain", str(domain.root), "release", "--force", _RUN_ID),
                 ("--fixture-domain", str(domain.root), "release", _RUN_ID, "--force")):
        calls = _patch_release(monkeypatch, _released())
        assert main(argv) == 0
        assert calls["force"] is True
        assert calls["run_id"] == _RUN_ID


def test_domain_passed_to_scheduler_is_fixture_domain(case, monkeypatch, capsys):
    domain = case.domain()
    root = case.project(domain)
    monkeypatch.chdir(root)
    _no_update_check(monkeypatch)
    calls = _patch_release(monkeypatch, _released())
    assert main(("--fixture-domain", str(domain.root), "release", _RUN_ID)) == 0
    assert Path(str(calls["domain"].root)) == domain.root
    capsys.readouterr()


@pytest.mark.parametrize("argv", [
    ("release",),
    ("release", "xyz"),
    ("release", "ABCDEF" + "0" * 26),
    ("release", "a" * 31),
    ("release", "a" * 33),
    ("release", _RUN_ID, "extra"),
])
def test_malformed_or_missing_run_id_exits_two_without_call(
        tmp_path, monkeypatch, capsys, argv):
    monkeypatch.chdir(tmp_path)
    calls = _patch_release(monkeypatch, _released())
    assert main(argv) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "release needs one run id (32 hex characters" in captured.err
    assert calls == {}


def test_bare_release_beside_release_dir_names_both(tmp_path, monkeypatch, capsys):
    (tmp_path / "release").mkdir()
    monkeypatch.chdir(tmp_path)
    calls = _patch_release(monkeypatch, _released())
    assert main(("release",)) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip() == (
        "invalid-config: release is both a ptest command and a path here: run "
        "`ptest ./release` for its tests, or `ptest release <run_id>` to free "
        "a stuck run")
    assert calls == {}


@pytest.mark.parametrize("argv", [
    ("release", _RUN_ID, "--json"),
    ("release", _RUN_ID, "--bogus"),
    ("release", _RUN_ID, "--force", "--force"),
    ("release", "--force", "--force", _RUN_ID),
])
def test_unknown_or_repeated_option_exits_two(tmp_path, monkeypatch, capsys, argv):
    monkeypatch.chdir(tmp_path)
    calls = _patch_release(monkeypatch, _released())
    assert main(argv) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert calls == {}
    if "--force" in argv[1:] and argv.count("--force") == 2:
        assert "option cannot be repeated" in captured.err
    else:
        assert "unknown inspection option" in captured.err


def test_release_help_flag_shows_topic(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _no_update_check(monkeypatch)
    assert main(("release", "--help")) == 0
    shown = capsys.readouterr()
    assert shown.err == ""
    assert main(("help", "release")) == 0
    direct = capsys.readouterr()
    assert shown.out == direct.out
    assert "ptest release: free a stuck run's slots" in shown.out


def test_release_never_calls_update_check(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _no_update_check(monkeypatch)
    _patch_release(monkeypatch, _released())
    assert main(("release", _RUN_ID)) == 0
    capsys.readouterr()


def test_guide_row_names_release_and_docs_match_resource():
    from importlib.resources import files

    resource = files("ptest").joinpath(
        "resources", "repository-agent-guide.md").read_text(encoding="utf-8")
    assert ("| `ownership-uncertain` | ptest could not prove that a run's processes "
            "are gone (a run started in another sandbox is judged by its lease "
            "lock only, which cannot see processes that run left behind)" in resource)
    assert "`ptest release <run_id>`" in resource
    assert "cannot see processes that run left behind" in resource
    root = Path(__file__).resolve().parent.parent.parent
    docs = (root / "docs" / "ptest-agent.md").read_text(encoding="utf-8")
    assert docs == resource
