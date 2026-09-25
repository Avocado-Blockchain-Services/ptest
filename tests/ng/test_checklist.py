"""Extended ChecklistEntry catalog pins (T4 owned).

Pins catalog ids, human labels, packaged recipes, deterministic skip rules,
and prompt invariants of the extended ``ChecklistEntry``: every entry carries
a short human label, an item-specific prompt (question, evidence, N/A rule;
absence stays unknown), and per-item evidence routing seeded by static
scanner hits plus file patterns.
"""
from __future__ import annotations

import re
from dataclasses import fields

from ptest.checklist import CATALOG

EXPECTED = (
    ("FIX-001", "Test data factories", "factories", None),
    ("FIX-002", "Fixture state isolation", "factories", None),
    ("DB-001", "Database setup reuse", "databases", None),
    ("DB-002", "Database isolation", "databases", None),
    ("CACHE-001", "Cache isolation", "cache", None),
    ("RESOURCE-001", "Files and ports", "files-ports", None),
    ("NETWORK-001", "Network isolation", "time-network", None),
    ("PROCESS-001", "Child processes", "processes", None),
    ("TIME-001", "Deterministic time", "time-network", None),
    ("SELECT-001", "Test selection", None, None),
    ("TIMING-001", "Test timing", None, None),
    ("PARALLEL-001", "Parallel execution", None, None),
)

EXPECTED_FIELD_ORDER = (
    "id", "label", "criterion", "evidence", "recommendation", "example",
    "verification", "recipe", "prompt", "path_patterns", "text_patterns",
    "scanner_codes", "skip",
)


def test_catalog_ids_labels_recipes_and_skip_rules():
    assert len(CATALOG) == 12
    assert [entry.id for entry in CATALOG] == [row[0] for row in EXPECTED]
    assert [entry.label for entry in CATALOG] == [row[1] for row in EXPECTED]
    assert [entry.recipe for entry in CATALOG] == [row[2] for row in EXPECTED]
    assert [entry.skip for entry in CATALOG] == [row[3] for row in EXPECTED]


def test_catalog_field_order_is_frozen():
    assert tuple(f.name for f in fields(CATALOG[0])) == EXPECTED_FIELD_ORDER


def test_catalog_labels_are_short_plain_text():
    for entry in CATALOG:
        size = len(entry.label.encode("utf-8"))
        assert 1 <= size <= 64, entry.id
        assert "\n" not in entry.label, entry.id
        assert all(ord(char) >= 32 for char in entry.label), entry.id


def test_catalog_skip_values_are_closed():
    for entry in CATALOG:
        assert entry.skip in (None, "no-database", "no-cache"), entry.id


def test_catalog_prompts_state_question_evidence_na_rule_and_unknown():
    for entry in CATALOG:
        assert isinstance(entry.prompt, str) and entry.prompt.strip(), entry.id
        assert "?" in entry.prompt, entry.id
        lowered = entry.prompt.casefold()
        assert ("n/a" in lowered or "cannot apply" in lowered), entry.id
        assert "unknown" in lowered, entry.id
        assert len(entry.prompt.encode("utf-8")) <= 2048, entry.id


def test_catalog_routing_patterns_compile_and_scanner_codes_are_known():
    from ptest import doctor as doctor_api

    known = {code for code, *_ in doctor_api._RULES}
    for entry in CATALOG:
        assert isinstance(entry.path_patterns, tuple), entry.id
        assert isinstance(entry.text_patterns, tuple), entry.id
        assert isinstance(entry.scanner_codes, tuple), entry.id
        assert entry.path_patterns or entry.text_patterns or entry.scanner_codes, (
            entry.id)
        for pattern in entry.path_patterns + entry.text_patterns:
            re.compile(pattern)
        for code in entry.scanner_codes:
            assert code in known, (entry.id, code)


def test_catalog_db_cache_items_route_their_scanner_families():
    by_id = {entry.id: entry for entry in CATALOG}
    assert any(code.startswith("db.") for code in by_id["DB-001"].scanner_codes)
    assert any(code.startswith("db.") for code in by_id["DB-002"].scanner_codes)
    assert any(code.startswith("cache.")
               for code in by_id["CACHE-001"].scanner_codes)


