"""Evidence admission, strict validation, and scoring (task 2 owned).

TDD contract for ``ptest.agent_assessment``: bounded packet collection with
explicit coverage counts and SHA-256 line identities, single-packet strict
validation reusing the frozen ``PublicDocument`` contract, and floor scoring.

Abuse twins (secure-by-spec): symlinked/private/secret-bearing/instruction/
generated/dependency content is never admitted; hostile packet text cannot
become citations; stale packets, duplicate/reordered IDs, unjustified N/A,
and model-supplied commands/scores are rejected, never projected away.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from ptest import contracts as C
from ptest import doctor

CHILD_PID = "ab" * 16
EXPECTED_IDS = (
    "FIX-001", "FIX-002", "DB-001", "DB-002", "CACHE-001",
    "RESOURCE-001", "NETWORK-001", "PROCESS-001", "TIME-001",
    "SELECT-001", "TIMING-001", "PARALLEL-001",
)


def _config(pid=CHILD_PID, runner_kind=C.RunnerKind.PYTEST):
    return C.Config(
        runner=C.RunnerConfig(
            kind=runner_kind, launcher=("uv",),
            test_roots=("tests",),
        ),
        setup=None,
        resources=C.ResourceConfig(),
        selection=C.SelectionPolicy(enabled=False, closed_inputs=False),
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


def _monorepo_root(root: Path, children: dict[str, str]) -> Path:
    (root / ".ptest.toml").write_text(
        "version = 2\n[monorepo]\nchildren = "
        + json.dumps(list(children)) + "\n",
        encoding="utf-8",
    )
    for declaration, config_text in children.items():
        child = root / declaration
        child.mkdir(parents=True)
        (child / ".ptest.toml").write_text(config_text, encoding="utf-8")
    return root


def _packet_for(root: Path, files: dict[str, str] | None = None):
    """Build one standalone packet over a tmp project."""
    from ptest import agent_assessment as AA

    for rel, text in (files or {}).items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    workspace, resolution = _workspace(root)
    packets = AA.build_packets(workspace, resolution, AA.EvidenceLimits())
    assert len(packets) == 1
    return packets[0]


def test_standalone_scoped_packet_excludes_files_outside_selected_directory(
        tmp_path):
    from ptest import agent_assessment as AA

    inside = tmp_path / "tests" / "test_inside.py"
    inside.parent.mkdir()
    inside.write_text("def test_inside():\n    assert True\n",
                      encoding="utf-8")
    outside = tmp_path / "src" / "private_sentinel.py"
    outside.parent.mkdir()
    outside.write_text("OUTSIDE_SCOPE_SENTINEL_61c0\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        "OUTSIDE_DEPENDENCY_SENTINEL_61c0\n", encoding="utf-8")
    resolution = _resolution(tmp_path)
    workspace = doctor.inspect_workspace(
        _domain(tmp_path), resolution, C.DEFAULT_SCAN_LIMITS, "tests")

    packet = AA.build_packets(workspace, resolution)[0]
    reviews = AA.plan_item_reviews(packet)
    planned = [review for review in reviews if review.request is not None]

    assert planned
    assert all(b"OUTSIDE_SCOPE_SENTINEL_61c0" not in review.request
               for review in planned)
    assert packet.declaration == "."
    assert packet.scope == "tests"
    assert [excerpt.path for excerpt in packet.excerpts] == [
        "tests/test_inside.py"]
    assert all(fact.ref_path != "pyproject.toml"
               for fact in packet.dependencies)


def test_v2_scoped_packet_rebases_scope_and_excludes_sibling_evidence(
        tmp_path):
    from ptest import agent_assessment as AA
    from ptest import config as config_api

    root = _monorepo_root(tmp_path, {
        "api": _v1_config_text("11" * 16, "pytest"),
    })
    inside = root / "api" / "tests" / "test_inside.py"
    inside.parent.mkdir()
    inside.write_text("def test_inside():\n    assert True\n",
                      encoding="utf-8")
    outside = root / "api" / "src" / "private_sentinel.py"
    outside.parent.mkdir()
    outside.write_text("OUTSIDE_SCOPE_SENTINEL_7ac4\n", encoding="utf-8")
    (root / "api" / "pyproject.toml").write_text(
        "OUTSIDE_DEPENDENCY_SENTINEL_7ac4\n", encoding="utf-8")
    resolution = config_api.resolve_config(root)
    workspace = doctor.inspect_workspace(
        _domain(root), resolution, C.DEFAULT_SCAN_LIMITS, "api/tests")

    packet = AA.build_packets(workspace, resolution)[0]
    reviews = AA.plan_item_reviews(packet)
    planned = [review for review in reviews if review.request is not None]

    assert planned
    assert packet.declaration == "api"
    assert packet.scope == "api/tests"
    assert [excerpt.path for excerpt in packet.excerpts] == [
        "api/tests/test_inside.py"]
    assert all(fact.ref_path != "api/pyproject.toml"
               for fact in packet.dependencies)
    for review in planned:
        body = json.loads(review.request.decode("utf-8"))
        assert b"OUTSIDE_SCOPE_SENTINEL_7ac4" not in review.request
        assert body["packet"]["declaration"] == "api"
        assert body["packet"]["scope"] == "api/tests"
        assert body["packet"]["packet_sha256"] == packet.packet_sha256
        assert {excerpt["path"] for excerpt in body["excerpts"]} <= {
            "api/tests/test_inside.py"}


@pytest.mark.parametrize(("local_scope", "workspace_scope"), [
    ("../outside", "../outside"),
    (42, "tests"),
    ("missing", "missing"),
    ("tests/test_inside.py", "tests/test_inside.py"),
    ("tests", "other"),
])
def test_build_packets_fails_closed_for_invalid_or_mismatched_local_scope(
        tmp_path, local_scope, workspace_scope):
    from ptest import agent_assessment as AA

    test_file = tmp_path / "tests" / "test_inside.py"
    test_file.parent.mkdir()
    test_file.write_text("assert True\n", encoding="utf-8")
    resolution = _resolution(tmp_path)
    inspected = doctor.inspect_workspace(
        _domain(tmp_path), resolution, C.DEFAULT_SCAN_LIMITS, "tests")
    repo = replace(inspected.repositories[0], local_scope=local_scope)
    workspace = replace(inspected, scope=(workspace_scope,),
                        repositories=(repo,))

    with pytest.raises(C.Problem):
        AA.build_packets(workspace, resolution)


def test_build_packets_fails_closed_when_local_scope_is_a_symlink(
        tmp_path):
    from ptest import agent_assessment as AA

    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_inside.py").write_text(
        "assert True\n", encoding="utf-8")
    (tmp_path / "linked-tests").symlink_to("tests", target_is_directory=True)
    resolution = _resolution(tmp_path)
    inspected = doctor.inspect_workspace(
        _domain(tmp_path), resolution, C.DEFAULT_SCAN_LIMITS, "tests")
    repo = replace(inspected.repositories[0], local_scope="linked-tests")
    workspace = replace(inspected, scope=("linked-tests",),
                        repositories=(repo,))

    with pytest.raises(C.Problem):
        AA.build_packets(workspace, resolution)


def test_scoping_at_excluded_directory_does_not_bypass_exclusion(
        tmp_path):
    from ptest import agent_assessment as AA

    excluded = tmp_path / ".claude"
    excluded.mkdir()
    (excluded / "notes.md").write_text(
        "EXCLUDED_SCOPE_SENTINEL_aa91\n", encoding="utf-8")
    resolution = _resolution(tmp_path)
    workspace = doctor.inspect_workspace(
        _domain(tmp_path), resolution, C.DEFAULT_SCAN_LIMITS, ".claude")

    packet = AA.build_packets(workspace, resolution)[0]
    reviews = AA.plan_item_reviews(packet)

    assert packet.excerpts == ()
    assert len(reviews) == 12
    assert all(b"EXCLUDED_SCOPE_SENTINEL_aa91" not in review.request
               for review in reviews if review.request is not None)
    assert packet.excluded_count > 0


def test_v2_full_child_packet_excludes_excluded_declaration_path(tmp_path):
    from ptest import agent_assessment as AA
    from ptest import config as config_api

    root = _monorepo_root(tmp_path, {
        ".claude": _v1_config_text("11" * 16, "pytest"),
    })
    sentinel = root / ".claude" / "notes.md"
    sentinel.write_text("EXCLUDED_DECLARATION_SENTINEL_0f42\n",
                        encoding="utf-8")
    resolution = config_api.resolve_config(root)
    workspace = doctor.inspect_workspace(
        _domain(root), resolution, C.DEFAULT_SCAN_LIMITS, None)

    packets = AA.build_packets(workspace, resolution)
    assert len(packets) == 1
    packet = packets[0]
    reviews = AA.plan_item_reviews(packet)

    assert resolution.monorepo.children == (".claude",)
    assert packet.declaration == ".claude"
    assert packet.scope == ".claude"
    assert packet.excerpts == ()
    assert packet.file_count == 0
    assert packet.byte_count == 0
    assert packet.excluded_count == 1
    assert len(reviews) == 12
    assert all(b"EXCLUDED_DECLARATION_SENTINEL_0f42" not in review.request
               for review in reviews if review.request is not None)


def _citation_for(packet, start=1, end=None):
    excerpt = packet.excerpts[0]
    return {"path": excerpt.path, "start_line": start,
            "end_line": end if end is not None else excerpt.end_line,
            "sha256": excerpt.sha256}


# --- build_packets: bounded admission ---------------------------------------

def test_build_packets_admits_bounded_evidence_with_counts_and_identity(
        tmp_path):
    from ptest import agent_assessment as AA

    packet = _packet_for(tmp_path, {
        "src/example.py": "import os\nprint(os.environ.get('X'))\n",
        "tests/test_example.py": "def test_x():\n    assert True\n",
    })
    assert packet.declaration == "."
    assert packet.file_count == 2
    assert packet.byte_count > 0
    assert packet.excluded_count >= 0 and packet.truncated_count == 0
    assert len(packet.excerpts) == 2
    for excerpt in packet.excerpts:
        assert excerpt.start_line == 1
        assert excerpt.end_line >= 1
        assert excerpt.sha256 == hashlib.sha256(
            excerpt.text.encode("utf-8")).hexdigest()
    body = (json.dumps({"declaration": packet.declaration,
                        "excerpts": [e.path for e in packet.excerpts]},
                       sort_keys=True).encode())
    assert len(packet.packet_sha256) == 64 and body


def test_rejected_candidates_consume_candidate_file_budget(
        tmp_path, monkeypatch):
    from ptest import agent_assessment as AA

    candidate_count = 300
    for index in range(candidate_count):
        rejected = b"\x00" if index % 2 == 0 else b"\xff"
        (tmp_path / f"candidate-{index:03}.py").write_bytes(rejected)
    workspace, resolution = _workspace(tmp_path)
    real_read = AA.read_regular
    reads = []

    def counted_read(root, relative, limit):
        raw = real_read(root, relative, limit)
        reads.append((relative, len(raw)))
        return raw

    monkeypatch.setattr(AA, "read_regular", counted_read)
    packet = AA.build_packets(
        workspace, resolution, AA.EvidenceLimits())[0]

    assert len(reads) <= 256
    assert sum(size for _, size in reads) <= 2 * 1024 * 1024
    assert packet.excluded_count == len(reads)
    assert packet.truncated_count == candidate_count - len(reads)
    assert packet.excerpts == () and packet.byte_count == 0


def test_nested_manifests_do_not_starve_test_configuration(tmp_path):
    """70 nested package.json files must not push conftest/vitest out.

    Tier 0 admits only child-root manifests and locks; nested ones rank
    last, so the 64-file cap still admits test configuration first.
    """
    from ptest import agent_assessment as AA

    assert AA._admission_tier("package.json") == 0
    assert AA._admission_tier("web/packages/pkg00/package.json") == 5
    files = {
        "package.json": '{"name": "demo"}\n',
        "vitest.config.ts": "export default {};\n",
        "tests/conftest.py": "import json\n",
    }
    for index in range(70):
        files[f"web/packages/pkg{index:02d}/package.json"] = (
            '{"name": "nested"}\n')
    packet = _packet_for(tmp_path, files)
    paths = {excerpt.path for excerpt in packet.excerpts}
    assert "package.json" in paths
    assert "vitest.config.ts" in paths
    assert "tests/conftest.py" in paths


def test_rejected_candidates_consume_candidate_byte_budget(
        tmp_path, monkeypatch):
    from ptest import agent_assessment as AA

    candidate_count = 40
    for index in range(candidate_count):
        (tmp_path / f"candidate-{index:03}.py").write_bytes(
            b"\x00" + b"x" * (65_537 - 1))
    workspace, resolution = _workspace(tmp_path)
    real_read = AA.read_regular
    reads = []

    def counted_read(root, relative, limit):
        raw = real_read(root, relative, limit)
        reads.append((relative, len(raw)))
        return raw

    monkeypatch.setattr(AA, "read_regular", counted_read)
    packet = AA.build_packets(
        workspace, resolution, AA.EvidenceLimits())[0]

    read_bytes = sum(size for _, size in reads)
    assert len(reads) <= 256
    assert read_bytes <= 2 * 1024 * 1024
    assert 0 < packet.excluded_count < candidate_count
    assert packet.excluded_count + packet.truncated_count == candidate_count
    assert packet.excerpts == () and packet.byte_count == 0


def test_candidate_read_budgets_are_configurable_in_evidence_limits(
        tmp_path, monkeypatch):
    from ptest import agent_assessment as AA

    candidate_count = 4
    for index in range(candidate_count):
        (tmp_path / f"candidate-{index:03}.py").write_bytes(b"\x00")
    workspace, resolution = _workspace(tmp_path)
    real_read = AA.read_regular
    reads = []

    def counted_read(root, relative, limit):
        raw = real_read(root, relative, limit)
        reads.append((relative, len(raw)))
        return raw

    monkeypatch.setattr(AA, "read_regular", counted_read)
    limits = AA.EvidenceLimits(max_candidate_files_per_child=2,
                               max_candidate_bytes_per_child=128)
    packet = AA.build_packets(workspace, resolution, limits)[0]

    assert [path for path, _ in reads] == [
        "candidate-000.py", "candidate-001.py"]
    assert packet.excluded_count == 2
    assert packet.truncated_count == 2


def test_packet_walk_checks_total_deadline_and_reports_progress(
        tmp_path, monkeypatch):
    import time
    from ptest import agent_assessment as AA

    (tmp_path / "a.py").write_text("a = 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("b = 2\n", encoding="utf-8")
    workspace, resolution = _workspace(tmp_path)
    now = [0.0]
    checkpoints = []
    monkeypatch.setattr(time, "monotonic", lambda: now[0])

    def progress():
        checkpoints.append(now[0])
        now[0] += 1

    with pytest.raises(C.Problem) as caught:
        AA.build_packets(workspace, resolution, deadline=8.0,
                         progress=progress)

    assert caught.value.code == "review-timeout"
    assert len(checkpoints) >= 8


def test_packet_walk_does_not_materialize_a_large_directory_past_its_cap(
        tmp_path, monkeypatch):
    from types import SimpleNamespace
    import stat
    from ptest import agent_assessment as AA

    class Entry:
        def __init__(self, index):
            self.name = f"file-{index:05}.py"

        def is_symlink(self):
            return False

        def is_dir(self, *, follow_symlinks):
            return False

        def stat(self, *, follow_symlinks):
            return SimpleNamespace(st_mode=stat.S_IFREG | 0o644, st_size=1)

    class HugeDirectory:
        def __init__(self):
            self.visited = 0

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def __iter__(self):
            return self

        def __next__(self):
            if self.visited == AA.MAX_WALK_ENTRIES + 50_000:
                raise StopIteration
            self.visited += 1
            return Entry(self.visited)

    directory = HugeDirectory()
    monkeypatch.setattr(AA.os, "scandir", lambda _path: directory)
    entries = []

    AA._iter_regular_files(tmp_path, "", entries)

    assert directory.visited <= AA.MAX_WALK_ENTRIES + 1
    assert len([entry for entry in entries if entry[0] == "file"]) <= (
        AA.MAX_WALK_ENTRIES)
    assert entries[-1][0] == "skip"


def test_packet_source_read_is_checked_against_total_deadline(
        tmp_path, monkeypatch):
    import time
    from ptest import agent_assessment as AA

    (tmp_path / "source.py").write_text("value = 1\n", encoding="utf-8")
    workspace, resolution = _workspace(tmp_path)
    now = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])

    def read_then_expire(*_args):
        now[0] = 1.0
        return b"value = 1\n"

    monkeypatch.setattr(AA, "read_regular", read_then_expire)
    with pytest.raises(C.Problem) as caught:
        AA.build_packets(workspace, resolution, deadline=1.0)

    assert caught.value.code == "review-timeout"


def test_packet_prompt_trimming_checks_total_deadline(
        tmp_path, monkeypatch):
    import time
    from ptest import agent_assessment as AA

    (tmp_path / "source.py").write_text("value = 1\n", encoding="utf-8")
    workspace, resolution = _workspace(tmp_path)
    now = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    real_dumps = AA.json.dumps

    def dump_then_expire(value, *args, **kwargs):
        result = real_dumps(value, *args, **kwargs)
        if isinstance(value, dict) and "excerpts" in value:
            now[0] = 1.0
        return result

    monkeypatch.setattr(AA.json, "dumps", dump_then_expire)
    with pytest.raises(C.Problem) as caught:
        AA.build_packets(
            workspace, resolution, AA.EvidenceLimits(max_prompt_bytes=1),
            deadline=1.0)

    assert caught.value.code == "review-timeout"


def test_build_packets_excludes_symlink_secret_instruction_generated_dependency(  # noqa: E501
        tmp_path):
    from ptest import agent_assessment as AA

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "real.py").write_text("x = 1\n", encoding="utf-8")
    os.symlink("real.py", tmp_path / "src" / "link.py")
    (tmp_path / ".env").write_text("SECRET=top\n", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("follow me\n", encoding="utf-8")
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "notes.md").write_text("x\n", encoding="utf-8")
    (tmp_path / "app.min.js").write_text("x\n", encoding="utf-8")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "env.py").write_text("x\n", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "objects").write_text("x\n", encoding="utf-8")
    packet = _packet_for(tmp_path)
    paths = [e.path for e in packet.excerpts]
    assert paths == ["src/real.py"]
    assert packet.excluded_count >= 6


def test_build_packets_skips_nonregular_and_invalid_utf8(tmp_path):
    packet = _packet_for(tmp_path, {"src/ok.py": "x = 1\n"})
    bad = tmp_path / "src" / "bad.py"
    bad.write_bytes(b"\xff\xfe not utf8 \x00\n")
    try:
        os.mkfifo(tmp_path / "src" / "pipe.py")
    except OSError:
        pytest.skip("fifo unavailable")
    workspace, resolution = _workspace(tmp_path)
    from ptest import agent_assessment as AA
    packets = AA.build_packets(workspace, resolution, AA.EvidenceLimits())
    paths = [e.path for e in packets[0].excerpts]
    assert "src/ok.py" in paths
    assert "src/bad.py" not in paths
    assert "src/pipe.py" not in paths
    assert packets[0].excluded_count >= 2


def test_build_packets_rejects_invalid_utf8_cut_to_empty_and_skips_empty_file(
        tmp_path, monkeypatch):
    from ptest import agent_assessment as AA

    (tmp_path / "empty.py").write_bytes(b"")
    # The one-byte cap cuts this nonempty declaration at its invalid lead byte.
    (tmp_path / "pyproject.toml").write_bytes(b"\xffx")
    workspace, resolution = _workspace(tmp_path)
    real_read = AA.read_regular
    reads = []

    def counted_read(root, relative, limit):
        raw = real_read(root, relative, limit)
        reads.append((relative, len(raw), limit))
        return raw

    monkeypatch.setattr(AA, "read_regular", counted_read)
    packet = AA.build_packets(
        workspace, resolution, AA.EvidenceLimits(max_bytes_per_file=1))[0]

    # A 0-byte file carries no evidence: it is excluded, never admitted
    # with a line-1 bound over zero lines (which later fails as stale).
    assert [excerpt.path for excerpt in packet.excerpts] == []
    assert not any(fact.ref_path == "pyproject.toml"
                   for fact in packet.dependencies)
    assert packet.file_count == 0 and packet.byte_count == 0
    assert packet.excluded_count == 1
    assert packet.truncated_count == 1
    assert len(reads) == 2
    assert sum(size for _, size, _ in reads) <= 2 * len(reads)
    assert all(size <= limit for _, size, limit in reads)


def test_build_packets_enforces_per_file_and_total_caps(tmp_path):
    from ptest import agent_assessment as AA

    big = "x" * 70000 + "\n"
    files = {f"src/f{i}.py": "x = 1\n" for i in range(4)}
    files["src/big.py"] = big
    packet = _packet_for(tmp_path, files)
    assert packet.file_count <= 64
    assert packet.byte_count <= 512 * 1024
    assert packet.truncated_count >= 1
    for excerpt in packet.excerpts:
        assert len(excerpt.text.encode("utf-8")) <= 64 * 1024


def test_build_packets_preserves_child_order_and_authority(tmp_path):
    from ptest import agent_assessment as AA

    for child in ("child-a", "child-b"):
        (tmp_path / child / "src").mkdir(parents=True)
        (tmp_path / child / "src" / "m.py").write_text(
            f"# {child}\nx = 1\n", encoding="utf-8")
    first = doctor.inspect_workspace(
        _domain(tmp_path), _resolution(tmp_path), C.DEFAULT_SCAN_LIMITS,
        None).repositories[0].report
    repo_a = doctor.RepositoryInspection(
        declaration="child-a", local_scope=None, report=first,
        config_problem=None, config=_config("11" * 16))
    repo_b = doctor.RepositoryInspection(
        declaration="child-b", local_scope=None, report=first,
        config_problem=None, config=_config("22" * 16))
    workspace = doctor.WorkspaceInspection(
        scope=(), repositories=(repo_a, repo_b), aggregate=first)
    packets = AA.build_packets(
        workspace, _resolution(tmp_path), AA.EvidenceLimits())
    assert [p.declaration for p in packets] == ["child-a", "child-b"]
    assert packets[0].packet_sha256 != packets[1].packet_sha256
    assert all(p.scope == p.declaration for p in packets)


def test_build_packets_use_each_v2_child_config_in_manifest_order(tmp_path):
    from ptest import agent_assessment as AA
    from ptest import config as config_api

    root = _monorepo_root(tmp_path, {
        "child-b": _v1_config_text("22" * 16, "command"),
        "child-a": _v1_config_text("11" * 16, "pytest"),
    })
    for declaration in ("child-b", "child-a"):
        source = root / declaration / "src" / "module.py"
        source.parent.mkdir()
        source.write_text(f"# {declaration}\nx = 1\n", encoding="utf-8")

    resolution = config_api.resolve_config(root)
    workspace = doctor.inspect_workspace(
        _domain(root), resolution, C.DEFAULT_SCAN_LIMITS, None)
    packets = AA.build_packets(workspace, resolution, AA.EvidenceLimits())

    assert resolution.config is None  # v2 root has no child identity fallback
    assert [repo.declaration for repo in workspace.repositories] == [
        "child-b", "child-a"]
    assert [(packet.project_id, packet.runner_kind) for packet in packets] == [
        ("22" * 16, "command"), ("11" * 16, "pytest")]
    assert all(packet.project_id != "0" * 32 for packet in packets)
    assert [packet.scope for packet in packets] == ["child-b", "child-a"]


@pytest.mark.parametrize("bad_config", [None, "malformed child config"])
def test_build_packets_reject_child_without_valid_config_before_building_any(
        tmp_path, monkeypatch, bad_config):
    from ptest import agent_assessment as AA

    report = _workspace(tmp_path)[0].repositories[0].report
    workspace = doctor.WorkspaceInspection(
        scope=(),
        repositories=(
            doctor.RepositoryInspection(
                declaration="child-a", local_scope=None, report=report,
                config_problem=None, config=_config("11" * 16)),
            doctor.RepositoryInspection(
                declaration="child-b", local_scope=None, report=report,
                config_problem=None, config=bad_config),
        ),
        aggregate=report,
    )
    built = []
    monkeypatch.setattr(AA, "_build_one_packet",
                        lambda *args: built.append(args))

    with pytest.raises(C.Problem) as caught:
        AA.build_packets(workspace, _resolution(tmp_path), AA.EvidenceLimits())

    assert caught.value.code == "invalid-config"
    assert built == []


def test_build_packets_dependency_provenance_is_static_and_unknown_where_unprovable(  # noqa: E501
        tmp_path):
    """No project-local env means ``uninspectable`` even though ptest
    itself runs from a virtualenv; ptest's runtime is never evidence."""
    packet = _packet_for(tmp_path, {
        "pyproject.toml": "[project]\nname = 'demo'\n",
        "uv.lock": "version = 1\n",
        "src/m.py": "x = 1\n",
    })
    kinds = {(d.ecosystem, d.status) for d in packet.dependencies}
    assert ("python", "declared") in kinds
    assert ("python-lock", "locked") in kinds
    env = [d for d in packet.dependencies
           if d.ecosystem == "environment"]
    assert len(env) == 1 and env[0].status == "uninspectable"
    assert ("Environments other than reported project-local metadata are "
            "not inspected." in env[0].detail)
    assert not any(fact.status == "installed"
                   for fact in packet.dependencies)
    assert all(".venv" not in e.path for e in packet.excerpts)


