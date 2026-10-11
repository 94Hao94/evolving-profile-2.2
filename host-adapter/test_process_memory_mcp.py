import json
import io
import sys
import pytest


def test_process_memory_tools_are_registered():
    sys.stdin = io.StringIO("")
    import evolving_profile_controller_mcp as mcp

    names = {tool["name"] for tool in (
        mcp.AGENT_TRAJECTORY_TOOL,
        mcp.AGENT_DRAFT_TOOL,
        mcp.AGENT_PROMOTION_TOOL,
        mcp.AGENT_SEARCH_TOOL,
        mcp.AGENT_READ_TOOL,
        mcp.AGENT_CAPABILITY_TOOL,
        mcp.AGENT_CONTEXT_TOOL,
        mcp.AGENT_REVALIDATION_TOOL,
        mcp.AGENT_EVALUATION_TOOL,
    )}
    assert names == {
        "record_agent_trajectory",
        "record_agent_process_draft",
        "promote_agent_process_memory",
        "search_agent_process_memory",
        "read_agent_process_memory",
        "record_agent_capability_observation",
        "prepare_agent_process_context",
        "revalidate_agent_process_memory",
        "evaluate_agent_process_memory",
    }


def test_process_memory_handler_records_and_reads(tmp_path, monkeypatch):
    sys.stdin = io.StringIO("")
    import evolving_profile_controller_mcp as mcp

    monkeypatch.setattr(mcp, "PROCESS_MEMORY_PATH", tmp_path / "records.json")
    monkeypatch.setattr(mcp, "runtime_disabled", lambda *_args: None)
    response = mcp.record_agent_trajectory({
        "task_archetype": ["software_engineering"],
        "process_dimensions": ["verification"],
        "phase": "verify",
        "text": "pytest passed",
        "model_profile": {"family": "model-a", "version": "1"},
    })
    payload = json.loads(response["content"][0]["text"])
    process_id = payload["record"]["process_memory_id"]
    read = mcp.read_agent_process_memory({"process_memory_id": process_id})
    read_payload = json.loads(read["content"][0]["text"])
    assert read_payload["status"] == "observed"
    assert read_payload["record"]["process_memory_id"] == process_id


def test_disabled_process_memory_does_not_touch_store(tmp_path, monkeypatch):
    sys.stdin = io.StringIO("")
    import evolving_profile_controller_mcp as mcp

    monkeypatch.setattr(mcp, "PROCESS_MEMORY_PATH", tmp_path / "records.json")
    monkeypatch.setattr(mcp, "runtime_disabled", lambda *_args: {"content": [{"type": "text", "text": "disabled"}], "isError": False})
    result = mcp.record_agent_trajectory({"task_archetype": ["other"], "phase": "observe", "text": "noop"})
    assert result["content"][0]["text"] == "disabled"
    assert not (tmp_path / "records.json").exists()


def test_process_draft_handler_records_candidate(tmp_path, monkeypatch):
    import evolving_profile_controller_mcp as mcp

    monkeypatch.setattr(mcp, "PROCESS_MEMORY_PATH", tmp_path / "records.json")
    monkeypatch.setattr(mcp, "runtime_disabled", lambda *_args: None)
    response = mcp.record_agent_process_draft({
        "task_archetype": ["software_engineering"],
        "phase": "recover",
        "text": "失败后修复并通过测试。",
        "failure_signature": ["test_failure"],
        "repair_actions": ["fix"],
        "source_trace_ids": ["trace-1"],
    })
    payload = json.loads(response["content"][0]["text"])
    assert payload["status"] == "candidate_recorded"
    assert payload["record"]["kind"] == "process_draft"


def test_tool_capture_stores_bounded_metadata(tmp_path, monkeypatch):
    import evolving_profile_controller_mcp as mcp

    monkeypatch.setattr(mcp, "PROCESS_MEMORY_PATH", tmp_path / "records.json")
    monkeypatch.setattr(mcp, "runtime_disabled", lambda *_args: None)
    mcp.capture_tool_trajectory("recall", {"check_id": "check-1", "query": "private prompt must not be copied", "project_id": "p1", "session_id": "s1", "task_id": "t1", "model_family": "luna"})
    rows = json.loads((tmp_path / "records.json").read_text())['records']
    assert rows[0]["kind"] == "trace"
    assert "private prompt" not in rows[0]["text"]
    assert "retrieval" in rows[0]["process_dimensions"]
    assert rows[0]["primary_context"] == {"project_id": "p1", "session_id": "s1", "task_id": "t1"}
    assert rows[0]["model_profile"]["family"] == "luna"


