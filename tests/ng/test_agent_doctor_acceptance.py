"""Deterministic acceptance journeys for agent-backed doctor and init.

All projects live under pytest's temporary root. Provider qualification and
review are represented by in-process fakes; this module never invokes a
provider CLI or a project test runner.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys

import pytest

from ptest import contracts as C
from ptest.cli import main


def _write_v1(root, project_id: str, *, runner: str = "command") -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / ".ptest.toml").write_text(
        "version = 1\n"
        f'project_id = "{project_id}"\n'
        "[runner]\n"
        f'kind = "{runner}"\n'
        'launcher = ["python"]\n'
        'args = []\n'
        'full_args = []\n'
        'test_roots = ["tests"]\n'
        "workers = 1\n"
        'lifecycle = "cooperative-process-group"\n',
        encoding="utf-8",
    )
    tests = root / "tests"
    tests.mkdir(exist_ok=True)
    (tests / "test_cache.py").write_text(
        "def test_cache_cleanup():\n"
        "    cache.flushall()\n",
        encoding="utf-8",
    )


def _write_v2(root, *, runner: str = "command") -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / ".ptest.toml").write_text(
        'version = 2\n[monorepo]\nchildren = ["api", "web"]\n',
        encoding="utf-8",
    )
    for name, prefix in (("api", "ab"), ("web", "cd")):
        _write_v1(root / name, prefix * 16, runner=runner)


def _tree_state(root):
    state = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if path.is_symlink():
            state[relative] = ("symlink", path.readlink())
        elif path.is_file():
            state[relative] = ("file", path.read_bytes())
        elif path.is_dir():
            state[relative] = ("directory",)
    return state


def _patch_qualification(monkeypatch, *, unavailable: bool = False):
    from ptest.agent_providers import (
        QualificationStatus,
        ReviewerAdapter,
    )

    statuses = []

    def qualification_status(name: str):
        statuses.append(name)
        return QualificationStatus(
            name=name, qualified=True, argv=(name,), note="synthetic acceptance profile",
        )

    def resolve_reviewer(name: str, env):
        if unavailable:
            raise C.Problem(
                code="provider-unavailable", message="synthetic provider unavailable",
                phase="provider", retryable=False,
            )
        return ReviewerAdapter(
            name=name, executable=f"/synthetic/{name}",
            argv=(f"/synthetic/{name}",), qualified=True,
            qualification_note="synthetic acceptance adapter",
        )

    monkeypatch.setattr("ptest.cli.agent_providers.qualification_status", qualification_status)
    monkeypatch.setattr("ptest.cli.agent_providers.resolve_reviewer", resolve_reviewer)
    return statuses


def _one_row_assessment(request: bytes) -> bytes:
    """Return one one-row reply with a cited cache-isolation gap row."""
    body = json.loads(request)
    item_id = body["policy"]["item"]["id"]
    excerpts = body["excerpts"]
    cache_id = next(row_id for row_id in C.AGENT_ASSESSMENT_CHECKLIST_IDS
                    if row_id.startswith("CACHE-"))
    if not excerpts:
        return json.dumps({
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
    return json.dumps({
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
                "Add an executable test that proves a neighbor cache key survives cleanup."
            ),
            "evidence": [citation],
        } if is_gap else None),
    }).encode("utf-8")


def _install_fake_review(monkeypatch, *, cancelled: bool = False):
    from ptest.agent_providers import ProviderResult

    launches = []

    def launch_many(adapter, requests, timeout_s, *, concurrency=4,
                    on_done=None,
                    progress=None):
        results = []
        for request, schema in requests:
            body = json.loads(request)
            launches.append(
                (adapter.name, body["packet"]["declaration"], timeout_s))
            results.append(ProviderResult(
                provider=adapter.name,
                ok=not cancelled,
                assessment=b"" if cancelled else _one_row_assessment(request),
                error="cancelled" if cancelled else "",
                exit_code=None if cancelled else 0,
                timed_out=False,
                cancelled=cancelled,
                truncated=False,
                pid=7301 + len(launches),
                argv=adapter.argv,
                scratch="/tmp/ptest-agent-doctor-acceptance",
            ))
            if on_done is not None:
                on_done(len(results) - 1, results[-1])
        return tuple(results)

    monkeypatch.setattr(
        "ptest.cli.agent_providers.launch_reviews", launch_many)
    return launches


@pytest.mark.parametrize("topology", ["standalone", "v2-monorepo"])
def test_static_doctor_modes_compose_without_review_or_report_writes(
        tmp_path, monkeypatch, capsys, topology):
    root = tmp_path / topology
    if topology == "standalone":
        _write_v1(root, "ab" * 16)
        expected_sources = ("tests/test_cache.py",)
    else:
        _write_v2(root)
        (root / "root_noise_test.py").write_text(
            "cache.flushall()\n", encoding="utf-8")
        expected_sources = ("api/tests/test_cache.py", "web/tests/test_cache.py")
    monkeypatch.chdir(root)
    monkeypatch.setattr(
        "ptest.cli.agent_providers.resolve_reviewer",
        lambda *args, **kwargs: pytest.fail("static doctor resolved a reviewer"),
    )
    monkeypatch.setattr(
        "ptest.cli.agent_providers.launch_reviews",
        lambda *args, **kwargs: pytest.fail("static doctor launched a reviewer"),
    )
    before = _tree_state(root)

    assert main(("doctor", "--offline")) == 0
    human = capsys.readouterr()
    assert "ptest doctor" in human.out
    assert human.err == ""
    for source in expected_sources:
        assert source in human.out
    if topology == "v2-monorepo":
        assert human.out.index("| 1 | api ") < human.out.index("| 2 | web ")
        assert "root_noise_test.py" not in human.out

    assert main(("doctor", "--offline", "--json")) == 0
    offline = capsys.readouterr()
    public = C.decode_public_document(offline.out.encode("utf-8"))
    assert public.kind == "agent-assessment" and public.error is None
    assert public.data["provider"]["name"] == "offline"
    assert [child["scope"] for child in public.data["children"]] == (
        ["."] if topology == "standalone" else ["api", "web"])
    assert public.data["publication"]["status"] == "skipped"
    assert offline.err == ""
    assert _tree_state(root) == before


def test_init_created_and_existing_configs_offer_review_but_decline_and_no_doctor_stay_offline(
        tmp_path, monkeypatch, capsys):
    root = tmp_path / "init-project"
    root.mkdir()
    git = root / ".git"
    git.mkdir()
    (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (git / "config").write_text("[core]\n\trepositoryformatversion = 0\n",
                                encoding="utf-8")
    (root / "pyproject.toml").write_text(
        '[project]\ndependencies = ["pytest>=8"]\n', encoding="utf-8")
    monkeypatch.chdir(root)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.delenv("CI", raising=False)
    statuses = _patch_qualification(monkeypatch)
    answers = iter(("codex", "no", "codex", "no"))
    inputs = []

    def input_answer():
        answer = next(answers)
        inputs.append(answer)
        return answer

    monkeypatch.setattr("builtins.input", input_answer)
    launches = []
    monkeypatch.setattr(
        "ptest.cli.agent_providers.launch_reviews",
        lambda *args, **kwargs: launches.append(args) or pytest.fail(
            "declined init launched a reviewer"),
    )

    assert main(("init", "--runner", "pytest")) == 0
    created = capsys.readouterr()
    config = root / ".ptest.toml"
    assert config.is_file() and "created" in created.out
    assert re.search(r"created +\.ptest\.toml", created.out)
    assert "created:" not in created.out
    assert "Optimization review is disabled" in created.err
    original_config = config.read_bytes()

    assert main(("init", "--runner", "pytest")) == 0
    existing = capsys.readouterr()
    assert re.search(r"config +unchanged +\.ptest\.toml", existing.out)
    assert "unchanged:" not in existing.out
    assert "already present" not in existing.out
    # File actions group by action: one unchanged line names every file.
    assert re.search(
        r"guidance +unchanged +docs/ptest-agent\.md, AGENTS\.md, "
        r"ptest skill for codex", existing.out)
    assert "Optimization review is disabled" in existing.err
    assert config.read_bytes() == original_config
    assert inputs == ["codex", "no", "codex", "no"]
    assert launches == []

    qualifications_before_no_doctor = len(statuses)
    guidance_prompts = []
    monkeypatch.setattr(
        "builtins.input",
        lambda: guidance_prompts.append("asked") or "none",
    )
    assert main(("init", "--runner", "pytest", "--no-doctor")) == 0
    no_doctor = capsys.readouterr()
    assert "Model review disclosure" not in no_doctor.err
    assert "Run this review once?" not in no_doctor.err
    assert len(statuses) == qualifications_before_no_doctor
    assert guidance_prompts == ["asked"]
    assert inputs == ["codex", "no", "codex", "no"]
    assert launches == []
    assert not (root / "recommendations.md").exists()


def test_non_tty_bare_doctor_requires_consent_before_resolution_and_preserves_report(
        tmp_path, monkeypatch, capsys):
    from ptest import cli
    from ptest import doctor as doctor_api
    from ptest.agent_providers import ReviewerAdapter

    root = tmp_path / "configured"
    _write_v1(root, "ab" * 16)
    real_inspect = doctor_api.inspect_workspace
    report = root / "recommendations.md"
    report.write_bytes(b"user-owned report; preserve exact bytes\n")
    before = (report.read_bytes(), report.stat().st_ino,
              report.stat().st_mtime_ns, report.stat().st_ctime_ns)
    monkeypatch.chdir(root)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.delenv("CI", raising=False)
    resolution_calls = []
    launch_calls = []

    def resolve(*args, **kwargs):
        resolution_calls.append(args)
        return ReviewerAdapter("claude", "/synthetic/claude", ("/synthetic/claude",))

    monkeypatch.setattr("ptest.cli.agent_providers.resolve_reviewer", resolve)
    monkeypatch.setattr(
        "ptest.cli.agent_providers.launch_reviews",
        lambda *args, **kwargs: launch_calls.append(args),
    )
    monkeypatch.setattr(
        "ptest.cli.doctor.inspect_workspace",
        lambda *args, **kwargs: pytest.fail("bare non-TTY doctor scanned source before consent"),
    )

    assert main(("doctor",)) == 2
    captured = capsys.readouterr()
    assert "consent-required" in captured.err + captured.out
    assert resolution_calls == []
    assert launch_calls == []
    assert (report.read_bytes(), report.stat().st_ino,
            report.stat().st_mtime_ns, report.stat().st_ctime_ns) == before

    # Sensitivity check: temporarily bypass the shared consent entry point.
    # The fake provider is cancelled immediately, and the no-launch property
    # below demonstrably fails while this local mutation is active.
    adapter = ReviewerAdapter("claude", "/synthetic/claude", ("/synthetic/claude",),
                              qualified=True, qualification_note="test-only bypass")
    from ptest.agent_providers import ProviderResult

    def cancelled_launches(adapter, requests, timeout_s, *, concurrency=4,
                           on_done=None,
                    progress=None):
        launch_calls.append((adapter.name,))
        return tuple(
            ProviderResult(
                provider=adapter.name, ok=False, assessment=b"",
                error="cancelled", exit_code=None, timed_out=False,
                cancelled=True, truncated=False, pid=7401, argv=adapter.argv,
                scratch="/tmp/ptest-agent-doctor-acceptance",
            )
            for _ in requests)

    def bypass(parsed, resolution, domain, *, interactive, preconsented=False):
        cli._run_doctor_review(parsed, resolution, domain, adapter=adapter,
                               interactive=interactive,
                               preconsented=preconsented)
        return False

    with monkeypatch.context() as disabled_boundary:
        disabled_boundary.setattr("ptest.cli.agent_providers.launch_reviews",
                                  cancelled_launches)
        disabled_boundary.setattr("ptest.cli._run_review_entry", bypass)
        disabled_boundary.setattr("ptest.cli.doctor.inspect_workspace",
                                  real_inspect)
        assert main(("doctor",)) == 130
        capsys.readouterr()
        with pytest.raises(AssertionError):
            assert launch_calls == []


def test_v2_review_emits_capabilities_first_public_assessment_and_self_verifying_report(
        tmp_path, monkeypatch, capsys):
    root = tmp_path / "review-monorepo"
    _write_v2(root, runner="pytest")
    monkeypatch.chdir(root)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setenv("PTEST_RECOMMENDATIONS_LOCK_DIR", str(tmp_path / "review-locks"))
    statuses = _patch_qualification(monkeypatch)
    launches = _install_fake_review(monkeypatch)
    monkeypatch.setattr(
        "ptest.operations.execute",
        lambda *args, **kwargs: pytest.fail("doctor review executed project tests"),
    )

    review_argv = ("doctor", "--reviewer", "claude", "--allow-model-review")
    assert main(review_argv) == 0
    human = capsys.readouterr()
    assert "api" in human.out and "web" in human.out
    assert human.out.index("api") < human.out.index("web")
    assert "recommendations.md" in human.out
    assert "api  pytest · " in human.out and "web  pytest · " in human.out
    assert human.out.index("api  pytest · ") < human.out.index(
        "web  pytest · ")
    assert re.search(r"api  pytest · \d+ ok · \d+ gap · \d+ unknown",
                     human.out)
    assert "Report: recommendations.md (created)" in human.out
    declarations = [item[1] for item in launches]
    api_count = declarations.count("api")
    web_count = declarations.count("web")
    assert api_count > 0 and web_count > 0
    assert declarations == ["api"] * api_count + ["web"] * web_count

    report = root / "recommendations.md"
    contents = report.read_bytes()
    marker, body = contents.split(b"\n", 1)
    match = re.fullmatch(
        rb"<!-- ptest-recommendations v1 sha256=([0-9a-f]{64}) -->", marker,
    )
    assert match is not None
    digest = hashlib.sha256(body).hexdigest().encode("ascii")
    assert match.group(1) == digest
    rendered = body.decode("utf-8")
    for proof in (
        "Execution verification: not run",
        "Verification focus (catalog; not run): Neighbor key survives "
        "concurrent cleanup.",
        "cross-owner reads, overwrites, and deletes",
        "Observed command: (blank)",
        "Observed cwd: (blank)",
        "Observed exit status: (blank)",
        "Observed output: (blank)",
        "Status: unverified",
        "ptest --full",
    ):
        assert proof in rendered
    assert "ptest api" not in rendered
    assert "ptest web" not in rendered

    assert main((*review_argv, "--json")) == 0
    public = C.decode_public_document(capsys.readouterr().out.encode("utf-8"))
    assert public.kind == "agent-assessment" and public.error is None
    assert set(public.data) == {
        "schema", "provider", "children", "limitations", "publication",
    }
    assert public.data["schema"] == C.AGENT_ASSESSMENT_SCHEMA
    assert public.data["provider"]["name"] == "claude"
    assert [child["scope"] for child in public.data["children"]] == ["api", "web"]
    for child in public.data["children"]:
        assert [row["id"] for row in child["rows"]] == list(
            C.AGENT_ASSESSMENT_CHECKLIST_IDS)
        assert child["score"] == {
            "satisfied": 0, "applicable": 11, "percent": 0}
        selection = next(row for row in child["rows"]
                         if row["id"] == "SELECT-001")
        assert selection["status"] == "not-applicable"
        assert all(row["label"] for row in child["rows"])
        assert child["execution"]["status"] in ("executable", "caveat")
    assert public.data["publication"]["path"] == "recommendations.md"
    updated_contents = report.read_bytes()
    updated_marker, updated_body = updated_contents.split(b"\n", 1)
    updated_match = re.fullmatch(
        rb"<!-- ptest-recommendations v1 sha256=([0-9a-f]{64}) -->",
        updated_marker,
    )
    assert updated_match is not None
    updated_digest = hashlib.sha256(updated_body).hexdigest().encode("ascii")
    assert updated_match.group(1) == updated_digest
    assert public.data["publication"]["sha256"] == hashlib.sha256(
        updated_contents).hexdigest()
    assert public.data["publication"]["status"] == "unchanged"
    rerun = [item[1] for item in launches][api_count + web_count:]
    assert rerun == declarations
    assert statuses == ["claude"] * 2


@pytest.mark.parametrize(
    ("failure", "expected_code", "expected_exit"),
    [("unavailable", "provider-unavailable", 2),
     ("cancelled", "review-cancelled", 130)],
)
def test_review_unavailable_or_cancelled_preserves_existing_report(
        tmp_path, monkeypatch, capsys, failure, expected_code, expected_exit):
    root = tmp_path / failure
    _write_v1(root, "ef" * 16)
    report = root / "recommendations.md"
    report.write_bytes(b"previous complete result\n")
    before = _tree_state(root)
    monkeypatch.chdir(root)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.delenv("CI", raising=False)
    _patch_qualification(monkeypatch, unavailable=(failure == "unavailable"))
    launches = []
    if failure == "cancelled":
        launches = _install_fake_review(monkeypatch, cancelled=True)

    assert main(("doctor", "--reviewer", "claude", "--allow-model-review",
                 "--json")) == expected_exit
    document = C.decode_public_document(capsys.readouterr().out.encode("utf-8"))
    assert document.kind == "doctor" and document.data is None
    assert document.error.code == expected_code
    assert _tree_state(root) == before
    scopes = [item[1] for item in launches]
    if failure == "unavailable":
        assert scopes == []
    else:
        assert scopes and all(scope == "." for scope in scopes)


def test_review_passes_heartbeat_progress_to_launch_reviews(
        tmp_path, monkeypatch, capsys):
    """cli wires its throttled heartbeat into launch_reviews progress."""
    from ptest import cli as cli_module

    root = tmp_path / "review-heartbeat"
    _write_v2(root, runner="pytest")
    monkeypatch.chdir(root)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setenv(
        "PTEST_RECOMMENDATIONS_LOCK_DIR", str(tmp_path / "hb-locks"))
    _patch_qualification(monkeypatch)
    _install_fake_review(monkeypatch)
    seen = []
    fake = cli_module.agent_providers.launch_reviews

    def spy(adapter, requests, timeout_s, **kwargs):
        seen.append(kwargs.get("progress"))
        return fake(adapter, requests, timeout_s, **kwargs)

    monkeypatch.setattr("ptest.cli.agent_providers.launch_reviews", spy)
    monkeypatch.setattr(
        "ptest.operations.execute",
        lambda *args, **kwargs: pytest.fail(
            "doctor review executed project tests"),
    )

    assert main(("doctor", "--reviewer", "claude", "--allow-model-review")) == 0
    assert seen, "review launched no items"
    assert all(callable(progress) for progress in seen)


@pytest.mark.parametrize("argv", [
    ("doctor", "--prompt"),
    ("doctor", "--assessment-json"),
    ("doctor", "--offline", "--prompt"),
])
def test_doctor_removed_output_flags_are_unknown_options(argv):
    from ptest.cli import parse_argv

    with pytest.raises(C.Problem, match="unknown inspection option"):
        parse_argv(argv)


def test_doctor_json_emits_versioned_assessment_document(
        tmp_path, monkeypatch, capsys):
    root = tmp_path / "json-review"
    _write_v1(root, "ab" * 16)
    monkeypatch.chdir(root)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.delenv("CI", raising=False)
    _patch_qualification(monkeypatch)
    _install_fake_review(monkeypatch)
    monkeypatch.setattr(
        "ptest.operations.execute",
        lambda *args, **kwargs: pytest.fail("doctor review executed project tests"),
    )

    assert main(("doctor", "--reviewer", "claude", "--allow-model-review",
                 "--json")) == 0
    public = C.decode_public_document(capsys.readouterr().out.encode("utf-8"))
    assert public.kind == "agent-assessment" and public.error is None
    assert set(public.data) == {
        "schema", "provider", "children", "limitations", "publication",
    }
    assert public.data["provider"]["name"] == "claude"
    assert [child["scope"] for child in public.data["children"]] == ["."]
    assert (root / "recommendations.md").is_file()


def test_doctor_offline_json_emits_assessment_document_without_review(
        tmp_path, monkeypatch, capsys):
    root = tmp_path / "offline-json"
    _write_v1(root, "ab" * 16)
    before = _tree_state(root)
    monkeypatch.chdir(root)
    monkeypatch.setattr(
        "ptest.cli.agent_providers.resolve_reviewer",
        lambda *args, **kwargs: pytest.fail("offline json resolved a reviewer"),
    )
    monkeypatch.setattr(
        "ptest.cli.agent_providers.launch_reviews",
        lambda *args, **kwargs: pytest.fail("offline json launched a reviewer"),
    )

    assert main(("doctor", "--offline", "--json")) == 0
    public = C.decode_public_document(capsys.readouterr().out.encode("utf-8"))
    assert public.kind == "agent-assessment" and public.error is None
    assert public.data["schema"] == C.AGENT_ASSESSMENT_SCHEMA
    assert public.data["provider"]["name"] == "offline"
    assert [child["scope"] for child in public.data["children"]] == ["."]
    for child in public.data["children"]:
        assert [row["id"] for row in child["rows"]] == list(
            C.AGENT_ASSESSMENT_CHECKLIST_IDS)
        assert any(row["status"] == "unknown" for row in child["rows"])
    assert public.data["publication"]["status"] == "skipped"
    assert _tree_state(root) == before
