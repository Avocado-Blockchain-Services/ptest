# T2 report — faster `rank_candidates`, identical selection

- taskWorktree: `/home/ingmar/worktrees/ptest/cc-doctor-offline/ptest-T2`
- taskBranch: `feature/doctor-offline-T2`
- base commit: `09f6f40` (chain branch head at task start)
- task commit: `0976d17` — "T2: faster rank_candidates with identical selection"
- status: DONE (all acceptance criteria met; no BLOCKED)

## What changed (owned files only)

- `src/ptest/review_evidence.py` (+129/−31):
  - `_ContextIndex` (frozen, slots: `roles` Mapping, `outgoing`/`incoming`/`config_paths` frozensets), `_build_context_index`, `_context_index` (memoized under `signal_cache[("context-index", id(context))] = (context, index)`, reused only when stored object `is context`; the stored ref pins the id against reuse).
  - `_relation_score` / `_source_role` / `_is_config_path` keep positional parameters, gain keyword-only `index`. `_item_rank` / `_has_item_anchor` gain keyword-only `index`, resolved via `_context_index(context, signal_cache)` when absent.
  - `_compiled_patterns` (`functools.lru_cache(maxsize=256)`); `_resource_patterns` delegates to it.
  - `_call_names_hit(patterns, call_names)`: joins names once as `"\n".join(f"{n}\n{n}(" ...)`, one MULTILINE prefilter search per pattern (variant cached per pattern in `_CALL_HIT_PREFILTERS`), exact per-name loop only on prefilter hit; sources containing `\A`, `\Z`, `(?<`, `(?=`, `(?!` always use the exact loop. Used by `_item_signal`.
  - `_IDENTIFIER_SPLIT_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|_+")`; `_python_identifier_text` does one `.sub` per text.
  - `rank_candidates` / `rank_item_candidates` use a local `{}` when given `None`, build the index once, pass it down. `item_source_chains` also localizes a `None` cache (memo-only, outputs identical).
  - Public signatures of `rank_candidates`, `rank_item_candidates`, `select_item_sources`, `item_source_chains`, `source_units`, `source_id` UNCHANGED.
- `tests/ng/test_rank_candidates_perf.py` (NEW, 11 tests): base goldens, differential oracles, operation counts.
- `scripts/bench_rank_candidates.py` (NEW, not a test): generates ~1000-file/2-child fixture, times `build_packets` (exercises ranking), prints one JSON line.

## Acceptance criteria

