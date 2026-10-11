"""Independent, optional JEV judgments; never a memory retriever or fact writer."""
from __future__ import annotations

import datetime
import hashlib
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "guidance"))
from runtime_settings import load_runtime_settings

PROVIDER_PATH = Path(__file__).resolve().parents[2] / "config/jev-provider.json"
ROOT = Path.home() / ".evolving-profile/audit/jev"
DEFAULT_SCOPES = json.loads(PROVIDER_PATH.read_text(encoding="utf-8"))["scopes"]
QUESTIONS = {
    "internal_memory": {
        "evidence": {"type": "choice", "instructions": "Is the bounded evidence sufficient for the stated query? Candidate summaries are unverified, not source evidence. Choose unknown if coverage cannot be established.", "criteria": {"sufficient": "direct-source evidence supports requested fields", "needs_source": "requires original source readback", "conflict": "conflicting object, version or source", "unknown": "cannot determine"}},
        "scope": {"type": "choice", "instructions": "Do these evidence records clearly belong to the same requested object and version? Session co-occurrence alone does not establish project identity.", "criteria": {"aligned": "verified matching scope", "mixed": "incompatible projects or versions", "unknown": "scope not verified"}},
    },
    "quality_diagnosis": {
        "diagnosis": {"type": "choice", "instructions": "Classify the observed receipt stage only. Unknown host delivery is not delivery failure. A planned route is not proof that a tool was required. Do not infer answer benefit.", "criteria": {"no_failure_observed": "no observed failure", "returned_empty": "tool returned zero", "binding_unknown": "activity is not bound to a Prompt", "delivery_unknown": "return observed but host delivery unmeasured", "tool_failed": "explicit tool error", "needs_investigation": "insufficient or conflicting telemetry"}},
    },
    "operation_risk": {
        "operation": {"type": "choice", "instructions": "Assess this proposed operation. This answer cannot grant permission. Flag irreversible changes, external publication, credential exposure or unverified fact promotion for confirmation.", "criteria": {"allow": "no additional risk identified", "confirm": "requires explicit user confirmation", "block": "clearly forbidden or credential exposure", "unknown": "insufficient scope or authorization evidence"}},
    },
}


def sanitize(value):
    if isinstance(value, dict):
        return {str(k): sanitize(v) for k, v in value.items() if str(k).lower() not in {"api_key", "authorization", "token", "secret", "password"}}
    if isinstance(value, list):
        return [sanitize(v) for v in value[:12]]
    if isinstance(value, str):
        return re.sub(r"apikey_[A-Za-z0-9_]+|sk-[A-Za-z0-9_-]{14,}", "[credential omitted]", value)[:600]
    return value


def rules(state, purposes):
    answers = {}
    if "internal_memory" in purposes:
        answers["evidence"] = {"type": "choice", "choice": "needs_source" if state.get("returned_count", 0) and not state.get("source_readback") else "unknown", "confidence": None}
        answers["scope"] = {"type": "choice", "choice": "unknown", "confidence": None}
    if "quality_diagnosis" in purposes:
        if state.get("error_type"):
            choice = "tool_failed"
        elif state.get("binding_state") not in (None, "prompt_bound"):
            choice = "binding_unknown"
        elif state.get("returned_count") == 0:
            choice = "returned_empty"
        elif state.get("returned_count") and state.get("host_visibility") != "observed":
            choice = "delivery_unknown"
        else:
            choice = "needs_investigation"
        answers["diagnosis"] = {"type": "choice", "choice": choice, "confidence": None}
    if "operation_risk" in purposes:
        answers["operation"] = {"type": "choice", "choice": "confirm" if state.get("irreversible") or state.get("external_send") else "unknown", "confidence": None}
    return answers


def caller_view(receipt):
    if receipt.get("mode") == "shadow":
        return {k: v for k, v in receipt.items() if k not in {"answers", "advice", "requires_confirmation", "keep_ids", "reject_ids", "risk"}}
    return receipt


