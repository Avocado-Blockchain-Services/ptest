"""Private v3 source-ID response contract for bounded doctor reviews."""
from __future__ import annotations

import re

PROTOCOL_VERSION = 3
MAX_EVIDENCE_IDS = 16
MAX_NEEDS = 4
MAX_INITIAL_FILES = 20
MAX_INITIAL_BYTES = 192 * 1024
MAX_FOLLOWUP_FILES = 4
MAX_FOLLOWUP_BYTES = 64 * 1024
MAX_FINAL_FILES = 24
MAX_FINAL_BYTES = 256 * 1024
SOURCE_ID_RE = re.compile(r"src-[0-9a-f]{24}\Z")
SOURCE_ID_SCHEMA_PATTERN = r"^src-[0-9a-f]{24}$"


def private_schema(public_schema: dict) -> dict:
    """Build the exact private schema from the stable public row schema."""
    schema = {
        "type": "object",
        "required": ["status", "rationale", "evidence", "finding", "needs"],
        "properties": {
            "status": public_schema["properties"]["status"],
            "rationale": public_schema["properties"]["rationale"],
            "evidence": {
                "type": "array", "maxItems": MAX_EVIDENCE_IDS,
                "items": {"type": "string",
                          "pattern": SOURCE_ID_SCHEMA_PATTERN},
            },
            "finding": {
                "type": ["object", "null"],
                "required": ["summary", "suggested_change", "evidence"],
                "properties": {
                    "summary": public_schema["properties"]["finding"]
                    ["properties"]["summary"],
                    "suggested_change": public_schema["properties"][
                        "finding"]["properties"]["suggested_change"],
                    "evidence": {
                        "type": "array", "minItems": 1,
                        "maxItems": MAX_EVIDENCE_IDS,
                        "items": {"type": "string",
                                  "pattern": SOURCE_ID_SCHEMA_PATTERN},
                    },
                },
                "additionalProperties": False,
            },
            "needs": {
                "type": "array", "maxItems": MAX_NEEDS,
                "uniqueItems": True,
                "items": {"type": "string",
                          "pattern": SOURCE_ID_SCHEMA_PATTERN},
            },
        },
        "additionalProperties": False,
    }
    return schema


def validate_private_fields(document: dict, source_ids: set[str],
                            offered_ids: set[str], *,
                            available_ids: set[str] | None = None,
                            followup: bool = False) -> tuple[str, ...]:
    """Validate evidence and needs IDs against one item's frozen source map."""
    evidence = document.get("evidence")
    if not isinstance(evidence, list) or len(evidence) > MAX_EVIDENCE_IDS:
        raise ValueError("reply.evidence must contain at most 16 source IDs")
    allowed = source_ids if available_ids is None else available_ids
    for value in evidence:
        if not isinstance(value, str) or not SOURCE_ID_RE.fullmatch(value):
            raise ValueError("reply.evidence contains an invalid source ID")
        if value not in source_ids or value not in allowed:
            raise ValueError("reply.evidence contains an unknown or unoffered source ID")
    if len(set(evidence)) != len(evidence):
        raise ValueError("reply.evidence contains duplicate source IDs")

    status = document.get("status")
    if status in ("satisfied", "gap", "not-applicable") and not evidence:
        raise ValueError("conclusive reply needs source evidence")

    finding = document.get("finding")
    if status == "gap":
        if not isinstance(finding, dict):
            raise ValueError("gap reply needs a finding")
        finding_ids = finding.get("evidence")
        if not isinstance(finding_ids, list) or not finding_ids:
            raise ValueError("gap reply needs finding evidence")
        for value in finding_ids:
            if (not isinstance(value, str) or value not in evidence
                    or value not in source_ids or value not in allowed):
                raise ValueError("finding evidence contains an unoffered source ID")
        if len(set(finding_ids)) != len(finding_ids):
            raise ValueError("finding evidence contains duplicate source IDs")
    elif finding is not None:
        raise ValueError("non-gap reply must carry finding null")

    needs = document.get("needs")
    if not isinstance(needs, list) or len(needs) > MAX_NEEDS:
        raise ValueError("reply.needs must contain at most four IDs")
    for value in needs:
        if not isinstance(value, str) or not SOURCE_ID_RE.fullmatch(value):
            raise ValueError("reply.needs contains an invalid source ID")
    if followup and needs:
        raise ValueError("verification reply must have empty needs")
    for value in needs:
        if value not in offered_ids:
            raise ValueError("reply.needs contains an unoffered source ID")
    if len(set(needs)) != len(needs):
        raise ValueError("reply.needs contains duplicate source IDs")
    return tuple(needs)


__all__ = ["PROTOCOL_VERSION", "MAX_EVIDENCE_IDS", "MAX_NEEDS",
           "MAX_INITIAL_FILES", "MAX_INITIAL_BYTES", "MAX_FOLLOWUP_FILES",
           "MAX_FOLLOWUP_BYTES", "MAX_FINAL_FILES", "MAX_FINAL_BYTES",
           "SOURCE_ID_RE", "SOURCE_ID_SCHEMA_PATTERN", "private_schema",
           "validate_private_fields"]