# --- score: floor math, unknown in denominator, null on zero applicable ------

def test_score_floor_unknown_in_denominator_and_null(tmp_path):
    from ptest import agent_assessment as AA

    packet = _packet_for(tmp_path, {"src/m.py": "x = 1\n"})
    excerpt = packet.excerpts[0]

    def row(row_id, status):
        rationale = (f"Row {row_id} judged {status} against "
                     f"{excerpt.path} excerpt.")
        evidence = [] if status == "unknown" else [_citation_for(packet)]
        if status == "not-applicable":
            rationale = (f"Affirmative: {excerpt.path} shows no artifact "
                         f"for {row_id}, confirmed by manifest.")
        from ptest.checklist import CATALOG as _CATALOG

        label = next(entry.label for entry in _CATALOG
                     if entry.id == row_id)
        return AA.AssessmentRow(id=row_id, status=status,
                                rationale=rationale,
                                evidence=tuple(
                                    AA.Citation(**c) for c in evidence),
                                label=label)

    rows = ((row("FIX-001", "satisfied"),)
            + tuple(row(i, "unknown") for i in ("FIX-002", "DB-001"))
            + tuple(row(i, "gap") for i in EXPECTED_IDS[3:]))
    result = AA.score(rows)
    assert (result.satisfied, result.applicable, result.percent) == (1, 12, 8)
    na_rows = tuple(row(i, "not-applicable") for i in EXPECTED_IDS)
    assert AA.score(na_rows) is None
    assert AA.score(()) is None


# --- project-local environment metadata (safe, no imports/execution) ---------

def _make_venv(root: Path, version="3.11.9", dists=None):
    site = root / ".venv" / "lib" / "python3.11" / "site-packages"
    site.mkdir(parents=True)
    (root / ".venv" / "pyvenv.cfg").write_text(
        f"home = /usr/bin\nversion = {version}\n", encoding="utf-8")
    for name in (dists if dists is not None
                 else ("pytest-8.3.4", "coverage-7.6.1")):
        (site / f"{name}.dist-info").mkdir()
    return site


def test_build_packets_reports_installed_python_environment(tmp_path):
    from ptest import agent_assessment as AA

    _make_venv(tmp_path)
    packet = _packet_for(tmp_path, {"src/m.py": "x = 1\n"})
    installed = [fact for fact in packet.dependencies
                 if fact.status == "installed"]
    assert len(installed) == 1
    detail = installed[0].detail
    assert "3.11.9" in detail
    assert "pytest 8.3.4" in detail and "coverage 7.6.1" in detail
    assert "2 distributions" in detail
    assert "/usr/bin" not in detail
    assert len(detail) <= 512
    assert not any(fact.status == "uninspectable"
                   for fact in packet.dependencies)
    assert all(".venv" not in excerpt.path for excerpt in packet.excerpts)


def test_build_packets_ignores_symlinked_venv(tmp_path):
    from ptest import agent_assessment as AA

    real = tmp_path / "real-env"
    site = real / "lib" / "python3.11" / "site-packages"
    site.mkdir(parents=True)
    (real / "pyvenv.cfg").write_text("home = /usr/bin\nversion = 9.9.9\n",
                                     encoding="utf-8")
    (site / "pytest-9.9.9.dist-info").mkdir()
    os.symlink(str(real), tmp_path / ".venv")
    packet = _packet_for(tmp_path, {"src/m.py": "x = 1\n"})
    assert not any(fact.status == "installed"
                   for fact in packet.dependencies)
    assert any(fact.status == "uninspectable"
               for fact in packet.dependencies)
    assert "9.9.9" not in " ".join(
        fact.detail for fact in packet.dependencies)


