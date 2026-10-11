"""Consumer-facing regressions from the six real retrieval audits."""
import json
from unittest.mock import patch

import pytest


def test_process_judge_reads_record_without_promoting_it_to_original_source():
    from lib.jev_judge import project_tool_review
    captured = {}
    def judge(purposes, state):
        captured.update(state)
        return {"status": "ok"}
    with patch("lib.jev_judge.review", side_effect=judge):
        project_tool_review("read_agent_process_memory", {}, {
            "source": "agent_process_memory", "record": {
                "process_memory_id": "pm_trace_a", "text": "Repair report",
                "source_integrity": {"status": "dangling"},
            }}, {"state": "prompt_bound"})
    assert captured["returned_count"] == 1
    assert captured["source_readback"] is False
    assert captured["candidates"][0]["id"] == "pm_trace_a"
    assert captured["candidates"][0]["source_integrity"]["status"] == "dangling"


def test_judge_original_source_requires_actual_body_not_tool_name():
    from lib.jev_judge import project_tool_review
    states = []
    with patch("lib.jev_judge.review", side_effect=lambda p, s: states.append(s) or {}):
        project_tool_review("read_source", {}, {"source": {"text": "original quote"}}, {})
        project_tool_review("read_source", {}, {"source": {}}, {})
    assert states[0]["source_readback"] is True
    assert states[1]["source_readback"] is False


@pytest.mark.parametrize("query,text", [
    ("文档文字溢出如何修复", "为了避免截断历史，设置召回分页和上下文token预算"),
    ("修复网页文字裁切", "Set pagination to avoid clipping retrieval history at a token budget"),
])
def test_context_budget_clipping_is_not_a_visual_layout_repair(query, text):
    from lib.recall_relevance import classify_candidate
    assert classify_candidate(query, {"text": text})["level"] in {"none", "weak"}


def test_new_root_trace_has_a_real_local_source_not_random_dangling_reference(tmp_path):
    from lib.process_memory import ProcessMemoryStore
    store = ProcessMemoryStore(tmp_path / "records.json")
    row = store.record_trajectory({"text": "Observed an actual operation", "phase": "observe"})
    assert row["source_trace_ids"] == [row["process_memory_id"]]
    assert store.audit_source_integrity(row)["source_integrity"]["status"] == "resolved"
    assert row["maturity"] == "observed"


def test_missing_target_scenario_is_not_reported_ready(tmp_path, monkeypatch):
    import evolving_profile_controller_mcp as mcp
    path = tmp_path / "context.json"
    path.write_text(json.dumps({"schema": "evolving-profile.context-index.v1", "sessions": [], "projects": []}))
    monkeypatch.setattr(mcp, "CONTEXT_INDEX_PATH", path)
    monkeypatch.setattr(mcp, "THREAD_SESSION_ROOT", tmp_path / "missing")
    monkeypatch.setattr(mcp, "runtime_disabled", lambda *a: None)
    r = json.loads(mcp.read_context_summary({"scenario_type": "session", "session_id": "00000000-0000-4000-8000-000000000001"})["content"][0]["text"])
    assert r["status"] == "not_found"
    assert r["items"] == []


def test_raw_session_locator_reads_requested_turn_without_claiming_verified_repair(tmp_path):
    from lib.raw_session_evidence import read_evidence
    root = tmp_path / "sessions"; root.mkdir()
    sid = "00000000-0000-4000-8000-000000000001"
    rows = [
        {"type": "turn_context", "payload": {"turn_id": "t1"}},
        {"type": "response_item", "payload": {"type": "message", "role": "assistant", "phase": "final_answer", "content": [{"text": "Old proposal"}]}},
        {"type": "turn_context", "payload": {"turn_id": "t2"}},
        {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"text": "The popup is unreadable"}]}},
        {"type": "response_item", "payload": {"type": "message", "role": "assistant", "phase": "final_answer", "content": [{"text": "I claim the repair passed"}]}},
    ]
    path = root / f"rollout-test-{sid}.jsonl"
    path.write_text("\n".join(json.dumps(x) for x in rows) + "\n")
    result = read_evidence(sid, root, cache_root=tmp_path / "cache", turn_id="t2")
    assert "unreadable" in result["source"]["text"]
    assert "Old proposal" not in result["source"]["text"]
    assert result["coverage"]["matched_messages"] == 2
    assert result["claims_independently_verified"] is False
    assert result["source"]["locators"][0]["raw_line_sha256"]


