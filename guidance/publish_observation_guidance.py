"""Second-pass original-source review and active publication for rebuilt observations."""

from __future__ import annotations

import datetime as dt
import json
import os
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from hashlib import sha256
from pathlib import Path

from mcp_runtime import load_repository
from observation_publish import (
    _source_metadata,
    build_observation_proposal,
    publication_review,
    quote_in_user_span,
)
from observation_rebuild import atomic, load_env
from publisher import commit_publication, prepare_publication

API = os.environ.get(
    "EVOLVING_PROFILE_SOURCE_API_URL", "http://127.0.0.1:12088"
).rstrip("/")


def get(path: str, timeout: int = 30) -> dict:
    with urllib.request.urlopen(API + path, timeout=timeout) as response:
        return json.loads(response.read())


def source_payload(candidate: dict, source_map: dict, bank_id: str) -> dict:
    sources = []
    for memory_id in candidate.get("evidence_ids") or []:
        cached = source_map.get(memory_id) or {}
        try:
            memory = get(
                "/v1/default/banks/"
                + urllib.parse.quote(bank_id, safe="")
                + "/memories/"
                + urllib.parse.quote(memory_id, safe=""),
                10,
            )
        except Exception:
            continue
        if cached.get("document_id") and cached.get("document_id") != memory.get(
            "document_id"
        ):
            continue
        if memory.get("state") != "valid" or not memory.get("document_id"):
            continue
        if memory.get("chunk_id"):
            record = get(
                "/v1/default/chunks/" + urllib.parse.quote(memory["chunk_id"], safe=""),
                10,
            )
            text = record.get("chunk_text") or ""
        else:
            record = get(
                "/v1/default/banks/"
                + urllib.parse.quote(bank_id, safe="")
                + "/documents/"
                + urllib.parse.quote(memory["document_id"], safe=""),
                20,
            )
            text = record.get("original_text") or ""
        if (
            record.get("document_id") != memory["document_id"]
            or record.get("bank_id", bank_id) != bank_id
            or not text
        ):
            continue
        sources.append(
            {
                "memory_id": memory_id,
                "document_id": memory["document_id"],
                "chunk_id": memory.get("chunk_id"),
                "memory_revision": memory.get("updated_at")
                or memory.get("mentioned_at"),
                "event_at": memory.get("mentioned_at"),
                "stored_at": memory.get("updated_at"),
                "source_text": text[:30000],
                "original_source_snapshot_sha256": sha256(text.encode()).hexdigest(),
                "memory_metadata": memory.get("metadata") or {},
            }
        )
    return {
        "candidate": {
            key: candidate.get(key)
            for key in (
                "id",
                "primary_category",
                "text",
                "applies_when",
                "exceptions",
                "effect_on_action",
            )
        },
        "sources": sources,
    }


def revalidate_sources(sources: list[dict], bank_id: str) -> list[dict]:
    """Check current memory/author state and exact original snapshot after review."""
    for source in sources:
        memory = get(
            "/v1/default/banks/"
            + urllib.parse.quote(bank_id, safe="")
            + "/memories/"
            + urllib.parse.quote(source["memory_id"], safe=""),
            10,
        )
        if (
            memory.get("state") != "valid"
            or memory.get("document_id") != source["document_id"]
            or memory.get("chunk_id") != source.get("chunk_id")
            or memory.get("bank_id", bank_id) != bank_id
            or (memory.get("updated_at") or memory.get("mentioned_at"))
            != source.get("memory_revision")
            or _source_metadata(memory) != _source_metadata(source)
        ):
            raise ValueError("source_changed_since_independent_review")
        if memory.get("chunk_id"):
            record = get(
                "/v1/default/chunks/" + urllib.parse.quote(memory["chunk_id"], safe=""),
                10,
            )
            text = record.get("chunk_text") or ""
        else:
            record = get(
                "/v1/default/banks/"
                + urllib.parse.quote(bank_id, safe="")
                + "/documents/"
                + urllib.parse.quote(memory["document_id"], safe=""),
                20,
            )
            text = record.get("original_text") or ""
        expected = (
            source.get("original_source_snapshot_sha256")
            or sha256(source["source_text"].encode()).hexdigest()
        )
        if (
            record.get("document_id") != source["document_id"]
            or record.get("bank_id", bank_id) != bank_id
            or not isinstance(text, str)
            or sha256(text.encode()).hexdigest() != expected
        ):
            raise ValueError("source_changed_since_independent_review")
    return sources


