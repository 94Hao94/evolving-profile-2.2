from lib.process_memory import ProcessMemoryStore


def episode(store, text, **extra):
    return store._append({"process_memory_id": text, "kind": "episode", "maturity": "verified", "text": text, "source_trace_ids": ["pm_missing_source"], **extra})


def test_matching_family_and_metadata_do_not_make_irrelevant_content_relevant(tmp_path):
    store = ProcessMemoryStore(tmp_path / "store.json")
    episode(store, "Potato soup recipe", task_archetype=["software_engineering"], metadata={"query": "pagination filtering"})
    assert store.search("pagination filtering", task_archetype="software_engineering") == []


def test_search_preserves_relation_and_reports_dangling_sources_without_writing(tmp_path):
    store = ProcessMemoryStore(tmp_path / "store.json")
    episode(store, "pagination filtering before offset", updated_at="2000-01-01")
    before = store.path.read_bytes()
    result = store.search_page("pagination filtering before offset", minimum_relevance="strong")
    row = result["records"][0]
    assert row["relevance_level"] == "strong"
    assert row["relevance_score"] > 0
    assert row["relevance_reasons"]
    assert row["source_integrity"]["missing_source_trace_ids"] == ["pm_missing_source"]
    assert row["source_integrity"]["status"] == "dangling"
    assert store.path.read_bytes() == before


def test_relation_filtering_precedes_page_counts_and_cross_domain_is_allowed(tmp_path):
    store = ProcessMemoryStore(tmp_path / "store.json")
    episode(store, "potato soup", task_archetype=["software_engineering"])
    episode(store, "filter rows before pagination offset", task_archetype=["document_office"])
    result = store.search_page("filter rows before pagination offset", task_archetype="software_engineering", minimum_relevance="medium", limit=1)
    assert result["total_count"] == 1
    assert result["records"][0]["text"] == "filter rows before pagination offset"


def test_recovered_capability_is_final_success_and_not_first_pass(tmp_path):
    store = ProcessMemoryStore(tmp_path / "store.json")
    result = store.record_capability_observation({"model_family": "model-a", "model_version": "v1", "task_archetype": "coding", "phase": "recover", "outcome": "recovered", "verifier_kind": "automated_test", "status": "passed", "evidence_refs": ["receipt-42"], "verification_evidence": [{"verifier_kind": "automated_test", "status": "passed", "id": "receipt-42"}]})
    assert result["outcome"] == "recovered"
    assert result["profile"]["success_count"] == 1
    assert result["profile"]["failure_count"] == 0
    assert result["profile"]["first_pass_success_count"] == 0
    assert result["profile"]["recovery_success_count"] == 1
    assert result["evidence_refs"] == ["receipt-42"]
    assert result["verification_evidence"][0]["id"] == "receipt-42"
    assert result["model_profile"]["version"] == "v1"


def test_unknown_ambiguous_and_blocked_are_not_automatic_capability_failure(tmp_path):
    store = ProcessMemoryStore(tmp_path / "store.json")
    for outcome in ["unknown", "ambiguous", "blocked"]:
        store.record_capability_observation({"model_family": "a", "task_archetype": "coding", "phase": "verify", "outcome": outcome, "verifier_kind": "automated_test", "status": "passed"})
    profile = store.capability_profile("a", "coding", "verify")
    assert profile["sample_count"] == 3
    assert profile["failure_count"] == 0
    assert profile["unknown_count"] == 3
    assert profile["confidence_interval"] == [0.0, 1.0]


def test_read_source_integrity_audit_is_non_mutating_and_separate_from_maturity(tmp_path):
    store = ProcessMemoryStore(tmp_path / "store.json")
    record = episode(store, "verified relevance content")
    before = store.path.read_bytes()
    audited = store.audit_source_integrity(record)
    assert audited["maturity"] == "verified"
    assert audited["source_integrity"]["status"] == "dangling"
    assert "source_integrity" not in record
    assert store.path.read_bytes() == before


