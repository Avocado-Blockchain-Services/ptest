"""Canonical doctor readiness-assessment checklist catalog.

This module owns the single ordered worksheet catalog consumed by renderers
and guide content. No renderer keeps a second list. Every row starts as
``unknown``; ptest static patterns never fill or upgrade a row.

Each entry carries a short human ``label``, an item-specific ``prompt``
(question, what counts as evidence, when N/A applies; absence stays
unknown), per-item evidence routing (``path_patterns`` matched against
excerpt paths, ``text_patterns`` matched against excerpt text, and
``scanner_codes`` seeding routing from static scanner hits), and a
deterministic ``skip`` rule (``no-database`` | ``no-cache`` | None).
"""
from __future__ import annotations

import importlib.resources
from dataclasses import dataclass, replace

# NOTE: ptest.contracts derives its checklist constants from CATALOG below,
# so this module must not import contracts at top level (import cycle).
# load_recipe() imports it lazily instead.

_RECIPE_MAX_BYTES = 65536
_RECIPE_FILES = {
    "factories": "recipes/factories.md",
    "databases": "recipes/databases.md",
    "cache": "recipes/cache.md",
    "files-ports": "recipes/files-ports.md",
    "processes": "recipes/processes.md",
    "time-network": "recipes/time-network.md",
}

# Shared routing fragments (regexes searched against excerpt paths).
TEST_DIR = r"(?i)(?:^|/)(?:tests?|__tests__)(?:/|$)"
TEST_FILE = r"(?i)(?:^|/)test_[^/]*\.py$|[^/]*_test\.py$|\.test\.|\.spec\."
SRC_DIR = r"(?:^|/)src(?:/|$)"
_CONFTEST = r"(?:^|/)conftest\.py$"

# Evidence standard shared by every item prompt: a gap needs a cited
# concrete violation, satisfied needs the guaranteeing mechanism, and
# anything less is unknown with the missing evidence named.
_PROMPT_STANDARD = (
    " Return gap only with a cited concrete violation; return satisfied "
    "only when the evidence shows the guaranteeing mechanism; otherwise "
    "return unknown with one sentence naming the missing evidence. "
    "Absence of code is unknown, never a guess."
)
_MANIFEST = (r"(?i)(?:^|/)(?:pyproject\.toml|package\.json|requirements"
             r"(?:[^/]*)?\.txt|uv\.lock|poetry\.lock|pdm\.lock|Cargo\.toml"
             r"|go\.mod|setup\.py|setup\.cfg|pytest\.ini|tox\.ini)$")
_PTEST_TOML = r"(?:^|/)\.ptest\.toml$"


@dataclass(frozen=True, slots=True)
class ChecklistEntry:
    id: str
    label: str
    criterion: str
    evidence: str
    recommendation: str
    example: str
    verification: str
    recipe: str | None
    prompt: str
    path_patterns: tuple[str, ...]
    text_patterns: tuple[str, ...]
    scanner_codes: tuple[str, ...]
    skip: str | None


