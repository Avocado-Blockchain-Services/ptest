### Task 1: Portable config, non-overwriting init and migration

**Own:** `src/ptest/config.py`, `tests/ng/test_config.py`, `tests/ng/test_init.py`, `tests/ng/fixtures/config/`, `.pipeline/out/task-1.json`.

**Consumes:** Config/ConfigResolution/InitOptions/InitResult and files helpers. **Produces:** `resolve_config(cwd)->ConfigResolution`, `init_project(cwd,options)->InitResult` with exact design grammar; no CLI registration.

Use T0's InitAction: existing target takes precedence even under dry-run; absent dry-run is preview/exists=false; creation is created/exists=true. Test each case without changing existing bytes. InitResult.config remains internal Config|null; T11 projects it through the frozen ConfigSummary, never raw config serialization.

- [ ] Write the non-overwrite test and resolution/legacy negative cases:

```python
def test_init_never_overwrites_existing(tmp_path):
    path = tmp_path / ".ptest.toml"
    original = b"version = 999\n# preserve me\n"
    path.write_bytes(original)
    result = init_project(tmp_path, InitOptions(runner=None, dry_run=False,
                           reveal_command=False, adopt_local=False))
    assert result.exists is True
    assert path.read_bytes() == original
```

- [ ] Run `scripts/ptest-bootstrap tests/ng/test_init.py -k never_overwrites`; implement bounded native-file discovery, nearest-root resolution, typed TOML validation and exclusive config creation. Preserve native addopts/coverage by leaving them native.
- [ ] Add tests for mixed runners, nested/moved worktrees, no git, executable symlink/path bounds, firstclass launcher profiles, setup side effects declaration, remote/compound legacy adoption refusal, unsupported PTEST_* location versus ignored HOME/XDG. Parse the exact design TOML example and assert project_id length32; reject31/33hex IDs with invalid-config. Invalid selection policy with valid execution returns full-fallback warning rather than partial policy use; invalid runner fails. All config creation uses T0 create_exclusive; no local writer clone.
- [ ] Run `scripts/ptest-bootstrap tests/ng/test_config.py tests/ng/test_init.py`; A1/A8/A11 negatives must pass. Commit `feat: add portable initialization and explicit legacy adoption`.
