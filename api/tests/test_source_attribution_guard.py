import json
from datetime import datetime, timezone

import pytest

from tests.test_fact_extraction_retry import _make_config, _make_llm_config
from evolving_profile_api.engine.retain.fact_extraction import _extract_facts_from_chunk


async def extract(rows, facts):
    return await _extract_facts_from_chunk(
        json.dumps(rows, ensure_ascii=False),
        0,
        1,
        datetime(2026, 10, 8, tzinfo=timezone.utc),
        "Codex human/assistant transcript",
        _make_llm_config({"facts": facts}),
        _make_config(llm_max_retries=0),
        metadata={"source_attribution_required": "role_quote_v1"},
    )


@pytest.mark.asyncio
async def test_assistant_proposal_cannot_become_user_confirmed_rule():
    proposal = "建议把保存、重启回读和真实页面验收作为长期规则。"
    facts, _ = await extract(
        [
            {"role": "user", "content": "复盘网页配置页故障经验。"},
            {"role": "assistant", "content": proposal, "source_record": {"session_id": "case", "line": 12}},
        ],
        [
            {
                "what": "用户确立长期验收规则：保存后重启回读。",
                "fact_type": "world",
                "source_quote": proposal,
                "source_role": "assistant",
                "source_message_index": 1,
            }
        ],
    )
    assert len(facts) == 1
    assert facts[0].fact_type == "experience"
    assert "用户确立" not in facts[0].fact
    assert proposal in facts[0].fact
    assert facts[0].metadata["statement_kind"] == "assistant_proposal"
    assert facts[0].metadata["independent_user_evidence"] == "false"
    assert json.loads(facts[0].metadata["source_record"])["line"] == 12


@pytest.mark.asyncio
async def test_role_lie_and_nonexistent_source_span_fail_closed():
    rows = [{"role": "user", "content": "请解释这个方案。"}, {"role": "assistant", "content": "我建议先保存再回读。"}]
    for quote, role, index in [("我建议先保存再回读。", "user", 1), ("用户说必须保存。", "user", 0)]:
        with pytest.raises(RuntimeError, match="source"):
            await extract(
                rows,
                [
                    {
                        "what": "用户明确保存规则。",
                        "fact_type": "world",
                        "source_quote": quote,
                        "source_role": role,
                        "source_message_index": index,
                    }
                ],
            )


@pytest.mark.asyncio
async def test_direct_user_statement_keeps_actual_user_witness():
    text = "以后提交前必须让我检查最终文件。"
    facts, _ = await extract(
        [{"role": "user", "content": text}],
        [{"what": text, "fact_type": "world", "source_quote": text, "source_role": "user", "source_message_index": 0}],
    )
    assert facts[0].fact_type == "world"
    assert facts[0].metadata["source_role"] == "user"
    assert facts[0].metadata["source_quote"] == text
    assert facts[0].metadata["independent_user_evidence"] == "true"


@pytest.mark.asyncio
async def test_quoted_old_memory_is_not_a_new_independent_user_vote():
    text = "旧记忆原文是“提交前必须让我检查最终文件”，请解释。"
    quote = "提交前必须让我检查最终文件"
    facts, _ = await extract(
        [{"role": "user", "content": text}],
        [
            {
                "what": "用户要求提交前必须检查最终文件。",
                "fact_type": "world",
                "source_quote": quote,
                "source_role": "user",
                "source_message_index": 0,
            }
        ],
    )
    assert facts[0].metadata["independent_user_evidence"] == "false"
    assert facts[0].metadata["statement_kind"] == "source_repetition"
    assert "用户要求" not in facts[0].fact


@pytest.mark.asyncio
async def test_missing_witness_cannot_silently_store_world_claim():
    with pytest.raises(RuntimeError, match="source"):
        await extract(
            [{"role": "user", "content": "请分析。"}, {"role": "assistant", "content": "建议先验证。"}],
            [{"what": "用户明确先验证。", "fact_type": "world"}],
        )