def test_build_packets_ignores_unsafe_pyvenv_cfg(tmp_path):
    """A symlinked, oversized, or non-regular pyvenv.cfg never hangs the
    scan and never yields an ``installed`` fact without a version."""
    from ptest import agent_assessment as AA

    _make_venv(tmp_path, dists=("pytest-8.3.4",))
    cfg = tmp_path / ".venv" / "pyvenv.cfg"
    target = tmp_path / "evil.cfg"
    target.write_text("home = /usr/bin\nversion = 6.6.6\n", encoding="utf-8")
    cfg.unlink()
    os.symlink(str(target), cfg)
    packet = _packet_for(tmp_path, {"src/m.py": "x = 1\n"})
    assert not any(fact.status == "installed"
                   for fact in packet.dependencies)
    assert any(fact.status == "uninspectable"
               for fact in packet.dependencies)
    assert "6.6.6" not in " ".join(
        fact.detail for fact in packet.dependencies)
    cfg.unlink()
    cfg.write_text("home = /usr/bin\nversion = 7.7.7\n" + "x" * 8192,
                   encoding="utf-8")
    packet = _packet_for(tmp_path, {"src/m.py": "x = 1\n"})
    assert not any(fact.status == "installed"
                   for fact in packet.dependencies)
    assert "7.7.7" not in " ".join(
        fact.detail for fact in packet.dependencies)


def test_build_packets_ignores_fifo_pyvenv_cfg_without_hang(tmp_path):
    from ptest import agent_assessment as AA

    _make_venv(tmp_path, dists=("pytest-8.3.4",))
    cfg = tmp_path / ".venv" / "pyvenv.cfg"
    cfg.unlink()
    try:
        os.mkfifo(cfg)
    except OSError:
        pytest.skip("fifo unavailable")
    packet = _packet_for(tmp_path, {"src/m.py": "x = 1\n"})
    assert not any(fact.status == "installed"
                   for fact in packet.dependencies)
    assert any(fact.status == "uninspectable"
               for fact in packet.dependencies)


def test_build_packets_ignores_escaping_dist_info_symlink(tmp_path):
    from ptest import agent_assessment as AA

    site = _make_venv(tmp_path, dists=("pytest-8.3.4",))
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "evil-1.2.3.dist-info").mkdir()
    os.symlink(str(outside / "evil-1.2.3.dist-info"),
               site / "evil-1.2.3.dist-info")
    packet = _packet_for(tmp_path, {"src/m.py": "x = 1\n"})
    installed = [fact for fact in packet.dependencies
                 if fact.status == "installed"]
    assert len(installed) == 1
    assert "evil" not in installed[0].detail
    assert "1 distribution" in installed[0].detail


def test_build_packets_sanitizes_hostile_distribution_names(tmp_path):
    from ptest import agent_assessment as AA

    site = _make_venv(tmp_path, dists=("pytest-8.3.4",))
    for hostile in ("evil-<script>-1.0", "back`tick-2.0",
                    "with space-3.0", "x" * 200 + "-4.0",
                    "pytest-9.9.9\nrun-me", "coverage-[x](http-e)-1.0"):
        (site / f"{hostile}.dist-info").mkdir()
    packet = _packet_for(tmp_path, {"src/m.py": "x = 1\n"})
    installed = [fact for fact in packet.dependencies
                 if fact.status == "installed"]
    assert len(installed) == 1
    detail = installed[0].detail
    for hostile in ("<script>", "back`tick", "with space", "run-me",
                    "http", "9.9.9"):
        assert hostile not in detail
    assert "pytest 8.3.4" in detail
    assert len(detail) <= 512


def test_build_packets_bounds_large_distribution_scans(tmp_path):
    from ptest import agent_assessment as AA

    site = _make_venv(tmp_path, dists=())
    for index in range(600):
        (site / f"pkg{index:03}-1.0.{index}.dist-info").mkdir()
    packet = _packet_for(tmp_path, {"src/m.py": "x = 1\n"})
    installed = [fact for fact in packet.dependencies
                 if fact.status == "installed"]
    assert len(installed) == 1
    assert "512+" in installed[0].detail
    assert len(installed[0].detail) <= 512


def test_build_packets_never_executes_fake_site_packages(tmp_path,
                                                         monkeypatch):
    """Planted ``sitecustomize``/``.pth`` payloads stay inert and no
    subprocess is spawned during environment inspection."""
    import subprocess

    from ptest import agent_assessment as AA

    site = _make_venv(tmp_path, dists=("pytest-8.3.4",))
    sentinel = tmp_path / "PWNED_BY_SITECUSTOMIZE"
    (site / "sitecustomize.py").write_text(
        f"import pathlib; pathlib.Path({str(sentinel)!r}).write_text('x')\n",
        encoding="utf-8")
    (site / "evil.pth").write_text("import os; os.system('true')\n",
                                   encoding="utf-8")

    def _no_spawn(*_args, **_kwargs):
        raise AssertionError("environment inspection must not spawn")

    monkeypatch.setattr(subprocess, "Popen", _no_spawn)
    packet = _packet_for(tmp_path, {"src/m.py": "x = 1\n"})
    assert not sentinel.exists()
    assert any(fact.status == "installed"
               for fact in packet.dependencies)


def test_build_packets_pyvenv_home_paths_never_enter_packet(tmp_path):
    from ptest import agent_assessment as AA

    _make_venv(tmp_path)
    secret_home = f"/secret/home-{tmp_path.name}"
    (tmp_path / ".venv" / "pyvenv.cfg").write_text(
        f"home = {secret_home}\ninclude-system-site-packages = false\n"
        "version = 3.11.9\n",
        encoding="utf-8")
    packet = _packet_for(tmp_path, {"src/m.py": "x = 1\n"})
    for fact in packet.dependencies:
        assert secret_home not in fact.detail
    reviews = AA.plan_item_reviews(packet)
    assert reviews
    assert all(secret_home.encode("utf-8") not in review.request
               for review in reviews if review.request is not None)


def test_build_packets_reports_installed_node_test_tools(tmp_path):
    import json as json_lib

    from ptest import agent_assessment as AA

    node_modules = tmp_path / "node_modules"
    (node_modules / "vitest").mkdir(parents=True)
    (node_modules / "vitest" / "package.json").write_text(
        json_lib.dumps({"name": "vitest", "version": "2.1.3"}),
        encoding="utf-8")
    (node_modules / "lodash").mkdir()
    (node_modules / "lodash" / "package.json").write_text(
        json_lib.dumps({"name": "lodash", "version": "4.17.21"}),
        encoding="utf-8")
    packet = _packet_for(tmp_path, {
        "src/m.py": "x = 1\n",
        "package.json": json_lib.dumps({
            "name": "demo",
            "devDependencies": {"vitest": "^2.0.0"},
            "dependencies": {"lodash": "^4.0.0"},
        }),
    })
    installed = [fact for fact in packet.dependencies
                 if fact.status == "installed"]
    assert len(installed) == 1
    assert "vitest 2.1.3" in installed[0].detail
    assert "lodash" not in installed[0].detail
    assert len(installed[0].detail) <= 512


def test_build_packets_node_symlink_policy(tmp_path):
    """A pnpm-style symlink staying inside node_modules is followed; an
    escaping one is ignored."""
    import json as json_lib

    from ptest import agent_assessment as AA

    node_modules = tmp_path / "node_modules"
    hidden = node_modules / ".pnpm" / "mocha@9.0.0" / "node_modules" / "mocha"
    hidden.mkdir(parents=True)
    (hidden / "package.json").write_text(
        json_lib.dumps({"name": "mocha", "version": "9.0.0"}),
        encoding="utf-8")
    os.symlink(str(hidden), node_modules / "mocha")
    outside = tmp_path / "outside-store"
    outside.mkdir()
    (outside / "package.json").write_text(
        json_lib.dumps({"name": "jest", "version": "1.2.3-evil"}),
        encoding="utf-8")
    os.symlink(str(outside), node_modules / "jest")
    packet = _packet_for(tmp_path, {
        "src/m.py": "x = 1\n",
        "package.json": json_lib.dumps({
            "name": "demo",
            "devDependencies": {"mocha": "^9.0.0", "jest": "^29.0.0"},
        }),
    })
    installed = [fact for fact in packet.dependencies
                 if fact.status == "installed"]
    assert len(installed) == 1
    assert "mocha 9.0.0" in installed[0].detail
    assert "1.2.3" not in installed[0].detail


def test_build_packets_ignores_oversized_or_invalid_node_manifests(
        tmp_path):
    import json as json_lib

    from ptest import agent_assessment as AA

    node_modules = tmp_path / "node_modules" / "vitest"
    node_modules.mkdir(parents=True)
    (node_modules / "package.json").write_text(
        json_lib.dumps({"name": "vitest", "version": "2.1.3"}),
        encoding="utf-8")
    big = {"name": "demo", "devDependencies": {"vitest": "^2.0.0"},
           "padding": "x" * (64 * 1024 + 1024)}
    packet = _packet_for(tmp_path, {
        "src/m.py": "x = 1\n",
        "package.json": json_lib.dumps(big),
    })
    assert not any(fact.status == "installed"
                   for fact in packet.dependencies)
    packet = _packet_for(tmp_path, {
        "src/m.py": "x = 1\n",
        "package.json": "{not valid json",
    })
    assert not any(fact.status == "installed"
                   for fact in packet.dependencies)


def test_build_packets_ignores_oversized_installed_node_manifest(tmp_path):
    import json as json_lib

    from ptest import agent_assessment as AA

    node_modules = tmp_path / "node_modules" / "vitest"
    node_modules.mkdir(parents=True)
    (node_modules / "package.json").write_text(
        json_lib.dumps({"name": "vitest", "version": "2.1.3"})
        + " " * (64 * 1024 + 1024),
        encoding="utf-8")
    packet = _packet_for(tmp_path, {
        "src/m.py": "x = 1\n",
        "package.json": json_lib.dumps({
            "name": "demo",
            "devDependencies": {"vitest": "^2.0.0"},
        }),
    })
    assert not any(fact.status == "installed"
                   for fact in packet.dependencies)


def test_installed_facts_create_no_public_limitation_code(tmp_path):
    """The additive ``installed`` status is packet-internal: the CLI
    limitation mapping ignores it, so no public code is created."""
    from ptest import agent_assessment as AA
    from ptest.cli import _assessment_limitations

    _make_venv(tmp_path)
    packet = _packet_for(tmp_path, {"src/m.py": "x = 1\n"})
    assert any(fact.status == "installed"
               for fact in packet.dependencies)
    codes = {item["code"] for item in _assessment_limitations((packet,))}
    assert "dependency-installed" not in codes
    assert not any("installed" in code for code in codes)


def test_dependency_env_scan_respects_review_deadline(tmp_path):
    from ptest import agent_assessment as AA

    _make_venv(tmp_path)
    with pytest.raises(C.Problem) as caught:
        AA._dependency_facts({"src/m.py"}, "", None, child_root=tmp_path,
                             deadline=0.0)
    assert caught.value.code == "review-timeout"


def test_build_packets_venv_without_pyvenv_cfg_stays_uninspectable(tmp_path):
    """A bare ``venv/`` dir proves nothing: no version, no listing, so no
    ``installed`` fact and the honest ``uninspectable`` one stays."""
    (tmp_path / "venv").mkdir()
    packet = _packet_for(tmp_path, {"src/m.py": "x = 1\n"})
    assert not any(fact.status == "installed"
                   for fact in packet.dependencies)
    env = [fact for fact in packet.dependencies
           if fact.ecosystem == "environment"]
    assert len(env) == 1 and env[0].status == "uninspectable"


def test_build_packets_pypy_layout_without_cpython_site_packages_stays_uninspectable(  # noqa: E501
        tmp_path):
    """pyvenv.cfg alone is not provenance: without a listed
    ``python3.*/site-packages`` there is no ``installed`` fact, even when
    another layout (here PyPy) holds distributions."""
    root = tmp_path / ".venv"
    site = root / "lib" / "pypy3.10" / "site-packages"
    site.mkdir(parents=True)
    (root / "pyvenv.cfg").write_text("home = /usr/bin\nversion = 3.10.12\n",
                                     encoding="utf-8")
    (site / "pytest-8.0.dist-info").mkdir()
    packet = _packet_for(tmp_path, {"src/m.py": "x = 1\n"})
    assert not any(fact.status == "installed"
                   for fact in packet.dependencies)
    env = [fact for fact in packet.dependencies
           if fact.ecosystem == "environment"]
    assert len(env) == 1 and env[0].status == "uninspectable"


def test_build_packets_mixed_declared_ecosystems_keep_uninspectable(tmp_path):
    """One ``installed`` fact must not silence the signal for the other
    declared ecosystems (node/rust here); wording stays neutral."""
    import json as json_lib

    _make_venv(tmp_path)
    (tmp_path / "node_modules").mkdir()
    packet = _packet_for(tmp_path, {
        "src/m.py": "x = 1\n",
        "pyproject.toml": "[project]\nname = 'demo'\n",
        "uv.lock": "version = 1\n",
        "package.json": json_lib.dumps({
            "name": "demo",
            "devDependencies": {"vitest": "^2.0.0"},
        }),
        "Cargo.toml": '[package]\nname = "demo"\n',
    })
    installed = [fact for fact in packet.dependencies
                 if fact.status == "installed"]
    assert [fact.ecosystem for fact in installed] == ["python"]
    env = [fact for fact in packet.dependencies
           if fact.ecosystem == "environment"]
    assert len(env) == 1 and env[0].status == "uninspectable"
    assert ("Environments other than reported project-local metadata are "
            "not inspected." in env[0].detail)
    assert "No project-local environment" not in env[0].detail


