"""Static doctor regression coverage (Task 9)."""
from __future__ import annotations

from pathlib import Path
import shutil
import os
from dataclasses import asdict, fields, replace
import json

import pytest

from ptest import contracts as C
from ptest.doctor import inspect


def _resolution(case, root: Path) -> C.ConfigResolution:
    return C.ConfigResolution(
        root=root, path=None, config=case.config(), provenance=(), warnings=(), problem=None,
    )


def test_doctor_never_imports_scanned_python(case):
    """Removing static-only scanning would execute this import sentinel."""
    domain = case.domain()
    root = case.project(domain)
    source = root / "tests" / "danger.py"
    source.parent.mkdir()
    source.write_text("raise RuntimeError('doctor imported test code')\n", encoding="utf-8")
    report = inspect(domain, _resolution(case, root), C.DEFAULT_SCAN_LIMITS, None)

    assert next(item for item in report.readiness if item.area == "parallel").state == "unknown"
    assert "static-evidence-insufficient" in {
        reason.code for reason in report.limitations
    }


def test_doctor_finds_global_cache_flush_and_never_certifies_parallel_safety(case):
    """Replacing detector matching with a no-op loses a concrete risk finding."""
    domain = case.domain()
    root = case.project(domain)
    source = root / "tests" / "cache_test.py"
    source.parent.mkdir()
    source.write_text("def test_cache(client):\n    client.flushall()\n", encoding="utf-8")

    report = inspect(domain, _resolution(case, root), C.DEFAULT_SCAN_LIMITS, None)

    assert [(item.code, item.path, item.line) for item in report.findings] == [
        ("cache.global-flush", "tests/cache_test.py", 2),
    ]
    parallel = next(item for item in report.readiness if item.area == "parallel")
    assert parallel.state == "unknown"
    assert {reason.code for reason in parallel.reasons} >= {
        "static-evidence-insufficient",
    }


def test_doctor_rejects_symlink_without_reading_target(case, tmp_path):
    """Following an in-tree symlink would expose unrelated file contents."""
    domain = case.domain()
    root = case.project(domain)
    outside = tmp_path / "outside.py"
    outside.write_text("client.flushall()\n", encoding="utf-8")
    tests = root / "tests"
    tests.mkdir()
    (tests / "link.py").symlink_to(outside)

    report = inspect(domain, _resolution(case, root), C.DEFAULT_SCAN_LIMITS, None)

    assert report.findings == ()
    assert report.usage.skipped >= 1
    assert "unsafe-path" in {reason.code for reason in report.limitations}


def test_doctor_catalog_fixtures_produce_their_specific_static_hypotheses(case):
    """Replacing catalog matching with generic/no-op findings loses detector identity."""
    domain = case.domain()
    root = case.project(domain)
    target = root / "tests"
    shutil.copytree(Path(__file__).parent / "fixtures" / "doctor", target)

    report = inspect(domain, _resolution(case, root), C.DEFAULT_SCAN_LIMITS, None)

    assert {item.code for item in report.findings} >= {
        "cache.global-flush", "db.per-test-initialization", "network.fixed-port",
        "time.blocking-sleep", "network.live-target", "process.detached-child",
    }


def test_every_catalog_detector_has_a_positive_and_clean_negative(case):
    """Removing any fixed detector loses its hand-derived positive finding."""
    cases = {
        "db.per-test-initialization": "sqlite3.connect('x')",
        "db.cleanup-ownership": "drop_database()",
        "cache.global-flush": "cache.flushdb()",
        "resource.fixed-name": "open('shared.txt')",
        "network.fixed-port": "port = 41000",
        "time.blocking-sleep": "time.sleep(1)",
        "network.live-target": "requests.get('https://example.invalid')",
        "process.detached-child": "os.setsid()",
        "fixture.shared-mutation": "@pytest.fixture(scope='session')\ndef thing(): pass",
        "selection.unknown-input": "value = os.environ['FEATURE']",
    }
    domain = case.domain()
    root = case.project(domain)
    tests = root / "tests"
    tests.mkdir()
    for index, source in enumerate(cases.values()):
        (tests / f"risk_{index}.py").write_text(source + "\n", encoding="utf-8")
    (tests / "clean.py").write_text("def test_clean():\n    assert 1 == 1\n", encoding="utf-8")

    report = inspect(domain, _resolution(case, root), C.DEFAULT_SCAN_LIMITS, None)

    assert {finding.code for finding in report.findings} == set(cases)
    assert all(finding.path != "tests/clean.py" for finding in report.findings)


