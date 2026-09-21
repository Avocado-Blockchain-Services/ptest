# ptest NG performance evidence

Performance is reported from retained finite samples, including failures and
timeout/cap samples. `scripts/benchmark.py` uses nearest-rank p95 and marks a
summary non-promotable when any attempt fails or is capped.

Run a bounded local sample set directly:

```sh
python scripts/benchmark.py --root "$PWD" --samples 20 --timeout 30 \
  --output /tmp/ptest-benchmark-evidence -- /absolute/path/to/ptest --version
```

The command stores stdout/stderr per attempt and a JSON record containing the
literal command, environment metadata, source commit, sample values, and
summary. It does not measure a remote service, invent memory readings, or
change project configuration.

The design targets (wrapper p95 ≤250ms, plan p95 ≤2s, completed doctor p95
≤5s, and the specified adoption/S2 paired samples) are release evidence gates,
not claims made by this document. No advanced-adoption or cross-platform
measurement is currently promoted.