def test_dependency_env_scan_counts_interpreter_entries_against_bound(
        tmp_path, monkeypatch):
    """Interpreter entries count against the scan bound: the lib/ loop is
    checkpointed lazily, so a small bound stops lower-bound early."""
    from ptest import agent_assessment as AA

    lib = tmp_path / ".venv" / "lib"
    lib.mkdir(parents=True)
    for extra in ("pypy3.10", "junk-a", "junk-b", "junk-c", "junk-d",
                  "junk-e"):
        (lib / extra).mkdir()
    monkeypatch.setattr(AA, "_MAX_ENV_SCAN_ENTRIES", 5)
    count, lower_bound, _tools, listed = AA._scan_dist_info(
        tmp_path, ".venv", deadline=None, progress=None)
    assert lower_bound is True
    assert listed is False
    assert count == 0


def test_dependency_env_scan_checkpoint_trips_mid_site_packages_loop(
        tmp_path):
    """A progress trip fires from inside the site-packages loop, not just
    before the scan starts."""
    from ptest import agent_assessment as AA

    _make_venv(tmp_path, dists=tuple(f"pkg{i:02}-1.0" for i in range(10)))
    calls = []

    def _progress():
        calls.append(1)
        if len(calls) > 4:
            raise C.Problem(code="review-timeout",
                            message="total review deadline expired",
                            phase="evidence", retryable=False)

    with pytest.raises(C.Problem) as caught:
        AA._dependency_facts({"src/m.py"}, "", None, child_root=tmp_path,
                             deadline=None, progress=_progress)
    assert caught.value.code == "review-timeout"
    assert len(calls) > 4


# --- T4: tiered admission priority and artifact exclusion ---------------------

def test_build_packets_admits_manifests_then_test_config_then_tests(tmp_path):
    from ptest import agent_assessment as AA
    from ptest import config as config_api

    root = _monorepo_root(tmp_path, {
        "api": _v1_config_text(CHILD_PID, "pytest"),
    })
    api = root / "api"
    (api / ".superpowers" / "sdd").mkdir(parents=True)
    (api / ".superpowers" / "sdd" / "x.diff").write_text(
        "drop_database(\n", encoding="utf-8")
    (api / ".pipeline").mkdir()
    (api / ".pipeline" / "review.md").write_text(
        "drop_database(\n", encoding="utf-8")
    (api / "recommendations.md").write_text(
        "drop_database(\n", encoding="utf-8")
    (api / "tests").mkdir()
    for index in range(80):
        (api / "tests" / f"test_n{index:02d}.py").write_text(
            f"def test_n{index:02d}():\n    assert True\n", encoding="utf-8")
    conftest_lines = [f"# conftest line {number}"
                      for number in range(1, 501)]
    conftest_lines[446] = 'TEST_DB_NAME = "worker-owned-db"'
    (api / "tests" / "conftest.py").write_text(
        "\n".join(conftest_lines) + "\n", encoding="utf-8")
    (api / "pyproject.toml").write_text(
        "[project]\nname = 'demo'\n", encoding="utf-8")
    (api / "uv.lock").write_text("version = 1\n", encoding="utf-8")

    resolution = config_api.resolve_config(root)
    workspace = doctor.inspect_workspace(
        _domain(root), resolution, C.DEFAULT_SCAN_LIMITS, None)
    packets = AA.build_packets(workspace, resolution)
    assert len(packets) == 1
    packet = packets[0]
    paths = [excerpt.path for excerpt in packet.excerpts]

    assert "api/.superpowers/sdd/x.diff" not in paths
    assert "api/.pipeline/review.md" not in paths
    assert "api/recommendations.md" not in paths
    assert not any(path.endswith((".diff", ".patch")) for path in paths)
    conftest = next(e for e in packet.excerpts
                    if e.path == "api/tests/conftest.py")
    assert (conftest.start_line, conftest.end_line) == (1, 500)
    assert paths[:3] == ["api/.ptest.toml", "api/pyproject.toml",
                         "api/tests/conftest.py"]

    reviews = AA.plan_item_reviews(packet)
    assert [review.item_id for review in reviews] == list(EXPECTED_IDS)
    db_isolation = next(r for r in reviews if r.item_id == "DB-002")
    assert "api/tests/conftest.py" in db_isolation.excerpt_paths


# --- T4: per-item review planning --------------------------------------------

def _pure_library_packet(tmp_path, extra=None):
    files = {
        "pyproject.toml": "[project]\nname = 'demo'\n",
        "tests/test_pure.py": "def test_pure():\n    assert True\n",
        "src/demo.py": "def add(a, b):\n    return a + b\n",
    }
    files.update(extra or {})
    return _packet_for(tmp_path, files)


def test_plan_item_reviews_returns_twelve_bounded_routed_reviews(tmp_path):
    import json

    from ptest import agent_assessment as AA
    from ptest import agent_providers as providers

    packet = _pure_library_packet(tmp_path)
    reviews = AA.plan_item_reviews(packet)
    assert len(reviews) == len(EXPECTED_IDS) == 12
    assert [review.item_id for review in reviews] == list(EXPECTED_IDS)
    for review in reviews:
        assert review.label.strip()
        assert isinstance(review.schema, bytes) and review.schema
        assert (review.request is None) == (review.skip_reason is not None)
        assert len(review.excerpt_paths) <= AA.ITEM_MAX_FILES
        if review.request is not None:
            assert len(review.request) <= (
                providers.PROMPT_INPUT_MAX_BYTES - len(review.schema))
            body = json.loads(review.request.decode("utf-8"))
            assert body["policy"]["item"]["id"] == review.item_id
            assert len(body["excerpts"]) <= AA.ITEM_MAX_FILES
            assert sum(len(item["text"].encode("utf-8"))
                       for item in body["excerpts"]) <= AA.ITEM_MAX_BYTES
            assert [item["path"] for item in body["excerpts"]] == list(
                review.excerpt_paths)


def test_plan_item_reviews_never_skip_on_absent_evidence(tmp_path):
    """Absence in admitted evidence never proves inapplicability.

    Milestone 1 removed the absence-based DB/CACHE skips: every item
    without a deterministic answer gets a model review, and unknown
    (not N/A) is the honest answer when the evidence cannot decide.
    With no admitted ``.ptest.toml`` no source is invented: the item
    carries a missing-context reason in its private request metadata
    instead of a fabricated citation.
    """
    import json

    from ptest import agent_assessment as AA

    packet = _pure_library_packet(tmp_path)
    assert ".ptest.toml" not in [e.path for e in packet.excerpts]
    reviews = {review.item_id: review
               for review in AA.plan_item_reviews(packet)}
    for item_id in ("DB-001", "DB-002", "CACHE-001"):
        review = reviews[item_id]
        assert review.request is not None
        assert review.skip_reason is None
        assert ".ptest.toml" not in review.excerpt_paths
        body = json.loads(review.request.decode("utf-8"))
        offered = [e["path"] for e in body["excerpts"]]
        assert ".ptest.toml" not in offered
        assert body["packet"]["missing"]


def test_plan_item_reviews_absent_library_reviews_carry_config(tmp_path):
    """Former skip rows are now ordinary reviews with runner config."""
    from ptest import agent_assessment as AA

    packet = _packet_for(tmp_path, {
        "pyproject.toml": "[project]\nname = 'demo'\n",
        "uv.lock": "version = 1\n",
        "tests/test_pure.py": "def test_pure():\n    assert True\n",
    })
    assert not any(e.path.endswith("uv.lock") for e in packet.excerpts)
    reviews = AA.plan_item_reviews(packet)
    child = AA.assemble_child(
        packet, reviews,
        tuple(None if review.request is None else _satisfied_reply(packet, review)
              for review in reviews))
    for row in child.rows:
        if row.id in ("DB-001", "DB-002", "CACHE-001"):
            assert row.status == "satisfied"


def _db_packet(tmp_path, manifest_text, code_text):
    return _packet_for(tmp_path, {
        "pyproject.toml": manifest_text,
        "tests/test_db.py": code_text,
    })


def test_plan_item_reviews_skip_absent_when_db_library_declared(tmp_path):
    from ptest import agent_assessment as AA

    packet = _db_packet(tmp_path, "[project]\nname = 'demo'\ndependencies = ['sqlalchemy']\n",
                        "def test_x():\n    assert True\n")
    reviews = {r.item_id: r for r in AA.plan_item_reviews(packet)}
    assert reviews["DB-001"].request is not None
    assert reviews["DB-002"].request is not None


def test_plan_item_reviews_skip_absent_when_db_usage_appears(tmp_path):
    from ptest import agent_assessment as AA

    packet = _db_packet(tmp_path, "[project]\nname = 'demo'\n",
                        "import sqlite3\ndef test_x():\n    sqlite3.connect('f.db')\n")
    reviews = {r.item_id: r for r in AA.plan_item_reviews(packet)}
    assert reviews["DB-001"].request is not None
    assert reviews["DB-002"].request is not None


def test_plan_item_reviews_skip_absent_when_scanner_hit_exists(tmp_path):
    from ptest import agent_assessment as AA

    packet = _db_packet(tmp_path, "[project]\nname = 'demo'\n",
                        "def teardown():\n    drop_database()\n")
    reviews = {r.item_id: r for r in AA.plan_item_reviews(packet)}
    assert reviews["DB-002"].request is not None


def test_plan_item_reviews_skip_absent_when_no_manifest_admitted(tmp_path):
    from ptest import agent_assessment as AA

    packet = _packet_for(tmp_path, {
        "tests/test_pure.py": "def test_pure():\n    assert True\n",
    })
    reviews = {r.item_id: r for r in AA.plan_item_reviews(packet)}
    assert reviews["DB-001"].request is not None
    assert reviews["CACHE-001"].request is not None


def test_plan_item_reviews_skip_absent_when_cache_library_declared(tmp_path):
    from ptest import agent_assessment as AA

    packet = _packet_for(tmp_path, {
        "pyproject.toml": "[project]\nname = 'demo'\ndependencies = ['redis']\n",
        "tests/test_pure.py": "def test_pure():\n    assert True\n",
    })
    reviews = {r.item_id: r for r in AA.plan_item_reviews(packet)}
    assert reviews["CACHE-001"].request is not None


def test_route_excerpts_ranks_scanner_hit_above_generic_files(tmp_path):
    """A scanner-hit file sorted after 24 generic files still routes."""
    from ptest import agent_assessment as AA

    files = {
        "pyproject.toml":
            "[project]\nname = 'demo'\ndependencies = ['sqlalchemy']\n",
    }
    for index in range(40):
        files[f"tests/test_a{index:02d}.py"] = (
            f"def test_a{index:02d}():\n    assert True\n")
    files["tests/test_zz_db.py"] = "def teardown():\n    drop_database(url)\n"
    packet = _packet_for(tmp_path, files)
    assert len(packet.excerpts) == 42
    reviews = {review.item_id: review
               for review in AA.plan_item_reviews(packet)}
    db_isolation = reviews["DB-002"]
    assert db_isolation.request is not None
    assert len(db_isolation.excerpt_paths) <= AA.ITEM_MAX_FILES
    assert "tests/test_zz_db.py" in db_isolation.excerpt_paths
    assert all(path in [e.path for e in packet.excerpts]
               for path in db_isolation.excerpt_paths)


@pytest.mark.parametrize(("manifest", "dependency", "items"), [
    ("pyproject.toml", "pymssql", ("DB-001", "DB-002")),
    ("pyproject.toml", "duckdb", ("DB-001", "DB-002")),
    ("package.json", '"pg": "^8.0.0"', ("DB-001", "DB-002")),
    ("package.json", '"mysql2": "^3.0.0"', ("DB-001", "DB-002")),
    ("package.json", '"mongodb": "^6.0.0"', ("DB-001", "DB-002")),
    ("package.json", '"drizzle-orm": "^0.30.0"', ("DB-001", "DB-002")),
    ("package.json", '"kysely": "^0.27.0"', ("DB-001", "DB-002")),
    ("package.json", '"@supabase/supabase-js": "^2.0.0"',
     ("DB-001", "DB-002")),
    ("go.mod", "gorm.io/gorm", ("DB-001", "DB-002")),
    ("go.mod", "github.com/jackc/pgx/v5", ("DB-001", "DB-002")),
    ("go.mod", "github.com/lib/pq", ("DB-001", "DB-002")),
    ("go.mod", "github.com/jmoiron/sqlx", ("DB-001", "DB-002")),
    ("Cargo.toml", 'diesel = "2"', ("DB-001", "DB-002")),
    ("Cargo.toml", 'rusqlite = "0.31"', ("DB-001", "DB-002")),
    ("Cargo.toml", 'sqlx = "0.7"', ("DB-001", "DB-002")),
    ("package.json", '"keyv": "^4.0.0"', ("CACHE-001",)),
    ("package.json", '"lru-cache": "^10.0.0"', ("CACHE-001",)),
    ("package.json", '"node-cache": "^5.0.0"', ("CACHE-001",)),
    ("package.json", '"diskcache": "^5.0.0"', ("CACHE-001",)),
])
def test_plan_item_reviews_skip_absent_for_declared_driver_per_ecosystem(
        tmp_path, manifest, dependency, items):
    """Declaring any known DB/cache driver per ecosystem forces a review."""
    from ptest import agent_assessment as AA

    if manifest == "package.json":
        manifest_text = ('{"name": "demo", "dependencies": {'
                         + dependency + "}}\n")
    elif manifest == "go.mod":
        manifest_text = "module demo\n\ngo 1.21\n\nrequire " + dependency + " v0.0.0\n"
    elif manifest == "Cargo.toml":
        manifest_text = ('[package]\nname = "demo"\nversion = "0.1.0"\n'
                         "[dependencies]\n" + dependency + "\n")
    else:
        manifest_text = ("[project]\nname = 'demo'\ndependencies = ['"
                         + dependency + "']\n")
    packet = _packet_for(tmp_path, {
        manifest: manifest_text,
        "tests/test_pure.py": "def test_pure():\n    assert True\n",
    })
    reviews = {review.item_id: review
               for review in AA.plan_item_reviews(packet)}
    for item_id in items:
        assert reviews[item_id].request is not None


@pytest.mark.parametrize("token", [
    "pg", "postgres", "mysql2", "mongodb", "mssql", "pymssql", "duckdb",
    "drizzle-orm", "kysely", "@supabase/supabase-js", "gorm", "pgx",
    "lib/pq", "sqlx", "diesel", "rusqlite", "jdbc", "hibernate", "jpa",
    "jooq", "mybatis",
])
def test_db_library_regex_covers_listed_drivers(token):
    """Every probed driver token (incl. JVM) matches the DB library regex."""
    from ptest import agent_assessment as AA

    assert AA._DB_LIBRARY_RE.search(token)


