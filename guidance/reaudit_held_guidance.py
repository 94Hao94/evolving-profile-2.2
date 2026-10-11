"""Re-audit held observation candidates without weakening source provenance."""

from __future__ import annotations

import datetime as dt
import json
import re
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from mcp_runtime import load_repository
from observation_publish import (
    _body,
    publication_review,
    quote_in_user_span,
    verified_evidence_ref,
)
from observation_rebuild import atomic, load_env
from publish_observation_guidance import revalidate_sources, source_payload
from publisher import commit_publication, prepare_publication

ROLE_BLOCK = re.compile(r"\[role:\s*user\]\s*(.*?)\s*\[user:end\]", re.S | re.I)


def evidence_threshold(nature: str, evidence: list[dict]) -> dict:
    origins = set()
    for item in evidence:
        try:
            _, origin = verified_evidence_ref(
                item, "threshold-only", "reaudit-threshold"
            )
            origins.add(origin)
        except (ValueError, KeyError, TypeError):
            return {"ok": False, "families": len(origins)}
    families = len(origins)
    required = 1 if nature in {"explicit_requirement", "declared_preference"} else 2
    return {"ok": families >= required, "families": families}


def build_reaudit_proposal(
    classification: dict, evidence: list[dict], bank_id: str, nature: str
) -> dict:
    threshold = evidence_threshold(nature, evidence)
    if not threshold["ok"]:
        raise ValueError("insufficient_source_families_for_nature")
    refs = []
    for item in evidence:
        ref, _ = verified_evidence_ref(
            item,
            bank_id,
            "guidance-reaudit:" + classification["id"] + ":" + item["memory_id"],
        )
        ref["statement_kind"] = (
            "request" if nature == "explicit_requirement" else "assertion"
        )
        refs.append(ref)
    families = sorted({ref["evidence_group_id"] for ref in refs})
    return {
        "proposal_id": "observation-guidance:" + classification["id"],
        "operation": "create",
        "target_id": "observation-guidance:" + classification["id"],
        "base_revision": None,
        "nature": nature,
        "primary_category": classification["primary_category"],
        "related_categories": classification.get("related_categories") or [],
        "text": classification["text"],
        "applies_when": classification.get("applies_when") or [],
        "exceptions": classification.get("exceptions") or [],
        "effect_on_action": classification.get("effect_on_action")
        or "在匹配范围内调整行动",
        "scope": {
            "user_id": "liuzhongyang",
            "agent_roles": [],
            "project_ids": [],
            "task_ids": [],
            "domains": [],
            "media": [],
        },
        "evidence_refs": refs,
        "source_family_ids": families,
        "preference_kind": classification.get("preference_kind") or "behavior_pattern",
        "polarity": classification.get("polarity") or "neutral",
        "scope_level": classification.get("scope_level") or "domain",
        "validity_kind": classification.get("validity_kind") or "context_sensitive",
        "confidence_inputs": {
            "independent_original_user_origins": len(families),
            "source_span_verified": True,
        },
        "support_count": len(families),
        "contradiction_count": 0,
        "source_turn_ids": sorted(
            {
                str(ref["source_record"]["turn_id"])
                for ref in refs
                if ref.get("source_record", {}).get("turn_id")
            }
        ),
        "supersedes": classification.get("supersedes") or [],
        "superseded_by": [],
        "cross_cutting": bool(classification.get("cross_cutting", False)),
    }


def user_spans(text: str) -> list[str]:
    try:
        messages = json.loads(text)
    except (ValueError, TypeError):
        messages = None
    if isinstance(messages, list):
        return [
            _body(message)
            for message in messages
            if isinstance(message, dict)
            and message.get("role") == "user"
            and _body(message).strip()
        ]
    return [
        match.group(1).strip()
        for match in ROLE_BLOCK.finditer(text)
        if match.group(1).strip()
    ]


