#!/usr/bin/env python3
"""Emit deterministic JSON Schema from contracts descriptors (Task0 owned).

Imports only ``ptest.contracts`` (plus stdlib) and writes
``docs/schemas/v1/<kind>.json`` plus ``src/ptest/runtime/protocol-v1.json``.
``--check`` verifies committed files match regeneration (schema drift gate).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def _load_descriptors():
    try:
        from ptest.contracts import PROTOCOL_V1_DESCRIPTOR, PUBLIC_SCHEMAS
    except ImportError:
        root = Path(__file__).resolve().parents[1]
        sys.path.insert(0, str(root / "src"))
        from ptest.contracts import PROTOCOL_V1_DESCRIPTOR, PUBLIC_SCHEMAS
    return PUBLIC_SCHEMAS, PROTOCOL_V1_DESCRIPTOR


def main(argv: list) -> int:
    root = Path(__file__).resolve().parents[1]
    schemas, protocol = _load_descriptors()
    check = "--check" in argv
    failed = False
    schema_dir = root / "docs" / "schemas" / "v1"
    targets = [
        (schema_dir / f"{kind}.json", schemas[kind])
        for kind in sorted(schemas)
    ]
    targets.append((root / "src" / "ptest" / "runtime" / "protocol-v1.json", protocol))
    for path, descriptor in targets:
        text = json.dumps(descriptor, indent=2, sort_keys=True) + "\n"
        if check:
            try:
                current = path.read_text(encoding="utf-8")
            except FileNotFoundError:
                print(f"schema missing: {path}")
                failed = True
                continue
            if current != text:
                print(f"schema drift: {path}")
                failed = True
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            print(f"wrote {path}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
