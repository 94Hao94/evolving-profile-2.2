"""Isolated external-file retrieval. This module never reads the EP Bank."""
from __future__ import annotations

import json
import re
import zipfile
import math
import time
import urllib.error
import urllib.request
from pathlib import Path
from xml.etree import ElementTree

from runtime_settings import load_runtime_settings, route_policy
from lib.recall_relevance import apply_relevance_policy, resolve_min_relevance

_TEXT_EXTENSIONS = {".txt", ".md", ".markdown", ".csv", ".json", ".html", ".htm"}
_STOP = {"的", "了", "是", "和", "与", "在", "对", "这个", "那个", "哪些", "什么"}
_JEV_PROVIDER = json.loads((Path(__file__).resolve().parents[2] / "config/jev-provider.json").read_text(encoding="utf-8"))
JEV_BASE_URL = _JEV_PROVIDER["endpoint"]
JEV_MODEL = _JEV_PROVIDER["model"]


def _terms(value: str) -> list[str]:
    raw = re.findall(r"[a-zA-Z][a-zA-Z0-9_.-]{1,}|[0-9]+(?:\.[0-9]+)?|[\u4e00-\u9fff]{2,}", value.casefold())
    terms = []
    for term in raw:
        if term in _STOP:
            continue
        terms.append(term)
        if re.fullmatch(r"[\u4e00-\u9fff]+", term):
            terms.extend(term[index:index + 2] for index in range(len(term) - 1))
    return list(dict.fromkeys(terms))


def _read_file(path: Path) -> str:
    if path.suffix.lower() in _TEXT_EXTENSIONS:
        return path.read_text(encoding="utf-8", errors="replace")
    if path.suffix.lower() == ".docx":
        with zipfile.ZipFile(path) as archive:
            xml = archive.read("word/document.xml")
        root = ElementTree.fromstring(xml)
        return " ".join(node.text or "" for node in root.iter() if node.tag.endswith("}t"))
    if path.suffix.lower() == ".pdf":
        try:
            from pypdf import PdfReader
            return "\n".join(page.extract_text() or "" for page in PdfReader(str(path)).pages)
        except Exception:
            return ""
    return ""


def _chunks(text: str, size: int = 1400) -> list[str]:
    normalized = re.sub(r"\s+", " ", text).strip()
    return [normalized[index:index + size] for index in range(0, len(normalized), size)] if normalized else []


def _vector_scores(query: str, chunks: list[str], model_name: str) -> tuple[list[float] | None, str]:
    if not model_name:
        return None, "not_configured"
    try:
        from sentence_transformers import SentenceTransformer
        model = SentenceTransformer(model_name)
        vectors = model.encode([query, *chunks], normalize_embeddings=True)
        query_vector = vectors[0]
        scores = [sum(float(a) * float(b) for a, b in zip(query_vector, vector)) for vector in vectors[1:]]
        return scores, "sentence_transformers"
    except Exception as error:
        return None, f"unavailable:{type(error).__name__}"


def _rerank(query: str, items: list[dict], model_name: str) -> tuple[list[dict], str]:
    if not model_name or not items:
        return items, "not_configured"
    try:
        from flashrank import Ranker, RerankRequest
        passages = [{"id": item["id"], "text": item["text"]} for item in items]
        ranked = Ranker(model_name=model_name).rerank(RerankRequest(query=query, passages=passages))
        by_id = {item["id"]: item for item in items}
        result = []
        for row in ranked:
            item = dict(by_id.get(row.get("id"), {}))
            if item:
                item["rerank_score"] = float(row.get("score", 0.0))
                item["retrieval"]["reranked"] = True
                result.append(item)
        return result or items, "flashrank"
    except Exception as error:
        return items, f"unavailable:{type(error).__name__}"


def _retrieval_model(settings: dict, kind: str, profile_id: str | None) -> tuple[dict, str]:
    """Resolve the model selected in the UI, without crossing into EP data."""
    models = settings.get("retrieval_models") or {}
    model = models.get(kind) if isinstance(models.get(kind), dict) else {}
    profiles = models.get(f"{kind}_profiles")
    if profile_id and isinstance(profiles, list):
        selected = next((item for item in profiles if isinstance(item, dict) and str(item.get("profile_id")) == str(profile_id)), None)
        if selected is not None:
            model = selected
    configured_id = str(model.get("profile_id") or "")
    if profile_id and configured_id and profile_id != configured_id:
        return {}, ""
    if not model.get("enabled", True):
        return model, ""
    local_path = str(model.get("local_path") or "").strip()
    name = local_path or str(model.get("model") or "").strip()
    return model, name


