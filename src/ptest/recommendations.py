"""Agent-review recommendations: pure Markdown plus guarded publication.

Assumed ``AssessmentRun`` interface (the assessment worker owns the real
dataclasses; this module never imports that new module): ``run`` is a
``PublicDocument`` with ``kind == "agent-assessment"`` (its ``.data`` dict
is used), a plain mapping, or any duck-typed object exposing ``provider``,
``children`` and ``limitations``. Shapes::

    provider: {"name", "cli_version", "profile"} (display only)
    children: [{"project_id" (32 hex), "scope" (root-relative),
                "packet_sha256" (64 hex), "rows": [...], "findings": [...],
                "limitations": [{"code", "message", "paths"}]}]
    rows: [{"id" (canonical checklist ID), "status", "rationale",
            "evidence": [{"path", "start_line", "end_line", "sha256"}]}]
    findings: [{"id", "summary", "suggested_change", "recipe_id" | None,
                "evidence": [...]}]  (one per gap row; a gap row without a
                finding renders as unresolved/unverified with a next step)
    limitations: [{"code", "message", "paths"}]

Controller note: scores are always recomputed here as
``satisfied / applicable`` with integer floor; any model-supplied score is
ignored. ``publish_recommendations`` returns the task-local
``PublishResult`` (``status`` in ``created``/``replaced``/``unchanged``),
NOT ``contracts.PublishResult`` (whose history-specific
``committed/baseline_published`` fields cannot express report ownership).
Conflicts raise ``Problem(code="report-conflict")`` and stale caller
identity raises ``Problem(code="stale-evidence")``.

Source-drift guard: ``publish_recommendations`` requires an explicit
kw-only ``source_proof`` list of collected file identities (root-relative
path, excerpt-chunk sha256, exact admitted ``byte_count`` per entry,
line interval); omission fails closed with ``stale-evidence`` and an
empty list is the explicit claim for no admitted files. Each entry's
sha256 is the SHA-256 of exactly the first ``byte_count`` admitted
bytes (0..64KiB; ``SourceExcerpt.text.encode('utf-8')`` length at the
caller, UTF-8-safe shorter on a multibyte boundary) -- the whole file
when it fits -- matching the assessment ``SourceExcerpt.sha256``
contract, never an impossible full-file hash for truncated input.
Only the admitted prefix is read or decoded; the rest of a large file
is never touched. Each entry is rechecked with a descriptor-walk
no-follow read inside the lock before staging and again immediately
before rename; any prefix drift, non-UTF-8 prefix, line bound beyond
the decoded admitted prefix, or symlink (including a symlinked parent
component) raises ``stale-evidence`` and preserves the prior report.
Beyond-prefix drift is unobservable here: once a file exceeds the
admitted prefix, later bytes are not compared, so the CLI caller must
recompute evidence packets and compare whole-packet/config identity
(``packet_sha256``) before publishing; a caller-supplied prior-report
identity is NOT source identity.

Durability: the temp-write/fchmod/fsync/atomic-replace/parent-sync
contract is enforced; a parent-directory fsync failure fails closed
(``state-unavailable``) after a best-effort restore of the prior
complete report. Displayed scope is allowlist-validated. Verification argv
comes from private CLI routing context checked by ptest's dispatcher; without
it, the report uses root ``ptest --full``. Model input cannot select a
command. Lock acquisition never silently degrades: an
unavailable lock path or lock open fails closed with
``coordinator-unavailable``/``report-conflict`` and no publication.

Residuals: ordinary POSIX rename is not a content compare-and-swap, so a
hostile same-user mutator acting inside the final check/rename window
can still win; the lock serializes cooperating writers but is not an
absolute concurrent-editor guarantee. Beyond-prefix drift (any change at
or after the admitted ``byte_count``) is not compared here, so the CLI
caller must recompute evidence packets (whole-packet ``packet_sha256``)
before publishing. Lock parent-directory ownership is not validated,
only the lock file itself (no-follow open, regular file, owner match).
"""
from __future__ import annotations

import errno
import fcntl
import hashlib
import math
import os
import pwd
import re
import secrets
import shlex
import stat
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from . import checklist as checklist_api
from .agent_assessment import FAILED_PREFIX, SKIP_PREFIX
from .contracts import Problem
from .project_facts import check_facts, long_lines

# Literal mirror of agent_assessment.PTEST_ANSWER_PREFIX (T3 never imports
# it). Rows carrying it were answered without a model call.
PTEST_ANSWER_PREFIX = "Answered by ptest: "

# Checklist rows grouped under a visible "parallel safety" heading, with
# PARALLEL-001 (added by the deterministic-items task) trailing them.
# Single-sourced from checklist (the canonical catalog owner).
_PARALLEL_SAFETY_IDS = frozenset(checklist_api.PARALLEL_SAFETY_IDS)
_PARALLEL_ITEM_ID = checklist_api.PARALLEL_ITEM_ID

_PHASE = "publication"
_REPORT_NAME = "recommendations.md"
_MARKER_VERSION = 1
_MARKER_RE = re.compile(
    r"\A<!-- ptest-recommendations v(\d+) sha256=([0-9a-f]{64}) -->\n\Z")
_MAX_CHILDREN = 256
_MAX_PROSE_CHARS = 2048
_MAX_EVIDENCE_PER_ITEM = 16
_MAX_REPORT_BYTES = 1 << 20
_MAX_TARGET_BYTES = 1 << 20
# A complete bounded review can admit 64 files in each of 256 children.
MAX_SOURCE_PROOF_ENTRIES = _MAX_CHILDREN * 64
_MAX_SOURCE_PROOF_ENTRIES = MAX_SOURCE_PROOF_ENTRIES
_MAX_SOURCE_BYTES = 64 * 1024
_PATH_BYTES = 4096
_LOCK_TIMEOUT_S = 30.0
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_DRIVE_RE = re.compile(r"[A-Za-z]:")
_ROW_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")

_BIDI = frozenset({
    0x061C, 0x200E, 0x200F, 0x202A, 0x202B, 0x202C, 0x202D, 0x202E,
    0x2066, 0x2067, 0x2068, 0x2069,
})
_LINK_RE = re.compile(r"\[([^\]\n]*)\]\([^)\n]*\)")
_AUTOLINK_RE = re.compile(
    r"<(?:https?://[^<>\s]*|[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+)>")
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_HTML_TAG_RE = re.compile(r"</?[A-Za-z][^<>\n]*>")
_HEADLINE_RE = re.compile(r"(?m)^[ \t]*#{1,6}(?=\s|$)")

STATUSES = frozenset({"satisfied", "gap", "unknown", "not-applicable"})

FOOTER = (
    "> Ask your LLM to read this file, verify every cited claim against the current\n"
    "> source, implement only recommendations you approve, record the actual ptest\n"
    "> command/cwd/exit/output for each verification, and finish with `ptest --full`.\n"
)

_SENTINEL_FAMILIES = ("DB", "CACHE", "RESOURCE")


def _fail(code: str, message: str) -> None:
    raise Problem(code=code, message=message, phase=_PHASE, retryable=False)


