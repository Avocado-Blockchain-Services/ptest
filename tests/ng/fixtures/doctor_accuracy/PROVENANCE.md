# Doctor-accuracy Milestone 1 source fixtures (synthetic, no Persea code).
#
# Provenance: hand-written for this task on 2026-09-25. Labels, if any are
# added later, are drafts until a recorded human adjudication marks them
# maintainer-reviewed (see the design doc, section 4).
#
# - vitest-single: one Vitest config with literal include/setupFiles, a unit
#   test importing a local helper, and an excluded Playwright e2e spec.
# - vitest-twins: two conflicting Vitest configs; selection must stay
#   partial unless an explicit literal --config disambiguates.
# - pytest-closure: conftest fixture used by a unit test plus an unrelated
#   test; the fixture/helper closure must outrank generic files.
# - Python source samples use the inert `.py.sample` suffix in this repository
#   and are materialized as `.py` only in test-owned temporary checkouts.