def _jev_endpoint(_: str = "") -> str:
    """JEV System One is a fixed hosted endpoint; operators only provide a key."""
    return JEV_BASE_URL


def _jev_review(query: str, items: list[dict], settings: dict) -> dict:
    """Run the optional JEV post-processor on bounded external-RAG metadata.

    JEV never retrieves documents and never receives EP records.  The default
    metadata scope sends ids, paths and scores only; a configured operator may
    explicitly opt into short chunk text through ``send_scope=bounded_text``.
    """
    judge = ((settings.get("retrieval_models") or {}).get("judge") or {})
    policy = str(judge.get("mode_policy") or "off").lower()
    enabled = bool(judge.get("enabled"))
    base_url = _jev_endpoint(judge.get("base_url"))
    model = JEV_MODEL
    api_key = str(judge.get("api_key") or "").strip()
    base = {"enabled": enabled, "mode": policy, "provider": "jev", "calls": 0, "fallback": "rules"}
    if not enabled or policy == "off" or not (judge.get("scopes") or {}).get("external_rag", True):
        return {**base, "status": "disabled", "decision": "not_run"}
    if not items:
        return {**base, "status": "no_candidates", "decision": "not_run"}
    if not api_key:
        return {**base, "status": "not_configured", "decision": "fallback_rules", "reason": "api_key_missing"}
    scope = str(judge.get("send_scope") or "metadata_summary")
    candidates = []
    for item in items[:12]:
        row = {"id": item.get("id"), "path": item.get("path"), "chunk_index": item.get("chunk_index"),
               "score": item.get("score"), "vector_score": item.get("vector_score"), "rerank_score": item.get("rerank_score")}
        if scope == "bounded_text":
            row["text"] = str(item.get("text") or "")[:600]
        candidates.append(row)
    state = json.dumps({"query": query[:1200], "candidates": candidates}, ensure_ascii=False)
    questions = {}
    for index, candidate in enumerate(candidates[:7]):
        questions[f"keep_{index}"] = {
            "type": "noul",
            "instructions": "Is this external-RAG candidate relevant enough to keep for the query? Keep borderline candidates; answer false only when clearly unrelated or contradictory.",
            "criteria": {"true": "relevant or useful background", "false": "clearly unrelated or contradictory"},
        }
    questions["risk"] = {"type": "choice", "instructions": "What is the evidence risk level of this candidate set?", "criteria": {"low": "low risk", "medium": "some uncertainty", "high": "conflict or high risk"}}
    payload = {"model": model, "state": state, "questions": questions}
    request = urllib.request.Request(base_url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), method="POST",
                                     headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"})
    started = time.monotonic()
    try:
        timeout = max(0.5, min(15.0, float(judge.get("timeout_ms") or 5000) / 1000))
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8", errors="replace"))
        result = body if isinstance(body, dict) else {}
        ids = {str(item.get("id")) for item in candidates}
        answers = result.get("answers") if isinstance(result.get("answers"), dict) else {}
        keep, reject = [], []
        for index, candidate in enumerate(candidates[:7]):
            answer = answers.get(f"keep_{index}") if isinstance(answers, dict) else None
            probability = answer.get("noul") if isinstance(answer, dict) else None
            try: probability = float(probability)
            except (TypeError, ValueError): probability = None
            candidate_id = str(candidate.get("id"))
            if probability is not None and probability >= 0.35: keep.append(candidate_id)
            elif probability is not None and probability < 0.20: reject.append(candidate_id)
        decision = "reviewed"
        return {**base, "calls": 1, "status": "ok", "decision": decision, "keep_ids": keep, "reject_ids": reject,
                "risk": str(((answers.get("risk") or {}).get("choice") if isinstance(answers, dict) and isinstance(answers.get("risk"), dict) else "unknown") or "unknown"),
                "reason": "JEV System One evidence review", "usage": result.get("usage") if isinstance(result, dict) else None,
                "latency_ms": round((time.monotonic() - started) * 1000)}
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError, OSError) as error:
        return {**base, "calls": 1, "status": "unavailable", "decision": "fallback_rules", "reason": f"{type(error).__name__}",
                "latency_ms": round((time.monotonic() - started) * 1000)}


def _apply_jev_review(items: list[dict], review: dict) -> list[dict]:
    for item in items:
        item.setdefault("retrieval", {})["jev_review"] = review.get("status")
    if review.get("mode") != "enforce" or review.get("status") != "ok":
        return items
    rejected = set(review.get("reject_ids") or [])
    return [item for item in items if str(item.get("id")) not in rejected]