def _clean(text: object, *, field: str, max_chars: int = _MAX_PROSE_CHARS) -> str:
    """Sanitize untrusted prose so it cannot alter Markdown structure."""
    if not isinstance(text, str):
        _fail("report-invalid", f"field {field!r} must be a string")
        raise AssertionError("unreachable")
    value = text.replace("\t", " ").replace("\r", "\n")
    value = "".join(
        char for char in value
        if char == "\n" or not (
            ord(char) < 0x20 or ord(char) == 0x7F
            or 0x80 <= ord(char) <= 0x9F or ord(char) in _BIDI))
    value = _HTML_COMMENT_RE.sub("", value)
    value = _LINK_RE.sub(r"\1", value)
    value = _AUTOLINK_RE.sub("", value)
    value = _HTML_TAG_RE.sub("", value)
    value = value.replace("<", "&lt;").replace(">", "&gt;")
    value = value.replace("|", "/").replace("`", "'")
    value = _HEADLINE_RE.sub("", value)
    value = re.sub(r"[ \u00a0]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value).strip()
    if len(value) > max_chars:
        value = value[:max_chars].rstrip() + " [truncated to 2048 characters]"
    return value


def _check_relpath(value: object, *, field: str,
                   allow_dot: bool = False) -> str:
    if not isinstance(value, str) or not value:
        _fail("report-invalid", f"field {field!r} must be a nonempty string")
        raise AssertionError("unreachable")
    if allow_dot and value == ".":
        return value
    if (value.startswith(("/", "\\")) or "\\" in value
            or value.startswith("~")):
        _fail("report-invalid", f"field {field!r} must be root-relative")
    if re.match(r"[A-Za-z]:", value):
        _fail("report-invalid", f"field {field!r} must be root-relative")
    for part in value.split("/"):
        if part in ("", ".", ".."):
            _fail("report-invalid", f"field {field!r} is not normalized")
    if ("`" in value or "|" in value or "\n" in value or "\r" in value
            or "\x00" in value):
        _fail("report-invalid",
              f"field {field!r} carries Markdown/command control text")
    if len(value.encode("utf-8")) > 4096:
        _fail("invalid-bound", f"field {field!r} exceeds its bound")
    return value


def _check_scope(value: object, *, field: str = "scope") -> str:
    """Doctor-safe scope validation; rendering quotes/escapes, never rejects.

    Accepts ``.`` (repository root) or any doctor-safe root-relative path:
    spaces and shell metacharacters are allowed and handled at render
    time by deterministic shell quoting (``shlex.quote``) and Markdown
    escaping of table cells/headings. Only actual controls (C0/DEL),
    traversal (empty/dot/dot-dot segments), absolute/Windows/drive
    forms, backslashes, and overlong values are rejected.
    """
    if value == ".":
        return "."
    if not isinstance(value, str) or not value:
        _fail("report-invalid", f"field {field!r} must be a nonempty string")
        raise AssertionError("unreachable")
    if (value.startswith(("/", "\\")) or "\\" in value
            or "\x00" in value or value.startswith("~")):
        _fail("report-invalid", f"field {field!r} must be root-relative")
    if _DRIVE_RE.match(value):
        _fail("report-invalid", f"field {field!r} must be root-relative")
    if _CONTROL_RE.search(value):
        _fail("report-invalid",
              f"field {field!r} carries control text")
    for part in value.split("/"):
        if part in ("", ".", ".."):
            _fail("report-invalid", f"field {field!r} is not normalized")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        _fail("report-invalid",
              f"field {field!r} is not encodable text")
        raise AssertionError("unreachable")
    if len(encoded) > _PATH_BYTES:
        _fail("invalid-bound", f"field {field!r} exceeds its bound")
    return value


def _shell_scope(scope: str) -> str:
    """Deterministic root-based argv display with safe quoting."""
    if scope == ".":
        return "ptest --full"
    return f"ptest {shlex.quote(scope)}"


def _md_scope(value: str) -> str:
    """Escape a validated scope for Markdown table cells and headings."""
    return (value.replace("\\", "\\\\").replace("|", "\\|")
            .replace("`", "\\`").replace("<", "&lt;")
            .replace(">", "&gt;"))


def _check_row_id(value: object, *, field: str) -> str:
    if (not isinstance(value, str) or not value
            or _ROW_ID_RE.match(value) is None):
        _fail("report-invalid", f"field {field!r} must be a safe row id")
    return value


def _check_hex(value: object, *, field: str, length: int) -> str:
    if (not isinstance(value, str) or len(value) != length
            or any(c not in "0123456789abcdef" for c in value)):
        _fail("report-invalid", f"field {field!r} must be {length} hex chars")
        raise AssertionError("unreachable")
    return value


def _as_mapping(value: object, *, field: str) -> Mapping:
    if not isinstance(value, Mapping):
        _fail("report-invalid", f"field {field!r} must be an object")
        raise AssertionError("unreachable")
    return value


def _citation_text(item: object) -> str:
    entry = _as_mapping(item, field="evidence[]")
    path = _check_relpath(entry.get("path"), field="evidence.path")
    start = entry.get("start_line")
    end = entry.get("end_line")
    if (isinstance(start, bool) or not isinstance(start, int) or start < 1
            or isinstance(end, bool) or not isinstance(end, int)
            or end < start):
        _fail("report-invalid", "evidence carries a bad line interval")
    sha = _check_hex(entry.get("sha256"), field="evidence.sha256", length=64)
    return f"`{path}` lines {start}-{end} (sha256:{sha})"


def _evidence_list(value: object, *, field: str, min_items: int) -> list:
    if not isinstance(value, list):
        _fail("report-invalid", f"field {field!r} must be a list")
        raise AssertionError("unreachable")
    if len(value) < min_items or len(value) > _MAX_EVIDENCE_PER_ITEM:
        _fail("report-invalid", f"field {field!r} has an invalid size")
    return value


_EXECUTION_STATUSES = ("executable", "caveat", "not-executable")


def _check_label(value: object, *, row_id: str):
    """Optional human row label: 1..64 bytes of plain text, no controls."""
    if value is None:
        return None
    if (not isinstance(value, str) or not value
            or len(value.encode("utf-8")) > 64
            or _CONTROL_RE.search(value)):
        _fail("report-invalid", f"row {row_id!r} has an invalid label")
        raise AssertionError("unreachable")
    return value


def _check_execution(value: object):
    """Optional public execution fact: exactly status, detail and fix."""
    if value is None:
        return None
    item = _as_mapping(value, field="execution")
    if set(item.keys()) != {"status", "detail", "fix"}:
        _fail("report-invalid",
              "execution must carry exactly status, detail and fix")
        raise AssertionError("unreachable")
    status = item.get("status")
    if status not in _EXECUTION_STATUSES:
        _fail("report-invalid", "execution status is unknown")
        raise AssertionError("unreachable")
    detail = item.get("detail")
    if (not isinstance(detail, str) or not detail
            or len(detail.encode("utf-8")) > 512):
        _fail("report-invalid",
              "execution detail must be 1..512 bytes of text")
        raise AssertionError("unreachable")
    fix = item.get("fix")
    if (fix is not None
            and (not isinstance(fix, str) or not fix
                 or len(fix.encode("utf-8")) > 512)):
        _fail("report-invalid",
              "execution fix must be null or 1..512 bytes of text")
        raise AssertionError("unreachable")
    return {"status": status, "detail": detail, "fix": fix}


def _check_rows(rows: object) -> list:
    if not isinstance(rows, list) or not rows:
        _fail("report-invalid", "child rows must be a nonempty list")
        raise AssertionError("unreachable")
    seen: set[str] = set()
    checked = []
    for entry in rows:
        item = _as_mapping(entry, field="rows[]")
        row_id = _check_row_id(item.get("id"), field="rows[].id")
        if row_id in seen:
            _fail("report-invalid", "child rows carry duplicate ids")
        seen.add(row_id)
        status = item.get("status")
        if status not in STATUSES:
            _fail("report-invalid", f"row {row_id!r} has an unknown status")
        rationale = _clean(item.get("rationale"), field=f"rows.{row_id}")
        if not rationale:
            _fail("report-invalid", f"row {row_id!r} needs a rationale")
        _evidence_list(item.get("evidence"), field=f"rows.{row_id}.evidence",
                       min_items=0)
        for citation in item["evidence"]:
            _citation_text(citation)
        dropped = item.get("dropped_citations", 0)
        if (isinstance(dropped, bool) or not isinstance(dropped, int)
                or dropped < 0):
            _fail("report-invalid",
                  f"row {row_id!r} has an invalid dropped_citations count")
        checked.append({"id": row_id, "status": status,
                        "rationale": rationale,
                        "evidence": list(item["evidence"]),
                        "dropped_citations": dropped,
                        "label": _check_label(item.get("label"),
                                             row_id=row_id)})
    return checked


def _finding_map(findings: object) -> dict:
    if not isinstance(findings, list):
        _fail("report-invalid", "child findings must be a list")
        raise AssertionError("unreachable")
    result = {}
    for entry in findings:
        item = _as_mapping(entry, field="findings[]")
        row_id = _check_row_id(item.get("id"), field="findings[].id")
        if row_id in result:
            _fail("report-invalid", "child findings carry duplicate ids")
        recipe = item.get("recipe_id")
        if recipe is not None and (not isinstance(recipe, str) or not recipe):
            _fail("report-invalid", "finding recipe_id must be a string or null")
        if recipe is not None and not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}",
                                                   recipe):
            _fail("report-invalid", "finding recipe_id is not a package name")
        _evidence_list(item.get("evidence"),
                       field=f"findings.{row_id}.evidence", min_items=1)
        for citation in item["evidence"]:
            _citation_text(citation)
        result[row_id] = {
            "summary": _clean(item.get("summary"),
                              field=f"findings.{row_id}.summary"),
            "suggested_change": _clean(item.get("suggested_change"),
                                       field=f"findings.{row_id}.change"),
            "recipe_id": recipe,
            "evidence": list(item["evidence"]),
        }
    return result