CATALOG: tuple[ChecklistEntry, ...] = (
    ChecklistEntry(
        id="FIX-001",
        label="Test data factories",
        criterion="Fixtures/factories create fresh test records without weakening assertions.",
        evidence="Cite fixture/factory definitions and callers.",
        recommendation="Prefer a small factory when records vary; pure tests need none.",
        example="Reuse recipes/factories.md.",
        verification="Scoped ptest proves original assertions and inventory remain.",
        recipe="factories",
        prompt=("Do fixtures or factories create fresh test records without "
                "weakening assertions? Cite fixture or factory definitions "
                "and their callers showing fresh records and specific "
                "assertions. Mark N/A only with evidence that no test in "
                "scope uses records at all. Absence of such evidence is "
                "unknown, never N/A."),
        path_patterns=(_CONFTEST,
                       r"(?i)(?:^|/)[^/]*(?:fixture|factor)[^/]*$",
                       TEST_DIR, TEST_FILE, SRC_DIR),
        text_patterns=(r"@pytest\.fixture", r"(?i)factor", r"def test_",
                       r"(?i)fixture"),
        scanner_codes=("fixture.shared-mutation",),
        skip=None,
    ),
    ChecklistEntry(
        id="FIX-002",
        label="Fixture state isolation",
        criterion="Mutable fixture state is isolated or reset for every test.",
        evidence="Cite fixture lifetime, mutation, and reset boundaries.",
        recommendation="Replace shared mutable state or prove deterministic reset.",
        example="Reuse recipes/factories.md.",
        verification="Run scoped order/worker permutations through ptest.",
        recipe="factories",
        prompt=("Is mutable fixture state isolated or reset for every test? "
                "Cite fixture lifetimes, mutation points, and reset "
                "boundaries. Mark N/A only with evidence that no shared "
                "mutable fixture state exists in scope. Absence of such "
                "evidence is unknown, never N/A."),
        path_patterns=(_CONFTEST,
                       r"(?i)(?:^|/)[^/]*(?:fixture|factor)[^/]*$",
                       TEST_DIR, TEST_FILE, SRC_DIR),
        text_patterns=(r"@pytest\.fixture",
                       r"scope\s*=\s*[\"'](?:session|module|package|class)[\"']",
                       r"(?i)fixture", r"(?i)shared|mutable|global"),
        scanner_codes=("fixture.shared-mutation",),
        skip=None,
    ),
    ChecklistEntry(
        id="DB-001",
        label="Database setup reuse",
        criterion="Expensive database/server/schema setup is reused per run or worker, not repeated per test.",
        evidence="Cite setup scope and cost.",
        recommendation="Prefer one owned database/schema template per run/worker; no ORM is mandated.",
        example="Reuse recipes/databases.md.",
        verification="Measured scoped ptest run shows setup reuse without semantic loss.",
        recipe="databases",
        prompt=("Is expensive database, server, or schema setup reused per "
                "run or worker instead of per test? Cite setup scope and "
                "cost evidence. Mark N/A only with evidence that no "
                "database, server, or schema setup exists in the admitted "
                "evidence. Absence of such evidence is unknown, never N/A."),
        path_patterns=(_CONFTEST, TEST_DIR, TEST_FILE, SRC_DIR, _MANIFEST,
                       _PTEST_TOML,
                       r"(?i)(?:^|/)[^/]*(?:migration|models?|database|db)[^/]*$"),
        text_patterns=(r"(?i)sqlalchemy|sqlmodel|django|alembic|psycopg|asyncpg"
                       r"|aiosqlite|sqlite3?|peewee|tortoise|prisma|sequelize"
                       r"|typeorm|knex|mongoose|pymongo|DATABASE_URL"
                       r"|database_url|create_all|drop_database|drop_all"
                       r"|truncate|connect\s*\(|Column\s*\(|sessionmaker"
                       r"|create_engine",),
        scanner_codes=("db.per-test-initialization", "db.cleanup-ownership"),
        skip="no-database",
    ),
    ChecklistEntry(
        id="DB-002",
        label="Database isolation",
        criterion="Database identities, records, and cleanup have explicit run/worker ownership.",
        evidence="Cite name derivation and teardown.",
        recommendation="Remove only owned records/namespaces; never infer ownership from a test-like name.",
        example="Reuse recipes/databases.md.",
        verification="Neighbor database/schema sentinel survives concurrent teardown.",
        recipe="databases",
        prompt=("Do database identities, records, and cleanup have explicit "
                "run or worker ownership? Cite name derivation and teardown "
                "boundaries. Mark N/A only with evidence that no database "
                "identities, records, or namespaces exist in the admitted "
                "evidence. Absence of such evidence is unknown, never N/A."),
        path_patterns=(_CONFTEST, TEST_DIR, TEST_FILE, SRC_DIR, _MANIFEST,
                       _PTEST_TOML,
                       r"(?i)(?:^|/)[^/]*(?:migration|models?|database|db)[^/]*$"),
        text_patterns=(r"(?i)sqlalchemy|sqlmodel|django|alembic|psycopg|asyncpg"
                       r"|aiosqlite|sqlite3?|peewee|tortoise|prisma|sequelize"
                       r"|typeorm|knex|mongoose|pymongo|DATABASE_URL"
                       r"|database_url|create_all|drop_database|drop_all"
                       r"|truncate|connect\s*\(|Column\s*\(|sessionmaker"
                       r"|create_engine",),
        scanner_codes=("db.per-test-initialization", "db.cleanup-ownership"),
        skip="no-database",
    ),
    ChecklistEntry(
        id="CACHE-001",
        label="Cache isolation",
        criterion="Redis, Valkey, and other mutable caches are namespaced and cleaned by owner.",
        evidence="Cite key prefix and deletion path.",
        recommendation=(
            "Prefer checkout/run/worker prefixes and owned-key deletion; verify any claimed "
            "disposable-service exclusivity before judging a global-flush hypothesis. "
            "Never recommend blanket flush as a repair."
        ),
        example="Reuse recipes/cache.md.",
        verification="Neighbor key survives concurrent cleanup.",
        recipe="cache",
        prompt=("Are Redis, Valkey, and other mutable caches namespaced and "
                "cleaned by owner? Cite key prefixes and deletion paths. "
                "Mark N/A only with evidence that no mutable cache client "
                "exists in the admitted evidence. Absence of such evidence "
                "is unknown, never N/A."),
        path_patterns=(_CONFTEST, TEST_DIR, TEST_FILE, SRC_DIR, _MANIFEST,
                       _PTEST_TOML,
                       r"(?i)(?:^|/)[^/]*cach[^/]*$"),
        text_patterns=(r"(?i)redis|valkey|memcach|aiocache|cachetools"
                       r"|flushall|flushdb|clear_all|invalidate|\bcache\b",),
        scanner_codes=("cache.global-flush",),
        skip="no-cache",
    ),
    ChecklistEntry(
        id="RESOURCE-001",
        label="Files and ports",
        criterion="Writable files and listening ports are uniquely owned and released.",
        evidence="Cite temp-root and port allocation.",
        recommendation="Use run/worker temp roots and OS-assigned ports.",
        example="Reuse recipes/files-ports.md.",
        verification="Concurrent scoped runs use distinct paths/ports and preserve a neighbor sentinel.",
        recipe="files-ports",
        prompt=("Are writable files and listening ports uniquely owned and "
                "released? Cite temp roots and port allocation. Mark N/A "
                "only with evidence that tests create no files and bind no "
                "ports. Absence of such evidence is unknown, never N/A."),
        path_patterns=(_CONFTEST, TEST_DIR, TEST_FILE, SRC_DIR,
                       _PTEST_TOML),
        text_patterns=(r"\bopen\s*\(", r"Path\s*\(",
                       r"tmp_path|tmpdir|TemporaryDirectory|tempfile"
                       r"|mkstemp|mkdtemp",
                       r"(?i)PORT\s*=\s*\d|port\s*=\s*\d|bind\s*\("
                       r"|listen\s*\(|socket",
                       r"/tmp",
                       r"\.lock\b|filelock|flock"),
        scanner_codes=("resource.fixed-name", "network.fixed-port"),
        skip=None,
    ),
    ChecklistEntry(
        id="NETWORK-001",
        label="Network isolation",
        criterion="External network is denied or replaced by a declared isolated fake.",
        evidence="Cite clients, targets, and denial/fake boundary.",
        recommendation="Do not require a live service for ordinary tests.",
        example="Reuse recipes/time-network.md.",
        verification="Scoped ptest succeeds under network denial or against the declared local fake.",
        recipe="time-network",
        prompt=("Is external network denied or replaced by a declared "
                "isolated fake? Cite clients, targets, and the denial or "
                "fake boundary. Mark N/A only with evidence that no network "
                "client or target exists in scope. Absence of such evidence "
                "is unknown, never N/A."),
        path_patterns=(_CONFTEST, TEST_DIR, TEST_FILE, SRC_DIR, _MANIFEST,
                       _PTEST_TOML),
        text_patterns=(r"https?://",
                       r"\brequests\b|\bhttpx\b|urllib|\baiohttp\b|axios"
                       r"|\bfetch\s*\(|socket",
                       r"\brespx\b|\bresponses\b|pytest[-_]socket"
                       r"|socket\.socket|disable_socket|block_network"
                       r"|deny_network|\bvcr\b",),
        scanner_codes=("network.live-target",),
        skip=None,
    ),
    ChecklistEntry(
        id="PROCESS-001",
        label="Child processes",
        criterion="Child processes remain owned, joined, cancelled, and reaped.",
        evidence="Cite spawn and teardown paths.",
        recommendation="Keep descendants in the owned foreground process group; do not detach.",
        example="Reuse recipes/processes.md.",
        verification="Cancellation leaves no owned descendant and does not affect a neighbor process.",
        recipe="processes",
        prompt=("Do child processes stay owned, joined, cancelled, and "
                "reaped? Cite spawn and teardown paths. Mark N/A only with "
                "evidence that no child process is spawned in scope. Absence "
                "of such evidence is unknown, never N/A."),
        path_patterns=(_CONFTEST, TEST_DIR, TEST_FILE, SRC_DIR,
                       _PTEST_TOML),
        text_patterns=(r"subprocess|asyncio\.create_subprocess|Popen"
                       r"|multiprocessing|start_new_session"
                       r"|setsid|detach|fork\s*\(|os\.fork|spawn|workers"
                       r"|lifecycle",
                       r"\.join\s*\(|\.terminate\s*\(|\.kill\s*\("
                       r"|\.wait\s*\(",),
        scanner_codes=("process.detached-child",),
        skip=None,
    ),
    ChecklistEntry(
        id="TIME-001",
        label="Deterministic time",
        criterion="Clocks and synchronization are deterministic.",
        evidence="Cite clock injection and barriers.",
        recommendation="Prefer fake clocks/events over wall-clock sleeps.",
        example="Reuse recipes/time-network.md.",
        verification="Scoped ptest repeats without wall-clock waiting or race-dependent outcome.",
        recipe="time-network",
        prompt=("Are clocks and synchronization deterministic? Cite clock "
                "injection and barriers. Mark N/A only with evidence that no "
                "clock read, sleep, or synchronization exists in scope. "
                "Absence of such evidence is unknown, never N/A."),
        path_patterns=(_CONFTEST, TEST_DIR, TEST_FILE, SRC_DIR),
        text_patterns=(r"(?i)sleep|monotonic|datetime|timezone|freeze_time"
                       r"|freezegun|deadline|timeout|clock",),
        scanner_codes=("time.blocking-sleep",),
        skip=None,
    ),
    ChecklistEntry(
        id="SELECT-001",
        label="Test selection",
        criterion="Test selection has declared closed inputs and a conservative full fallback.",
        evidence="Cite policy, source-to-test mapping, environment, generated outputs, and full triggers.",
        recommendation="Dynamic unknown input widens to full.",
        example="Static plan previews are insufficient evidence.",
        verification=(
            "Exercise input changes through authorized ptest execution and inspect "
            "actual selection/fallback artifacts; static plan previews are insufficient."
        ),
        recipe=None,
        prompt=("Does test selection use declared closed inputs with a "
                "conservative full fallback? Cite the selection policy and "
                "triggers. Mark N/A only with evidence that selection is "
                "disabled or absent. Absence of such evidence is unknown, "
                "never N/A."),
        path_patterns=(_PTEST_TOML, _CONFTEST, TEST_DIR, TEST_FILE,
                       SRC_DIR),
        text_patterns=(r"os\.environ|getenv|subprocess|Path\.glob"
                       r"|\.glob\s*\(|selection|closed_inputs|input_roots"
                       r"|full_triggers",),
        scanner_codes=("selection.unknown-input",),
        skip=None,
    ),
    ChecklistEntry(
        id="TIMING-001",
        label="Test timing",
        criterion="Per-test timings are measured and interpreted without hiding integration work.",
        evidence="Cite ptest result/history timing.",
        recommendation=(
            "<0.5s healthy, 0.5-<2s inspect, 2-3s optimize, >3s investigate or justify; "
            "these are guidance, not failures."
        ),
        example="Absence of timings stays unknown.",
        verification="Repeat the scoped ptest measurement; absence of timings stays unknown.",
        recipe=None,
        prompt=("Are per-test timings measured and interpreted without "
                "hiding integration work? Cite ptest result or history "
                "timing. Mark N/A only with evidence that timing measurement "
                "cannot apply to this project. Absence of timings stays "
                "unknown, never N/A."),
        path_patterns=(_PTEST_TOML, _MANIFEST,
                       r"(?i)(?:^|/)(?:pytest\.ini|tox\.ini|setup\.cfg)$",
                       TEST_DIR, TEST_FILE, SRC_DIR),
        text_patterns=(r"(?i)timeout|durations|benchmark|timing|\bslow\b"
                       r"|workers",),
        scanner_codes=(),
        skip=None,
    ),
    ChecklistEntry(
        id="PARALLEL-001",
        label="Parallel execution",
        criterion="Tests run in parallel under ptest once the parallel-safety items allow it.",
        evidence="Cite the pytest xdist configuration or the vitest pool settings.",
        recommendation="Enable xdist workers or vitest pools only after the parallel-safety gaps are closed.",
        example="A serial fallback names its reason and fix.",
        verification="Scoped ptest shows the granted worker count and preserves neighbor records.",
        recipe=None,
        prompt=("Does this project run its tests in parallel under ptest? "
                "Cite the pytest xdist worker configuration or the vitest "
                "pool settings showing workers actually run. Mark N/A only "
                "with evidence that no automated tests exist in scope. "
                "Absence of such evidence is unknown, never N/A."),
        path_patterns=(_PTEST_TOML, _MANIFEST),
        text_patterns=(r"xdist|\bworkers\b|-n auto|numprocesses"
                       r"|pool\s*:\s*['\"]?(?:threads|forks|vmThreads)",
                       r"poolOptions|maxWorkers|minWorkers"),
        scanner_codes=(),
        skip=None,
    ),
)

# Every item prompt carries the same evidence standard. Applied here (not
# repeated in each literal) so no item can miss it: gap needs a cited
# concrete violation, satisfied needs the guaranteeing mechanism, and
# anything less is unknown naming the missing evidence.
CATALOG = tuple(
    replace(entry, prompt=entry.prompt + _PROMPT_STANDARD)
    for entry in CATALOG
)


def load_recipe(name: str) -> str:
    """Return one bounded packaged recipe; fail closed on unknown/missing data."""
    from . import contracts as C

    relative = _RECIPE_FILES.get(name)
    if relative is None:
        raise C.Problem(code="unsafe-path", message="packaged recipe name is not known",
                        phase="render")
    try:
        text = importlib.resources.files("ptest").joinpath(
            "resources", relative).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise C.Problem(code="state-unavailable", message="packaged recipe is unavailable",
                        phase="render") from None
    if len(text.encode("utf-8")) > _RECIPE_MAX_BYTES:
        raise C.Problem(code="invalid-bound", message="packaged recipe exceeds its bound",
                        phase="render")
    return text


__all__ = ["CATALOG", "ChecklistEntry", "load_recipe", "TEST_DIR",
           "TEST_FILE", "SRC_DIR"]