def test_search_page_reports_excluded_relations_before_pagination(tmp_path):
    store = ProcessMemoryStore(tmp_path / "store.json")
    episode(store, "presentation overflow rendering")
    episode(store, "presentation guidance")
    episode(store, "Potato soup recipe")
    page = store.search_page("presentation overflow rendering", minimum_relevance="strong", limit=1)
    assert page["relevance_audit"]["level_counts"]["strong"] == 1
    assert page["relevance_audit"]["level_counts"]["weak"] == 1
    assert page["relevance_audit"]["level_counts"]["none"] == 1
    assert page["relevance_audit"]["excluded_count"] == 2
    assert page["relevance_audit"]["kept_count"] == 1


def test_resolved_policy_keeps_rejected_looser_request_reason(tmp_path):
    from lib.recall_relevance import resolve_min_relevance
    store = ProcessMemoryStore(tmp_path / "store.json")
    episode(store, "presentation guidance")
    policy = resolve_min_relevance({"recall_policy": {"default_min_relevance": "strong"}}, "agent_memory", requested="weak")
    page = store.search_page("presentation overflow rendering", policy=policy)
    assert page["records"] == []
    assert "request_would_loosen_policy" in page["relevance_audit"]["reasons"]
    assert page["relevance_audit"]["requested_level"] == "weak"


def test_search_reuses_source_index_and_reads_store_once_for_all_candidates(tmp_path):
    class CountingStore(ProcessMemoryStore):
        reads = 0
        indexes = 0

        def _read(self):
            self.reads += 1
            return super()._read()

        def _source_id_index(self, records):
            self.indexes += 1
            return super()._source_id_index(records)

    store = CountingStore(tmp_path / "store.json")
    store._write({"records": [{"process_memory_id": f"row-{i}", "kind": "episode", "maturity": "verified", "text": "filter rows before pagination offset", "source_trace_ids": ["row-0"]} for i in range(80)]})
    result = store.search_page("filter rows before pagination offset")
    assert result["total_count"] == 80
    assert store.reads == 1
    assert store.indexes == 1
    assert all(row["source_integrity"]["resolved_source_trace_ids"] == ["row-0"] for row in result["records"])


def test_malformed_source_references_never_resolve_via_string_coercion(tmp_path):
    store = ProcessMemoryStore(tmp_path / "store.json")
    references = ["pm_missing", None, 42, {"id": "source"}, ""]
    result = store.audit_source_integrity({"source_trace_ids": references, "maturity": "verified"}, known_ids={"42", "None", "source"})
    audit = result["source_integrity"]
    assert audit["resolved_source_trace_ids"] == []
    assert audit["missing_source_trace_ids"] == ["pm_missing"]
    assert audit["invalid_source_trace_ids"] == [None, 42, {"id": "source"}, ""]
    assert result["source_trace_ids"] == references


def test_tightening_store_request_keeps_actual_plane_override_provenance(tmp_path):
    from lib.recall_relevance import resolve_min_relevance
    store = ProcessMemoryStore(tmp_path / "store.json")
    policy = resolve_min_relevance({"recall_policy": {"default_min_relevance": "weak", "agent_memory": "medium"}}, "agent_memory")
    audit = store.search_page("pagination", policy=policy, minimum_relevance="strong")["relevance_audit"]
    assert audit["configuration_source"] == "plane_override"
    assert audit["global_level"] == "weak"
    assert audit["plane_setting"] == "medium"
    assert audit["configured_level"] == "medium"
    assert audit["effective_level"] == "strong"


def test_missing_record_ids_and_nonlist_references_do_not_fabricate_links(tmp_path):
    store = ProcessMemoryStore(tmp_path / "store.json")
    store._write({"records": [{"process_memory_id": None}, {}]})
    missing = store.audit_source_integrity({"source_trace_ids": ["None"]})["source_integrity"]
    assert missing["status"] == "dangling"
    assert missing["resolved_source_trace_ids"] == []
    malformed = store.audit_source_integrity({"source_trace_ids": "source-id"})["source_integrity"]
    assert malformed["status"] == "invalid"
    assert malformed["invalid_source_trace_ids"] == ["source-id"]
    assert malformed["missing_source_trace_ids"] == []