def _normalize_run(run: object) -> dict:
    data: object = run
    if hasattr(run, "data") and isinstance(getattr(run, "data"), dict):
        data = getattr(run, "data")
    elif hasattr(run, "data"):
        _fail("report-invalid", "run data must be an object or absent")
    if isinstance(data, Mapping):
        provider = data.get("provider", {})
        children = data.get("children", [])
        limitations = data.get("limitations", [])
    elif (hasattr(run, "provider") and hasattr(run, "children")):
        provider = getattr(run, "provider")
        children = getattr(run, "children")
        limitations = getattr(run, "limitations", [])
    else:
        raise TypeError(
            "run must be a PublicDocument (kind agent-assessment), a mapping "
            "with provider/children/limitations, or a duck-typed object "
            "exposing provider/children/limitations")
    provider = _as_mapping(provider, field="provider")
    name = _clean(provider.get("name", "unknown-reviewer"), field="provider")
    cli_version = _clean(provider.get("cli_version", "unknown"),
                         field="provider")
    profile = _clean(provider.get("profile", "unknown"), field="provider")
    if not isinstance(children, (list, tuple)):
        _fail("report-invalid", "run children must be a list")
        raise AssertionError("unreachable")
    if len(children) > _MAX_CHILDREN:
        _fail("invalid-bound", "run children exceed the 256-child bound")
    if not isinstance(limitations, (list, tuple)):
        _fail("report-invalid", "run limitations must be a list")
        raise AssertionError("unreachable")
    normalized_children = []
    for child in children:
        entry = _as_mapping(child, field="children[]")
        normalized_children.append({
            "project_id": _check_hex(entry.get("project_id"),
                                     field="project_id", length=32),
            "scope": _check_scope(entry.get("scope"), field="scope"),
            "packet_sha256": _check_hex(entry.get("packet_sha256"),
                                        field="packet_sha256", length=64),
            "rows": _check_rows(entry.get("rows")),
            "findings": _finding_map(entry.get("findings", [])),
            "limitations": _normalize_limitations(entry.get("limitations", [])),
            "execution": _check_execution(entry.get("execution")),
            # Invalid or empty facts fall back to execution downstream.
            "facts": (check_facts(entry.get("facts")) or None)
            if "facts" in entry else None,
        })
    return {"provider": {"name": name, "cli_version": cli_version,
                         "profile": profile},
            "children": normalized_children,
            "limitations": _normalize_limitations(limitations)}


def _normalize_limitations(value: object) -> list:
    if not isinstance(value, (list, tuple)):
        _fail("report-invalid", "limitations must be a list")
        raise AssertionError("unreachable")
    result = []
    for entry in value:
        item = _as_mapping(entry, field="limitations[]")
        code = item.get("code")
        if not isinstance(code, str) or not code:
            _fail("report-invalid", "limitation code must be nonempty")
        result.append({"code": _clean(code, field="limitation", max_chars=128),
                       "message": _clean(item.get("message", ""),
                                         field="limitation"),
                       "paths": [_check_relpath(p, field="limitation.paths",
                                                  allow_dot=True)
                                 for p in (item.get("paths", [])
                                           if isinstance(item.get("paths", []),
                                                         list) else [])]})
    return result


def _group_limitations(limitations: list) -> list[dict]:
    """Merge repeated limitation prose while retaining every root path."""
    grouped = {}
    for limitation in limitations:
        key = (limitation["code"], limitation["message"])
        entry = grouped.setdefault(key, {"paths": [], "unscoped": False})
        paths = limitation["paths"]
        if not paths:
            entry["unscoped"] = True
        for path in paths:
            if path not in entry["paths"]:
                entry["paths"].append(path)
    return [
        {"code": code, "message": message, **details}
        for (code, message), details in grouped.items()
    ]


def _limitation_line(limitation: dict) -> str:
    line = f"- {limitation['code']}: {limitation['message']}"
    if limitation["paths"]:
        paths = ", ".join(f"`{_md_scope(path)}`"
                           for path in limitation["paths"])
        line += f" (root-relative paths: {paths})"
    return line


def _score_text(rows: list) -> str:
    applicable = sum(1 for row in rows
                     if row["status"] != "not-applicable")
    if applicable == 0:
        return "no score (every row is not-applicable)"
    satisfied = sum(1 for row in rows if row["status"] == "satisfied")
    return f"{satisfied} of {applicable} checks confirmed from evidence"


def _fact_bullets(child: dict) -> list[str]:
    """Plain-language fact bullets; execution fallback when facts invalid."""
    facts = child.get("facts")
    if facts:
        # Facts arrive validated, but they still carry untrusted
        # config-derived prose: clean every bullet so it cannot alter
        # the Markdown structure (links, tags, code spans, headings).
        return [f"- {_clean(line, field='facts')}"
                for line in long_lines(facts)]
    execution = child.get("execution")
    if execution is None:
        return ["- runs: not recorded"]
    detail = _clean(execution["detail"], field="execution", max_chars=512)
    fix = execution["fix"]
    if execution["status"] == "not-executable":
        if fix is None:
            return [f"- runs: no — {detail}"]
        return [f"- runs: no — {detail} → "
                f"{_clean(fix, field='execution', max_chars=512)}"]
    return ["- runs: yes"]


def _parallel_safety_heading() -> str:
    return ("## Parallel safety\n\n"
            "Isolation checks that decide whether tests can run side by "
            "side; PARALLEL-001 below rolls them up into the parallel "
            "verdict.\n")


