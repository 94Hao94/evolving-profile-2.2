"""Reflect distinguishes unavailable retrieval from a successful empty lookup."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from evolving_profile_api.engine.reflect.agent import run_reflect_agent
from evolving_profile_api.engine.response_models import LLMToolCall, LLMToolCallResult, TokenUsage


class InvalidToolRequest(RuntimeError):
    status_code = 400


def provider(responses):
    return SimpleNamespace(
        provider="mock", model="test", _provider_impl=None,
        call_with_tools=AsyncMock(side_effect=responses),
        call=AsyncMock(return_value=("No stored information.", TokenUsage())),
    )


def retrieval(call_id="lookup"):
    return LLMToolCallResult(tool_calls=[LLMToolCall(id=call_id, name="recall", arguments={"query":"team"})])


def callbacks(memories=None):
    return {
        "search_mental_models_fn":AsyncMock(return_value={"mental_models":[]}),
        "search_observations_fn":AsyncMock(return_value={"observations":[]}),
        "recall_fn":AsyncMock(return_value={"memories":memories or []}),
        "expand_fn":AsyncMock(return_value={"memories":[]}),
    }


async def run(llm, functions, **kwargs):
    return await run_reflect_agent(llm_config=llm, bank_id="test", query="team overview", bank_profile={},
                                  include_observations=False, max_iterations=4, **functions, **kwargs)


@pytest.mark.asyncio
async def test_failed_retrieval_cannot_be_reported_as_no_stored_information():
    llm = provider([RuntimeError("transport unavailable"), RuntimeError("transport unavailable")])
    with pytest.raises(RuntimeError, match="Retrieval unavailable"):
        await run(llm, callbacks())


@pytest.mark.asyncio
async def test_failed_lookup_cannot_be_hidden_by_a_done_tool_answer():
    llm = provider([retrieval(), LLMToolCallResult(tool_calls=[
        LLMToolCall(id="done", name="done", arguments={"answer":"No stored information."}),
    ])])
    functions = callbacks()
    functions["recall_fn"].side_effect = RuntimeError("retrieval storage unavailable")
    with pytest.raises(RuntimeError, match="Retrieval unavailable"):
        await run(llm, functions)


@pytest.mark.asyncio
async def test_a_completed_empty_lookup_remains_a_valid_empty_answer():
    llm = provider([retrieval(), RuntimeError("transport unavailable"), RuntimeError("transport unavailable")])
    result = await run(llm, callbacks())
    assert result.text == "No stored information."


@pytest.mark.asyncio
async def test_partial_retrieval_keeps_the_available_source_when_a_later_call_fails():
    llm = provider([retrieval(), RuntimeError("transport unavailable")])
    result = await run(llm, callbacks([{"id":"alice-source", "text":"Alice is an engineer."}]))
    assert result.tool_trace[0].output["memories"][0]["id"] == "alice-source"
    assert any(call.scope.endswith("_err") for call in result.llm_trace)


@pytest.mark.asyncio
async def test_context_only_task_does_not_gain_a_requirement_to_search_history():
    llm = provider([RuntimeError("transport unavailable"), RuntimeError("transport unavailable")])
    llm.call.return_value = ("Rewritten supplied text.", TokenUsage())
    result = await run(llm, callbacks(), include_recall=False, context="Rewrite this supplied sentence.")
    assert result.text == "Rewritten supplied text."


@pytest.mark.asyncio
async def test_http_reports_retrieval_unavailable_instead_of_successful_empty_data(api_client, memory, request_context):
    bank_id = "test-retrieval-unavailable-http"
    await memory.get_bank_profile(bank_id=bank_id, request_context=request_context)
    memory._reflect_llm_config._provider_impl.set_mock_exception(RuntimeError("upstream tool selection unavailable"))
    response = await api_client.post(f"/v1/default/banks/{bank_id}/reflect", json={"query":"Who is on the team?"})
    assert response.status_code == 503
    assert response.json()["detail"]["error"] == "retrieval_unavailable"


@pytest.mark.asyncio
async def test_http_keeps_real_sources_and_marks_degraded_after_a_later_model_error(api_client, memory, request_context):
    secret = "fixture-private-token=http-trace"
    bank_id = "test-retrieval-partial-http"
    await memory.retain_async(bank_id=bank_id, content="Alice is a senior engineer.", request_context=request_context)
    memory._reflect_llm_config._provider_impl.call_with_tools = AsyncMock(side_effect=[
        retrieval(), RuntimeError(secret),
    ])
    response = await api_client.post(
        f"/v1/default/banks/{bank_id}/reflect", json={"query":"Who is Alice?", "include":{"facts":{}, "tool_calls":{}}},
    )
    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "degraded"
    assert result["errors"]
    assert secret not in response.text
    assert any("Alice" in row["text"] for row in result["based_on"]["memories"])


@pytest.mark.asyncio
@pytest.mark.parametrize("failure,retryable", [
    (RuntimeError("context_length_exceeded"), False),
    (TimeoutError("upstream unavailable"), True),
    (InvalidToolRequest("invalid tool choice"), False),
])
async def test_unavailable_retrieval_exposes_safe_retryability(failure, retryable):
    llm = provider([failure, failure])
    with pytest.raises(RuntimeError) as raised:
        await run(llm, callbacks())
    assert raised.value.retryable is retryable


@pytest.mark.asyncio
@pytest.mark.parametrize("returned_error", [False, True])
async def test_callback_error_details_never_reach_the_model_or_public_trace(returned_error):
    secret = "fixture-private-token=do-not-disclose"
    llm = provider([retrieval("first"), retrieval("second"), LLMToolCallResult(tool_calls=[
        LLMToolCall(id="done", name="done", arguments={"answer":"Alice is an engineer.", "memory_ids":["alice-source"]}),
    ])])
    functions = callbacks()
    functions["recall_fn"].side_effect = [
        {"memories":[{"id":"alice-source", "text":"Alice is an engineer."}]},
        {"error":secret, "details":secret} if returned_error else RuntimeError(secret),
    ]
    result = await run(llm, functions)
    assert secret not in result.model_dump_json()
    assert secret not in json.dumps(llm.call_with_tools.await_args.kwargs["messages"])
    assert result.used_memory_ids == ["alice-source"]


@pytest.mark.asyncio
@pytest.mark.parametrize("rows", [[], [{"memory_id":"alice-source", "memory":{"id":"alice-source", "text":"Alice is an engineer.", "type":"world"}}]])
async def test_bank_scoped_expand_results_are_completed_lookups(rows):
    llm = provider([LLMToolCallResult(tool_calls=[
        LLMToolCall(id="expand", name="expand", arguments={"memory_ids":["alice-source"]}),
    ]), RuntimeError("transport unavailable"), RuntimeError("transport unavailable")])
    functions = callbacks()
    functions["expand_fn"].return_value = {"results":rows, "count":len(rows)}
    result = await run(llm, functions)
    assert result.text == "No stored information."


@pytest.mark.asyncio
async def test_premature_done_without_required_lookup_cannot_be_a_no_data_success():
    done = LLMToolCallResult(tool_calls=[LLMToolCall(id="done", name="done", arguments={"answer":"No stored information."})])
    llm = provider([done, done, done])
    with pytest.raises(RuntimeError) as raised:
        await run(llm, callbacks())
    assert raised.value.category == "required_retrieval_not_completed"
    assert raised.value.retryable is False


@pytest.mark.asyncio
@pytest.mark.parametrize("repaired", [True, False])
async def test_malformed_done_gets_one_changed_input_repair_with_original_sources(repaired):
    malformed = LLMToolCallResult(tool_calls=[LLMToolCall(id="broken", name="done", arguments={"_raw":'{"answer":'})])
    fixed = LLMToolCallResult(tool_calls=[LLMToolCall(id="fixed", name="done", arguments={"answer":"Alice is an engineer.", "memory_ids":["alice-source"]})])
    llm = provider([retrieval(), malformed, fixed if repaired else malformed])
    functions = callbacks([{"id":"alice-source", "text":"Alice is an engineer."}])
    if repaired:
        result = await run(llm, functions)
        assert result.text == "Alice is an engineer."
        assert result.used_memory_ids == ["alice-source"]
    else:
        with pytest.raises(RuntimeError, match="returned no answer"):
            await run(llm, functions)
    assert llm.call_with_tools.await_count == 3
    correction = llm.call_with_tools.await_args.kwargs
    assert correction["scope"] == "reflect_done_repair"
    assert "alice-source" in json.dumps(correction["messages"])
    assert correction["messages"][-1]["role"] == "tool"


@pytest.mark.asyncio
async def test_nominal_transport_content_is_not_exposed_as_a_public_error(api_client, memory, request_context):
    secret = "fixture-private-token=nominal-transport"
    bank_id = "test-nominal-error-privacy"
    await memory.get_bank_profile(bank_id=bank_id, request_context=request_context)
    memory._reflect_llm_config._provider_impl.call_with_tools = AsyncMock(return_value=LLMToolCallResult(tool_calls=[], content=secret))
    response = await api_client.post(f"/v1/default/banks/{bank_id}/reflect", json={"query":"team overview", "include":{"tool_calls":{}}})
    assert response.status_code == 500
    assert secret not in response.text


@pytest.mark.asyncio
async def test_expand_partial_error_preserves_real_memory_and_hides_diagnostics():
    secret = "fixture-private-token=expand-diagnostics"
    llm = provider([LLMToolCallResult(tool_calls=[
        LLMToolCall(id="expand", name="expand", arguments={"memory_ids":["alice-source", "bad-source"]}),
    ]), LLMToolCallResult(tool_calls=[LLMToolCall(id="done", name="done", arguments={"answer":"Alice is an engineer.", "memory_ids":["alice-source"]})])])
    functions = callbacks()
    functions["expand_fn"].return_value = {"results":[
        {"memory_id":"alice-source", "memory":{"id":"alice-source", "text":"User-provided original token stays in memory.", "type":"world"}},
        {"memory_id":"bad-source", "error":secret, "details":secret},
    ], "count":2, "details":secret}
    result = await run(llm, functions)
    assert result.used_memory_ids == ["alice-source"]
    assert result.tool_trace[0].output["error"] == "retrieval_unavailable"
    assert secret not in result.model_dump_json()
    assert secret not in json.dumps(llm.call_with_tools.await_args.kwargs["messages"])
    assert result.tool_trace[0].output["results"][0]["memory"]["text"] == "User-provided original token stays in memory."


@pytest.mark.asyncio
@pytest.mark.parametrize("failure,code", [(RuntimeError("fixture-private-token=final"),500), (TimeoutError("fixture-private-token=final"),504)])
async def test_final_synthesis_errors_keep_transport_details_out_of_http(api_client, memory, request_context, failure, code):
    bank_id = f"test-final-error-privacy-{code}"
    await memory.retain_async(bank_id=bank_id, content="Alice is a senior engineer.", request_context=request_context)
    implementation = memory._reflect_llm_config._provider_impl
    implementation.call_with_tools = AsyncMock(side_effect=[retrieval(), LLMToolCallResult(tool_calls=[], content="")])
    implementation.call = AsyncMock(side_effect=failure)
    response = await api_client.post(f"/v1/default/banks/{bank_id}/reflect", json={"query":"Who is Alice?", "include":{"tool_calls":{}}})
    assert response.status_code == code
    assert "fixture-private-token" not in response.text
