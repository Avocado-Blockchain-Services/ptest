from __future__ import annotations

import json

from ptest import contracts as C
from ptest.render import render_json, repair_prompt


def test_render_json_uses_shared_descriptor_and_never_exposes_argv():
    command = C.summarize_command(
        C.RunnerKind.COMMAND, C.Mode.FULL,
        ("/secret/executable", "secret-sentinel", "$(literal)"),
        workers=1, provenance=("config",),
    )
    payload = {
        "root": "/tmp/project",
        "config_path": "/tmp/project/.ptest.toml",
        "initialized": True,
        "runner_kind": "command",
        "capability": None,
        "commands": [
            {**command.__dict__, "kind": "command", "mode": "scoped",
             "generated_options": [], "provenance": ["config"]},
            {**command.__dict__, "kind": "command", "mode": "full",
             "generated_options": [], "provenance": ["config"]},
        ],
        "effective_limits": {"max_slots": 1, "max_jobs": 1,
                              "memory_mb": None, "repo_workers": None},
        "provenance": ["config"],
        "warnings": [],
    }
    # Render accepts a PublicDocument, keeping arbitrary payload projection in
    # contracts rather than inventing a second public schema.
    raw = render_json(C.PublicDocument(
        kind="where", ptest_version=C.PTEST_VERSION, domain=None,
        data=payload, error=None,
    ))
    document = json.loads(raw)
    assert "secret-sentinel" not in raw.decode()
    assert "$(literal)" not in raw.decode()
    assert document["data"]["commands"][0]["argument_count"] == 3


def test_repair_prompt_is_bounded_and_forbids_unsafe_repairs(case):
    report = __import__("ptest.doctor", fromlist=["inspect"])
    domain = case.domain()
    config = case.config()
    resolution = C.ConfigResolution(root=domain.root, path=config.config_path,
                                    config=config)
    limits = C.DEFAULT_SCAN_LIMITS
    doctor_report = report.inspect(domain, resolution, limits, None)
    prompt = repair_prompt(doctor_report)
    assert len(prompt.encode()) <= C.MAX_PROMPT_BYTES
    assert "global flush" in prompt.lower()
    assert "sleep" in prompt.lower()
    assert "drop" in prompt.lower()
    assert "ptest" in prompt