def _verify_block(root_label: str, verification_scope: str | None) -> str:
    # ``verification_scope`` is private routing context supplied only after
    # CLI validation. Display scope alone never authorizes a command.
    argv = ("ptest --full" if verification_scope is None
            else _shell_scope(verification_scope))
    if verification_scope is None:
        scope_note = "repository root full suite (no safe narrower route)"
    else:
        scope_note = ("repository root scope" if verification_scope == "."
                      else f"scope {_md_scope(verification_scope)}")
    return (
        f"Verify with ptest only (deterministic, root-based; never a "
        f"model-provided shell command):\n"
        f"- command ({scope_note}):\n"
        f"      {argv}\n"
        f"- cwd: repository root ({root_label})\n"
        f"- prerequisites: clean checkout; packaged recipe available with the "
        f"ptest installation\n"
        f"- expected: exit 0; cited assertions unchanged; test inventory "
        f"identical; coverage behavior preserved; neighbor sentinel preserved "
        f"where applicable\n"
        f"- Observed command: (blank)\n"
        f"- Observed cwd: (blank)\n"
        f"- Observed exit status: (blank)\n"
        f"- Observed output: (blank)\n"
        f"- Status: unverified\n")


def _recipe_line(recipe_id: str | None) -> str:
    if recipe_id is None:
        return ("Canonical recipe: none (this criterion has no packaged "
                "recipe; follow the specific change below).")
    return (f"Canonical recipe: `recipes/{recipe_id}.md` (packaged with "
            f"ptest; reuse it, do not invent tooling).")


def _item_heading(row: dict) -> str:
    """One ``## <ID> <label>`` heading per checklist item."""
    row_id = row["id"]
    label = row.get("label")
    if label:
        safe = _clean(label, field=f"rows.{row_id}.label", max_chars=64)
        return f"## {row_id} {safe}"
    return f"## {row_id}"


def _dropped_line(row: dict) -> str | None:
    """One report line naming the item's dropped invalid-citation count."""
    dropped = row.get("dropped_citations", 0)
    if not dropped:
        return None
    noun = "citation" if dropped == 1 else "citations"
    return f"{dropped} invalid {noun} dropped."


def _strip_answer_prefix(rationale: str) -> tuple[str, bool]:
    """Rationale without its review-flow prefix; bool is answered by ptest."""
    if rationale.startswith(PTEST_ANSWER_PREFIX):
        return rationale[len(PTEST_ANSWER_PREFIX):], True
    if rationale.startswith(FAILED_PREFIX):
        return ("review failed: " + rationale[len(FAILED_PREFIX):]), False
    if rationale.startswith(SKIP_PREFIX):
        return rationale[len(SKIP_PREFIX):], False
    return rationale, False


def _status_section(row: dict) -> str:
    """Compact report section for one non-gap row."""
    status = row["status"]
    rationale = row["rationale"]
    reason, by_ptest = _strip_answer_prefix(rationale)
    lines = [_item_heading(row), ""]
    dropped = _dropped_line(row)
    if dropped is not None:
        lines.append(dropped)
        lines.append("")
    if status == "satisfied":
        lines.append("Status: satisfied.")
        lines.append("")
    elif status == "unknown":
        lines.append("Status: unknown.")
        lines.append("")
        lines.append(f"Reason: {reason}")
        lines.append("")
    elif rationale.startswith(SKIP_PREFIX):
        reason = rationale[len(SKIP_PREFIX):]
        lines.append("Status: not applicable (skipped without a model call).")
        lines.append("")
        lines.append(f"Reason: {reason}")
        lines.append("")
    elif rationale.startswith(PTEST_ANSWER_PREFIX):
        lines.append("Status: not applicable.")
        lines.append("")
        lines.append(f"Reason: {reason}")
        lines.append("")
    else:
        lines.append("Status: not applicable.")
        lines.append("")
        lines.append(f"Rationale: {rationale}")
        lines.append("")
    if by_ptest:
        lines.append("Source: ptest configuration/history fact "
                     "(not execution proof).")
        lines.append("")
    elif rationale.startswith(SKIP_PREFIX):
        lines.append("Source: ptest dependency fact (not execution proof).")
        lines.append("")
    elif rationale.startswith(FAILED_PREFIX):
        lines.append("Source: provider response unavailable; no review "
                     "conclusion was produced.")
        lines.append("")
    else:
        lines.append("Source: model review — not execution proof.")
        lines.append("")
    if row["evidence"]:
        lines.append("Evidence:")
        lines.append("")
        for citation in row["evidence"]:
            lines.append(f"- {_citation_text(citation)}")
        if status == "satisfied":
            lines.append(f"- Why it matters: {reason}")
        lines.append("")
    return "\n".join(lines)


def _finding_section(row: dict, finding: dict | None, root_label: str,
                     verification_scope: str | None) -> str:
    row_id = row["id"]
    rationale, by_ptest = _strip_answer_prefix(row["rationale"])
    lines = [_item_heading(row), ""]
    dropped = _dropped_line(row)
    if dropped is not None:
        lines.append(dropped)
        lines.append("")
    citations = row["evidence"] if finding is None else finding["evidence"]
    if finding is None:
        lines.append(f"No finding was supplied for gap `{row_id}`. This item "
                     "remains unresolved and unverified.")
        lines.append("")
        lines.append("Bounded evidence-gathering next step: re-run the "
                     "consented review for this scope so the reviewer can "
                     "supply a finding, or gather the cited excerpts below "
                     "and propose a change through the normal review flow. "
                     "Keep this item unverified until then.")
        lines.append("")
        why = rationale if by_ptest else row["rationale"]
    else:
        source = ("ptest configuration/history fact (not execution proof)"
                  if by_ptest else "model review — not execution proof")
        lines.append(f"{source}: {finding['summary']}")
        lines.append("")
        why = rationale if by_ptest else row["rationale"]
    lines.append("Evidence (exact collected identity and why it matters):")
    lines.append("")
    for citation in citations:
        lines.append(f"- {_citation_text(citation)}")
    lines.append(f"- Why it matters: {why}")
    lines.append("")
    if finding is None:
        lines.append("Specific change: pending reviewer finding "
                     "(see next step above).")
        lines.append("")
        lines.append(_recipe_line(None))
    else:
        lines.append(f"Specific change: {finding['suggested_change']}")
        lines.append("")
        lines.append(_recipe_line(finding["recipe_id"]))
    lines.append("")
    lines.append("Preserve: every existing assertion, coverage behavior, and "
                 "the full test inventory. Scoped ptest must show the same "
                 "test inventory before and after the change.")
    lines.append("")
    catalog_entry = next((entry for entry in checklist_api.CATALOG
                          if entry.id == row_id), None)
    if catalog_entry is not None:
        lines.append("Verification focus (catalog; not run): "
                     + catalog_entry.verification)
    lines.append("")
    lines.append(_verify_block(root_label, verification_scope))
    return "\n".join(lines)


