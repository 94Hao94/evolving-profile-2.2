"""Policy, binding and pagination regressions at the actual tool boundary."""
import importlib
import json
import io
import datetime
import contextlib
from pathlib import Path

import pytest


def adapter():
    return importlib.import_module("evolving_profile_controller_mcp")


def test_tool_recall_returns_agent_owned_expansion_hint_without_extra_calls(tmp_path, monkeypatch):
    mcp = adapter()
    calls = []
    def search(*args, **kwargs):
        calls.append(kwargs)
        return {"memories": [], "relevance_audit": {"decisions": []}}
    monkeypatch.setattr(mcp, "search", search)
    monkeypatch.setattr(mcp, "runtime_disabled", lambda *args: None)
    monkeypatch.setattr(mcp, "load_runtime_settings", lambda: {"recall_policy": {"default_min_relevance": "medium"}})
    monkeypatch.setattr(mcp, "guidance_value", lambda *args: {})
    monkeypatch.setattr(mcp, "scenario_followup", lambda *args: {})
    result = json.loads(mcp.evidence_recall({"query": "exact QKM_552", "minimum_relevance": "strong"})["content"][0]["text"])
    assert result["adaptive_hint"]["allowed_tool_arguments"] == [{"query": "exact QKM_552", "minimum_relevance": "medium"}]
    assert result["adaptive_hint"]["retrieval_performed"] is False
    assert len(calls) == 1


def test_agent_scope_gate_keeps_verified_context_and_generic_method(tmp_path, monkeypatch):
    mcp = adapter()
    from lib.process_memory import ProcessMemoryStore
    path = tmp_path / "process.json"
    path.write_text(json.dumps({"records": [
        {"process_memory_id": "pm_trace_unknown", "kind": "trace", "maturity": "verified", "status": "active", "text": "Orion pagination filtering offset"},
        {"process_memory_id": "pm_trace_known", "kind": "trace", "maturity": "verified", "status": "active", "text": "Orion pagination filtering offset", "primary_context": {"project": "Orion"}, "scope_verification": {"status": "verified", "source": "explicit_project_source_receipt"}},
        {"process_memory_id": "pm_trace_method", "kind": "trace", "maturity": "verified", "status": "active", "text": "For reporting pagination, filter rows before calculating offset."},
    ]}))
    monkeypatch.setattr(mcp, "PROCESS_MEMORY_WORKSPACE_ROOT", tmp_path / "workspaces")
    monkeypatch.setattr(mcp, "load_runtime_settings", lambda: {"recall_policy": {"advanced": {"scope_unknown_mode": "require_verified"}}})
    page, _ = mcp._snapshot_page(ProcessMemoryStore(path), "scoped", "sig", query="Orion pagination filtering offset", facets=[], compatibility={}, task_archetype=None, primary_context={"project": "Orion"}, include_unverified=True, offset=0, limit=8)
    assert {row["process_memory_id"] for row in page["records"]} == {"pm_trace_known", "pm_trace_method"}


def test_matching_project_bucket_cannot_verify_formal_project_identity(tmp_path, monkeypatch):
    mcp = adapter()
    from lib.process_memory import ProcessMemoryStore
    path = tmp_path / "process.json"
    path.write_text(json.dumps({"records": [{"process_memory_id": "pm_trace_bucket", "kind": "trace", "maturity": "verified", "status": "active", "text": "Orion pagination filtering offset", "primary_context": {"project": "Orion"}}]}))
    monkeypatch.setattr(mcp, "PROCESS_MEMORY_WORKSPACE_ROOT", tmp_path / "workspaces")
    monkeypatch.setattr(mcp, "load_runtime_settings", lambda: {"recall_policy": {"advanced": {"scope_unknown_mode": "require_verified"}}})
    page, _ = mcp._snapshot_page(ProcessMemoryStore(path), "bucket", "sig", query="Orion pagination filtering offset", facets=[], compatibility={}, task_archetype=None, primary_context={"project": "Orion"}, include_unverified=True, offset=0, limit=8)
    assert page["records"] == []
    assert page["relevance_audit"]["decisions"][0]["level"] == "strong"


