# T1 report: update check and `ptest update` (finish)

Branch: `feature/update-check-T1`. Base: WIP `e1b52f0` (interrupted run),
finished in `b27356e`. No push, no merge, main untouched. Docs untouched (T2).

## Root cause of the 5 failures

Two independent bugs, both reproduced before fixing.

1. Four `did not switch the launcher` failures
   (`test_success_installs_side_by_side_and_switches`,
   `test_reexec_failure_continues_with_next_run_notice`,
   `test_explicit_older_version_installs`,
   `test_accept_updates_then_reexecs_with_literal_argv`).
   Cause was in the test fixture, not the product: `_fake_install_sh`
   wrote the fake bundle marker with
   `printf '{"version":1,"bundle_id":"$V-test",...}'`. Single quotes
   suppress shell expansion, so the marker kept the literal strings
   `$V-test` / `$V`. `uninstall._is_bundle_dir` then correctly refused
   the bundle (bundle_id mismatch) and `_post_verify` correctly reported
   `the installer did not switch the launcher to <v>`. Verified by
   extracting the fixture tarball by hand: `complete.json` contained
   `{"bundle_id":"$V-test",...}`. Fix: expand the version in Python when
   building the fixture script (`tests/ng/test_update.py:137`). The
   product verifier matches the real installer contract (relative
   symlink `<root>/ptest` into `.ptest-bundles/<id>/venv/bin/ptest`,
   `complete.json` with `version: 1`, matching `bundle_id`, matching
   `ptest_version`).
2. `test_stage_paths_reject_existing_files` (`io.open`/FileNotFoundError).
   Cause in the product: `_bounded_copy` wrapped `os.open(O_EXCL)` inside
   the try whose `except C.Problem` unlinks the target, so a rejected
   pre-existing stage file was deleted. Fix: open outside the
   cleanup-guarded try so only files this call created are ever unlinked
   (`src/ptest/update.py:328`, mirroring `_download_to`). The over-cap
   cleanup contract is unchanged.

## Per-criterion completion

- `update.py` ownership (resolution via `/releases/latest` Location
  exact-match, no token; cache; download; SHA-256; safe extract; run
  bundled `install.sh --dest <root>` as `get.sh` does; `cli.py` only
  wires): present from WIP, verified against `install.sh`/`install.py`.
- `ptest update` (latest or `--version X`; HTTPS fixed host; verify
  before anything runs; side-by-side + atomic switch; S5 up-to-date
  exit 0; `--check`; no downgrade unless named): verified, tests green.
- Negative contracts: all tested in `test_update.py`; fixed the
  pre-existing-stage-file violation above; every other contract held.
- No real network/install in tests: `FakeTransport`, tmp install root,
  conftest `PTEST_NO_UPDATE_CHECK=1` + network-deny guard; no `invoke()`
  in `test_update.py`. Verified.
- Startup check: wired in `cli.main` (`cli.py:3573`, exempt
  help/version/update); 24 h claim-then-fetch cache in state area
  (`PTEST_STATE_DIR` respected, tested); 2 s daemon-thread bound;
  silent on errors; exit status/output untouched; TTY prompt + re-exec
  with literal argv; non-TTY S2; opt-outs (`PTEST_NO_UPDATE_CHECK`,
  truthy `CI`, `--fixture-domain`, `-q`); source refusal S4.
  One fidelity fix: flush stdout as well as stderr before re-exec
  (design 3.3 step 10).
- `update --json` document + `docs/schemas/v1/update.json`:
  `scripts/export-schemas.py --check` exits 0. Verified.

## Final test lines

- `ptest --workers 2 --queue-timeout 1800 tests/ng/test_update.py tests/ng/test_cli.py tests/ng/test_contracts.py tests/ng/test_help.py` → **622 passed** (was 617 passed + 5 failed).
- Extended gate adding `test_install.py test_uninstall.py` (shared installer/layout code) → **705 passed**; `src/ptest/update.py` coverage **89%**.
- `python scripts/export-schemas.py --check` → clean, exit 0.

Not done (not in task run list, stated plainly): design 4.7's aspirational
90% coverage gate (89% observed), `bandit` SAST, `graphify update .`.