def _sentinel_section(children: list) -> str:
    wanted = [row_id for child in children for row in child["rows"]
              for row_id in (row["id"],)
              if row_id.split("-", 1)[0] in _SENTINEL_FAMILIES
              and row["status"] == "gap"]
    lines = ["## Isolation sentinels (worker and run ownership)", ""]
    if not wanted:
        lines.append("No database, cache, or writable-file gaps in this "
                     "review; no sentinel proof required.")
        lines.append("")
        return "\n".join(lines)
    lines.append("A naming convention alone is not proof. For each affected "
                 "area below, use an executable sentinel design across both "
                 "workers and concurrent runs:")
    lines.append("")
    lines.append("- create one sentinel owned by a neighbor run/worker "
                 "(database/schema, cache key, or writable file);")
    lines.append("- attempt cross-owner reads, overwrites, and deletes from "
                 "the test owner and assert each attempt is refused or "
                 "scoped to owned names only;")
    lines.append("- run the owned teardown/cleanup and assert the neighbor "
                 "sentinel still exists with identical bytes;")
    lines.append("- run the checks through supported ptest, using a validated "
                 "root-relative path for a narrow finding or `ptest --full` "
                 "for a whole-child finding.")
    lines.append("")
    lines.append(f"Affected criteria: {', '.join(sorted(set(wanted)))}.")
    lines.append("")
    lines.append("Where evidence or a runnable target is unavailable, say so "
                 "in the finding, give a bounded evidence-gathering next "
                 "step, and keep that item unverified rather than inventing "
                 "a file citation or command.")
    lines.append("")
    return "\n".join(lines)


def render_recommendations(
        run: object, *,
        verification_scopes: tuple[str | None, ...] | None = None) -> bytes:
    """Render the report with only caller-validated command routes.

    The CLI supplies ``verification_scopes`` after checking each route
    against the real dispatcher. Without that private routing context, the
    report uses the safe root ``ptest --full`` command.
    """
    if run is None or isinstance(run, (bytes, str, int, float, bool)):
        raise TypeError(
            "run must be a PublicDocument (kind agent-assessment), a mapping "
            "with provider/children/limitations, or a duck-typed object "
            "exposing provider/children/limitations")
    normalized = _normalize_run(run)
    provider = normalized["provider"]
    children = normalized["children"]
    if verification_scopes is None:
        safe_scopes = (None,) * len(children)
    else:
        if (not isinstance(verification_scopes, tuple)
                or len(verification_scopes) != len(children)):
            _fail("report-invalid",
                  "verification routes must match the reviewed children")
        safe_scopes = tuple(
            None if scope is None else _check_scope(
                scope, field="verification route")
            for scope in verification_scopes)
    out: list[str] = []
    out.append("# Test-quality recommendations (agent-reviewed)")
    out.append("")
    out.append(f"Reviewer: {provider['name']} (profile {provider['profile']}, "
               f"cli {provider['cli_version']}). Execution verification: "
               f"not run; review conclusions are not execution proof.")
    out.append("")
    out.append("Scope / packet / checklist (recomputed; model scores are "
               "never used):")
    out.append("")
    run_limitations = _group_limitations(normalized["limitations"])
    if run_limitations:
        out.append("Review limitations:")
        out.append("")
        for limitation in run_limitations:
            out.append(_limitation_line(limitation))
        out.append("")
    out.append("| Scope | Packet sha256 | Checklist |")
    out.append("| --- | --- | --- |")
    for child in children:
        out.append(f"| {_md_scope(child['scope'])} | "
                   f"{child['packet_sha256']} | "
                   f"{_score_text(child['rows'])} |")
    out.append("")
    for child_index, child in enumerate(children):
        out.append(f"## Scope {_md_scope(child['scope'])}")
        out.append("")
        out.extend(_fact_bullets(child))
        out.append("")
        out.append(f"Packet: {child['packet_sha256']}. Checklist: "
                   f"{_score_text(child['rows'])}.")
        out.append("")
        scope_limitations = []
        for limitation in _group_limitations(child["limitations"]):
            aggregate = next((item for item in run_limitations
                              if item["code"] == limitation["code"]
                              and item["message"] == limitation["message"]),
                             None)
            if aggregate is None:
                scope_limitations.append(limitation)
            elif limitation["paths"]:
                if not set(limitation["paths"]).issubset(aggregate["paths"]):
                    scope_limitations.append(limitation)
            elif not aggregate["unscoped"]:
                scope_limitations.append(limitation)
        if scope_limitations:
            out.append("Scope limitations:")
            out.append("")
            for limitation in scope_limitations:
                out.append(_limitation_line(limitation))
            out.append("")
        # Partition rows the same way the terminal renderer does: main
        # rows first, then the eight isolation items as a visible
        # "parallel safety" group with PARALLEL-001 trailing it. Never
        # group by first-seen order: catalog order puts SELECT-001 and
        # TIMING-001 before the safety rows, and they are not isolation
        # checks.
        def _emit(row: dict) -> None:
            if row["status"] != "gap":
                out.append(_status_section(row))
                out.append("")
                return
            out.append(_finding_section(
                row, child["findings"].get(row["id"]),
                root_label="repository root",
                verification_scope=safe_scopes[child_index]))
            out.append("")

        main = [row for row in child["rows"]
                if row["id"] not in _PARALLEL_SAFETY_IDS
                and row["id"] != _PARALLEL_ITEM_ID]
        safety = [row for row in child["rows"]
                  if row["id"] in _PARALLEL_SAFETY_IDS]
        parallel = [row for row in child["rows"]
                    if row["id"] == _PARALLEL_ITEM_ID]
        for row in main:
            _emit(row)
        if safety or parallel:
            out.append(_parallel_safety_heading())
            out.append("")
            for row in safety + parallel:
                _emit(row)
    out.append(_sentinel_section(children))
    out.append("## Root-monorepo live probe limitation")
    out.append("")
    out.append("Root-monorepo live --probe permutations are unsupported: the "
               "root dispatcher supports static doctor review only. Do not "
               "invent a root probe flag. Configured worker settings are "
               "reported as ptest facts; this static review does not prove "
               "parallel isolation. Verify ordinary tests through the "
               "validated ptest route above, then finish with the final gate.")
    out.append("")
    out.append("## Final gate")
    out.append("")
    out.append("Finish with `ptest --full` from the repository root after "
               "every approved recommendation is implemented and its scoped "
               "verification recorded above.")
    out.append("")
    out.append(FOOTER.strip("\n"))
    out.append("")
    body = ("\n".join(out)).encode("utf-8")
    full = _marker_for(body) + body
    if len(full) > _MAX_REPORT_BYTES + 256:
        _fail("invalid-bound", "rendered report exceeds its bound")
    return full


@dataclass(frozen=True, kw_only=True)
class PublishedIdentity:
    """Evidence identity of a previously published report."""

    sha256: str
    st_dev: int
    st_ino: int
    size: int

    def __post_init__(self) -> None:
        if (not isinstance(self.sha256, str) or len(self.sha256) != 64
                or any(c not in "0123456789abcdef"
                       for c in self.sha256)):
            raise TypeError("sha256 must be 64 lowercase hex chars")
        for field in ("st_dev", "st_ino", "size"):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{field} must be an int")


@dataclass(frozen=True, kw_only=True)
class PublishResult:
    """Outcome of one guarded report publication."""

    status: str
    path: str
    sha256: str

    def __post_init__(self) -> None:
        if self.status not in ("created", "replaced", "unchanged"):
            raise TypeError("status must be created, replaced or unchanged")
        if self.path != _REPORT_NAME:
            raise TypeError("path must be recommendations.md")
        if (not isinstance(self.sha256, str) or len(self.sha256) != 64
                or any(c not in "0123456789abcdef"
                       for c in self.sha256)):
            raise TypeError("sha256 must be 64 lowercase hex chars")


