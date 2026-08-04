import subprocess
from pathlib import Path


SCRIPT = Path(__file__).resolve().parent.parent / "smoke.sh"
KEY = "a" * 64


def run_fixture_assertion(tmp_path, owner, joiner, cached):
    paths = []
    for name, content in (("owner.log", owner), ("joiner.log", joiner), ("cached.log", cached)):
        path = tmp_path / name
        path.write_text(content)
        paths.append(path)
    return subprocess.run(
        ["bash", str(SCRIPT), "--assert-dedup-fixtures", *(str(path) for path in paths)],
        text=True,
        capture_output=True,
    )


def test_smoke_parser_accepts_same_request_join_and_cache_reuse(tmp_path):
    result = run_fixture_assertion(
        tmp_path,
        f"[ptest] request: {KEY}\n[ptest] remote: fake-job\n",
        f"[ptest] request: {KEY}\n[ptest] joining identical remote execution fake-job-abc12\n",
        f"[ptest] request: {KEY}\n[ptest] reused passing result from fake-job-abc12\n",
    )

    assert result.returncode == 0, result.stderr
    assert "dedup fixtures: request" in result.stdout


def test_smoke_parser_rejects_mismatched_request_keys(tmp_path):
    result = run_fixture_assertion(
        tmp_path,
        f"[ptest] request: {KEY}\n",
        f"[ptest] request: {'b' * 64}\n[ptest] joining identical remote execution fake-job-abc12\n",
        f"[ptest] request: {KEY}\n[ptest] reused passing result from fake-job-abc12\n",
    )

    assert result.returncode != 0
    assert "request keys differ" in result.stderr


def test_smoke_parser_accepts_either_concurrent_caller_as_joiner(tmp_path):
    result = run_fixture_assertion(
        tmp_path,
        f"[ptest] request: {KEY}\n[ptest] joining identical remote execution fake-job-abc12\n",
        f"[ptest] request: {KEY}\n[ptest] remote: fake-job\n",
        f"[ptest] request: {KEY}\n[ptest] reused passing result from fake-job-abc12\n",
    )

    assert result.returncode == 0, result.stderr


def test_smoke_parser_accepts_same_host_lock_join_and_local_cache_hit(tmp_path):
    result = run_fixture_assertion(
        tmp_path,
        f"[ptest] request: {KEY}\n[ptest] remote: fake-job\n",
        f"[ptest] request: {KEY}\n[ptest] joined identical local request {KEY}\n",
        f"[ptest] request: {KEY}\n[ptest] reused passing local result for {KEY}\n",
    )

    assert result.returncode == 0, result.stderr


def test_readme_preemption_smoke_baselines_generation_after_vm_stop():
    readme = (Path(__file__).resolve().parent.parent / "README.md").read_text()
    stop = readme.index("gcloud compute instances stop")
    baseline = readme.index("post_stop_generation=")
    comparison = readme.index('"$new_generation" -gt "$post_stop_generation"')

    assert stop < baseline < comparison


def test_readme_spot_smoke_rejects_local_fallback_and_remote_failure():
    readme = (Path(__file__).resolve().parent.parent / "README.md").read_text()
    live_section = readme.split("After a separately approved apply", 1)[1]
    block = live_section.split("```bash", 1)[1].split("```", 1)[0]

    assert "ptest --full --fresh 2>spot-request.log" in block
    assert "spot_rc=$?" in block
    assert 'test "$spot_rc" -eq 0' in block
    assert "slow_rc=$?" in block
    assert 'test "$slow_rc" -eq 0' in block
    assert block.count('d["status"] == "passed" and d["exit_code"] == 0') == 2
    assert block.count("assert json.load(sys.stdin) == []") == 2


def test_readme_calls_the_monitoring_overflow_gate_advisory_not_a_hard_cap():
    readme = (Path(__file__).resolve().parent.parent / "README.md").read_text()
    prose = " ".join(readme.split())

    assert "advisory overflow brake" in prose
    assert "not a hard admission cap" in prose
