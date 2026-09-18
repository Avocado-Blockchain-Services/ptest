import subprocess


def test_static_snapshot_without_key_creates_no_state_and_cannot_narrow(case):
    from ptest.source import snapshot

    domain = case.domain()
    normal_sentinel = case.base / "normal-state-sentinel"
    normal_sentinel.write_text("do not read")
    result = snapshot(domain, case.config(), None, None)
    assert result.digest is None
    assert any(reason.code == "state-unavailable" for reason in result.limitations)
    assert not (domain.root / "input-hmac.key").exists()
    assert normal_sentinel.read_text() == "do not read"


def test_execution_key_creation_is_scoped_to_supplied_domain(case):
    from ptest.source import ensure_fingerprint_key

    first = case.domain()
    second = case.domain()
    ensure_fingerprint_key(first)
    assert (first.root / "input-hmac.key").is_file()
    assert not (second.root / "input-hmac.key").exists()


def test_snapshot_reads_git_nul_status_and_committed_base_union(case):
    from ptest.source import ensure_fingerprint_key, snapshot

    domain, root = _repository(case)
    config = _config(case, domain)
    ensure_fingerprint_key(domain)
    initial = _git(root, "rev-parse", "HEAD")
    (root / "src" / "a.py").write_text("two\n")
    _git(root, "add", "src/a.py"); _git(root, "commit", "-m", "change")
    baseline = case.history(with_baseline=True).baseline
    baseline = type(baseline)(run_id=baseline.run_id, head=initial, input_digest=baseline.input_digest,
                              compatibility="x", inventory=baseline.inventory,
                              policy_digest=baseline.policy_digest, created_at=baseline.created_at)
    (root / "tests" / "test_a.py").write_text("dirty\n")
    (root / "untracked.py").write_text("u\n")
    result = snapshot(domain, config, baseline, initial)
    assert result.digest is not None
    assert {change.new for change in result.changes if change.new} >= {"src/a.py", "tests/test_a.py", "untracked.py"}


def test_snapshot_rejects_nonancestor_baseline_and_bad_base(case):
    from ptest.source import ensure_fingerprint_key, snapshot

    domain, root = _repository(case); config = _config(case, domain); ensure_fingerprint_key(domain)
    baseline = case.history(with_baseline=True).baseline
    baseline = type(baseline)(run_id=baseline.run_id, head="deadbeef", input_digest=baseline.input_digest,
                              compatibility="x", inventory=baseline.inventory, policy_digest=baseline.policy_digest,
                              created_at=baseline.created_at)
    assert snapshot(domain, config, baseline, None).digest is None
    assert snapshot(domain, config, None, "deadbeef").digest is None


def test_declared_environment_is_hmaced_without_exposing_value(case, monkeypatch):
    from ptest.source import ensure_fingerprint_key, snapshot
    from ptest import contracts as C

    domain, root = _repository(case); ensure_fingerprint_key(domain)
    config = _config(case, domain)
    policy = C.SelectionPolicy(enabled=True, closed_inputs=True, input_roots=("src", "tests"), environment=("FIXTURE_TOKEN",))
    config = case.config(checkout=config.checkout, selection=policy)
    monkeypatch.setenv("FIXTURE_TOKEN", "first-secret")
    first = snapshot(domain, config, None, None)
    monkeypatch.setenv("FIXTURE_TOKEN", "second-secret")
    second = snapshot(domain, config, None, None)
    assert first.digest != second.digest
    assert "secret" not in repr(second)


def test_deleted_and_renamed_paths_are_reported_without_losing_snapshot(case):
    from ptest.source import ensure_fingerprint_key, snapshot

    domain, root = _repository(case); ensure_fingerprint_key(domain); config = _config(case, domain)
    (root / "src" / "a.py").rename(root / "src" / "renamed.py")
    (root / "tests" / "test_a.py").unlink()
    _git(root, "add", "-A")
    result = snapshot(domain, config, None, None)
    assert result.digest is not None
    assert any(change.kind == "renamed" for change in result.changes)
    assert any(change.kind == "deleted" for change in result.changes)