def test_prepare_context_obeys_advanced_scope_gate(tmp_path, monkeypatch):
    mcp = adapter()
    from lib.process_memory import ProcessMemoryStore
    path = tmp_path / "process.json"
    path.write_text(json.dumps({"records": [{"process_memory_id": "pm_trace_unknown", "kind": "trace", "maturity": "verified", "status": "active", "text": "Orion pagination filtering offset"}]}))
    monkeypatch.setattr(mcp, "_process_store", lambda: ProcessMemoryStore(path))
    monkeypatch.setattr(mcp, "process_runtime_disabled", lambda *args: None)
    monkeypatch.setattr(mcp, "load_runtime_settings", lambda: {"recall_policy": {"advanced": {"scope_unknown_mode": "require_verified"}}})
    result = json.loads(mcp.prepare_agent_process_context({"query": "Orion pagination filtering offset", "primary_context": {"project": "Orion"}})["content"][0]["text"])
    assert result["hints"] == []
    assert result["adaptive_hint"]["retrieval_performed"] is False


def test_read_preference_unit_counts_the_returned_body_not_zero():
    mcp = adapter()
    assert mcp._returned_count({"status": "found", "unit": {"id": "u1", "text": "Verify visible UI"}}, "read_preference_unit") == 1
    assert mcp._returned_count({"status": "not_found", "unit": None}, "read_preference_unit") == 0


def test_supplemental_read_tools_accept_explicit_prompt_binding():
    mcp = adapter()
    assert "check_id" in mcp.GUIDANCE_UNIT_TOOL["inputSchema"]["properties"]
    assert mcp.RUNTIME_GUIDANCE_TOOL is not None
    assert "check_id" in mcp.RUNTIME_GUIDANCE_TOOL["inputSchema"]["properties"]


def test_agent_facet_fusion_preserves_main_subject_and_relevance_order(tmp_path, monkeypatch):
    mcp = adapter()
    from lib.process_memory import ProcessMemoryStore
    path = tmp_path / "process.json"
    path.write_text(json.dumps({"records": [
        {"process_memory_id": "pm_trace_orion", "kind": "trace", "maturity": "verified", "status": "active", "text": "Orion retrieval counts duplicate child counts. Fixed counter aggregation.", "updated_at": "2026-09-02"},
        {"process_memory_id": "pm_trace_garden", "kind": "trace", "maturity": "verified", "status": "active", "text": "Garden irrigation counts changed the watering schedule.", "updated_at": "2026-09-01"},
    ]}))
    monkeypatch.setattr(mcp, "PROCESS_MEMORY_WORKSPACE_ROOT", tmp_path / "workspaces")
    monkeypatch.setattr(mcp, "load_runtime_settings", lambda: {"recall_policy": {"default_min_relevance": "weak"}})
    store = ProcessMemoryStore(path)
    page, _ = mcp._snapshot_page(store, "facet-test", "sig", query="Orion retrieval counts", facets=["counts"], compatibility={}, task_archetype=None, primary_context=None, include_unverified=True, offset=0, limit=8)
    assert page["records"][0]["process_memory_id"] == "pm_trace_orion"
    assert page["relevance_audit"]["effective_level"] == "weak"
    assert page["records"][0]["relevance_match_signals"]["main_query_used"]