def _split_marker(payload: bytes) -> tuple[str, bytes]:
    if not isinstance(payload, (bytes, bytearray)):
        raise TypeError("payload must be bytes")
    raw = bytes(payload)
    if len(raw) > _MAX_REPORT_BYTES + 256:
        _fail("invalid-bound", "payload exceeds its bound")
    newline = raw.find(b"\n")
    if newline < 0:
        _fail("report-invalid", "payload carries no marker line")
    first = raw[:newline + 1]
    try:
        text = first.decode("utf-8")
    except UnicodeDecodeError:
        _fail("report-invalid", "payload marker is not UTF-8")
        raise AssertionError("unreachable")
    match = _MARKER_RE.match(text)
    if match is None:
        _fail("report-invalid", "payload carries no recognized marker")
    if match.group(1) != str(_MARKER_VERSION):
        _fail("report-invalid", "payload marker version is unsupported")
    return match.group(2), raw[newline + 1:]


def _marker_for(body: bytes) -> bytes:
    digest = hashlib.sha256(body).hexdigest()
    return (f"<!-- ptest-recommendations v{_MARKER_VERSION} "
            f"sha256={digest} -->\n").encode("utf-8")


def _lock_path_for(root: Path) -> Path | None:
    """Resolve the account-local root-keyed lock path, if available."""
    override = os.environ.get("PTEST_RECOMMENDATIONS_LOCK_DIR")
    if override:
        candidate = Path(override)
        try:
            candidate.mkdir(parents=True, exist_ok=True)
        except OSError:
            return None
        base = candidate
    else:
        try:
            home = Path(pwd.getpwuid(os.getuid()).pw_dir)
            base = home / ".local" / "state" / "ptest" / "coordination"
            (base / "recommendation-locks").mkdir(parents=True, exist_ok=True)
            base = base / "recommendation-locks"
        except (KeyError, OSError):
            return None
    try:
        stamp = os.lstat(root)
    except OSError:
        return None
    return base / f"rec-{stamp.st_dev:x}-{stamp.st_ino:x}.lock"


