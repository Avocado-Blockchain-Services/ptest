### Task 2: Platform domain and process identity primitives

**Own:** `src/ptest/platform.py`, `tests/ng/test_platform.py`, `.pipeline/out/task-2.json`.

**Consumes:** DomainPaths/ProcessIdentity/QuiescenceProof. **Produces:** `domain_paths(fixture)->DomainPaths`, `process_identity(pid)->ProcessIdentity|None`; internal filesystem/boot/group-absence helpers consumed by scheduler through this module, named `probe_group(pgid)->GroupObservation` and `boot_identity()->str`. T0 defines `GroupObservation(exists: bool|None, permission: bool, checked_at: float)` before barrier.

- [ ] Test domain invariance without writing the normal domain:

```python
def test_domain_ignores_home_and_xdg(monkeypatch):
    before = domain_paths(None)
    monkeypatch.setenv("HOME", "/unused/tui-home")
    monkeypatch.setenv("XDG_STATE_HOME", "/unused/tui-state")
    assert domain_paths(None).root == before.root
```

- [ ] Run `scripts/ptest-bootstrap tests/ng/test_platform.py -k ignores_home`; implement pwd-account roots, supported OS/local filesystem validation, equal real/effective uid requirement, birth/pgid observations and no-signalling group probes. Never read process argv/env.
- [ ] Add negative twins: unsupported OS, wrong owner/permissions/hardlinks/symlink components, unknown/network filesystem, stale fixture marker/dev/inode, outside fixture roots, inaccessible identity, changed boot and reused PID observations. Simulate both absent canonical parents and existing0755 parents/0644 legacy siblings under a private account-home fixture; assert domain_paths resolves the correct coordination location **without creating anything or changing any mode/byte**. Creation/execution belongs to T4/T11, not platform. No real home chmod/prune. Direct normal-domain tests use T0's cleared bootstrap env; deliberate PTEST_CONFIG still rejects. Monkeypatch platform observations only for deterministic branches; actual Linux/macOS evidence stays separate.
- [ ] Run `scripts/ptest-bootstrap tests/ng/test_platform.py`; commit `feat: establish canonical domain and conservative process identity`.