def review_prompt(payloads: list[dict]) -> str:
    return """你是多维度偏好false-negative复核器。candidate不可信，sources只提供完整user角色原文。判断candidate的全部正文、适用条件、例外和行动影响是否被原文完整支持。
规则：
1. 用户直接、明确、具有可复用范围的要求可标explicit_requirement，一份独立原文即可；不得把单次项目要求扩大成跨项目规则。
2. inferred_pattern必须至少两条独立原始用户消息支持同一模式；可在同一document内，同一原始消息的分块、重试、传输副本只算一个来源。
3. 助手总结、系统规则、测试句、假设、引用他人、重复副本不能支持用户指导。
4. 只要candidate有一个实质主张未被支持就标partial或unsupported，不得删改candidate来凑通过。
只返回JSON {"results":[{"id":"...","support":"supported|partial|unsupported|uncertain","nature":"explicit_requirement|declared_preference|inferred_pattern","scope_ok":true,"conditions_preserved":true,"source_role_ok":true,"hypothetical_only":false,"conflicts":[],"evidence":[{"memory_id":"...","quote":"原文中连续15到180字符"}],"reason":""}]}。每个输入恰好一项；quote必须逐字来自给定user_span，引用旧记录的内容不能借用外层用户请求的授权。
输入：
""" + json.dumps(payloads, ensure_ascii=False)


