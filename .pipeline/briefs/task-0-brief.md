### Task 0: Runnable bootstrap, contracts and bounded file foundation

**Own:** `pyproject.toml`, `uv.lock`, `src/ptest/__init__.py`, `src/ptest/adapters/__init__.py`, `src/ptest/runtime/__init__.py`, `src/ptest/contracts.py`, `src/ptest/files.py`, `src/ptest/storage.py`, `scripts/ptest-bootstrap`, `scripts/export-schemas.py`, `tests/ng/conftest.py`, `tests/ng/support.py`, `tests/ng/test_contracts.py`, `tests/ng/test_files.py`, `tests/ng/test_storage.py`, generated `docs/schemas/v1/*.json`, generated `src/ptest/runtime/protocol-v1.json`, `.gitignore` (only local artifact/env entries), `.pipeline/out/task-0.json`. Do not replace root ptest or import the provisional package.

**Consumes:** Approved design sections3–7 JSON/type/path/budget catalog. **Produces:** all frozen records/codecs/default constants and guard ControlFrame/LaunchManifest descriptors; `read_regular(root,relative,limit)->bytes`, `validate_private_dir(path)->None`, `validate_private_file(path)->None`, `ensure_private_dir(parent,name)->Path`, `create_exclusive(root,relative,data,*,private=True)->Path`, `publish_atomic(root,name,data)->Path`, `open_database(root,name,*,max_bytes,read_only=False)->sqlite3.Connection`; fixture helpers and bootstrap wrapper. T1/T3/T4/T8/T11/T13 consume these shared APIs; no independent unsafe writer/SQLite-opening implementation. No production scheduler/runner stub.

- [ ] Resolve installed legacy executable read-only, build the manifest/lock with dev pytest9.1.1 + optional test dependency xdist3.8.0, package data/console entrypoint declaration, and local bootstrap wrapper. T0 alone creates all three package __init__.py files. Packaging must include runtime/*.py, runtime/*.mjs, runtime/protocol-v1.json, resources/agent-guide.md and resources/recipes/*.md as later tasks add them; T13 wheel contents test verifies each. The CLI module may be absent until T11; module-specific tests must still run. Record actual resolved dependency/build versions, not a public license guess.
- [ ] Write behavioral negative tests before `files.py` and codecs. Start with this test plus a strict unknown-version and command-summary redaction test:

```python
def test_read_regular_rejects_escape(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    outside = tmp_path / "sentinel"
    outside.write_text("outside-secret")
    (root / "alias").symlink_to(outside)
    with pytest.raises(Problem, match="unsafe-path"):
        read_regular(root, "alias", 1024)
```

- [ ] Run `scripts/ptest-bootstrap tests/ng/test_files.py -k rejects_escape`; distinguish bootstrap import failure from intended failing assertion. Implement the **exported shared** descriptor-relative read/exclusive-write/atomic-publish/private-validation APIs and the tiny common SQLite opener. Add directory-swap, FIFO, hardlink/state, wrong-mode/no-chmod, existing-file-no-overwrite, atomic-temp cleanup, DB pragma/read-only/oversize/corruption and malformed JSON/TOML tests.
- [ ] Define every shared record/enum/default and public/private schema descriptor from the design, including register kind/payload and guard frame lengths/tags. Generate runtime/protocol-v1.json for dependency-free bridge loading. Serialize explicit allowlisted public fields, not `asdict` on a transient argv/env record. Execute `scripts/ptest-bootstrap tests/ng/test_contracts.py tests/ng/test_files.py tests/ng/test_storage.py`; assert all eight public documents independently parse, register success/error match schema, unknown-major/frame-truncation/wrong-nonce/oversize rejection, boundary integers/default drift and absent command sentinels. Run schema export `--check`.
- [ ] Prove wrapper failure sensitivity and local-only provenance; commit with `build: establish local NG contract bootstrap`. Root/Opus validate barrier before any parallel task.

