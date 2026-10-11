"""Receipts record observed boundaries without upgrading unknown to zero."""
from __future__ import annotations

from hashlib import sha256
import json
import time
from pathlib import Path
import uuid

from status_projection import select_delivery_view


def stdout_receipt(occurrence: dict, items: list[dict]) -> dict:
    record_ids = [item["id"] for item in items]
    body = json.dumps(items, ensure_ascii=False, sort_keys=True)
    return {"occurrence_id": occurrence["occurrence_id"], "stage": "stdout_written", "at": time.time(), "record_ids": record_ids,
            "content_sha256": sha256(body.encode()).hexdigest(), "host_visibility": "unknown"}


def host_observation(occurrence: dict, items: list[dict]) -> dict:
    record_ids = [item["id"] for item in items]
    body = json.dumps(items, ensure_ascii=False, sort_keys=True)
    return {"occurrence_id": occurrence["occurrence_id"], "stage": "host_tool_response_observed", "at": time.time(), "record_ids": record_ids,
            "content_sha256": sha256(body.encode()).hexdigest(), "host_visibility": "observed", "model_attention": "not_measured"}


def record_mcp_stdout(root: str | Path, request: dict, result: dict | list[dict]) -> dict:
    root = Path(root); root.mkdir(parents=True, exist_ok=True)
    occurrence = {"occurrence_id": str(request.get("context_ref") or uuid.uuid4()), "context_ref": request.get("context_ref")}
    if isinstance(result, list):
        result = {"included": result, "model_sections": [], "deferred": []}
    items = list(result.get("included") or [])
    receipt = stdout_receipt(occurrence, items)
    category_counts = {}
    for item in items:
        category = str(item.get("primary_category") or "unknown")
        category_counts[category] = category_counts.get(category, 0) + 1
    receipt.update(
        schema="guidance_mcp_delivery.v2",
        kind="guidance_mcp_delivery",
        host_context_observed=None,
        request_id=request.get("context_ref"),
        task=request.get("task") or {},
        selection_revision=result.get("selection_revision"),
        guidance_items=items,
        category_counts=dict(sorted(category_counts.items())),
        model_sections=list(result.get("model_sections") or []),
        deferred=list(result.get("deferred") or []),
        already_loaded_valid=list(result.get("already_loaded_valid") or []),
        budget=result.get("budget") or {},
        coverage=result.get("coverage"),
        next_cursor=result.get("next_cursor"),
    )
    path = root / (uuid.uuid4().hex + ".json")
    path.write_text(json.dumps(receipt, ensure_ascii=False), encoding="utf-8")
    return receipt


def _json_value(value):
    if isinstance(value, str):
        try:
            return _json_value(json.loads(value))
        except (ValueError, TypeError):
            return None
    if not isinstance(value, dict):
        return None
    if isinstance(value.get("delivery_receipt"), dict) and isinstance(value.get("included"), list):
        return value
    for block in value.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parsed = _json_value(block.get("text"))
            if parsed:
                return parsed
    if "result" in value:
        return _json_value(value.get("result"))
    return None


def record_host_tool_response(root: str | Path, host_event: dict) -> dict:
    tool = str(host_event.get("tool_name") or host_event.get("tool") or host_event.get("name") or "")
    if "user_preference" not in tool and "get_preference" not in tool:
        return {"state": "ignored_non_guidance_tool", "tool_name": tool}
    payload = _json_value(host_event.get("tool_response", host_event.get("tool_result", host_event.get("result"))))
    if not payload:
        return {"state": "unrecognized_tool_response", "tool_name": tool}
    embedded = payload["delivery_receipt"]
    occurrence_id = str(embedded.get("occurrence_id") or "")
    record_ids = [str(item.get("id")) for item in payload.get("included") or [] if item.get("id")]
    if not occurrence_id or record_ids != list(embedded.get("record_ids") or []):
        return {"state": "receipt_mismatch", "tool_name": tool}
    root = Path(root)
    target = None
    stdout = None
    for path in sorted(root.glob("*.json"), key=lambda value: value.stat().st_mtime, reverse=True)[:200]:
        try:
            candidate = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        if candidate.get("kind") == "guidance_mcp_delivery" and candidate.get("occurrence_id") == occurrence_id:
            target, stdout = path, candidate
            break
    if target is None or stdout is None:
        return {"state": "stdout_receipt_not_found", "occurrence_id": occurrence_id, "tool_name": tool}
    if record_ids != list(stdout.get("record_ids") or []) or embedded.get("content_sha256") != stdout.get("content_sha256"):
        return {"state": "receipt_mismatch", "occurrence_id": occurrence_id, "tool_name": tool}
    host = {
        "occurrence_id": occurrence_id,
        "stage": "host_tool_response_observed",
        "at": time.time(),
        "record_ids": record_ids,
        "content_sha256": stdout["content_sha256"],
        "host_visibility": "observed",
        "model_attention": "not_measured",
        "host_id": host_event.get("host_id"),
        "session_id": host_event.get("session_id"),
        "turn_id": host_event.get("turn_id"),
        "tool_call_id": host_event.get("tool_call_id") or host_event.get("tool_use_id"),
        "tool_name": tool,
    }
    stdout["host_context_observed"] = host
    stdout["host_visibility"] = "observed_tool_response"
    target.write_text(json.dumps(stdout, ensure_ascii=False), encoding="utf-8")
    return {"state": "host_tool_response_observed", **host}


