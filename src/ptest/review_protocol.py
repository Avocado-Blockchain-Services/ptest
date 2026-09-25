"""Private proof protocol and packet-only evidence follow-up.

The public assessment rows keep their v1 shape. This module validates the
private response trace and constructs the one bounded follow-up request from
the immutable excerpts already admitted to a child packet.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

PROTOCOL_VERSION = 2
PROOF_ROLES = frozenset({
    "applicability", "mechanism", "violation", "counterevidence"})
MAX_PROOF_ENTRIES = 4
MAX_QUOTE_BYTES = 512
MAX_NEEDS = 4
MAX_INITIAL_FILES = 20
MAX_INITIAL_BYTES = 192 * 1024
MAX_FOLLOWUP_FILES = 4
MAX_FOLLOWUP_BYTES = 64 * 1024
MAX_FINAL_FILES = 24
MAX_FINAL_BYTES = 256 * 1024
_OPAQUE_ID_RE = re.compile(r"ctx-[0-9a-f]{24}\Z")
_PROOF_KEYS = frozenset({"role", "citation_index", "quote"})
_CITATION_KEYS = frozenset({"path", "start_line", "end_line", "sha256"})


@dataclass(frozen=True, slots=True)
class OmittedExcerpt:
    """One packet excerpt omitted from an item's first request."""

    opaque_id: str
    excerpt: object
    role: str

    def __post_init__(self) -> None:
        if not isinstance(self.opaque_id, str) \
                or not _OPAQUE_ID_RE.fullmatch(self.opaque_id):
            raise ValueError("omitted excerpt ID is invalid")
        if not hasattr(self.excerpt, "path") \
                or not hasattr(self.excerpt, "text"):
            raise TypeError("omitted excerpt must be a source excerpt")
        if self.role not in {"config", "setup", "fixture", "helper",
                             "test", "source"}:
            raise ValueError("omitted excerpt role is invalid")

    @property
    def size(self) -> int:
        return len(self.excerpt.text.encode("utf-8"))

    def public_inventory(self) -> dict:
        """Safe offer metadata; source identity remains in the packet."""
        return {"id": self.opaque_id, "path": self.excerpt.path,
                "role": self.role, "bytes": self.size}


def opaque_id(packet_sha256: str, item_id: str, path: str,
              excerpt_sha256: str) -> str:
    """Stable opaque identity bound to packet, item and excerpt content."""
    material = "\0".join((packet_sha256, item_id, path,
                           excerpt_sha256)).encode("utf-8")
    return "ctx-" + hashlib.sha256(material).hexdigest()[:24]


def private_schema(public_schema: dict) -> dict:
    """Return the private one-row schema with proof and needs fields."""
    schema = json.loads(json.dumps(public_schema))
    schema["required"] = list(schema["required"]) + ["proof", "needs"]
    schema["properties"]["proof"] = {
        "type": "array", "maxItems": MAX_PROOF_ENTRIES,
        "items": {
            "type": "object",
            "required": ["role", "citation_index", "quote"],
            "properties": {
                "role": {"type": "string", "enum": sorted(PROOF_ROLES)},
                "citation_index": {"type": "integer", "minimum": 0},
                "quote": {"type": "string", "maxBytes": MAX_QUOTE_BYTES},
            },
            "additionalProperties": False,
        },
    }
    schema["properties"]["needs"] = {
        "type": "array", "maxItems": MAX_NEEDS, "uniqueItems": True,
        "items": {"type": "string", "pattern": r"^ctx-[0-9a-f]{24}$"},
    }
    return schema


def _citation_slice(citation: object, subset: dict) -> tuple[dict, str]:
    if not isinstance(citation, dict) or set(citation) != _CITATION_KEYS:
        raise ValueError("proof citation must be an object")
    path = citation.get("path")
    excerpt = subset.get(path) if isinstance(path, str) else None
    if excerpt is None:
        raise ValueError("proof cites evidence outside the item packet")
    if citation.get("sha256") != excerpt.sha256:
        raise ValueError("proof citation identity is stale")
    start, end = citation.get("start_line"), citation.get("end_line")
    if (isinstance(start, bool) or not isinstance(start, int)
            or isinstance(end, bool) or not isinstance(end, int)
            or not excerpt.start_line <= start <= end <= excerpt.end_line):
        raise ValueError("proof citation escapes its excerpt")
    lines = excerpt.text.splitlines() or [""]
    lo = start - excerpt.start_line
    hi = end - excerpt.start_line + 1
    return citation, "\n".join(lines[lo:hi])