def test_prepare_process_context_returns_explicit_hint_packet(tmp_path, monkeypatch):
    import evolving_profile_controller_mcp as mcp
    monkeypatch.setattr(mcp, "PROCESS_MEMORY_PATH", tmp_path / "records.json")
    monkeypatch.setattr(mcp, "runtime_disabled", lambda *_args: None)
    trace = mcp.record_agent_trajectory({"task_archetype": ["software_engineering"], "phase": "recover", "text": "test failure repaired", "model_profile": {"family": "model-a", "version": "1"}})
    trace_id = json.loads(trace["content"][0]["text"])["record"]["process_memory_id"]
    mcp.promote_agent_process_memory({"target_kind": "episode", "source_ids": [trace_id], "payload": {"text": "repair", "verification_evidence": [{"verifier_kind": "automated_test", "status": "passed"}]}})
    response = mcp.prepare_agent_process_context({"query": "test failure", "model_family": "model-a", "model_version": "1", "task_archetype": "software_engineering"})
    packet = json.loads(response["content"][0]["text"])
    assert packet["schema"] == "evolving-profile.agent-process-context.v1"
    assert packet["status"] == "prepared"
    assert packet["hints"][0]["evidence_links"]
    assert packet["automatic_injection"] is False


def test_agent_recall_paginates_without_semantic_total_cap_and_persists_workspace(tmp_path, monkeypatch):
    import evolving_profile_controller_mcp as mcp
    monkeypatch.setattr(mcp, "PROCESS_MEMORY_PATH", tmp_path / "records.json")
    monkeypatch.setattr(mcp, "PROCESS_MEMORY_WORKSPACE_ROOT", tmp_path / "workspaces")
    monkeypatch.setattr(mcp, "runtime_disabled", lambda *_args: None)
    for index in range(7):
        mcp.record_agent_trajectory({
            "task_archetype": ["web_ui_operation"], "phase": "recover",
            "text": f"拖动框缩放手柄视觉检查第{index}次",
        })
    first = json.loads(mcp.search_agent_process_memory({
        "query": "框无法拖动 缩放手柄 视觉检查", "task_archetype": "web_ui_operation", "limit": 2,
    })["content"][0]["text"])
    second = json.loads(mcp.search_agent_process_memory({
        "query": "框无法拖动 缩放手柄 视觉检查", "task_archetype": "web_ui_operation", "limit": 2,
        "offset": first["next_offset"], "workspace_id": first["workspace_id"],
    })["content"][0]["text"])
    assert first["total_count"] == 7
    assert first["next_offset"] == 2
    assert second["offset"] == 2
    assert not ({row["process_memory_id"] for row in first["records"]} & {row["process_memory_id"] for row in second["records"]})
    assert first["workspace"]["path"]
    assert (tmp_path / "workspaces" / f"{first['workspace_id']}.json").exists()


def test_agent_recall_keeps_unrelated_query_empty(tmp_path, monkeypatch):
    import evolving_profile_controller_mcp as mcp
    monkeypatch.setattr(mcp, "PROCESS_MEMORY_PATH", tmp_path / "records.json")
    monkeypatch.setattr(mcp, "PROCESS_MEMORY_WORKSPACE_ROOT", tmp_path / "workspaces")
    monkeypatch.setattr(mcp, "runtime_disabled", lambda *_args: None)
    mcp.record_agent_trajectory({"task_archetype": ["web_ui_operation"], "phase": "observe", "text": "拖动框缩放手柄视觉检查"})
    result = json.loads(mcp.search_agent_process_memory({"query": "家庭旅行 美食 景点", "limit": 2})["content"][0]["text"])
    assert result["returned_count"] == 0
    assert result["total_count"] == 0
    assert result["next_offset"] is None


