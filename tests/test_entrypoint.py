"""Source-level contracts for the remote runner shell entrypoint."""
from pathlib import Path


ENTRYPOINT = Path(__file__).resolve().parent.parent / "entrypoint.sh"


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