def validate_private_fields(document: dict, subset: dict,
                            offered_ids: set[str], *,
                            followup: bool = False,
                            valid_evidence: set[tuple] | None = None,
                            valid_finding_evidence: set[tuple] | None = None
                            ) -> tuple[str, ...]:
    """Validate private proof/needs fields against the cited packet lines.

    Raises ``ValueError`` for any invalid trace. Callers map that failure to
    the existing public ``unknown`` row; quotes and needs never escape into
    the public report.
    """
    proof = document.get("proof")
    if not isinstance(proof, list) or len(proof) > MAX_PROOF_ENTRIES:
        raise ValueError("reply.proof must contain at most four entries")
    citations = document.get("evidence")
    if not isinstance(citations, list):
        raise ValueError("reply.evidence must be a list")
    roles: set[str] = set()
    proof_citations: dict[str, set[tuple]] = {}
    for position, entry in enumerate(proof):
        if not isinstance(entry, dict) or set(entry) != _PROOF_KEYS:
            raise ValueError(f"reply.proof[{position}] has invalid fields")
        role = entry["role"]
        index = entry["citation_index"]
        quote = entry["quote"]
        if role not in PROOF_ROLES:
            raise ValueError("reply.proof has an unknown role")
        if isinstance(index, bool) or not isinstance(index, int) \
                or not 0 <= index < len(citations):
            raise ValueError("reply.proof citation index is out of range")
        if not isinstance(quote, str) or not quote:
            raise ValueError("reply.proof quote must be nonempty")
        try:
            quote_size = len(quote.encode("utf-8"))
        except UnicodeEncodeError:
            raise ValueError("reply.proof quote is not valid UTF-8") from None
        if quote_size > MAX_QUOTE_BYTES:
            raise ValueError("reply.proof quote exceeds its bound")
        citation, cited_lines = _citation_slice(citations[index], subset)
        if quote not in cited_lines:
            raise ValueError("reply.proof quote is not in its citation")
        roles.add(role)
        proof_citations.setdefault(role, set()).add((
            citation.get("path"), citation.get("start_line"),
            citation.get("end_line"), citation.get("sha256")))

    status = document.get("status")
    if status == "satisfied" and not {"applicability", "mechanism"} <= roles:
        raise ValueError("satisfied reply needs applicability and mechanism proof")
    if status == "gap":
        if not {"applicability", "violation"} <= roles:
            raise ValueError("gap reply needs applicability and violation proof")
        finding = document.get("finding")
        finding_evidence = (finding.get("evidence")
                            if isinstance(finding, dict) else None)
        if not isinstance(finding_evidence, list):
            raise ValueError("gap reply needs finding evidence")
        violation_citations = proof_citations.get("violation", set())
        finding_keys = {
            (item.get("path"), item.get("start_line"), item.get("end_line"),
             item.get("sha256")) for item in finding_evidence
            if isinstance(item, dict)}
        if not violation_citations & finding_keys:
            raise ValueError("violation proof must appear in finding evidence")
    if status == "not-applicable" and "applicability" not in roles:
        raise ValueError("not-applicable reply needs affirmative proof")

    if valid_evidence is not None and any(
            not citations_for_role <= valid_evidence
            for citations_for_role in proof_citations.values()):
        raise ValueError("proof cites evidence dropped by validation")
    if status == "gap" and valid_finding_evidence is not None:
        violation_citations = proof_citations.get("violation", set())
        if not violation_citations & valid_finding_evidence:
            raise ValueError(
                "violation proof cites finding evidence dropped by validation")

    needs = document.get("needs")
    if not isinstance(needs, list) or len(needs) > MAX_NEEDS:
        raise ValueError("reply.needs must contain at most four IDs")
    if any(not isinstance(value, str) or not _OPAQUE_ID_RE.fullmatch(value)
           for value in needs):
        raise ValueError("reply.needs contains an invalid ID")
    if len(set(needs)) != len(needs):
        raise ValueError("reply.needs contains duplicate IDs")
    if any(value not in offered_ids for value in needs):
        raise ValueError("reply.needs contains an unoffered ID")
    if status != "unknown" and needs:
        raise ValueError("only an unknown reply may request evidence")
    if followup and needs:
        raise ValueError("follow-up reply must have empty needs")
    return tuple(needs)


