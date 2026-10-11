import dataclasses
import hashlib
import json

import pytest

from evolving_profile_api.engine.retain.fact_extraction import (
    _split_chunk_for_output_retry,
    chunk_text,
    extract_facts_from_contents,
)
from evolving_profile_api.engine.retain.source_attribution import _text, validate_write_policy, verify_attribution
from evolving_profile_api.engine.retain.types import RetainContent
from tests.test_fact_extraction_retry import _make_config, _make_llm_config


META = {
    "source": "codex-hook-token-batch",
    "source_attribution_required": "role_quote_v1",
    "retention_write_policy": "per_turn_v1",
}


def source(role, text, message_id, allowed=True, blocks=False):
    return {
        "role": role,
        "content": [{"type": "text", "text": text}] if blocks else text,
        "source_record": {
            "session_id": "native-session",
            "message_id": message_id,
            "raw_line_sha256": hashlib.sha256((role + text).encode()).hexdigest(),
        },
        "write_policy": {"version": 1, "knowledge_allowed": allowed},
    }


def witness(chunk, index, quote):
    row = json.loads(chunk)[index]
    return verify_attribution(
        chunk, {"source_role": row["role"], "source_message_index": index, "source_quote": quote}, META
    )


def test_17546_character_native_message_has_valid_bounded_idempotent_envelopes():
    text = ("正文内容\\引用\n" * 2100) + "最后一段"
    text = text[:17546]
    chunks = chunk_text(json.dumps([source("user", text, "user-1", blocks=True)], ensure_ascii=False), 3000)
    assert len(chunks) > 5
    originals = []
    groups = []
    cursor = 0
    for chunk in chunks:
        assert len(chunk) <= 3000
        assert chunk_text(chunk, 3000) == [chunk]
        validate_write_policy(chunk, META)
        rows = json.loads(chunk)
        assert len(rows) == 1 and rows[0]["role"] == "user"
        fragment = _text(rows[0])
        originals.append(fragment)
        _, _, metadata = witness(chunk, 0, fragment)
        assert int(metadata["source_start"]) == cursor
        assert int(metadata["source_end"]) == cursor + len(fragment)
        assert metadata["original_source_content_sha256"] == hashlib.sha256(text.encode()).hexdigest()
        assert metadata["independent_user_evidence"] == "true"
        groups.append(metadata["evidence_group_id"])
        cursor += len(fragment)
    assert "".join(originals) == text
    assert len(set(groups)) == 1


def test_source_wrapper_counts_toward_the_default_chunk_budget():
    row = source("user", "x", "border-source")
    overhead = len(json.dumps(row, ensure_ascii=False)) - 1
    row["content"] = "x" * (2999 - overhead)
    chunks = chunk_text(json.dumps([row], ensure_ascii=False), 3000)
    assert all(len(chunk) <= 3000 for chunk in chunks)


@pytest.mark.asyncio
async def test_regular_retain_3000_chunks_keep_two_authors_and_original_quote_offset():
    prefix = "前置说明。" * 1900
    user = prefix + "唯一的用户偏好原话。" + "后续背景。" * 600
    assistant = "我建议先回读。" + "执行说明。" * 1600
    rows = [source("user", user, "user-1", blocks=True), source("assistant", assistant, "assistant-1")]
    llm = _make_llm_config({"facts": []})
    calls = []
    usage = (await llm.call())[-1]

    async def respond(**kwargs):
        chunk = kwargs["messages"][-1]["content"].split("\nText:\n", 1)[1]
        calls.append(chunk)
        facts = []
        for index, row in enumerate(json.loads(chunk)):
            text = _text(row)
            quote = "唯一的用户偏好原话。" if "唯一的用户偏好原话。" in text else text
            facts.append(
                {
                    "what": "模型错误地说用户确立长期规则。",
                    "fact_type": "world",
                    "source_quote": quote,
                    "source_role": row["role"],
                    "source_message_index": index,
                }
            )
        return {"facts": facts}, usage

    llm.call.side_effect = respond
    config = dataclasses.replace(
        _make_config(llm_max_retries=0), retain_chunk_size=3000, retain_structured_chunk_size=3000
    )
    facts, chunks, _ = await extract_facts_from_contents(
        [RetainContent(content=json.dumps(rows, ensure_ascii=False), metadata=META)], llm, "Codex", config
    )
    assert len(calls) == len(chunks) > 6
    userfacts = [f for f in facts if f.metadata["source_role"] == "user"]
    assistantfacts = [f for f in facts if f.metadata["source_role"] == "assistant"]
    target = next(f for f in userfacts if f.fact_text == "唯一的用户偏好原话。")
    assert int(target.metadata["source_start"]) == len(prefix)
    assert target.metadata["independent_user_evidence"] == "true"
    assert all(
        f.fact_type == "experience" and f.metadata["independent_user_evidence"] == "false" for f in assistantfacts
    )
    assert len({f.metadata["evidence_group_id"] for f in userfacts}) == 1
    assert len({f.metadata["evidence_group_id"] for f in assistantfacts}) == 1
    assert userfacts[0].metadata["evidence_group_id"] != assistantfacts[0].metadata["evidence_group_id"]


