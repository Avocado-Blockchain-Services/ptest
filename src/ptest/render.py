"""Pure public renderers for NG documents and doctor repair guidance."""
from __future__ import annotations

import importlib.resources

from . import contracts as C


def render_json(document: C.PublicDocument) -> bytes:
    """Encode one already-projected public document through the shared codec."""
    if not isinstance(document, C.PublicDocument):
        raise TypeError("render_json requires PublicDocument")
    return C.encode_public_document(
        document.kind, document.data, error=document.error, domain=document.domain,
    )


def _reason(reason: C.Reason) -> dict:
    return {"code": reason.code, "message": reason.message,
            "paths": list(reason.paths)}


def _doctor_data(report: C.DoctorReport) -> dict:
    if not isinstance(report, C.DoctorReport):
        raise TypeError("doctor renderer requires DoctorReport")
    return {
        "scope": list(report.scope),
        "readiness": [
            {"area": item.area, "state": item.state,
             "reasons": [_reason(reason) for reason in item.reasons]}
            for item in report.readiness
        ],
        "findings": [
            {"code": item.code, "severity": item.severity,
             "confidence": item.confidence, "path": item.path,
             "line": item.line, "evidence_type": item.evidence_type,
             "consequence": item.consequence,
             "remediation": item.remediation,
             "verification": item.verification}
            for item in report.findings
        ],
        "limits": None if report.limits is None else {
            "entries": report.limits.entries, "files": report.limits.files,
            "file_bytes": report.limits.file_bytes,
            "total_bytes": report.limits.total_bytes,
            "findings": report.limits.findings,
            "output_bytes": report.limits.output_bytes,
            "elapsed_s": report.limits.elapsed_s,
            "depth": report.limits.depth, "ast_nodes": report.limits.ast_nodes,
        },
        "usage": None if report.usage is None else {
            "entries": report.usage.entries, "files": report.usage.files,
            "file_bytes": report.usage.file_bytes,
            "total_bytes": report.usage.total_bytes,
            "findings": report.usage.findings,
            "output_bytes": report.usage.output_bytes,
            "elapsed_s": report.usage.elapsed_s,
            "skipped": report.usage.skipped,
            "truncated": report.usage.truncated,
        },
        "limitations": [_reason(reason) for reason in report.limitations],
    }


def render_doctor_json(report: C.DoctorReport, *, domain: dict | None = None) -> bytes:
    return render_json(C.PublicDocument(
        kind="doctor", ptest_version=C.PTEST_VERSION, domain=domain,
        data=_doctor_data(report), error=None,
    ))


def render_doctor(report: C.DoctorReport) -> str:
    """Human output retains unknown/blocked readiness rather than certifying it."""
    data = _doctor_data(report)
    lines = ["ptest doctor", ""]
    for readiness in data["readiness"]:
        lines.append(f"{readiness['area']}: {readiness['state']}")
        for reason in readiness["reasons"]:
            lines.append(f"  - {reason['message']}")
    if report.findings:
        lines.extend(("", "Findings:"))
        for finding in report.findings:
            location = "" if finding.path is None else f" ({finding.path})"
            lines.append(f"- {finding.code}{location}: {finding.consequence}")
    if report.limitations:
        lines.extend(("", "Limitations:"))
        lines.extend(f"- {reason.message}" for reason in report.limitations)
    return "\n".join(lines) + "\n"


def _guide() -> str:
    # This is package data, not repository input.  Missing package data is a
    # packaging error and falls back to a safe bounded instruction set.
    try:
        return importlib.resources.files("ptest").joinpath(
            "resources", "agent-guide.md").read_text(encoding="utf-8")
    except (FileNotFoundError, OSError, AttributeError):
        return (
            "Use ptest doctor, inspect findings, preserve assertions and coverage, "
            "and verify a focused run before the full gate.\n"
        )


def repair_prompt(report: C.DoctorReport) -> str:
    """Build a bounded repair prompt from allowlisted doctor evidence only."""
    if not isinstance(report, C.DoctorReport):
        raise TypeError("repair_prompt requires DoctorReport")
    findings = []
    for finding in report.findings:
        path = "(path withheld)" if finding.path is None else finding.path
        findings.append(
            f"[{finding.code}] {path}: {finding.remediation} "
            f"Verify: {finding.verification}"
        )
    limitations = [f"[limitation] {reason.message}" for reason in report.limitations]
    body = _guide().rstrip() + "\n\nBounded doctor evidence:\n"
    body += "\n".join(findings + limitations) if (findings or limitations) else "(none)"
    body += (
        "\n\nRepair constraints: make the smallest maintainable change; preserve "
        "assertions and coverage; use factories, per-worker/run ownership, "
        "cache namespaces, private files, assigned ports, joined processes, "
        "and deterministic time/network boundaries. Never use blanket flush or "
        "drop, sleep synchronization, failure suppression, or trust/config/TUI "
        "mutation. Run a focused ptest command, then the full gate.\n"
    )
    encoded = body.encode("utf-8")
    if len(encoded) <= C.MAX_PROMPT_BYTES:
        return body
    # Keep the prompt valid UTF-8 and reserve a visible truncation marker.
    marker = "\n[doctor prompt truncated at the configured bound]\n"
    room = C.MAX_PROMPT_BYTES - len(marker.encode("utf-8"))
    return encoded[:room].decode("utf-8", errors="ignore") + marker


def render_guide() -> str:
    return _guide()


__all__ = [
    "render_json", "render_doctor_json", "render_doctor", "repair_prompt",
    "render_guide",
]
