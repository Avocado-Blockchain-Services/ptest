"""Hermetic doctor smoke coverage (Task: doctor checklist).

Ordinary tests here are fully hermetic: synthetic repositories under tmp_path,
library-level inspection only, no network, no real checkouts. The single
external test is opt-in via ``PTEST_DOCTOR_SMOKE_MANIFEST`` and skipped
otherwise; it never reads ``/home/ingmar/code`` directly.
"""
from __future__ import annotations

import json
import os
import hashlib
import stat
import subprocess
from pathlib import Path

import pytest

from ptest import contracts as C


def _write_v1(root: Path, project_id: str) -> None:
    (root / ".ptest.toml").write_text(
        "version = 1\n"
        f'project_id = "{project_id}"\n'
        "[runner]\n"
        'kind = "command"\n'
        'launcher = ["true"]\n'
        "args = []\n"
        "full_args = []\n"
        'test_roots = ["tests"]\n'
        "workers = 1\n"
        'lifecycle = "cooperative-process-group"\n',
        encoding="utf-8",
    )


def test_smoke_standalone_reports_bounded_worksheet_and_unknown_readiness(case):
    from ptest.doctor import inspect_workspace
    from ptest.render import render_doctor, repair_prompt

    domain = case.domain()
    root = case.project(domain)
    (root / "tests").mkdir(exist_ok=True)
    (root / "tests" / "cache_test.py").write_text(
        "def test_cache(client):\n    client.flushall()\n", encoding="utf-8")
    resolution = C.ConfigResolution(
        root=root, path=None, config=case.config(), provenance=(),
        warnings=(), problem=None,
    )
    workspace = inspect_workspace(domain, resolution, C.DEFAULT_SCAN_LIMITS, None)

    assert [repo.declaration for repo in workspace.repositories] == ["."]
    assert workspace.aggregate.scope == ()
    assert [(item.code, item.path) for item in workspace.aggregate.findings] == [
        ("cache.global-flush", "tests/cache_test.py")]
    assert all(item.state in {"blocked", "unknown"}
               for item in workspace.aggregate.readiness)

    text = render_doctor(workspace.aggregate, workspace=workspace)
    assert text.startswith("ptest doctor")
    assert "FIX-001" in text and "TIMING-001" in text
    prompt = repair_prompt(workspace.aggregate, workspace=workspace)
    assert "Assessment request:" in prompt
    assert len(prompt.encode("utf-8")) <= C.MAX_PROMPT_BYTES


def test_smoke_monorepo_reports_children_in_declaration_order(case, tmp_path):
    from ptest import config as config_api
    from ptest.doctor import inspect_workspace

    (tmp_path / ".ptest.toml").write_text(
        "version = 2\n[monorepo]\nchildren = [\"api\", \"web\"]\n",
        encoding="utf-8",
    )
    for child, project_id in (("api", "ab"), ("web", "cd")):
        root = tmp_path / child
        (root / "tests").mkdir(parents=True)
        _write_v1(root, project_id * 16)
        (root / "tests" / "cache_test.py").write_text(
            "def test_cache(client):\n    client.flushall()\n", encoding="utf-8")

    domain = case.domain()
    workspace = inspect_workspace(
        domain, config_api.resolve_config(tmp_path), C.DEFAULT_SCAN_LIMITS, None)

    assert [repo.declaration for repo in workspace.repositories] == ["api", "web"]
    assert [item.path for item in workspace.aggregate.findings] == [
        "api/tests/cache_test.py", "web/tests/cache_test.py"]
    # The bounded scanner inspects each declared child manifest as well as its
    # test source; both are ordinary regular files in the doctor contract.
    assert workspace.aggregate.usage.files == 4


def test_smoke_external_manifest_is_opt_in_and_sandboxed():
    """External real-repository smoke runs only with an explicit manifest.

    The manifest names an absolute candidate ``ptest`` binary and snapshot
    directories prepared by the integration gate. Without it this test
    skips; it never touches real checkouts on its own.
    """
    manifest_path = os.environ.get("PTEST_DOCTOR_SMOKE_MANIFEST")
    if not manifest_path:
        pytest.skip("no PTEST_DOCTOR_SMOKE_MANIFEST; external smoke is opt-in")
    manifest_file = Path(manifest_path).resolve(strict=True)
    smoke_root = manifest_file.parent
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    assert manifest.get("version") == 1
    assert set(manifest) >= {"binary", "wheel", "wheel_sha256", "snapshots"}
    assert manifest["binary"] == "venv/bin/ptest"
    assert isinstance(manifest["wheel"], str)
    assert manifest["wheel"].startswith("wheel/ptest_ng-")
    assert manifest["wheel"].endswith(".whl")

    def owned(relative: object, *, directory: bool = False) -> Path:
        assert isinstance(relative, str) and relative and not Path(relative).is_absolute()
        parts = Path(relative).parts
        assert all(part not in {"", ".", ".."} for part in parts)
        current = smoke_root
        for part in parts:
            current = current / part
            stamp = os.lstat(current)
            assert not stat.S_ISLNK(stamp.st_mode)
        resolved = current.resolve(strict=True)
        assert resolved.is_relative_to(smoke_root)
        assert stat.S_ISDIR(os.lstat(resolved).st_mode) if directory else stat.S_ISREG(os.lstat(resolved).st_mode)
        return resolved

    binary = owned(manifest["binary"])
    wheel = owned(manifest["wheel"])
    assert hashlib.sha256(wheel.read_bytes()).hexdigest() == manifest["wheel_sha256"]
    assert manifest["snapshots"], "manifest must name at least one snapshot"
    snapshots = []
    approved_snapshots = {
        "monorepo-root": "snapshots/persea_content_maker_unified",
        "scammeter-api": "snapshots/persea_scam_meter",
        "scammeter-web": "snapshots/persea_scam_meter_ui",
    }
    for item in manifest["snapshots"]:
        assert isinstance(item, dict) and set(item) >= {"name", "path"}
        assert item["name"] in approved_snapshots
        assert item["path"] == approved_snapshots[item["name"]]
        snapshots.append((item["name"], owned(item["path"], directory=True)))
    assert {name for name, _snapshot in snapshots} == set(approved_snapshots)

    for name, snapshot in snapshots:
        def run(*args):
            return subprocess.run([str(binary), *args], cwd=snapshot,
                                  text=True, capture_output=True, timeout=60)
        human = run("doctor")
        assert human.returncode == 0 and "ptest doctor" in human.stdout
        assert "FIX-001" in human.stdout and "TIMING-001" in human.stdout
        structured = run("doctor", "--json")
        assert structured.returncode == 0
        assert C.decode_public_document(structured.stdout).kind == "doctor"
        prompt = run("doctor", "--prompt")
        assert prompt.returncode == 0 and "Assessment request:" in prompt.stdout
        guide = run("guide")
        assert guide.returncode == 0 and "Doctor assessment checklist" in guide.stdout
        if name == "monorepo-root":
            for scope in ("api", "web", "api/tests"):
                assert run("doctor", "--scope", scope).returncode == 0
            assert run("doctor", "--max-files", "1").returncode == 0
            for unsafe in ("ghost", "../api", "api/.ptest.toml"):
                rejected = run("doctor", "--scope", unsafe)
                assert rejected.returncode == 2 and not rejected.stdout