1. Per-context lookups precomputed once per call — proven by `test_context_index_built_once_per_rank_candidates` (counts `_build_context_index`: exactly 1 per `rank_candidates` / `rank_item_candidates` call, `None` or explicit cache).
2. One pattern match against joined call names per file — proven by `test_call_names_hit_prefilters_without_exact_searches` (zero exact-loop searches + exactly `len(patterns)` prefilter searches on the no-hit path).
3. Single regex pass for identifiers — proven by `test_python_identifier_text_single_regex_pass` (exactly 1 `.sub` via module-attribute proxy).
4. Identical results — 5 base-captured golden tests (rank tuple, DB-001 rank, chains, standalone `packet_sha256`, v2 child `packet_sha256`) plus 3 differential oracles (identifier corpus incl. `camelCase`, `HTTPServer`, `a1B`, `__dunder__`, `snake__case_`, non-ASCII, syntax-error text; relation/role/config over every fixture path; call-hit over every catalog entry's patterns plus crafted `^ $ \b \s \A` lookbehind/lookahead patterns).
5. Bench before/after (below).
6. Public signatures unchanged — verified by grep + passing callers (`agent_assessment.py:1932`, existing `test_review_evidence.py` unmodified).

## Golden capture record

- Capture commands (throwaway probes, kept out of the repo at `/tmp/capture_goldens.py`, `/tmp/capture_packets.py`):
  `uv run --locked --extra test python /tmp/capture_goldens.py` and `... /tmp/capture_packets.py`, run on UNMODIFIED code at `09f6f40`; literals pasted into the test file before implementing.
- Perf fixture: 30 py + 10 js + 6 tests + conftest + config + helper + setup = 50 roles (>30), 12 relations incl. `fixture-use` edges, fixed content (no `.ptest.toml` project_id needed for rank goldens; packet goldens use `doctor_assessment_project(root, "aa"*8)` and a v2 `api` child with `agent_v1_config_text("bb"*16, "pytest")`).

## Verification (commands run in the task worktree)

- RED: `ptest --workers 2 --queue-timeout 1800 tests/ng/test_rank_candidates_perf.py` on base → new-name tests FAILED (`_call_names_hit` etc. absent), goldens passed. Direct probe also showed all 7 new names absent.
- GREEN: `ptest --workers 2 --queue-timeout 1800 tests/ng/test_rank_candidates_perf.py tests/ng/test_review_evidence.py tests/ng/test_agent_assessment.py tests/ng/test_review_context.py` → **268 passed** (exit 0). Re-run of the perf file post-stash → **11 passed**.
- No wall-clock assertions in tests. No migration. No push/merge/deploy. `graphify update .` run; committed `0976d17`; worktree clean.

## Benchmark (informative only, not asserted)

- Base (stashed src change): `{"mode": "rank_candidates", "files": 1002, "seconds": 13.156}`
- After: `{"mode": "rank_candidates", "files": 1002, "seconds": 7.628}`, repeat `7.696` → ~1.7× on the ranking path.

## Re-read findings (dan-jefferies Pass 1/3, summary)

- One benign note: `_CALL_HIT_PREFILTERS` keys on pattern objects, so callers must pass hashable patterns — true for every producer (`_compiled_patterns` outputs, test proxies). No handling added; an unhashable pattern would fail loudly, not silently.
- No stubs/TODOs; no duplicate concept (grep for synonyms: no existing memo/context-index/call-hit helper); `_unit_priority` takes no context so no index threading applied there; contract drift checked — all producers/consumers of touched helpers are inside `review_evidence.py` except public rank functions and one positional `_source_role` test call, all green.
- Edge paths: empty patterns/names → `False`; syntax-error text → `""`; `signal_cache=None` → local dict; stale/different context under same cache → identity check rebuilds. All covered by tests above.
- Claimed-vs-shipped delta: none. Deferred: none. Out-of-scope smells: none observed.

## Integration notes

- No seam: T2 touches only its owned files and needs nothing from T1/T3. T1's identity test will exercise T2's ranking inside the reference full path after merge; goldens here guarantee selection is unchanged.
- Confidence: high — identical-output proof is golden + differential, reductions are operation-counted, and the full scoped suite passes unmodified.

## Fix round 1 (audit response: bench now isolates ranking time)

- Finding: `scripts/bench_rank_candidates.py` timed the whole `AA.build_packets` call and labelled it `{"mode": "rank_candidates"}`; the 13.156 s → 7.628 s figures were whole-pipeline totals, not ranking. Design §T2 step 4 requires timing `rank_candidates` captured through `build_packets`.
- Fix (task worktree `ptest-T2`, commit `563a447`, one file): `main()` warms up unwrapped, then monkeypatches the `review_evidence.rank_candidates` module attribute with a wall-time-accumulating wrapper for the timed `build_packets` call, restoring it in `finally`. `_build_one_packet` does `from . import review_evidence as RE` at call time (`src/ptest/agent_assessment.py:1929`) and calls `RE.rank_candidates` once per child (`:1932`, the only ranking call in the packet path), so the patch intercepts every ranking call and nothing else. Output is now `{"mode": "rank_candidates", "files", "seconds" (= ranking-only sum), "total_seconds" (= whole-pipeline span), "rank_calls"}`. `rank_calls == 2` on every run below is the non-vacuous guard: a missed patch would report `seconds: 0.0, rank_calls: 0`.
- Re-recorded with the amended script (1002 files; base = `src/ptest/review_evidence.py` at `09f6f40` swapped over the worktree, HEAD = `0976d17` + fix; box load ~13, so wall clock is noisy — figures informative only):
  - Base ranking-only `seconds`: `13.279`, `13.851` (totals `17.63`, `18.704`).
  - HEAD ranking-only `seconds`: `12.616`, `5.346`, `9.23`, `8.028` (totals `15.526`, `6.94`, `12.73`, `11.251`).
  - Medians: ~13.6 s base vs ~8.6 s HEAD on the ranking path (~1.6×); the old whole-pipeline numbers are superseded and must not be quoted as ranking.
- Verification: `ptest --workers 2 --queue-timeout 1800 tests/ng/test_review_evidence.py` in `ptest-T2` → `ptest: passed · 36 tests` (exit 0; `36 passed in 47.06s`). Bench selection-identity is unchanged by construction (wrapper delegates to the original); goldens/differentials from the main report still hold and no src file was touched in this round.
- Incidents: a `git stash push -- src/ptest/review_evidence.py` for the base run reported "No local changes to save" (the T2 src change is committed), and the trailing `git stash pop` consumed a pre-existing stash (`92155c2`) that rewrote `.ptest.toml` (workers 8→1, split cov args, dev-group setup argv, rotated project_id). `.ptest.toml` is not T2-owned; it was restored to HEAD (`git checkout -- .ptest.toml`) and the tree is otherwise untouched — final diff of this round is the bench script only. Base runs were done by explicit `git show 09f6f40:...` file swap with byte restore afterwards (verified via `git status`: only the bench script modified).
- dan-jefferies passes: re-read the diff (found nothing beyond the intended hunk; `functools.wraps` metadata copy is inert here); re-anchored to the finding (wrap module attribute ✓, report sum + optional total ✓, re-record both ✓, scoped tests ✓); smell sweep — no src change, no new helper (no shared timing helper exists; `perf_counter` appears only in this script), no consumers of the bench JSON in-repo, restore-on-exception covered, edge `rank_calls: 0` case would self-evidently report zeros.
- Status: implemented — bench isolates ranking time; verified — 36-test scoped run green, interception proved by `rank_calls: 2`, base/after re-recorded; not verified — full suite (`--full` is the orchestrator's gate); deferred — none; discovered-but-not-fixed — none in scope (the `.ptest.toml` rewrite source is unknown; flagged here, file restored).
- Confidence: medium-high — the isolation mechanism is proven by `rank_calls`, but absolute speedups are load-noisy and should be re-taken on a quiet box before quoting.
