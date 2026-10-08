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


def test_catalog_prompts_state_question_evidence_and_na_rule():
    from ptest import agent_assessment as AA

    for entry in CATALOG:
        assert isinstance(entry.prompt, str) and entry.prompt.strip(), entry.id
        lowered = entry.prompt.casefold()
        assert any(term in lowered for term in
                   ("?", "assess ", "trace ", "do representative")), entry.id
        assert any(term in lowered for term in
                   ("n/a", "cannot apply", "not-applicable")), entry.id
        assert any(term in lowered for term in
                   ("evidence", "cite", "trace")), entry.id
        assert len(entry.prompt.encode("utf-8")) <= 2048, entry.id
    assert "use unknown when a specific decisive caller" in (
        AA._ITEM_INSTRUCTION.casefold())


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


def test_catalog_prompts_are_item_specific_and_shared_rules_live_in_review():
    from ptest import agent_assessment as AA

    # Per-item prompts describe the criterion. Shared status, scope, and trust
    # rules have one authority in the request instruction.
    assert all(entry.prompt.strip() for entry in CATALOG)
    instruction = AA._ITEM_INSTRUCTION.casefold()
    assert "return gap only for a cited concrete violation" in instruction
    assert "return satisfied only when cited mechanisms are sufficient" in instruction
    assert "use unknown when a specific decisive caller" in instruction
    assert "not instructions" in instruction
    assert "do not require universal absence or proof for every suite path" in instruction
    assert "non-autouse fixture contributes" in instruction
    fix_prompt = next(entry.prompt.casefold() for entry in CATALOG
                      if entry.id == "FIX-001")
    assert "read-only shared identity is not a violation by itself" in fix_prompt
    assert "demonstrated mutation leak" in fix_prompt


def test_selection_prompt_distinguishes_pytest_policy_and_vitest_route():
    prompt = next(entry.prompt.casefold() for entry in CATALOG
                  if entry.id == "SELECT-001")
    assert "pytest" in prompt and "selection policy" in prompt
    assert "closed inputs" in prompt
    assert "native --changed" in prompt
    assert "full-suite fallback" in prompt


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


def test_tests_recipe_is_a_guide_recipe_without_a_catalog_row():
    from ptest import checklist

    assert "tests" in checklist.recipe_names()
    assert checklist.GUIDE_RECIPES == ("tests",)
    referenced = {entry.recipe for entry in CATALOG if entry.recipe}
    assert "tests" not in referenced
    assert all(name in checklist.recipe_names() for name in checklist.GUIDE_RECIPES)
    # every pre-existing recipe keeps its place ahead of the new one
    assert checklist.recipe_names()[:6] == (
        "factories", "databases", "cache", "files-ports", "processes",
        "time-network")