def test_policy_change_invalidates_an_agent_candidate_workspace(tmp_path, monkeypatch):
    mcp = adapter()
    from lib.process_memory import ProcessMemoryStore
    path = tmp_path / "process.json"
    path.write_text(json.dumps({"records": [
        {"process_memory_id": "pm_trace_weak", "kind": "trace", "maturity": "verified", "status": "active", "text": "Presentation rendering caught clipping.", "updated_at": "2026-09-01"},
    ]}))
    monkeypatch.setattr(mcp, "PROCESS_MEMORY_WORKSPACE_ROOT", tmp_path / "workspaces")
    settings = {"recall_policy": {"default_min_relevance": "weak"}}
    monkeypatch.setattr(mcp, "load_runtime_settings", lambda: settings)
    store = ProcessMemoryStore(path)
    kwargs = dict(query="presentation overflow rendering delivery", facets=[], compatibility={}, task_archetype=None, primary_context=None, include_unverified=True, offset=0, limit=8)
    first, _ = mcp._snapshot_page(store, "policy-test", "same-query", **kwargs)
    assert first["total_count"] == 1
    settings["recall_policy"]["default_min_relevance"] = "strong"
    second, _ = mcp._snapshot_page(store, "policy-test", "same-query", **kwargs)
    assert second["total_count"] == 0
    assert second["relevance_audit"]["effective_level"] == "strong"


def test_user_policy_filters_before_pagination_and_keeps_full_content_anchor(tmp_path):
    from evidence_workspace import search
    from lib.recall_relevance import resolve_min_relevance
    import uuid
    ids = [str(uuid.uuid4()) for _ in range(3)]
    rows = [{"id": ids[0], "text": "Install Git and list command line tools", "state": "valid"},
            {"id": ids[1], "text": "Translate cooking recipes", "state": "valid"},
            {"id": ids[2], "text": "padding " * 110 + "The exact validation code is QKM_552.", "state": "valid"}]
    def api(path, body=None, timeout=None):
        if path.endswith("/recall"):
            return {"results": rows}
        return next(row for row in rows if row["id"] == path.rsplit("/", 1)[-1])
    policy = resolve_min_relevance({"recall_policy": {"default_min_relevance": "weak"}}, "user_memory")
    value = search("bank", "Find the exact validation code QKM_552", api, tmp_path, page_size=1, relevance_policy=policy)
    assert [row["id"] for row in value["memories"]] == [ids[2]]
    assert value["discovered_reference_count"] == 1
    assert value["raw_discovered_reference_count"] == 3
    assert value["next_offset"] is None
    assert value["relevance_audit"]["excluded_count"] == 2


def test_changed_policy_cannot_turn_source_failures_into_successful_empty(tmp_path):
    from evidence_workspace import read_page
    from lib.recall_relevance import resolve_min_relevance
    import uuid
    rid = str(uuid.uuid4())
    weak = resolve_min_relevance({"recall_policy": {"default_min_relevance": "weak"}})
    strong = resolve_min_relevance({"recall_policy": {"default_min_relevance": "strong"}})
    (tmp_path / (rid + ".json")).write_text(json.dumps({"bank":"bank","created_at":9_999_999_999,"status":"discovered_not_verified","memory_ids":["m1"],"raw_memory_ids":["m1"],"query":"layout","seconds":0.1,"tool_call_count":1,"invalid_reference_ids":[],"relevance_policy":weak}))
    def unavailable(*args, **kwargs):
        raise ConnectionError("Controlled source outage")
    with pytest.raises(RuntimeError, match="source"):
        read_page("bank", rid, 0, unavailable, tmp_path, relevance_policy=strong)
    assert json.loads((tmp_path / (rid + ".json")).read_text())["raw_memory_ids"] == ["m1"]