def audit_external_review(receipt, binding):
    if receipt.get("calls"):
        ROOT.mkdir(parents=True, exist_ok=True)
        with (ROOT / "reviews.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({**receipt, "effect": "audit_only" if receipt.get("mode") == "shadow" else "advisory", "purposes": ["external_rag"], "at": datetime.datetime.now(datetime.timezone.utc).isoformat(), "check_id": binding.get("check_id"), "tool": "rag_search"}, ensure_ascii=False) + "\n")


def review(purposes, state, settings=None, *, root=None):
    settings = settings if settings is not None else load_runtime_settings()
    judge = (settings.get("retrieval_models") or {}).get("judge") or {}
    mode = str(judge.get("mode_policy") or "off")
    scopes = {**DEFAULT_SCOPES, **(judge.get("scopes") or {})}
    enabled = [p for p in dict.fromkeys(purposes) if p in QUESTIONS and (judge.get("risk_gate_enabled", False) if p == "operation_risk" else scopes.get(p, False))]
    receipt = {"schema": "evolving-profile.jev-review.v1", "mode": mode, "purposes": enabled,
               "status": "disabled", "calls": 0, "cache_hit": False, "memory_mutated": False,
               "effect": "audit_only" if mode == "shadow" else "advisory", "fallback": "rules"}
    if not judge.get("enabled") or mode == "off" or not enabled:
        return receipt
    state = sanitize(state if isinstance(state, dict) else {})
    questions = {k: v for purpose in enabled for k, v in QUESTIONS[purpose].items()}
    receipt["answers"] = rules(state, enabled)
    key = str(judge.get("api_key") or "").strip()
    if not key:
        return {**receipt, "status": "not_configured"}
    provider = json.loads(PROVIDER_PATH.read_text(encoding="utf-8"))
    body = {"model": provider["model"], "state": state, "questions": questions}
    signature = hashlib.sha256(json.dumps([body, mode, hashlib.sha256(key.encode()).hexdigest()], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    root = Path(root or ROOT)
    cache = root / (signature + ".json")
    try:
        saved = json.loads(cache.read_text())
        if saved.get("status") == "ok" and time.time() - saved.get("cached_at", 0) < 600:
            return {**saved, "calls": 0, "cache_hit": True}
    except (OSError, ValueError, TypeError):
        pass
    # No caller can replace the provider endpoint or send credentials in state.
    request = urllib.request.Request(provider["endpoint"], data=json.dumps(body, ensure_ascii=False).encode(), method="POST", headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
    started = time.monotonic()
    try:
        timeout = max(.5, min(8, float(judge.get("timeout_ms") or 5000) / 1000))
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read(100000).decode())
        answers = result.get("answers") or {}
        valid = {}
        for name, question in questions.items():
            answer = answers.get(name) or {}
            if answer.get("type") == "choice" and answer.get("choice") in question["criteria"]:
                valid[name] = {"type": "choice", "choice": answer["choice"], "confidence": answer.get("confidence")}
        if len(valid) != len(questions):
            raise ValueError("invalid_judge_response")
        receipt.update(status="ok", answers=valid, model=result.get("model"), usage=result.get("usage") or {}, fallback=None)
    except (urllib.error.URLError, TimeoutError, ValueError, OSError, TypeError) as error:
        receipt.update(status="unavailable", error_type=type(error).__name__)
    receipt.update(calls=1, latency_ms=round((time.monotonic() - started) * 1000), cached_at=time.time(), at=datetime.datetime.now(datetime.timezone.utc).isoformat())
    operation = receipt.get("answers", {}).get("operation", {}).get("choice")
    if "operation_risk" in enabled:
        receipt["requires_confirmation"] = operation in {"confirm", "block", "unknown"}
    try:
        root.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(receipt, ensure_ascii=False), encoding="utf-8")
        with (root / "reviews.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({**receipt, "review_id": signature, "check_id": state.get("check_id"), "tool": state.get("tool")}, ensure_ascii=False) + "\n")
    except OSError:
        receipt["audit_status"] = "unavailable"
    return receipt


def project_tool_review(tool, args, value, binding):
    """Bounded evidence summaries and telemetry, not original conversations."""
    rows = value.get("memories") or value.get("records") or []
    if not rows and isinstance(value.get("record"), dict):
        rows = [value["record"]]
    source = value.get("raw_source") or value.get("source")
    source = source if isinstance(source, dict) else {}
    original_read = bool(source.get("text")) and tool in {"read_source", "read_agent_process_memory"}
    count = value.get("returned_count")
    if count is None:
        count = len(rows) if rows else int(bool(source.get("text")))
    state = {"tool": tool, "query": str(args.get("query") or ""), "check_id": args.get("check_id"),
             "binding_state": binding.get("state"), "returned_count": count,
             "candidate_coverage": {"returned_count": count, "eligible_count": value.get('total_count', value.get('discovered_reference_count')), "next_offset": value.get('next_offset')},
             "original_message_coverage": value.get('raw_source_coverage') or {},
             "host_visibility": (value.get("delivery") or {}).get("host_visibility") or "unknown",
             "source_readback": original_read,
             "evidence_boundary": "source_excerpt_not_independent_verification" if original_read else "extracted_record_not_original_source",
             "scope_unresolved": (value.get("scenario_followup") or {}).get("required"),
             "candidates": [{"id": r.get("id") or r.get("process_memory_id"), "summary": str(r.get("text") or "")[:350],
                 "scope": r.get("primary_context") or r.get("metadata"), "source_locator": r.get("source_locator"), "maturity": r.get("maturity"),
                 "source_integrity": r.get("source_integrity")} for r in rows[:8] if isinstance(r, dict)],
             "source_excerpt": str(source.get("text") or "")[:1800]}
    return caller_view(review(["internal_memory", "quality_diagnosis"], state))


if __name__ == "__main__":
    # The Console passes a small state through stdin, never secrets in argv.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    request = json.loads(sys.stdin.read(20000))
    print(json.dumps(review(request.get("purposes") or ["quality_diagnosis"], request.get("state") or {}), ensure_ascii=False))
