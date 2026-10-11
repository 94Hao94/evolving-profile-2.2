"""Shared, local-only EP and external RAG runtime settings."""
from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host-adapter"))
from lib.recall_relevance import normalize_recall_policy, policy_defaults, resolve_min_relevance

SETTINGS_PATH = Path(os.environ.get("EVOLVING_PROFILE_RUNTIME_SETTINGS", str(Path.home() / ".evolving-profile/config/runtime-settings.json")))
_RECALL_DEFAULTS = policy_defaults()

_MODULE = {"record": True, "retrieve": True, "inject": True}
_PROCESS_MEMORY_MODULE = {"record": True, "retrieve": True, "inject": True}
AGENT_PROCESS_MODULES = {
    "agent_process_trajectory": "原始轨迹",
    "agent_process_observation": "过程观察",
    "agent_process_failure_episode": "失败事件",
    "agent_process_repair_pattern": "修复模式",
    "agent_process_capability": "能力观测",
    "agent_process_strategy": "可复用过程策略",
    "agent_process_revalidation": "迁移与再验证",
}
DEFAULT_SETTINGS = {
    "schema": "evolving-profile.runtime-settings.v1",
    "recall_policy": _RECALL_DEFAULTS["recall_policy"],
    "modules": {
        "facts": dict(_MODULE), "experiences": dict(_MODULE), "entities": dict(_MODULE),
        "preferences": dict(_MODULE), "scenario_summary": dict(_MODULE),
        "mental_models": dict(_MODULE), "source_readback": dict(_MODULE),
        "background_reflection": dict(_MODULE),
        "agent_process_memory": dict(_PROCESS_MEMORY_MODULE),
        **{name: dict(_PROCESS_MEMORY_MODULE) for name in AGENT_PROCESS_MODULES},
    },
    "routing": {"mode": "auto", "ep_enabled": True, "external_rag_enabled": False,
                "allow_parallel": False, "conflict_policy": "show_both"},
    "budgets": {"ep_total_tokens": 4000, "rag_total_tokens": 4000, "total_tokens": 6000,
                "preference_tokens": 1200, "scenario_tokens": 1200, "source_tokens": 2400},
    "rag": {"enabled": False, "minimum_relevance": _RECALL_DEFAULTS["rag"]["minimum_relevance"], "root_path": "", "collection": "default", "lexical_enabled": True,
            "vector_enabled": True, "fusion": "rrf", "lexical_weight": 0.5, "vector_weight": 0.5,
            "rerank_enabled": True, "rerank_provider": "local", "rerank_model": "",
            "top_k": 20, "score_threshold": 0.35, "max_chunks": 8, "auto_index": False},
    "providers": {"primary": {"name": "", "base_url": "", "model": "", "api_key": ""}, "fallbacks": []},
}


def _merge(base, override):
    result = copy.deepcopy(base)
    if not isinstance(override, dict):
        return result
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


def load_runtime_settings(path: Path | None = None) -> dict:
    target = Path(path or SETTINGS_PATH)
    try:
        stored = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return copy.deepcopy(DEFAULT_SETTINGS)
    if not isinstance(stored, dict):
        raise ValueError("invalid_runtime_settings_object")
    if "recall_policy" in stored:
        if not isinstance(stored["recall_policy"], dict):
            raise ValueError("invalid_recall_policy_object")
        normalize_recall_policy(stored["recall_policy"])
    for field in ("modules", "routing", "budgets", "rag", "providers", "retrieval_models"):
        if field in stored and not isinstance(stored[field], dict):
            raise ValueError(f"invalid_{field}_object")
    providers = stored.get("providers", {})
    if "fallbacks" in providers and (not isinstance(providers["fallbacks"], list) or any(not isinstance(item, dict) for item in providers["fallbacks"])):
        raise ValueError("invalid_providers_fallbacks_array")
    value = _merge(DEFAULT_SETTINGS, stored)
    value["recall_policy"] = normalize_recall_policy(value["recall_policy"])
    resolve_min_relevance(value, "external_rag")
    return value


def module_enabled(settings: dict, module: str, action: str = "retrieve") -> bool:
    return bool(((settings.get("modules") or {}).get(module) or {}).get(action, True))


def route_policy(settings: dict, request_source: str = "auto") -> dict:
    routing = settings.get("routing") or {}
    rag_enabled = bool((settings.get("rag") or {}).get("enabled")) and bool(routing.get("external_rag_enabled", False))
    ep_enabled = bool(routing.get("ep_enabled", True))
    mode = request_source if request_source in {"ep", "external_rag", "both_isolated"} else routing.get("mode", "auto")
    if mode == "ep": sources = ["ep"] if ep_enabled else []
    elif mode == "external_rag": sources = ["external_rag"] if rag_enabled else []
    elif mode == "both_isolated": sources = [source for source, enabled in (("ep", ep_enabled), ("external_rag", rag_enabled)) if enabled]
    else: sources = ["ep"] if ep_enabled else []
    return {"mode": mode, "sources": sources, "ep_enabled": ep_enabled, "rag_enabled": rag_enabled,
            "conflict_policy": routing.get("conflict_policy", "show_both")}