def test_partial_appended_line_does_not_lose_later_raw_evidence(tmp_path):
    from lib.raw_session_evidence import read_evidence
    root = tmp_path / "sessions"; root.mkdir()
    sid = "00000000-0000-4000-8000-000000000002"
    path = root / f"rollout-test-{sid}.jsonl"
    raw = json.dumps({"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"text": "new evidence"}]}}).encode()
    path.write_bytes(raw[:15])
    assert read_evidence(sid, root, cache_root=tmp_path / "cache")["status"] == "source_empty"
    with path.open("ab") as f: f.write(raw[15:] + b"\n")
    assert "new evidence" in read_evidence(sid, root, cache_root=tmp_path / "cache")["source"]["text"]


def test_prompt_binding_survives_prefix_truncation(monkeypatch):
    import recall
    cid = "0123456789abcdef0123456789abcdef"
    monkeypatch.setattr(recall, "ENTRY_GUIDANCE_CONTEXT", "instructions" * 1000 + f'<evolving_profile_memory_route check_id="{cid}">route</evolving_profile_memory_route>')
    assert cid in recall._with_entry_guidance("candidates")[:300]


def test_shared_workspace_reports_repeated_candidates_without_dropping_them(tmp_path, monkeypatch):
    import evolving_profile_controller_mcp as mcp
    monkeypatch.setattr(mcp, "PROCESS_MEMORY_WORKSPACE_ROOT", tmp_path)
    first = mcp._write_process_workspace("shared", {"candidate_refs": [{"process_memory_id": "p1"}]})
    second = mcp._write_process_workspace("shared", {"candidate_refs": [{"process_memory_id": "p1"}, {"process_memory_id": "p2"}]})
    assert first["new_candidate_ids"] == ["p1"]
    assert second["new_candidate_ids"] == ["p2"]
    assert second["repeated_candidate_ids"] == ["p1"]
    assert second["candidate_count"] == 2


def test_scenario_search_discovers_current_process_session_not_only_legacy_index(tmp_path, monkeypatch):
    import evolving_profile_controller_mcp as mcp
    from lib.process_memory import ProcessMemoryStore
    path = tmp_path / "context.json"
    path.write_text(json.dumps({"schema": "evolving-profile.context-index.v1", "sessions": [], "projects": []}))
    monkeypatch.setattr(mcp, "CONTEXT_INDEX_PATH", path)
    monkeypatch.setattr(mcp, "PROCESS_MEMORY_PATH", tmp_path / "records.json")
    monkeypatch.setattr(mcp, "process_runtime_disabled", lambda *args: None)
    monkeypatch.setattr(mcp, "runtime_disabled", lambda *args: None)
    sid = "00000000-0000-4000-8000-000000000001"
    ProcessMemoryStore(tmp_path / "records.json").record_trajectory({"text": "弹窗布局文字溢出：调整容器高度再渲染", "primary_context": {"session_id": sid}})
    result = json.loads(mcp.search_scenario_summary({"query": "弹窗布局文字溢出"})["content"][0]["text"])
    assert any(row.get("session_id") == sid for row in result["items"])
    assert result["items"][0]["evidence_role"] == "context_navigation_only"
    assert result['coverage']['navigation_only'] is True
    assert result['items'][0]['next_tool'] == 'read_scenario_summary'


def test_scenario_search_preserves_canonical_navigation_without_leaking_summary(tmp_path, monkeypatch):
    import evolving_profile_controller_mcp as mcp
    sid = "00000000-0000-4000-8000-000000000001"
    path = tmp_path / "context.json"
    path.write_text(json.dumps({"schema": "evolving-profile.context-index.v1", "sessions": [{
        "context_id": "session:" + sid, "context_type": "session", "session_id": sid,
        "title": "执行复盘", "summary": {"compact": "弹窗布局文字溢出：调整容器高度后验证"},
    }], "projects": []}))
    monkeypatch.setattr(mcp, "CONTEXT_INDEX_PATH", path)
    monkeypatch.setattr(mcp, "runtime_disabled", lambda *a: None)
    monkeypatch.setattr(mcp, "process_runtime_disabled", lambda *a: {"disabled": True})
    result = json.loads(mcp.search_scenario_summary({"query": "弹窗布局文字溢出"})["content"][0]["text"])
    assert any(row.get("session_id") == sid for row in result["items"])
    assert all("summary" not in row for row in result["items"])


def test_raw_evidence_marks_body_truncation_even_when_header_consumes_budget(tmp_path):
    from lib.raw_session_evidence import read_evidence
    sid = "00000000-0000-4000-8000-000000000003"
    root = tmp_path / "sessions"; root.mkdir()
    (root / f"rollout-test-{sid}.jsonl").write_text(json.dumps({"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"text": "x" * 100}]}}) + "\n")
    r = read_evidence(sid, root, cache_root=tmp_path / "cache", max_chars=110)
    assert r["coverage"]["partial"] is True
    assert (tmp_path / "cache").stat().st_mode & 0o777 == 0o700
    assert (tmp_path / "cache" / (sid + ".sqlite")).stat().st_mode & 0o777 == 0o600


def test_recall_pagination_survives_interleaved_research_same_prompt(tmp_path, monkeypatch):
    import evolving_profile_controller_mcp as mcp
    from lib.process_memory import ProcessMemoryStore
    monkeypatch.setattr(mcp, "PROCESS_MEMORY_PATH", tmp_path / "records.json")
    monkeypatch.setattr(mcp, "PROCESS_MEMORY_WORKSPACE_ROOT", tmp_path / "workspaces")
    monkeypatch.setattr(mcp, "process_runtime_disabled", lambda *a: None)
    store = ProcessMemoryStore(tmp_path / "records.json")
    for i in range(4):store.record_trajectory({"text": f"弹窗布局文字溢出，调整容器高度再渲染验证 {i}"})
    args = {"check_id": "same-prompt", "query": "弹窗布局文字溢出", "limit": 2}
    first = json.loads(mcp.search_agent_process_memory(args)["content"][0]["text"])
    mcp.research_agent_process_memory({**args, "facets": ["调整容器高度"]})
    last = json.loads(mcp.search_agent_process_memory({**args, "offset": 2})["content"][0]["text"])
    assert not ({r['process_memory_id'] for r in first['records']} & {r['process_memory_id'] for r in last['records']})


def test_changed_raw_source_is_invalidated_and_next_read_recovers(tmp_path):
    from lib.raw_session_evidence import read_evidence
    sid = "00000000-0000-4000-8000-000000000004"
    root = tmp_path / "sessions"; root.mkdir()
    path = root / f"rollout-test-{sid}.jsonl"
    raw = json.dumps({"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"text": "old text"}]}}) + "\n"
    path.write_text(raw)
    assert read_evidence(sid, root, cache_root=tmp_path / "cache")["status"] == "source_read"
    path.write_text(raw.replace('old text', 'new text'))
    assert read_evidence(sid, root, cache_root=tmp_path / "cache")["status"] == "source_changed"
    assert 'new text' in read_evidence(sid, root, cache_root=tmp_path / "cache")["source"]["text"]


def test_ambient_envelope_does_not_hide_following_real_request(tmp_path):
    from lib.raw_session_evidence import read_evidence
    sid = "00000000-0000-4000-8000-000000000005"
    root = tmp_path / "sessions"; root.mkdir()
    text = '<environment_context>host data</environment_context>\n## My request:\nFix the popup'
    (root / f"rollout-test-{sid}.jsonl").write_text(json.dumps({"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"text": text}]}}) + "\n")
    assert 'Fix the popup' in read_evidence(sid, root, cache_root=tmp_path / "cache")["source"]["text"]
