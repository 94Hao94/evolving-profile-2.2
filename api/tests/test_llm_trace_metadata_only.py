from datetime import datetime, timezone

import pytest

from evolving_profile_api.engine.llm_trace import LLMRequestRecord
from tests.test_llm_trace import trace_api_client, bank_id  # noqa: F401 -- shared real HTTP fixtures


async def seed(memory, trace_api_client, bank_id):
    await trace_api_client.put(f"/v1/default/banks/{bank_id}", json={"name": "Metadata test"})
    at = datetime.now(timezone.utc)
    for index in range(3):
        await memory._llm_recorder._safe_write(LLMRequestRecord(
            bank_id=bank_id, operation="consolidation", trace_id="metadata-run" if index < 2 else "other-run",
            provider="test", model="test", scope="consolidation", status="error", started_at=at, ended_at=at,
            input={"messages": [{"content": "private prompt payload"}]}, output={"text": "private output payload"},
            error="ValidationError: observation_id missing",
        ))


@pytest.mark.asyncio
async def test_metadata_only_http_preserves_counts_without_prompt_or_output(memory, trace_api_client, bank_id):
    await seed(memory, trace_api_client, bank_id)
    path = f"/v1/default/banks/{bank_id}/llm-requests"
    response = await trace_api_client.get(path, params={"include_content": "false", "limit": 2})
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 3 and len(body["items"]) == 2
    assert all(item["input"] is None and item["output"] is None for item in body["items"])
    assert "private prompt payload" not in response.text and "private output payload" not in response.text
    legacy = await trace_api_client.get(path)
    assert legacy.json()["items"][0]["input"] is not None


@pytest.mark.asyncio
async def test_count_only_grouped_http_counts_runs_without_loading_rows(memory, trace_api_client, bank_id, monkeypatch):
    await seed(memory, trace_api_client, bank_id)
    def no_payload_mapping(*args, **kwargs):
        raise AssertionError("count-only must not map any trace rows")
    monkeypatch.setattr(memory, "_llm_request_entry", no_payload_mapping)
    response = await trace_api_client.get(f"/v1/default/banks/{bank_id}/llm-requests", params={"group": "true", "count_only": "true", "limit": 1})
    assert response.status_code == 200
    assert response.json()["total"] == 2 and response.json()["items"] == []