def test_ignored_generated_input_is_fingerprinted_when_declared(case):
    from ptest.source import ensure_fingerprint_key, snapshot
    from ptest import contracts as C

    domain, root = _repository(case); ensure_fingerprint_key(domain)
    (root / ".gitignore").write_text("generated/\n"); _git(root, "add", ".gitignore"); _git(root, "commit", "-m", "ignore")
    (root / "generated").mkdir(); (root / "generated" / "input.py").write_text("generated\n")
    original = _config(case, domain)
    policy = C.SelectionPolicy(enabled=True, closed_inputs=True, input_roots=("src", "tests"),
                               ignored_inputs=("generated",))
    result = snapshot(domain, case.config(checkout=original.checkout, selection=policy), None, None)
    assert result.digest is not None
    assert "generated/input.py" in {fingerprint.path for fingerprint in result.files}


def test_fingerprint_scan_envelopes_fail_closed(case, monkeypatch):
    from ptest.source import ensure_fingerprint_key, snapshot
    from ptest import source

    domain, root = _repository(case); ensure_fingerprint_key(domain); config = _config(case, domain)
    monkeypatch.setattr(source, "_MAX_FILES", 1)
    assert snapshot(domain, config, None, None).digest is None
    monkeypatch.setattr(source, "_MAX_FILES", 100_000)
    monkeypatch.setattr(source, "_MAX_TOTAL_BYTES", 1)
    assert snapshot(domain, config, None, None).digest is None
    monkeypatch.setattr(source, "_MAX_TOTAL_BYTES", 512 * 1024 * 1024)
    ticks = iter((0.0, 11.0, 12.0))
    monkeypatch.setattr(source.time, "monotonic", lambda: next(ticks))
    assert snapshot(domain, config, None, None).digest is None


def test_mode_change_and_gitlink_fail_closed(case):
    from ptest.source import ensure_fingerprint_key, snapshot

    domain, root = _repository(case); ensure_fingerprint_key(domain); config = _config(case, domain)
    (root / "src" / "a.py").chmod(0o755)
    assert any(change.new == "src/a.py" for change in snapshot(domain, config, None, None).changes)
    _git(root, "update-index", "--add", "--cacheinfo", "160000," + "a" * 40 + ",submodule")
    assert snapshot(domain, config, None, None).digest is None


def test_unmerged_conflict_and_shallow_baseline_fail_closed(case):
    from ptest.source import ensure_fingerprint_key, snapshot

    domain, root = _repository(case); ensure_fingerprint_key(domain); config = _config(case, domain)
    initial = _git(root, "rev-parse", "HEAD")
    main_branch = _git(root, "branch", "--show-current")
    _git(root, "checkout", "-b", "other")
    (root / "src" / "a.py").write_text("other\n"); _git(root, "commit", "-am", "other")
    _git(root, "checkout", main_branch)
    (root / "src" / "a.py").write_text("master\n"); _git(root, "commit", "-am", "master")
    assert subprocess.run(("git", "-C", str(root), "merge", "other")).returncode != 0
    conflicted = snapshot(domain, config, None, None)
    assert conflicted.digest is None
    assert any(reason.code == "unknown-input" for reason in conflicted.limitations)
    _git(root, "merge", "--abort")
    shallow = domain.root / "shallow"
    subprocess.run(("git", "clone", "--depth", "1", "file://" + str(root), str(shallow)), check=True, stdout=subprocess.PIPE)
    shallow_checkout = case.checkout(domain); object.__setattr__(shallow_checkout, "root", shallow)
    baseline = case.history(with_baseline=True).baseline
    baseline = type(baseline)(run_id=baseline.run_id, head=initial, input_digest=baseline.input_digest,
                              compatibility="x", inventory=baseline.inventory, policy_digest=baseline.policy_digest,
                              created_at=baseline.created_at)
    assert snapshot(domain, case.config(checkout=shallow_checkout), baseline, None).digest is None
def _git(root, *args):
    return subprocess.run(("git", "-C", str(root), *args), check=True,
                          stdout=subprocess.PIPE).stdout.decode().strip()


def _repository(case):
    domain = case.domain()
    root = domain.root / "repo"
    root.mkdir()
    _git(root, "init")
    _git(root, "config", "user.email", "fixture@example.test")
    _git(root, "config", "user.name", "Fixture")
    (root / "src").mkdir(); (root / "tests").mkdir()
    (root / "src" / "a.py").write_text("one\n")
    (root / "tests" / "test_a.py").write_text("pass\n")
    _git(root, "add", "."); _git(root, "commit", "-m", "initial")
    return domain, root


def _config(case, domain):
    checkout = case.checkout(domain)
    object.__setattr__(checkout, "root", domain.root / "repo")
    return case.config(checkout=checkout)
