"""Private proof and one-packet follow-up contracts for doctor items."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ptest import contracts as C
from ptest import doctor

CHILD_PID = "cd" * 16


def _packet(tmp_path: Path):
    from ptest import agent_assessment as AA

    (tmp_path / "tests").mkdir(parents=True)
    (tmp_path / "tests" / "test_factory.py").write_text(
        "def make_user():\n    return {'name': 'new'}\n\n"
        "def test_user():\n    user = make_user()\n    assert user['name'] == 'new'\n",
        encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "review-notes.txt").write_text(
        "synthetic packet source available only for bounded follow-up\n",
        encoding="utf-8")
    config = C.Config(
        runner=C.RunnerConfig(kind=C.RunnerKind.PYTEST,
                              launcher=("uv",), test_roots=("tests",)),
        setup=None, resources=C.ResourceConfig(),
        selection=C.SelectionPolicy(enabled=False, closed_inputs=False),
        project_id=CHILD_PID)
    resolution = C.ConfigResolution(
        root=tmp_path, path=None, config=config, monorepo=None,
        provenance=(), warnings=(), problem=None)
    domain = C.DomainPaths(
        root=tmp_path, machine_config=tmp_path / ".ptest" / "config.toml",
        ledger=tmp_path / ".ptest" / "ledger",
        marker=tmp_path / ".ptest" / "marker", fixture=True,
        domain_id=None)
    workspace = doctor.inspect_workspace(
        domain, resolution, C.DEFAULT_SCAN_LIMITS, None)
    return AA.build_packets(workspace, resolution)[0]


def _review_with_excerpt(packet):
    from ptest import agent_assessment as AA

    for review in AA.plan_item_reviews(packet):
        if review.request is not None and json.loads(
                review.request.decode("utf-8"))["excerpts"]:
            return review
    raise AssertionError("fixture packet has no routed source excerpt")


def _response(review, *, status="satisfied", quote=None, needs=None,
              finding=None, proof_roles=None):
    request = json.loads(review.request.decode("utf-8"))
    excerpts = request["excerpts"]
    if not excerpts:
        status = "unknown"
        needs = ()
        proof_roles = ()
        finding = None
        evidence = []
        source = None
    else:
        source = excerpts[0]
        evidence = None
    citation = {key: source[key] for key in
                ("path", "start_line", "end_line", "sha256")} \
        if source is not None else None
    if quote is None and source is not None:
        quote = source["text"].splitlines()[0]
    roles = (proof_roles if proof_roles is not None else
             (("applicability", "mechanism") if status == "satisfied"
              else ("applicability", "violation") if status == "gap"
              else ("applicability",) if status == "not-applicable" else ()))
    proof = ([{"role": role, "citation_index": 0, "quote": quote}
              for role in roles] if source is not None else [])
    if finding is None and status == "gap":
        finding = {"summary": "A concrete violation is present.",
                   "suggested_change": "Preserve the owner at teardown.",
                   "evidence": [citation]}
    return json.dumps({
        "status": status,
        "rationale": "The cited lines show the relevant review evidence.",
        "evidence": ([citation] if status != "unknown" else [])
        if evidence is None else evidence,
        "finding": finding if status == "gap" else None,
        "proof": proof,
        "needs": list(needs or ()),
    }).encode("utf-8")


def test_private_schema_requires_proof_and_needs(tmp_path):
    from ptest import agent_assessment as AA

    review = AA.plan_item_reviews(_packet(tmp_path))[0]
    payload = json.loads(review.request.decode("utf-8"))
    schema = payload["policy"]["response_schema"]
    assert {"status", "rationale", "evidence", "finding", "proof",
            "needs"} == set(schema["required"])
    assert schema["properties"]["proof"]["maxItems"] == 4
    assert schema["properties"]["needs"]["maxItems"] == 4


def test_exact_private_proof_supports_a_satisfied_row(tmp_path):
    from ptest import agent_assessment as AA

    packet = _packet(tmp_path)
    review = _review_with_excerpt(packet)
    subset = {excerpt.path: excerpt for excerpt in packet.excerpts
              if excerpt.path in review.excerpt_paths}
    row, finding = AA._validate_one_row(
        _response(review), subset, AA._CATALOG_BY_ID[review.item_id])
    assert row.status == "satisfied"
    assert finding is None
    assert not hasattr(row, "proof")


def test_quote_outside_cited_lines_cannot_support_a_verdict(tmp_path):
    from ptest import agent_assessment as AA

    packet = _packet(tmp_path)
    review = _review_with_excerpt(packet)
    subset = {excerpt.path: excerpt for excerpt in packet.excerpts
              if excerpt.path in review.excerpt_paths}
    with pytest.raises(C.Problem, match="quote"):
        AA._validate_one_row(
            _response(review, quote="fabricated supporting text"), subset,
            AA._CATALOG_BY_ID[review.item_id])


def test_violation_proof_must_also_appear_in_finding_evidence(tmp_path):
    from ptest import agent_assessment as AA

    packet = _packet(tmp_path)
    review = _review_with_excerpt(packet)
    subset = {excerpt.path: excerpt for excerpt in packet.excerpts
              if excerpt.path in review.excerpt_paths}
    response = _response(review, status="gap", finding={
        "summary": "A concrete violation is present.",
        "suggested_change": "Preserve the owner at teardown.",
        "evidence": [],
    })
    with pytest.raises(C.Problem, match="finding.*citations|finding evidence"):
        AA._validate_one_row(
            response, subset, AA._CATALOG_BY_ID[review.item_id])


def test_required_proof_cannot_bind_to_a_dropped_row_citation(tmp_path):
    from ptest import agent_assessment as AA

    packet = _packet(tmp_path)
    review = _review_with_excerpt(packet)
    subset = {excerpt.path: excerpt for excerpt in packet.excerpts
              if excerpt.path in review.excerpt_paths}
    source = json.loads(review.request.decode("utf-8"))["excerpts"][0]
    first = {key: source[key] for key in
             ("path", "start_line", "end_line", "sha256")}
    dropped = {**first, "unrecognized": "must be dropped"}
    second = dict(first)
    second["start_line"] = min(source["end_line"], source["start_line"] + 1)
    second["end_line"] = second["start_line"]
    document = json.loads(_response(review))
    document["evidence"] = [dropped, second]
    document["proof"] = [
        {"role": "applicability", "citation_index": 0,
         "quote": source["text"].splitlines()[0]},
        {"role": "mechanism", "citation_index": 0,
         "quote": source["text"].splitlines()[0]},
    ]
    with pytest.raises(C.Problem, match="proof|citation"):
        AA._validate_one_row(json.dumps(document).encode(), subset,
                             AA._CATALOG_BY_ID[review.item_id])


def test_violation_proof_must_survive_finding_citation_validation(tmp_path):
    from ptest import agent_assessment as AA

    packet = _packet(tmp_path)
    review = _review_with_excerpt(packet)
    subset = {excerpt.path: excerpt for excerpt in packet.excerpts
              if excerpt.path in review.excerpt_paths}
    source = json.loads(review.request.decode("utf-8"))["excerpts"][0]
    first = {key: source[key] for key in
             ("path", "start_line", "end_line", "sha256")}
    second = dict(first)
    second["start_line"] = min(source["end_line"], source["start_line"] + 1)
    second["end_line"] = second["start_line"]
    document = json.loads(_response(review, status="gap"))
    document["evidence"] = [first, second]
    document["proof"] = [
        {"role": "applicability", "citation_index": 0,
         "quote": source["text"].splitlines()[0]},
        {"role": "violation", "citation_index": 0,
         "quote": source["text"].splitlines()[0]},
    ]
    document["finding"]["evidence"] = [
        {**first, "unrecognized": "this finding citation is dropped"},
        second,
    ]
    with pytest.raises(C.Problem, match="finding evidence|proof"):
        AA._validate_one_row(json.dumps(document).encode(), subset,
                             AA._CATALOG_BY_ID[review.item_id])


def test_foreign_followup_id_is_rejected_and_row_becomes_unknown(tmp_path):
    from ptest import agent_assessment as AA

    packet = _packet(tmp_path)
    reviews = AA.plan_item_reviews(packet)
    review = next(item for item in reviews if item.request is not None
                  and json.loads(item.request.decode("utf-8"))["excerpts"])
    invalid_initial = _response(
        review, status="unknown", needs=("ctx-" + "f" * 24,))
    assert AA.plan_followup_review(
        packet, review, invalid_initial) == (None, None)
    child = AA.assemble_child(
        packet, reviews,
        tuple(invalid_initial
              if item.item_id == review.item_id else _response(item)
              for item in reviews))
    row = next(row for row in child.rows if row.id == review.item_id)
    assert row.status == "unknown"
    assert row.rationale.startswith(AA.FAILED_PREFIX)


def test_initial_request_reserves_one_bounded_followup_inventory(tmp_path):
    from ptest import agent_assessment as AA

    packet = _packet(tmp_path)
    review = AA.plan_item_reviews(packet)[0]
    payload = json.loads(review.request.decode("utf-8"))
    offered = payload["packet"]["omission_inventory"]
    assert len(payload["excerpts"]) <= 20
    assert sum(len(entry["text"].encode("utf-8"))
               for entry in payload["excerpts"]) <= 192 * 1024
    assert len(offered) <= len(packet.excerpts)
    assert all(set(entry) == {"id", "path", "role", "bytes"}
               for entry in offered)


def test_one_followup_adds_only_offered_packet_evidence_and_cannot_recurse(
        tmp_path, monkeypatch):
    from ptest import agent_assessment as AA

    packet = _packet(tmp_path)
    review = _review_with_excerpt(packet)
    assert review.followup_inventory
    offered = review.followup_inventory[0]
    monkeypatch.setattr(
        AA, "read_regular",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("follow-up must not read the filesystem")))
    first_reply = _response(
        review, status="unknown", needs=(offered.opaque_id,))
    followup, failure = AA.plan_followup_review(packet, review, first_reply)
    assert failure is None
    assert followup is not None
    body = json.loads(followup.request.decode("utf-8"))
    assert body["packet"]["phase"] == "evidence-followup"
    assert body["packet"]["packet_sha256"] == packet.packet_sha256
    assert offered.excerpt.path in followup.excerpt_paths
    assert followup.followup_phase
    assert followup.followup_inventory == ()
    final = _response(followup, status="satisfied")
    assert AA.plan_followup_review(packet, followup, final) == (None, None)


def test_followup_sends_stable_subset_that_fits_bounds(tmp_path):
    from ptest.agent_assessment import SourceExcerpt
    from ptest.review_protocol import OmittedExcerpt, build_followup_request

    first = OmittedExcerpt(
        "ctx-" + "1" * 24,
        SourceExcerpt("src/a.py", 1, 1, "a" * 64, "a" * 40_000),
        "source")
    second = OmittedExcerpt(
        "ctx-" + "2" * 24,
        SourceExcerpt("src/b.py", 1, 1, "b" * 64, "b" * 40_000),
        "source")
    payload = {
        "policy": {},
        "packet": {"phase": "initial",
                   "omission_inventory": [first.public_inventory(),
                                          second.public_inventory()],
                   "omitted": ["src/a.py", "src/b.py"]},
        "excerpts": [],
    }
    request, paths = build_followup_request(
        json.dumps(payload).encode("utf-8"), (first, second),
        (first.opaque_id, second.opaque_id), max_request_bytes=50_000)

    assert paths == ("src/a.py",)
    body = json.loads(request.decode("utf-8"))
    assert [excerpt["path"] for excerpt in body["excerpts"]] == ["src/a.py"]
    assert body["packet"]["omitted"] == ["src/b.py"]
    assert [entry["id"] for entry in body["packet"]["omission_inventory"]] == [
        second.opaque_id]

    fitting = tuple(OmittedExcerpt(
        f"ctx-{index:024x}",
        SourceExcerpt(f"src/fit-{index}.py", 1, 1,
                      f"{index + 3:064x}", "x" * 1024),
        "source") for index in range(4))
    four_payload = {
        "policy": {},
        "packet": {"phase": "initial",
                   "omission_inventory": [entry.public_inventory()
                                          for entry in fitting],
                   "omitted": [entry.excerpt.path for entry in fitting]},
        "excerpts": [],
    }
    four_request, four_paths = build_followup_request(
        json.dumps(four_payload).encode("utf-8"), fitting,
        tuple(entry.opaque_id for entry in fitting), max_request_bytes=16_000)
    four_body = json.loads(four_request.decode("utf-8"))
    assert four_paths == tuple(entry.excerpt.path for entry in fitting)
    assert len(four_body["excerpts"]) == 4


def test_followup_with_nonempty_final_needs_becomes_unknown(tmp_path):
    from ptest import agent_assessment as AA

    packet = _packet(tmp_path)
    review = _review_with_excerpt(packet)
    followup, failure = AA.plan_followup_review(
        packet, review,
        _response(review, status="unknown",
                  needs=(review.followup_inventory[0].opaque_id,)))
    assert failure is None and followup is not None
    stray_id = "ctx-" + "e" * 24
    child = AA.assemble_child(
        packet, (followup,),
        (_response(followup, status="unknown", needs=(stray_id,)),))
    assert child.rows[0].status == "unknown"
    assert child.rows[0].rationale.startswith(AA.FAILED_PREFIX)


def test_nine_item_prompts_name_the_required_counterevidence():
    from ptest.checklist import CATALOG

    prompts = {entry.id: entry.prompt.lower() for entry in CATALOG}
    assert "caller" in prompts["FIX-001"]
    assert "immutable" in prompts["FIX-002"]
    assert "per test" in prompts["DB-001"]
    assert "worker" in prompts["DB-002"]
    assert "client construction" in prompts["CACHE-001"]
    assert "port 0" in prompts["RESOURCE-001"]
    assert "configured shared setup" in prompts["NETWORK-001"]
    assert "wait" in prompts["PROCESS-001"]
    assert "fake timer" in prompts["TIME-001"]