@pytest.mark.parametrize("token", [
    "keyv", "lru-cache", "node-cache", "diskcache",
])
def test_cache_library_regex_covers_listed_caches(token):
    """Every probed cache token matches the cache library regex."""
    from ptest import agent_assessment as AA

    assert AA._CACHE_LIBRARY_RE.search(token)


@pytest.mark.parametrize("usage", [
    "url = 'mongodb://localhost:27017'\n",
    "url = 'mssql://user@host/db'\n",
    "import duckdb\nduckdb.sql('select 1')\n",
])
def test_plan_item_reviews_skip_absent_for_db_usage_variants(
        tmp_path, usage):
    """URL schemes and driver call styles in evidence force a DB review."""
    from ptest import agent_assessment as AA

    packet = _packet_for(tmp_path, {
        "pyproject.toml": "[project]\nname = 'demo'\n",
        "tests/test_db.py": "def test_q():\n    " + usage.replace("\n", "\n    "),
    })
    reviews = {review.item_id: review
               for review in AA.plan_item_reviews(packet)}
    assert reviews["DB-001"].request is not None
    assert reviews["DB-002"].request is not None


def test_plan_item_reviews_skip_absent_for_declared_duckdb_end_to_end(
        tmp_path):
    """Finding repro: pymssql+duckdb declared and used must not skip."""
    from ptest import agent_assessment as AA

    packet = _packet_for(tmp_path, {
        "pyproject.toml": ("[project]\nname = 'demo'\n"
                           "dependencies = ['pymssql', 'duckdb']\n"),
        "tests/test_duck.py": ("import duckdb\ndef test_q():\n"
                               "    duckdb.sql('select 1')\n"
                               "    assert True\n"),
    })
    reviews = {review.item_id: review
               for review in AA.plan_item_reviews(packet)}
    assert reviews["DB-001"].request is not None
    assert reviews["DB-002"].request is not None


def test_plan_item_reviews_is_pure_without_filesystem(tmp_path, monkeypatch):
    from ptest import agent_assessment as AA

    packet = _pure_library_packet(tmp_path)

    def _boom(*args, **kwargs):
        raise AssertionError("plan touched the filesystem")

    monkeypatch.setattr("os.scandir", _boom)
    monkeypatch.setattr("os.lstat", _boom)
    monkeypatch.setattr("os.stat", _boom)
    reviews = AA.plan_item_reviews(packet)
    assert len(reviews) == 12


# --- T4: one-row replies and child assembly -----------------------------------

def _packet_in_root(root, files):
    """Build one standalone packet over an explicit project root."""
    from ptest import agent_assessment as AA

    for rel, text in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    workspace, resolution = _workspace(root)
    packets = AA.build_packets(workspace, resolution, AA.EvidenceLimits())
    assert len(packets) == 1
    return packets[0]


def _subset_citation(packet, path, start=1, end=None):
    from ptest import agent_assessment as AA

    # Every cited path must be an actually admitted packet excerpt;
    # invented sources are never citable, so no fallback is allowed.
    excerpt = next(e for e in packet.excerpts if e.path == path)
    assert isinstance(excerpt, AA.SourceExcerpt)
    return {"path": path, "start_line": start,
            "end_line": end if end is not None else excerpt.end_line,
            "sha256": excerpt.sha256}


def _one_row_bytes(packet, review, *, status, paths, rationale=None,
                   finding="null"):
    if rationale is None:
        rationale = (f"Row {review.item_id} judged {status} against "
                     "the cited excerpt lines.")
    evidence = [_subset_citation(packet, item) if isinstance(item, str)
                else _subset_citation(packet, *item) for item in paths]
    if finding == "null":
        finding_value = None
    else:
        summary, change, fpaths = finding
        finding_value = {
            "summary": summary, "suggested_change": change,
            "evidence": [_subset_citation(packet, item) for item in fpaths]}
    citation_ids = [
        (cite["path"], cite["start_line"], cite["end_line"], cite["sha256"])
        for cite in evidence]
    if status == "gap" and finding_value is not None:
        finding_ids = {
            (cite["path"], cite["start_line"], cite["end_line"], cite["sha256"])
            for cite in finding_value["evidence"]}
        proof_index = next((index for index, identity in enumerate(citation_ids)
                            if identity in finding_ids), 0)
        roles = ("applicability", "violation")
    elif status == "satisfied":
        proof_index = 0
        roles = ("applicability", "mechanism")
    elif status == "not-applicable":
        proof_index = 0
        roles = ("applicability",)
    else:
        proof_index = 0
        roles = ()
    proof = []
    if evidence and roles:
        cite = evidence[proof_index]
        excerpt = next(item for item in packet.excerpts
                       if item.path == cite["path"])
        lines = excerpt.text.splitlines() or [""]
        line = lines[cite["start_line"] - excerpt.start_line]
        quote = line[:512]
        proof = [{"role": role, "citation_index": proof_index,
                  "quote": quote} for role in roles]
    return __import__("json").dumps(
        {"status": status, "rationale": rationale, "evidence": evidence,
         "finding": finding_value, "proof": proof, "needs": []}).encode("utf-8")


def _satisfied_reply(packet, review):
    assert review.excerpt_paths, review.item_id
    return _one_row_bytes(packet, review, status="satisfied",
                          paths=[review.excerpt_paths[0]])


def _assembled_all_ok(packet):
    from ptest import agent_assessment as AA

    reviews = AA.plan_item_reviews(packet)
    replies = tuple(
        None if review.request is None else _satisfied_reply(packet, review)
        for review in reviews)
    return packet, reviews, AA.assemble_child(packet, reviews, replies)


def test_assemble_child_valid_replies_carry_labels_findings_and_score(tmp_path):
    from ptest import agent_assessment as AA
    from ptest.checklist import CATALOG

    labels = {entry.id: entry.label for entry in CATALOG}
    recipes = {entry.id: entry.recipe for entry in CATALOG}
    packet = _pure_library_packet(
        tmp_path, {"pyproject.toml": "[project]\nname = 'demo'\ndependencies = ['sqlalchemy', 'redis']\n",
                   "tests/test_db.py": "import sqlalchemy\ndef test_x():\n    assert True\n"})
    reviews = AA.plan_item_reviews(packet)
    assert all(review.request is not None for review in reviews)
    replies = []
    for review in reviews:
        if review.item_id == "DB-002":
            replies.append(_one_row_bytes(
                packet, review, status="gap",
                paths=[review.excerpt_paths[0]],
                rationale="The teardown removes records without naming an owner.",
                finding=("Unowned teardown removes shared records.",
                         "Record the owner before cleanup and remove only that namespace.",
                         [review.excerpt_paths[0]])))
        else:
            replies.append(_satisfied_reply(packet, review))
    child = AA.assemble_child(packet, reviews, tuple(replies))
    assert [row.id for row in child.rows] == list(EXPECTED_IDS)
    assert all(row.label == labels[row.id] for row in child.rows)
    assert [finding.id for finding in child.findings] == ["DB-002"]
    assert child.findings[0].recipe_id == recipes["DB-002"]
    assert child.score is not None
    assert (child.score.satisfied, child.score.applicable) == (11, 12)
    assert child.score.percent == (100 * 11) // 12


def test_assemble_child_noop_fails_without_valid_assertion(tmp_path):
    """A no-op assembler returning all-unknown must fail this test's sightline.

    Guards against vacuous assembly coverage: replacing the implementation
    with ``unknown`` rows changes the asserted statuses. Absence-based
    N/A skips are gone (milestone 1), so all twelve rows are satisfied.
    """
    packet, _, child = _assembled_all_ok(_pure_library_packet(tmp_path))
    assert len(child.rows) == 12
    assert all(row.status == "satisfied" for row in child.rows)


def _invalid_reply_cases(packet, review):
    import json

    good_path = review.excerpt_paths[0]
    good_cite = _subset_citation(packet, good_path)
    bad_cite = {"path": "elsewhere/missing.py", "start_line": 1,
                "end_line": 1, "sha256": "0" * 64}
    cases = {
        "not-json": b"{nope",
        "extra-key": json.dumps({"status": "satisfied", "rationale": "Rationale with enough substance here.",
                                 "evidence": [good_cite], "finding": None,
                                 "score": 1}).encode(),
        "missing-key": json.dumps({"status": "satisfied",
                                   "rationale": "Rationale with enough substance here.",
                                   "evidence": []}).encode(),
        "outside-subset": json.dumps({"status": "satisfied", "rationale": "Cites a file outside the routed subset.",
                                      "evidence": [bad_cite], "finding": None}).encode(),
        "gap-without-finding": json.dumps({"status": "gap", "rationale": "A gap with no finding attached.",
                                           "evidence": [good_cite], "finding": None}).encode(),
        "satisfied-with-finding": json.dumps({"status": "satisfied", "rationale": "Satisfied yet carries a finding.",
                                              "evidence": [good_cite],
                                              "finding": {"summary": "Extra summary.",
                                                          "suggested_change": "Extra change.",
                                                          "evidence": [good_cite]}}).encode(),
        "injected-failed-prefix": json.dumps({"status": "satisfied",
                                              "rationale": "Review failed: timed out here.",
                                              "evidence": [good_cite], "finding": None}).encode(),
        "untrusted-prose": json.dumps({"status": "satisfied",
                                       "rationale": "See https://invalid.test for detail.",
                                       "evidence": [good_cite], "finding": None}).encode(),
        "na-too-brief": json.dumps({"status": "not-applicable", "rationale": "N/A.",
                                    "evidence": [good_cite], "finding": None}).encode(),
        "satisfied-no-citation": json.dumps({"status": "satisfied", "rationale": "No citation given at all.",
                                             "evidence": [], "finding": None}).encode(),
        "unhashable-status-list": json.dumps({"status": ["satisfied"],
                                            "rationale": "Status arrives as a list.",
                                            "evidence": [good_cite], "finding": None}).encode(),
        "unhashable-status-dict": json.dumps({"status": {"name": "satisfied"},
                                            "rationale": "Status arrives as a dict.",
                                            "evidence": [good_cite], "finding": None}).encode(),
        "non-string-status-int": json.dumps({"status": 0,
                                           "rationale": "Status arrives as a number.",
                                           "evidence": [good_cite], "finding": None}).encode(),
        "null-status": json.dumps({"status": None,
                                 "rationale": "Status arrives as null.",
                                 "evidence": [good_cite], "finding": None}).encode(),
        "gap-with-list-finding": json.dumps({"status": "gap", "rationale": "A gap carrying a list finding.",
                                           "evidence": [good_cite], "finding": ["not", "a", "dict"]}).encode(),
        "gap-with-string-finding": json.dumps({"status": "gap", "rationale": "A gap carrying a string finding.",
                                             "evidence": [good_cite], "finding": "fix it"}).encode(),
    }
    # Keep these cases focused on their original malformed shape, prose, or
    # citation. Private fields are present but deliberately empty.
    for name, payload in tuple(cases.items()):
        if name == "not-json":
            continue
        document = json.loads(payload)
        document.setdefault("proof", [])
        document.setdefault("needs", [])
        cases[name] = json.dumps(document).encode()
    return cases


def test_assemble_child_invalid_replies_become_unknown_only(tmp_path):
    from ptest import agent_assessment as AA

    packet = _pure_library_packet(
        tmp_path, {"pyproject.toml": "[project]\nname = 'demo'\ndependencies = ['sqlalchemy', 'redis']\n",
                   "tests/test_db.py": "import sqlalchemy\ndef test_x():\n    assert True\n"})
    reviews = AA.plan_item_reviews(packet)
    target = next(r for r in reviews if r.item_id == "FIX-001")
    details = {
        "not-json": "not JSON",
        "extra-key": "carries an unknown field",
        "missing-key": "missing 'finding'",
        "outside-subset": "no valid citations",
        "gap-without-finding": "gap reply needs a finding",
        "satisfied-with-finding": "non-gap reply must carry finding null",
        "injected-failed-prefix": "rationale carries untrusted model content",
        "untrusted-prose": "rationale carries untrusted model content",
        "na-too-brief": "needs a specific not-applicable rationale",
        "satisfied-no-citation": "no valid citations",
        "unhashable-status-list": "has an unknown status",
        "unhashable-status-dict": "has an unknown status",
        "non-string-status-int": "has an unknown status",
        "null-status": "has an unknown status",
        "gap-with-list-finding": "gap reply needs a finding",
        "gap-with-string-finding": "gap reply needs a finding",
    }
    cases = _invalid_reply_cases(packet, target)
    assert set(cases) == set(details), "twin map must cover every case"
    for name, payload in cases.items():
        replies = tuple(
            payload if review.item_id == "FIX-001"
            else _satisfied_reply(packet, review) for review in reviews)
        child = AA.assemble_child(packet, reviews, replies)
        row = next(r for r in child.rows if r.id == "FIX-001")
        assert (name, row.status, row.rationale) == (
            name, "unknown",
            AA.FAILED_PREFIX + "invalid reply: " + details[name])
        assert [r.status for r in child.rows if r.id != "FIX-001"] == [
            "satisfied"] * 11


def test_assemble_child_str_replies_become_named_failures(tmp_path):
    from ptest import agent_assessment as AA

    packet = _pure_library_packet(tmp_path)
    reviews = AA.plan_item_reviews(packet)
    target = next(r for r in reviews if r.request is not None)
    replies = tuple(
        "timed out" if review.item_id == target.item_id
        else (None if review.request is None
              else _satisfied_reply(packet, review))
        for review in reviews)
    child = AA.assemble_child(packet, reviews, replies)
    row = next(r for r in child.rows if r.id == target.item_id)
    assert row.status == "unknown"
    assert row.rationale == AA.FAILED_PREFIX + "timed out"


def test_assemble_child_rejects_misaligned_and_stale_inputs(tmp_path):
    from ptest import agent_assessment as AA

    packet = _pure_library_packet(tmp_path)
    reviews = AA.plan_item_reviews(packet)
    good = tuple(
        None if review.request is None else _satisfied_reply(packet, review)
        for review in reviews)
    with __import__("pytest").raises(ValueError):
        AA.assemble_child(packet, reviews, good[:-1])
    bad_type = list(good)
    for index, review in enumerate(reviews):
        if review.request is not None:
            bad_type[index] = 123
            break
    with __import__("pytest").raises(TypeError):
        AA.assemble_child(packet, reviews, tuple(bad_type))
    other_root = tmp_path / "other"
    other_root.mkdir()
    other = _packet_in_root(other_root, {
        "pyproject.toml": "[project]\nname = 'other'\n",
        "tests/test_other.py": "def test_other():\n    assert True\n",
    })
    with __import__("pytest").raises(C.Problem) as excinfo:
        AA.assemble_child(other, reviews, good)
    assert excinfo.value.code == "stale-evidence"
    with __import__("pytest").raises(TypeError):
        AA.assemble_child(packet, list(reviews), good)


