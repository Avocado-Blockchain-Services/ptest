"""The Spot backend is retired but not deleted: switched off, not removed.

The queue, worker, controller and their Terraform stay in the tree so the
experiment can be resumed. What changes is that nothing reaches them unless a
project asks for `backend = "spot_queue"` AND the switch is thrown, and that a
`local` backend never wanders into the Spot submitter by falling off the end of
a dispatch chain — which is what broke `--full` for every local project.
"""

import pytest


CFG_OFF = {"defaults": {}}
CFG_ON = {"defaults": {"spot_enabled": True}}
SPOT = {"backend": "spot_queue", "kind": "pytest"}
CLOUD = {"backend": "cloudrun", "kind": "pytest"}
LOCAL = {"backend": "local", "kind": "pytest"}


def test_spot_is_off_by_default(ptest):
    assert ptest.spot_enabled(SPOT, CFG_OFF) is False


def test_spot_turns_back_on_from_the_defaults(ptest):
    assert ptest.spot_enabled(SPOT, CFG_ON) is True


def test_a_project_can_turn_spot_on_by_itself(ptest):
    pcfg = {"backend": "spot_queue", "spot_enabled": True}
    assert ptest.spot_enabled(pcfg, CFG_OFF) is True


def test_only_a_real_true_counts(ptest):
    """A truthy string in TOML must not read as "on"."""
    assert ptest.spot_enabled({"spot_enabled": "yes"}, CFG_OFF) is False


def test_disabled_spot_refuses_without_reaching_the_queue(ptest, tmp_path, monkeypatch):
    reached = []
    monkeypatch.setattr(ptest, "run_spot_queue",
                        lambda *a, **kw: reached.append(1))
    rc = ptest.run_remote_backend(SPOT, CFG_OFF, "p", tmp_path, "pytest")
    assert rc == 2
    assert reached == [], "the Spot submitter must not be reached while disabled"


def test_enabled_spot_still_dispatches(ptest, tmp_path, monkeypatch):
    """Disabled, not deleted — one flag brings the whole path back."""
    monkeypatch.setattr(ptest, "run_spot_queue", lambda *a, **kw: 0)
    assert ptest.run_remote_backend(SPOT, CFG_ON, "p", tmp_path, "pytest") == 0


def test_cloudrun_is_unaffected_by_the_spot_switch(ptest, tmp_path, monkeypatch):
    monkeypatch.setattr(ptest, "run_cloudrun", lambda *a, **kw: 0)
    monkeypatch.setattr(ptest, "run_spot_queue",
                        lambda *a, **kw: pytest.fail("cloudrun must never reach Spot"))
    assert ptest.run_remote_backend(CLOUD, CFG_OFF, "p", tmp_path, "pytest") == 0


def test_a_local_backend_is_never_a_remote_backend(ptest):
    assert ptest.backend_is_remote(LOCAL) is False
    assert ptest.backend_is_remote({}) is False
    assert ptest.backend_is_remote(CLOUD) is True
    assert ptest.backend_is_remote(SPOT) is True


def test_local_backend_never_reaches_the_spot_submitter(ptest, tmp_path, monkeypatch):
    """The regression itself: `local` fell through the dispatch chain to Spot."""
    monkeypatch.setattr(ptest, "run_spot_queue",
                        lambda *a, **kw: pytest.fail("a local backend reached Spot"))
    assert ptest.run_remote_backend(LOCAL, CFG_OFF, "p", tmp_path, "pytest") == 2
