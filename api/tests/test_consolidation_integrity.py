"""Batch-bound output recovery never turns ambiguous identifiers into writes."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError

from evolving_profile_api.engine.consolidation.consolidator import (
    _BatchLLMResult,
    _build_response_model,
    _consolidate_batch_with_llm,
    _process_memory_batch,
)


def _model():
    return _build_response_model(observation_ids={"obs-a", "obs-b"}, source_fact_ids={"fact-a", "fact-b"})


@pytest.mark.parametrize("action", ["deletes", "updates"])
def test_batch_bound_id_alias_recovers_without_mutating_provider_payload(action):
    item = {"id": "obs-a", "reason": "superseded"}
    if action == "updates":
        item.update(text="supported update", source_fact_ids=["fact-a"])
    payload = {"creates": [], "updates": [], "deletes": [], action: [item]}
    original = deepcopy(payload)
    result = _model().model_validate(payload)
    assert getattr(result, action)[0].observation_id == "obs-a"
    assert "id" not in result.model_dump()[action][0]
    assert payload == original


@pytest.mark.parametrize(
    "item",
    [
        {"id": "foreign"},
        {"id": "obs-b", "observation_id": "obs-a"},
        {"observation_id": "foreign"},
        {"id": 7},
        {"observation_id": ["obs-a"]},
    ],
)
def test_ambiguous_or_foreign_delete_rejects_entire_response(item):
    with pytest.raises(ValidationError):
        _model().model_validate({"deletes": [item]})


def test_canonical_and_matching_alias_keep_canonical_schema():
    result = _model().model_validate({"deletes": [{"observation_id": "obs-a", "id": "obs-a"}]})
    assert result.deletes[0].observation_id == "obs-a"
    schema = _model().model_json_schema()["$defs"]["_DeleteAction"]
    assert "observation_id" in schema["required"]
    assert "id" not in schema["properties"]


@pytest.mark.parametrize("sources", [[], ["foreign"], ["fact-a", "foreign"]])
def test_missing_or_foreign_sources_hold_complete_batch(sources):
    with pytest.raises(ValidationError):
        _model().model_validate({"creates": [{"text": "new", "source_fact_ids": sources}]})


def _config():
    return SimpleNamespace(
        observations_mission=None,
        llm_output_language=None,
        llm_supports_max_items=True,
        consolidation_max_attempts=3,
        consolidation_llm_max_retries=0,
        consolidation_max_completion_tokens=4096,
        llm_strict_schema_consolidation=True,
        llm_temperature_consolidation=0.0,
        max_observations_per_scope=-1,
        observation_scope_limits=[],
        consolidation_dedup_threshold=1.0,
    )


@pytest.mark.asyncio
async def test_missing_observation_field_gets_one_changed_schema_correction():
    prompts = []

    async def call(**kwargs):
        prompts.append(deepcopy(kwargs["messages"]))
        payload = {"deletes": [{"reason": "superseded"}]} if len(prompts) == 1 else {"deletes": []}
        return kwargs["response_format"].model_validate(payload)

    result = await _consolidate_batch_with_llm(
        SimpleNamespace(call=call),
        [{"id": "fact-a", "text": "evidence"}],
        [],
        {},
        _config(),
    )
    assert not result.failed
    assert len(prompts) == 2
    assert prompts[0] != prompts[1]
    assert "fact-a" in prompts[1][1]["content"]
    assert "observation_id" in prompts[1][-1]["content"]


@pytest.mark.asyncio
async def test_repeated_missing_field_correction_stops_with_original_evidence():
    prompts = []

    async def call(**kwargs):
        prompts.append(deepcopy(kwargs["messages"]))
        return kwargs["response_format"].model_validate({"deletes": [{"reason": "superseded"}]})

    result = await _consolidate_batch_with_llm(
        SimpleNamespace(call=call),
        [{"id": "fact-a", "text": "original evidence"}],
        [],
        {},
        _config(),
    )
    assert result.failed
    assert len(prompts) == 2
    assert all("original evidence" in prompt[1]["content"] for prompt in prompts)
    assert not result.deletes


def test_update_requires_the_targets_own_recalled_source_relation():
    model = _build_response_model(
        observation_ids={"obs-a", "obs-b"},
        source_fact_ids={"fact-a", "fact-b"},
        observation_source_ids={"obs-a": {"fact-a"}, "obs-b": {"fact-b"}},
    )
    with pytest.raises(ValidationError):
        model.model_validate(
            {"updates": [{"observation_id": "obs-b", "text": "wrong pairing", "source_fact_ids": ["fact-a"]}]}
        )
    result = model.model_validate(
        {"updates": [{"observation_id": "obs-b", "text": "supported", "source_fact_ids": ["fact-b"]}]}
    )
    assert result.updates[0].source_fact_ids == ["fact-b"]


def test_source_authority_keeps_lineage_separate_from_independent_user_origins():
    from evolving_profile_api.engine.consolidation.consolidator import _source_authority_for_llm

    rows = [
        {
            "id": "a",
            "metadata": {
                "source_role": "user",
                "independent_user_evidence": "true",
                "evidence_group_id": "same-origin",
            },
        },
        {
            "id": "b",
            "metadata": {"source_role": "user", "independent_user_evidence": True, "evidence_group_id": "same-origin"},
        },
        {
            "id": "c",
            "metadata": {
                "source_role": "assistant",
                "independent_user_evidence": False,
                "evidence_group_id": "proposal",
            },
        },
    ]
    result = _source_authority_for_llm(rows)
    assert result["source_lineage_count"] == 3
    assert result["independent_user_origin_count"] == 1
    assert result["assistant_source_ids"] == ["c"]


def test_incomplete_user_origin_never_becomes_two_votes_from_transport_ids():
    from evolving_profile_api.engine.consolidation.consolidator import _source_authority_for_llm

    assert (
        _source_authority_for_llm(
            [
                {"id": "copy-a", "metadata": {"source_role": "user", "independent_user_evidence": "true"}},
                {"id": "copy-b", "metadata": {"source_role": "user", "independent_user_evidence": "true"}},
            ]
        )["independent_user_origin_count"]
        == 0
    )


@pytest.mark.asyncio
async def test_pending_source_fetch_does_not_discard_authority_metadata():
    from evolving_profile_api.engine.consolidation.consolidator import _fetch_unconsolidated_rows
    from evolving_profile_api.engine.memories.base import StoredMemory

    metadata = {"source_role": "assistant", "independent_user_evidence": False, "evidence_group_id": "proposal"}
    memory = StoredMemory(
        unit_id="cd211ab2-6372-4de9-b8ed-63fe367c8228",
        text="unconfirmed advice",
        fact_type="experience",
        metadata=metadata,
    )
    with patch(
        "evolving_profile_api.engine.consolidation.consolidator.get_memories",
        return_value=SimpleNamespace(find_unconsolidated=AsyncMock(return_value=[memory])),
    ):
        result = await _fetch_unconsolidated_rows(None, "bank", ["experience"], 1, None)
    assert result[0]["metadata"] == metadata


@pytest.mark.asyncio
async def test_source_relation_error_gets_one_changed_correction_and_retains_context():
    prompts = []

    async def call(**kwargs):
        prompts.append(deepcopy(kwargs["messages"]))
        target = "obs-b" if len(prompts) == 1 else "obs-a"
        return kwargs["response_format"].model_validate(
            {"updates": [{"observation_id": target, "text": "supported", "source_fact_ids": ["fact-a"]}]}
        )

    result = await _consolidate_batch_with_llm(
        SimpleNamespace(call=call),
        [{"id": "fact-a", "text": "original evidence"}],
        [
            SimpleNamespace(
                id="obs-a",
                text="current",
                source_fact_ids=[],
                tags=[],
                occurred_start=None,
                occurred_end=None,
                mentioned_at=None,
            ),
            SimpleNamespace(
                id="obs-b",
                text="parent-only",
                source_fact_ids=[],
                tags=[],
                occurred_start=None,
                occurred_end=None,
                mentioned_at=None,
            ),
        ],
        {},
        _config(),
        observation_source_ids={"obs-a": {"fact-a"}, "obs-b": set()},
    )
    assert not result.failed and result.updates[0].observation_id == "obs-a"
    assert len(prompts) == 2 and prompts[0] != prompts[1]
    assert all("original evidence" in p[1]["content"] and "parent-only" in p[1]["content"] for p in prompts)


@pytest.mark.asyncio
async def test_repeated_source_relation_error_is_bounded_without_a_write():
    calls = []

    async def call(**kwargs):
        calls.append(kwargs)
        return kwargs["response_format"].model_validate(
            {"updates": [{"observation_id": "obs-b", "text": "wrong pairing", "source_fact_ids": ["fact-a"]}]}
        )

    result = await _consolidate_batch_with_llm(
        SimpleNamespace(call=call),
        [{"id": "fact-a", "text": "original evidence"}],
        [
            SimpleNamespace(
                id="obs-b",
                text="context",
                source_fact_ids=[],
                tags=[],
                occurred_start=None,
                occurred_end=None,
                mentioned_at=None,
            )
        ],
        {},
        _config(),
        observation_source_ids={"obs-b": set()},
    )
    assert result.failed and not result.updates and len(calls) == 2


@pytest.mark.asyncio
async def test_split_child_retains_parent_observation_and_source_context():
    observation_a = SimpleNamespace(id="obs-a")
    observation_b = SimpleNamespace(id="obs-b")
    source_a = SimpleNamespace(id="source-a")
    context = {}
    inputs = []

    async def llm(**kwargs):
        inputs.append(kwargs)
        return _BatchLLMResult(failed=True)

    recall_a = SimpleNamespace(results=[observation_a], source_facts={"source-a": source_a})
    recall_b = SimpleNamespace(results=[observation_b], source_facts={})
    recall_empty = SimpleNamespace(results=[], source_facts={})
    facts = [{"id": "fact-a", "text": "a"}, {"id": "fact-b", "text": "b"}]
    with (
        patch(
            "evolving_profile_api.engine.consolidation.consolidator._find_related_observations",
            new=AsyncMock(side_effect=[recall_a, recall_b, recall_empty, recall_empty]),
        ),
        patch("evolving_profile_api.engine.consolidation.consolidator._consolidate_batch_with_llm", new=llm),
    ):
        for batch in (facts, facts[:1], facts[1:]):
            result, deleted, failed = await _process_memory_batch(
                pool=None,
                memory_engine=None,
                llm_config=None,
                bank_id="test",
                memories=batch,
                request_context=None,
                config=_config(),
                observation_context=context,
            )
            assert failed and deleted == 0
        assert [m["id"] for call in inputs[1:] for m in call["memories"]] == ["fact-a", "fact-b"]
        for call in inputs:
            assert {obs.id for obs in call["union_observations"]} == {"obs-a", "obs-b"}
            assert call["union_source_facts"] == {"source-a": source_a}


@pytest.mark.asyncio
async def test_split_sibling_update_uses_prior_sibling_text_and_source_lineage():
    from evolving_profile_api.engine.consolidation.consolidator import _UpdateAction
    from evolving_profile_api.engine.response_models import MemoryFact

    original = MemoryFact(id="obs-a", text="Original observation", fact_type="observation", source_fact_ids=["origin"])
    recall = SimpleNamespace(results=[original], source_facts={})
    context = {}
    prompts = []
    writes = []

    async def llm(**kwargs):
        prompts.append(kwargs)
        facts = kwargs["memories"]
        if len(facts) > 1:
            return _BatchLLMResult(failed=True)
        fid = str(facts[0]["id"])
        return _BatchLLMResult(
            updates=[_UpdateAction(observation_id="obs-a", text=f"Updated by {fid}", source_fact_ids=[fid])]
        )

    async def write(**kwargs):
        writes.append(kwargs["observations"][0].model_copy(deep=True))
        return "[0.0]"

    facts = [{"id": "fact-a", "text": "a"}, {"id": "fact-b", "text": "b"}]
    with (
        patch(
            "evolving_profile_api.engine.consolidation.consolidator._find_related_observations",
            new=AsyncMock(return_value=recall),
        ),
        patch("evolving_profile_api.engine.consolidation.consolidator._consolidate_batch_with_llm", new=llm),
        patch("evolving_profile_api.engine.consolidation.consolidator._execute_update_action", new=write),
    ):
        for batch in (facts, facts[:1], facts[1:]):
            await _process_memory_batch(
                pool=None,
                memory_engine=None,
                llm_config=None,
                bank_id="test",
                memories=batch,
                request_context=None,
                config=_config(),
                observation_context=context,
            )
    assert writes[1].text == "Updated by fact-a"
    assert writes[1].source_fact_ids == ["origin", "fact-a"]
    assert prompts[-1]["union_source_facts"]["fact-a"].text == "a"
    assert original.text == "Original observation"
    assert original.source_fact_ids == ["origin"]


@pytest.mark.asyncio
async def test_split_sibling_cannot_resurrect_an_observation_deleted_in_same_batch():
    from contextlib import asynccontextmanager

    from evolving_profile_api.engine.consolidation.consolidator import _DeleteAction
    from evolving_profile_api.engine.response_models import MemoryFact

    observation = MemoryFact(id="obs-a", text="Superseded", fact_type="observation")
    recall = SimpleNamespace(results=[observation], source_facts={})
    context = {}
    inputs = []

    async def llm(**kwargs):
        inputs.append(kwargs)
        if len(kwargs["memories"]) > 1:
            return _BatchLLMResult(failed=True)
        return (
            _BatchLLMResult(deletes=[_DeleteAction(observation_id="obs-a")]) if len(inputs) == 2 else _BatchLLMResult()
        )

    @asynccontextmanager
    async def connection(*_args, **_kwargs):
        yield None

    facts = [{"id": "fact-a", "text": "a"}, {"id": "fact-b", "text": "b"}]
    with (
        patch(
            "evolving_profile_api.engine.consolidation.consolidator._find_related_observations",
            new=AsyncMock(return_value=recall),
        ),
        patch("evolving_profile_api.engine.consolidation.consolidator._consolidate_batch_with_llm", new=llm),
        patch("evolving_profile_api.engine.consolidation.consolidator._execute_delete_action", new=AsyncMock()),
        patch("evolving_profile_api.engine.consolidation.consolidator.acquire_with_retry", new=connection),
    ):
        for batch in (facts, facts[:1], facts[1:]):
            await _process_memory_batch(
                pool=None,
                memory_engine=None,
                llm_config=None,
                bank_id="test",
                memories=batch,
                request_context=None,
                config=_config(),
                observation_context=context,
            )
    assert inputs[-1]["union_observations"] == []


@pytest.mark.asyncio
async def test_split_sibling_dedup_fold_advances_survivor_and_removes_old_target():
    from unittest.mock import MagicMock

    from evolving_profile_api.engine.consolidation.consolidator import _DedupOutcome, _UpdateAction
    from evolving_profile_api.engine.response_models import MemoryFact

    observations = [
        MemoryFact(id="obs-a", text="a", fact_type="observation", source_fact_ids=["source-a"]),
        MemoryFact(id="obs-b", text="b", fact_type="observation", source_fact_ids=["source-b"]),
    ]
    recall = SimpleNamespace(results=observations, source_facts={})
    context = {}
    inputs = []

    async def llm(**kwargs):
        inputs.append(kwargs)
        if len(kwargs["memories"]) > 1:
            return _BatchLLMResult(failed=True)
        return (
            _BatchLLMResult(updates=[_UpdateAction(observation_id="obs-a", text="new a", source_fact_ids=["fact-a"])])
            if len(inputs) == 2
            else _BatchLLMResult()
        )

    config = _config()
    config.consolidation_dedup_threshold = 0.9
    facts = [{"id": "fact-a", "text": "a"}, {"id": "fact-b", "text": "b"}]
    with (
        patch(
            "evolving_profile_api.engine.consolidation.consolidator._find_related_observations",
            new=AsyncMock(return_value=recall),
        ),
        patch("evolving_profile_api.engine.consolidation.consolidator._consolidate_batch_with_llm", new=llm),
        patch(
            "evolving_profile_api.engine.consolidation.consolidator._execute_update_action",
            new=AsyncMock(return_value="[0]"),
        ),
        patch(
            "evolving_profile_api.engine.consolidation.consolidator._dedup_reconcile_update",
            new=AsyncMock(return_value=_DedupOutcome(best_id="obs-b", merged_text="Merged a and b", should_merge=True)),
        ),
    ):
        for batch in (facts, facts[:1], facts[1:]):
            await _process_memory_batch(
                pool=None,
                memory_engine=MagicMock(),
                llm_config=None,
                bank_id="test",
                memories=batch,
                request_context=None,
                config=config,
                observation_context=context,
            )
    assert [obs.id for obs in inputs[-1]["union_observations"]] == ["obs-b"]
    survivor = inputs[-1]["union_observations"][0]
    assert survivor.text == "Merged a and b"
    assert set(survivor.source_fact_ids) == {"source-a", "source-b", "fact-a"}


@pytest.mark.asyncio
async def test_split_create_fold_keeps_evidence_for_later_sibling_update():
    from unittest.mock import MagicMock

    from evolving_profile_api.engine.consolidation.consolidator import _CreateAction, _DedupOutcome
    from evolving_profile_api.engine.response_models import MemoryFact

    observation = MemoryFact(id="obs-a", text="Original", fact_type="observation", source_fact_ids=["origin"])
    recall = SimpleNamespace(results=[observation], source_facts={})
    context = {}
    inputs = []

    async def llm(**kwargs):
        inputs.append(kwargs)
        if len(kwargs["memories"]) > 1:
            return _BatchLLMResult(failed=True)
        return (
            _BatchLLMResult(creates=[_CreateAction(text="new evidence", source_fact_ids=["fact-a"])])
            if len(inputs) == 2
            else _BatchLLMResult()
        )

    async def fold(*_args, **kwargs):
        if kwargs.get("merge_outcomes") is not None:
            kwargs["merge_outcomes"].append(
                _DedupOutcome(best_id="obs-a", merged_text="Original and new evidence", should_merge=True)
            )
        return "obs-a"

    config = _config()
    config.consolidation_dedup_threshold = 0.9
    facts = [{"id": "fact-a", "text": "first evidence"}, {"id": "fact-b", "text": "second evidence"}]
    with (
        patch(
            "evolving_profile_api.engine.consolidation.consolidator._find_related_observations",
            new=AsyncMock(return_value=recall),
        ),
        patch("evolving_profile_api.engine.consolidation.consolidator._consolidate_batch_with_llm", new=llm),
        patch("evolving_profile_api.engine.consolidation.consolidator._dedup_reconcile_create", new=fold),
    ):
        for batch in (facts, facts[:1], facts[1:]):
            await _process_memory_batch(
                pool=None,
                memory_engine=MagicMock(),
                llm_config=None,
                bank_id="test",
                memories=batch,
                request_context=None,
                config=config,
                observation_context=context,
            )
    cached = inputs[-1]["union_observations"][0]
    assert cached.text == "Original and new evidence"
    assert cached.source_fact_ids == ["origin", "fact-a"]
    assert inputs[-1]["union_source_facts"]["fact-a"].text == "first evidence"


@pytest.mark.asyncio
async def test_new_sibling_observation_is_visible_and_updatable_before_store_commit():
    from evolving_profile_api.engine.consolidation.consolidator import _CreateAction, _UpdateAction

    context = {}
    inputs = []
    writes = []

    async def llm(**kwargs):
        inputs.append(kwargs)
        if len(kwargs["memories"]) > 1:
            return _BatchLLMResult(failed=True)
        if len(inputs) == 2:
            return _BatchLLMResult(creates=[_CreateAction(text="First observation", source_fact_ids=["fact-a"])])
        return _BatchLLMResult(
            updates=[_UpdateAction(observation_id="new-obs", text="Both facts", source_fact_ids=["fact-b"])]
        )

    async def create(**kwargs):
        if kwargs.get("created_ids") is not None:
            kwargs["created_ids"].append("new-obs")
        return "created"

    async def update(**kwargs):
        writes.append(kwargs["observations"][0])
        return "[0]"

    facts = [{"id": "fact-a", "text": "a"}, {"id": "fact-b", "text": "b"}]
    with (
        patch(
            "evolving_profile_api.engine.consolidation.consolidator._find_related_observations",
            new=AsyncMock(return_value=SimpleNamespace(results=[], source_facts={})),
        ),
        patch("evolving_profile_api.engine.consolidation.consolidator._consolidate_batch_with_llm", new=llm),
        patch("evolving_profile_api.engine.consolidation.consolidator._execute_create_action", new=create),
        patch("evolving_profile_api.engine.consolidation.consolidator._execute_update_action", new=update),
    ):
        for batch in (facts, facts[:1], facts[1:]):
            result, _, failed = await _process_memory_batch(
                pool=None,
                memory_engine=None,
                llm_config=None,
                bank_id="test",
                memories=batch,
                request_context=None,
                config=_config(),
                observation_context=context,
            )
    assert not failed
    assert result == [{"action": "updated"}]
    assert writes[0].text == "First observation"
    assert writes[0].source_fact_ids == ["fact-a"]
    assert inputs[-1]["union_source_facts"]["fact-a"].text == "a"


@pytest.mark.asyncio
async def test_exact_duplicate_create_keeps_new_source_lineage_and_latest_text():
    from evolving_profile_api.engine.consolidation.consolidator import _CreateAction, _UpdateAction
    from evolving_profile_api.engine.response_models import MemoryFact

    observation = MemoryFact(id="obs-a", text="Old text", fact_type="observation", source_fact_ids=["origin"])
    output = _BatchLLMResult(
        updates=[_UpdateAction(observation_id="obs-a", text="Current text", source_fact_ids=["fact-a"])],
        creates=[_CreateAction(text="Current text", source_fact_ids=["fact-b"])],
    )
    writes = []

    async def update(**kwargs):
        writes.append(kwargs)
        return "[0]"

    with (
        patch(
            "evolving_profile_api.engine.consolidation.consolidator._find_related_observations",
            new=AsyncMock(return_value=SimpleNamespace(results=[observation], source_facts={})),
        ),
        patch(
            "evolving_profile_api.engine.consolidation.consolidator._consolidate_batch_with_llm",
            new=AsyncMock(return_value=output),
        ),
        patch("evolving_profile_api.engine.consolidation.consolidator._execute_update_action", new=update),
    ):
        result, _, failed = await _process_memory_batch(
            pool=None,
            memory_engine=None,
            llm_config=None,
            bank_id="test",
            memories=[{"id": "fact-a", "text": "a"}, {"id": "fact-b", "text": "b"}],
            request_context=None,
            config=_config(),
            observation_context={},
        )
    assert not failed
    assert result == [{"action": "updated"}, {"action": "updated"}]
    assert writes[-1]["source_memory_ids"] == ["fact-b"]
    assert writes[-1]["new_text"] == "Current text"
    assert writes[-1]["observations"][0].source_fact_ids == ["origin", "fact-a"]


@pytest.mark.asyncio
async def test_unseen_dedup_survivor_keeps_existing_source_references_in_sibling_context():
    from contextlib import asynccontextmanager
    from unittest.mock import MagicMock

    from evolving_profile_api.engine.consolidation.consolidator import _DedupOutcome, _UpdateAction
    from evolving_profile_api.engine.memories.base import StoredMemory
    from evolving_profile_api.engine.response_models import MemoryFact

    observation = MemoryFact(id="obs-a", text="Original a", fact_type="observation", source_fact_ids=["source-a"])
    stored = StoredMemory(unit_id="obs-b", text="Original b", fact_type="observation", source_memory_ids=["source-b"])
    store = SimpleNamespace(get_memories=AsyncMock(return_value=[stored]))
    context = {}
    inputs = []

    async def llm(**kwargs):
        inputs.append(kwargs)
        if len(kwargs["memories"]) > 1:
            return _BatchLLMResult(failed=True)
        return (
            _BatchLLMResult(updates=[_UpdateAction(observation_id="obs-a", text="new a", source_fact_ids=["fact-a"])])
            if len(inputs) == 2
            else _BatchLLMResult()
        )

    @asynccontextmanager
    async def connection(*_args, **_kwargs):
        yield None

    config = _config()
    config.consolidation_dedup_threshold = 0.9
    facts = [{"id": "fact-a", "text": "a"}, {"id": "fact-b", "text": "b"}]
    with (
        patch(
            "evolving_profile_api.engine.consolidation.consolidator._find_related_observations",
            new=AsyncMock(return_value=SimpleNamespace(results=[observation], source_facts={})),
        ),
        patch("evolving_profile_api.engine.consolidation.consolidator._consolidate_batch_with_llm", new=llm),
        patch(
            "evolving_profile_api.engine.consolidation.consolidator._execute_update_action",
            new=AsyncMock(return_value="[0]"),
        ),
        patch(
            "evolving_profile_api.engine.consolidation.consolidator._dedup_reconcile_update",
            new=AsyncMock(return_value=_DedupOutcome(best_id="obs-b", merged_text="Merged a and b", should_merge=True)),
        ),
        patch("evolving_profile_api.engine.consolidation.consolidator.acquire_with_retry", new=connection),
        patch("evolving_profile_api.engine.consolidation.consolidator.get_memories", return_value=store),
    ):
        for batch in (facts, facts[:1], facts[1:]):
            await _process_memory_batch(
                pool=None,
                memory_engine=MagicMock(),
                llm_config=None,
                bank_id="test",
                memories=batch,
                request_context=None,
                config=config,
                observation_context=context,
            )
    survivor = inputs[-1]["union_observations"][0]
    assert survivor.id == "obs-b"
    assert set(survivor.source_fact_ids) == {"source-a", "source-b", "fact-a"}
    assert survivor.text == "Merged a and b"


@pytest.mark.asyncio
@pytest.mark.memory_backend_incompatible
async def test_stale_observation_snapshot_cannot_drop_persisted_source_evidence(memory, request_context):
    import json
    import uuid

    from evolving_profile_api.engine.consolidation.consolidator import (
        _create_observation_directly,
        _execute_update_action,
    )
    from evolving_profile_api.engine.response_models import MemoryFact
    from tests.test_consolidation_failure_isolation import _insert_memory

    bank_id = f"test-integrity-lineage-{uuid.uuid4().hex[:8]}"
    await memory.get_bank_profile(bank_id=bank_id, request_context=request_context)
    try:
        async with memory._pool.acquire() as conn:
            sources = [await _insert_memory(conn, bank_id, f"Source {i}", []) for i in range(3)]
            for index, source in enumerate(sources):
                metadata = {
                    "source_role": "user" if index < 2 else "assistant",
                    "independent_user_evidence": "true" if index < 2 else "false",
                    "evidence_group_id": "same-original" if index < 2 else "assistant-original",
                }
                await conn.execute(
                    "UPDATE memory_units SET metadata=$2::jsonb WHERE id=$1", source, json.dumps(metadata)
                )
        created = await _create_observation_directly(
            pool=memory._pool,
            memory_engine=memory,
            bank_id=bank_id,
            source_memory_ids=sources[:2],
            observation_text="Already cites two sources",
        )
        obs_id = created["observation_id"]
        async with memory._pool.acquire() as conn:
            raw_metadata = await conn.fetchval("SELECT metadata FROM memory_units WHERE id=$1", uuid.UUID(str(obs_id)))
        created_metadata = json.loads(raw_metadata) if isinstance(raw_metadata, str) else (raw_metadata or {})
        assert created_metadata["independent_user_evidence"] == "false"
        assert created_metadata["independent_user_origin_count"] == "1"
        stale = MemoryFact(
            id=str(obs_id), text="Old snapshot", fact_type="observation", source_fact_ids=[str(sources[0])]
        )
        await _execute_update_action(
            pool=await memory._get_backend(),
            memory_engine=memory,
            bank_id=bank_id,
            source_memory_ids=sources[2:],
            observation_id=str(obs_id),
            new_text="All three sources",
            observations=[stale],
        )
        async with memory._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT source_memory_ids, proof_count, metadata FROM memory_units WHERE id = $1",
                uuid.UUID(str(obs_id)),
            )
        assert {str(mid) for mid in row["source_memory_ids"]} == {str(mid) for mid in sources}
        assert row["proof_count"] == 3
        metadata = json.loads(row["metadata"]) if isinstance(row["metadata"], str) else row["metadata"]
        assert metadata["source_role"] == "inferred_projection"
        assert metadata["statement_kind"] == "derived_observation"
        assert metadata["independent_user_origin_count"] == "1"
        assert metadata["source_lineage_count"] == "3"
        from evolving_profile_api.config import get_config
        from evolving_profile_api.engine.consolidation.consolidator import (
            _dedup_reconcile_create,
            _dedup_reconcile_update,
            _DedupOutcome,
            _TemporalBounds,
        )

        # Legacy twins must gain the same authority marker when a fold bypasses
        # the normal create/update writers. Source IDs remain the evidence.
        async with memory._pool.acquire() as conn:
            await conn.execute("UPDATE memory_units SET metadata='{}'::jsonb WHERE id=$1", uuid.UUID(str(obs_id)))
        with patch(
            "evolving_profile_api.engine.consolidation.consolidator._dedup_adjudicate",
            new=AsyncMock(
                return_value=_DedupOutcome(
                    best_id=str(obs_id), merged_text="Folded create", should_merge=True, best_text="All three sources"
                )
            ),
        ):
            folded = await _dedup_reconcile_create(
                await memory._get_backend(),
                memory,
                bank_id,
                get_config(),
                None,
                "same evidence",
                sources[2:],
                [],
                _TemporalBounds(),
            )
        assert folded == str(obs_id)
        async with memory._pool.acquire() as conn:
            raw = await conn.fetchval("SELECT metadata FROM memory_units WHERE id=$1", uuid.UUID(str(obs_id)))
        assert (json.loads(raw) if isinstance(raw, str) else raw)["independent_user_evidence"] == "false"
        second = await _create_observation_directly(memory._pool, memory, bank_id, sources[:1], "Second twin")
        with patch(
            "evolving_profile_api.engine.consolidation.consolidator._dedup_adjudicate",
            new=AsyncMock(
                return_value=_DedupOutcome(
                    best_id=str(obs_id), merged_text="Folded update", should_merge=True, best_text="Folded create"
                )
            ),
        ):
            await _dedup_reconcile_update(
                await memory._get_backend(),
                memory,
                bank_id,
                get_config(),
                None,
                second["observation_id"],
                "Second twin",
                "[0.1]",
                [],
            )
        async with memory._pool.acquire() as conn:
            raw = await conn.fetchval("SELECT metadata FROM memory_units WHERE id=$1", uuid.UUID(str(obs_id)))
        assert (json.loads(raw) if isinstance(raw, str) else raw)["source_lineage_count"] == "3"
    finally:
        await memory.delete_bank(bank_id, request_context=request_context)


@pytest.mark.asyncio
async def test_invalid_update_evidence_holds_deletes_before_any_write():
    from evolving_profile_api.engine.consolidation.consolidator import _DeleteAction, _UpdateAction

    # obs-b exists in the batch union, but was recalled only for fact-b.
    # An update claiming fact-a as its evidence must hold the whole result,
    # including an otherwise valid delete, before a connection is acquired.
    recalls = [
        SimpleNamespace(results=[SimpleNamespace(id="obs-a")], source_facts={}),
        SimpleNamespace(results=[SimpleNamespace(id="obs-b")], source_facts={}),
    ]
    output = _BatchLLMResult(
        deletes=[_DeleteAction(observation_id="obs-a")],
        updates=[_UpdateAction(observation_id="obs-b", text="unsupported", source_fact_ids=["fact-a"])],
    )
    with (
        patch(
            "evolving_profile_api.engine.consolidation.consolidator._find_related_observations",
            new=AsyncMock(side_effect=recalls),
        ),
        patch(
            "evolving_profile_api.engine.consolidation.consolidator._consolidate_batch_with_llm",
            new=AsyncMock(return_value=output),
        ),
    ):
        result, deleted, failed = await _process_memory_batch(
            pool=None,
            memory_engine=None,
            llm_config=None,
            bank_id="test",
            memories=[{"id": "fact-a", "text": "a"}, {"id": "fact-b", "text": "b"}],
            request_context=None,
            config=_config(),
        )
    assert failed and deleted == 0
    assert result == [{"action": "failed"}, {"action": "failed"}]


@pytest.mark.asyncio
@pytest.mark.memory_backend_incompatible
async def test_adaptive_bisection_retains_every_original_source_and_marks_only_successes(memory, request_context):
    import uuid

    from evolving_profile_api.engine.consolidation.consolidator import _CreateAction, run_consolidation_job
    from tests.test_consolidation_failure_isolation import _insert_memory, _override_config

    bank_id = f"test-integrity-split-{uuid.uuid4().hex[:8]}"
    await memory.get_bank_profile(bank_id=bank_id, request_context=request_context)
    batches = []
    try:
        async with memory._pool.acquire() as conn:
            source_ids = [await _insert_memory(conn, bank_id, f"Evidence {i}", ["test:split"]) for i in range(4)]
        held_id = str(source_ids[-1])

        async def llm(**kwargs):
            facts = kwargs["memories"]
            ids = [str(m["id"]) for m in facts]
            batches.append(ids)
            if len(ids) > 1 or ids[0] == held_id:
                return _BatchLLMResult(failed=True)
            return _BatchLLMResult(creates=[_CreateAction(text=f"Supported {facts[0]['text']}", source_fact_ids=ids)])

        with (
            _override_config(
                memory, enable_observations=True, consolidation_llm_batch_size=4, consolidation_llm_parallelism=1
            ),
            patch.object(memory, "submit_async_consolidation"),
            patch(
                "evolving_profile_api.engine.consolidation.consolidator._find_related_observations",
                new=AsyncMock(return_value=SimpleNamespace(results=[], source_facts={})),
            ),
            patch("evolving_profile_api.engine.consolidation.consolidator._consolidate_batch_with_llm", new=llm),
        ):
            result = await run_consolidation_job(memory_engine=memory, bank_id=bank_id, request_context=request_context)

        assert {ids[0] for ids in batches if len(ids) == 1} == {str(mid) for mid in source_ids}
        assert len([ids for ids in batches if len(ids) == 1]) == 4
        assert result["memories_processed"] == 4
        assert result["memories_failed"] == 1
        assert result["observations_created"] == 3
        async with memory._pool.acquire() as conn:
            sources = await conn.fetch(
                "SELECT id, consolidated_at, consolidation_failed_at FROM memory_units WHERE id = ANY($1::uuid[])",
                source_ids,
            )
            observations = await conn.fetch(
                "SELECT source_memory_ids FROM memory_units WHERE bank_id = $1 AND fact_type = 'observation'", bank_id
            )
        assert len(sources) == 4
        for source in sources:
            if str(source["id"]) == held_id:
                assert source["consolidated_at"] is None
                assert source["consolidation_failed_at"] is not None
            else:
                assert source["consolidated_at"] is not None
                assert source["consolidation_failed_at"] is None
        assert {str(mid) for obs in observations for mid in obs["source_memory_ids"]} == {
            str(mid) for mid in source_ids[:-1]
        }
    finally:
        await memory.delete_bank(bank_id, request_context=request_context)