def test_one_row_schema_shape_is_exact(tmp_path):
    import json

    from ptest import agent_assessment as AA

    packet = _pure_library_packet(tmp_path)
    reviews = AA.plan_item_reviews(packet)
    schemas = {review.schema for review in reviews}
    assert len(schemas) == 1
    schema = json.loads(reviews[0].schema.decode("utf-8"))
    assert set(schema["required"]) == {
        "status", "rationale", "evidence", "finding", "proof", "needs"}


# --- T4: dependency presence facts ------------------------------------------------

def test_dependency_facts_report_present_not_admitted_and_missing_locks(tmp_path):
    from ptest import agent_assessment as AA

    packet = _packet_for(tmp_path, {
        "pyproject.toml": "[project]\nname = 'demo'\n",
        "uv.lock": "invalid \x00 binary\n",
        "tests/test_pure.py": "def test_pure():\n    assert True\n",
    })
    by_eco = {}
    for fact in packet.dependencies:
        by_eco.setdefault((fact.ecosystem, fact.status), []).append(fact)
    present = by_eco.get(("python-lock", "uninspectable"), [])
    assert any("uv.lock is present but was not admitted to the review packet"
               in fact.detail for fact in present)
    missing = by_eco.get(("python-lock", "missing"), [])
    assert missing == []


def test_dependency_facts_emit_one_missing_lock_line(tmp_path):
    """No lock of the ecosystem: ONE missing fact, not three lines."""
    from ptest import agent_assessment as AA

    assert AA._LOCKS["uv.lock"] == "python-lock"
    packet = _packet_for(tmp_path, {
        "pyproject.toml": "[project]\nname = 'demo'\n",
        "tests/test_pure.py": "def test_pure():\n    assert True\n",
    })
    locks = [fact for fact in packet.dependencies
             if fact.ecosystem == "python-lock"]
    assert [(fact.status, fact.detail) for fact in locks] == [
        ("missing", "no lockfile (uv.lock, poetry.lock or pdm.lock)")]


def test_dependency_facts_emit_one_missing_lock_line_node_twin(tmp_path):
    """Twin: node without a lock gets one missing line too."""
    packet = _packet_for(tmp_path, {
        "package.json": '{"name": "demo"}\n',
        "tests/test_pure.py": "def test_pure():\n    assert True\n",
    })
    locks = [fact for fact in packet.dependencies
             if fact.ecosystem == "node-lock"]
    assert [(fact.status, fact.detail) for fact in locks] == [
        ("missing",
         "no lockfile (package-lock.json, pnpm-lock.yaml or yarn.lock)")]


def test_dependency_facts_report_only_lock_in_use(tmp_path):
    """A project using uv.lock reports only uv.lock, never its siblings.

    poetry.lock and pdm.lock are not listed as missing when uv.lock is
    the lock the ecosystem actually uses.
    """
    packet = _packet_for(tmp_path, {
        "pyproject.toml": "[project]\nname = 'demo'\n",
        "uv.lock": "invalid \x00 binary\n",
        "tests/test_pure.py": "def test_pure():\n    assert True\n",
    })
    locks = [fact for fact in packet.dependencies
             if fact.ecosystem == "python-lock"]
    assert [fact.detail for fact in locks] == [
        "uv.lock is present but was not admitted to the review packet."]


# --- T4: public child dict minus execution validates ------------------------------

def test_assembled_child_as_public_dict_minus_execution_validates(tmp_path):
    import json

    packet, _, child = _assembled_all_ok(_pure_library_packet(tmp_path))
    rows = [{"id": row.id, "status": row.status, "rationale": row.rationale,
             "label": row.label,
             "evidence": [{"path": cite.path, "start_line": cite.start_line,
                           "end_line": cite.end_line, "sha256": cite.sha256}
                          for cite in row.evidence]} for row in child.rows]
    findings = [{"id": finding.id, "summary": finding.summary,
                 "suggested_change": finding.suggested_change,
                 "recipe_id": finding.recipe_id,
                 "evidence": [{"path": cite.path,
                               "start_line": cite.start_line,
                               "end_line": cite.end_line,
                               "sha256": cite.sha256}
                              for cite in finding.evidence]}
                for finding in child.findings]
    score = None if child.score is None else {
        "satisfied": child.score.satisfied, "applicable": child.score.applicable,
        "percent": child.score.percent}
    child_dict = {"project_id": packet.project_id, "scope": packet.scope,
                  "packet_sha256": packet.packet_sha256, "rows": rows,
                  "score": score, "findings": findings, "limitations": []}
    payload = {"schema": "ptest.agent-assessment/v1",
               "provider": {"name": "claude", "cli_version": "1.2.3",
                            "profile": "ptest-item-review-v1"},
               "children": [child_dict], "limitations": [],
               "publication": {"status": "created",
                               "path": "recommendations.md",
                               "sha256": "12" * 32}}
    raw = json.dumps({"schema_version": 1, "kind": "agent-assessment",
                      "ptest_version": "0.1.5", "domain": None, "data": payload,
                      "error": None}).encode()
    document = C.decode_public_document(raw)
    assert document.kind == "agent-assessment"
    assert len(document.data["children"][0]["rows"]) == 12


# --- T4: vendored real-reply regression ------------------------------------------

_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "agent_assessment"
_E2E_SOURCE_PATHS = (
    ".ptest.toml", "pyproject.toml", "src/demo/__init__.py",
    "tests/test_demo.py",
)


def _e2e_packet(tmp_path, with_dependencies=False):
    import json

    request = json.loads(
        (_FIXTURE_DIR / "e2e-demo-request.json").read_text(
            encoding="utf-8"))
    texts = {excerpt["path"]: excerpt["text"]
             for excerpt in request["packet"]["excerpts"]
             if excerpt["path"] in _E2E_SOURCE_PATHS}
    assert set(texts) == set(_E2E_SOURCE_PATHS)
    if with_dependencies:
        texts["pyproject.toml"] = texts["pyproject.toml"].replace(
            'version = "0.1.0"\n',
            'version = "0.1.0"\ndependencies = ["sqlalchemy", "redis"]\n')
    return _packet_for(tmp_path, texts)


def _rebind_citation(cite, index, subset_paths):
    if cite["path"] not in subset_paths:
        return None
    excerpt = index[cite["path"]]
    start = max(excerpt.start_line,
                min(cite["start_line"], excerpt.end_line))
    end = max(start, min(cite["end_line"], excerpt.end_line))
    return {"path": cite["path"], "start_line": start, "end_line": end,
            "sha256": excerpt.sha256}


def _check_real_reply_regression(tmp_path, fixture_name,
                                 with_dependencies=False):
    import json

    from ptest import agent_assessment as AA

    packet = _e2e_packet(tmp_path, with_dependencies=with_dependencies)
    index = {excerpt.path: excerpt for excerpt in packet.excerpts}
    reviews = {review.item_id: review
               for review in AA.plan_item_reviews(packet)}
    document = json.loads(
        (_FIXTURE_DIR / fixture_name).read_text(encoding="utf-8"))
    rows = document["data"]["children"][0]["rows"]
    # The vendored recordings predate PARALLEL-001: replay exactly the
    # recorded rows, and pin the delta explicitly so future catalog growth
    # revisits this instead of silently skipping the new item.
    covered = [row["id"] for row in rows]
    assert set(EXPECTED_IDS) - set(covered) == {"PARALLEL-001"}
    assert covered == [row_id for row_id in EXPECTED_IDS
                       if row_id in set(covered)]
    replies = []
    expected = []
    for row in rows:
        review = reviews[row["id"]]
        subset_paths = set(review.excerpt_paths)
        if review.request is None:
            # Preserve the current deterministic answer. Historical model
            # recordings are not allowed to override ptest-owned facts.
            replies.append(None)
            expected.append(review.answer.status
                            if review.answer is not None
                            else "not-applicable")
            continue
        rebound = [_rebind_citation(cite, index, subset_paths)
                   for cite in row["evidence"]]
        if any(cite is None for cite in rebound):
            replies.append(json.dumps({
                "status": row["status"], "rationale": row["rationale"],
                "evidence": row["evidence"], "finding": None}).encode())
            expected.append("unknown")
            continue
        # These are genuine historical provider outputs and predate the
        # private proof protocol. Keep that provenance intact: without
        # proof/needs fields they now degrade to unknown instead of being
        # retroactively blessed with invented evidence.
        replies.append(json.dumps({
            "status": row["status"], "rationale": row["rationale"],
            "evidence": rebound, "finding": None}).encode())
        expected.append("unknown")
    if with_dependencies:
        assert all(review.request is not None
                   for review in reviews.values())
        assert all(
            all(cite["path"] in set(reviews[row["id"]].excerpt_paths)
                for cite in row["evidence"])
            for row in rows), "every fixture citation must route in-subset"
    ordered = tuple(reviews[row_id] for row_id in covered)
    ordered_replies = tuple(
        replies[[row["id"] for row in rows].index(row_id)]
        for row_id in covered)
    child = AA.assemble_child(packet, ordered, ordered_replies)
    assert [row.status for row in child.rows] == expected
    assert child.score == AA.score(child.rows)


def test_real_reply_regression_claude_2(tmp_path):
    _check_real_reply_regression(tmp_path, "claude-e2e-raw-assessment-2.json")


def test_real_reply_regression_claude_3(tmp_path):
    _check_real_reply_regression(tmp_path, "claude-e2e-raw-assessment-3.json")


def test_real_reply_regression_codex_1(tmp_path):
    _check_real_reply_regression(tmp_path, "codex-e2e-raw-assessment-1.json")


def test_real_reply_regression_full_review_without_skips(tmp_path):
    """With dependencies declared, every row is model-reviewed in-subset."""
    _check_real_reply_regression(
        tmp_path, "claude-e2e-raw-assessment-2.json", with_dependencies=True)
    _check_real_reply_regression(
        tmp_path, "codex-e2e-raw-assessment-1.json", with_dependencies=True)


def test_real_reply_outside_subset_drops_to_invalid(tmp_path):
    import json

    from ptest import agent_assessment as AA

    packet = _e2e_packet(tmp_path)
    reviews = AA.plan_item_reviews(packet)
    target = next(r for r in reviews if r.request is not None)
    stray = {"path": "ghost/missing.py", "start_line": 1, "end_line": 1,
             "sha256": "0" * 64}
    replies = tuple(
        json.dumps({"status": "satisfied",
                    "rationale": "Cites a file outside the routed subset.",
                    "evidence": [stray], "finding": None,
                    "proof": [], "needs": []}).encode()
        if review.item_id == target.item_id
        else (None if review.request is None
              else _satisfied_reply(packet, review))
        for review in reviews)
    child = AA.assemble_child(packet, reviews, replies)
    row = next(r for r in child.rows if r.id == target.item_id)
    assert (row.status, row.rationale) == (
        "unknown", AA.FAILED_PREFIX + "invalid reply: no valid citations")


# --- T4: chain through the real provider launcher ---------------------------------

def test_chain_fake_claude_through_real_launch_review(tmp_path, monkeypatch):
    import json
    import os
    import stat as stat_module

    from ptest import agent_assessment as AA
    from ptest import agent_providers as providers

    packet = _pure_library_packet(tmp_path)
    reviews = AA.plan_item_reviews(packet)
    pending = [review for review in reviews if review.request is not None]
    assert pending
    # Absence-based skips are gone (milestone 1): every item is reviewed.
    assert all(review.request is not None for review in reviews)

    fake = tmp_path / "claude"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "request = json.loads(sys.stdin.read())\n"
        "first = request['excerpts'][0]\n"
        "item_id = request['policy']['item']['id']\n"
        "reply = {'status': 'satisfied',\n"
        "         'rationale': 'Reviewed ' + item_id + ' against the cited excerpt lines.',\n"
        "         'evidence': [{'path': first['path'], 'start_line': first['start_line'],\n"
        "                     'end_line': first['end_line'], 'sha256': first['sha256']}],\n"
        "         'finding': None,\n"
        "         'proof': [{'role': 'applicability', 'citation_index': 0, 'quote': first['text'].splitlines()[0][:512]},\n"
        "                   {'role': 'mechanism', 'citation_index': 0, 'quote': first['text'].splitlines()[0][:512]}],\n"
        "         'needs': []}\n"
        "envelope = {'type': 'result', 'subtype': 'success', 'is_error': False,\n"
        "            'num_turns': 1, 'permission_denials': [],\n"
        "            'result': json.dumps(reply)}\n"
        "sys.stdout.write(json.dumps(envelope))\n",
        encoding="utf-8")
    os.chmod(fake, os.stat(fake).st_mode | stat_module.S_IXUSR)
    adapter = providers.ReviewerAdapter(
        name="claude", executable=str(fake), argv=(str(fake),), qualified=True,
        qualification_note="test double qualified")

    replies = []
    for review in reviews:
        if review.request is None:
            replies.append(None)
            continue
        result = providers.launch_review(
            adapter, review.request, review.schema, 60, lambda event: None)
        assert result.ok, result.error
        replies.append(result.assessment)
    child = AA.assemble_child(packet, reviews, tuple(replies))
    assert [row.id for row in child.rows] == list(EXPECTED_IDS)
    assert child.score is not None
    assert child.score.applicable == len(pending)
    assert child.score.satisfied == len(pending)
    assert child.findings == ()


def test_plan_and_assemble_empty_packet(tmp_path):
    import json

    from ptest import agent_assessment as AA

    packet = _packet_for(tmp_path, {})
    assert packet.excerpts == ()
    reviews = AA.plan_item_reviews(packet)
    assert len(reviews) == 12
    assert all(review.request is not None for review in reviews)
    replies = tuple(json.dumps({
        "status": "unknown", "rationale": "No evidence was admitted.",
        "evidence": [], "finding": None, "proof": [], "needs": []
    }).encode() for _ in reviews)
    child = AA.assemble_child(packet, reviews, replies)
    assert [row.status for row in child.rows] == ["unknown"] * 12
    assert child.score is not None
    assert (child.score.satisfied, child.score.applicable,
            child.score.percent) == (0, 12, 0)


