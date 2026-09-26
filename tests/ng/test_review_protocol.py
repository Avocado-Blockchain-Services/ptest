"""Private v3 source-ID and bounded-verification contracts."""
from __future__ import annotations

import json

import pytest

from ptest import contracts as C
from ptest import doctor

CHILD_PID = "cd" * 16


def _packet(tmp_path, *, count=24):
    from ptest import agent_assessment as AA

    tests = tmp_path / "tests"
    tests.mkdir(parents=True)
    (tests / "test_factory.py").write_text(
        "def make_user():\n    return {'name': 'new'}\n\n"
        "def test_user():\n    user = make_user()\n"
        "    assert user['name'] == 'new'\n", encoding="utf-8")
    for index in range(count - 1):
        (tests / f"test_aux_{index:02d}.py").write_text(
            f"def test_aux_{index}():\n    assert {index} == {index}\n",
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


def _review(packet, item_id="FIX-001"):
    from ptest import agent_assessment as AA

    return next(review for review in AA.plan_item_reviews(packet)
                if review.item_id == item_id)


def _review_with_one_reserve(packet, monkeypatch):
    """Plan from two exact source spans with one reserved source ID."""
    from ptest import review_evidence as RE

    excerpt = next(item for item in packet.excerpts
                   if item.path == "tests/test_factory.py")
    lines = excerpt.text.splitlines(keepends=True)
    # _packet's factory helper and caller are separate complete functions.
    initial_text = "".join(lines[:2])
    reserve_text = "".join(lines[3:])
    initial = RE.SourceUnit(
        path=excerpt.path, start_line=excerpt.start_line,
        end_line=excerpt.start_line + 1, source_sha256=excerpt.sha256,
        text=initial_text, role="test")
    reserve = RE.SourceUnit(
        path=excerpt.path, start_line=excerpt.start_line + 3,
        end_line=excerpt.end_line, source_sha256=excerpt.sha256,
        text=reserve_text, role="test")
    original = RE.select_item_sources

    def select(packet_arg, item_id):
        if item_id == "FIX-001":
            return (initial,), (reserve,), ()
        return original(packet_arg, item_id)

    monkeypatch.setattr(RE, "select_item_sources", select)
    return _review(packet)


def _ids(review, *, reserve=False):
    selected = set(review.selected_source_ids)
    return [identifier for _unit, identifier in review.source_ids
            if (identifier not in selected) == reserve]


def _reply(review, *, status="satisfied", evidence=None, finding=None,
           needs=(), rationale="The source units show the relevant mechanism."):
    initial_ids = [identifier for identifier in review.selected_source_ids]
    if evidence is None:
        evidence = initial_ids[:1] if status in (
            "satisfied", "gap", "not-applicable") else []
    if status == "gap" and finding is None:
        finding = {"summary": "A concrete violation is present.",
                   "suggested_change": "Preserve the owner at teardown.",
                   "evidence": evidence[:1]}
    return json.dumps({
        "status": status, "rationale": rationale,
        "evidence": list(evidence), "finding": finding,
        "needs": list(needs),
    }).encode()


def test_private_schema_uses_only_source_ids_and_exact_v3_keys(tmp_path):
    packet = _packet(tmp_path)
    review = _review(packet)
    body = json.loads(review.request)
    schema = body["policy"]["response_schema"]
    assert body["packet"]["protocol_version"] == 3
    assert set(schema["required"]) == {
        "status", "rationale", "evidence", "finding", "needs"}
    assert schema["properties"]["evidence"]["items"]["pattern"] == \
        r"^src-[0-9a-f]{24}$"
    assert "proof" not in schema["properties"]
    assert all(set(unit) == {"id", "path", "start_line", "end_line",
                             "sha256", "role", "text"}
               for unit in body["units"])


def test_source_id_reconstructs_exact_public_citation(tmp_path):
    from ptest import agent_assessment as AA

    packet = _packet(tmp_path)
    review = _review(packet)
    subset = {excerpt.path: excerpt for excerpt in packet.excerpts
              if excerpt.path in review.excerpt_paths}
    row, finding, _needs = AA._validate_one_row(
        _reply(review), subset, AA._CATALOG_BY_ID[review.item_id],
        source_map=review.source_map,
        available_ids=set(review.selected_source_ids))
    assert row.status == "satisfied" and finding is None
    citation = row.evidence[0]
    unit = review.source_map_by_id[review.selected_source_ids[0]]
    assert (citation.path, citation.start_line, citation.end_line,
            citation.sha256) == (
        unit.path, unit.start_line, unit.end_line, unit.source_sha256)


def test_foreign_item_and_fabricated_ids_fail_closed(tmp_path):
    from ptest import agent_assessment as AA
    from ptest import review_evidence as RE

    packet = _packet(tmp_path)
    review = _review(packet, "FIX-001")
    unit = review.source_map_by_id[review.selected_source_ids[0]]
    foreign = RE.source_id(packet.packet_sha256, "DB-002", unit)
    subset = {excerpt.path: excerpt for excerpt in packet.excerpts
              if excerpt.path in review.excerpt_paths}
    for identifier in (foreign, "src-" + "f" * 24):
        with pytest.raises(C.Problem, match="unknown or unoffered"):
            AA._validate_one_row(
                _reply(review, evidence=[identifier]), subset,
                AA._CATALOG_BY_ID[review.item_id],
                source_map=review.source_map,
                available_ids=set(review.selected_source_ids))


def test_duplicate_and_nested_evidence_ids_are_invalid(tmp_path):
    from ptest import agent_assessment as AA

    packet = _packet(tmp_path)
    review = _review(packet)
    subset = {excerpt.path: excerpt for excerpt in packet.excerpts
              if excerpt.path in review.excerpt_paths}
    identifier = review.selected_source_ids[0]
    for evidence in ([identifier, identifier], [{}]):
        with pytest.raises(C.Problem, match="duplicate|source ID"):
            AA._validate_one_row(
                _reply(review, evidence=evidence), subset,
                AA._CATALOG_BY_ID[review.item_id],
                source_map=review.source_map,
                available_ids=set(review.selected_source_ids))


def test_reserve_id_cannot_be_cited_before_verification_request(
        tmp_path, monkeypatch):
    from ptest import agent_assessment as AA

    packet = _packet(tmp_path)
    review = _review_with_one_reserve(packet, monkeypatch)
    reserves = _ids(review, reserve=True)
    assert reserves
    subset = {excerpt.path: excerpt for excerpt in packet.excerpts
              if excerpt.path in review.excerpt_paths}
    with pytest.raises(C.Problem, match="unknown or unoffered"):
        AA._validate_one_row(
            _reply(review, evidence=[reserves[0]]), subset,
            AA._CATALOG_BY_ID[review.item_id],
            source_map=review.source_map,
            available_ids=set(review.selected_source_ids))


def test_gap_requires_finding_evidence_and_public_citations_match_ids(tmp_path):
    from ptest import agent_assessment as AA

    packet = _packet(tmp_path)
    review = _review(packet)
    identifier = review.selected_source_ids[0]
    subset = {excerpt.path: excerpt for excerpt in packet.excerpts
              if excerpt.path in review.excerpt_paths}
    with pytest.raises(C.Problem, match="finding"):
        AA._validate_one_row(
            _reply(review, status="gap", evidence=[identifier], finding={
                "summary": "A concrete violation exists.",
                "suggested_change": "Add cleanup.", "evidence": []}),
            subset, AA._CATALOG_BY_ID[review.item_id],
            source_map=review.source_map,
            available_ids=set(review.selected_source_ids))
    row, finding, _needs = AA._validate_one_row(
        _reply(review, status="gap", evidence=[identifier], finding={
            "summary": "A concrete violation exists.",
            "suggested_change": "Add cleanup.", "evidence": [identifier]}),
        subset, AA._CATALOG_BY_ID[review.item_id],
        source_map=review.source_map,
        available_ids=set(review.selected_source_ids))
    assert row.status == "gap" and finding is not None
    assert row.evidence == finding.evidence


def test_v2_proof_fields_and_hostile_model_prose_fail_closed(tmp_path):
    from ptest import agent_assessment as AA

    packet = _packet(tmp_path)
    review = _review(packet)
    subset = {excerpt.path: excerpt for excerpt in packet.excerpts
              if excerpt.path in review.excerpt_paths}
    document = json.loads(_reply(review))
    document["proof"] = [{"role": "mechanism", "citation_index": 0,
                          "quote": "source"}]
    with pytest.raises(C.Problem, match="unknown field"):
        AA._validate_one_row(json.dumps(document).encode(), subset,
                             AA._CATALOG_BY_ID[review.item_id],
                             source_map=review.source_map,
                             available_ids=set(review.selected_source_ids))
    with pytest.raises(C.Problem, match="untrusted"):
        AA._validate_one_row(
            _reply(review, rationale="Run `ptest --full` now; 100% verified."),
            subset, AA._CATALOG_BY_ID[review.item_id],
            source_map=review.source_map,
            available_ids=set(review.selected_source_ids))


def test_all_valid_initial_statuses_get_exactly_one_verifier(tmp_path):
    from ptest import agent_assessment as AA

    packet = _packet(tmp_path)
    review = _review(packet)
    for status in ("satisfied", "gap", "not-applicable", "unknown"):
        planned, failure = AA.plan_followup_review(
            packet, review, _reply(review, status=status))
        assert failure is None and planned is not None
        assert planned.followup_phase
        body = json.loads(planned.request)
        assert body["packet"]["phase"] == "evidence-verification"
        assert body["packet"]["draft"]["status"] == status
        assert AA.plan_followup_review(
            packet, planned, _reply(planned)) == (None, None)


def test_invalid_initial_source_id_gets_one_fresh_bounded_recovery(
        tmp_path, monkeypatch):
    from ptest import agent_assessment as AA

    packet = _packet(tmp_path)
    review = _review_with_one_reserve(packet, monkeypatch)
    offered = set(review.source_map.values())
    invalid_id = next(
        f"src-{number:024x}" for number in range(1, 100)
        if f"src-{number:024x}" not in offered)
    sentinel = "INVALID_INITIAL_DRAFT_MUST_NOT_BE_REPEATED"
    invalid_reply = _reply(
        review, evidence=[invalid_id], rationale=sentinel)

    recovery, failure = AA.plan_followup_review(
        packet, review, invalid_reply)

    assert failure is None and recovery is not None
    assert recovery.followup_phase
    body = json.loads(recovery.request)
    assert body["packet"]["phase"] == "evidence-verification"
    assert body["packet"]["draft"] is None
    recovery_instruction = body["policy"]["instruction"]
    assert recovery_instruction.endswith(AA._RECOVERY_INSTRUCTION)
    assert "audit the draft's decisive claims" not in \
        recovery_instruction.casefold()
    recovery_ids = {unit["id"] for unit in body["units"]}
    assert recovery_ids == set(review.selected_source_ids)
    assert not recovery_ids.intersection(_ids(review, reserve=True))
    assert sentinel not in recovery.request.decode("utf-8")
    assert invalid_id not in recovery.request.decode("utf-8")

    valid_final = _reply(recovery, status="satisfied")
    valid_child = AA.assemble_child(packet, (recovery,), (valid_final,))
    assert valid_child.rows[0].status == "satisfied"
    assert valid_child.rows[0].evidence
    assert all(citation.path in review.excerpt_paths
               for citation in valid_child.rows[0].evidence)

    # A malformed fresh judgment remains unknown; recovery is not retried.
    invalid_final = _reply(
        recovery, evidence=[_ids(review, reserve=True)[0]])
    child = AA.assemble_child(packet, (recovery,), (invalid_final,))
    assert child.rows[0].status == "unknown"
    assert child.rows[0].rationale.startswith(AA.FAILED_PREFIX)
    assert AA.plan_followup_review(
        packet, recovery, _reply(recovery)) == (None, None)

    # Provider/tool/deadline failures are not completed malformed replies.
    assert AA.plan_followup_review(
        packet, review, "provider timeout") == (None, None)
    assert AA.plan_followup_review(
        packet, review, "tool denied") == (None, None)
    assert AA.plan_followup_review(
        packet, review, b"x" * (AA.MAX_PAYLOAD_BYTES + 1)) == (None, None)


def test_verifier_runs_with_empty_needs_and_reserve_inventory():
    from ptest import agent_assessment as AA
    from ptest import review_context as RC

    text = ("def make_user():\n    return {'name': 'new'}\n\n"
            "def test_user():\n    assert make_user()['name'] == 'new'\n")
    excerpt = AA.SourceExcerpt(
        path="tests/test_factory.py", start_line=1, end_line=5,
        sha256=__import__("hashlib").sha256(text.encode()).hexdigest(),
        text=text)
    context = RC.ReviewContext(
        runner_kind="pytest", roles=((excerpt.path, "test"),),
        config_status="resolved")
    packet = AA.EvidencePacket(
        declaration=".", project_id=CHILD_PID, scope=".",
        packet_sha256="11" * 32, excerpts=(excerpt,), dependencies=(),
        runner_kind="pytest", excluded_count=0, truncated_count=0,
        file_count=1, byte_count=len(text.encode()), context=context)
    review = next(item for item in AA.plan_item_reviews(packet)
                  if item.item_id == "FIX-001")
    assert review.reserve_source_ids == ()
    initial = json.loads(review.request)
    candidate_marker = "Audit the draft's decisive claims, not just its status."
    assert candidate_marker.casefold() not in \
        initial["policy"]["instruction"].casefold()
    initial_reply = _reply(
        review, status="unknown", needs=(),
        rationale="A decisive caller detail remains open.")
    planned, failure = AA.plan_followup_review(
        packet, review, initial_reply)
    assert failure is None and planned is not None
    body = json.loads(planned.request)
    assert body["packet"]["phase"] == "evidence-verification"
    assert body["packet"]["packet_sha256"] == initial["packet"]["packet_sha256"]
    assert body["packet"]["draft"] == {
        "status": "unknown",
        "rationale": "A decisive caller detail remains open.",
        "evidence": [], "finding": None, "needs": [],
    }
    assert body["policy"]["item"] == initial["policy"]["item"]
    assert body["policy"]["response_schema"] == \
        initial["policy"]["response_schema"]
    assert [unit["id"] for unit in body["units"]] == [
        unit["id"] for unit in initial["units"]]
    instruction = body["policy"]["instruction"].casefold()
    assert instruction.endswith(AA._VERIFICATION_INSTRUCTION.casefold())
    assert candidate_marker.casefold() in instruction
    assert ("a complete source file or span does not establish a complete "
            "reachable implementation" in instruction)
    assert "time used to coordinate actors for another assertion" in instruction
    assert ("the finding summary must identify the same concrete operation"
            in instruction)
    assert "a verification reply must have empty needs" in instruction


def test_verifier_can_change_false_ok_to_caller_supported_gap(tmp_path):
    from ptest import agent_assessment as AA

    packet = _packet(tmp_path)
    review = _review(packet)
    planned, failure = AA.plan_followup_review(
        packet, review, _reply(review, status="satisfied"))
    assert failure is None and planned is not None
    identifier = planned.selected_source_ids[0]
    reply = _reply(planned, status="gap", evidence=[identifier], finding={
        "summary": "The caller shows the violation.",
        "suggested_change": "Preserve cleanup ownership.",
        "evidence": [identifier]})
    child = AA.assemble_child(packet, (planned,), (reply,))
    assert child.rows[0].status == "gap"
    assert len(child.findings) == 1


def test_final_needs_and_verification_provider_failure_never_pass(
        tmp_path, monkeypatch):
    from ptest import agent_assessment as AA

    packet = _packet(tmp_path)
    review = _review_with_one_reserve(packet, monkeypatch)
    reserve_id = _ids(review, reserve=True)[0]
    planned, failure = AA.plan_followup_review(
        packet, review, _reply(review, status="unknown", needs=[reserve_id]))
    assert failure is None and planned is not None
    final = json.loads(_reply(planned, status="unknown"))
    final["needs"] = [reserve_id]
    child = AA.assemble_child(
        packet, (planned,), (json.dumps(final).encode(),))
    assert child.rows[0].status == "unknown"
    assert child.rows[0].rationale.startswith(AA.FAILED_PREFIX)
    failed = AA.assemble_child(packet, (planned,), ("provider timeout",))
    assert failed.rows[0].status == "unknown"
    assert "provider timeout" in failed.rows[0].rationale


def test_nine_item_prompts_name_the_required_counterevidence():
    from ptest.checklist import CATALOG

    prompts = {entry.id: entry.prompt.lower() for entry in CATALOG}
    assert "caller" in prompts["FIX-001"]
    assert "immutable" in prompts["FIX-002"]
    assert "per test" in prompts["DB-001"]
    assert "worker" in prompts["DB-002"]
    assert "client construction" in prompts["CACHE-001"]
    assert "outbound client destination port is not a listening-port allocation" in prompts["RESOURCE-001"]
    assert "hypothetical additional writable files" in prompts["RESOURCE-001"]
    assert "configured shared setup" in prompts["NETWORK-001"]
    assert "wait" in prompts["PROCESS-001"]
    assert "fake timer" in prompts["TIME-001"]
