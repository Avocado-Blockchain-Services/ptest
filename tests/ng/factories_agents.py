"""Agent provider, doctor and review factories (test-only, T5 owned).

Registered through ``tests/ng/conftest.py`` ``pytest_configure`` (never via
``pytest_plugins``).  Every fixture name starts with one of the T5 prefixes
(``fake_provider``, ``agent_``, ``doctor_``, ``review_``) and is
function-scoped; every resource a fixture creates lives under the test's
``tmp_path``.  Module-level builder functions take explicit paths so
module-level test helpers can delegate without fixture threading; their
explicit keyword-only signatures reject unknown overrides with ``TypeError``.

* ``fake_provider_bin`` / ``fake_provider``: fake ``claude``/``codex`` CLI
  executables with scripted responses.  ``fake_provider_bin`` pins ``PATH``
  to the fake dir plus the system dirs it needs and never includes
  ``~/.local/bin``, so a fake can never resolve a real provider.
* ``doctor_*``: in-process reviewer fakes (qualification + review) and the
  command-kind project builder used by the agent-doctor acceptance journeys.
* ``agent_*``: assessment / recommendation record builders (citation, row,
  finding, score, api facts) shared by the contract, recommendation and
  render suites, plus review-context builders (config, resolution, domain,
  workspace, packet) shared by the assessment, review-context and
  deterministic-items suites.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from ptest import contracts as C
from support import (
    ptest_toml_text,
    write_executable,
    write_file,
)

AGENT_CHILD_PID = "ab" * 16
AGENT_CITATION_SHA = "ef" * 32
AGENT_PACKET_SHA = "cd" * 32

AGENT_RECORD_RECIPES = {
    "FIX-001": "factories", "FIX-002": "factories",
    "DB-001": "databases", "DB-002": "databases",
    "CACHE-001": "cache", "RESOURCE-001": "files-ports",
    "NETWORK-001": "time-network", "PROCESS-001": "processes",
    "TIME-001": "time-network", "SELECT-001": None,
    "TIMING-001": None, "PARALLEL-001": None,
}

# System dirs a fake provider child may need (sh, cat, sleep).  The real
# PATH is filtered, never inherited wholesale, so ~/.local/bin (or any
# other .local entry, or a cwd-relative entry) can never leak in.
_SYSTEM_PATH_FALLBACK = ("/usr/bin", "/bin")


def _pinned_system_path() -> str:
    parts = []
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry or not os.path.isabs(entry):
            continue
        if ".local" in entry.split("/"):
            continue
        if entry not in parts:
            parts.append(entry)
    for entry in _SYSTEM_PATH_FALLBACK:
        if entry not in parts:
            parts.append(entry)
    return os.pathsep.join(parts)


@pytest.fixture
def fake_provider_bin(tmp_path, monkeypatch):
    """Per-test provider bin dir with PATH pinned to it plus system dirs."""
    bindir = tmp_path / "fake-provider-bin"
    bindir.mkdir()
    system = _pinned_system_path()
    monkeypatch.setenv(
        "PATH", str(bindir) if not system else str(bindir) + os.pathsep + system
    )
    return bindir


def write_fake_provider(bin_dir, name, *, stdout="", exit_code=0,
                        script=None) -> Path:
    """Write a fake provider executable; return its path.

    The default script consumes stdin, replays ``stdout`` (str or bytes)
    from a sidecar file and exits with ``exit_code``.  Pass ``script`` for
    bespoke behaviour (dispatch, delays, fifos); it is written verbatim.
    """
    if isinstance(exit_code, bool) or not isinstance(exit_code, int):
        raise TypeError("exit_code must be an int")
    bindir = Path(bin_dir)
    if script is None:
        payload = stdout if isinstance(stdout, bytes) else str(stdout).encode()
        blob = bindir / f"{name}.payload"
        blob.write_bytes(payload)
        script = ("#!/bin/sh\ncat >/dev/null\ncat \"" + str(blob)
                  + f"\"\nexit {exit_code}\n")
    return write_executable(bindir / name, script)


@pytest.fixture
def fake_provider(fake_provider_bin):
    """Factory for fake provider executables on ``fake_provider_bin``."""
    def make(name, *, stdout="", exit_code=0, script=None):
        return write_fake_provider(
            fake_provider_bin, name, stdout=stdout,
            exit_code=exit_code, script=script,
        )
    return make


def fake_provider_env(bin_dir) -> dict:
    """Minimal resolve env pinning PATH to one fake bin dir."""
    return {"PATH": str(bin_dir)}


def agent_echo_claude_script(log=None, delay_s=0.0) -> str:
    """Python fake source: echo the stdin packet id in a Claude envelope."""
    lines = [
        "import json, sys, time",
    ]
    if log is not None:
        lines.append(
            f"open({log!r}, 'a').write('start %d\\n' % time.monotonic_ns())")
    lines.append("body = sys.stdin.read()")
    if delay_s:
        lines.append(f"time.sleep({delay_s!r})")
    if log is not None:
        lines.append(
            f"open({log!r}, 'a').write('end %d\\n' % time.monotonic_ns())")
    lines += [
        "item = json.loads(body)['id']",
        "envelope = {'type': 'result', 'subtype': 'success',",
        "            'is_error': False, 'num_turns': 1,",
        "            'permission_denials': [], 'result': item}",
        "sys.stdout.write(json.dumps(envelope))",
    ]
    return "\n".join(lines) + "\n"


def agent_codex_stream(*lines: str) -> bytes:
    return ("\n".join(lines) + "\n").encode("utf-8")


# ---- doctor reviewer fakes -------------------------------------------------


def doctor_patch_qualification(monkeypatch, *, unavailable: bool = False):
    """Patch provider qualification/resolution with synthetic fakes."""
    from ptest.agent_providers import QualificationStatus, ReviewerAdapter

    statuses = []

    def qualification_status(name: str):
        statuses.append(name)
        return QualificationStatus(
            name=name, qualified=True, argv=(name,),
            note="synthetic acceptance profile",
        )

    def resolve_reviewer(name: str, env):
        if unavailable:
            raise C.Problem(
                code="provider-unavailable",
                message="synthetic provider unavailable",
                phase="provider", retryable=False,
            )
        return ReviewerAdapter(
            name=name, executable=f"/synthetic/{name}",
            argv=(f"/synthetic/{name}",), qualified=True,
            qualification_note="synthetic acceptance adapter",
        )

    monkeypatch.setattr(
        "ptest.cli.agent_providers.qualification_status", qualification_status)
    monkeypatch.setattr(
        "ptest.cli.agent_providers.resolve_reviewer", resolve_reviewer)
    return statuses


@pytest.fixture
def doctor_qualification(monkeypatch):
    """Available-qualification fake; returns the queried names."""
    return doctor_patch_qualification(monkeypatch)


def doctor_one_row_assessment(request: bytes) -> bytes:
    """One one-row reply with a cited cache-isolation gap row."""
    import json as _json

    body = _json.loads(request)
    item_id = body["policy"]["item"]["id"]
    excerpts = body["excerpts"]
    cache_id = next(row_id for row_id in C.AGENT_ASSESSMENT_CHECKLIST_IDS
                    if row_id.startswith("CACHE-"))
    if not excerpts:
        return _json.dumps({
            "status": "unknown",
            "rationale": ("The item subset admitted no excerpts for "
                          "this row."),
            "evidence": [],
            "finding": None,
            "proof": [],
            "needs": [],
        }).encode("utf-8")
    excerpt = excerpts[0]
    citation = {
        key: excerpt[key]
        for key in ("path", "start_line", "end_line", "sha256")
    }
    is_gap = item_id == cache_id
    quote = excerpt["text"].splitlines()[0][:512]
    return _json.dumps({
        "status": "gap" if is_gap else "unknown",
        "rationale": (
            "The cited bounded source shows cache cleanup without evidence "
            "that another owner remains isolated."
            if is_gap else
            "The supplied bounded evidence does not establish this criterion."
        ),
        "evidence": [citation] if is_gap else [],
        "proof": ([{"role": "applicability", "citation_index": 0,
                    "quote": quote},
                   {"role": "violation", "citation_index": 0,
                    "quote": quote}] if is_gap else []),
        "needs": [],
        "finding": ({
            "summary": "Cache cleanup has no neighbor ownership assertion.",
            "suggested_change": (
                "Add an executable test that proves a neighbor cache key "
                "survives cleanup."
            ),
            "evidence": [citation],
        } if is_gap else None),
    }).encode("utf-8")


def doctor_install_fake_review(monkeypatch, *, cancelled: bool = False):
    """Install the fake ``launch_reviews``; return the launches list."""
    import json as _json

    from ptest.agent_providers import ProviderResult

    launches = []

    def launch_many(adapter, requests, timeout_s, *, concurrency=4,
                    on_done=None, progress=None, deadline=None):
        results = []
        for request, schema in requests:
            body = _json.loads(request)
            launches.append(
                (adapter.name, body["packet"]["declaration"], timeout_s))
            results.append(ProviderResult(
                provider=adapter.name,
                ok=not cancelled,
                assessment=b"" if cancelled else doctor_one_row_assessment(
                    request),
                error="cancelled" if cancelled else "",
                exit_code=None if cancelled else 0,
                timed_out=False,
                cancelled=cancelled,
                truncated=False,
                pid=7301 + len(launches),
                argv=adapter.argv,
                # Payload-only scratch label, never a filesystem path.
                scratch="/tmp/ptest-agent-doctor-acceptance",
            ))
            if on_done is not None:
                on_done(len(results) - 1, results[-1])
        return tuple(results)

    monkeypatch.setattr(
        "ptest.cli.agent_providers.launch_reviews", launch_many)
    return launches


@pytest.fixture
def doctor_review(monkeypatch):
    """Installed fake review; returns the launches list."""
    return doctor_install_fake_review(monkeypatch)


def doctor_assessment_project(root, project_id: str,
                              *, runner: str = "command") -> Path:
    """Command-kind project with one cache test file; return its root."""
    root = Path(root)
    write_ptest_toml(
        root, kind=runner, launcher=("python",), args=[], full_args=(),
        test_roots=("tests",), workers=1, project_id=project_id,
    )
    write_file(root / "tests" / "test_cache.py",
               "def test_cache_cleanup():\n    cache.flushall()\n")
    return root


# ---- assessment / recommendation record builders ---------------------------


def agent_citation(path="src/example.py", start=3, end=9,
                   sha=AGENT_CITATION_SHA):
    return {"path": path, "start_line": start, "end_line": end,
            "sha256": sha}


def agent_row(row_id, status="satisfied", rationale=None, evidence=None,
              label=None):
    if rationale is None:
        rationale = f"Row {row_id} judged {status} against packet excerpt."
    if evidence is None:
        evidence = [] if status == "unknown" else [agent_citation()]
    row = {"id": row_id, "status": status, "rationale": rationale,
           "evidence": evidence}
    if label is not None:
        row["label"] = label
    return row


def agent_finding(row_id, summary=None, change=None, recipe="__catalog__",
                  evidence=None):
    if recipe == "__catalog__":
        recipe = AGENT_RECORD_RECIPES[row_id]
    return {"id": row_id,
            "summary": summary or f"Close gap {row_id} with owned setup.",
            "suggested_change": change or f"Apply packaged recipe for {row_id}.",
            "recipe_id": recipe,
            "evidence": evidence if evidence is not None else [agent_citation()]}


def agent_score(satisfied, applicable):
    return {"satisfied": satisfied, "applicable": applicable,
            "percent": (100 * satisfied) // applicable}


def agent_api_facts(**overrides):
    facts = {
        "project": "api", "runner": "pytest", "runs": True,
        "runs_reason": None, "runs_fix": None,
        "parallel": "4 workers (xdist, --dist loadgroup)",
        "parallel_short": "4 workers", "parallel_fix": None,
        "setup": "uv sync --locked",
        "full_suite": 'your pytest config: -m "not slow"',
        "full_blocked": None,
    }
    facts.update(overrides)
    return facts


# ---- review-context builders -----------------------------------------------


def agent_config(pid=AGENT_CHILD_PID, runner_kind=C.RunnerKind.PYTEST,
                 **runner_overrides):
    kwargs = dict(kind=runner_kind, launcher=("uv",),
                  test_roots=("tests",))
    kwargs.update(runner_overrides)
    return C.Config(
        runner=C.RunnerConfig(**kwargs),
        setup=None,
        resources=C.ResourceConfig(),
        selection=C.SelectionPolicy(enabled=False, closed_inputs=False),
        project_id=pid,
    )


def agent_resolution(root, config=None):
    return C.ConfigResolution(
        root=root, path=None,
        config=config if config is not None else agent_config(),
        monorepo=None, provenance=(), warnings=(), problem=None,
    )


def agent_domain(root):
    root = Path(root)
    return C.DomainPaths(
        root=root, machine_config=root / ".ptest" / "config.toml",
        ledger=root / ".ptest" / "ledger",
        marker=root / ".ptest" / "marker",
        fixture=True, domain_id=None,
    )


def agent_workspace(root, config=None):
    from ptest import doctor

    resolution = agent_resolution(root, config)
    return doctor.inspect_workspace(
        agent_domain(root), resolution, C.DEFAULT_SCAN_LIMITS, None), resolution


def agent_packet_for(root, files=None, config=None):
    """Build one standalone packet over a tmp project."""
    from ptest import agent_assessment as AA

    for rel, text in (files or {}).items():
        target = Path(root) / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    workspace, _ = agent_workspace(root, config)
    packets = AA.build_packets(
        workspace, agent_resolution(root, config), AA.EvidenceLimits())
    assert len(packets) == 1
    return packets[0]


def agent_v1_config_text(project_id: str, runner_kind: str) -> str:
    """Canonical v1 command-style config text via ``support``."""
    return ptest_toml_text(
        kind=runner_kind, launcher=("true",), args=[], full_args=(),
        test_roots=("tests",), workers=1, project_id=project_id,
    )