# --- Round 15 twins: fenced replies, specific reasons, empty files -----------


def _fenced(payload: bytes, *, tag: str = "json") -> bytes:
    """Wrap a one-row payload the way Claude serves it: one markdown fence."""
    return b"```" + tag.encode("utf-8") + b"\n" + payload + b"\n```"


def test_fenced_valid_reply_is_accepted(tmp_path):
    """A real-Claude-shaped fenced reply validates like its bare payload."""
    from ptest import agent_assessment as AA

    packet = _pure_library_packet(tmp_path)
    reviews = AA.plan_item_reviews(packet)
    target = next(r for r in reviews if r.request is not None)
    bare = _satisfied_reply(packet, target)
    for reply in (bare, _fenced(bare), _fenced(bare, tag=""),
                  b"  \n" + _fenced(bare) + b"\n  "):
        replies = tuple(
            reply if review.item_id == target.item_id
            else (None if review.request is None
                  else _satisfied_reply(packet, review))
            for review in reviews)
        child = AA.assemble_child(packet, reviews, replies)
        row = next(r for r in child.rows if r.id == target.item_id)
        assert row.status == "satisfied", reply[:20]
        assert row.evidence, reply[:20]


def test_fence_with_prose_or_second_block_is_rejected(tmp_path):
    """Prose outside the fence, or a second fenced block, stays invalid."""
    from ptest import agent_assessment as AA

    packet = _pure_library_packet(tmp_path)
    reviews = AA.plan_item_reviews(packet)
    target = next(r for r in reviews if r.request is not None)
    good = _satisfied_reply(packet, target)
    bad = {
        "fence-plus-prose": _fenced(good) + b"\nHope this helps.",
        "prose-plus-fence": b"Here is the review:\n" + _fenced(good),
        "two-blocks": _fenced(good) + b"\n" + _fenced(good),
        "two-json-blocks": good + b"\n" + good,
    }
    for name, payload in bad.items():
        replies = tuple(
            payload if review.item_id == target.item_id
            else (None if review.request is None
                  else _satisfied_reply(packet, review))
            for review in reviews)
        child = AA.assemble_child(packet, reviews, replies)
        row = next(r for r in child.rows if r.id == target.item_id)
        assert (name, row.status, row.rationale) == (
            name, "unknown", AA.FAILED_PREFIX + "invalid reply: not JSON")


def test_fenced_reply_with_forbidden_keys_is_rejected(tmp_path):
    """A fence does not smuggle unknown fields past validation."""
    import json

    from ptest import agent_assessment as AA

    packet = _pure_library_packet(tmp_path)
    reviews = AA.plan_item_reviews(packet)
    target = next(r for r in reviews if r.request is not None)
    cite = _subset_citation(packet, target.excerpt_paths[0])
    document = json.loads(_r17_reply(
        packet, target,
        rationale="Rationale with enough substance here.",
        evidence=[cite]))
    document["score"] = 1
    payload = _fenced(json.dumps(document).encode())
    replies = tuple(
        payload if review.item_id == target.item_id
        else (None if review.request is None
              else _satisfied_reply(packet, review))
        for review in reviews)
    child = AA.assemble_child(packet, reviews, replies)
    row = next(r for r in child.rows if r.id == target.item_id)
    assert (row.status, row.rationale) == (
        "unknown",
        AA.FAILED_PREFIX + "invalid reply: carries an unknown field")


def test_invalid_replies_carry_specific_reasons(tmp_path):
    """Validation failures name their reason after the failed prefix."""
    from ptest import agent_assessment as AA

    packet = _pure_library_packet(tmp_path)
    reviews = AA.plan_item_reviews(packet)
    target = next(r for r in reviews if r.request is not None)
    good = _satisfied_reply(packet, target)
    assert AA.assemble_child(
        packet, reviews,
        tuple(good if review.item_id == target.item_id
              else (None if review.request is None
                    else _satisfied_reply(packet, review))
              for review in reviews))
    row = next(r for r in AA.assemble_child(
        packet, reviews,
        tuple(b"{nope" if review.item_id == target.item_id
              else (None if review.request is None
                    else _satisfied_reply(packet, review))
              for review in reviews)).rows if r.id == target.item_id)
    assert row.status == "unknown"
    assert row.rationale == AA.FAILED_PREFIX + "invalid reply: not JSON"


def test_empty_files_are_never_admitted(tmp_path):
    """A 0-byte tests/__init__.py carries no evidence and stays out."""
    from ptest import agent_assessment as AA

    root = tmp_path / "proj"
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "__init__.py").write_bytes(b"")
    (root / "tests" / "test_demo.py").write_text(
        "def test_demo():\n    assert True\n", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        "[project]\nname = 'demo'\n", encoding="utf-8")
    workspace, resolution = _workspace(root)
    packet = AA.build_packets(workspace, resolution)[0]
    assert "tests/__init__.py" not in [e.path for e in packet.excerpts]
    assert packet.excluded_count >= 1
    assert all(e.text for e in packet.excerpts)


def test_citation_to_empty_file_excluded_at_admission(tmp_path):
    """An empty file never enters the excerpt subset, so citing it is invalid."""
    from ptest import agent_assessment as AA

    packet = _pure_library_packet(
        tmp_path, {"tests/__init__.py": ""})
    assert "tests/__init__.py" not in [e.path for e in packet.excerpts]
    reviews = AA.plan_item_reviews(packet)
    target = next(r for r in reviews if r.request is not None)
    stray = {"path": "tests/__init__.py", "start_line": 1, "end_line": 1,
             "sha256": hashlib.sha256(b"").hexdigest()}
    replies = tuple(
        json.dumps({"status": "satisfied",
                    "rationale": "Cites the empty init file.",
                    "evidence": [stray],
                    "finding": None}).encode()
        if review.item_id == target.item_id
        else (None if review.request is None
              else _satisfied_reply(packet, review))
        for review in reviews)
    child = AA.assemble_child(packet, reviews, replies)
    row = next(r for r in child.rows if r.id == target.item_id)
    assert row.status == "unknown"
    assert row.rationale.startswith(AA.FAILED_PREFIX + "invalid reply")


# --- Round 17 twins: backtick normalization, per-citation drops ------------


def _r17_reply(packet, review, *, rationale, evidence, status="satisfied",
               finding=None):
    roles = (("applicability", "mechanism") if status == "satisfied"
             else ("applicability", "violation") if status == "gap"
             else ("applicability",) if status == "not-applicable" else ())
    subset = {excerpt.path: excerpt for excerpt in packet.excerpts
              if excerpt.path in review.excerpt_paths}
    finding_ids = set()
    if isinstance(finding, dict):
        finding_ids = {(item.get("path"), item.get("start_line"),
                        item.get("end_line"), item.get("sha256"))
                       for item in finding.get("evidence", [])
                       if isinstance(item, dict)}
    proof_index = None
    for index, cite in enumerate(evidence):
        if not isinstance(cite, dict) or cite.get("path") not in subset:
            continue
        identity = (cite.get("path"), cite.get("start_line"),
                    cite.get("end_line"), cite.get("sha256"))
        if status != "gap" or identity in finding_ids:
            proof_index = index
            break
    proof = []
    if proof_index is not None and roles:
        cite = evidence[proof_index]
        excerpt = subset[cite["path"]]
        line_index = cite["start_line"] - excerpt.start_line
        quote = (excerpt.text.splitlines() or [""])[line_index][:512]
        proof = [{"role": role, "citation_index": proof_index,
                  "quote": quote} for role in roles]
    return json.dumps({"status": status, "rationale": rationale,
                       "evidence": evidence, "finding": finding,
                       "proof": proof, "needs": []}).encode()


def _r17_assemble(packet, reviews, target, payload):
    from ptest import agent_assessment as AA

    child = AA.assemble_child(
        packet, reviews,
        tuple(payload if review.item_id == target.item_id
              else (None if review.request is None
                    else _satisfied_reply(packet, review))
              for review in reviews))
    return next(r for r in child.rows if r.id == target.item_id)


def test_backtick_prose_passes_and_is_stored_without_backticks(tmp_path):
    """Inline code like `db_session` normalizes to plain text, not a failure."""
    from ptest import agent_assessment as AA

    packet = _pure_library_packet(tmp_path)
    reviews = AA.plan_item_reviews(packet)
    target = next(r for r in reviews if r.request is not None)
    good = _subset_citation(packet, target.excerpt_paths[0])
    row = _r17_assemble(
        packet, reviews, target,
        _r17_reply(packet, target,
                   rationale="The db_session fixture from `db_session` "
                             "isolates each test with a savepoint rollback.",
                   evidence=[good]))
    assert row.status == "satisfied"
    assert "`" not in row.rationale
    assert "db_session" in row.rationale
    assert row.dropped_citations == 0


def test_command_shape_after_backtick_strip_stays_rejected(tmp_path):
    """Stripping backticks must not launder a command shape like `pytest -x`."""
    from ptest import agent_assessment as AA

    packet = _pure_library_packet(tmp_path)
    reviews = AA.plan_item_reviews(packet)
    target = next(r for r in reviews if r.request is not None)
    good = _subset_citation(packet, target.excerpt_paths[0])
    row = _r17_assemble(
        packet, reviews, target,
        _r17_reply(packet, target,
                   rationale="Run `pytest -x` to confirm the isolation.",
                   evidence=[good]))
    assert row.status == "unknown"
    assert row.rationale == (
        AA.FAILED_PREFIX + "invalid reply: "
        "rationale carries untrusted model content")


def test_link_after_backtick_strip_stays_rejected(tmp_path):
    """A Markdown link stays rejected with or without backticks nearby."""
    from ptest import agent_assessment as AA

    packet = _pure_library_packet(tmp_path)
    reviews = AA.plan_item_reviews(packet)
    target = next(r for r in reviews if r.request is not None)
    good = _subset_citation(packet, target.excerpt_paths[0])
    row = _r17_assemble(
        packet, reviews, target,
        _r17_reply(packet, target,
                   rationale="See `db_session` and [docs](https://invalid.test/x).",
                   evidence=[good]))
    assert row.status == "unknown"
    assert row.rationale == (
        AA.FAILED_PREFIX + "invalid reply: "
        "rationale carries untrusted model content")


def test_mixed_citations_keep_valid_and_count_dropped(tmp_path):
    """One valid citation among invalid ones keeps the row, minus the bad."""
    from ptest import agent_assessment as AA

    packet = _pure_library_packet(tmp_path)
    reviews = AA.plan_item_reviews(packet)
    target = next(r for r in reviews if r.request is not None)
    excerpt = next(e for e in packet.excerpts
                   if e.path == target.excerpt_paths[0])
    good = _subset_citation(packet, target.excerpt_paths[0])
    stale = dict(good, sha256="0" * 64)
    escaped = dict(good, start_line=excerpt.start_line,
                   end_line=excerpt.end_line + 50)
    outside = {"path": "elsewhere/missing.py", "start_line": 1,
               "end_line": 1, "sha256": "0" * 64}
    row = _r17_assemble(
        packet, reviews, target,
        _r17_reply(packet, target,
                   rationale="Kept by the one valid citation.",
                   evidence=[good, stale, escaped, outside]))
    assert row.status == "satisfied"
    assert [(c.path, c.start_line, c.end_line, c.sha256)
            for c in row.evidence] == [
        (good["path"], good["start_line"], good["end_line"],
         good["sha256"])]
    assert row.dropped_citations == 3


def test_all_invalid_citations_fail_with_no_valid_citations(tmp_path):
    """A row whose status needs evidence but keeps none fails as failed."""
    from ptest import agent_assessment as AA

    packet = _pure_library_packet(tmp_path)
    reviews = AA.plan_item_reviews(packet)
    target = next(r for r in reviews if r.request is not None)
    outside = {"path": "elsewhere/missing.py", "start_line": 1,
               "end_line": 1, "sha256": "0" * 64}
    row = _r17_assemble(
        packet, reviews, target,
        _r17_reply(packet, target,
                   rationale="Every citation misses the subset.",
                   evidence=[outside]))
    assert row.status == "unknown"
    assert row.rationale == (
        AA.FAILED_PREFIX + "invalid reply: no valid citations")


def test_finding_citations_drop_independently(tmp_path):
    """A gap keeps its row when the finding holds one valid citation."""
    from ptest import agent_assessment as AA

    packet = _pure_library_packet(tmp_path)
    reviews = AA.plan_item_reviews(packet)
    target = next(r for r in reviews if r.request is not None)
    good = _subset_citation(packet, target.excerpt_paths[0])
    stale = dict(good, sha256="0" * 64)
    outside = {"path": "elsewhere/missing.py", "start_line": 1,
               "end_line": 1, "sha256": "0" * 64}
    child = AA.assemble_child(
        packet, reviews,
        tuple(_r17_reply(
            packet, target,
            rationale="The teardown drops shared records without an owner.",
            evidence=[good], status="gap",
            finding={"summary": "Unowned teardown removes shared records.",
                     "suggested_change": "Remove only the owned namespace.",
                     "evidence": [outside, good, stale]})
        if review.item_id == target.item_id
        else (None if review.request is None
              else _satisfied_reply(packet, review))
        for review in reviews))
    row = next(r for r in child.rows if r.id == target.item_id)
    assert row.status == "gap"
    assert row.dropped_citations == 2
    finding = next(f for f in child.findings if f.id == target.item_id)
    assert [(c.path, c.start_line, c.end_line) for c in finding.evidence] == [
        (good["path"], good["start_line"], good["end_line"])]


def test_finding_without_any_valid_citation_fails_item(tmp_path):
    """A gap whose finding loses every citation fails like a bad row."""
    from ptest import agent_assessment as AA

    packet = _pure_library_packet(tmp_path)
    reviews = AA.plan_item_reviews(packet)
    target = next(r for r in reviews if r.request is not None)
    good = _subset_citation(packet, target.excerpt_paths[0])
    outside = {"path": "elsewhere/missing.py", "start_line": 1,
               "end_line": 1, "sha256": "0" * 64}
    row = _r17_assemble(
        packet, reviews, target,
        _r17_reply(packet, target,
                   rationale="The teardown drops shared records without an owner.",
                   evidence=[good], status="gap",
                   finding={"summary": "Unowned teardown removes shared records.",
                            "suggested_change": "Remove only the owned namespace.",
                            "evidence": [outside]}))
    assert row.status == "unknown"
    assert row.rationale == (
        AA.FAILED_PREFIX + "invalid reply: "
        "finding has no valid citations")