def test_catalog_prompts_pin_gap_satisfied_unknown_standard():
    for entry in CATALOG:
        lowered = entry.prompt.casefold()
        assert "gap only with a cited concrete violation" in lowered, entry.id
        assert "satisfied only when the evidence shows the guaranteeing mechanism" in lowered, entry.id
        assert "one sentence naming the missing evidence" in lowered, entry.id
        assert "absence of code is unknown" in lowered, entry.id


def test_resource_item_routes_code_signals():
    import re

    by_id = {entry.id: entry for entry in CATALOG}
    for signal in ("tmp_path", "tempfile", "mkdtemp", "socket", "bind(",
                   "PORT = 8080", "port=8080", "/tmp", "app.lock",
                   "filelock", "flock"):
        assert any(re.search(pattern, "probe " + signal + " probe")
                   for pattern in by_id["RESOURCE-001"].text_patterns), signal


def test_network_item_routes_code_signals():
    import re

    by_id = {entry.id: entry for entry in CATALOG}
    for signal in ("httpx", "requests", "aiohttp", "respx", "responses",
                   "pytest-socket", "socket.socket", "disable_socket",
                   "vcr"):
        assert any(re.search(pattern, "probe " + signal + " probe")
                   for pattern in by_id["NETWORK-001"].text_patterns), signal


def test_process_item_routes_code_signals():
    import re

    by_id = {entry.id: entry for entry in CATALOG}
    for signal in ("subprocess", "asyncio.create_subprocess",
                   "multiprocessing", "Popen", "os.fork",
                   "worker.join(", "proc.terminate(", "proc.kill(",
                   "proc.wait("):
        assert any(re.search(pattern, "probe " + signal + " probe")
                   for pattern in by_id["PROCESS-001"].text_patterns), signal


def test_parallel_ids_are_single_sourced():
    """PARALLEL_SAFETY_IDS / PARALLEL_ITEM_ID live in checklist only."""
    import pytest

    from ptest import checklist as checklist_api
    from ptest import deterministic_items as deterministic_api
    from ptest import recommendations as recommendations_api
    from ptest import render as render_api

    assert checklist_api.PARALLEL_ITEM_ID == "PARALLEL-001"
    assert tuple(checklist_api.PARALLEL_SAFETY_IDS) == (
        "FIX-002", "DB-001", "DB-002", "CACHE-001", "RESOURCE-001",
        "NETWORK-001", "PROCESS-001", "TIME-001",
    )
    assert set(deterministic_api.PARALLEL_SAFETY_IDS) == set(
        checklist_api.PARALLEL_SAFETY_IDS)
    assert set(render_api._PARALLEL_SAFETY_IDS) == set(
        checklist_api.PARALLEL_SAFETY_IDS)
    assert set(recommendations_api._PARALLEL_SAFETY_IDS) == set(
        checklist_api.PARALLEL_SAFETY_IDS)
    assert render_api._PARALLEL_ITEM_ID == checklist_api.PARALLEL_ITEM_ID
    assert recommendations_api._PARALLEL_ITEM_ID == (
        checklist_api.PARALLEL_ITEM_ID)
    assert deterministic_api.DETERMINISTIC_ITEM_IDS[-1] == (
        checklist_api.PARALLEL_ITEM_ID)
    with pytest.raises(AttributeError):
        render_api._PARALLEL_SAFETY_IDS.add("PARALLEL-001")


def test_score_bounds_derive_from_checklist_length():
    """Score bounds and the 'all N' row count follow the catalog length."""
    import pytest

    from ptest import agent_assessment as assessment_api
    from ptest import checklist as checklist_api
    from ptest import contracts as contracts_api

    count = len(checklist_api.CATALOG)
    assert count == 12
    assert tuple(contracts_api.AGENT_ASSESSMENT_CHECKLIST_IDS) == tuple(
        entry.id for entry in checklist_api.CATALOG)
    full = assessment_api.Score(satisfied=count, applicable=count,
                               percent=100)
    assert (full.satisfied, full.applicable) == (count, count)
    with pytest.raises(ValueError):
        assessment_api.Score(satisfied=count + 1, applicable=count,
                            percent=100)
    with pytest.raises(ValueError):
        assessment_api.Score(satisfied=count, applicable=count + 1,
                            percent=100)
    schema = contracts_api._aa_score_schema()
    assert schema["properties"]["satisfied"]["maximum"] == count
    assert schema["properties"]["applicable"]["maximum"] == count
    child = contracts_api._aa_child_schema()
    assert child["properties"]["rows"]["minItems"] == count
    assert child["properties"]["rows"]["maxItems"] == count