def delivery_snapshot(root: str | Path) -> list[dict]:
    rows = []
    for path in sorted(Path(root).glob("*.json"), key=lambda value: value.stat().st_mtime, reverse=True)[:50]:
        try:
            stdout = json.loads(path.read_text(encoding="utf-8"))
            if stdout.get("kind") != "guidance_mcp_delivery":
                continue
            rows.append({"request_id": stdout.get("request_id"), **select_delivery_view(stdout, stdout.get("host_context_observed"))})
        except (OSError, ValueError, TypeError):
            rows.append({"state": "unknown", "error": "unreadable_receipt"})
    return rows


def _receipt_rows(root: str | Path, scan_limit: int = 500) -> list[dict]:
    rows = []
    for path in sorted(Path(root).glob("*.json"), key=lambda value: value.stat().st_mtime, reverse=True)[:scan_limit]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        if value.get("kind") == "guidance_mcp_delivery":
            rows.append(value)
    return rows


def delivery_list(root: str | Path, limit: int = 50) -> dict:
    limit = max(1, min(100, int(limit)))
    items = []
    # Entry-adapter checks have their own auditable lane and are surfaced from
    # the instruction status endpoint. Keep the actual tool-delivery list
    # focused on host-visible guidance tool calls.
    rows = [row for row in _receipt_rows(root)
            if not str(row.get("occurrence_id") or "").startswith("entry:")
            and not bool((row.get("task") or {}).get("entry_adapter"))
            and not any("advisory reference" in str(item) for item in ((row.get("task") or {}).get("current_constraints") or []))]
    for receipt in rows[:limit]:
        view = select_delivery_view(receipt, receipt.get("host_context_observed"))
        items.append({
            "occurrence_id": receipt.get("occurrence_id"),
            "request_id": receipt.get("request_id"),
            "at": receipt.get("at"),
            "task": receipt.get("task") or {},
            "coverage": receipt.get("coverage"),
            "category_counts": receipt.get("category_counts") or {},
            "guidance_count": len(receipt.get("guidance_items") or receipt.get("record_ids") or []),
            "model_section_count": len(receipt.get("model_sections") or []),
            "deferred_count": len(receipt.get("deferred") or []),
            "host_id": (receipt.get("host_context_observed") or {}).get("host_id"),
            "session_id": (receipt.get("host_context_observed") or {}).get("session_id"),
            "turn_id": (receipt.get("host_context_observed") or {}).get("turn_id"),
            "tool_call_id": (receipt.get("host_context_observed") or {}).get("tool_call_id"),
            "host_state": view["state"],
            "model_attention": (receipt.get("host_context_observed") or {}).get("model_attention", "unknown"),
        })
    return {"schema": "guidance.delivery-list.v1", "items": items, "count": len(items), "has_more": len(rows) > limit}


def delivery_detail(root: str | Path, occurrence_id: str) -> dict:
    occurrence_id = str(occurrence_id or "")
    for receipt in _receipt_rows(root):
        if str(receipt.get("occurrence_id") or "") == occurrence_id:
            return receipt
    return {"status": "not_found", "occurrence_id": occurrence_id}
