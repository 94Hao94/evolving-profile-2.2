"""Legacy scope shapes must preserve their exact tag set before SQL recall."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from evolving_profile_api.engine.consolidation.consolidator import (
    _BatchLLMResult,
    _process_memory_batch,
    _resolve_obs_tags_list,
    _resolve_write_scopes,
)


@pytest.mark.parametrize(
    "value",
    [
        "project:legacy",
        json.dumps("project:legacy"),
        ["project:legacy", "user:owner"],
        json.dumps(["project:legacy", "user:owner"]),
    ],
)
def test_legacy_scope_is_one_complete_tag_set_not_character_or_tag_fanout(value):
    memory = {"tags": ["unrelated"], "observation_scopes": value}
    expected = (
        [["project:legacy"]]
        if isinstance(value, str) and not value.startswith("[")
        else [["project:legacy", "user:owner"]]
    )
    assert _resolve_obs_tags_list(memory) == expected
    assert _resolve_write_scopes(memory) == [frozenset(expected[0])]


@pytest.mark.parametrize("value", ["private", json.dumps("private")])
def test_imported_private_mode_preserves_the_complete_original_tag_scope(value):
    tags = ["obsidian-scope:agent_memory", "source:obsidian-vault", "migration:sample", "privacy:private"]
    memory = {"tags": tags, "observation_scopes": value}
    assert _resolve_obs_tags_list(memory) is None
    assert _resolve_write_scopes(memory) == [frozenset(tags)]


@pytest.mark.parametrize("value", ["private", json.dumps("private")])
@pytest.mark.parametrize("tags", [None, []])
def test_private_without_source_tags_is_held_and_never_becomes_global(value, tags):
    memory = {"tags": tags, "observation_scopes": value}
    with pytest.raises(ValueError, match="private"):
        _resolve_obs_tags_list(memory)
    with pytest.raises(ValueError, match="private"):
        _resolve_write_scopes(memory)
    assert memory == {"tags": tags, "observation_scopes": value}


@pytest.mark.parametrize("value", ["each_tag", json.dumps("each_tag")])
def test_legacy_each_tag_mode_matches_per_tag_without_character_scopes(value):
    memory = {"tags": ["project:legacy", "user:owner"], "observation_scopes": value}
    assert _resolve_obs_tags_list(memory) == [["project:legacy"], ["user:owner"]]
    assert _resolve_write_scopes(memory) == [frozenset({"project:legacy"}), frozenset({"user:owner"})]


@pytest.mark.parametrize(
    "value",
    [
        ["project:a", ["user:b"]],
        [["project:a", 7]],
        {"project:a": []},
        7,
        True,
        "",
        [""],
        [[None]],
    ],
)
def test_invalid_or_mixed_scope_shape_holds_instead_of_widening_or_guessing(value):
    memory = {"tags": ["fallback-must-not-be-used"], "observation_scopes": value}
    with pytest.raises(ValueError):
        _resolve_obs_tags_list(memory)
    with pytest.raises(ValueError):
        _resolve_write_scopes(memory)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scope_value,tags,expected",
    [
        (json.dumps("project:legacy"), ["unrelated"], [["project:legacy"]]),
        (
            json.dumps("private"),
            ["obsidian-scope:agent_memory", "source:obsidian-vault", "migration:sample", "privacy:private"],
            [["obsidian-scope:agent_memory", "source:obsidian-vault", "migration:sample", "privacy:private"]],
        ),
    ],
)
async def test_dispatch_scope_reaches_recall_as_full_array_before_sql(scope_value, tags, expected):
    memory = {
        "id": "fact-a",
        "text": "synthetic evidence",
        "tags": tags,
        "observation_scopes": scope_value,
    }
    config = SimpleNamespace(observation_scope_limits=[], max_observations_per_scope=-1)
    received = []

    async def recall(**kwargs):
        received.append(kwargs["tags"])
        return SimpleNamespace(results=[], source_facts={})

    with (
        patch("evolving_profile_api.engine.consolidation.consolidator._find_related_observations", new=recall),
        patch(
            "evolving_profile_api.engine.consolidation.consolidator._consolidate_batch_with_llm",
            new=AsyncMock(return_value=_BatchLLMResult(failed=True)),
        ),
    ):
        scopes = _resolve_obs_tags_list(memory)
        for scope in scopes if scopes is not None else [None]:
            await _process_memory_batch(
                pool=None,
                memory_engine=None,
                llm_config=None,
                bank_id="test",
                memories=[memory],
                request_context=None,
                config=config,
                obs_tags_override=scope,
            )
    assert received == expected
