"""Milestone 1: runner-aware review context and admission (TDD).

Covers ``ptest.review_context`` (immutable context metadata, pure
classification/routing helpers, single Vitest config selector, bounded
static parsing) and the context-related ``agent_assessment`` behavior:
setup-first admission, runner config in every item, raw-lock exclusion
with dependency facts kept, partial-on-ambiguity (never union), bounded
known-suite exclusions, and selected-directory privacy.

Abuse twins (secure-by-spec): hostile/dynamic configs are never
executed, symlinks/traversal/depth/byte limits degrade to explicit
missing reasons, unknown argv never proves exclusion, and secret names
never enter context inventories.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from ptest import contracts as C
from ptest import doctor
from factories_agents import (
    agent_config as _config,
    agent_domain as _domain,
    agent_packet_for as _packet_for,
    agent_resolution as _resolution,
    agent_v1_config_text as _v1_config_text,
)

CHILD_PID = "ab" * 16
FIXTURES = Path(__file__).parent / "fixtures" / "doctor_accuracy"


def _copy_fixture(tmp_path: Path, name: str) -> Path:
    target = tmp_path / name
    shutil.copytree(FIXTURES / name, target, symlinks=True)
    # Python samples are stored as inert data in the test tree so the
    # repository's own pytest collection cannot import them. Materialize
    # their source suffix only in the isolated temporary child checkout.
    for sample in target.rglob("*.py.sample"):
        sample.rename(sample.with_suffix(""))
    return target


# --- ReviewContext value semantics -------------------------------------------


def test_review_context_is_frozen_with_compat_default(tmp_path):
    from ptest import agent_assessment as AA
    from ptest import review_context as RC

    packet = _packet_for(tmp_path, {"src/m.py": "x = 1\n"})
    assert isinstance(packet.context, RC.ReviewContext)
    assert packet.context.config_status in (
        "resolved", "partial", "unavailable")
    with pytest.raises(Exception):
        packet.context.runner_kind = "vitest"  # frozen
    legacy = AA.EvidencePacket(
        declaration=".", project_id=CHILD_PID, scope=".",
        packet_sha256="0" * 64, excerpts=(), dependencies=(),
        runner_kind="pytest", excluded_count=0, truncated_count=0,
        file_count=0, byte_count=0)
    assert isinstance(legacy.context, RC.ReviewContext)


def test_packet_hash_includes_context(tmp_path):
    from ptest import agent_assessment as AA
    from ptest import review_context as RC

    packet = _packet_for(tmp_path, {"src/m.py": "x = 1\n"})
    altered = RC.replace(packet.context, config_status="partial")
    twin = AA.EvidencePacket(
        declaration=packet.declaration, project_id=packet.project_id,
        scope=packet.scope, packet_sha256="0" * 64,
        excerpts=packet.excerpts, dependencies=packet.dependencies,
        runner_kind=packet.runner_kind,
        excluded_count=packet.excluded_count,
        truncated_count=packet.truncated_count,
        file_count=packet.file_count, byte_count=packet.byte_count,
        context=altered)
    assert AA.packet_hash(twin) != packet.packet_sha256


def test_empty_context_records_unavailable(tmp_path):
    from ptest import review_context as RC

    ctx = RC.empty_context("command")
    assert ctx.runner_kind == "command"
    assert ctx.config_status == "unavailable"
    assert ctx.config_paths == () and ctx.missing != ()


# --- Vitest single-config selector --------------------------------------------


def test_vitest_two_candidates_stay_partial_and_never_union(tmp_path):
    from ptest import review_context as RC

    root = _copy_fixture(tmp_path, "vitest-twins")
    selected, status = RC.select_vitest_config(root, ())
    assert selected is None
    assert status == "partial"
    includes = RC.selected_include_patterns(root, selected, ())
    assert includes is None  # no union of conflicting patterns


def test_vitest_explicit_config_selects_among_candidates(tmp_path):
    from ptest import review_context as RC

    root = _copy_fixture(tmp_path, "vitest-twins")
    selected, status = RC.select_vitest_config(
        root, ("--config", "vitest.config.mjs"))
    assert selected == "vitest.config.mjs"
    assert status == "resolved"
    selected, status = RC.select_vitest_config(
        root, ("--config=vitest.config.ts",))
    assert selected == "vitest.config.ts"
    assert status == "resolved"


def test_vitest_conflicting_config_flags_stay_partial(tmp_path):
    from ptest import review_context as RC

    root = _copy_fixture(tmp_path, "vitest-twins")
    selected, status = RC.select_vitest_config(
        root, ("--config", "vitest.config.ts",
               "--config", "vitest.config.mjs"))
    assert selected is None
    assert status == "partial"


def test_vitest_single_candidate_resolves(tmp_path):
    from ptest import review_context as RC

    root = _copy_fixture(tmp_path, "vitest-single")
    selected, status = RC.select_vitest_config(root, ())
    assert selected == "vitest.config.ts"
    assert status == "resolved"


def test_vitest_config_outside_scope_is_not_selected(tmp_path):
    from ptest import review_context as RC

    root = _copy_fixture(tmp_path, "vitest-single")
    selected, status = RC.select_vitest_config(
        root, ("--config", "../outside.config.ts"))
    assert selected is None
    assert status == "partial"


# --- argv profiles --------------------------------------------------------------


def test_membership_changing_full_args_recorded_per_profile():
    from ptest import review_context as RC

    scoped = RC.effective_argv(("src/a.test.ts",), ())
    full = RC.effective_argv(("src/a.test.ts",), ("e2e/filter",))
    assert RC.argv_membership_status(scoped) == "partial"
    assert RC.argv_membership_status(full) == "partial"
    assert RC.argv_membership_status(("--config", "vitest.config.ts",
                                      "e2e/load.spec.ts")) == "partial"
    assert scoped != full


def test_unknown_membership_argv_never_proves_exclusion():
    from ptest import review_context as RC

    assert RC.argv_membership_status(("--some-future-flag",)) == "partial"
    assert RC.argv_membership_status(("-t", "pattern")) == "partial"
    assert RC.argv_membership_status(()) == "resolved"
    assert not RC.conclusively_excluded(
        "src/a.test.ts", ("partial",), ("resolved",))


def test_conclusive_exclusion_needs_every_reviewed_profile():
    from ptest import review_context as RC

    assert RC.conclusively_excluded(
        "e2e/load.spec.ts", ("excluded",), ("excluded",))
    assert not RC.conclusively_excluded(
        "e2e/load.spec.ts", ("excluded",), ("unknown",))


# --- hostile config handling -----------------------------------------------------


def test_traversal_import_in_config_is_not_followed(tmp_path):
    from ptest import review_context as RC

    reads: list[str] = []

    def reader(rel: str, limit: int):
        reads.append(rel)
        if rel == "vitest.config.ts":
            return b"import x from '../../../etc/evil';\n"
        raise FileNotFoundError(rel)

    ctx = RC.collect_vitest_context(
        tmp_path, "", (), reader, {"vitest.config.ts"})
    reasons = [reason for _, reason in ctx.missing]
    assert "outside-selected-scope" in reasons or \
        "unresolved-import" in reasons
    assert not any(r.startswith("..") or r.startswith("/")
                   for r in reads if r != "vitest.config.ts")


def test_symlinked_config_is_never_followed(tmp_path):
    from ptest import review_context as RC

    real = tmp_path / "real.config.ts"
    real.write_text("export default {};\n", encoding="utf-8")
    os.symlink(str(real), tmp_path / "vitest.config.ts")

    def reader(rel: str, limit: int):
        from ptest.files import read_regular
        return read_regular(tmp_path, rel, limit)

    ctx = RC.collect_vitest_context(
        tmp_path, "", (), reader, {"vitest.config.ts"})
    assert ctx.config_status in ("partial", "unavailable")
    assert ctx.config_paths == ()


def test_context_traversal_depth_is_bounded(tmp_path):
    from ptest import review_context as RC

    chain = {}
    for depth in range(10):
        nxt = f"f{depth + 1}" if depth < 9 else None
        body = f"import './{nxt}';\n" if nxt else "export const x = 1;\n"
        chain[f"f{depth}.ts"] = body

    def reader(rel: str, limit: int):
        if rel in chain:
            return chain[rel].encode("utf-8")
        raise FileNotFoundError(rel)

    ctx = RC.collect_vitest_context(
        tmp_path, "", (), reader, set(), seeds=("f0.ts",))
    assert len(ctx.relations) <= RC.CONTEXT_MAX_FILES
    assert any(reason in ("depth-limit", "read-limit", "unresolved-import")
               for _, reason in ctx.missing)


def test_oversized_config_records_read_limit(tmp_path):
    from ptest import review_context as RC

    def reader(rel: str, limit: int):
        raise MemoryError("too large")

    ctx = RC.collect_vitest_context(
        tmp_path, "", (), reader, {"vitest.config.ts"})
    assert ctx.config_status in ("partial", "unavailable")
    assert any(reason == "read-limit" for _, reason in ctx.missing)


def test_dynamic_config_values_stay_partial(tmp_path):
    from ptest import review_context as RC

    def reader(rel: str, limit: int):
        if rel == "vitest.config.ts":
            return (b"import { defineConfig } from 'vitest/config';\n"
                    b"const extra = compute();\n"
                    b"export default defineConfig({ test: { setupFiles: extra } });\n")
        raise FileNotFoundError(rel)

    ctx = RC.collect_vitest_context(
        tmp_path, "", (), reader, {"vitest.config.ts"})
    assert ctx.config_status == "partial"
    assert any(reason == "dynamic-config" for _, reason in ctx.missing)


def test_conditional_config_export_cannot_exclude_active_suite(tmp_path):
    """Literal arrays inside a mode branch are not effective config proof."""
    from ptest import review_context as RC

    config = (b"import { defineConfig } from 'vitest/config';\n"
              b"export default defineConfig(({ mode }) => mode === 'unit' ? "
              b"{ test: { exclude: ['e2e/**'] } } : "
              b"{ test: { include: ['e2e/**'] } });\n")

    def reader(rel: str, limit: int):
        if rel == "vitest.config.ts":
            return config
        raise FileNotFoundError(rel)

    ctx = RC.collect_vitest_context(
        tmp_path, "", (), reader, {"vitest.config.ts"})
    assert ctx.config_status == "partial"
    assert "e2e/**" not in ctx.known_excluded
    assert ("vitest.config.ts", "dynamic-config") in ctx.missing


def test_full_args_membership_change_keeps_excluded_candidate_unknown(tmp_path):
    from ptest import agent_assessment as AA

    root = tmp_path / "child"
    packet = _packet_for(root, {
        "vitest.config.ts": "export default { test: { exclude: ['e2e/**'] } };\n",
        "src/active.test.ts": "test('active', () => {});\n",
        "e2e/load.spec.ts": "test('browser', () => {});\n",
    }, config=_config(
        runner_kind=C.RunnerKind.VITEST, launcher=("node",),
        args=("--config=vitest.config.ts",),
        full_args=("--project", "integration"), test_roots=("src",)))
    assert packet.context.suite_profiles[0].status == "resolved"
    assert packet.context.suite_profiles[1].status == "partial"
    assert "e2e/load.spec.ts" in [excerpt.path for excerpt in packet.excerpts]
    assert "e2e/load.spec.ts" not in packet.context.known_excluded


def test_unexported_test_object_is_not_effective_suite_config():
    """An unused object literal cannot lend excludes to exported config."""
    from ptest import review_context as RC

    text = ("import { defineConfig, configDefaults } from 'vitest/config';\n"
            "const unused = { test: { exclude: ['e2e/**'] } };\n"
            "export default defineConfig({ test: { include: ['src/**'] } });\n")

    patterns = RC.parse_config_text(text)
    assert patterns.excludes == ()
    assert patterns.includes == ("src/**",)


def test_arbitrary_test_object_spread_is_partial_but_known_defaults_stay_supported():
    from ptest import review_context as RC

    unknown = RC.parse_config_text(
        "export default { test: { ...sharedTest, exclude: ['e2e/**'] } };\n")
    supported = RC.parse_config_text(
        "export default { test: { exclude: [...configDefaults.exclude, "
        "'e2e/**'] } };\n")
    assert unknown.dynamic
    assert supported.dynamic is False
    assert supported.excludes == ("e2e/**",)


@pytest.mark.parametrize("config_text", [
    "export default { test: { exclude: ['e2e/**'], exclude: [] } };\n",
    "export default defineConfig({ test: { exclude: ['e2e/**'] } });\n",
    ("function defineConfig(value) { return { ...value, "
     "test: { exclude: [] } }; }\n"
     "export default defineConfig({ test: { exclude: ['e2e/**'] } });\n"),
])
def test_vitest_ambiguous_exported_arrays_never_exclude_suite(
        tmp_path, config_text):
    """Duplicate keys and unverified wrappers cannot decide membership."""
    from ptest import agent_assessment as AA

    root = tmp_path / "child"
    packet = _packet_for(root, {
        "vitest.config.ts": config_text,
        "src/unit.test.ts": "test('active', () => {});\n",
        "e2e/load.spec.ts": "test('browser', () => {});\n",
    }, config=_config(
        runner_kind=C.RunnerKind.VITEST, launcher=("node",),
        args=("--config=vitest.config.ts",), test_roots=("src",)))

    assert packet.context.suite_profiles[0].status == "partial"
    assert ("vitest.config.ts", "dynamic-config") in packet.context.missing
    assert "e2e/load.spec.ts" in {excerpt.path
                                  for excerpt in packet.excerpts}
    assert AA._conclusive_suite_skips(
        runner_kind="vitest", context=packet.context,
        rels={"e2e/load.spec.ts"}, role_of=dict(packet.context.roles),
        linked=set(), runner_args=("--config=vitest.config.ts",),
        runner_full_args=()) == set()


def test_vitest_literal_include_membership_can_exclude_outside_files(
        tmp_path):
    from ptest import agent_assessment as AA

    root = tmp_path / "child"
    packet = _packet_for(root, {
        "vitest.config.ts": (
            "export default { test: { include: ['src/unit/**'] } };\n"),
        "src/unit/active.test.ts": "test('active', () => {});\n",
        "e2e/load.spec.ts": "test('browser', () => {});\n",
    }, config=_config(
        runner_kind=C.RunnerKind.VITEST, launcher=("node",),
        args=("--config=vitest.config.ts",), test_roots=("src",)))

    assert packet.context.suite_profiles[0].status == "resolved"
    assert packet.context.suite_profiles[0].includes == ("src/unit/**",)
    assert "e2e/load.spec.ts" not in {excerpt.path
                                       for excerpt in packet.excerpts}
    assert packet.excluded_count >= 1
    assert AA._conclusive_suite_skips(
        runner_kind="vitest", context=packet.context,
        rels={"e2e/load.spec.ts"}, role_of=dict(packet.context.roles),
        linked=set(), runner_args=("--config=vitest.config.ts",),
        runner_full_args=()) == {"e2e/load.spec.ts"}


def test_vitest_unsupported_include_glob_keeps_membership_unknown(tmp_path):
    root = tmp_path / "child"
    packet = _packet_for(root, {
        "vitest.config.ts": (
            "export default { test: { "
            "include: ['src/@(unit|integration)/**'] } };\n"),
        "src/unit/active.test.ts": "test('active', () => {});\n",
        "e2e/load.spec.ts": "test('browser', () => {});\n",
    }, config=_config(
        runner_kind=C.RunnerKind.VITEST, launcher=("node",),
        args=("--config=vitest.config.ts",), test_roots=("src",)))

    assert packet.context.suite_profiles[0].status == "partial"
    assert "e2e/load.spec.ts" in {excerpt.path
                                  for excerpt in packet.excerpts}


def test_vitest_full_profile_unknown_keeps_outside_include_candidate(tmp_path):
    root = tmp_path / "child"
    packet = _packet_for(root, {
        "vitest.config.ts": (
            "export default { test: { include: ['src/unit/**'] } };\n"),
        "src/unit/active.test.ts": "test('active', () => {});\n",
        "e2e/load.spec.ts": "test('browser', () => {});\n",
    }, config=_config(
        runner_kind=C.RunnerKind.VITEST, launcher=("node",),
        args=("--config=vitest.config.ts",),
        full_args=("--project", "integration"), test_roots=("src",)))

    assert packet.context.suite_profiles[0].status == "resolved"
    assert packet.context.suite_profiles[1].status == "partial"
    assert "e2e/load.spec.ts" in {excerpt.path
                                  for excerpt in packet.excerpts}


def test_pytest_mandatory_review_includes_fixture_closure_without_item_hits(
        tmp_path):
    import re

    from ptest import agent_assessment as AA
    from ptest.checklist import CATALOG

    root = tmp_path / "child"
    packet = _packet_for(root, {
        "tests/conftest.py": "pytest_plugins = ['support.fixtures']\n",
        "support/fixtures.py": (
            "def deny_remote_access(config):\n"
            "    config.addinivalue_line('markers', 'offline')\n"),
        "tests/test_basic.py": "def test_basic():\n    assert True\n",
    })
    role_map = dict(packet.context.roles)
    assert role_map["support/fixtures.py"] == "fixture"

    network_item = next(entry for entry in CATALOG
                        if entry.id == "NETWORK-001")
    helper = next(excerpt for excerpt in packet.excerpts
                  if excerpt.path == "support/fixtures.py")
    assert not any(re.search(pattern, helper.path)
                   for pattern in network_item.path_patterns)
    assert not any(re.search(pattern, helper.text)
                   for pattern in network_item.text_patterns)

    mandatory_paths = {excerpt.path
                       for excerpt in AA._mandatory_context_excerpts(packet)}
    assert "support/fixtures.py" in mandatory_paths
    network_review = next(review for review in AA.plan_item_reviews(packet)
                          if review.item_id == "NETWORK-001")
    assert "support/fixtures.py" in network_review.excerpt_paths


def test_secret_names_never_enter_context_inventory(tmp_path):
    from ptest import review_context as RC

    def reader(rel: str, limit: int):
        if rel == "vitest.config.ts":
            return (b"export default { test: { setupFiles: ['./.env'], "
                    b"include: ['./secrets/x.test.ts'] } };\n")
        raise FileNotFoundError(rel)

    ctx = RC.collect_vitest_context(
        tmp_path, "", (), reader, {"vitest.config.ts"})
    inventory = (list(ctx.config_paths) + list(ctx.known_excluded)
                 + [path for path, _ in ctx.roles]
                 + [path for path, _ in ctx.missing])
    assert not any(".env" in path or "secret" in path
                   for path in inventory)


# --- packet-level admission -------------------------------------------------------


def test_lock_bodies_not_admitted_but_facts_kept(tmp_path):
    packet = _packet_for(tmp_path, {
        "pyproject.toml": "[project]\nname = 'demo'\n",
        "uv.lock": "version = 1\n",
        "src/m.py": "x = 1\n",
    })
    assert not any(e.path.endswith("uv.lock") for e in packet.excerpts)
    kinds = {(d.ecosystem, d.status) for d in packet.dependencies}
    assert ("python", "declared") in kinds
    assert ("python-lock", "locked") in kinds
    assert all("version = 1" not in e.text for e in packet.excerpts)


def test_runner_config_in_every_item_request(tmp_path):
    """Admitted .ptest.toml reaches every item; metadata stays honest."""
    import json

    from ptest import agent_assessment as AA

    root = tmp_path / "child"
    packet = _packet_for(root, {
        ".ptest.toml": _v1_config_text(CHILD_PID, "pytest"),
        "pyproject.toml": "[project]\nname = 'demo'\n",
        "tests/test_pure.py": "def test_pure():\n    assert True\n",
    })
    assert ".ptest.toml" in [e.path for e in packet.excerpts]
    reviews = AA.plan_item_reviews(packet)
    assert all(r.request is not None for r in reviews)
    for review in reviews:
        body = json.loads(review.request.decode("utf-8"))
        assert body["packet"]["runner_kind"] == "pytest"
        assert ".ptest.toml" in review.excerpt_paths
        offered = [e["path"] for e in body["excerpts"]]
        assert ".ptest.toml" in offered
        assert body["packet"]["config_status"] in (
            "resolved", "partial", "unavailable")


def test_every_offered_source_comes_from_the_frozen_packet(tmp_path):
    """No item may offer or cite a source identity outside the packet."""
    import json

    from ptest import agent_assessment as AA

    packet = _packet_for(tmp_path, {
        "pyproject.toml": "[project]\nname = 'demo'\n",
        "tests/test_pure.py": "def test_pure():\n    assert True\n",
    })
    known = {e.path for e in packet.excerpts}
    reviews = AA.plan_item_reviews(packet)
    assert all(r.request is not None for r in reviews)
    for review in reviews:
        for path in review.excerpt_paths:
            assert path in known
        body = json.loads(review.request.decode("utf-8"))
        for offered in body["excerpts"]:
            assert offered["path"] in known


def test_absent_config_is_missing_reason_not_invented_source(tmp_path):
    """Absent .ptest.toml is a missing-context reason, never a citation."""
    import json

    from ptest import agent_assessment as AA

    packet = _packet_for(tmp_path, {
        "pyproject.toml": "[project]\nname = 'demo'\n",
        "tests/test_pure.py": "def test_pure():\n    assert True\n",
    })
    assert ".ptest.toml" not in [e.path for e in packet.excerpts]
    assert AA.runner_config_excerpt(packet) is None
    reviews = AA.plan_item_reviews(packet)
    assert all(r.request is not None for r in reviews)
    for review in reviews:
        assert ".ptest.toml" not in review.excerpt_paths
        body = json.loads(review.request.decode("utf-8"))
        offered = [e["path"] for e in body["excerpts"]]
        assert ".ptest.toml" not in offered
        missing = body["packet"]["missing"]
        assert missing, review.item_id
        assert body["packet"]["config_status"] in (
            "resolved", "partial", "unavailable")


def test_citation_to_unadmitted_config_is_rejected(tmp_path):
    """A reply citing absent .ptest.toml degrades to unknown."""
    import json

    from ptest import agent_assessment as AA

    packet = _packet_for(tmp_path, {
        "pyproject.toml": "[project]\nname = 'demo'\n",
        "tests/test_pure.py": "def test_pure():\n    assert True\n",
    })
    reviews = AA.plan_item_reviews(packet)
    target = next(r for r in reviews if r.request is not None)
    offered = {e["path"]: e for e in
               json.loads(target.request.decode("utf-8"))["excerpts"]}
    if ".ptest.toml" in offered:  # pre-fix invented source: cite its sha
        toml = offered[".ptest.toml"]
        forged = {"path": ".ptest.toml", "start_line": 1,
                  "end_line": toml["end_line"], "sha256": toml["sha256"]}
    else:
        forged = {"path": ".ptest.toml", "start_line": 1,
                  "end_line": 5, "sha256": "0" * 64}
    reply = json.dumps(
        {"status": "satisfied",
         "rationale": "Runner configuration proves reuse scope.",
         "evidence": [forged], "finding": None}).encode("utf-8")
    child = AA.assemble_child(
        packet, reviews,
        tuple(reply if r.item_id == target.item_id else None
              if r.request is None else _satisfied_reply(packet, r)
              for r in reviews))
    row = next(r for r in child.rows if r.id == target.item_id)
    assert row.status == "unknown"
    assert row.evidence == ()


def _satisfied_reply(packet, review):
    import json

    from ptest import agent_assessment as AA

    assert review.excerpt_paths, review.item_id
    try:
        first = next(e for e in packet.excerpts
                     if e.path == review.excerpt_paths[0])
    except StopIteration:  # pre-fix invented source: cite real evidence
        first = packet.excerpts[0]
    return json.dumps(
        {"status": "satisfied",
         "rationale": f"Row {review.item_id} holds on the cited lines.",
         "evidence": [{"path": first.path,
                       "start_line": first.start_line,
                       "end_line": first.end_line,
                       "sha256": first.sha256}],
         "finding": None}).encode("utf-8")


def test_saturated_vitest_packet_seeds_real_config_and_setup(tmp_path):
    """Saturated Vitest packet: real config+setup in every item, no e2e."""
    import json

    from ptest import agent_assessment as AA

    root = _copy_fixture(tmp_path, "vitest-saturated")
    (root / ".ptest.toml").write_text(
        _v1_config_text(CHILD_PID, "vitest").replace(
            'launcher = ["true"]', 'launcher = ["node"]'),
        encoding="utf-8")
    (root / "package.json").write_text(
        '{"name": "demo", "devDependencies": {"vitest": "^3.0.0"}}',
        encoding="utf-8")
    resolution = _resolution(
        root, _config(runner_kind=C.RunnerKind.VITEST,
                      launcher=("node",), test_roots=("src",)))
    workspace = doctor.inspect_workspace(
        _domain(root), resolution, C.DEFAULT_SCAN_LIMITS, None)
    packets = AA.build_packets(workspace, resolution, AA.EvidenceLimits())
    packet = packets[0]
    assert "vitest.config.ts" in [e.path for e in packet.excerpts]
    assert "src/setup.ts" in [e.path for e in packet.excerpts]
    assert not any("e2e" in e.path for e in packet.excerpts)
    reviews = AA.plan_item_reviews(packet)
    assert all(r.request is not None for r in reviews)
    by_id = {r.item_id: r for r in reviews}
    assert "vitest.config.ts" in by_id["DB-001"].excerpt_paths
    assert "src/setup.ts" in by_id["DB-001"].excerpt_paths
    for review in reviews:
        assert "vitest.config.ts" in review.excerpt_paths, review.item_id
        assert "src/setup.ts" in review.excerpt_paths, review.item_id
        body = json.loads(review.request.decode("utf-8"))
        offered = [e["path"] for e in body["excerpts"]]
        assert not any("e2e" in path for path in offered), review.item_id
        assert len(review.excerpt_paths) <= AA.ITEM_MAX_FILES


def test_setup_closure_outranks_generic_samples(tmp_path):
    from ptest import agent_assessment as AA

    root = _copy_fixture(tmp_path, "pytest-closure")
    (root / ".ptest.toml").write_text(
        _v1_config_text(CHILD_PID, "pytest"), encoding="utf-8")
    resolution = _resolution(root)
    workspace = doctor.inspect_workspace(
        _domain(root), resolution, C.DEFAULT_SCAN_LIMITS, None)
    tiny = AA.EvidenceLimits(max_files_per_child=4)
    packets = AA.build_packets(workspace, resolution, tiny)
    paths = [e.path for e in packets[0].excerpts]
    assert "tests/conftest.py" in paths
    assert "tests/helpers.py" in paths
    assert packets[0].context.config_status in (
        "resolved", "partial", "unavailable")


def test_excluded_e2e_is_separate_from_active_evidence(tmp_path):
    from ptest import agent_assessment as AA

    root = _copy_fixture(tmp_path, "vitest-single")
    (root / ".ptest.toml").write_text(
        _v1_config_text(CHILD_PID, "vitest").replace(
            'launcher = ["true"]', 'launcher = ["node"]'),
        encoding="utf-8")
    (root / "package.json").write_text(
        '{"name": "demo", "devDependencies": {"vitest": "^3.0.0"}}',
        encoding="utf-8")
    resolution = _resolution(
        root, _config(runner_kind=C.RunnerKind.VITEST,
                      launcher=("node",), test_roots=("src",)))
    workspace = doctor.inspect_workspace(
        _domain(root), resolution, C.DEFAULT_SCAN_LIMITS, None)
    packets = AA.build_packets(workspace, resolution, AA.EvidenceLimits())
    packet = packets[0]
    assert packet.context.runner_kind == "vitest"
    assert any("e2e" in path for path in packet.context.known_excluded)
    assert not any("e2e" in e.path for e in packet.excerpts
                   if "test" in e.path or "spec" in e.path)


def test_scoped_review_records_outside_selected_scope(tmp_path):
    from ptest import agent_assessment as AA

    root = tmp_path / "child"
    (root / "tests" / "unit").mkdir(parents=True)
    (root / "tests" / "unit" / "test_u.py").write_text(
        "def test_u():\n    assert True\n", encoding="utf-8")
    (root / "tests" / "conftest.py").write_text(
        "import pytest\n", encoding="utf-8")
    (root / ".ptest.toml").write_text(
        _v1_config_text(CHILD_PID, "pytest"), encoding="utf-8")
    resolution = _resolution(root)
    workspace = doctor.inspect_workspace(
        _domain(root), resolution, C.DEFAULT_SCAN_LIMITS, None)
    from dataclasses import replace as _replace

    workspace = _replace(
        workspace,
        repositories=(_replace(
            workspace.repositories[0], local_scope="tests/unit"),),
        scope=("tests/unit",))
    packets = AA.build_packets(workspace, resolution, AA.EvidenceLimits())
    packet = packets[0]
    assert packet.scope == "tests/unit"
    assert not any(e.path == "tests/conftest.py" for e in packet.excerpts)
    assert any(reason == "outside-selected-scope"
               for _, reason in packet.context.missing)


def test_context_reads_share_existing_candidate_budgets(tmp_path):
    from ptest import agent_assessment as AA
    from ptest import files as files_api

    reads: list[tuple[str, int]] = []
    real_read = files_api.read_regular

    def spy(root: Path, relative: str, limit: int):
        raw = real_read(root, relative, limit)
        reads.append((relative, len(raw)))
        return raw

    import ptest.agent_assessment as AAmod
    monkey = pytest.MonkeyPatch()
    monkey.setattr(AAmod, "read_regular", spy)
    try:
        root = _copy_fixture(tmp_path, "pytest-closure")
        (root / ".ptest.toml").write_text(
            _v1_config_text(CHILD_PID, "pytest"), encoding="utf-8")
        resolution = _resolution(root)
        workspace = doctor.inspect_workspace(
            _domain(root), resolution, C.DEFAULT_SCAN_LIMITS, None)
        limits = AA.EvidenceLimits()
        packets = AA.build_packets(workspace, resolution, limits)
    finally:
        monkey.undo()
    assert packets
    assert len(reads) <= limits.max_candidate_files_per_child
    assert sum(size for _, size in reads) <= limits.max_candidate_bytes_per_child


def test_context_reader_caps_read_to_remaining_candidate_bytes(tmp_path,
                                                               monkeypatch):
    from ptest import agent_assessment as AA
    from ptest import files as files_api

    root = tmp_path / "child"
    root.mkdir()
    (root / "vitest.config.ts").write_text(
        "export default { test: { include: ['tests/**/*.test.ts'] } }",
        encoding="utf-8")
    actual = files_api.read_regular
    limits_seen = []

    def spy(path, relative, limit):
        limits_seen.append(limit)
        return actual(path, relative, limit)

    monkeypatch.setattr(AA, "read_regular", spy)
    state = AA._AdmissionState()
    limits = AA.EvidenceLimits(max_candidate_files_per_child=8,
                               max_candidate_bytes_per_child=8)
    state.candidate_bytes_read = 7
    context = AA._collect_packet_context(
        child_root=root, scan_start="", runner_kind="vitest",
        runner_args=(), runner_full_args=(), test_roots=("tests",),
        regular=[("vitest.config.ts", 64)], state=state, limits=limits,
        max_candidate_read=64, cache={}, deadline=None, progress=None)
    assert limits_seen == [1]
    assert state.candidate_files_read == 1
    assert state.candidate_bytes_read == 8
    assert context.config_status == "partial"
    assert ("vitest.config.ts", "read-limit") in context.missing


def test_lock_probe_reserves_only_remaining_candidate_bytes(tmp_path,
                                                            monkeypatch):
    from ptest import agent_assessment as AA

    root = tmp_path / "child"
    root.mkdir()
    (root / "uv.lock").write_bytes(b"lock contents")
    limits = AA.EvidenceLimits(max_candidate_files_per_child=4,
                               max_candidate_bytes_per_child=8)
    state = AA._AdmissionState()
    state.candidate_bytes_read = 7
    observed = []

    def spy(path, relative, limit):
        observed.append(limit)
        return b"x"

    monkeypatch.setattr(AA, "read_regular", spy)
    fact = AA._probe_lock(root, "uv.lock", "uv.lock", state, limits)
    assert observed == [1]
    assert state.candidate_files_read == 1
    assert state.candidate_bytes_read == 8
    assert fact.status == "uninspectable"


def test_admission_reuses_charged_config_cache_at_exact_candidate_cap(
        tmp_path):
    from ptest import agent_assessment as AA

    raw = b"export default { test: { include: ['tests/**/*.test.ts'] } }"
    root = tmp_path / "child"
    root.mkdir()
    state = AA._AdmissionState()
    limits = AA.EvidenceLimits(max_files_per_child=2,
                               max_candidate_files_per_child=1,
                               max_candidate_bytes_per_child=len(raw))
    state.candidate_files_read = 1
    state.candidate_bytes_read = len(raw)
    admitted = AA._admit_candidate(
        state, root, "vitest.config.ts", "", limits,
        max_candidate_read=len(raw), remaining_after=0,
        cache={"vitest.config.ts": raw})
    assert admitted
    assert [excerpt.path for excerpt in state.excerpts] == [
        "vitest.config.ts"]
    assert state.candidate_files_read == 1
    assert state.candidate_bytes_read == len(raw)


def test_context_import_traversal_shares_private_path_exclusions(
        tmp_path, monkeypatch):
    from ptest import agent_assessment as AA
    from ptest import files as files_api

    root = tmp_path / "child"
    (root / "src").mkdir(parents=True)
    (root / ".codex").mkdir()
    (root / ".pipeline").mkdir()
    (root / "node_modules" / "pkg").mkdir(parents=True)
    (root / "vitest.config.ts").write_text(
        "import './secrets.json';\n"
        "export default { test: { setupFiles: ['./src/setup.ts'] } };\n",
        encoding="utf-8")
    (root / "src" / "setup.ts").write_text(
        "import '../.codex/helper.ts';\n"
        "import '../.pipeline/agent.ts';\n"
        "import '../node_modules/pkg/private.ts';\n",
        encoding="utf-8")
    (root / "secrets.json").write_text('{"token":"x"}', encoding="utf-8")
    for relative in (".codex/helper.ts", ".pipeline/agent.ts",
                     "node_modules/pkg/private.ts"):
        (root / relative).write_text("export const x = 1;\n",
                                     encoding="utf-8")

    real = files_api.read_regular
    reads = []

    def spy(path, relative, limit):
        reads.append(relative)
        return real(path, relative, limit)

    monkeypatch.setattr(AA, "read_regular", spy)
    packet = _build_vitest_packet(root)
    forbidden = ("secrets.json", ".codex", ".pipeline", "node_modules")
    assert not any(any(part in path for part in forbidden) for path in reads)
    context = packet.context
    inventory = repr((context.config_paths, context.active_roots,
                      context.roles, context.relations,
                      context.known_excluded, context.missing))
    assert not any(part in inventory for part in forbidden)
    request_metadata = json.loads(
        AA.plan_item_reviews(packet)[0].request.decode("utf-8"))["packet"]
    assert not any(part in repr(request_metadata) for part in forbidden)


def test_pytest_conftest_import_and_plugin_closure_is_seeded_first(tmp_path):
    from ptest import review_context as RC
    from ptest.files import read_regular

    root = tmp_path / "child"
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "conftest.py").write_text(
        "import tests.helper\npytest_plugins = ['tests.plugin']\n",
        encoding="utf-8")
    (root / "tests" / "helper.py").write_text(
        "import tests.deep\n", encoding="utf-8")
    (root / "tests" / "deep.py").write_text(
        "VALUE = 1\n", encoding="utf-8")
    (root / "tests" / "plugin.py").write_text(
        "@pytest.fixture\ndef shared():\n    return 1\n",
        encoding="utf-8")
    (root / "tests" / "test_case.py").write_text(
        "def test_case(shared):\n    assert shared == 1\n",
        encoding="utf-8")
    reads = []

    def reader(relative, limit):
        reads.append(relative)
        return read_regular(root, relative, limit)

    context = RC.collect_pytest_context(
        root, "", reader, {"tests/conftest.py"},
        ("tests/test_case.py",), ("tests",))
    roles = dict(context.roles)
    assert roles["tests/conftest.py"] == "fixture"
    assert roles["tests/helper.py"] == "fixture"
    assert roles["tests/deep.py"] == "fixture"
    assert roles["tests/plugin.py"] == "fixture"
    assert ("tests/conftest.py", "tests/helper.py", "local-import") \
        in context.relations
    assert reads.index("tests/conftest.py") < reads.index("tests/helper.py")
    assert reads.index("tests/helper.py") < reads.index("tests/test_case.py")


def test_pytest_fixture_owners_are_nearest_applicable_ancestors(tmp_path):
    from ptest import review_context as RC
    from ptest.files import read_regular

    root = tmp_path / "child"
    sources = {
        "tests/conftest.py": (
            "@pytest.fixture\ndef db():\n    return 'root'\n"),
        "tests/unit/conftest.py": (
            "@pytest.fixture\ndef db():\n    return 'unit'\n"),
        "tests/integration/conftest.py": (
            "@pytest.fixture\ndef sibling_only():\n    return 'integration'\n"),
        "tests/unit/test_unit.py": (
            "def test_db(db):\n    assert db\n"
            "def test_sibling(sibling_only):\n    assert sibling_only\n"),
        "tests/integration/test_integration.py": (
            "def test_sibling(sibling_only):\n    assert sibling_only\n"),
    }
    for relative, text in sources.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def reader(relative, limit):
        return read_regular(root, relative, limit)

    context = RC.collect_pytest_context(
        root, "", reader,
        {"tests/conftest.py", "tests/unit/conftest.py",
         "tests/integration/conftest.py"},
        ("tests/unit/test_unit.py", "tests/integration/test_integration.py"),
        ("tests",))
    relations = set(context.relations)
    assert ("tests/unit/test_unit.py", "tests/unit/conftest.py",
            "fixture-use") in relations
    assert ("tests/unit/test_unit.py", "tests/integration/conftest.py",
            "fixture-use") not in relations
    assert ("tests/integration/test_integration.py",
            "tests/integration/conftest.py", "fixture-use") in relations


def test_pytest_fixture_use_fanout_is_capped_and_reported(
        tmp_path, monkeypatch):
    from ptest import review_context as RC
    from ptest.files import read_regular

    root = tmp_path / "child"
    (root / "tests").mkdir(parents=True)
    fixture_names = [f"missing_resource_{index:02d}"
                     for index in range(24)]
    test = "def test_many(" + ", ".join(fixture_names) + "):\n    pass\n"
    (root / "tests" / "test_many.py").write_text(test, encoding="utf-8")
    candidates = set()
    for index in range(24):
        relative = f"tests/group_{index:02d}/conftest.py"
        target = root / relative
        target.parent.mkdir(parents=True)
        target.write_text("# sibling conftest\n", encoding="utf-8")
        candidates.add(relative)

    lookups = [0]
    find_owner = RC._find_fixture_owner

    def count_lookup(*args, **kwargs):
        lookups[0] += 1
        return find_owner(*args, **kwargs)

    monkeypatch.setattr(RC, "_find_fixture_owner", count_lookup)

    def reader(relative, limit):
        return read_regular(root, relative, limit)

    context = RC.collect_pytest_context(
        root, "", reader, candidates,
        ("tests/test_many.py",), ("tests",))
    assert lookups[0] <= RC._CONTEXT_MAX_LINKS_PER_FILE
    assert ("tests/test_many.py", "item-limit") in context.missing


def test_pytest_exclusions_use_only_effective_config_addopts(tmp_path):
    from ptest import review_context as RC
    from ptest.files import read_regular

    root = tmp_path / "child"
    (root / "tests").mkdir(parents=True)
    (root / "pytest.ini").write_text(
        "[pytest]\n"
        "addopts = --maxfail=1\n"
        "# --ignore=tests/from-comment.py\n"
        "[unrelated]\n"
        "addopts = --ignore=tests/from-other-section.py\n",
        encoding="utf-8")
    (root / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\n"
        "addopts = '--ignore=tests/from-inactive-config.py'\n",
        encoding="utf-8")
    (root / "tests" / "test_live.py").write_text(
        "def test_live():\n    assert True\n", encoding="utf-8")

    def reader(relative, limit):
        return read_regular(root, relative, limit)

    context = RC.collect_pytest_context(
        root, "", reader, {"pytest.ini", "pyproject.toml"},
        ("tests/test_live.py",), ("tests",))
    assert context.config_paths == ("pytest.ini",)
    assert context.known_excluded == ()
    assert context.config_status == "resolved"

    (root / "pytest.ini").unlink()
    (root / "pyproject.toml").write_text(
        "[project]\nname = 'sample'\n", encoding="utf-8")
    (root / "tox.ini").write_text(
        "[pytest]\naddopts = --ignore=tests/from-tox.py\n",
        encoding="utf-8")
    context = RC.collect_pytest_context(
        root, "", reader, {"pyproject.toml", "tox.ini"},
        ("tests/test_live.py",), ("tests",))
    assert context.config_paths == ("tox.ini",)
    assert context.known_excluded == ("tests/from-tox.py",)


def test_saturated_missing_pytest_imports_do_not_empty_the_packet(
        tmp_path, monkeypatch):
    from ptest import agent_assessment as AA
    from ptest import files as files_api

    root = _copy_fixture(tmp_path, "pytest-closure")
    (root / ".ptest.toml").write_text(
        _v1_config_text(CHILD_PID, "pytest"), encoding="utf-8")
    imports = "\n".join(f"import missing_pkg_{i}.helper" for i in range(16))
    (root / "tests" / "conftest.py").write_text(imports + "\n",
                                                encoding="utf-8")
    real = files_api.read_regular
    reads = []

    def spy(path, relative, limit):
        reads.append(relative)
        return real(path, relative, limit)

    monkeypatch.setattr(AA, "read_regular", spy)
    resolution = _resolution(root)
    workspace = doctor.inspect_workspace(
        _domain(root), resolution, C.DEFAULT_SCAN_LIMITS, None)
    packet = AA.build_packets(workspace, resolution, AA.EvidenceLimits())[0]
    assert packet.excerpts
    assert ".ptest.toml" in [excerpt.path for excerpt in packet.excerpts]
    assert "tests/conftest.py" in [excerpt.path for excerpt in packet.excerpts]
    assert not any(path.startswith("missing_pkg_")
                   or "/missing_pkg_" in path for path in reads)


def _build_vitest_packet(root):
    from ptest import agent_assessment as AA
    from ptest import contracts as C
    from ptest import doctor

    (root / ".ptest.toml").write_text(
        _v1_config_text(CHILD_PID, "vitest").replace(
            'launcher = ["true"]', 'launcher = ["node"]'),
        encoding="utf-8")
    (root / "package.json").write_text(
        '{"name":"fixture","devDependencies":{"vitest":"^3"}}',
        encoding="utf-8")
    resolution = _resolution(
        root, _config(runner_kind=C.RunnerKind.VITEST,
                      launcher=("node",), test_roots=("src",)))
    workspace = doctor.inspect_workspace(
        _domain(root), resolution, C.DEFAULT_SCAN_LIMITS, None)
    return AA.build_packets(workspace, resolution, AA.EvidenceLimits())[0]