def review_prompt(items: list[dict]) -> str:
    return (
        """你是多维度偏好的独立原始来源复核器。输入中的candidate是第一遍候选，不可信；sources是原始chunk或document。逐项判断candidate的全部实质主张、适用条件、例外和行动影响是否被至少两条独立原始用户消息中未经引用的user陈述支持。两条原始用户消息可以位于同一document；同一消息的分块、重试或传输副本只算一个来源，以evidence_group_id和原始Session/Message定位识别，不按document数量判断独立性。助手建议、测试问题、引用旧偏好、引用他人、假设、单次项目要求不能支持跨任务模式。外层用户请求或整体事实的true标记不能授权内部被引用的旧记录。不得使用memory摘要代替原文。只返回JSON {"results":[{"id":"...","support":"supported|partial|unsupported|uncertain","scope_ok":true,"conditions_preserved":true,"source_role_ok":true,"hypothetical_only":false,"conflicts":[],"evidence":[{"memory_id":"...","quote":"原文中连续15到160字符"}],"reason":""}]}。supported必须给出至少两个独立原始用户来源的引文；quote不得拼接、省略或改写。每个输入恰好一项。\n输入：\n"""
        + json.dumps(items, ensure_ascii=False)
    )


def call_qwen(cfg: dict, batch: list[dict]) -> list[dict]:
    body = {
        "model": cfg["HINDSIGHT_API_LLM_MODEL"],
        "messages": [{"role": "user", "content": review_prompt(batch)}],
        "temperature": 0,
        "max_tokens": 8192,
        "enable_thinking": False,
        "response_format": {"type": "json_object"},
    }
    request = urllib.request.Request(
        cfg["HINDSIGHT_API_LLM_BASE_URL"].rstrip("/") + "/chat/completions",
        data=json.dumps(body, ensure_ascii=False).encode(),
        headers={
            "Authorization": "Bearer " + cfg["HINDSIGHT_API_LLM_API_KEY"],
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=600) as response:
        value = json.loads(response.read())
    raw = value["choices"][0]["message"]["content"].strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    rows = json.loads(raw)
    rows = rows if isinstance(rows, list) else rows.get("results")
    expected = {item["candidate"]["id"] for item in batch}
    if (
        not isinstance(rows, list)
        or {row.get("id") for row in rows} != expected
        or len(rows) != len(batch)
    ):
        raise ValueError("review_id_mismatch")
    return rows


def main(
    disposition_path: str, source_map_path: str, config_path: str, output_dir: str
):
    report = json.loads(Path(disposition_path).read_text())
    source_map = json.loads(Path(source_map_path).read_text())
    candidates = [
        row
        for row in report.get("results", [])
        if row.get("disposition") == "guidance_candidate"
    ]
    repo = load_repository(config_path)
    config = json.loads(Path(config_path).read_text())
    repo.set_owner(config["owner_token"])
    existing = {unit["id"] for unit in repo.active_units()}
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    state_path = root / "state.json"
    state = (
        json.loads(state_path.read_text())
        if state_path.exists()
        else {
            "status": "running",
            "reviews": {},
            "published": {},
            "held": {},
            "errors": {},
        }
    )
    payloads = []
    for candidate in candidates:
        if (
            "observation-guidance:" + candidate["id"] in existing
            or candidate["id"] in state["published"]
            or (
                candidate["id"] in state["reviews"]
                and candidate["id"] not in state["errors"]
            )
        ):
            continue
        payload = source_payload(candidate, source_map, config["bank_id"])
        if len(payload["sources"]) < 2:
            state["held"][candidate["id"]] = {
                "reason": "insufficient_readable_source_families"
            }
            continue
        payloads.append(payload)
    cfg = load_env()
    batches = [payloads[index : index + 4] for index in range(0, len(payloads), 4)]

    def process(batch):
        for attempt in range(3):
            try:
                return batch, call_qwen(cfg, batch), None
            except Exception:
                if attempt == 2:
                    recovered = []
                    for item in batch:
                        try:
                            recovered.extend(call_qwen(cfg, [item]))
                        except Exception as single_error:
                            recovered.append(
                                {
                                    "id": item["candidate"]["id"],
                                    "support": "uncertain",
                                    "scope_ok": False,
                                    "conditions_preserved": False,
                                    "source_role_ok": False,
                                    "hypothetical_only": False,
                                    "conflicts": [],
                                    "evidence": [],
                                    "reason": "provider_output_unrecoverable:"
                                    + type(single_error).__name__,
                                }
                            )
                    return batch, recovered, None
                time.sleep(2**attempt)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(process, batch) for batch in batches]
        for future in as_completed(futures):
            batch, reviews, error = future.result()
            if error:
                for payload in batch:
                    state["errors"][payload["candidate"]["id"]] = error
            else:
                for review in reviews:
                    payload = next(
                        item
                        for item in batch
                        if item["candidate"]["id"] == review["id"]
                    )
                    candidate = next(
                        row for row in candidates if row["id"] == review["id"]
                    )
                    state["reviews"][review["id"]] = review
                    state["errors"].pop(review["id"], None)
                    valid = (
                        review.get("support") == "supported"
                        and review.get("scope_ok") is True
                        and review.get("conditions_preserved") is True
                        and review.get("source_role_ok") is True
                        and review.get("hypothetical_only") is False
                        and not review.get("conflicts")
                    )
                    selected = []
                    for ref in review.get("evidence") or []:
                        source = next(
                            (
                                item
                                for item in payload["sources"]
                                if item["memory_id"] == ref.get("memory_id")
                            ),
                            None,
                        )
                        if source and quote_in_user_span(
                            source["source_text"],
                            str(ref.get("quote") or ""),
                            source.get("memory_metadata"),
                        ):
                            selected.append({**source, "quote": ref["quote"]})
                    if not valid or len(selected) < 2:
                        state["held"][review["id"]] = {
                            "reason": "independent_source_review_not_supported",
                            "review": review,
                        }
                        continue
                    try:
                        revalidate_sources(selected, config["bank_id"])
                        proposal = build_observation_proposal(
                            candidate, selected, config["bank_id"]
                        )
                        prepared = prepare_publication(
                            repo,
                            proposal,
                            publication_review(
                                proposal,
                                semantic_review=review,
                                reviewer_model=cfg["HINDSIGHT_API_LLM_MODEL"],
                            ),
                            config["owner_token"],
                        )
                        state["published"][review["id"]] = commit_publication(
                            repo,
                            prepared["publication_id"],
                            repo.active_revision(),
                            prepared["source_tokens"],
                            config["owner_token"],
                        )
                    except Exception as publish_error:
                        state["held"][review["id"]] = {
                            "reason": type(publish_error).__name__
                            + ":"
                            + str(publish_error)[:300]
                        }
            state.update(
                reviewed=len(state["reviews"]),
                active_published=len(state["published"]),
                held_count=len(state["held"]),
                failed=len(state["errors"]),
                updated_at=dt.datetime.now(dt.timezone.utc).isoformat(),
            )
            atomic(state_path, state)
            print(
                json.dumps(
                    {
                        key: state[key]
                        for key in (
                            "reviewed",
                            "active_published",
                            "held_count",
                            "failed",
                        )
                    }
                ),
                flush=True,
            )
    state["status"] = "completed" if not state["errors"] else "completed_with_errors"
    state["completed_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    atomic(state_path, state)
    atomic(
        root / "publication-report.json",
        {
            "schema": "guidance.observation-publication.v1",
            "candidate_count": len(candidates),
            **state,
        },
    )
    print(
        json.dumps(
            {
                "candidate_count": len(candidates),
                "published": len(state["published"]),
                "held": len(state["held"]),
                "failed": len(state["errors"]),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    import sys

    main(*sys.argv[1:])