def build_followup_request(first_request: bytes,
                           inventory: tuple[OmittedExcerpt, ...],
                           requested_ids: tuple[str, ...], *,
                           max_request_bytes: int) -> tuple[bytes, tuple[str, ...]]:
    """Add at most four already-collected excerpts to one item request."""
    if not requested_ids or len(requested_ids) > MAX_NEEDS \
            or len(set(requested_ids)) != len(requested_ids):
        raise ValueError("follow-up needs are empty, duplicated, or oversized")
    by_id = {entry.opaque_id: entry for entry in inventory}
    if any(identifier not in by_id for identifier in requested_ids):
        raise ValueError("follow-up requested an unoffered excerpt")
    additions = [by_id[identifier] for identifier in requested_ids]
    try:
        payload = json.loads(first_request.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ValueError("first request is invalid") from None
    excerpts = payload.get("excerpts")
    packet = payload.get("packet")
    if not isinstance(excerpts, list) or not isinstance(packet, dict):
        raise ValueError("first request is incomplete")
    existing_paths = {entry.get("path") for entry in excerpts
                      if isinstance(entry, dict)}
    existing_bytes = sum(len(str(entry.get("text", "")).encode("utf-8"))
                         for entry in excerpts if isinstance(entry, dict))
    if any(entry.excerpt.path in existing_paths for entry in additions):
        raise ValueError("follow-up did not add new evidence")

    sent_ids: set[str] = set()
    added_bytes = 0

    def render() -> bytes:
        packet["phase"] = "evidence-followup"
        packet["omission_inventory"] = [
            entry.public_inventory() for entry in inventory
            if entry.opaque_id not in sent_ids]
        packet["omitted"] = [
            entry.get("path") for entry in packet["omission_inventory"]]
        payload["packet"] = packet
        return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=True).encode("utf-8")

    selected: list[OmittedExcerpt] = []
    for entry in additions:
        if len(selected) >= MAX_FOLLOWUP_FILES:
            break
        if added_bytes + entry.size > MAX_FOLLOWUP_BYTES:
            continue
        if len(excerpts) + 1 > MAX_FINAL_FILES \
                or existing_bytes + added_bytes + entry.size > MAX_FINAL_BYTES:
            continue
        excerpt = entry.excerpt
        excerpts.append({
            "path": excerpt.path,
            "start_line": excerpt.start_line,
            "end_line": excerpt.end_line,
            "sha256": excerpt.sha256,
            "role": entry.role,
            "completeness": ("complete" if excerpt.complete else "partial"),
            "text": excerpt.text,
        })
        sent_ids.add(entry.opaque_id)
        added_bytes += entry.size
        candidate = render()
        if len(candidate) > max_request_bytes:
            excerpts.pop()
            sent_ids.remove(entry.opaque_id)
            added_bytes -= entry.size
            continue
        selected.append(entry)

    if not selected:
        raise ValueError("no requested excerpt fits the follow-up bounds")
    request = render()
    return request, tuple(entry.excerpt.path for entry in selected)


__all__ = [
    "PROTOCOL_VERSION", "PROOF_ROLES", "OmittedExcerpt", "opaque_id",
    "private_schema", "validate_private_fields", "build_followup_request",
    "MAX_INITIAL_FILES", "MAX_INITIAL_BYTES", "MAX_FOLLOWUP_FILES",
    "MAX_FOLLOWUP_BYTES", "MAX_FINAL_FILES", "MAX_FINAL_BYTES",
]
