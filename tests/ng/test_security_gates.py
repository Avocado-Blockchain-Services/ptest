from __future__ import annotations

from pathlib import Path
import importlib.util

_spec = importlib.util.spec_from_file_location("security_checks", Path(__file__).parents[2] / "scripts" / "security-checks.py")
security_checks = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(security_checks)


def test_unavailable_detector_is_unpassed(monkeypatch, tmp_path):
    monkeypatch.setattr(security_checks.shutil, "which", lambda _: None)
    result = security_checks.run_gate(tmp_path, "bandit")
    assert result == {"tool": "bandit", "status": "unpassed", "reason": "unavailable"}


def test_insensitive_detector_is_unpassed(monkeypatch, tmp_path):
    monkeypatch.setattr(security_checks.shutil, "which", lambda _: "/bin/detector")
    monkeypatch.setattr(security_checks.subprocess, "run", lambda *a, **k: type("R", (), {"returncode": 0})())
    result = security_checks.run_gate(tmp_path, "bandit")
    assert result["status"] == "unpassed"
    assert result["reason"] == "sensitivity-failed"