def test_agent_process_scenario_followup_only_requires_summary_for_ambiguous_scope(tmp_path, monkeypatch):
    import evolving_profile_controller_mcp as mcp
    context_path = tmp_path / "context-index.json"
    context_path.write_text(json.dumps({
        "sessions": [
            {"context_id": "session:s1", "context_type": "session", "session_id": "s1", "project_key": "p1", "title": "项目一"},
            {"context_id": "session:s2", "context_type": "session", "session_id": "s2", "project_key": "p2", "title": "项目二"},
        ], "projects": [],
    }, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(mcp, "CONTEXT_INDEX_PATH", context_path)
    one = mcp.agent_process_scenario_followup([{"process_memory_id": "pm1", "primary_context": {"session_id": "s1"}}])
    assert one["required"] is False
    assert one["next_tool"] is None
    many = mcp.agent_process_scenario_followup([
        {"process_memory_id": "pm1", "primary_context": {"session_id": "s1"}},
        {"process_memory_id": "pm2", "primary_context": {"session_id": "s2"}},
    ])
    assert many["required"] is True
    assert many["next_tool"] == "read_scenario_summary"
    assert len(many["scenarios"]) == 2


def test_revalidation_handler_requires_evidence_to_restore(tmp_path, monkeypatch):
    import evolving_profile_controller_mcp as mcp
    monkeypatch.setattr(mcp, "PROCESS_MEMORY_PATH", tmp_path / "records.json")
    monkeypatch.setattr(mcp, "runtime_disabled", lambda *_args: None)
    trace = mcp.record_agent_trajectory({"task_archetype": ["software_engineering"], "phase": "verify", "text": "verified"})
    record_id = json.loads(trace["content"][0]["text"])["record"]["process_memory_id"]
    held = mcp.revalidate_agent_process_memory({"process_memory_id": record_id, "drift_status": "revalidation_required"})
    assert json.loads(held["content"][0]["text"])["record"]["drift_status"] == "revalidation_required"
    with pytest.raises(ValueError, match="revalidation_evidence_required"):
        mcp.revalidate_agent_process_memory({"process_memory_id": record_id, "drift_status": "stable"})


def test_evaluation_handler_is_deterministic_and_read_only(monkeypatch):
    import evolving_profile_controller_mcp as mcp
    monkeypatch.setattr(mcp, "runtime_disabled", lambda *_args: None)
    response = mcp.evaluate_agent_process_memory({"baseline": [{"failed": True, "recovery_seconds": 20}], "memory": [{"failed": False, "recovery_seconds": 10}], "transfer": {"sample_count": 6, "improvement_rate": 0.2}})
    payload = json.loads(response["content"][0]["text"])
    assert payload["schema"] == "evolving-profile.agent-process-evaluation.v1"
    assert payload["transfer"]["status"] == "candidate"


def test_rollout_handler_has_record_gate(tmp_path, monkeypatch):
    import evolving_profile_controller_mcp as mcp
    monkeypatch.setattr(mcp, 'PROCESS_MEMORY_PATH', tmp_path / 'records.json')
    monkeypatch.setattr(mcp, 'runtime_disabled', lambda *_: {'content': [{'type': 'text', 'text': 'disabled'}]})
    assert mcp.manage_agent_process_rollout({'action': 'rollback', 'process_memory_id': 'missing'})['content'][0]['text'] == 'disabled'
    assert not (tmp_path / 'records.json').exists()
    assert mcp.AGENT_ROLLOUT_TOOL['name'] == 'manage_agent_process_rollout'


def test_process_dimensions_use_independent_runtime_modules(monkeypatch):
    import evolving_profile_controller_mcp as mcp
    calls = []
    monkeypatch.setattr(mcp, 'runtime_disabled', lambda module, action='retrieve': calls.append((module, action)) or None)
    assert mcp.process_module_for_kind('episode') == 'agent_process_failure_episode'
    assert mcp.process_module_for_kind('skill') == 'agent_process_strategy'
    assert mcp.process_module_for_kind('unknown') == 'agent_process_memory'
    mcp.process_runtime_disabled('inject', 'episode')
    assert calls == [('agent_process_memory', 'inject'), ('agent_process_failure_episode', 'inject')]