def test_unknown_row_without_citation_demand_keeps_valid_only(tmp_path):
    """Unknown needs no citations, so drops never fail it."""
    from ptest import agent_assessment as AA

    packet = _pure_library_packet(tmp_path)
    reviews = AA.plan_item_reviews(packet)
    target = next(r for r in reviews if r.request is not None)
    good = _subset_citation(packet, target.excerpt_paths[0])
    outside = {"path": "elsewhere/missing.py", "start_line": 1,
               "end_line": 1, "sha256": "0" * 64}
    row = _r17_assemble(
        packet, reviews, target,
        _r17_reply(packet, target, status="unknown",
                   rationale="The admitted excerpts say nothing either way.",
                   evidence=[outside, good]))
    assert row.status == "unknown"
    assert len(row.evidence) == 1
    assert row.dropped_citations == 1
    assert not row.rationale.startswith(AA.FAILED_PREFIX)


# --- T4 wave: deterministic answers, prompts, code-signal routing -------------

def _t4_packet(tmp_path):
    return _packet_in_root(tmp_path, {
        "pyproject.toml": "[project]\nname = 'demo'\n",
        ".ptest.toml": "[selection]\nenabled = false\n",
        "tests/test_pure.py": "def test_pure():\n    assert True\n",
    })


def _t4_answers():
    from ptest import deterministic_items as DI

    return {
        "SELECT-001": DI.DeterministicAnswer(
            item_id="SELECT-001", status="gap",
            reason=("automatic changed-input selection is disabled in "
                    ".ptest.toml; explicit file/path scopes still work"),
            evidence_paths=(".ptest.toml",),
            finding_summary=("Automatic changed-input selection is disabled "
                             "in .ptest.toml; explicit file/path scopes "
                             "still work."),
            finding_change="Enable selection with closed inputs in .ptest.toml."),
        "TIMING-001": DI.DeterministicAnswer(
            item_id="TIMING-001", status="satisfied",
            reason="ptest recorded per-test timings for 2 tests in the last clean full run; 0 take over 3 s (slowest 0.4 s)",
            evidence_paths=(".ptest.toml",)),
    }


def test_ptest_answer_prefix_is_frozen_literal():
    from ptest import agent_assessment as AA

    assert AA.PTEST_ANSWER_PREFIX == "Answered by ptest: "


def test_plan_item_reviews_binds_deterministic_answers_without_calls(tmp_path):
    from ptest import agent_assessment as AA

    packet = _t4_packet(tmp_path)
    reviews = {review.item_id: review
               for review in AA.plan_item_reviews(packet, answers=_t4_answers())}
    assert len(reviews) == 12
    for item_id in ("SELECT-001", "TIMING-001"):
        review = reviews[item_id]
        assert review.request is None
        assert review.skip_reason is None
        assert review.answer is not None
        assert review.answer.item_id == item_id
    assert all(review.request is not None
               for item_id, review in reviews.items()
               if item_id not in ("SELECT-001", "TIMING-001",
                                  "DB-001", "DB-002", "CACHE-001"))


def test_item_review_requires_exactly_one_of_request_skip_answer(tmp_path):
    from ptest import agent_assessment as AA

    packet = _t4_packet(tmp_path)
    reviews = {review.item_id: review
               for review in AA.plan_item_reviews(packet)}
    sample = reviews["TIME-001"]
    with __import__("pytest").raises((TypeError, ValueError)):
        AA.ItemReview(item_id=sample.item_id, label=sample.label,
                      scope=sample.scope, request=sample.request,
                      schema=sample.schema,
                      excerpt_paths=sample.excerpt_paths,
                      skip_reason="both set", answer=None)


def test_assemble_child_builds_deterministic_gap_and_satisfied(tmp_path):
    from ptest import agent_assessment as AA

    packet = _t4_packet(tmp_path)
    answers = _t4_answers()
    reviews = AA.plan_item_reviews(packet, answers=answers)
    replies = tuple(
        None if review.request is None else "synthetic provider failure"
        for review in reviews)
    child = AA.assemble_child(packet, reviews, replies)
    by_id = {row.id: row for row in child.rows}
    gap = by_id["SELECT-001"]
    assert gap.status == "gap"
    assert gap.rationale == (
        AA.PTEST_ANSWER_PREFIX
        + "automatic changed-input selection is disabled in .ptest.toml; "
        "explicit file/path scopes still work")
    assert len(gap.evidence) == 1
    assert gap.evidence[0].path == ".ptest.toml"
    excerpt = next(e for e in packet.excerpts if e.path == ".ptest.toml")
    assert gap.evidence[0].start_line == excerpt.start_line
    assert gap.evidence[0].end_line == excerpt.end_line
    assert [finding.id for finding in child.findings] == ["SELECT-001"]
    assert child.findings[0].recipe_id is None
    assert child.findings[0].summary == answers["SELECT-001"].finding_summary
    satisfied = by_id["TIMING-001"]
    assert satisfied.status == "satisfied"
    assert satisfied.rationale.startswith(AA.PTEST_ANSWER_PREFIX)
    assert satisfied.evidence[0].path == ".ptest.toml"


def test_assemble_child_rejects_reply_for_answered_review(tmp_path):
    from ptest import agent_assessment as AA

    packet = _t4_packet(tmp_path)
    reviews = AA.plan_item_reviews(packet, answers=_t4_answers())
    replies = tuple(
        b"{}" if review.answer is not None else None
        if review.request is None else "failure"
        for review in reviews)
    with __import__("pytest").raises(ValueError):
        AA.assemble_child(packet, reviews, replies)


def test_assemble_child_degrades_uncitable_answer_to_unknown(tmp_path):
    from ptest import agent_assessment as AA
    from ptest import deterministic_items as DI

    packet = _packet_in_root(tmp_path, {
        "pyproject.toml": "[project]\nname = 'demo'\n",
        "tests/test_pure.py": "def test_pure():\n    assert True\n",
    })
    assert ".ptest.toml" not in {e.path for e in packet.excerpts}
    answers = {"SELECT-001": DI.DeterministicAnswer(
        item_id="SELECT-001", status="gap",
        reason=("automatic changed-input selection is disabled in "
                ".ptest.toml; explicit file/path scopes still work"),
        evidence_paths=(".ptest.toml",),
        finding_summary=("Automatic changed-input selection is disabled in "
                         ".ptest.toml; explicit file/path scopes still work."),
        finding_change="Enable selection with closed inputs in .ptest.toml.")}
    reviews = AA.plan_item_reviews(packet, answers=answers)
    replies = tuple(
        None if review.request is None else "synthetic provider failure"
        for review in reviews)
    child = AA.assemble_child(packet, reviews, replies)
    row = next(r for r in child.rows if r.id == "SELECT-001")
    assert row.status == "unknown"
    assert row.rationale == (
        AA.PTEST_ANSWER_PREFIX
        + "automatic changed-input selection is disabled in .ptest.toml; "
        "explicit file/path scopes still work"
        + " (the ptest config is not in the review evidence)")
    assert row.evidence == ()
    assert [finding.id for finding in child.findings] == []


def test_model_reply_with_ptest_prefix_is_rejected(tmp_path):
    from ptest import agent_assessment as AA

    packet = _pure_library_packet(tmp_path)
    reviews = AA.plan_item_reviews(packet)
    target = next(r for r in reviews if r.request is not None)
    reply = _one_row_bytes(
        packet, target, status="satisfied",
        paths=[target.excerpt_paths[0]],
        rationale=AA.PTEST_ANSWER_PREFIX + "the excerpts show the mechanism.")
    child = AA.assemble_child(
        packet, reviews,
        tuple(reply if r is target else (None if r.request is None else "down")
              for r in reviews))
    row = next(r for r in child.rows if r.id == target.item_id)
    assert row.status == "unknown"
    assert row.rationale.startswith(AA.FAILED_PREFIX)


def test_item_instruction_pins_gap_satisfied_unknown_standard():
    from ptest import agent_assessment as AA

    instruction = AA._ITEM_INSTRUCTION.casefold()
    assert "gap only with a cited concrete violation" in instruction
    assert "satisfied only when the evidence shows the guaranteeing mechanism" in instruction
    assert "otherwise" in instruction and "unknown" in instruction
    assert "missing evidence" in instruction
    assert "absence of code is unknown" in instruction
    assert "answered by ptest" in instruction


@pytest.mark.parametrize("signal", [
    "tmp_path", "tempfile", "mkdtemp", "socket", "bind(",
    "PORT = 8080", "port=8080", "/tmp", "app.lock", "filelock", "flock",
])
def test_resource_signals_route_to_resource_item(tmp_path, signal):
    from ptest import agent_assessment as AA

    packet = _packet_in_root(tmp_path, {
        "pyproject.toml": "[project]\nname = 'demo'\n",
        "tests/test_res.py": f"def test_x():\n    assert {signal!r} != ''\n",
    })
    reviews = {review.item_id: review
               for review in AA.plan_item_reviews(packet)}
    assert "tests/test_res.py" in reviews["RESOURCE-001"].excerpt_paths


@pytest.mark.parametrize("signal", [
    "httpx", "requests", "aiohttp", "respx", "responses",
    "pytest-socket", "socket.socket", "disable_socket", "vcr",
])
def test_network_signals_route_to_network_item(tmp_path, signal):
    from ptest import agent_assessment as AA

    packet = _packet_in_root(tmp_path, {
        "pyproject.toml": "[project]\nname = 'demo'\n",
        "tests/test_net.py": f"def test_x():\n    assert {signal!r} != ''\n",
    })
    reviews = {review.item_id: review
               for review in AA.plan_item_reviews(packet)}
    assert "tests/test_net.py" in reviews["NETWORK-001"].excerpt_paths


@pytest.mark.parametrize("signal", [
    "subprocess", "asyncio.create_subprocess", "multiprocessing", "Popen",
    "os.fork", ".join(", ".terminate(", ".kill(", ".wait(",
])
def test_process_signals_route_to_process_item(tmp_path, signal):
    from ptest import agent_assessment as AA

    packet = _packet_in_root(tmp_path, {
        "pyproject.toml": "[project]\nname = 'demo'\n",
        "tests/test_proc.py": f"def test_x():\n    assert {signal!r} != ''\n",
    })
    reviews = {review.item_id: review
               for review in AA.plan_item_reviews(packet)}
    assert "tests/test_proc.py" in reviews["PROCESS-001"].excerpt_paths


def test_conftest_fixture_defining_used_fixture_ranks_first(tmp_path):
    """A referenced conftest fixture outranks a scanner hit."""
    from ptest import agent_assessment as AA

    packet = _packet_in_root(tmp_path, {
        "pyproject.toml": "[project]\nname = 'demo'\n",
        "conftest.py": ("import pytest\n\n\n@pytest.fixture\n"
                        "def owned_tmp(tmp_path):\n    return tmp_path\n"),
        "tests/test_res.py": ("def test_x(owned_tmp):\n"
                              "    assert str(owned_tmp) != ''\n"
                              "    assert 'tmp_path' != ''\n"),
    })
    reviews = {review.item_id: review
               for review in AA.plan_item_reviews(packet)}
    paths = reviews["RESOURCE-001"].excerpt_paths
    assert "conftest.py" in paths
    assert "tests/test_res.py" in paths
    assert paths.index("conftest.py") < paths.index("tests/test_res.py")


def test_plan_item_reviews_rejects_mismatched_answer_id(tmp_path):
    from ptest import agent_assessment as AA

    packet = _t4_packet(tmp_path)
    swapped = dict(_t4_answers())
    swapped["SELECT-001"] = _t4_answers()["TIMING-001"]
    with __import__("pytest").raises(ValueError):
        AA.plan_item_reviews(packet, answers=swapped)


def test_assemble_child_degrades_answer_without_evidence_paths(tmp_path):
    """A satisfied answer with no citable excerpt degrades to unknown."""
    from ptest import agent_assessment as AA
    from ptest import deterministic_items as DI

    packet = _t4_packet(tmp_path)
    answers = {"TIMING-001": DI.DeterministicAnswer(
        item_id="TIMING-001", status="satisfied",
        reason="ptest recorded per-test timings for 2 tests in the last clean full run; 0 take over 3 s (slowest 0.4 s)",
        evidence_paths=())}
    reviews = AA.plan_item_reviews(packet, answers=answers)
    replies = tuple(
        None if review.request is None else "synthetic provider failure"
        for review in reviews)
    child = AA.assemble_child(packet, reviews, replies)
    row = next(r for r in child.rows if r.id == "TIMING-001")
    assert row.status == "unknown"
    assert row.rationale.endswith(
        "(the ptest config is not in the review evidence)")
    assert row.evidence == ()


def test_addopts_span_ignores_wrong_section_pyproject():
    """Only [tool.pytest.ini_options] decides addopts in pyproject.toml."""
    from ptest import agent_assessment as AA

    text = ("[tool.other]\naddopts = \"zzz\"\n"
            "[tool.pytest.ini_options]\naddopts = \"-n 4\"\n")
    assert AA._addopts_span(text, "pyproject.toml") == (4, 4)


def test_addopts_span_ignores_trailing_comment_bracket():
    """A bracket inside a trailing comment must not extend the span."""
    from ptest import agent_assessment as AA

    text = "[tool.pytest.ini_options]\naddopts = \"-n 4\"  # (see docs\n"
    assert AA._addopts_span(text, "pyproject.toml") == (2, 2)


def test_addopts_span_multiline_toml_array():
    from ptest import agent_assessment as AA

    text = ("[tool.pytest.ini_options]\naddopts = [\n  \"-n\",\n  \"4\",\n]\n")
    assert AA._addopts_span(text, "pyproject.toml") == (2, 5)


def test_addopts_span_ini_indented_continuation():
    from ptest import agent_assessment as AA

    text = "[pytest]\naddopts = -n 4\n  --dist=loadgroup\n  -q\n"
    assert AA._addopts_span(text, "pytest.ini") == (2, 4)


def test_addopts_span_entry_on_last_line():
    from ptest import agent_assessment as AA

    text = "[pytest]\naddopts = -n 4"
    assert AA._addopts_span(text, "pytest.ini") == (2, 2)


def test_bracket_depth_ignores_comment_bracket():
    from ptest import agent_assessment as AA

    assert AA._bracket_depth('addopts = "-n 4"  # (see docs') == 0