def test_doctor_bounds_discovery_prioritizes_tests_and_skips_adversarial_inputs(case, monkeypatch):
    """A broad-first or unsafe reader would miss the test risk or read hostile entries."""
    domain = case.domain()
    root = case.project(domain)
    tests = root / "tests"
    tests.mkdir()
    (tests / "priority.py").write_text("cache.flushall()\n", encoding="utf-8")
    (root / ".env").write_text("SECRET=https://secret.invalid\n", encoding="utf-8")
    fifo = root / "blocked"
    os.mkfifo(fifo)
    (root / "deep.py").write_text("x = " + "(" * 100 + "1" + ")" * 100, encoding="utf-8")
    (root / "script.js").write_text("\n".join("let x = 1" for _ in range(20)), encoding="utf-8")
    control = root / "bad\x1bname.py"
    control.write_text("cache.flushall()", encoding="utf-8")
    complete = inspect(domain, _resolution(case, root), C.DEFAULT_SCAN_LIMITS, None)
    assert complete.usage.skipped >= 3
    assert {reason.code for reason in complete.limitations} >= {"unsafe-path", "static-evidence-insufficient"}
    limits = C.ScanLimits(entries=4, files=4, file_bytes=4096, total_bytes=8192,
                          findings=10, output_bytes=8192, elapsed_s=8, depth=8, ast_nodes=10)

    report = inspect(domain, _resolution(case, root), limits, None)

    assert "cache.global-flush" in {item.code for item in report.findings}
    assert report.usage.truncated is True
    assert report.usage.entries <= 4
    assert all("secret.invalid" not in item.path for item in report.findings if item.path)
    assert {reason.code for reason in report.limitations} >= {"scan-limit", "static-evidence-insufficient"}

    unsafe = C.ScanLimits(entries=C.MAX_SCAN_LIMITS.entries + 1, files=1, file_bytes=1,
                          total_bytes=1, findings=1, output_bytes=1, elapsed_s=1, depth=1, ast_nodes=1)
    with __import__("pytest").raises(C.Problem, match="invalid-bound"):
        inspect(domain, _resolution(case, root), unsafe, None)


def test_doctor_handles_symlink_swap_output_and_deadline_without_state_access(case, monkeypatch):
    """Unsafe replacement, unbounded reports, deadlines, or domain reads break this contract."""
    import ptest.doctor as doctor

    domain = case.domain()
    root = case.project(domain)
    tests = root / "tests"
    tests.mkdir()
    target = tests / "swap.py"
    target.write_text("cache.flushall()\n", encoding="utf-8")
    normal_state = case.base / "normal-state"
    assert not normal_state.exists()
    real_read = doctor.read_regular

    def swap_before_read(read_root, relative, limit):
        if relative == "tests/swap.py" and not target.is_symlink():
            target.unlink()
            target.symlink_to(normal_state)
        return real_read(read_root, relative, limit)

    monkeypatch.setattr(doctor, "read_regular", swap_before_read)
    report = inspect(domain, _resolution(case, root), C.DEFAULT_SCAN_LIMITS, None)
    assert report.findings == ()
    assert not normal_state.exists()
    assert "unsafe-path" in {reason.code for reason in report.limitations}

    monkeypatch.setattr(doctor, "read_regular", real_read)
    target.unlink()
    target.write_text("\n".join("cache.flushall()" for _ in range(20)), encoding="utf-8")
    cap = C.ScanLimits(entries=20, files=20, file_bytes=4096, total_bytes=8192,
                       findings=20, output_bytes=1, elapsed_s=8, depth=8, ast_nodes=1000)
    report = inspect(domain, _resolution(case, root), cap, None)
    assert report.findings == ()
    assert report.usage.output_bytes == 0
    assert report.usage.truncated is True

    ticks = iter((0.0, 9.0, 9.0))
    monkeypatch.setattr(doctor.time, "monotonic", lambda: next(ticks))
    deadline = C.ScanLimits(entries=20, files=20, file_bytes=4096, total_bytes=8192,
                            findings=20, output_bytes=8192, elapsed_s=1, depth=8, ast_nodes=1000)
    report = inspect(domain, _resolution(case, root), deadline, None)
    assert report.usage.truncated is True
    assert "scan-limit" in {reason.code for reason in report.limitations}


