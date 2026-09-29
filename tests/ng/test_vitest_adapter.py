"""Vitest command preparation contracts.

``kind = "vitest"`` executes through the project-local Vitest CLI: bounded by
environment caps when ptest can read the install and worker settings, else as
one literal exclusive command. Scope arrives through the effective runner args
(the caller scope is already appended there); ``plan.files`` must stay empty.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from ptest import contracts as C
from ptest.adapters import vitest as vitest_adapter
from ptest.runners import adapter_for

RUN_ID = "12" * 16
NONCE = "34" * 32


def _grant(slots: int = 1) -> C.Grant:
    return C.Grant(run_id=RUN_ID, nonce=NONCE, slots=slots, memory_estimate_mb=None,
                   reserved_memory_mb=None, generation=0, domain_id="56" * 16)


def _attempt(workers: int = 1) -> C.AttemptIdentity:
    return C.AttemptIdentity(run_id=RUN_ID, attempt_id="a001",
                             resource_prefix="pt_checkout_run_a001_w000", worker_count=workers)


def _config(case, **overrides):
    entry = case.base / "node_modules" / "vitest" / "vitest.mjs"
    entry.parent.mkdir(parents=True, exist_ok=True)
    entry.write_text("// installed\n", encoding="utf-8")
    config = case.config(runner_kind="vitest", config_path=case.base / "ptest.toml")
    runner = replace(config.runner, launcher=("node",))
    if overrides:
        runner = replace(runner, **overrides)
    return replace(config, runner=runner)


def test_scoped_builds_literal_exclusive_argv_from_effective_args(case):
    config = _config(case, args=("src/a.test.ts",))
    plan = C.Plan(mode=C.Mode.SCOPED, execution="scoped", files=())

    prepared = vitest_adapter.prepare(config, plan, _grant(), _attempt())

    assert prepared.argv == ("node", vitest_adapter.VITEST_ENTRY, "run", "src/a.test.ts")
    assert prepared.cwd == case.base
    assert prepared.env_updates == ()
    assert prepared.capability.execution is C.ExecutionTier.EXCLUSIVE_COMMAND
    assert prepared.capability.selection is False
    assert [reason.code for reason in prepared.capability.limitations] == ["unsupported-capability"]
    assert prepared.capability.limitations[0].message == vitest_adapter.VITEST_EXCLUSIVE_NOTE


def test_full_appends_full_args_after_runner_args(case):
    config = _config(case, args=("--reporter", "verbose"), full_args=("--coverage",))
    plan = C.Plan(mode=C.Mode.FULL, execution="full", files=())

    prepared = vitest_adapter.prepare(config, plan, _grant(), _attempt())

    assert prepared.argv == ("node", vitest_adapter.VITEST_ENTRY, "run",
                             "--reporter", "verbose", "--coverage")


def test_absolute_node_launcher_is_accepted(case):
    config = _config(case)
    config = replace(config, runner=replace(config.runner, launcher=("/usr/bin/node",)))
    plan = C.Plan(mode=C.Mode.SCOPED, execution="scoped", files=())

    prepared = vitest_adapter.prepare(config, plan, _grant(), _attempt())

    assert prepared.argv[:3] == ("/usr/bin/node", vitest_adapter.VITEST_ENTRY, "run")


def test_selected_execution_is_rejected(case):
    config = _config(case)
    plan = C.Plan(mode=C.Mode.AUTOMATIC, execution="selected", files=("tests/a.test.ts",))

    with pytest.raises(C.Problem, match="unsupported-capability"):
        vitest_adapter.prepare(config, plan, _grant(), _attempt())


def test_nonempty_plan_files_are_rejected(case):
    config = _config(case)
    plan = C.Plan(mode=C.Mode.SCOPED, execution="scoped", files=("tests/a.test.ts",))

    with pytest.raises(C.Problem, match="plan.files"):
        vitest_adapter.prepare(config, plan, _grant(), _attempt())


# Payload only: rejected launcher values, never executed.
@pytest.mark.parametrize("launcher", [("npm",), ("npx", "vitest"), ("node", "--inspect"),
                                      ("/tmp/not-node",), ("node", "node")])
def test_non_node_launcher_is_rejected(case, launcher):
    config = case.config(runner_kind="vitest", config_path=case.base / "ptest.toml")
    config = replace(config, runner=replace(config.runner, launcher=launcher))
    plan = C.Plan(mode=C.Mode.SCOPED, execution="scoped", files=())

    with pytest.raises(C.Problem, match="Node"):
        vitest_adapter.prepare(config, plan, _grant(), _attempt())


def test_non_vitest_config_is_rejected(case):
    config = case.config(runner_kind="pytest", config_path=case.base / "ptest.toml")
    plan = C.Plan(mode=C.Mode.SCOPED, execution="scoped", files=())

    with pytest.raises(C.Problem, match="vitest"):
        vitest_adapter.prepare(config, plan, _grant(), _attempt())


def test_registry_marks_vitest_exclusive_and_automatic_full(case):
    adapter = adapter_for(C.RunnerKind.VITEST)

    assert adapter.requires_exclusive(_config(case)) is True
    assert adapter.mode_for_automatic() == "full"


def test_registry_prepare_advanced_raises_unsupported_capability(case):
    adapter = adapter_for(C.RunnerKind.VITEST)
    config = _config(case)
    plan = C.Plan(mode=C.Mode.AUTOMATIC, execution="selected", files=("tests/a.test.ts",))
    grant = C.Grant(run_id=RUN_ID, nonce=NONCE, slots=1,
                    memory_estimate_mb=None, reserved_memory_mb=None,
                    generation=1, domain_id="01" * 16)
    attempt = C.AttemptIdentity(run_id=grant.run_id, attempt_id="a001",
                                resource_prefix="ptest_a001_w000", worker_count=1)

    with pytest.raises(C.Problem, match="unsupported-capability"):
        adapter.prepare_advanced(config, plan, grant, attempt)


def test_vitest_entry_literal_is_stable():
    assert vitest_adapter.VITEST_ENTRY == "node_modules/vitest/vitest.mjs"
    assert Path(vitest_adapter.VITEST_ENTRY).name == "vitest.mjs"


# --- worker caps: vitest stops taking the whole machine ---------------------
#
# Measured on real installs (3.1.4, 3.2.6, 4.0.18, 4.1.11, 5.0.1): the
# VITEST_MAX_WORKERS/_THREADS/_FORKS environment (with MIN_THREADS/FORKS=1)
# caps every pool, including Vitest 4.x projects where --maxWorkers is
# ignored, and needs no version-specific flags.

def _versioned(case, version, config_text=None, **overrides):
    config = _config(case, **overrides)
    (case.base / "node_modules" / "vitest" / "package.json").write_text(
        '{"name": "vitest", "version": "%s"}' % version, encoding="utf-8")
    if config_text is not None:
        (case.base / "vitest.config.ts").write_text(config_text, encoding="utf-8")
    return config


def _env(n):
    return (("VITEST_MAX_WORKERS", str(n)), ("VITEST_MAX_THREADS", str(n)),
            ("VITEST_MAX_FORKS", str(n)), ("VITEST_MIN_THREADS", "1"),
            ("VITEST_MIN_FORKS", "1"))


def _prepare(config, slots):
    plan = C.Plan(mode=C.Mode.SCOPED, execution="scoped", files=())
    return vitest_adapter.prepare(config, plan, _grant(slots), _attempt())


@pytest.mark.parametrize("version", ["3.1.4", "3.2.6", "4.1.11", "5.0.1"])
def test_known_vitest_is_bounded_through_its_environment(case, version):
    config = _versioned(case, version, args=("src/a.test.ts",))

    prepared = _prepare(config, 3)

    assert prepared.argv == ("node", vitest_adapter.VITEST_ENTRY, "run", "src/a.test.ts")
    assert prepared.env_updates == _env(3)
    assert prepared.capability.execution is C.ExecutionTier.BOUNDED_NATIVE
    assert adapter_for(C.RunnerKind.VITEST).requires_exclusive(config) is False


@pytest.mark.parametrize("text,limit", [
    ("export default { test: { maxWorkers: 1 } }", 1),
    ("export default { test: { poolOptions: { forks: { maxForks: 2 } } } }", 2),
    ("export default { test: { poolOptions: { threads: { minThreads: 4, maxThreads: 8 } } } }", 8),
    ("export default { test: { fileParallelism: false } }", 1),
    ("export default { test: { poolOptions: { forks: { singleFork: true } } } }", 1),
    ("export default { test: { fileParallelism: true, globals: true } }", None),
])
def test_a_projects_own_literal_ceiling_is_kept_never_raised(case, text, limit):
    config = _versioned(case, "3.2.6", text)

    assert vitest_adapter.bound(config) == vitest_adapter.VitestBound(limit=limit)
    expected = 4 if limit is None else min(4, limit)
    assert _prepare(config, 4).env_updates == _env(expected)


@pytest.mark.parametrize("text", [
    "export default { test: { maxWorkers: process.env.CI ? 2 : 8 } }",
    "export default { test: { maxWorkers: '50%' } }",
    "export default { test: { poolOptions: { threads: { singleThread: isCi } } } }",
    "export default { test: { projects: ['packages/*'] } }",
])
def test_unreadable_worker_settings_keep_vitest_3_exclusive(case, text):
    config = _versioned(case, "3.2.6", text)

    prepared = _prepare(config, 4)

    assert prepared.env_updates == ()
    assert prepared.capability.execution is C.ExecutionTier.EXCLUSIVE_COMMAND
    assert adapter_for(C.RunnerKind.VITEST).requires_exclusive(config) is True


@pytest.fixture(autouse=True)
def _no_inherited_vitest_env(monkeypatch):
    for name in ("VITEST_MAX_WORKERS", "VITEST_MAX_THREADS", "VITEST_MAX_FORKS",
                 "VITEST_MIN_THREADS", "VITEST_MIN_FORKS"):
        monkeypatch.delenv(name, raising=False)


# Each of these hides a serial setting where the literal root text cannot
# show it; the environment would raise it, so ptest stays exclusive.
@pytest.mark.parametrize("version", ["3.2.6", "5.0.1"])
@pytest.mark.parametrize("text", [
    "import shared from './vitest.shared.mjs'\nexport default mergeConfig(shared, {})",
    "export default { test: { projects: ['packages/*'] } }",
    "export default { test: { workspace: ['a'] } }",
    "export default { test: { extends: './base.config.mjs' } }",
    "export default { test: { browser: { enabled: true } } }",
    "import base from '@acme/vitest-config'\nexport default base",
    "export default { test: { \"maxWorkers\": 1 } }",
    "const maxWorkers = 1\nexport default { test: { maxWorkers } }",
])
def test_worker_settings_ptest_cannot_see_keep_vitest_exclusive(case, version, text):
    config = _versioned(case, version, text)
    assert vitest_adapter.bound(config) is None


def test_vite_config_limits_count_alongside_vitest_config(case):
    config = _versioned(case, "3.2.6", "export default { test: { globals: true } }")
    (case.base / "vite.config.mjs").write_text(
        "export default { test: { poolOptions: { forks: { maxForks: 1 } } } }",
        encoding="utf-8")
    assert vitest_adapter.bound(config) == vitest_adapter.VitestBound(limit=1)


@pytest.mark.parametrize("version,expected", [("3.2.6", None), ("4.1.11", None), ("5.0.1", "bounded")])
def test_a_parent_directory_config_keeps_vitest_below_5_exclusive(case, version, expected):
    config = _versioned(case, version)
    (case.base.parent / "vitest.config.mjs").write_text(
        "export default { test: { maxWorkers: 1 } }", encoding="utf-8")
    try:
        result = vitest_adapter.bound(config)
    finally:
        (case.base.parent / "vitest.config.mjs").unlink()
    assert (result is None) if expected is None else (result == vitest_adapter.VitestBound(limit=None))


def test_comments_and_urls_are_not_settings(case):
    config = _versioned(case, "5.0.1", (
        'import { defineConfig } from "vitest/config";\n'
        'import react from "@vitejs/plugin-react";\n'
        "// Pass --maxWorkers on local runs to go faster.\n"
        "/* singleFork: true was too slow */\n"
        "export default defineConfig({ plugins: [react()], test: {\n"
        "  pool: 'forks', env: { API: 'http://localhost:1' },\n"
        "  exclude: [...configDefaults.exclude, 'e2e/**'] } })\n"))
    assert vitest_adapter.bound(config) == vitest_adapter.VitestBound(limit=None)


def test_an_inherited_serial_environment_is_kept(case, monkeypatch):
    config = _versioned(case, "5.0.1")
    monkeypatch.setenv("VITEST_MAX_WORKERS", "1")
    assert vitest_adapter.bound(config) == vitest_adapter.VitestBound(limit=1)
    monkeypatch.setenv("VITEST_MAX_WORKERS", "50%")
    assert vitest_adapter.bound(config) is None


def test_a_vitest_3_workspace_file_keeps_it_exclusive(case):
    config = _versioned(case, "3.2.6")
    (case.base / "vitest.workspace.ts").write_text("export default ['a']", encoding="utf-8")
    assert vitest_adapter.bound(config) is None


@pytest.mark.parametrize("version", ["2.1.9", "not-a-version", "", "3"])
def test_unknown_or_unsupported_vitest_versions_stay_exclusive(case, version):
    config = _versioned(case, version)

    prepared = _prepare(config, 4)

    assert prepared.env_updates == ()
    assert prepared.capability.execution is C.ExecutionTier.EXCLUSIVE_COMMAND
    assert adapter_for(C.RunnerKind.VITEST).requires_exclusive(config) is True


def test_missing_vitest_package_json_stays_exclusive(case):
    config = _config(case)
    assert adapter_for(C.RunnerKind.VITEST).requires_exclusive(config) is True


def test_deeply_nested_package_json_stays_exclusive(case):
    config = _config(case)
    (case.base / "node_modules" / "vitest" / "package.json").write_text(
        "[" * 60000, encoding="utf-8")
    assert adapter_for(C.RunnerKind.VITEST).requires_exclusive(config) is True


@pytest.mark.parametrize("args", [
    ("--maxWorkers=2",), ("--maxWorkers", "2"), ("--minWorkers=1",),
    ("--pool=forks",), ("--pool", "threads"), ("--poolOptions.threads.maxThreads=2",),
    ("--no-file-parallelism",), ("--fileParallelism=false",),
    ("--config", "other.config.ts"), ("-c", "other.config.ts"), ("-cother.config.ts",),
    ("--project", "api"), ("--root", "sub"), ("-r", "sub"), ("--browser",),
    ("--browser.enabled",),
])
def test_project_owned_worker_controls_keep_vitest_exclusive(case, args):
    config = _versioned(case, "3.2.6", args=args)

    prepared = _prepare(config, 4)

    assert prepared.argv == ("node", vitest_adapter.VITEST_ENTRY, "run", *args)
    assert prepared.env_updates == ()
    assert adapter_for(C.RunnerKind.VITEST).requires_exclusive(config) is True


def test_symlinked_vitest_package_json_stays_exclusive(case):
    config = _config(case)
    real = case.base / "elsewhere.json"
    real.write_text('{"version": "3.2.6"}', encoding="utf-8")
    (case.base / "node_modules" / "vitest" / "package.json").symlink_to(real)
    assert adapter_for(C.RunnerKind.VITEST).requires_exclusive(config) is True


def test_prepare_follows_the_admission_decision(case):
    # Admission decided once; prepare must not re-decide from a changed tree.
    config = _versioned(case, "3.2.6")
    token = vitest_adapter.DECISION.set(None)
    try:
        prepared = _prepare(config, 4)
    finally:
        vitest_adapter.DECISION.reset(token)
    assert prepared.env_updates == ()
    assert prepared.capability.execution is C.ExecutionTier.EXCLUSIVE_COMMAND


def test_comment_markers_inside_glob_strings_never_hide_settings(case):
    # '/*' in 'tests/*.test.js' and '*/' in '**/node_modules/**' are string
    # content; pairing them as a comment used to delete maxWorkers: 1.
    config = _versioned(case, "5.0.1", (
        "export default { test: {\n"
        "  include: ['tests/*.test.js'],\n"
        "  maxWorkers: 1,\n"
        "  exclude: ['**/node_modules/**'],\n"
        "} }\n"))
    assert vitest_adapter.bound(config) == vitest_adapter.VitestBound(limit=1)


@pytest.mark.parametrize("files,text", [
    ({"shared.mjs": "export default { test: { maxWorkers: 1 } }"},
     "import s from './shared'\nexport default { test: { ...s.test } }"),
    ({}, "import base from '../base'\nexport default { test: base.test }"),
    ({}, "import shared from '@acme/shared'\nexport default defineConfig(shared)"),
    ({}, "import preset from '@acme/vitest-preset'\nexport default { test: { ...preset.test } }"),
    ({}, "import s from './missing'\nexport default { test: { ...s.test } }"),
    ({}, "export default { test: { include: ['unterminated] } }"),
])
def test_settings_behind_imports_or_unreadable_text_keep_vitest_exclusive(case, files, text):
    config = _versioned(case, "5.0.1", text)
    for name, content in files.items():
        (case.base / name).write_text(content, encoding="utf-8")
    assert vitest_adapter.bound(config) is None


def test_a_harmless_relative_import_stays_bounded(case):
    config = _versioned(case, "3.2.6", (
        "import { defineConfig, configDefaults } from 'vitest/config';\n"
        "import react from '@vitejs/plugin-react';\n"
        "import { COVERAGE_THRESHOLDS } from './src/test/coverage-thresholds';\n"
        "export default defineConfig({ plugins: [react()], test: {\n"
        "  fileParallelism: true, pool: 'threads',\n"
        "  poolOptions: { threads: { minThreads: 4, maxThreads: 8 } },\n"
        "  exclude: [...configDefaults.exclude, 'e2e/**'],\n"
        "  coverage: { thresholds: COVERAGE_THRESHOLDS } } })\n"))
    target = case.base / "src" / "test"
    target.mkdir(parents=True)
    (target / "coverage-thresholds.ts").write_text(
        "export const COVERAGE_THRESHOLDS = { lines: 80 }\n", encoding="utf-8")
    assert vitest_adapter.bound(config) == vitest_adapter.VitestBound(limit=8)
