from datetime import datetime, timedelta, timezone

import pytest


IDENTITY = {
    "schema": "ptest-remote-key-v1",
    "project": "persea-front",
    "kind": "vitest",
    "command": "npm test -- --coverage",
    "job": "ptest-persea-front",
    "region": "us-central1",
    "gcp_project": "seed-staging",
    "runner_namespace": "v1",
    "tree_digest": "a" * 64,
}


def test_request_key_is_canonical_and_every_identity_field_matters(ptest):
    baseline = ptest.remote_request_key(IDENTITY)
    assert baseline == ptest.remote_request_key(dict(reversed(list(IDENTITY.items()))))
    assert len(baseline) == 64

    for field in IDENTITY:
        changed = dict(IDENTITY)
        changed[field] = changed[field] + "-changed"
        assert ptest.remote_request_key(changed) != baseline, field


@pytest.mark.parametrize("age, expected", [(3599.999, True), (3600, False), (3601, False)])
def test_passing_record_freshness_has_an_exact_one_hour_boundary(ptest, age, expected):
    completed = datetime(2026, 8, 3, 12, tzinfo=timezone.utc)
    record = {
        "schema": "ptest-pass-v1",
        "status": "passed",
        "completed_at": completed.isoformat(),
    }

    assert ptest.passing_record_is_fresh(
        record, completed + timedelta(seconds=age)
    ) is expected


@pytest.mark.parametrize(
    "record",
    [
        {},
        {"schema": "wrong", "status": "passed", "completed_at": "2026-08-03T12:00:00+00:00"},
        {"schema": "ptest-pass-v1", "status": "failed", "completed_at": "2026-08-03T12:00:00+00:00"},
        {"schema": "ptest-pass-v1", "status": "passed", "completed_at": "not-a-time"},
        {"schema": "ptest-pass-v1", "status": "passed", "completed_at": "2026-08-03T12:00:00"},
    ],
)
def test_malformed_or_nonpassing_records_are_never_fresh(ptest, record):
    now = datetime(2026, 8, 3, 12, tzinfo=timezone.utc)
    assert ptest.passing_record_is_fresh(record, now) is False


def test_coordination_paths_are_content_addressed_and_contain_no_raw_inputs(ptest):
    key = "b" * 64
    digest = "c" * 64

    paths = ptest.coordination_paths(key, digest)

    assert paths.claim == f"coord/v1/{key}/claim.json"
    assert paths.execution == f"coord/v1/{key}/execution.json"
    assert paths.passing == f"coord/v1/{key}/pass.json"
    assert paths.source == f"sources/{digest}.tar.gz"


@pytest.mark.parametrize("key,digest", [("short", "c" * 64), ("b" * 64, "../escape")])
def test_coordination_paths_reject_unvalidated_object_names(ptest, key, digest):
    with pytest.raises(ValueError):
        ptest.coordination_paths(key, digest)


@pytest.mark.parametrize(
    "ttl,claim,namespace",
    [(0, 2100, "v1"), (3600, 1800, "v1"), (3600, 2100, "../v1"), (3600, 2100, "")],
)
def test_remote_cache_settings_reject_invalid_values(ptest, ttl, claim, namespace):
    with pytest.raises(ValueError):
        ptest.remote_cache_settings(
            {"remote_cache_ttl_seconds": ttl,
             "remote_claim_ttl_seconds": claim,
             "runner_namespace": namespace},
            {},
        )


def test_remote_cache_settings_use_documented_defaults(ptest):
    settings = ptest.remote_cache_settings({}, {})
    assert settings.ttl_seconds == 3600
    assert settings.claim_ttl_seconds == 2100
    assert settings.runner_namespace == "v1"