def test_quoted_old_source_crossing_fragment_boundary_never_becomes_user_testimony():
    text = "引用旧记忆：“" + "历史上下文。" * 1800 + "唯一引用规则。" + "尾部。" * 300 + "”这里只解释。"
    chunks = chunk_text(json.dumps([source("user", text, "quoted-source")], ensure_ascii=False), 3000)
    target = next(c for c in chunks if "唯一引用规则。" in c)
    assert "引用旧记忆" not in _text(json.loads(target)[0])
    _, kind, metadata = witness(target, 0, "唯一引用规则。")
    assert kind == "experience"
    assert metadata["statement_kind"] == "source_repetition"
    assert metadata["independent_user_evidence"] == "false"
    assert int(metadata["source_start"]) == text.index("唯一引用规则。")


@pytest.mark.asyncio
async def test_long_private_turn_cannot_be_authorized_by_neighboring_allowed_author():
    rows = [
        source("user", "不要写入记忆。" + "私人内容。" * 4000, "private-user", allowed=False),
        source("assistant", "建议保存。" * 1000, "assistant"),
    ]
    config = dataclasses.replace(
        _make_config(llm_max_retries=0), retain_chunk_size=3000, retain_structured_chunk_size=3000
    )
    with pytest.raises(RuntimeError, match="knowledge_write"):
        await extract_facts_from_contents(
            [RetainContent(content=json.dumps(rows, ensure_ascii=False), metadata=META)],
            _make_llm_config({"facts": []}),
            "Codex",
            config,
        )


def test_output_retry_preserves_original_span_and_single_witness_group():
    text = "原始内容。" * 300
    original = json.dumps([source("user", text, "retry-source")], ensure_ascii=False)
    first, second = _split_chunk_for_output_retry(original)
    parts = [_text(json.loads(c)[0]) for c in (first, second)]
    assert "".join(parts) == text
    a = witness(first, 0, parts[0])[2]
    b = witness(second, 0, parts[1])[2]
    assert int(a["source_start"]) == 0
    assert int(b["source_start"]) == len(parts[0])
    assert a["evidence_group_id"] == b["evidence_group_id"]


def test_missing_original_fragment_witness_cannot_claim_confirmed_user_authority():
    row = source("user", "截断的用户片段。", "unknown-source")
    row["source_record"]["original_message_start"] = 8000
    with pytest.raises(ValueError, match="original"):
        witness(json.dumps([row], ensure_ascii=False), 0, row["content"])


def test_partial_original_witness_requires_original_quote_scope():
    row = source("user", "截断的用户片段。", "unknown-quote-scope")
    row["source_record"].update(
        original_message_start=8000,
        original_message_end=8000 + len(row["content"]),
        original_message_length=9000,
        original_message_content_sha256="a" * 64,
    )
    with pytest.raises(ValueError, match="original"):
        witness(json.dumps([row], ensure_ascii=False), 0, row["content"])


@pytest.mark.parametrize("mutation", ["role", "text", "offset", "group"])
def test_mutated_source_fragment_cannot_claim_confirmed_user_authority(mutation):
    chunks = chunk_text(json.dumps([source("user", "原话。" * 2000, "immutable-source")], ensure_ascii=False), 3000)
    row = json.loads(chunks[1])[0]
    if mutation == "role":
        row["role"] = "assistant"
    if mutation == "text":
        row["content"] = "篡改片段"
    if mutation == "offset":
        row["source_fragment"]["start"] += 7
        row["source_fragment"]["end"] += 7
    if mutation == "group":
        row["source_fragment"]["evidence_group_id"] = "invented-new-vote"
    with pytest.raises(ValueError, match="original"):
        witness(json.dumps([row], ensure_ascii=False), 0, row["content"])