def call_qwen(cfg: dict, payloads: list[dict]) -> list[dict]:
    body = {
        "model": cfg["HINDSIGHT_API_LLM_MODEL"],
        "messages": [{"role": "user", "content": review_prompt(payloads)}],
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
    parsed = json.loads(raw)
    rows = parsed if isinstance(parsed, list) else parsed.get("results")
    expected = {item["candidate"]["id"] for item in payloads}
    if (
        not isinstance(rows, list)
        or {row.get("id") for row in rows} != expected
        or len(rows) != len(expected)
    ):
        raise ValueError("reaudit_review_id_mismatch")
    return rows


def review_with_single_recovery(payloads: list[dict], invoke):
    """Retry an invalid batch response item-by-item before marking errors."""
    if not payloads:
        return [], {}, 0
    calls = 1
    try:
        return invoke(payloads), {}, calls
    except Exception:
        rows, errors = [], {}
        for payload in payloads:
            calls += 1
            try:
                rows.extend(invoke([payload]))
            except Exception as error:
                errors[payload["candidate"]["id"]] = (
                    type(error).__name__ + ":" + str(error)[:300]
                )
        return rows, errors, calls


def main(
    publication_path: str,
    disposition_path: str,
    source_map_path: str,
    config_path: str,
    output_dir: str,
):
    publication = json.loads(Path(publication_path).read_text())
    dispositions = json.loads(Path(disposition_path).read_text())
    source_map = json.loads(Path(source_map_path).read_text())
    config = json.loads(Path(config_path).read_text())
    candidates = {
        row["id"]: row
        for row in dispositions["results"]
        if row.get("disposition") == "guidance_candidate"
    }
    repo = load_repository(config_path)
    repo.set_owner(config["owner_token"])
    existing = {unit["id"] for unit in repo.active_units()}
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    payloads, source_cache = [], {}
    for candidate_id, held in (publication.get("held") or {}).items():
        if (
            "observation-guidance:" + candidate_id in existing
            or candidate_id not in candidates
        ):
            continue
        old_review = (
            held.get("review")
            or (publication.get("reviews") or {}).get(candidate_id)
            or {}
        )
        if (
            old_review.get("support") != "supported"
            or old_review.get("scope_ok") is not True
            or old_review.get("conditions_preserved") is not True
            or old_review.get("source_role_ok") is not True
            or old_review.get("hypothetical_only") is True
            or old_review.get("conflicts")
        ):
            continue
        full = source_payload(candidates[candidate_id], source_map, config["bank_id"])
        sources = []
        for source in full["sources"]:
            spans = user_spans(source["source_text"])
            if spans:
                sources.append(
                    {
                        "memory_id": source["memory_id"],
                        "document_id": source["document_id"],
                        "user_spans": spans,
                        "memory_metadata": source.get("memory_metadata") or {},
                    }
                )
                source_cache[(candidate_id, source["memory_id"])] = source
        if sources:
            payloads.append(
                {
                    "candidate": {
                        key: candidates[candidate_id].get(key)
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
            )
    atomic(
        root / "shortlist.json",
        {
            "schema": "guidance.reaudit-shortlist.v1",
            "count": len(payloads),
            "items": payloads,
        },
    )
    cfg = load_env()
    reviews = {}
    errors = {}
    llm_calls = 0
    batches = [payloads[index : index + 4] for index in range(0, len(payloads), 4)]

    def process(batch):
        for attempt in range(3):
            try:
                rows, errors, calls = review_with_single_recovery(
                    batch, lambda current: call_qwen(cfg, current)
                )
                return rows, errors, calls, None
            except Exception as error:
                if attempt == 2:
                    return (
                        [],
                        {
                            item["candidate"]["id"]: type(error).__name__
                            + ":"
                            + str(error)[:300]
                            for item in batch
                        },
                        1,
                        None,
                    )
                time.sleep(2**attempt)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {pool.submit(process, batch): batch for batch in batches}
        for future in as_completed(futures):
            rows, batch_errors, calls, error = future.result()
            llm_calls += calls
            errors.update(batch_errors)
            for row in rows:
                reviews[row["id"]] = row
    published, held = {}, {}
    for payload in payloads:
        candidate_id = payload["candidate"]["id"]
        review = reviews.get(candidate_id)
        if not review:
            held[candidate_id] = {"reason": errors.get(candidate_id, "missing_review")}
            continue
        nature = review.get("nature")
        valid = (
            review.get("support") == "supported"
            and nature
            in {"explicit_requirement", "declared_preference", "inferred_pattern"}
            and review.get("scope_ok") is True
            and review.get("conditions_preserved") is True
            and review.get("source_role_ok") is True
            and review.get("hypothetical_only") is False
            and review.get("conflicts") == []
        )
        selected = []
        for ref in review.get("evidence") or []:
            source = source_cache.get((candidate_id, ref.get("memory_id")))
            if source and quote_in_user_span(
                source["source_text"],
                str(ref.get("quote") or ""),
                source.get("memory_metadata"),
            ):
                selected.append({**source, "quote": ref["quote"]})
        threshold = (
            evidence_threshold(nature, selected)
            if nature
            else {"ok": False, "families": 0}
        )
        if not valid or not threshold["ok"]:
            held[candidate_id] = {
                "reason": "reaudit_not_fully_supported",
                "review": review,
                "verified_families": threshold["families"],
            }
            continue
        try:
            revalidate_sources(selected, config["bank_id"])
            proposal = build_reaudit_proposal(
                candidates[candidate_id], selected, config["bank_id"], nature
            )
            prepared = prepare_publication(
                repo,
                proposal,
                publication_review(
                    proposal,
                    reviewer_model=cfg["HINDSIGHT_API_LLM_MODEL"],
                    semantic_review=review,
                ),
                config["owner_token"],
            )
            published[candidate_id] = commit_publication(
                repo,
                prepared["publication_id"],
                repo.active_revision(),
                prepared["source_tokens"],
                config["owner_token"],
            )
        except Exception as error:
            held[candidate_id] = {
                "reason": type(error).__name__ + ":" + str(error)[:300],
                "review": review,
            }
    report = {
        "schema": "guidance.held-reaudit.v1",
        "at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "shortlisted": len(payloads),
        "reviewed": len(reviews),
        "published": published,
        "published_count": len(published),
        "held": held,
        "held_count": len(held),
        "errors": errors,
        "llm_calls": llm_calls,
        "active_total": len(repo.active_units()),
    }
    atomic(root / "reaudit-report.json", report)
    print(
        json.dumps(
            {
                key: report[key]
                for key in (
                    "shortlisted",
                    "reviewed",
                    "published_count",
                    "held_count",
                    "llm_calls",
                    "active_total",
                )
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    import sys

    main(*sys.argv[1:])
