# ptest NG performance evidence

Performance is reported from retained finite samples, including failures and
timeout/cap samples. `scripts/benchmark.py` uses nearest-rank p95 and marks a
summary non-promotable when any attempt fails or is capped.

Run a bounded local sample set directly:

```sh
python scripts/benchmark.py --root "$PWD" --candidate /absolute/path/to/ptest \
  --profile local-miniature-v1 --samples 20 --timeout 30 \
  --output /tmp/ptest-benchmark-evidence -- /absolute/path/to/ptest --version
```

The command stores stdout/stderr per attempt and a JSON record containing the
literal command, environment metadata, source commit, sample values, and
summary. It does not measure a remote service, invent memory readings, or
change project configuration. An arbitrary command or untyped candidate is
retained as diagnostic evidence but is never promotable. The current
version-only diagnostic is also deliberately non-promotable.

The typed `local-miniature-v1` workload declares exactly 20 warmups, 20
completed-doctor samples, 5 alternating full/selected pairs, and 10 paired S2
trials for each of: default unknown memory, known estimates with a configured
budget, and unknown memory with opt-in reservation. Each sample records setup,
queue wait, execution, observed RSS, completion, exit code, inventory digest,
and coverage digest. A cap-truncated doctor sample is incomplete, not a fast
pass. The harness currently records the schema and local version smoke only;
these performance targets remain unverified.

The design targets (wrapper p95 ≤250ms, plan p95 ≤2s, completed doctor p95
≤5s, and the specified adoption/S2 paired samples) are release evidence gates,
not claims made by this document. No advanced-adoption or cross-platform
measurement is currently promoted.