class _CooperativeLock:
    """Fail-closed exclusive lock shared by cooperating ptest writers.

    An unavailable lock path or lock open never degrades to unlocked
    publication: it raises ``coordinator-unavailable`` (or
    ``report-conflict`` for a symlinked/foreign-owned lock file) before
    any report state is touched.
    """

    def __init__(self, path: Path | None,
                 deadline: float | None = None) -> None:
        self._path = path
        self._deadline = deadline
        self._fd: int | None = None

    def _abort(self, code: str, message: str) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
        _fail(code, message)
        raise AssertionError("unreachable")

    def __enter__(self) -> "_CooperativeLock":
        if self._path is None:
            _fail("coordinator-unavailable",
                  "recommendation lock is unavailable; refusing to publish "
                  "without serialization")
            raise AssertionError("unreachable")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            self._abort("coordinator-unavailable",
                        "recommendation lock directory is unavailable; "
                        "refusing to publish without serialization")
        try:
            self._fd = os.open(self._path,
                               os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        except OSError as exc:
            self._fd = None
            if exc.errno == errno.ELOOP:
                _fail("report-conflict",
                      "recommendation lock is a symlink; refusing to "
                      "follow it")
                raise AssertionError("unreachable")
            _fail("coordinator-unavailable",
                  "recommendation lock cannot be opened; refusing to "
                  "publish without serialization")
            raise AssertionError("unreachable")
        try:
            stamp = os.fstat(self._fd)
        except OSError:
            self._abort("coordinator-unavailable",
                        "recommendation lock cannot be inspected; refusing "
                        "to publish without serialization")
        if not stat.S_ISREG(stamp.st_mode):
            self._abort("report-conflict",
                        "recommendation lock is not a regular file")
        if stamp.st_uid != os.geteuid():
            self._abort("report-conflict",
                        "recommendation lock has a foreign owner")
        lock_deadline = time.monotonic() + _LOCK_TIMEOUT_S
        if self._deadline is not None:
            if time.monotonic() >= self._deadline:
                self._abort("review-timeout",
                            "total review deadline expired before report publication")
            lock_deadline = min(lock_deadline, self._deadline)
        while True:
            now = time.monotonic()
            if now >= lock_deadline:
                if (self._deadline is not None
                        and now >= self._deadline):
                    self._abort(
                        "review-timeout",
                        "total review deadline expired waiting for report lock")
                self._abort("coordinator-unavailable",
                            "recommendation publishers are busy")
            try:
                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                if (self._deadline is not None
                        and time.monotonic() >= self._deadline):
                    self._abort(
                        "review-timeout",
                        "total review deadline expired acquiring report lock")
                return self
            except BlockingIOError:
                now = time.monotonic()
                if now >= lock_deadline:
                    if (self._deadline is not None
                            and now >= self._deadline):
                        self._abort(
                            "review-timeout",
                            "total review deadline expired waiting for report lock")
                    self._abort("coordinator-unavailable",
                                "recommendation publishers are busy")
                time.sleep(min(0.01, lock_deadline - now))

    def __exit__(self, *args: object) -> None:
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None


def _open_root(root: object) -> int:
    if not isinstance(root, (str, Path)):
        raise TypeError("root must be a path")
    anchor = Path(root)
    try:
        fd = os.open(anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        _fail("state-unavailable", "publication root does not exist")
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            _fail("report-conflict", "publication root is a symlink")
        _fail("unsafe-path", "publication root is not a usable directory")
    stamp = os.fstat(fd)
    if not stat.S_ISDIR(stamp.st_mode):
        os.close(fd)
        _fail("unsafe-path", "publication root is not a directory")
    return fd


def _read_target(root_fd: int) -> tuple[bytes, int, int, int] | None:
    """No-follow descriptor-relative read; None when the target is absent."""
    try:
        fd = os.open(_REPORT_NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                     dir_fd=root_fd)
    except FileNotFoundError:
        return None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            _fail("report-conflict",
                  "existing recommendations.md is a symlink; refusing to "
                  "follow it")
        _fail("report-conflict",
              "existing recommendations.md cannot be opened safely")
    try:
        stamp = os.fstat(fd)
        if not stat.S_ISREG(stamp.st_mode):
            _fail("report-conflict",
                  "existing recommendations.md is not a regular file")
        if stamp.st_uid != os.geteuid():
            _fail("report-conflict",
                  "existing recommendations.md has a foreign owner")
        chunks = []
        remaining = _MAX_TARGET_BYTES + 1
        while remaining > 0:
            try:
                piece = os.read(fd, min(8192, remaining))
            except OSError:
                _fail("report-conflict",
                      "existing recommendations.md is unreadable")
            if not piece:
                break
            chunks.append(piece)
            remaining -= len(piece)
        raw = b"".join(chunks)
        if len(raw) > _MAX_TARGET_BYTES:
            _fail("report-conflict",
                  "existing recommendations.md exceeds its bound")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            _fail("report-conflict",
                  "existing recommendations.md is not UTF-8")
        match = _MARKER_RE.match(text.split("\n", 1)[0] + "\n"
                                 if "\n" in text else "")
        if match is None:
            _fail("report-conflict",
                  "existing recommendations.md is custom (no recognized "
                  "ptest marker); refusing to clobber it")
        if match.group(1) != str(_MARKER_VERSION):
            _fail("report-conflict",
                  "existing recommendations.md has an unsupported marker "
                  "version")
        body = raw[raw.find(b"\n") + 1:]
        if hashlib.sha256(body).hexdigest() != match.group(2):
            _fail("report-conflict",
                  "existing recommendations.md was edited or changed "
                  "concurrently (marker hash mismatch); refusing to clobber "
                  "it")
        return raw, stamp.st_dev, stamp.st_ino, len(raw)
    finally:
        os.close(fd)


def _check_previous(previous: object,
                    current: tuple[bytes, int, int, int] | None) -> None:
    """Fail closed when caller evidence identity no longer matches."""
    if previous is None:
        return
    if not isinstance(previous, PublishedIdentity):
        raise TypeError("previous must be PublishedIdentity or None")
    if current is None:
        _fail("stale-evidence",
              "the previously published report is gone; refusing to publish "
              "against stale source identity")
    raw, dev, ino, size = current
    if (previous.sha256 != hashlib.sha256(raw).hexdigest()
            or previous.st_dev != dev or previous.st_ino != ino
            or previous.size != size):
        _fail("stale-evidence",
              "the report changed since the caller read it (marker, hash, "
              "inode identity, or bytes differ); refusing to publish "
              "against stale source identity")


def _stage_temp(root_fd: int, full: bytes) -> str:
    name = f"{_REPORT_NAME}.tmp.{os.getpid()}.{secrets.token_hex(8)}"
    try:
        fd = os.open(name,
                     os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o644, dir_fd=root_fd)
    except OSError:
        _fail("state-unavailable", "cannot stage the report temp file")
        raise AssertionError("unreachable")
    try:
        view = memoryview(full)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fchmod(fd, 0o644)
        os.fsync(fd)
        os.close(fd)
        return name
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(name, dir_fd=root_fd)
        except OSError:
            pass
        raise


def _check_source_proof(value: object) -> list:
    """Validate the required bounded source-proof list shape.

    Each entry binds one collected source file: its root-relative
    ``path``, the excerpt-chunk ``sha256`` observed at collection time
    (SHA-256 of exactly the first ``byte_count`` bytes of the file,
    which is the whole file when it fits, matching
    ``SourceExcerpt.sha256`` over ``SourceExcerpt.text.encode()``),
    the exact admitted ``byte_count`` (0..``_MAX_SOURCE_BYTES``), and
    the cited ``start_line``/``end_line`` interval. Malformed proof is
    ``report-invalid`` (``invalid-bound`` for an out-of-range count or
    oversize list); drift detected later is ``stale-evidence``.
    """
    if not isinstance(value, (list, tuple)):
        raise TypeError("source_proof must be a list of mappings or None")
    if len(value) > _MAX_SOURCE_PROOF_ENTRIES:
        _fail("invalid-bound", "source proof exceeds its entry bound")
        raise AssertionError("unreachable")
    checked = []
    for entry in value:
        item = _as_mapping(entry, field="source_proof[]")
        path = _check_relpath(item.get("path"), field="source_proof.path")
        sha = _check_hex(item.get("sha256"), field="source_proof.sha256",
                         length=64)
        count = item.get("byte_count")
        if isinstance(count, bool) or not isinstance(count, int):
            _fail("report-invalid",
                  "source proof byte_count must be an int")
            raise AssertionError("unreachable")
        if count < 0 or count > _MAX_SOURCE_BYTES:
            _fail("invalid-bound",
                  "source proof byte_count outside 0..64KiB")
            raise AssertionError("unreachable")
        start = item.get("start_line")
        end = item.get("end_line")
        if (isinstance(start, bool) or not isinstance(start, int)
                or start < 1 or isinstance(end, bool)
                or not isinstance(end, int) or end < start):
            _fail("report-invalid",
                  "source proof carries a bad line interval")
        checked.append({"path": path, "sha256": sha,
                        "byte_count": count,
                        "start_line": start, "end_line": end})
    return checked


def _open_source_leaf(root_fd: int, path: str) -> int:
    """Open a proof path without following any symlink component.

    Each parent component is walked through a no-follow directory
    descriptor, so a swapped-in symlinked parent (not just a symlinked
    leaf) fails closed with ``stale-evidence`` instead of redirecting
    the recheck into attacker content. Returns the open leaf fd.
    """
    parts = path.split("/")
    owned: list[int] = []
    ancestor_fd = root_fd
    try:
        for part in parts[:-1]:
            try:
                child = os.open(part,
                                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                dir_fd=ancestor_fd)
            except FileNotFoundError:
                _fail("stale-evidence",
                      f"source {path!r} is gone since collection; refusing "
                      f"to publish against stale evidence")
                raise AssertionError("unreachable")
            except OSError as exc:
                if exc.errno == errno.ELOOP:
                    _fail("stale-evidence",
                          f"source {path!r} passes through a symlink; "
                          f"refusing to publish against stale evidence")
                    raise AssertionError("unreachable")
                _fail("stale-evidence",
                      f"source {path!r} cannot be opened safely; refusing "
                      f"to publish against stale evidence")
                raise AssertionError("unreachable")
            owned.append(child)
            ancestor_fd = child
        try:
            return os.open(parts[-1],
                           os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                           dir_fd=ancestor_fd)
        except FileNotFoundError:
            _fail("stale-evidence",
                  f"source {path!r} is gone since collection; refusing to "
                  f"publish against stale evidence")
            raise AssertionError("unreachable")
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                _fail("stale-evidence",
                      f"source {path!r} is a symlink; refusing to publish "
                      f"against stale evidence")
                raise AssertionError("unreachable")
            _fail("stale-evidence",
                  f"source {path!r} cannot be opened safely; refusing to "
                  f"publish against stale evidence")
            raise AssertionError("unreachable")
    finally:
        for owned_fd in owned:
            try:
                os.close(owned_fd)
            except OSError:
                pass


def _verify_source_proof(root_fd: int, proof: list) -> None:
    """No-follow recheck of collected source identities.

    Every entry is opened via a descriptor walk that follows no
    symlink at any path component, and exactly its admitted prefix
    (the first ``byte_count`` bytes, 0..``_MAX_SOURCE_BYTES``) is
    compared against the collection-time excerpt-chunk sha256
    alongside the cited line bound over the decoded admitted prefix
    only. The rest of the file is never read or decoded, so sources
    larger than 1 MiB stay publishable and invalid UTF-8 beyond the
    prefix is irrelevant. Any absence, symlink (leaf or parent
    component), type change, prefix drift, non-UTF-8 prefix, or line
    bound beyond the admitted prefix raises ``stale-evidence``; the
    caller preserves the prior report. Beyond-prefix drift is
    unobservable here and must be covered by whole-packet identity
    before publishing.
    """
    if not proof:
        return
    for entry in proof:
        path = entry["path"]
        count = entry["byte_count"]
        fd = _open_source_leaf(root_fd, path)
        try:
            stamp = os.fstat(fd)
            if not stat.S_ISREG(stamp.st_mode):
                _fail("stale-evidence",
                      f"source {path!r} is no longer a regular file; "
                      f"refusing to publish against stale evidence")
            chunks = []
            remaining = count
            while remaining > 0:
                try:
                    piece = os.read(fd, min(8192, remaining))
                except OSError:
                    _fail("stale-evidence",
                          f"source {path!r} is unreadable; refusing to "
                          f"publish against stale evidence")
                    raise AssertionError("unreachable")
                if not piece:
                    break
                chunks.append(piece)
                remaining -= len(piece)
            admitted = b"".join(chunks)
            if hashlib.sha256(admitted).hexdigest() != entry["sha256"]:
                _fail("stale-evidence",
                      f"source {path!r} changed since collection "
                      f"(admitted prefix bytes differ); refusing to "
                      f"publish against stale evidence")
            try:
                text = admitted.decode("utf-8")
            except UnicodeDecodeError:
                _fail("stale-evidence",
                      f"source {path!r} admitted prefix is not UTF-8; "
                      f"refusing to publish against stale evidence")
                raise AssertionError("unreachable")
            nlines = text.count("\n") + (
                0 if (not text or text.endswith("\n")) else 1)
            if entry["end_line"] > nlines:
                _fail("stale-evidence",
                      f"source {path!r} shrank since collection (line "
                      f"bound {entry['end_line']} beyond {nlines} lines); "
                      f"refusing to publish against stale evidence")
        finally:
            os.close(fd)


def _read_target_raw(root_fd: int) -> bytes | None:
    """No-follow raw read of the live report; None when unreadable.

    Unlike ``_read_target`` this performs no marker validation: the
    rollback path must compare against whatever is on disk (including
    an intervening custom report) rather than fail on it. Never raises.
    """
    try:
        fd = os.open(_REPORT_NAME,
                     os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                     dir_fd=root_fd)
    except OSError:
        return None
    try:
        stamp = os.fstat(fd)
        if not stat.S_ISREG(stamp.st_mode):
            return None
        chunks = []
        remaining = _MAX_TARGET_BYTES + 1
        while remaining > 0:
            try:
                piece = os.read(fd, min(8192, remaining))
            except OSError:
                return None
            if not piece:
                break
            chunks.append(piece)
            remaining -= len(piece)
        raw = b"".join(chunks)
        if len(raw) > _MAX_TARGET_BYTES:
            return None
        return raw
    except OSError:
        return None
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _restore_after_sync_failure(
        root_fd: int,
        current: tuple[bytes, int, int, int] | None,
        written: bytes) -> bool:
    """Best-effort restore and confirmation of the prior complete report.

    The live target is reread and compared before any restore: when an
    intervening edit landed after the atomic rename (custom report,
    newer publication), it is preserved instead of being clobbered by
    the rollback. Only when the disk still holds exactly the bytes this
    call wrote is the prior report restored.
    """
    try:
        live = _read_target_raw(root_fd)
        if live != written:
            return True
        if current is None:
            try:
                os.unlink(_REPORT_NAME, dir_fd=root_fd)
            except OSError:
                return False
        else:
            try:
                name = _stage_temp(root_fd, current[0])
            except BaseException:
                return False
            try:
                os.rename(name, _REPORT_NAME,
                          src_dir_fd=root_fd, dst_dir_fd=root_fd)
            except BaseException:
                return False
            finally:
                try:
                    os.unlink(name, dir_fd=root_fd)
                except OSError:
                    pass
        try:
            os.fsync(root_fd)
        except OSError:
            return False
        restored = _read_target_raw(root_fd)
        return restored is None if current is None else restored == current[0]
    except BaseException:
        return False


def _check_deadline(deadline: float | None) -> None:
    if deadline is not None and time.monotonic() >= deadline:
        _fail("review-timeout",
              "total review deadline expired during report publication")


def publish_recommendations(root: object, payload: object,
                            previous: object,
                            *, source_proof: object = None,
                            deadline: float | None = None) -> PublishResult:
    """Publish one marker-headed report with ownership checks.

    ``payload`` must be ``render_recommendations`` bytes (marker line plus
    body whose SHA-256 matches the marker). ``previous`` is an optional
    ``PublishedIdentity`` from an earlier read; when given, any drift in
    marker/hash/inode/bytes raises ``stale-evidence`` and the prior report
    is preserved. ``source_proof`` is an optional bounded list of at most
    16,384 collected source identities (256 children times 64 files each;
    ``path``/``sha256``/``start_line``/
    ``end_line``); each entry is rechecked with a no-follow read before
    staging and again immediately before rename, and any drift raises
    ``stale-evidence`` with the prior report preserved. A prior-report
    identity is never treated as source identity: with
    ``source_proof=None`` no source-drift claim is made. Custom, edited,
    symlinked, non-regular, or unreadable targets raise
    ``report-conflict`` and are never clobbered. A parent-directory fsync
    failure raises ``state-unavailable`` after a best-effort restore of
    the prior complete report. ``deadline`` is an optional absolute
    ``time.monotonic()`` deadline; it bounds lock waiting and publication
    work, and expiry after rename triggers a rollback attempt. Returns
    ``created``, ``replaced``, or ``unchanged`` (identical bytes).
    """
    if (deadline is not None
            and (isinstance(deadline, bool)
                 or not isinstance(deadline, (int, float))
                 or not math.isfinite(deadline))):
        raise TypeError("deadline must be a finite monotonic timestamp or None")
    _check_deadline(deadline)
    claimed, body = _split_marker(payload)
    if hashlib.sha256(body).hexdigest() != claimed:
        _fail("report-invalid",
              "payload marker hash does not match its body")
    full = bytes(payload)  # type: ignore[arg-type]
    if source_proof is None:
        _fail("stale-evidence",
              "source proof is required; omission makes no source-drift "
              "claim, so publication is refused and the prior report is "
              "preserved (pass [] to claim no admitted files)")
        raise AssertionError("unreachable")
    proof = _check_source_proof(source_proof)
    _check_deadline(deadline)
    with _CooperativeLock(_lock_path_for(Path(root)), deadline):  # type: ignore[arg-type]
        _check_deadline(deadline)
        root_fd = _open_root(root)
        try:
            _check_deadline(deadline)
            current = _read_target(root_fd)
            _check_deadline(deadline)
            _check_previous(previous, current)
            if previous is not None and not isinstance(
                    previous, PublishedIdentity):
                raise TypeError(
                    "previous must be PublishedIdentity or None")
            _verify_source_proof(root_fd, proof)
            _check_deadline(deadline)
            if current is not None and current[0] == full:
                return PublishResult(
                    status="unchanged", path=_REPORT_NAME,
                    sha256=hashlib.sha256(full).hexdigest())
            temp_name = _stage_temp(root_fd, full)
            try:
                _check_deadline(deadline)
                rechecked = _read_target(root_fd)
                if (current is None) != (rechecked is None):
                    _fail("report-conflict",
                          "recommendations.md appeared or vanished while "
                          "staging; refusing to replace it")
                if current is not None and rechecked is not None:
                    if current[0] != rechecked[0]:
                        _fail("report-conflict",
                              "recommendations.md changed while staging "
                              "(bytes differ); refusing to replace it")
                    if current[1:4] != rechecked[1:4]:
                        _fail("report-conflict",
                              "recommendations.md changed while staging "
                              "(identity differs); refusing to replace it")
                _verify_source_proof(root_fd, proof)
                _check_deadline(deadline)
                try:
                    os.rename(temp_name, _REPORT_NAME,
                              src_dir_fd=root_fd, dst_dir_fd=root_fd)
                    _check_deadline(deadline)
                    os.fsync(root_fd)
                    _check_deadline(deadline)
                except OSError:
                    if not _restore_after_sync_failure(root_fd, current, full):
                        _fail("state-unavailable",
                              "report publication failed and the prior "
                              "complete report could not be confirmed")
                    _fail("state-unavailable",
                          "parent sync or atomic replace failed; the prior "
                          "complete report was restored where practical")
                except BaseException:
                    if not _restore_after_sync_failure(root_fd, current, full):
                        _fail("state-unavailable",
                              "report publication was interrupted and the "
                              "prior complete report could not be confirmed")
                    raise
            finally:
                try:
                    os.unlink(temp_name, dir_fd=root_fd)
                except OSError:
                    pass
            return PublishResult(
                status="replaced" if current is not None else "created",
                path=_REPORT_NAME,
                sha256=hashlib.sha256(full).hexdigest())
        finally:
            os.close(root_fd)


__all__ = [
    "FOOTER",
    "MAX_SOURCE_PROOF_ENTRIES",
    "PublishedIdentity",
    "PublishResult",
    "publish_recommendations",
    "render_recommendations",
]