def test_failed_tool_keeps_explicit_binding_and_does_not_report_empty_success(tmp_path, monkeypatch):
    mcp = adapter()
    check = "a" * 32
    root = tmp_path / ".evolving-profile/audit"
    root.mkdir(parents=True)
    (root / "prompt-ingress.jsonl").write_text(json.dumps({"at":datetime.datetime.now(datetime.timezone.utc).isoformat(),"hook_invocation_id":check,"session_id":"s","turn_id":"t"}) + "\n")
    monkeypatch.setattr(mcp.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(mcp, "CURRENT_TOOL_CALL", {"name":"read_source","arguments":{"check_id":check,"memory_id":"00000000-0000-0000-0000-000000000000"}})
    with contextlib.redirect_stdout(io.StringIO()):
        mcp.reply("failed-source", error={"code":-32000,"message":"HTTP 404 Not Found"})
    event = json.loads((root / "mcp-tool-activity.jsonl").read_text().splitlines()[-1])
    assert event["binding_state"] == "prompt_bound"
    assert event["failed"] is True
    assert event["returned_count"] is None
    assert event["source_read_count"] == 0


def test_agent_snapshot_does_not_reread_the_store_for_every_source_link(tmp_path, monkeypatch):
    mcp = adapter()
    from lib.process_memory import ProcessMemoryStore
    path = tmp_path / "process.json"
    path.write_text(json.dumps({"records":[{"process_memory_id":f"pm_trace_{i}","kind":"trace","maturity":"verified","status":"active","text":"Orion retrieval counts duplicated; corrected counter aggregation."} for i in range(10)]}))
    store = ProcessMemoryStore(path)
    original = store._read
    reads = []
    def counted():
        reads.append(1)
        return original()
    monkeypatch.setattr(store,"_read",counted)
    monkeypatch.setattr(mcp,"PROCESS_MEMORY_WORKSPACE_ROOT",tmp_path/"workspaces")
    monkeypatch.setattr(mcp,"load_runtime_settings",lambda:{})
    result,_ = mcp._snapshot_page(store,"one-read","sig",query="Orion retrieval counts",facets=["counts","aggregation"],compatibility={},task_archetype=None,primary_context=None,include_unverified=True,offset=0,limit=8)
    assert result["total_count"] == 10
    assert len(reads) == 1


def test_agent_workspace_identity_includes_hard_model_scope(tmp_path, monkeypatch):
    mcp = adapter()
    from lib.process_memory import ProcessMemoryStore
    path = tmp_path / "process.json"
    path.write_text(json.dumps({"records":[{"process_memory_id":f"pm_trace_{family}","kind":"trace","maturity":"verified","status":"active","text":"pagination retrieval source","model_profile":{"family":family}} for family in ("model-a","model-b")]}))
    monkeypatch.setattr(mcp,"PROCESS_MEMORY_WORKSPACE_ROOT",tmp_path/"workspaces")
    monkeypatch.setattr(mcp,"load_runtime_settings",lambda:{})
    store = ProcessMemoryStore(path)
    common = dict(query="pagination retrieval",facets=[],task_archetype=None,primary_context=None,include_unverified=True,offset=0,limit=8)
    first,_=mcp._snapshot_page(store,"scope-test","samequery",compatibility={"model_family":"model-a"},**common)
    second,_=mcp._snapshot_page(store,"scope-test","samequery",compatibility={"model_family":"model-b"},**common)
    assert [r["process_memory_id"] for r in first["records"]]==["pm_trace_model-a"]
    assert [r["process_memory_id"] for r in second["records"]]==["pm_trace_model-b"]


def test_unchanged_policy_source_outage_is_failure_not_empty(tmp_path):
    from evidence_workspace import read_page
    from lib.recall_relevance import resolve_min_relevance
    import uuid
    rid=str(uuid.uuid4());policy=resolve_min_relevance({})
    (tmp_path/(rid+".json")).write_text(json.dumps({"bank":"bank","created_at":9_999_999_999,"status":"discovered_not_verified","memory_ids":["m1"],"raw_memory_ids":["m1"],"query":"pagination","seconds":0.1,"tool_call_count":1,"invalid_reference_ids":[],"relevance_policy":policy}))
    def unavailable(*args,**kwargs):raise ConnectionError("source down")
    with pytest.raises(RuntimeError,match="source"):
        read_page("bank",rid,0,unavailable,tmp_path,relevance_policy=policy)
