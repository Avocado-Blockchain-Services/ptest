"""Static doctor regression coverage (Task 9)."""
from __future__ import annotations

from pathlib import Path
import shutil
import os

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
        "timing.slow-test": "@pytest.mark.slow\ndef test_slow(): pass",
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
    assert report.usage.skipped >= 2
    assert all("secret.invalid" not in item.path for item in report.findings if item.path)
    assert {reason.code for reason in report.limitations} >= {"scan-limit", "unsafe-path", "static-evidence-insufficient"}

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
