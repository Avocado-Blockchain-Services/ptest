import json
import subprocess

import pytest


BASE = ["gcloud", "--project=test-project", "--account=test@example.invalid"]


def completed(args, returncode=0, stdout=b"", stderr=b""):
    return subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr=stderr)


def test_create_json_uses_a_create_only_generation_precondition(ptest, monkeypatch):
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        payload = json.loads(open(args[-3], encoding="utf-8").read())
        assert payload == {"owner": "one"}
        return completed(args)

    monkeypatch.setattr(ptest.subprocess, "run", run)
    store = ptest.GcsCoordination(BASE, "private-bucket")

    assert store.create_json("coord/key/claim.json", {"owner": "one"}) is True
    args, kwargs = calls[0]
    assert args[: len(BASE)] == BASE
    assert args[-2:] == ["gs://private-bucket/coord/key/claim.json", "--if-generation-match=0"]
    assert kwargs["capture_output"] is True


def test_create_json_reports_a_precondition_collision_without_treating_it_as_outage(
    ptest, monkeypatch
):
    monkeypatch.setattr(
        ptest.subprocess,
        "run",
        lambda args, **kwargs: completed(args, 1, stderr=b"HTTPError 412: conditionNotMet"),
    )
    store = ptest.GcsCoordination(BASE, "private-bucket")

    assert store.create_json("coord/key/claim.json", {"owner": "two"}) is False


def test_read_json_returns_value_and_generation(ptest, monkeypatch):
    responses = iter(
        [
            completed([], stdout=b'{"generation":"42"}'),
            completed([], stdout=b'{"owner":"one"}'),
        ]
    )
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        response = next(responses)
        return completed(args, response.returncode, response.stdout, response.stderr)

    monkeypatch.setattr(ptest.subprocess, "run", run)
    store = ptest.GcsCoordination(BASE, "private-bucket")

    stored = store.read_json("coord/key/claim.json")

    assert stored.value == {"owner": "one"}
    assert stored.generation == 42
    assert calls[1][-1] == "gs://private-bucket/coord/key/claim.json#42"


@pytest.mark.parametrize("message", [b"HTTPError 404: Not Found", b"No URLs matched"])
def test_read_json_returns_none_only_for_known_not_found_errors(
    ptest, monkeypatch, message
):
    monkeypatch.setattr(
        ptest.subprocess,
        "run",
        lambda args, **kwargs: completed(args, 1, stderr=message),
    )
    store = ptest.GcsCoordination(BASE, "private-bucket")

    assert store.read_json("coord/key/missing.json") is None


def test_replace_and_delete_are_generation_safe(ptest, monkeypatch):
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return completed(args)

    monkeypatch.setattr(ptest.subprocess, "run", run)
    store = ptest.GcsCoordination(BASE, "private-bucket")

    assert store.replace_json("coord/key/claim.json", {"owner": "new"}, 42) is True
    assert store.delete_if_generation("coord/key/claim.json", 43) is True

    assert calls[0][-1] == "--if-generation-match=42"
    assert calls[1][-1] == "--if-generation-match=43"


def test_generation_conflict_on_replace_or_delete_returns_false(ptest, monkeypatch):
    monkeypatch.setattr(
        ptest.subprocess,
        "run",
        lambda args, **kwargs: completed(args, 1, stderr=b"412 Precondition Failed"),
    )
    store = ptest.GcsCoordination(BASE, "private-bucket")

    assert store.replace_json("coord/key/claim.json", {}, 1) is False
    assert store.delete_if_generation("coord/key/claim.json", 1) is False


@pytest.mark.parametrize(
    "responses",
    [
        [completed([], 1, stderr=b"503 backend unavailable")],
        [completed([], stdout=b"not-json")],
        [completed([], stdout=b'{"generation":"7"}'), completed([], stdout=b"not-json")],
    ],
)
def test_ambiguous_failures_and_malformed_json_raise_coordination_unavailable(
    ptest, monkeypatch, responses
):
    remaining = iter(responses)
    monkeypatch.setattr(
        ptest.subprocess,
        "run",
        lambda args, **kwargs: next(remaining),
    )
    store = ptest.GcsCoordination(BASE, "private-bucket")

    with pytest.raises(ptest.CoordinationUnavailable):
        store.read_json("coord/key/claim.json")


def test_object_exists_distinguishes_present_missing_and_outage(ptest, monkeypatch):
    responses = iter(
        [
            completed([], stdout=b'{"generation":"1"}'),
            completed([], 1, stderr=b"404 Not Found"),
            completed([], 1, stderr=b"permission denied"),
        ]
    )
    monkeypatch.setattr(ptest.subprocess, "run", lambda args, **kwargs: next(responses))
    store = ptest.GcsCoordination(BASE, "private-bucket")

    assert store.object_exists("sources/one.tar.gz") is True
    assert store.object_exists("sources/two.tar.gz") is False
    with pytest.raises(ptest.CoordinationUnavailable):
        store.object_exists("sources/three.tar.gz")