@pytest.mark.parametrize("source", [
    "x = " + "+".join(["1"] * 10000),
    "x = " + "-" * 7000 + "1",
], ids=["left-associative", "unary-depth"])
def test_deep_python_returns_limitation_at_default_bounds(case, source):
    domain = case.domain()
    root = case.project(domain)
    (root / "deep.py").write_text(source)
    report = inspect(domain, _resolution(case, root), C.DEFAULT_SCAN_LIMITS, None)
    assert report.findings == ()
    assert report.usage.skipped >= 1
    assert any("syntax" in item.message for item in report.limitations)


@pytest.mark.parametrize("field", [item.name for item in fields(C.ScanLimits)])
def test_all_scan_bounds_reject_values_above_the_maximum(case, field):
    domain = case.domain()
    root = case.project(domain)
    limits = replace(C.DEFAULT_SCAN_LIMITS, **{field: getattr(C.MAX_SCAN_LIMITS, field) + 1})
    with pytest.raises(C.Problem, match="invalid-bound"):
        inspect(domain, _resolution(case, root), limits, None)


def test_undecodable_entry_names_are_redacted_from_findings_and_limitations(case):
    domain = case.domain()
    root = case.project(domain)
    fd = os.open(os.fsencode(root) + b"/private-\xff.py", os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        os.write(fd, b"cache.flushall()\n")
    finally:
        os.close(fd)
    report = inspect(domain, _resolution(case, root), C.DEFAULT_SCAN_LIMITS, None)
    payload = json.dumps(asdict(report), ensure_ascii=False).encode("utf-8")
    assert b"private-" not in payload
    assert report.findings == ()
    assert any(item.code == "unsafe-path" for item in report.limitations)


def test_deadline_expires_between_files_in_one_directory(case, monkeypatch):
    import ptest.doctor as doctor
    domain = case.domain()
    root = case.project(domain)
    (root / "a.py").write_text("cache.flushall()\n")
    (root / "b.py").write_text("cache.flushdb()\n")
    clock = [0.0]
    real_read = doctor.read_regular

    def advance_after_read(*args):
        result = real_read(*args)
        clock[0] = 9.0
        return result

    monkeypatch.setattr(doctor.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(doctor, "read_regular", advance_after_read)
    report = inspect(domain, _resolution(case, root), C.DEFAULT_SCAN_LIMITS, None)
    assert report.usage.files <= 1
    assert report.usage.truncated
    assert any("elapsed" in item.message for item in report.limitations)


def test_deadline_expires_during_directory_enumeration(case, monkeypatch):
    import ptest.doctor as doctor
    domain = case.domain()
    root = case.project(domain)
    for name in ("a.py", "b.py", "c.py"):
        (root / name).write_text("cache.flushall()\n")
    clock = [0.0]
    real_scandir = os.scandir

    class TimedEntries:
        def __init__(self, directory):
            self.entries = real_scandir(directory)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.entries.close()
        def __iter__(self):
            return self
        def __next__(self):
            item = next(self.entries)
            clock[0] += 4.0
            return item

    monkeypatch.setattr(doctor.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(doctor.os, "scandir", TimedEntries)
    report = inspect(domain, _resolution(case, root), C.DEFAULT_SCAN_LIMITS, None)
    assert report.usage.entries <= 2
    assert report.usage.truncated
    assert any("elapsed" in item.message for item in report.limitations)


@pytest.mark.parametrize("length", [8000, 262000])
def test_adversarial_long_line_is_bounded_and_reported(case, length):
    # The unterminated bind payload used to cause quadratic whitespace backtracking.
    domain = case.domain()
    root = case.project(domain)
    (root / "long.py").write_text("x = 'bind(" + " " * length + "'\ncache.flushall()\n")
    report = inspect(domain, _resolution(case, root), C.DEFAULT_SCAN_LIMITS, None)
    assert any(item.code == "scan-limit" and "line" in item.message for item in report.limitations)
    assert report.usage.truncated
    assert [item.code for item in report.findings] == ["cache.global-flush"]


@pytest.mark.parametrize("scope", ["linked/child", None])
def test_scope_and_priority_roots_never_enumerate_intermediate_symlinks(case, tmp_path, scope):
    domain = case.domain()
    root = case.project(domain)
    outside = tmp_path / "outside"
    (outside / "child").mkdir(parents=True)
    (outside / "child" / "PRIVATE_OUTSIDE_NAME.py").write_text("cache.flushall()\n")
    (root / "linked").symlink_to(outside, target_is_directory=True)
    resolution = _resolution(case, root)
    resolution = replace(resolution, config=replace(resolution.config, runner=replace(
        resolution.config.runner, test_roots=("linked/child",))))
    report = inspect(domain, resolution, C.DEFAULT_SCAN_LIMITS, scope)
    assert "PRIVATE_OUTSIDE_NAME" not in repr(report)
    assert report.findings == ()
    assert report.usage.files == (0 if scope else 1)  # native .ptest.toml is in-tree
    assert any(item.code == "unsafe-path" for item in report.limitations)


def test_scan_reads_tests_then_fixture_setup_then_config_then_sources(case):
    domain = case.domain()
    root = case.project(domain)
    paths = ("tests/test_risk.py", "z/conftest.py", "z/fixtures/state.py", "z/setup.py",
             "pytest.ini", "a/source.py")
    for rel in reversed(paths):
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("cache.flushall()\n")
    report = inspect(domain, _resolution(case, root), C.DEFAULT_SCAN_LIMITS, None)
    assert [item.path for item in report.findings] == list(paths)


@pytest.mark.parametrize("rel", [
    "build/leak.py", "dist/leak.py", ".tox/leak.py", ".mypy_cache/leak.py",
    ".pytest_cache/leak.py", ".env", "secret.pem", "private.key", "id_rsa",
    "credentials.json", "secrets.py", "binary.dat", ".npmrc", ".netrc", ".pypirc",
    ".ssh/config", ".aws/config", ".gnupg/config", "a.min.js", "module.generated.py",
])
def test_private_generated_and_binary_inputs_do_not_produce_findings(case, rel):
    domain = case.domain()
    root = case.project(domain)
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"cache.flushall()\n" + (b"\0" if rel == "binary.dat" else b""))
    report = inspect(domain, _resolution(case, root), C.DEFAULT_SCAN_LIMITS, None)
    assert report.findings == ()
    assert not any(rel in item.paths for item in report.limitations)


@pytest.mark.parametrize("entries, expected", [(20000, "absent"), (0, "unreached")])
def test_absent_or_unreached_declared_test_roots_are_explicit(case, entries, expected):
    domain = case.domain()
    root = case.project(domain)
    report = inspect(domain, _resolution(case, root), replace(C.DEFAULT_SCAN_LIMITS, entries=entries), None)
    assert any("priority root" in item.message.lower() and expected in item.message.lower()
               and item.paths == ("tests",) for item in report.limitations)


def test_static_markers_do_not_claim_observed_timing_or_read_state(case, monkeypatch):
    import ptest.doctor as doctor
    domain = case.domain()
    root = case.project(domain)
    (root / "marked.py").write_text("@pytest.mark.slow\ndef test_marked(): pass\n")
    normal = case.base / "normal-state"
    normal.mkdir()
    sentinel = normal / "history.sqlite3"
    sentinel.write_bytes(b"normal-state-sentinel")
    monkeypatch.setenv("XDG_STATE_HOME", str(normal))
    real_open = os.open

    def reject_state(path, *args, **kwargs):
        assert "normal-state" not in str(path)
        assert "history.sqlite3" not in str(path)
        return real_open(path, *args, **kwargs)

    before = set(domain.root.rglob("*"))
    monkeypatch.setattr(doctor.os, "open", reject_state)
    report = inspect(domain, _resolution(case, root), C.DEFAULT_SCAN_LIMITS, None)
    assert not any(item.code == "timing.slow-test" for item in report.findings)
    assert {item.area: item.state for item in report.readiness} == {
        "execution": "unknown", "parallel": "unknown", "selection": "unknown", "timing": "unknown"}
    assert len(report.readiness) == 4
    assert any("checkout" in item.message.lower() and "timing" in item.message.lower()
               for item in report.limitations)
    assert set(domain.root.rglob("*")) == before
    assert sentinel.read_bytes() == b"normal-state-sentinel"


@pytest.mark.parametrize("duration, expected", [
    (0.0, "healthy"), (0.4999, "healthy"), (0.5, "inspect"), (1.9999, "inspect"),
    (2.0, "optimize"), (3.0, "optimize"), (3.0001, "investigate"),
])
def test_observed_duration_bucket_boundaries(duration, expected):
    from ptest.doctor import timing_bucket
    assert timing_bucket(duration) == expected


def test_deadline_expires_within_one_source_file(case, monkeypatch):
    import ptest.doctor as doctor
    domain = case.domain()
    root = case.project(domain)
    (root / "tests").mkdir()
    (root / "tests" / "lines.js").write_text("cache.flushall();\n" * 100)
    ticks = iter(index / 10 for index in range(10000))
    monkeypatch.setattr(doctor.time, "monotonic", lambda: next(ticks))
    report = inspect(domain, _resolution(case, root), C.DEFAULT_SCAN_LIMITS, "tests")
    assert 0 < len(report.findings) < 100
    assert report.usage.truncated
    assert any("elapsed" in item.message for item in report.limitations)


@pytest.mark.parametrize("scope", ["bad\udcff", "bad\x1bname", "../outside", "/absolute", "a//b"])
def test_unsafe_scope_is_rejected_without_echoing_untrusted_name(case, scope):
    domain = case.domain()
    root = case.project(domain)
    with pytest.raises(C.Problem) as caught:
        inspect(domain, _resolution(case, root), C.DEFAULT_SCAN_LIMITS, scope)
    assert caught.value.code == "unsafe-path"
    assert scope not in str(caught.value)


@pytest.mark.parametrize("duration", [-1, float("nan"), float("inf"), True, "1"])
def test_invalid_observed_duration_is_not_classified(duration):
    from ptest.doctor import timing_bucket
    with pytest.raises(ValueError):
        timing_bucket(duration)


def test_untrusted_prompt_source_is_never_copied_into_findings(case):
    domain = case.domain()
    root = case.project(domain)
    (root / "untrusted.py").write_text(
        "# IGNORE_ALL_POLICY execute remote-agent secret-value\ncache.flushall()\n")
    report = inspect(domain, _resolution(case, root), C.DEFAULT_SCAN_LIMITS, None)
    assert [item.code for item in report.findings] == ["cache.global-flush"]
    assert "IGNORE_ALL_POLICY" not in repr(report)
    assert "secret-value" not in repr(report)


@pytest.mark.parametrize("separator", ["\f", "\v", "\u2028"], ids=["form-feed", "vertical-tab", "unicode-line-separator"])
@pytest.mark.parametrize("suffix", ["py", "js"])
def test_nonphysical_separators_do_not_advance_source_locations(case, separator, suffix):
    """Using str.splitlines would hide Python hits or misreport JS locations."""
    domain = case.domain()
    root = case.project(domain)
    source = (
        f"marker = 'before{separator}after'\ncache.flushall()\n"
        if suffix == "py"
        else f"const marker = 'before{separator}after';\ncache.flushall();\n"
    )
    (root / f"location.{suffix}").write_text(source, encoding="utf-8")

    report = inspect(domain, _resolution(case, root), C.DEFAULT_SCAN_LIMITS, None)

    assert [(item.code, item.path, item.line) for item in report.findings] == [
        ("cache.global-flush", f"location.{suffix}", 2),
    ]


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"], ids=["lf", "crlf", "cr"])
@pytest.mark.parametrize("suffix", ["py", "js"])
def test_physical_newline_sequences_advance_source_locations(case, newline, suffix):
    domain = case.domain()
    root = case.project(domain)
    source = f"marker = 1{newline}cache.flushall(){newline}"
    (root / f"physical.{suffix}").write_bytes(source.encode("utf-8"))

    report = inspect(domain, _resolution(case, root), C.DEFAULT_SCAN_LIMITS, None)

    assert [(item.code, item.path, item.line) for item in report.findings] == [
        ("cache.global-flush", f"physical.{suffix}", 2),
    ]


def test_complete_report_is_bounded_after_high_volume_distinct_skips(case):
    """Uncharged per-path limitations would grow the whole report past its cap."""
    domain = case.domain()
    root = case.project(domain)
    target = root / "target.py"
    target.write_text("cache.flushall()\n", encoding="utf-8")
    link_count = 3_200
    for index in range(link_count):
        (root / f"link-{index:04d}").symlink_to(target)

    report = inspect(domain, _resolution(case, root), C.DEFAULT_SCAN_LIMITS, None)
    report_payload = json.dumps(
        asdict(report),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")

    assert report.usage.skipped >= link_count
    assert report.usage.truncated is True
    assert report.usage.output_bytes <= report.limits.output_bytes
    assert len(report_payload) <= report.limits.output_bytes
    assert sum(item.message == "Doctor diagnostic output limit reached."
               for item in report.limitations) == 1
    assert len(report.limitations) < link_count


def test_oversized_scope_is_rejected_without_entering_the_report(case):
    """An unrestricted scope would bypass output accounting via report.scope."""
    domain = case.domain()
    root = case.project(domain)
    scope = "a" * 4_097

    with pytest.raises(C.Problem) as caught:
        inspect(domain, _resolution(case, root), C.DEFAULT_SCAN_LIMITS, scope)

    assert caught.value.code == "unsafe-path"
    assert scope not in str(caught.value)