@pytest.mark.asyncio
async def test_source_witness_survives_pipeline_fact_conversion():
    from evolving_profile_api.engine.retain.fact_extraction import extract_facts_from_contents
    from evolving_profile_api.engine.retain.types import RetainContent

    text = "我建议使用保存、回读、页面验收这三个步骤。"
    config = _make_config(llm_max_retries=0)
    llm = _make_llm_config(
        {
            "facts": [
                {
                    "what": "用户制定三个步骤。",
                    "fact_type": "world",
                    "source_quote": text,
                    "source_role": "assistant",
                    "source_message_index": 0,
                }
            ]
        }
    )
    facts, _, _ = await extract_facts_from_contents(
        [
            RetainContent(
                content=json.dumps([{"role": "assistant", "content": text}]),
                metadata={"source_attribution_required": "role_quote_v1"},
            )
        ],
        llm,
        "Codex",
        config,
    )
    assert facts[0].metadata["statement_kind"] == "assistant_proposal"
    assert facts[0].metadata["independent_user_evidence"] == "false"


def test_chunks_mode_keeps_assistant_authority_instead_of_world_rule():
    import dataclasses
    from evolving_profile_api.engine.retain.fact_extraction import _extract_facts_chunks
    from evolving_profile_api.engine.retain.types import RetainContent

    rows = [{"role": "user", "content": "请复盘。"}, {"role": "assistant", "content": "我建议先回读。"}]
    facts, _, _ = _extract_facts_chunks(
        [RetainContent(content=json.dumps(rows), metadata={"source_attribution_required": "role_quote_v1"})],
        dataclasses.replace(_make_config(), retain_extraction_mode="chunks"),
    )
    assert [f.fact_type for f in facts] == ["world", "experience"]
    assert facts[1].metadata["independent_user_evidence"] == "false"


def test_verbatim_does_not_merge_assistant_and_user_witnesses():
    from evolving_profile_api.engine.retain.fact_extraction import _collapse_to_verbatim
    from evolving_profile_api.engine.retain.types import ExtractedFact, ChunkMetadata

    facts = [
        ExtractedFact(fact_text="用户原话", fact_type="world", metadata={"source_role": "user"}),
        ExtractedFact(
            fact_text="Assistant statement: 建议", fact_type="experience", metadata={"source_role": "assistant"}
        ),
    ]
    actual = _collapse_to_verbatim(facts, [ChunkMetadata("混合原文", 2, 0, 0)])
    assert [f.fact_text for f in actual] == ["用户原话", "Assistant statement: 建议"]


def test_transport_copies_of_one_original_source_share_evidence_group():
    from evolving_profile_api.engine.retain.source_attribution import verify_attribution

    quote = "以后提交前让我确认。"
    groups = []
    for transport in ("copy-a", "copy-b"):
        row = {
            "role": "user",
            "content": quote,
            "source_record": {
                "transcript_path": "original.jsonl",
                "byte_offset": 42,
                "raw_line_sha256": "original-line",
                "session_id": "original-session",
                "queue_item_id": transport,
            },
        }
        groups.append(
            verify_attribution(
                json.dumps([row]),
                {"source_message_index": 0, "source_role": "user", "source_quote": quote},
                {"source_attribution_required": "role_quote_v1"},
            )[2]["evidence_group_id"]
        )
    assert groups[0] == groups[1]


@pytest.mark.asyncio
async def test_forbidden_turn_envelope_is_rejected_before_extraction():
    rows = [{"role": "user", "content": "不要写入记忆。", "write_policy": {"version": 1, "knowledge_allowed": False}}]
    with pytest.raises(RuntimeError, match="knowledge_write"):
        await _extract_facts_from_chunk(
            json.dumps(rows),
            0,
            1,
            None,
            "Codex",
            _make_llm_config({"facts": []}),
            _make_config(llm_max_retries=0),
            metadata={"source_attribution_required": "role_quote_v1", "retention_write_policy": "per_turn_v1"},
        )


@pytest.mark.asyncio
async def test_legacy_host_payload_without_turn_permission_requires_recovery():
    with pytest.raises(RuntimeError, match="knowledge_write"):
        await _extract_facts_from_chunk(
            json.dumps([{"role": "user", "content": "不要修改记忆。"}]),
            0,
            1,
            None,
            "Codex",
            _make_llm_config({"facts": []}),
            _make_config(llm_max_retries=0),
            metadata={"source": "codex-hook-token-batch"},
        )
