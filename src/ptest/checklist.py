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
from dataclasses import dataclass

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
    "tests": "recipes/tests.md",
}

# Recipes `ptest guide` prints after the catalog although no catalog item
# references them (they guide writing tests, not repairing a doctor finding).
GUIDE_RECIPES: tuple[str, ...] = ("tests",)

# Shared routing fragments (regexes searched against excerpt paths).
TEST_DIR = r"(?i)(?:^|/)(?:tests?|__tests__)(?:/|$)"
TEST_FILE = r"(?i)(?:^|/)test_[^/]*\.py$|[^/]*_test\.py$|\.test\.|\.spec\."
SRC_DIR = r"(?:^|/)src(?:/|$)"
_CONFTEST = r"(?:^|/)conftest\.py$"

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
                "weakening assertions? Trace a concrete factory through its "
                "caller and cite both the creation and caller assertions. "
                "A factory name or definition alone proves nothing; check "
                "whether callers reuse mutable records or weaken assertions. "
                "Read-only shared identity is not a violation by itself; "
                "require a demonstrated mutation leak, unreset mutable owner, "
                "or weakened assertion. If no decisive consumer is shown, "
                "name that missing fact instead of inferring one. "
                "Use N/A only with affirmative evidence that record creation "
                "cannot apply in the assessed scope."),
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
        criterion="Mutable fixture state is owned or reset between the reviewed uses.",
        evidence="Cite fixture lifetime, mutation, and reset boundaries.",
        recommendation="Replace shared mutable state or prove deterministic reset.",
        example="Reuse recipes/factories.md.",
        verification=("Mutate one fixture instance, then verify the next "
                      "instance is independent while original assertions "
                      "and inventory remain."),
        recipe="factories",
        prompt=("Is mutable fixture state owned or reset between the "
                "representative uses shown? Trace the fixture lifetime, "
                "mutations, and reset boundary in actual reachable callers. "
                "A module-scoped immutable object is not a shared-state gap; "
                "a mutable object needs a per-test reset or fresh owner. "
                "Use N/A only with affirmative evidence that mutable shared "
                "fixture state cannot apply in the assessed scope."),
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
        verification=("Count database/schema setup calls across at least two "
                      "tests and confirm expensive run/worker-owned setup is "
                      "reused without weakening assertions."),
        recipe="databases",
        prompt=("Is expensive database, server, or schema initialization "
                "reused per run or worker rather than invoked per test? "
                "Trace the actual creation/schema operation to its caller "
                "and fixture scope. Importing a database library or resetting "
                "records does not prove repeated server or schema creation. "
                "Missing setup evidence is unknown, never N/A. Use N/A only "
                "with affirmative evidence that database setup cannot apply "
                "in the assessed scope."),
        path_patterns=(_CONFTEST, TEST_DIR, TEST_FILE, SRC_DIR, _MANIFEST,
                       _PTEST_TOML,
                       r"(?i)(?:^|/)[^/]*(?:migration|models?|database|db)[^/]*$"),
        text_patterns=(r"(?i)sqlalchemy|sqlmodel|django|alembic|psycopg|asyncpg"
                       r"|aiosqlite|sqlite3?|peewee|tortoise|prisma|sequelize"
                       r"|typeorm|knex|mongoose|pymongo|DATABASE_URL"
                       r"|database_url|create_all|drop_database|drop_all"
                       r"|truncate|connect\s*\(|Column\s*\(|sessionmaker"
                       r"|create_engine|\.execute\s*\(",),
        scanner_codes=("db.per-test-initialization", "db.cleanup-ownership"),
        skip=None,
    ),
    ChecklistEntry(
        id="DB-002",
        label="Database isolation",
        criterion="Database identities, records, and cleanup have explicit ownership for the reviewed paths.",
        evidence="Cite name derivation and teardown.",
        recommendation="Remove only owned records/namespaces; never infer ownership from a test-like name.",
        example="Reuse recipes/databases.md.",
        verification="Neighbor database/schema sentinel survives concurrent teardown.",
        recipe="databases",
        prompt=("Do database identities, records, and cleanup have explicit "
                "ownership in the representative paths shown? A fresh "
                "SQLite database or instance per test/use can establish "
                "isolation; shared mutable services need identities scoped "
                "to the overlapping run or worker owners. "
                "Trace the namespace/name helper from an actual reachable "
                "caller through teardown; do not infer every caller from one "
                "sample. A hardcoded prefix alone "
                "does not prove collision if a helper adds run or worker "
                "identity. Check shared setup and counterevidence; missing "
                "ownership evidence is unknown, never N/A. Use N/A only with "
                "affirmative evidence that database identity and cleanup "
                "cannot apply in the assessed scope."),
        path_patterns=(_CONFTEST, TEST_DIR, TEST_FILE, SRC_DIR, _MANIFEST,
                       _PTEST_TOML,
                       r"(?i)(?:^|/)[^/]*(?:migration|models?|database|db)[^/]*$"),
        text_patterns=(r"(?i)sqlalchemy|sqlmodel|django|alembic|psycopg|asyncpg"
                       r"|aiosqlite|sqlite3?|peewee|tortoise|prisma|sequelize"
                       r"|typeorm|knex|mongoose|pymongo|DATABASE_URL"
                       r"|database_url|create_all|drop_database|drop_all"
                       r"|truncate|connect\s*\(|Column\s*\(|sessionmaker"
                       r"|create_engine|\.execute\s*\(",),
        scanner_codes=("db.per-test-initialization", "db.cleanup-ownership"),
        skip=None,
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
                "cleaned by owner? Trace the key prefix, cleanup target, and "
                "any proven exclusive disposable-service boundary. Client "
                "construction alone does not prove unowned deletion; a "
                "global flush is safe only when exclusivity is established. "
                "Missing ownership evidence is unknown, never N/A. Use N/A "
                "only with affirmative evidence that mutable caches cannot "
                "apply in the assessed scope."),
        path_patterns=(_CONFTEST, TEST_DIR, TEST_FILE, SRC_DIR, _MANIFEST,
                       _PTEST_TOML,
                       r"(?i)(?:^|/)[^/]*cach[^/]*$"),
        text_patterns=(r"(?i)redis|valkey|memcach|aiocache|cachetools"
                       r"|flushall|flushdb|clear_all|invalidate|\bcache\b",),
        scanner_codes=("cache.global-flush",),
        skip=None,
    ),
    ChecklistEntry(
        id="RESOURCE-001",
        label="Files and ports",
        criterion="Representative writable files and listening ports are uniquely owned and released.",
        evidence="Cite temp-root and port allocation.",
        recommendation="Use run/worker temp roots and OS-assigned ports.",
        example="Reuse recipes/files-ports.md.",
        verification="Concurrent scoped runs use distinct paths/ports and preserve a neighbor sentinel.",
        recipe="files-ports",
        prompt=("Assess actual writable-file and listening-port operations "
                "shown on representative reachable paths. Trace allocation, "
                "exclusive ownership or coordination, and release for those "
                "operations. Fixture temporary roots, context managers, "
                "finally blocks, and operating-system release of file handles "
                "and locks are valid mechanisms. A fixed lock path is not "
                "automatically a collision when the supplied mechanism "
                "coordinates exclusive access. An outbound client destination "
                "port is not a listening-port allocation. If the shown file "
                "mechanism is sufficient, hypothetical additional writable "
                "files or absent listening-port operations are not reasons "
                "for unknown; qualify satisfied to the sampled file paths. "
                "Database record or schema cleanup is assessed by the "
                "database items, not by this file/port criterion. Return gap "
                "for a concrete reachable violation in the shown code; return "
                "unknown only when a decisive allocation, caller, ownership, "
                "or release fact for a shown applicable resource is missing. "
                "Use not-applicable only with affirmative evidence that "
                "neither resource has a role in the assessed scope. Check "
                "concrete counterevidence before deciding."),
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
        criterion="External network used by the representative callers is denied or replaced by an isolated fake.",
        evidence="Cite clients, targets, and denial/fake boundary.",
        recommendation="Do not require a live service for ordinary tests.",
        example="Reuse recipes/time-network.md.",
        verification=("Exercise an unexpected request through the configured "
                      "test setup and confirm denial intercepts it while "
                      "allowed local fake requests still work."),
        recipe="time-network",
        prompt=("For the representative callers shown, is external network "
                "denied or replaced by an isolated fake? Trace the active "
                "client's interception or "
                "denial boundary through configured shared setup. An external "
                "URL beneath a global mock is not proof of live traffic. "
                "Missing or incomplete setup is unknown, not suite-wide "
                "satisfaction. Use N/A only with affirmative evidence that "
                "network access cannot apply in the assessed scope."),
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
        criterion="Representative child processes remain owned, joined, cancelled, and reaped.",
        evidence="Cite spawn and teardown paths.",
        recommendation="Keep descendants in the owned foreground process group; do not detach.",
        example="Reuse recipes/processes.md.",
        verification="Cancellation leaves no owned descendant and does not affect a neighbor process.",
        recipe="processes",
        prompt=("Do representative test-created child processes stay owned, "
                "joined, cancelled, and reaped? Trace nested launches as a "
                "process tree from the test-owned parent. A child's own "
                "timeout cannot guarantee descendant cleanup after that child "
                "is terminated by its parent. When complete reachable "
                "spawn-owner code shows a timeout or exception boundary "
                "without required cancel or join, that is a concrete gap; "
                "unknown is for missing decisive owner or caller code. "
                "Standard subprocess.run waits for and reaps its direct child; "
                "its timeout kills and waits for that child only, not its "
                "descendants. "
                "Inspect all reachable branches independently before "
                "comparing the untrusted draft. Distinguish a runner deadline "
                "from cancellation of awaited work or entry into finally; "
                "do not assume the latter follows from the former. "
                "Runner or provider worker declarations alone do not prove "
                "an orphan test process. Use not-applicable only with "
                "affirmative evidence that no test-created child process can "
                "exist in scope."),
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
        prompt=("Are clocks and synchronization deterministic? Determine "
                "whether a wall-clock delay or read controls an assertion or "
                "synchronization, and whether fake timers or shared setup "
                "intercept it. A sleep-like name alone is not a gap. Cite the "
                "clock use, synchronization role, and any counterevidence; "
                "missing setup is unknown. Use N/A only with affirmative "
                "evidence that time-dependent assertions and synchronization "
                "cannot apply in the assessed scope."),
        path_patterns=(_CONFTEST, TEST_DIR, TEST_FILE, SRC_DIR),
        text_patterns=(r"(?i)sleep|monotonic|datetime|timezone|freeze_time"
                       r"|freezegun|deadline|timeout|clock|threading\s*\.\s*Timer"
                       r"|\bTimer\s*\(|threading\s*\.\s*(?:Event|Barrier|Condition)"
                       r"|\b(?:Event|Barrier|Condition)\s*\(|asyncio\s*\.\s*Event"
                       r"|asyncio\s*\.\s*wait_for|call_later\s*|\.wait\s*\(",),
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
                "conservative full fallback? For pytest, assess each child's "
                "selection policy: enabled closed inputs can satisfy this; "
                "disabled or open policy is a gap, even when explicit "
                "file/path scopes remain available. Root ptest --changed "
                "uses each affected pytest child's policy. For Vitest, "
                "ptest does not own per-test selection: an explicit-base "
                "root ptest --changed --base REF delegates affected children "
                "to native --changed REF, while no explicit base uses the "
                "full-suite fallback. Cite policy, routing, and triggers. "
                "Use N/A only when the runner has no ptest-owned selection "
                "policy or per-test protocol; missing evidence is unknown, "
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

#: Parallel-safety items gating the PARALLEL-001 enabling suggestion: the
#: suggestion to add workers is offered only when none of these has a gap.
#: Single source — deterministic_items, render, and recommendations import
#: these names instead of keeping literal copies.
PARALLEL_SAFETY_IDS: tuple[str, ...] = (
    "FIX-002", "DB-001", "DB-002", "CACHE-001", "RESOURCE-001",
    "NETWORK-001", "PROCESS-001", "TIME-001",
)

#: Checklist id of the parallel-execution item trailing the safety group.
PARALLEL_ITEM_ID = "PARALLEL-001"


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


def recipe_names() -> tuple[str, ...]:
    """Packaged recipe names in a stable order: the `ptest guide <topic>` topics."""
    return tuple(_RECIPE_FILES)


__all__ = ["CATALOG", "ChecklistEntry", "load_recipe", "recipe_names",
           "TEST_DIR", "TEST_FILE", "SRC_DIR", "PARALLEL_SAFETY_IDS",
           "PARALLEL_ITEM_ID"]
