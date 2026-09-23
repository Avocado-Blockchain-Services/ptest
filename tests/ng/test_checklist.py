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
    ("DB-001", "Database setup reuse", "databases", "no-database"),
    ("DB-002", "Database isolation", "databases", "no-database"),
    ("CACHE-001", "Cache isolation", "cache", "no-cache"),
    ("RESOURCE-001", "Files and ports", "files-ports", None),
    ("NETWORK-001", "Network isolation", "time-network", None),
    ("PROCESS-001", "Child processes", "processes", None),
    ("TIME-001", "Deterministic time", "time-network", None),
    ("SELECT-001", "Test selection", None, None),
    ("TIMING-001", "Test timing", None, None),
)

EXPECTED_FIELD_ORDER = (
    "id", "label", "criterion", "evidence", "recommendation", "example",
    "verification", "recipe", "prompt", "path_patterns", "text_patterns",
    "scanner_codes", "skip",
)


def test_catalog_ids_labels_recipes_and_skip_rules():
    assert len(CATALOG) == 11
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
        assert "n/a" in lowered, entry.id
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
