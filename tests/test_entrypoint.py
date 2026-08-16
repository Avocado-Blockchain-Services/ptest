"""Source-level contracts for the remote runner shell entrypoint."""
from pathlib import Path


ENTRYPOINT = Path(__file__).resolve().parent.parent / "entrypoint.sh"
DOCKERFILE = Path(__file__).resolve().parent.parent / "Dockerfile.pytest"


def test_pytest_precompiles_source_and_tests_after_sync_before_running_suite():
    """Precompilation is a guarded pytest-path runner step, not a Vitest step."""
    source = ENTRYPOINT.read_text()

    vitest_path = source.index('if [ "$PTEST_KIND" = "vitest" ]')
    vitest_exit = source.index('exit "$_rc"', vitest_path)
    pytest_path = source.index('# ── 2. Boot Postgres in RAM (pytest path only)')
    sync = source.index('uv sync --frozen')
    compile_step = source.index('uv run python -m compileall -q src tests')
    run_suite = source.index('log "running: $PTEST_CMD"', pytest_path)

    assert vitest_path < vitest_exit < pytest_path < sync < compile_step < run_suite
    assert '[ -d src ] && [ -d tests ]' in source
    assert 'die "bytecode precompilation failed"' in source


def test_the_pytest_image_ships_redis():
    """fullon2 touches Redis in 102 test files; Postgres alone is not enough."""
    assert "redis-server" in DOCKERFILE.read_text()


def test_redis_starts_on_the_pytest_path_before_the_suite():
    source = ENTRYPOINT.read_text()

    vitest_exit = source.index('exit "$_rc"',
                               source.index('if [ "$PTEST_KIND" = "vitest" ]'))
    redis = source.index("redis-server")
    run_suite = source.index('log "running: $PTEST_CMD"', vitest_exit)

    assert vitest_exit < redis < run_suite, \
        "Redis belongs to the pytest path, started before the suite runs"


def test_a_redis_that_will_not_start_is_a_runner_failure_not_a_red_suite():
    """Exit 2 means "the runner broke"; a missing service must not read as failing tests."""
    source = ENTRYPOINT.read_text()
    redis_block = source[source.index("redis-server"):]
    assert 'die "redis' in redis_block.lower() or "die \"redis" in redis_block.lower()


def test_redis_does_not_persist_anything():
    """A test cache that writes an RDB to a RAM filesystem costs memory for nothing."""
    source = ENTRYPOINT.read_text()
    assert "--save ''" in source or '--save ""' in source