def _fuse(items: list[dict], rag: dict) -> list[dict]:
    """Rank discovery signals without assigning semantic relevance bands."""
    if rag.get("fusion", "rrf") == "rrf":
        scores = {item["id"]: 0.0 for item in items}
        for key in ("score", "vector_score"):
            ranked = sorted((item for item in items if item[key] > 0), key=lambda item: (-item[key], item["id"]))
            for rank, item in enumerate(ranked, 1):
                scores[item["id"]] += 1 / (60 + rank)
        for item in items:
            item["fusion_score"] = scores[item["id"]]
    else:
        for item in items:
            item["fusion_score"] = float(rag.get("lexical_weight", 0.5)) * item["score"] + float(rag.get("vector_weight", 0.5)) * item["vector_score"]
    return sorted(items, key=lambda item: (-item["fusion_score"], item["id"]))


def search_external_rag(query: str, *, limit: int = 8, min_relevance: str | None = None) -> dict:
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise ValueError("invalid_rag_limit")
    settings = load_runtime_settings()
    policy = route_policy(settings, "external_rag")
    relevance = resolve_min_relevance(settings, "external_rag", requested=min_relevance)
    not_run = {**relevance, "status": "not_run", "reason": "retrieval_not_run", "level_counts": None, "kept_count": None, "excluded_count": None}
    rag = settings.get("rag") or {}
    if not policy["rag_enabled"]:
        return {"schema": "evolving-profile.external-rag.v1", "status": "disabled_by_runtime_settings", "items": [], "source": "external_rag", "ep_accessed": False, "relevance_policy": not_run}
    root_value = str(rag.get("root_path") or "").strip()
    root = Path(root_value).expanduser()
    if not root_value or not root.exists() or not root.is_dir():
        return {"schema": "evolving-profile.external-rag.v1", "status": "root_unavailable", "root_path": str(root), "items": [], "source": "external_rag", "ep_accessed": False, "relevance_policy": not_run}
    query_terms = _terms(query)
    rows = []
    documents = []
    for path in root.rglob("*"):
        if not path.is_file() or path.name.startswith("."):
            continue
        text = _read_file(path)
        for index, chunk in enumerate(_chunks(text)):
            documents.append((path, index, chunk))
    embedding, embedding_name = _retrieval_model(settings, "embedding", rag.get("embedding_profile_id"))
    reranker, reranker_name = _retrieval_model(settings, "reranker", rag.get("reranker_profile_id"))
    vector_values, vector_backend = _vector_scores(query, [row[2] for row in documents], embedding_name) if rag.get("vector_enabled", True) else (None, "disabled")
    for position, (path, index, chunk) in enumerate(documents):
            chunk_terms = set(_terms(chunk))
            overlap = len(chunk_terms.intersection(query_terms))
            lexical = overlap / max(1, len(query_terms)) if rag.get("lexical_enabled", True) else 0.0
            vector = float(vector_values[position]) if vector_values else 0.0
            if lexical > 0 or vector > 0:
                rows.append((lexical, vector, path, index, chunk))
    items = [{"id": f"rag:{path}:{index}", "source": "external_rag", "path": str(path), "chunk_index": index,
              "score": round(lexical, 4), "text": chunk, "retrieval": {"lexical": lexical > 0, "vector": vector > 0, "reranked": False}, "vector_score": round(vector, 4)}
             for lexical, vector, path, index, chunk in rows]
    items = _fuse(items, rag)
    items, rerank_backend = _rerank(query, items, reranker_name if rag.get("rerank_enabled", True) else "")
    items, relevance_audit = apply_relevance_policy(query, items, relevance, main_query=query)
    judge = _jev_review(query, items, settings)
    before_judge = len(items)
    items = _apply_jev_review(items, judge)
    relevance_audit.update({"status": "ok", "jev_excluded_count": before_judge - len(items), "returned_count": min(limit, len(items)), "pagination_omitted_count": max(0, len(items) - limit)})
    items = items[:limit]
    return {"schema": "evolving-profile.external-rag.v1", "status": "ok", "items": items,
            "source": "external_rag", "ep_accessed": False, "query_terms": query_terms, "relevance_policy": relevance_audit,
            "retrieval": {"mode": "hybrid", "fusion": rag.get("fusion", "rrf"), "legacy_score_threshold": {"configured_value": rag.get("score_threshold"), "applied": False, "semantics": "legacy_engine_score_setting_not_semantic_relevance"}, "rerank_enabled": bool(rag.get("rerank_enabled", True)), "rerank_backend": rerank_backend, "vector_enabled": bool(rag.get("vector_enabled", True)), "vector_backend": vector_backend, "embedding_profile_id": rag.get("embedding_profile_id") or embedding.get("profile_id"), "reranker_profile_id": rag.get("reranker_profile_id") or reranker.get("profile_id"), "judge": judge}}
