# Shadow sensitivity fixture

The four-file project is intentionally small: `src/shared.py` is mapped only
to `tests/test_alpha.py` in the bad policy, although `tests/test_beta.py` also
consumes it. A value change therefore leaves selected alpha passing while the
complete full gate finds beta's real failure. Gamma and delta are independent
controls.
