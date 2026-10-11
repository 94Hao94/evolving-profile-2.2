#!/usr/bin/env python3
"""Auto-recall hook for UserPromptSubmit.

Fires before each user prompt. Retrieves relevant memories from Hindsight
and injects them into the Codex context via hookSpecificOutput.additionalContext.

Flow:
  1. Read hook input from stdin (session_id, transcript_path, prompt/user_prompt)
  2. Resolve API URL
  3. Derive bank ID and ensure mission
  4. Compose multi-turn query if recallContextTurns > 1
  5. Truncate to recallMaxQueryChars
  6. Call Hindsight recall API
  7. Format memories and output hookSpecificOutput.additionalContext

Exit codes:
  0 — always (graceful degradation on any error)
"""

from __future__ import annotations

import io
import hashlib
from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher
import json
import os
import re
import subprocess
import sys
import time
import threading
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Optional
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from guidance_entry import prepare_agent_owned_entry, run_entry_check, simple_self_contained
from lib.memory_policy import classify_memory_policy

# Filled once per UserPromptSubmit.  It is prepended to every Hook output,
# including deliberate zero/timeout branches, so the forced task-guidance
# check cannot disappear when recall is skipped.
ENTRY_GUIDANCE_CONTEXT = ""
ENTRY_GUIDANCE_RECEIPT = {}

from lib.runtime_paths import ham_source_root

HAM_SOURCE_ROOT = ham_source_root(__file__)
if HAM_SOURCE_ROOT not in sys.path:
    sys.path.insert(0, HAM_SOURCE_ROOT)
try:
    from ham.adapter import emit as ham_emit
except Exception:
    def ham_emit(*_args, **_kwargs):
        return {"attempted": False, "reason": "adapter_unavailable"}

from lib.bank import derive_bank_id, ensure_bank_mission
from lib.client import HindsightClient
from lib.config import debug_log, load_config
from lib.content import (
    compose_recall_query,
    extract_user_request,
    format_current_time,
    format_memories,
    read_transcript,
    truncate_recall_query,
)
from lib.context_coordination import (
    build_profile as build_context_memory_profile,
    build_contextual_intent_envelope,
    build_contextual_recall_query,
    build_full_prompt_context_slice,
    coordinate_items as coordinate_context_memory_items,
    filter_identity_subject as filter_identity_subject_items,
    format_live_context_reactivations,
    merge_metrics as merge_context_memory_metrics,
    select_live_context_reactivations,
)
from lib.session_context_index import (
    DEFAULT_INDEX_PATH as SESSION_CONTEXT_INDEX_PATH,
    index_transcript as index_session_transcript,
    retrieve_context_bundle,
)
from lib.daemon import get_api_url
from lib.governance import (
    apply_recall_governance,
    semantic_dependency_contract_results,
    is_identity_query,
    is_backup_runtime_query,
    is_current_coding_plan_query,
    is_current_runtime_query,
    is_wps_sync_current_query,
    is_unknown_attribute_policy_question,
    is_smalltalk_memory_policy_question,
    is_local_edit_memory_policy_question,
    requires_historical_bank_recall,
)
from lib.handoff import format_handoff, select_handoff
from lib.memory_router_v4 import build_plan as build_memory_plan_v4
from lib.memory_router_v4 import load_policy as load_memory_policy_v4
from lib.memory_router_v4 import resolve_client_adapter
from lib.memory_packet import build_memory_packet
from lib.relevance import (
    POLICY as RELEVANCE_POLICY,
    admission_decision as relevance_admission_decision,
    admit_items as relevance_admit_items,
    cross_encoder_scores as relevance_cross_encoder_scores,
    explicit_user_source_request,
    operational_audit_query,
)
from lib.guidance_provenance import registry_revision
from lib.admission_contract import partition_verified_decisions
from lib.state import read_state, write_state
from iphone_memory_mode import apply_strategy as apply_iphone_strategy
from iphone_memory_mode import consume_strategy as consume_iphone_strategy

LAST_RECALL_STATE = "last_recall.json"

# This is deliberately derived from the deployed Hook and admission policy,
# rather than a hand-maintained version string. A Controller reuse is safe only
# when the Hook that turns candidates into a Packet has the same contract.
ADMISSION_CONTRACT_SCHEMA = "hindsight.admission-contract.v1"


def admission_contract_revision() -> str:
    digest = hashlib.sha256()
    digest.update(ADMISSION_CONTRACT_SCHEMA.encode("utf-8"))
    for path in (Path(__file__), *(Path(__file__).parent / 'lib' / name for name in ('relevance.py','governance.py','input_origin.py','admission_contract.py','guidance_provenance.py'))):
        try:
            digest.update(path.read_bytes())
        except OSError:
            digest.update(str(path).encode("utf-8"))
    return f"{ADMISSION_CONTRACT_SCHEMA}:{digest.hexdigest()[:20]}"


ADMISSION_CONTRACT_REVISION = admission_contract_revision()


def controller_turn_headers(hook_input: dict, source_revision: str, mode: str) -> dict[str, str]:
    """Build bounded, non-content headers that bind one Controller decision."""
    return {
        "X-Memory-Session-Id": str(hook_input.get("session_id") or "")[:160],
        "X-Memory-Turn-Id": str(hook_input.get("turn_id") or "")[:160],
        "X-Memory-Source-Revision": str(source_revision or "unavailable")[:160],
        "X-Memory-Admission-Mode": str(mode or "legacy").casefold()[:32],
    }


def partition_controller_admission(results: list[dict], controller_receipt: dict, context: dict) -> dict[str, list[dict]]:
    """Reuse a matching Controller decision; never run a second topic score."""
    receipt = dict(controller_receipt or {})
    decisions = receipt.get("admission_decisions")
    if not isinstance(decisions, list):
        return {"admit": [], "reject": [], "defer": [], "unverified": [dict(item or {}) for item in results or []]}
    return partition_verified_decisions(results or [], decisions, context)


def controller_contract_shadow(results: list[dict], controller_receipt: dict, context: dict) -> dict:
    """Describe contract coverage while the legacy Hook still owns output."""
    partition = partition_controller_admission(results, controller_receipt, context)
    counts = {name: len(rows) for name, rows in partition.items()}
    return {
        "schema": "hindsight.admission_contract_shadow.v1",
        "state": "verified" if not counts["unverified"] else "legacy_fallback_required",
        "counts": counts,
        "meaning": "影子结果不改变本轮 Hook 候选、准入或 Packet；仅显示Controller合同是否完整覆盖。",
    }


def enforce_controller_admission(results: list[dict], controller_receipt: dict, context: dict) -> dict:
    """Use Controller semantics only when every supplied candidate is bound."""
    partition = partition_controller_admission(results, controller_receipt, context)
    counts = {name: len(rows) for name, rows in partition.items()}
    if partition["unverified"]:
        return {
            "state": "legacy_fallback_required",
            "items": [dict(item or {}) for item in results or []],
            "partition": partition,
            "counts": counts,
            "reason": "本轮至少一条候选没有匹配的Controller合同；不能把缺口伪装成Hook语义拒绝。",
        }
    return {
        "state": "enforced",
        "items": partition["admit"],
        "partition": partition,
        "counts": counts,
        "reason": "所有候选均由同回合Controller合同覆盖；Hook只执行传输边界保护。",
    }


def controller_owned_hook_lanes(mode: str, mental_model_route, evidence_route):
    """Disable Hook-only semantic lanes once the Controller contract enforces."""
    if str(mode or "").casefold() == "enforce":
        return None, None
    return mental_model_route, evidence_route


def _load_dotenv_values(path: Path) -> dict:
    values = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" not in line or line.lstrip().startswith("#"):
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    except OSError:
        pass
    return values


def qwen_full_prompt_fallback(prompt: str, messages: list[dict], *, timeout_ms: int = 12000) -> dict:
    """Ask the configured Coding Plan model to resolve an ambiguous turn.

    Codex's UserPromptSubmit payload does not expose the host model's own
    context-resolved intent.  This is therefore a labelled fallback, never an
    attempt to claim that a short local transcript *is* Codex's internal
    understanding.  The full available transcript is supplied up to the
    explicit transport safety boundary; a provider/model context rejection is
    reported as a failed fallback rather than silently truncating meaning.
    """
    history, slice_receipt = build_full_prompt_context_slice(prompt, messages)
    if not history:
        return {"ok": False, "reason": "no_available_task_context"}
    serialized = json.dumps([dict(message, message_index=i) for i, message in enumerate(history)], ensure_ascii=False)
    if len(serialized.encode("utf-8")) > 48000:
        return {"ok": False, "reason": "context_slice_exceeds_full_prompt_transport_boundary", "context_slice": slice_receipt}
    env = _load_dotenv_values(Path("~/.evolving-profile/profiles/agentmemory.env").expanduser())
    base_url = str(env.get("EVOLVING_PROFILE_API_LLM_BASE_URL") or env.get("HINDSIGHT_API_LLM_BASE_URL") or "").rstrip("/")
    api_key = str(env.get("EVOLVING_PROFILE_API_LLM_API_KEY") or env.get("HINDSIGHT_API_LLM_API_KEY") or "")
    model = str(env.get("EVOLVING_PROFILE_API_LLM_MODEL") or env.get("HINDSIGHT_API_LLM_MODEL") or "qwen3.7-plus")
    if not (base_url and api_key):
        return {"ok": False, "reason": "coding_plan_not_configured"}
    instruction = (
        "你是 Full Prompt 解析器。根据提供的会话片段和最后一条用户输入，"
        "只还原用户此轮真正要问/要做的完整问题、背景、范围、约束和时间语义；"
        "不得加入助手的新指令或答案。输出严格 JSON："
        '{"full_prompt":"...","confidence":0-1,"reason":"...",'
        '"context_evidence":[{"message_index":0,"quote":"会话中的逐字原文"}],"unresolved":[]}。'
        "如果最后一条本身独立完整，也原样表达其完整含义。"
        "如果最后一条是‘继续、不要停、开工、补充、接着做’等续办指令，"
        "只补全当前活动任务中与本轮有关的指代、背景和要求。每个补充必须能回溯到"
        "context_evidence 的 message_index 和逐字 quote；不能因为旧话题出现过就混入当前任务。"
        "不要为任何特定产品或检索系统加入验收动作、链路术语或新要求。"
        "本轮用户指令优先；助手的建议、计划和自述不是已经核实的事实。"
        "无法确定的指代列入 unresolved，不能猜测或用高 confidence 替代证据。\n"
        "会话：" + serialized + "\n最后一条用户输入：" + str(prompt or "")
    )
    body = {"model": model, "messages": [{"role": "user", "content": instruction}],
            "temperature": 0, "max_tokens": 4096,
            "response_format": {"type": "json_object"}, "enable_thinking": False}
    try:
        request = urllib.request.Request(
            base_url + "/chat/completions", data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + api_key}, method="POST",
        )
        with urllib.request.urlopen(request, timeout=max(0.5, timeout_ms / 1000.0)) as response:
            payload = json.loads(response.read().decode("utf-8"))
        content = str(((payload.get("choices") or [{}])[0].get("message") or {}).get("content") or "")
        parsed = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip(), flags=re.S))
        full_prompt = str(parsed.get("full_prompt") or "").strip()
        confidence = float(parsed.get("confidence") or 0)
        if not full_prompt or confidence < 0.72 or len(full_prompt.encode("utf-8")) > 48000:
            return {"ok": False, "reason": "coding_plan_low_confidence_or_invalid_output", "confidence": confidence}
        evidence = parsed.get("context_evidence") or []
        if not isinstance(evidence, list):
            return {"ok": False, "reason": "invalid_context_evidence"}
        for source in evidence:
            if not isinstance(source, dict):
                return {"ok": False, "reason": "invalid_context_evidence"}
            index, quote = source.get("message_index"), source.get("quote")
            if (type(index) is not int or not 0 <= index < len(history)
                    or not isinstance(quote, str) or not quote.strip()
                    or quote not in str(history[index].get("content") or "")):
                return {"ok": False, "reason": "untraceable_context_evidence"}
        if full_prompt != str(prompt or "").strip() and not evidence:
            return {"ok": False, "reason": "expanded_prompt_without_context_evidence"}
        unresolved = parsed.get("unresolved") or []
        if not isinstance(unresolved, list) or any(not isinstance(value, str) for value in unresolved):
            return {"ok": False, "reason": "invalid_unresolved_references"}
        # Missing task data is a reason to search, not a reason to discard the
        # grounded question. Preserve the uncertainty in the retrieval input.
        if unresolved:
            full_prompt += "\n尚待查证，不能当成已知事实：" + "；".join(unresolved)
        return {"ok": True, "full_prompt": full_prompt, "confidence": round(confidence, 3),
                "unresolved": unresolved,
                "resolution_state": "partial_with_unresolved" if unresolved else "source_traced",
                "context_evidence": evidence, "grounding_check": "quote_integrity_only_not_semantic_entailment",
                "reason": str(parsed.get("reason") or "")[:300], "context_slice": slice_receipt}
    except Exception as error:
        return {"ok": False, "reason": "coding_plan_full_prompt_failed", "error": type(error).__name__}


def _with_entry_guidance(context: str = "") -> str:
    combined = ENTRY_GUIDANCE_CONTEXT + ("\n" + str(context or "") if ENTRY_GUIDANCE_CONTEXT and context else str(context or ""))
    # Bind before lengthy instructions/candidates can be truncated by a host.
    match = re.search(r'<evolving_profile_memory_route[^>]*check_id="([a-f0-9]{32})"', combined)
    if match:
        return ('<ep_prompt_binding check_id="' + match.group(1) + '">'
                'Use this ID for every EP tool call and follow-up read in this Prompt, not a later Prompt. '
                'Candidate return, original-source readback and verified outcome are separate.'
                '</ep_prompt_binding>\n' + combined)
    return combined


def emit_hook_output(context: str = "") -> None:
    """Always honour Codex's Hook JSON contract, including a deliberate zero.

    A zero-result turn is a completed routing decision, not an absent Hook
    response.  Returning no bytes makes real Codex and shadow replays behave
    differently and previously left the status page with a chain that looked
    truncated.  Keep the output shape stable while leaving ``context`` empty
    when no memory is permitted to cross the boundary.
    """
    json.dump({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": _with_entry_guidance(context),
        }
    }, sys.stdout)
    sys.stdout.flush()


def record_agent_choice_pending_receipt(hook_input: dict, prompt: str, config: dict, invocation_id: str) -> None:
    """Record that automatic recall is disabled without inventing a model decision.

    Codex may still call the MCP tools later.  This receipt only proves the
    Hook did not auto-call recall and leaves the agent's later choice unknown
    until a same-turn PostToolUse receipt arrives.
    """
    try:
        root=Path(os.path.expanduser(config.get("hookOutputReceiptRoot", "~/.evolving-profile/audit/hook-output-receipts")))/"production"
        root.mkdir(parents=True,exist_ok=True)
        receipt={
            "at":datetime.now(timezone.utc).isoformat(),
            "session_id":str(hook_input.get("session_id") or "unknown"),
            "turn_id":hook_input.get("turn_id"),
            "hook_invocation_id":invocation_id,
            "raw_user_prompt":prompt,
            "prompt_origin":str(hook_input.get("memory_prompt_origin") or "user_direct"),
            "memory_action":"auto_recall_disabled",
            "history_decision":"agent_decides",
            "history_decision_evidence":"hook_config_auto_recall_false",
            "candidate_count":None,
            "injected_count":0,
            "packet_delivery":{"state":"hook_stdout_write_completed","reason":"入口说明已输出；自动历史召回关闭，等待 Codex 按需 MCP 调用。"},
        }
        name=hashlib.sha256((receipt["session_id"]+":"+invocation_id).encode()).hexdigest()
        target=root/(name+".json"); temporary=root/(name+".tmp")
        temporary.write_text(json.dumps(receipt,ensure_ascii=False),encoding="utf-8"); temporary.chmod(0o600); temporary.replace(target)
    except OSError:
        pass


def emit_bounded_system_probe(hook_input, prompt, config, invocation_id):
    """One bounded direct Bank read; never re-enter the legacy recall pipeline."""
    from system_probe import plan_history, run_probe
    task=((ENTRY_GUIDANCE_RECEIPT or {}).get('request') or {}).get('task') or {}
    plan=plan_history(prompt,task)
    state_root=Path(os.environ.get('EVOLVING_PROFILE_STATE_ROOT',str(Path.home()/'.evolving-profile')))
    try:settings=json.loads((state_root/'config/guidance-settings.json').read_text())
    except (OSError,ValueError):settings={'auto_probe':True,'probe_max_tokens':500}
    base=os.environ.get('EVOLVING_PROFILE_SOURCE_API_URL','http://127.0.0.1:12088').rstrip('/')
    def api(path,body,timeout):
        endpoint=urllib.parse.urlparse(base)
        if endpoint.scheme!='http' or endpoint.hostname not in ('127.0.0.1','localhost','::1'):
            raise ValueError('probe requires local trusted data plane')
        headers={'Content-Type':'application/json','X-Memory-Client':'ep-system-probe',
                 'X-Memory-Invocation-Id':invocation_id}
        if config.get('evolvingProfileApiToken'):headers['Authorization']='Bearer '+config['evolvingProfileApiToken']
        request=urllib.request.Request(base+path,data=json.dumps(body,ensure_ascii=False).encode(),headers=headers,method='POST')
        with urllib.request.urlopen(request,timeout=timeout) as response:data=response.read(2*1024*1024+1)
        if len(data)>2*1024*1024:raise ValueError('probe response too large')
        return json.loads(data)
    bank=derive_bank_id(hook_input,config)
    context,probe=run_probe(plan,settings,api,bank)
    if not is_shadow_replay() and hook_input.get('session_id') and hook_input.get('turn_id'):
        try:
            from memory_turn_check import register_route
            register_route({
                'invocation_id':invocation_id,
                'session_id':hook_input.get('session_id'),
                'turn_id':hook_input.get('turn_id'),
                'raw_prompt':prompt,
                'execution_mode':'production',
                'prompt_origin':hook_input.get('memory_prompt_origin','user_direct'),
                'required_ep_tool':plan.get('required_ep_tool'),
                'allow_native_memory':plan.get('allow_native_memory',False),
                'memory_policy':plan.get('memory_policy'),
                'recommended_route':plan.get('recommended_route'),
            })
        except Exception as error:
            debug_log(config,'Memory access guard route receipt unavailable: '+type(error).__name__)
    identity={'session_id':hook_input.get('session_id'),'turn_id':hook_input.get('turn_id'),
              'hook_invocation_id':invocation_id}
    route={k:plan[k] for k in ('recommended_route','history_dependency','minimum_action','reason','required_slots','context_source','candidate_policy','fallback_route','fallback_trigger','required_ep_tool','allow_native_memory')}
    route['check_id']=invocation_id
    route_context='<evolving_profile_memory_route>\n'+json.dumps(route,ensure_ascii=False).replace('<','\\u003c')+'\n</evolving_profile_memory_route>'
    output=route_context+'\n'+context
    if not is_shadow_replay():
        record_prompt_ingress(config,prompt,str(hook_input.get('session_id') or 'unknown'),str(hook_input.get('cwd') or ''),
            turn_id=hook_input.get('turn_id'),hook_invocation_id=invocation_id,prompt_origin=hook_input.get('memory_prompt_origin','user_direct'))
    emit_hook_output(output)
    probe['delivery_stage']='hook_stdout_write_completed'
    receipt={'at':datetime.now(timezone.utc).isoformat(),**identity,'raw_user_prompt':prompt,
        'prompt_origin':hook_input.get('memory_prompt_origin','user_direct'),'bank_id':bank,
        'memory_action':'system_probe','system_probe':probe,'history_plan':route,
        'history_decision':'not_needed' if plan['minimum_action']=='skip' else 'needed',
        'history_decision_evidence':'bounded_system_probe',
        'candidate_count':probe['candidate_count'],'injected_count':probe['returned_count'],
        'injected_items':probe['items'],'packet_delivery':{'state':'hook_stdout_write_completed'},
        'context_sha256':hashlib.sha256(_with_entry_guidance(output).encode()).hexdigest()}
    receipt.update({
        'route_required': plan.get('recommended_route') in {'recall', 'research'},
        'route_started': False,
        'tool_called': False,
        'returned_count': 0,
        'delivery_state': 'not_started' if plan.get('recommended_route') in {'recall', 'research'} else 'not_required',
        'unresolved': ['EP历史工具尚未调用；本地文件搜索或候选提示不计为历史核验'] if plan.get('recommended_route') in {'recall', 'research'} else [],
    })
    from evidence_workspace import _save
    lane='diagnostic' if is_shadow_replay() or hook_input.get('memory_prompt_origin')=='test_probe' else 'production'
    _save(state_root/'audit/hook-output-receipts'/lane/(hashlib.sha256((str(identity['session_id'])+':'+invocation_id).encode()).hexdigest()+'.json'),receipt)
    _save(state_root/'audit/memory-route-receipts'/(hashlib.sha256(invocation_id.encode()).hexdigest()+'.json'),
          {**plan,'check_id':invocation_id,'raw_prompt':prompt,'prompt_binding':identity,'system_probe':probe})


def route_requires_ep_history(prompt: str, task: dict | None = None) -> bool:
    """Return whether the entry route requires a real EP history call.

    The agent-owned entry path may provide navigation, but a required history
    route cannot end at the bounded probe because that probe is not Recall or
    Research. The legacy pipeline remains the execution owner for this turn.
    """
    from system_probe import plan_history
    plan = plan_history(prompt, task or {})
    return plan.get('minimum_action') in {'recall_probe', 'agent_query', 'live_audit'} or plan.get('recommended_route') in {'recall', 'research', 'live_audit'}


def publish_injection(context: str, payload: dict, config: dict) -> bool:
    """Record output only after the actual stdout boundary succeeds."""
    delivered_context = _with_entry_guidance(context)
    payload.setdefault("entry_guidance", dict(ENTRY_GUIDANCE_RECEIPT or {}))
    payload["entry_guidance_context_chars"] = len(delivered_context)
    emit_hook_output(context)
    payload.setdefault("packet_delivery", {}).update({
        "state": "hook_stdout_write_completed",
        "reason": "Hook JSON 已写出并 flush；宿主接收、截断和回答使用需独立核验。",
    })
    # Local output evidence must not depend on the HTTP audit queue catching
    # up. Keep small, atomic occurrence-keyed receipts in independent lanes.
    if payload.get("session_id") and payload.get("hook_invocation_id"):
        root=Path(os.path.expanduser(config.get("hookOutputReceiptRoot", "~/.evolving-profile/audit/hook-output-receipts")))
        lane="diagnostic" if is_shadow_replay() or payload.get('prompt_origin')=='test_probe' else "production"
        root=root/lane;root.mkdir(parents=True,exist_ok=True)
        receipt={key:payload[key] for key in (
            "session_id","turn_id","hook_invocation_id","query_id","execution_id","stage","bank_id","raw_user_prompt","host_id",
            "prompt_origin","injected_count","injected_ids","injected_items","packet_delivery","memory_action",
            "claim_delivery","context_chars","entry_guidance_context_chars","entry_guidance","source_guard","injection_receipt_state","outcome","retrieval_failure","candidate_count","memory_needs","guidance_sidecar","guidance_receipt","admission_contract_shadow","admission_contract_enforcement","admission_decisions","source_revision","admission_policy",
            "bank_query_count","memory_policy",
        ) if key in payload}
        receipt.update(at=datetime.now(timezone.utc).isoformat(),execution_mode="shadow_replay" if is_shadow_replay() else "production",
                       context_sha256=hashlib.sha256(delivered_context.encode()).hexdigest())
        name=hashlib.sha256((str(payload['session_id'])+":"+str(payload['hook_invocation_id'])).encode()).hexdigest()
        target=root/(name+'.json');temporary=root/(name+'.tmp')
        temporary.write_text(json.dumps(receipt,ensure_ascii=False),encoding='utf-8');temporary.chmod(0o600);temporary.replace(target)
        for old in sorted(root.glob('*.json'),key=lambda p:p.stat().st_mtime,reverse=True)[240:]:old.unlink(missing_ok=True)
    return post_memory_feedback(config, payload)


def publish_empty_injection(payload: dict, config: dict, controller_receipt: dict,
                            candidate_items: list, deferred_items: list) -> bool:
    """Close a deliberate zero through the same stdout receipt as a Packet.

    Every normal zero must carry its turn identity and guidance decision to the
    output lane.  Calling the feedback endpoint alone records a controller
    choice but cannot prove that Codex received an explicit empty Hook result.
    """
    from lib.memory_needs import guidance_receipt
    payload["guidance_receipt"] = guidance_receipt(
        controller_receipt, candidate_items, [], deferred_items=deferred_items
    )
    return publish_injection("", payload, config)


def fit_hook_context(context: str, packet: dict, packet_args: dict, config: dict):
    """Defer whole evidence bundles before the host truncates their contents.

    This is a character budget, not a fixed number of memories. The current
    prompt and wrapper remain intact; deferred IDs remain in the audit Packet.
    """
    # Native Codex can truncate a 5,000-character Hook payload inside the
    # developer-message boundary.  Keep a measured safety margin so the
    # occurrence-keyed output receipt and the host-visible record IDs agree.
    limit = max(1000, int(config.get("hookContextMaxChars", 3600)))
    if len(context) <= limit:
        return context, packet
    original = packet["rendered_context"]
    overhead = len(context) - len(original)
    limited = build_memory_packet(**packet_args, max_rendered_chars=max(0, limit-overhead))
    limited["host_output_budget"] = {"max_chars":limit,"original_chars":len(context),
                                    "reason":"avoid_native_host_truncation"}
    return context.replace(original, limited["rendered_context"], 1), limited


def explicit_no_history_request(query: str) -> bool:
    """Recognize an explicit user request to stay self-contained.

    This is deliberately narrower than generic short-question detection.  A
    direct phrase such as “只根据这句话”“不查历史” is an executable boundary:
    the Hook must not call Bank recall or inject nearby memories merely because
    a noun (for example “预算”) overlaps an archived record.
    """
    return not classify_memory_policy(query)["history_allowed"]


def emit_explicit_no_history_receipt(hook_input: dict, prompt: str, config: dict, *, invocation_id: str,
                                     policy: dict | None = None) -> None:
    """Persist a truthful self-contained zero without touching Hindsight."""
    session_id = str(hook_input.get("session_id") or "unknown")
    query_id = invocation_id
    payload = {
        "query_id": query_id,
        "stage": "injection",
        "session_id": session_id,
        "turn_id": hook_input.get("turn_id"),
        **feedback_turn_identity(prompt, invocation_id, hook_input.get("memory_prompt_origin") or "user_direct"),
        "execution_id": query_id,
        "raw_user_prompt": prompt,
        "bank_query_count": 0,
        "execution_mode": "shadow_replay" if is_shadow_replay() else "production",
        "bank_id": derive_bank_id(hook_input, config),
        "injected_ids": [],
        "injected_items": [],
        "candidate_items": [],
        "rejected_items": [],
        "deferred_items": [],
        "injected_count": 0,
        "actual_injected_count": 0,
        "candidate_count": 0,
        "rejected_count": 0,
        "deferred_count": 0,
        "injection_receipt_state": "verified_empty",
        "packet_delivery": {"state": "not_delivered_empty_packet", "transport_record_ids": []},
        "memory_action": "noop_long_term",
        "use_status": "not_applicable",
        "reason": "用户明确要求只根据当前内容；未执行历史检索，也未注入长期记忆。",
        "feedback_id": "no-history:" + invocation_id,
    }
    policy = policy or classify_memory_policy(prompt)
    guidance_forbidden = policy.get('guidance_memory_policy') == 'forbidden'
    payload['memory_policy'] = policy
    payload['memory_needs']={'facts':{'needed':False,'reason':'explicitly_forbidden'},'guidance':{'needed':not guidance_forbidden,'reason':'explicitly_forbidden' if guidance_forbidden else 'collaboration_preferences_still_allowed'}}
    payload['guidance_sidecar']={'enabled':not guidance_forbidden,'observation':False,'mental_model':False}
    payload['source_guard']={'active':True,'kind':'explicit_no_history','reason':payload['reason']}
    from lib.memory_needs import guidance_receipt
    payload['guidance_receipt']=guidance_receipt(payload,[],[])
    publish_injection('',payload,config)
    state = {
        "context": "",
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "session_id": session_id,
        "turn_id": hook_input.get("turn_id"),
        **feedback_turn_identity(prompt, invocation_id, hook_input.get("memory_prompt_origin") or "user_direct"),
        "bank_query_count": 0,
        "query_id": query_id,
        "bank_id": payload["bank_id"],
        "result_count": 0,
        "memory_action": "noop_long_term",
        "injection_receipt_state": "verified_empty",
        "actual_injected_count": 0,
        "injected_count": 0,
        "candidate_count": 0,
        "rejected_count": 0,
        "deferred_count": 0,
        "packet_delivery": payload["packet_delivery"],
        "injected_ids": [],
        "injected_items": [],
        "candidate_items": [],
        "rejected_items": [],
        "pipeline_stages": {
            "candidate_ledger": {"candidate_count": 0},
            "packet_delivery": payload["packet_delivery"],
        },
        "source_guard": {"active": True, "kind": "explicit_no_history", "reason": payload["reason"]},
        "memory_needs": payload['memory_needs'],
        "guidance_receipt": payload['guidance_receipt'],
    }
    persist_hook_state(LAST_RECALL_STATE, state)
    persist_hook_state(session_recall_state_name(session_id), state)


def is_shadow_replay() -> bool:
    """Whether this invocation is an isolated cassette replay.

    A replay must exercise the same Hook logic, but it must never write a
    Codex session state, submit correction feedback, or be mistaken for a
    user-visible production turn.  The Controller still receives a trace with
    an explicit mode so the status page can be audited separately.
    """
    mode = os.environ.get("EVOLVING_PROFILE_EXECUTION_MODE") or os.environ.get("HINDSIGHT_EXECUTION_MODE", "")
    return mode.strip().casefold() in {
        "replay", "shadow_replay", "cassette_replay"
    }


SHADOW_REPLAY_FIXTURE_ROOT = Path(HAM_SOURCE_ROOT) / "tests" / "fixtures" / "hindsight-replay"
LEGACY_SHADOW_REPLAY_FIXTURE_ROOT = Path("/Users/apple/Documents/Codex/2026-07-11/ag/hindsight-memory-os/tests/fixtures/hindsight-replay")


def allow_shadow_fixture_transcript(transcript_path: str) -> bool:
    """Allow a context-bearing replay only from the committed test-fixture root.

    Normal shadow replays deliberately ignore transcripts: a locally active
    conversation must never be accidentally evaluated as if it were a Bank
    recall.  The realistic replay harness needs a narrow exception for static,
    versioned fixtures that model the actual Hook envelope.  Both the explicit
    environment opt-in and the path boundary are required.
    """
    transcript_opt_in = os.environ.get("EVOLVING_PROFILE_SHADOW_REPLAY_WITH_TRANSCRIPT") or os.environ.get("HINDSIGHT_SHADOW_REPLAY_WITH_TRANSCRIPT")
    if not is_shadow_replay() or transcript_opt_in != "1":
        return False
    try:
        path = Path(transcript_path).expanduser().resolve()
        roots = [SHADOW_REPLAY_FIXTURE_ROOT.resolve()]
        try:
            roots.append(LEGACY_SHADOW_REPLAY_FIXTURE_ROOT.resolve())
        except (OSError, RuntimeError):
            pass
        return any(str(path).startswith(str(root) + os.sep) for root in roots)
    except (OSError, RuntimeError):
        return False


def persist_hook_state(name: str, value: dict) -> None:
    """Persist production state or an isolated replay receipt."""
    if not is_shadow_replay():
        write_state(name, value)
        return
    try:
        target = Path.home() / ".evolving-profile/audit/replay-state" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def session_recall_state_name(session_id: str) -> str:
    value = hashlib.sha256(str(session_id or "unknown").encode("utf-8")).hexdigest()[:20]
    return f"last_recall-{value}.json"


def post_memory_feedback(config: dict, payload: dict) -> bool:
    """Submit local audit feedback; never block the user prompt on failure."""
    url = str(config.get("memoryEffectivenessFeedbackUrl") or "http://127.0.0.1:12079/v1/feedback")
    headers = {"Content-Type": "application/json", "X-Memory-Client": "codex-hook"}
    if is_shadow_replay():
        headers["X-Memory-Execution-Mode"] = "shadow_replay"
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=1.5) as response:
            return 200 <= response.status < 300
    except (OSError, urllib.error.URLError):
        return False


def ham_shadow_canary(hook_input: dict, query: str, v1_results: int) -> None:
    """Run a bounded v2 shadow query; it never changes the injected v1 response."""
    try:
        contract = json.loads(Path("~/.evolving-profile/memory-contract-v5.json").expanduser().read_text(encoding="utf-8")).get("memoryOS") or {}
        percent = int(contract.get("canaryShadowPercent", 0) or 0)
        if contract.get("phase") != "6" or percent <= 0:
            return
        session = str(hook_input.get("session_id") or "unknown")
        bucket = int(hashlib.sha256((session + "\n" + query).encode("utf-8")).hexdigest()[:8], 16) % 100
        if bucket >= min(100, percent):
            return
        cwd = str(hook_input.get("cwd") or "unknown")
        payload = {"execution_id": "canary-" + hashlib.sha256((session + query).encode()).hexdigest()[:24], "task_id": hashlib.sha256((session + "|" + cwd).encode()).hexdigest()[:24], "user_request": query[:4000], "remaining_context_tokens": 1200}
        request = urllib.request.Request("http://127.0.0.1:12079/v2/memory-os/query", data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers={"Content-Type": "application/json", "X-HAM-Adapter": "codex"}, method="POST")
        with urllib.request.urlopen(request, timeout=0.75) as response:
            value = json.loads(response.read().decode("utf-8"))
        record = {"at": datetime.now(timezone.utc).isoformat(), "schema": "ham.canary.shadow.v1", "bucket": bucket, "percent": percent, "v1_result_count": v1_results, "v2_evidence_count": value.get("progress", {}).get("available_evidence_count"), "v2_execution_status": value.get("execution_status"), "v2_coverage_status": value.get("coverage_status"), "trace_id": value.get("trace_id")}
        target = Path("~/.evolving-profile/audit/ham-canary-shadow.jsonl").expanduser(); target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as out: out.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    except Exception:
        return

def start_ham_shadow_canary(hook_input: dict, query: str, v1_results: int) -> None:
    if is_shadow_replay():
        return
    threading.Thread(target=ham_shadow_canary, args=(hook_input, query, v1_results), daemon=True).start()

def feedback_item(item: dict) -> dict:
    metadata = item.get("metadata") or {}
    scores = item.get("scores") or {}
    # Controller rejection/defer receipts are already compact summaries.  They
    # intentionally expose ``text_preview``, ``admission`` and entity/graph
    # receipts at the top level rather than reconstructing the full Bank row.
    # Treat that shape as first-class input: feeding it through this Hook
    # projection used to erase both the candidate text and the rejection cause,
    # leaving 9998 with an unexplained ID and an empty reason.
    text = " ".join(str(
        item.get("text") or item.get("content") or item.get("text_preview") or ""
    ).split())
    stable_id = str(item.get("id") or item.get("chunk_id") or "")[:120]
    # Deterministic authority snapshots and governance entries are real prompt
    # injections too, even when they do not originate from a Hindsight row with
    # a UUID.  Give them a stable local provenance ID so the UI never shows
    # “0 injected” after Hook actually supplied such a rule to the model.
    if not stable_id:
        stable_id = "local:" + hashlib.sha256(
            (str(metadata.get("source") or "local") + "\n" + text).encode("utf-8")
        ).hexdigest()[:24]
    admission = dict(metadata.get("_ccy_admission") or item.get("admission") or {})
    # Keep the section boundary that crossed (or failed) the admission gate.
    # The UI needs this to distinguish "the business mental model was routed"
    # from "this exact heading-bounded section was injected".  Without these
    # fields a future audit can only see a mental-model ID and may incorrectly
    # assume that the whole synthesized profile entered the prompt.
    if metadata.get("mental_model_section") is not None:
        admission.update({
            "mental_model_route": metadata.get("mental_model_route"),
            "mental_model_parent_id": metadata.get("mental_model_parent_id"),
            "mental_model_section": metadata.get("mental_model_section"),
            "mental_model_section_gate": True,
        })
    entity_anchor = dict(metadata.get("_ccy_entity_anchor") or item.get("entity_anchor") or {})
    # Identity correction is a governed, deterministic entity-resolution path.
    # It does not pretend that a graph traversal happened, but the receipt must
    # still say why the entity fact was allowed through instead of displaying an
    # empty entity column on a real injected identity answer.
    if str(metadata.get("source") or "") == "identity-authority" and not entity_anchor:
        entity_anchor = {
            "matched": True,
            "route": "deterministic_identity_alias_authority",
            "reason": "已确认姓名与语音错别名的受治理实体规则。",
        }
    return {
        "id": stable_id,
        "type": str(item.get("type") or item.get("memory_type") or "unknown")[:40],
        "text_preview": text[:240],
        "document_id": str(item.get("document_id") or "")[:160],
        "source": str(metadata.get("source") or item.get("source") or "")[:120],
        "mentioned_at": item.get("mentioned_at") or item.get("occurred_start"),
        "score": scores.get("final") or item.get("score"),
        "admission": admission,
        # Preserve controller entity evidence in the injection receipt.  The
        # Hook formats only the text for Codex, but the local audit must retain
        # which entity was used to prioritize this row.
        "entities": [str(value)[:160] for value in (item.get("entities") or [])[:12]],
        "entity_anchor": entity_anchor,
        # Graph provenance is distinct from semantic retrieval.  Preserve it
        # in the Hook receipt so the 9998 inspector can truthfully show which
        # memories entered through an official constellation/relationship
        # route rather than merely claiming a graph was consulted.
        "graph_evidence": dict(metadata.get("_ccy_graph_evidence") or item.get("graph_evidence") or {}),
        "context_coordination": dict(metadata.get("_ccy_context_coordination") or item.get("context_coordination") or {}),
    }


def empty_injection_receipt_fields(
    *,
    candidate_items: Optional[list[dict]] = None,
    rejected_items: Optional[list[dict]] = None,
    deferred_items: Optional[list[dict]] = None,
    reason: str,
    receipt_state: str = "verified_empty",
) -> dict:
    """Return an explicit, auditable zero-delivery receipt.

    Older early-return branches only wrote ``injected_ids=[]``.  That made a
    genuine empty Packet indistinguishable from a missing/legacy Hook receipt
    in 9998.  Keep every boundary count explicit, including a transport state,
    without inventing a memory record or turning a timeout into a normal zero.
    """
    candidates = list(candidate_items or [])
    rejected = list(rejected_items or [])
    deferred = list(deferred_items or [])
    return {
        "actual_injected_count": 0,
        "actual_hook_injected_record_ids": [],
        "injected_count": 0,
        "candidate_count": len(candidates),
        "rejected_count": len(rejected),
        "deferred_count": len(deferred),
        # A timeout/failure is not a legitimate empty result. Callers may
        # override the state so the status page never collapses an upstream
        # failure into an apparently valid zero-memory decision.
        "injection_receipt_state": receipt_state,
        "packet_delivery": {
            "state": "not_delivered_empty_packet",
            "transport_record_ids": [],
            "deferred_claim_ids": [],
            "reason": reason,
        },
    }


def canonical_pipeline_item(item: dict) -> dict:
    """Give every Packet candidate the same stable record ID used in audit.

    Hook-owned authorities are legitimate context records but some legacy
    builders supplied only text and source.  Letting the packet call these
    ``unknown-claim`` made the rendered context and the actual-injection ledger
    disagree.  Canonicalise once, before packet construction, so transport,
    receipt and UI all use one record identity.
    """
    row = dict(item or {})
    if str(row.get("id") or row.get("chunk_id") or ""):
        return row
    metadata = dict(row.get("metadata") or {})
    text = str(row.get("text") or row.get("content") or "")
    source = str(metadata.get("source") or row.get("source") or "local")
    row["id"] = "local:" + hashlib.sha256(
        (source + "\n" + text).encode("utf-8")
    ).hexdigest()[:24]
    return row


def _unique_pipeline_items(items: list[dict]) -> list[dict]:
    """Deduplicate audit rows without changing their ranked order."""
    output, seen = [], set()
    for raw in items or []:
        item = dict(raw or {})
        key = str(item.get("id") or item.get("chunk_id") or "")
        if not key:
            key = hashlib.sha256(json.dumps(item, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:24]
        if key in seen:
            continue
        seen.add(key)
        output.append(item)
    return output


def build_memory_pipeline_stages(
    controller_receipt: Optional[dict],
    *,
    candidate_items: list[dict],
    hook_admitted_items: list[dict],
    hook_rejected_items: list[dict],
    deferred_items: list[dict],
    injected_items: list[dict],
) -> dict:
    """Build the owner-auditable four-stage retrieval-to-delivery ledger.

    Counts at different boundaries are deliberately not collapsed.  In
    particular, a late Controller helper can produce candidates without ever
    crossing the Hook/Memory Packet boundary for the current turn.
    """
    receipt = dict(controller_receipt or {})
    upstream = dict(receipt.get("pipeline_stages") or {})
    retrieval = dict(upstream.get("retrieval") or {})
    admission = dict(receipt.get("relevance_admission") or {})
    candidates = _unique_pipeline_items(candidate_items)
    hook_admitted = _unique_pipeline_items(hook_admitted_items)
    hook_rejected = _unique_pipeline_items(hook_rejected_items)
    deferred = _unique_pipeline_items(deferred_items)
    delivered = _unique_pipeline_items(injected_items)
    delivered_ids = {str(item.get("id") or "") for item in delivered}
    not_delivered = [
        item for item in hook_admitted
        if str(item.get("id") or "") not in delivered_ids
    ]
    retrieval.setdefault("raw_result_count", int(admission.get("candidate_count") or len(candidates)))
    retrieval.setdefault("fused_candidate_count", int(admission.get("candidate_count") or len(candidates)))
    retrieval.setdefault("candidate_items", candidates)
    retrieval["recorded_item_count"] = len(retrieval.get("candidate_items") or [])
    controller_stage = {
        "input_count": int(admission.get("candidate_count") or retrieval.get("fused_candidate_count") or len(candidates)),
        "qualified_count": int(admission.get("qualified_count") or admission.get("admitted_count") or 0),
        "eligible_after_budget_count": int(admission.get("injected_eligible_count") or 0),
        "rejected_count": int(admission.get("rejected_count") or 0),
        "deferred_count": int(admission.get("deferred_for_token_budget_count") or 0),
        "rejected_items": list(admission.get("rejected_items") or []),
        "deferred_items": list(admission.get("deferred_items") or []),
        "semantic_reranker_error": admission.get("semantic_reranker_error"),
        "policy": admission.get("policy"),
    }
    hook_input_items = _unique_pipeline_items(hook_admitted + hook_rejected)
    # The Hook candidate ledger can contain a small number of deterministic
    # sidecars that are appended after the Controller's ranked/admission set
    # (for example a local policy or an authority snapshot).  They are real
    # Hook inputs, but they must not be silently folded into the Controller
    # denominator.  Older receipts exposed only one ``candidate_count`` and
    # therefore made an honest 83 -> 84 boundary look like a broken counter.
    # Canonicalise both sets with the same projection used by the delivery
    # receipt, then expose the boundary explicitly for the owner UI.
    upstream_controller_items = list(retrieval.get("candidate_items") or [])
    upstream_controller_items.extend(retrieval.get("sidecar_candidate_items") or [])
    controller_candidate_ids = []
    for item in upstream_controller_items:
        item_id = feedback_item(item).get("id")
        if item_id and item_id not in controller_candidate_ids:
            controller_candidate_ids.append(item_id)
    hook_candidate_ids = []
    for item in candidates:
        item_id = feedback_item(item).get("id")
        if item_id and item_id not in hook_candidate_ids:
            hook_candidate_ids.append(item_id)
    controller_id_set = set(controller_candidate_ids)
    hook_only_candidate_ids = [item_id for item_id in hook_candidate_ids if item_id not in controller_id_set]
    candidate_ledger = {
        "schema": "hindsight.candidate_boundary.v1",
        "controller_candidate_count": int(controller_stage["input_count"]),
        "controller_candidate_ids_count": len(controller_candidate_ids),
        "hook_candidate_count": len(hook_candidate_ids),
        "hook_only_candidate_count": len(hook_only_candidate_ids),
        "hook_only_candidate_ids": hook_only_candidate_ids,
        "meaning": "Controller 候选是主检索与准入分母；Hook 候选可额外包含本地治理侧路。两者不同不代表注入丢失。",
    }
    return {
        "schema": "hindsight.memory_pipeline_stages.v1",
        "candidate_ledger": candidate_ledger,
        "retrieval": retrieval,
        "controller_admission": controller_stage,
        "hook_postprocessing": {
            "input_count": len(hook_input_items),
            "admitted_count": len(hook_admitted),
            "rejected_count": len(hook_rejected),
            "deferred_count": len(deferred),
            "rejected_items": hook_rejected,
            "deferred_items": deferred,
        },
        "packet_delivery": {
            "input_count": len(hook_admitted),
            "delivered_count": len(delivered),
            "delivered_items": delivered,
            "not_delivered_count": len(not_delivered),
            "not_delivered_items": not_delivered,
            "boundary": "codex_hook_additional_context",
        },
    }


def build_claim_delivery_receipt(
    controller_receipt: Optional[dict],
    *,
    injected_items: list[dict],
    candidate_items: list[dict],
    pipeline_stages: Optional[dict] = None,
) -> dict:
    """Join the Controller's claim ledger to the Hook's actual delivery event.

    The Controller can say which source records supported a distinct claim; it
    cannot say what Codex received.  This function runs only after the Hook has
    built `injected_items`, so the resulting receipt never mistakes a retrieved
    or admitted record for actual context delivery.
    """
    controller_receipt = dict(controller_receipt or {})
    source = dict(controller_receipt.get("claim_receipt") or {})
    claims = [dict(row) for row in (source.get("claims") or []) if isinstance(row, dict)]
    injected_ids = list(dict.fromkeys(
        str(item.get("id") or "") for item in injected_items if str(item.get("id") or "")
    ))

    def claim_match_keys(value: str) -> tuple[str, str, bool]:
        """Compare duplicate claim text across formatting/case drift.

        Claim IDs are rendered statements rather than immutable record IDs.
        The same Bank proposition can therefore appear once with a space at a
        Chinese/Latin boundary (``Hindsight 当前``) and once without it
        (``Hindsight当前``).  Treating those as different keys makes the
        delivery receipt report a false ``not_delivered`` even when the
        supporting record crossed the Hook boundary.  This key is only for
        receipt reconciliation; it does not merge or rank memory records.
        """
        # Claim IDs are rendered statements followed by optional provenance
        # (``| involving: ...``/``| 规范...``).  Provenance belongs in the
        # evidence record, not in the semantic identity used for delivery
        # reconciliation.  Taking the statement segment prevents a duplicate
        # with a different provenance suffix from being reported as missing.
        statement = re.split(r"\s*\|\s*", str(value or ""), maxsplit=1)[0]
        compact = re.sub(r"\s+", "", statement).casefold()
        # Chinese renderers may emit optional copula/function words such as
        # “规定为” vs “规定”.  Normalize only these grammar fillers; record
        # contents, ranking and actual Packet IDs remain unchanged.
        # ``规定：`` and ``规定为：`` are the same copula rendered by two
        # claim compilers; canonicalize both to ``为``.  This is deliberately
        # limited to predicate grammar, never arbitrary content words.
        compact = re.sub(r"(规定|定义|称为)为", "为", compact)
        compact = re.sub(r"(规定|定义|称为)(?=[:：])", "为", compact)
        # Bank rows can differ only by an optional full stop immediately
        # before the provenance separator (``。| when`` vs ``| when``).
        # Remove that formatting-only punctuation before comparing; the
        # separator itself remains part of the claim shape so unrelated
        # statements are not merged by this receipt-only normalizer.
        compact = re.sub(r"[。．\.、，,；;：:!?！？]+(?=\|)", "", compact)
        compact = re.sub(r"[。．\.、，,；;：:!?！？]+$", "", compact)
        # Keep an explicit version in the primary key so v2 and v3 claims
        # cannot be silently merged.  A secondary key drops only the leading
        # product/version prefix; it is used for an unversioned generic claim
        # (``迁移阶段顺序``) when a delivered versioned record covers it.
        body = re.sub(r"^hindsight", "", compact)
        versioned = bool(re.match(r"^(?:hindsight)?v\d+(?:\.\d+)*", compact))
        body_no_version = re.sub(r"^v\d+(?:\.\d+)*", "", body)
        return compact, body_no_version, versioned

    def claim_match_key(value: str) -> str:
        # Kept as a tiny local wrapper for readability at call sites; the
        # secondary/fallback key logic below is handled explicitly so an
        # explicit v2 claim cannot match an unrelated v3 claim.
        return claim_match_keys(value)[0]
    candidate_ids = []
    for item in candidate_items:
        item_id = feedback_item(item).get("id")
        if item_id and item_id not in candidate_ids:
            candidate_ids.append(item_id)
    candidate_ledger = dict((pipeline_stages or {}).get("candidate_ledger") or {})
    controller_candidate_count = int(
        candidate_ledger.get("controller_candidate_count")
        or (controller_receipt.get("relevance_admission") or {}).get("candidate_count")
        or (controller_receipt.get("pipeline_stages") or {}).get("controller_admission", {}).get("input_count")
        or 0
    )
    hook_candidate_count = len(candidate_ids)
    hook_only_candidate_count = int(
        candidate_ledger.get("hook_only_candidate_count")
        if candidate_ledger.get("hook_only_candidate_count") is not None
        else max(0, hook_candidate_count - controller_candidate_count)
    )
    injected_set = set(injected_ids)
    # A Controller claim can cite a whole mental model while the Hook safely
    # transports only its heading-bounded child section.  Treat the declared
    # parent model ID as delivered evidence for claim accounting, while keeping
    # the child ID as the exact Packet record shown to users.
    delivered_evidence_ids = set(injected_set)
    for item in injected_items:
        metadata = dict(item.get("metadata") or {})
        admission = dict(item.get("admission") or metadata.get("_ccy_admission") or {})
        parent_id = str(admission.get("mental_model_parent_id") or metadata.get("mental_model_parent_id") or "")
        if parent_id:
            delivered_evidence_ids.add(parent_id)
    admitted: list[str] = []
    not_delivered: list[str] = []
    evidence_ids: set[str] = set()
    delivered_claim_keys: set[str] = set()
    delivered_unversioned_bodies: set[str] = set()
    # Compute delivered claim keys first.  A duplicate claim may be ordered
    # before its delivered sibling; a single pass would then leave a stale
    # false-positive in ``not_delivered_claim_ids``.
    for claim in claims:
        claim_id = str(claim.get("claim_id") or claim.get("claim_key") or "")
        supporting_ids = {
            str(value) for value in (claim.get("evidence_ids") or []) if str(value)
        }
        if claim_id and supporting_ids.intersection(delivered_evidence_ids):
            primary, body_no_version, _versioned = claim_match_keys(claim_id)
            delivered_claim_keys.add(primary)
            delivered_unversioned_bodies.add(body_no_version)
    for claim in claims:
        claim_id = str(claim.get("claim_id") or claim.get("claim_key") or "")
        supporting_ids = {
            str(value) for value in (claim.get("evidence_ids") or []) if str(value)
        }
        evidence_ids.update(supporting_ids)
        if claim_id and supporting_ids.intersection(delivered_evidence_ids):
            admitted.append(claim_id)
        elif claim_id:
            primary, body_no_version, versioned = claim_match_keys(claim_id)
            # An unversioned generic proposition may be covered by a
            # delivered versioned sibling; an explicit version must match its
            # exact versioned key and never fall back across versions.
            covered_by_generic_fallback = (not versioned) and body_no_version in delivered_unversioned_bodies
            if primary not in delivered_claim_keys and not covered_by_generic_fallback:
                not_delivered.append(claim_id)
    return {
        "schema": "ham.claim_delivery_receipt.v1",
        "execution_id": str(source.get("execution_id") or controller_receipt.get("execution_id") or controller_receipt.get("query_id") or ""),
        "retrieved_claim_count": len(claims),
        # This Hook receipt exposes the complete pre-delivery candidate ledger;
        # its count must use the same unit as `candidate_record_ids`.  The
        # Controller claim compiler sees only admitted results, so retain that
        # narrower input count under an explicit field instead of overwriting
        # the browser-visible candidate cardinality.
        "candidate_record_count": len(candidate_ids),
        "controller_candidate_record_count": controller_candidate_count,
        "hook_candidate_record_count": hook_candidate_count,
        "hook_only_candidate_count": hook_only_candidate_count,
        "candidate_boundary_scope": "controller_admission_plus_hook_sidecars",
        "controller_claim_input_count": int(source.get("candidate_record_count") or 0),
        "candidate_record_ids": candidate_ids,
        "actual_hook_injected_record_ids": injected_ids,
        "actual_hook_injected_claim_ids": admitted,
        # These claims were supported by Controller-admitted records but did
        # not survive Hook post-processing.  They stopped before Packet
        # construction; they are not transport failures.  Keep the legacy
        # ``not_delivered_claim_ids`` field for API compatibility and expose a
        # precise alias so the owner UI and validators cannot conflate the two
        # boundaries.
        "controller_admitted_not_packet_claim_ids": not_delivered,
        "controller_admitted_not_packet_count": len(not_delivered),
        "not_delivered_claim_ids": not_delivered,
        "non_claim_injection_ids": [item_id for item_id in injected_ids if item_id not in evidence_ids],
        "reason": "仅当某命题的支撑记录实际进入 Hook 上下文时，才记为本次实际注入命题；Controller 准入后未进入 Packet 与 Packet 传输未投递分开记录。",
    }


def packet_delivery_source_items(
    results: list[dict], evidence_results: list[dict], mental_section_items: list[dict], authority_packet_items: list[dict],
) -> list[dict]:
    """Return the exact source rows eligible for an actual Packet receipt.

    Packet construction uses heading-bounded ``mental_section_items``. The
    receipt must use that same list, rather than the legacy synthesized
    ``mental_model`` object, or a genuinely transported mental-model section
    is falsely displayed as absent from the Hook context.
    """
    return _unique_pipeline_items(
        list(results) + list(evidence_results) + list(mental_section_items) + list(authority_packet_items)
    )


def explicit_correction(prompt: str) -> bool:
    normalized = re.sub(r"\s+", "", str(prompt or "")).casefold()
    markers = (
        "不对", "不是这个意思", "你记错", "记错了", "错了", "纠正一下", "我纠正",
        "以这个为准", "以附件为准", "不要用前面", "前面的不对", "最新版",
    )
    # Mentioning an old version as an analysis object (for example, asking how
    # to arbitrate old and new evidence) is not itself a correction.  Only an
    # explicit replacement/negation may activate source-first and suppress
    # long-term recall.
    old_version_correction = any(marker in normalized for marker in (
        "旧版本不对", "旧版本错误", "旧版本作废", "旧版本不用", "不要旧版本", "以新版本为准",
    ))
    return old_version_correction or any(marker in normalized for marker in markers)


def extract_current_source_paths(prompt: str) -> list[str]:
    """Find user-supplied local attachment paths without opening or indexing them."""
    # Adapter transaction context is background/rubric, not a newly uploaded
    # user attachment. Keep the original prompt untouched for grading/recall.
    # Only the authority-path classification excludes this known envelope.
    from lib.input_origin import user_surface_text
    prompt = user_surface_text(prompt)
    found = []
    for raw in re.findall(r"(?m)^##[^\n]*?:\s*(/[^\n<>]+)$", str(prompt or "")):
        value = raw.strip().rstrip("`。，,;；")
        if value and value not in found:
            found.append(value)
    for raw in re.findall(r"(?<![A-Za-z0-9])(/(?:tmp|var|Users)/[^\n<>\"']+?\.(?:pdf|docx|xlsx|pptx|png|jpg|jpeg|webp|csv|md|txt))", str(prompt or ""), flags=re.I):
        value = raw.strip().rstrip("`。，,;；")
        if value and value not in found:
            found.append(value)
    return found[:8]


def apply_project_runtime_headers(client, hook_input: dict, prompt: str, session_id: str) -> dict:
    cwd = str(hook_input.get("cwd") or "")[:1024]
    paths = extract_current_source_paths(prompt)
    correction = explicit_correction(prompt)
    label = "、".join(Path(path).name for path in paths)[:360]
    fingerprint = hashlib.sha256("\n".join(paths).encode("utf-8")).hexdigest()[:24] if paths else ""
    client.request_headers.update({
        "X-Memory-Project-Cwd": urllib.parse.quote(cwd, safe=""),
        "X-Memory-Session-Id": str(session_id)[:160],
        "X-Memory-User-Correction": "1" if correction else "0",
        # Make source authority invocation-scoped.  Some adapters reuse a
        # mutable HTTP client while moving from an attachment turn to an
        # ordinary text turn; an old ``Source-Kind: attachment`` header must
        # never leak into the next request and activate source-first.
        "X-Memory-Source-Present": "1" if paths else "0",
    })
    if paths:
        client.request_headers.update({
            "X-Memory-Source-Kind": "attachment",
            "X-Memory-Source-Label": urllib.parse.quote(label, safe=""),
            "X-Memory-Source-Fingerprint": fingerprint,
        })
    else:
        for key in (
            "X-Memory-Source-Kind",
            "X-Memory-Source-Label",
            "X-Memory-Source-Fingerprint",
        ):
            client.request_headers.pop(key, None)
    return {
        "project_key": hashlib.sha256(cwd.encode("utf-8")).hexdigest()[:16] if cwd else "",
        "source_paths": paths,
        "source_label": label,
        "source_authority": bool(paths),
        "user_correction": correction,
    }


def classify_recall_profile(query: str) -> str:
    normalized = re.sub(r"\s+", "", str(query or "")).lower()
    explicit_deep = (
        "深度召回", "完整回顾", "完整回忆", "全面回顾", "全面回忆", "全量召回", "全部历史", "所有历史",
        "跨会话复盘", "跨项目复盘", "完整时间线", "深入回顾",
    )
    historical_anchor = ("历史", "以前", "当时", "跨会话", "跨项目", "时间线", "回顾", "复盘")
    breadth_cue = ("所有", "全部", "完整", "整体", "全面", "根因", "对比", "脉络")
    # A relation/closure question is not a one-fact “current” check merely
    # because it contains words such as 当前.  It needs the ordinary bounded
    # recall window so the Controller can read the official graph lane; the
    # 15-second precise profile could abort before graph evidence returned.
    closure_cue = ("关联", "关系", "联动", "别名", "全称", "接管", "迁移", "回滚", "上下游", "前后步骤", "因果链")
    if any(term in normalized for term in explicit_deep) or (
        any(term in normalized for term in historical_anchor)
        and any(term in normalized for term in breadth_cue)
    ):
        return "deep"
    if any(term in normalized for term in closure_cue):
        return "normal"
    if any(term in normalized for term in (
        "我叫什么", "姓名", "当前", "现在", "是否开启", "哪个通道", "发给我",
    )) or len(normalized) <= 16:
        return "precise"
    return "normal"


def adaptive_recall_settings(query: str, config: dict) -> dict:
    profile = classify_recall_profile(query) if config.get("adaptiveRecallEnabled", True) else "normal"
    defaults = {
        "precise": {"budget": "low", "maxTokens": 700, "timeout": min(15, int(config.get("recallTimeout", 20)))},
        "normal": {"budget": config.get("recallBudget", "mid"), "maxTokens": config.get("recallMaxTokens", 1024), "timeout": int(config.get("recallTimeout", 20))},
        "deep": {"budget": "high", "maxTokens": 1800, "timeout": int(config.get("deepRecallTimeout", 175))},
    }
    value = dict(defaults[profile])
    value.update((config.get("adaptiveRecallProfiles") or {}).get(profile) or {})
    return {"profile": profile, "budget": value["budget"], "max_tokens": int(value["maxTokens"]), "timeout": int(value.get("timeout", config.get("recallTimeout", 20)))}


def reconcile_recall_settings_with_plan(settings: dict, plan: dict, config: dict) -> dict:
    """Let the full Controller plan widen an early lexical Hook estimate.

    The Hook classifies before it has the Controller's multi-facet plan.  A
    long inventory request can contain a word such as ``现在`` and therefore
    receive the 15-second precise timeout even though the Controller has an
    18-second complex deadline.  In that state a valid response is guaranteed
    to be vulnerable to atomic loss at the transport boundary.  The plan owns
    breadth, budget and deadline; the early classifier remains a lower bound.
    """
    result = dict(settings or {})
    plan = dict(plan or {})
    shape = str(plan.get("primary_shape") or "point").casefold()
    result.update({
        "profile": f"shape:{shape}",
        "budget": plan.get("budget") or result.get("budget") or "mid",
        "max_tokens": int(plan.get("max_tokens") or result.get("max_tokens") or 1200),
    })
    complex_plan = bool(
        shape in {"current", "timeline", "inventory", "synthesis", "audit", "conflict", "missing", "procedure"}
        and (
            int(plan.get("query_count") or 0) > 1
            or bool(plan.get("coverage_required"))
            or len(plan.get("matched_shapes") or []) > 1
        )
    )
    uncertain_plan = bool(plan.get("controller_fallback"))
    explicit_deep = bool(plan.get("explicit_deep_recall")) or str(
        settings.get("profile") or ""
    ).casefold() == "deep"
    result["controller_profile"] = "deep" if explicit_deep else (
        "normal" if (complex_plan or uncertain_plan) else str(settings.get("profile") or "normal")
    )
    if explicit_deep:
        # The Controller has identified a complete origin-to-present / deep
        # evidence task.  It is unsafe to retain an earlier lexical `precise`
        # 15-second transport timeout: that silently drops a valid Controller
        # result before the Hook can inject it.  Respect the configured deep
        # ceiling, leaving a small reserve for serialising the Hook receipt.
        deadline_seconds = (int(plan.get("deadline_ms") or 0) + 999) // 1000
        reserve = max(1, int(config.get("recallTransportReserveSeconds", 4)))
        deep_cap = max(20, int(config.get("deepRecallTimeout", 185)))
        result["timeout"] = min(deep_cap, max(
            int(result.get("timeout") or 0),
            deadline_seconds + reserve,
        ))
    if (complex_plan or uncertain_plan) and not explicit_deep:
        deadline_seconds = (int(plan.get("deadline_ms") or 0) + 999) // 1000
        # The Controller's deadline covers retrieval/reranking.  Building the
        # auditable admission ledger, appending it, serialising the four-stage
        # receipt and transferring it to the Hook happen afterwards.  Real
        # broad relation queries produce a few hundred KB of receipt data, so
        # two seconds was not a safe boundary and turned completed six-item
        # recalls into zero-item socket timeouts.  Four seconds stays inside
        # the 22-second foreground cap and is only paid by complex/deep work.
        reserve = max(1, int(config.get("recallTransportReserveSeconds", 4)))
        # Leave the outer 25-second Codex Hook several seconds for formatting,
        # feedback and stdout.  Deep work uses the existing continuation lane.
        foreground_cap = max(18, int(config.get("foregroundRecallMaxTimeout", 22)))
        # A failed plan probe leaves the Hook unable to know the Controller's
        # real deadline or response size.  The Controller may still recognise
        # a relation/coverage query and use its complete 18-second execution
        # window.  Its admission ledger is serialised only after that work is
        # done, so an ordinary 20-second socket can discard a successfully
        # completed response at the transport boundary.  Use the bounded
        # foreground ceiling only for this uncertain path; known atomic point
        # plans keep their short timeout and known complex plans retain the
        # deadline-plus-reserve calculation below.
        if uncertain_plan:
            result["timeout"] = foreground_cap
        else:
            result["timeout"] = min(
                foreground_cap,
                max(
                    int(result.get("timeout") or 0),
                    deadline_seconds + reserve,
                ),
            )
    return result


def effective_query_plan(
    query_plan: Optional[dict], controller_receipt: Optional[dict]
) -> dict:
    """Return the route that actually executed, preserving probe diagnostics."""
    probe = dict(query_plan or {})
    actual = dict(controller_receipt or {})
    if not actual.get("primary_shape"):
        return probe
    merged = dict(probe)
    for key in (
        "primary_shape", "matched_shapes", "strategies", "coverage_dimensions",
        "scope_claim", "coverage_required", "memory_action", "query_count",
        "queries_requested", "queries_completed", "deadline_ms",
    ):
        if key in actual:
            merged[key] = actual[key]
    merged["planning_probe_fallback"] = bool(probe.get("controller_fallback"))
    merged["planning_probe_error"] = probe.get("controller_error")
    merged["controller_fallback"] = False
    merged["router"] = "memory-query-controller-response"
    return merged


def _invalid_mental_model(content: str, model: dict, config: dict) -> bool:
    value = str(content or "").strip()
    if not value:
        return True
    # Hindsight marks a model stale after any newer memory write.  Here the
    # refresh owner is evidence-gated (8 qualified observations or 24 hours),
    # so treating that advisory bit as a hard ban would hide every model after
    # almost every write.  Keep the last validated model usable with an
    # is_stale warning; dynamic source guards and invalid-pattern checks still
    # reject known obsolete content.
    if bool(model.get("is_stale")) and not config.get(
        "mentalModelAllowStaleWithinGate", True
    ):
        return True
    if any(pattern in value for pattern in config.get("mentalModelInvalidPatterns") or []):
        return True
    if config.get("mentalModelRequireSources", True):
        sources = model.get("sources") or (model.get("reflect_response") or {}).get("based_on") or {}
        if isinstance(sources, dict):
            sources = [item for values in sources.values() for item in (values or [])]
        if not sources:
            return True
    return False


def load_cached_mental_model(route: dict, config: dict) -> Optional[dict]:
    path = Path(os.path.expanduser(config.get(
        "mentalModelCachePath", "~/.evolving-profile/control-plane/mental-model-cache.json"
    )))
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    generated_at = payload.get("generated_at")
    max_age = int(config.get("mentalModelCacheMaxAgeSeconds", 7200))
    if not generated_at:
        return None
    try:
        generated = datetime.fromisoformat(str(generated_at).replace("Z", "+00:00"))
        if generated.tzinfo is None:
            generated = generated.replace(tzinfo=timezone.utc)
        if (datetime.now(timezone.utc) - generated).total_seconds() > max_age:
            return None
    except (TypeError, ValueError):
        return None
    model = (payload.get("models") or {}).get(route.get("mentalModelId"))
    return model if isinstance(model, dict) else None


def append_recall_audit(config: dict, payload: dict) -> None:
    path = Path(os.path.expanduser(config.get(
        "semanticAuditPath", "~/.evolving-profile/audit/recall-semantic.jsonl"
    )))
    if is_shadow_replay():
        path = Path.home() / ".evolving-profile/audit/replay-semantic.jsonl"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **payload}
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except OSError:
        pass


def record_prompt_ingress(config: dict, prompt: str, session_id: str, cwd: str, *, turn_id=None, hook_invocation_id=None, prompt_origin="user_direct", transcript_path=None, model=None, model_provider=None) -> None:
    """Append a local, model-free receipt before any recall branch can return.

    The receipt is deliberately separate from a Controller trace: it proves that
    the UserPromptSubmit hook saw the question, but never claims that a recall
    ran or that a memory was injected.
    """
    try:
        path = Path(os.path.expanduser(config.get(
            "promptIngressAuditPath", "~/.evolving-profile/audit/prompt-ingress.jsonl"
        )))
        normalized = " ".join(str(prompt or "").split())
        if not normalized:
            return
        row = {
            "at": datetime.now(timezone.utc).isoformat(),
            "event": "prompt_ingress",
            "session_id": str(session_id or "unknown"),
            "turn_id": turn_id,
            "hook_invocation_id": hook_invocation_id,
            "host_id": "codex",
            "cwd": str(cwd or ''),
            "transcript_path": str(transcript_path) if transcript_path else None,
            "model": str(model) if model else None,
            "model_provider": str(model_provider) if model_provider else None,
            "prompt_origin": prompt_origin,
            "project_key": hashlib.sha256(str(cwd or "").encode("utf-8")).hexdigest()[:16] if cwd else "",
            "prompt_preview": normalized[:600],
            "prompt_fingerprint": hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16],
            "source": "codex-userpromptsubmit",
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        # A receipt must never make the prompt hook fail.
        pass


def record_memory_route_probe(hook_input: dict, prompt: str, config: dict, invocation_id: str) -> dict:
    """Run the cheap catalog route and bind its evidence to this prompt."""
    try:
        root=Path(os.path.expanduser(config.get("memoryRouteReceiptRoot", "~/.evolving-profile/audit/memory-route-receipts")))
        root.mkdir(parents=True,exist_ok=True)
        normalized=" ".join(str(prompt or "").split())
        guidance_task=((ENTRY_GUIDANCE_RECEIPT or {}).get('request') or {}).get('task') or {}
        route_query=normalized
        if guidance_task.get('continuation') and str(guidance_task.get('context_summary') or '').strip():
            route_query += '\n必要前文：' + str(guidance_task.get('context_summary'))[:2000]
        status_url=str(config.get('memoryRouteStatusUrl') or os.environ.get('EVOLVING_PROFILE_STATUS_API_URL') or 'http://127.0.0.1:12098').rstrip('/')
        endpoint=status_url+'/api/guidance/memory-check?q='+urllib.parse.quote(route_query)
        request=urllib.request.Request(endpoint,headers={'Cache-Control':'no-store','X-Memory-Client':'evolving-profile-hook-catalog'})
        try:
            with urllib.request.urlopen(request,timeout=2.5) as response:
                value=json.loads(response.read(512*1024))
        except Exception as error:
            value={"schema":"evolving-profile.memory-check.v1","query":route_query,"decision":"unknown","recommended_route":"unknown",
                   "reason":"目录探针不可用；Codex仍按当前上下文和记忆说明判断。","matched_nodes":[],"catalog_hints":[],
                   "catalog_probe":{"status":"unavailable","candidate_count":None,"matched_entities":[],"error_type":type(error).__name__},
                   "confidence":0.0,"requires_receipt":True,"agent_may_override":True}
        value.update(check_id=invocation_id,raw_prompt=normalized,tool='memory_check',bank_detail_read=False,
            prompt_binding={"session_id":hook_input.get("session_id"),"turn_id":hook_input.get("turn_id"),"hook_invocation_id":invocation_id,
                            "prompt_fingerprint":hashlib.sha256(normalized.encode()).hexdigest()[:16],"prompt_preview":normalized[:600]})
        target=root/(hashlib.sha256(invocation_id.encode()).hexdigest()+".json");temporary=target.with_suffix(".tmp")
        temporary.write_text(json.dumps(value,ensure_ascii=False),encoding="utf-8");temporary.replace(target)
        return value
    except (OSError,ValueError,TypeError):
        return {}


def format_memory_route_context(value: dict) -> str:
    check_id=str(value.get('check_id') or '')
    route=str(value.get('recommended_route') or 'unknown')
    reason=str(value.get('reason') or '')
    probe=value.get('catalog_probe') or {}
    entities=[str(v) for v in probe.get('matched_entities') or []][:8]
    hints=[]
    for item in (value.get('catalog_hints') or [])[:3]:
        if not isinstance(item,dict):continue
        hints.append({k:item.get(k) for k in ('topic_id','title','abstract','overview','entities','time_range','source_count','coverage','pending_changes','conflicts','refreshed_at','boundary','memory_id','type','topic','mentioned_at','occurred_start','occurred_end','state') if item.get(k) is not None})
    payload={'check_id':check_id,'recommended_route':route,'reason':reason,'candidate_count':probe.get('candidate_count'),
             'catalog_status':probe.get('status'),'catalog_coverage':probe.get('catalog_coverage'),'matched_entities':entities,'catalog_hints':hints,'agent_may_override':True,
             'catalog_miss_does_not_prove_bank_absence':True}
    route_instruction = (
        "单点历史问题必须实际调用 user_recall；若为空、主体不匹配或时间/范围不足，升级一次 user_research。"
        if route == "recall" else
        "这是开放盘点或多实体时间线，必须实际调用 user_research，并继续分页或回读原文。"
        if route == "research" else
        "这是对用户已记录偏好、格式或协作习惯的询问；必须实际调用 user_preference，并按返回的适用条件和例外回答。"
        if route == "get_preference" else
        "这是实时链路审计，优先调用 audit_thread_history（若当前宿主已挂载）并读取 Hook 回执和 MCP 活动记录，不查询普通历史 Bank。"
        if route == "live_audit" else
        "当前任务可先依据现有上下文处理；若发现证据缺口，再调用相应历史工具。"
    )
    return ('<evolving_profile_memory_route check_id="'+check_id+'">\n'
            '本轮已执行低成本目录探针：'+json.dumps(payload,ensure_ascii=False)+'\n'
            + route_instruction + '目录只用于判断是否需要历史读取，不是事实证据；目录未命中不等于Bank不存在。空的 system_probe 只代表没有候选注入，不能代替实际 MCP 检索；此路线标为必需时，须先调用对应的 EP 工具，不能用本地 Codex Memory 作为无标记替代。'
            '\n</evolving_profile_memory_route>')


def feedback_turn_identity(prompt: str, hook_invocation_id: str, prompt_origin: str, *, turn_id=None) -> dict:
    """Return the immutable identity carried by every injection outcome.

    A zero, a timeout, and a populated Packet must be equally joinable to the
    originating UserPromptSubmit.  The status projection must never infer that
    relation by timestamp or reuse a nearby turn's receipt.
    """
    normalized = " ".join(str(prompt or "").split())
    identity = {
        "prompt_fingerprint": hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16] if normalized else "",
        "hook_invocation_id": str(hook_invocation_id or "")[:160],
        "prompt_origin": str(prompt_origin or "user_direct")[:40],
    }
    # A Hook invocation can span controller, explicit empty Packet and host
    # receipts.  Keep the host-provided turn id whenever it exists; a blank
    # synthetic value would overwrite explicit IDs in older callers.
    if turn_id:
        identity["turn_id"] = str(turn_id)[:160]
    return identity


def capture_hook_cassette(config: dict, hook_input: dict, raw_prompt: str, prompt: str) -> None:
    """Passively preserve the real Hook envelope for later isolated replay.

    It intentionally records the exact raw prompt *and* the extracted user
    request.  The latter makes it possible to detect an extraction mismatch;
    no replay is ever sent back into Codex.  The newest cassettes are bounded
    to keep this local audit from becoming a second memory store.
    """
    if is_shadow_replay() or not raw_prompt:
        return
    try:
        root = Path(os.path.expanduser(config.get("hookCassettePath", "~/.evolving-profile/audit/hook-cassettes")))
        root.mkdir(parents=True, exist_ok=True)
        event_id = uuid.uuid4().hex
        safe_input = {
            key: hook_input.get(key) for key in (
                "session_id", "cwd", "transcript_path", "prompt", "user_prompt",
                "turn_id", "event_id", "hook_invocation_id", "invocation_id",
                "source_sequence", "sequence", "occurred_at", "timestamp",
                "memory_prompt_origin", "memory_execution_mode",
            )
            if hook_input.get(key) is not None
        }
        row = {
            "schema": "hindsight.hook-cassette.v1",
            "event_id": event_id,
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "source": "codex-userpromptsubmit",
            "raw_prompt": raw_prompt,
            "extracted_prompt": prompt,
            "raw_prompt_fingerprint": hashlib.sha256(raw_prompt.encode("utf-8")).hexdigest(),
            "hook_input": safe_input,
        }
        (root / f"{event_id}.json").write_text(json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8")
        files = sorted(root.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True)
        for stale in files[240:]:
            stale.unlink(missing_ok=True)
    except OSError:
        pass


def reconcile_native_context_async(hook_input: dict) -> None:
    """Backfill only a missing Controller receipt from the local transcript.

    Some Codex-native memory injections bypass the local Controller process.
    This produces an explicit audit receipt on the next normal hook run rather
    than silently mislabelling the turn as a recall failure.
    """
    transcript = str(hook_input.get("transcript_path") or "")
    tool = Path.home() / ".evolving-profile/bin/hindsight-trace-reconcile.py"
    if not transcript or not Path(transcript).is_file() or not tool.is_file():
        return
    try:
        subprocess.run(
            [sys.executable, str(tool), "--session", transcript, "--tail", "240"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=0.75, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


# Deterministic delivery-channel guard.  This is intentionally local to the
# Codex recall hook: high-impact channel routing must not depend on semantic
# ranking of old conversation chunks.
def delivery_channel_guard(query: str) -> str:
    normalized = re.sub(r"\s+", "", str(query or "")).lower()
    delivery_terms = ("发给我", "发送", "投递", "发文件", "文件", "附件", "下载")
    has_delivery = any(term in normalized for term in delivery_terms)
    mentions_xiaodai = "小黛" in normalized or "xiaodai" in normalized
    mentions_wechat = any(term in normalized for term in ("微信", "wechat", "weixin", "企业微信"))
    if mentions_xiaodai and has_delivery:
        return (
            "<delivery_channel_guard>\n"
            "当前用户约定：‘通过小黛发给我’固定指飞书中的小黛私聊。"
            "先完成本地文件校验，再走小黛飞书通道并保存回执；飞书投递异常时仅保留本地文件和失败状态，"
            "不切换到微信、企业微信或任何 UI/坐标自动化通道。\n"
            "</delivery_channel_guard>"
        )
    if mentions_wechat and has_delivery:
        return (
            "<delivery_channel_guard>\n"
            "历史事件：微信文件自动投递曾触发客户端冷却。当前任务不得通过微信、企业微信或 UI 自动化投递文件；"
            "如需交付，使用已明确的飞书小黛通道或仅交付本地文件。\n"
            "</delivery_channel_guard>"
        )
    return ""


def select_evidence_bank(query: str, config: dict) -> Optional[dict]:
    """Choose at most one direct-user-evidence bank for this query.

    Routes are evaluated in configured order.  Requiring a routed keyword
    prevents broad archive search from injecting an unrelated quotation.
    """
    normalized = re.sub(r"\s+", "", query).lower()
    for route in config.get("evidenceFallbackRoutes") or []:
        if not isinstance(route, dict) or not route.get("bankId"):
            continue
        keywords = [str(value).replace(" ", "").lower() for value in route.get("keywords") or []]
        if any(keyword and keyword in normalized for keyword in keywords):
            return route
    return None


def select_mental_model_route(query: str, config: dict) -> Optional[dict]:
    """Select one mental model only by exact configured keywords."""
    normalized = re.sub(r"\s+", "", query).lower()
    best = None
    best_hits = 0
    for route in config.get("mentalModelRoutes") or []:
        if not isinstance(route, dict) or not route.get("mentalModelId"):
            continue
        keywords = [str(value).replace(" ", "").lower() for value in route.get("keywords") or []]
        hits = sum(1 for keyword in keywords if keyword and keyword in normalized)
        if hits > best_hits:
            best = route
            best_hits = hits
    if best:
        return best
    if not config.get("mentalModelSemanticFallbackEnabled", True):
        return None

    threshold = float(config.get("mentalModelSemanticFallbackThreshold", 0.58))
    best_score = 0.0
    for route in config.get("mentalModelRoutes") or []:
        if not isinstance(route, dict) or not route.get("mentalModelId"):
            continue
        keywords = [
            str(value).replace(" ", "").lower()
            for value in route.get("keywords") or []
            if str(value).strip()
        ]
        score = max(
            (
                SequenceMatcher(None, keyword, normalized).ratio()
                for keyword in keywords
            ),
            default=0.0,
        )
        if score > best_score:
            best = route
            best_score = score
    if best_score >= threshold:
        debug_log(
            config,
            f"Mental-model semantic fallback score={best_score:.3f}, "
            f"route={best.get('mentalModelId')}",
        )
        return best
    return None


def select_deterministic_authority_snapshots(query: str, config: dict) -> list[dict]:
    """Return only explicitly-declared, stable policy snapshots.

    These are not a second semantic-memory system.  They cover a small class of
    operational/learning rules whose truth must not depend on vector ranking:
    current memory taxonomy, the learning interaction contract, and the
    Trainer review contract.  A snapshot is emitted only when at least two of
    its declared terms match, preventing broad preference leakage.
    """
    normalized = re.sub(r"\s+", "", str(query or "")).casefold()
    # This is a stable, evidence-backed interaction baseline, not a mutable
    # project preference.  Keep it in the governed hook as well as in optional
    # user configuration: config-reconciliation must never silently remove the
    # only explicit carrier for this cross-agent semantic contract.
    rules = list(config.get("deterministicAuthoritySnapshots") or [])
    if not any(str(rule.get("id") or "") == "ai-collaboration-baseline" for rule in rules if isinstance(rule, dict)):
        rules.append({
            "id": "ai-collaboration-baseline",
            "keywords": ["AI", "聊天", "搭档", "干活", "实际工作", "基础设施", "生产", "认知"],
            "minKeywordHits": 2,
            "text": "用户把 AI 定位为承担实际工作和高认知执行的协作搭档，也是个人认知与生产基础设施；不是只做单点问答或普通聊天。其价值来自把 Codex、长期记忆、自动化、学习训练和多模型协作组合为可积累、可复用、可验证的工作系统。涉及当前事实、授权或行动时，最终判断仍以本轮用户 Prompt、当前权威来源和用户决定为准。",
        })
    selected = []
    for rule in rules:
        if not isinstance(rule, dict) or not str(rule.get("text") or "").strip():
            continue
        keywords = [
            re.sub(r"\s+", "", str(item)).casefold()
            for item in rule.get("keywords") or []
            if str(item).strip()
        ]
        min_hits = max(1, int(rule.get("minKeywordHits", 2)))
        if sum(bool(keyword and keyword in normalized) for keyword in keywords) >= min_hits:
            selected.append(rule)
    return selected


def format_deterministic_authority_snapshots(snapshots: list[dict]) -> str:
    if not snapshots:
        return ""
    values = [str(item.get("text") or "").strip() for item in snapshots]
    values = [item for item in values if item]
    if not values:
        return ""
    return (
        "<deterministic_authority_snapshots>\n"
        "以下是已声明、版本化的稳定规则；仅覆盖其直接命题，"
        "不替代对新事实、经历和观察的召回：\n- "
        + "\n- ".join(values)
        + "\n</deterministic_authority_snapshots>"
    )


def format_mental_model(model: dict) -> str:
    content = str(model.get("content") or "").strip()
    if not content:
        return ""
    stale = str(bool(model.get("is_stale"))).lower()
    name = model.get("name") or model.get("id") or "unnamed"
    refreshed = model.get("last_refreshed_at") or "unknown"
    return (
        "<mental_model>\n"
        "以下是为高频问题预先综合的心智模型；仅在直接相关时使用。"
        "若 is_stale=true 或与较新事实冲突，必须回到较新事实核验。\n"
        f"name={name}｜is_stale={stale}｜last_refreshed_at={refreshed}\n"
        f"{content}\n"
        "</mental_model>"
    )


def split_mental_model_sections(content: str) -> list[str]:
    """Split a synthesized model at Markdown headings before relevance checks.

    A mental model is a compact knowledge document, not one atomic fact.  Whole-
    document reranking creates the same long-context dilution problem as
    retrieval over an unchunked archive: one matching sentence can pull several
    unrelated sections into the prompt, while a relevant section in the middle
    can receive a low score.  Keep heading-bounded sections independently
    auditable and injectable.
    """
    value = str(content or "").strip()
    if not value:
        return []
    sections: list[str] = []
    current: list[str] = []
    for line in value.splitlines():
        # Consolidation sometimes writes a broad model section as a sequence
        # of bold-labelled paragraphs rather than Markdown subheadings. Treat
        # those labels as section boundaries too: otherwise one relevant
        # “Hindsight” bullet injects unrelated finance, video or hiring advice
        # that happened to live in the same generated chapter.
        boundary = bool(
            re.match(r"^#{1,4}\s+\S", line)
            or re.match(r"^\s*(?:[-*]\s+)?\*\*[^*\n]{2,80}\*\*\s*[:：]", line)
        )
        if boundary and current:
            section = "\n".join(current).strip()
            if section:
                sections.append(section)
            current = [line]
        else:
            current.append(line)
    if current:
        section = "\n".join(current).strip()
        if section:
            sections.append(section)
    return sections or [value]


def expand_returned_mental_model_sections(items: list[dict]) -> list[dict]:
    """Turn Controller-returned whole models into independently gated sections.

    Controller sidecars and local model lookups are separate paths.  Splitting
    only the local path left a Controller-returned full model able to bypass
    section-level relevance and inject unrelated business/project chapters.
    There are two important boundary cases:

    * The current Controller sidecar already returns one heading-bounded
      section.  Splitting it again creates a decorative title fragment and
      drops its admission metadata, so it must pass through unchanged.
    * Older Controller responses may contain a qualified chapter rather than a
      section.  When that explicit section-gate receipt is present, preserve
      it on each heading-bounded child.  The previous implementation deleted
      the receipt, then the Hook's narrower second gate rejected every child;
      the status page consequently showed ``qualified > 0`` but an empty
      Memory Packet.  Keeping the parent gate makes the cross-layer decision
      monotonic and remains auditable via ``controller_parent_qualified``.
    """
    output: list[dict] = []
    for item in items or []:
        item_type = str(item.get("type") or "").casefold()
        metadata = dict(item.get("metadata") or {})
        source_class = str(metadata.get("source_class") or metadata.get("source") or "").casefold()
        if item_type != "mental_model" and source_class not in {"mental_model", "mental-model"}:
            output.append(item)
            continue
        # The live Controller sidecar has already selected a heading-bounded
        # section and records its model/section identity.  Do not split it a
        # second time: nested ``###`` headings are part of the same bounded
        # chapter and its explicit admission must reach the Packet intact.
        if (
            metadata.get("mental_model_section_gate")
            and metadata.get("mental_model_id")
            and metadata.get("mental_model_section")
        ):
            output.append(item)
            continue
        content = str(item.get("text") or item.get("content") or "")
        sections = split_mental_model_sections(content)
        if len(sections) <= 1:
            output.append(item)
            continue
        parent_id = str(item.get("id") or "mental-model")
        parent_admission = dict(metadata.get("_ccy_admission") or {})
        parent_admission.update(dict(item.get("admission") or {}))
        parent_is_controller_qualified = (
            str(parent_admission.get("decision") or "") == "qualified"
            and bool(parent_admission.get("policy"))
            and str(parent_admission.get("source_class") or source_class).casefold()
            in {"mental_model", "mental-model"}
            and bool(
                parent_admission.get("stable_guidance_sidecar")
                or parent_admission.get("mental_model_section_gate")
            )
        )
        for index, section in enumerate(sections, start=1):
            child = dict(item)
            child["id"] = f"{parent_id}:section:{index}"
            child["text"] = section
            child["content"] = section
            child_metadata = dict(metadata)
            child_metadata.update({
                "source_class": "mental_model",
                "mental_model_parent_id": parent_id,
                "mental_model_section": index,
                "mental_model_section_gate": True,
            })
            child_metadata.pop("_ccy_admission", None)
            # Preserve a qualified Controller chapter gate across the
            # heading-boundary transformation.  This is not a generic bypass:
            # it requires the Controller's explicit policy, mental-model
            # source class and stable-guidance section gate.  The child ID and
            # parent ID remain visible so the inspector can distinguish the
            # exact transported section from the parent model.
            if parent_is_controller_qualified:
                child_admission = dict(parent_admission)
                child_admission.update({
                    "decision": "qualified",
                    "source_class": "mental_model",
                    "mental_model_section_gate": True,
                    "stable_guidance_sidecar": True,
                    "controller_parent_qualified": True,
                    "mental_model_parent_id": parent_id,
                    "mental_model_section": index,
                    "reason": (
                        "Controller 已完成稳定心智模型章节准入；本段仅是同一章节的标题边界切分，"
                        "保留父门槛，避免 Hook 二次通用门误杀。"
                    ),
                })
                child_metadata["_ccy_admission"] = child_admission
            child["metadata"] = child_metadata
            output.append(child)
    return output


def asks_for_direct_evidence(query: str, config: dict) -> bool:
    # Generic words such as “证据 / 时间 / 来源” occur in system audits and do
    # not authorize raw archive access.  Require a request for the user's own
    # wording/source/provenance.
    return explicit_user_source_request(query)


def is_substantive_evidence_query(query: str) -> bool:
    """Keep source-blind evidence automatic but skip greetings and tiny commands."""
    normalized = re.sub(r"\s+", "", query)
    return len(normalized) >= 8


def should_expand_evidence(
    query: str,
    primary_results: list[dict],
    config: dict,
    *,
    has_mental_model: bool = False,
) -> bool:
    """Raw archives open only for explicit user-provenance requests.

    Weak semantic recall must escalate within structured Hindsight memory, not
    pull unrelated raw Doubao/Qianwen/Feishu chunks across the prompt boundary.
    """
    return asks_for_direct_evidence(query, config)


def compose_evidence_query(query: str) -> str:
    """Remove provenance-question boilerplate that harms semantic retrieval."""
    value = query.strip()
    patterns = [
        r"我?(?:之前|以前|当时)?在?(?:飞书|千问|豆包)(?:里|中)?",
        r"(?:什么时候|哪次|是否)?说过",
        r"(?:请)?(?:找|查|给我)?(?:原话|证据|来源|时间)",
    ]
    for pattern in patterns:
        value = re.sub(pattern, " ", value, flags=re.IGNORECASE)
    value = re.sub(r"[？?，,：:]", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value if len(value) >= 5 else query


def format_evidence_memories(results: list[dict], bank_id: str, label: str) -> str:
    """Format direct evidence with explicit provenance and temporal fields."""
    lines = []
    for result in results:
        text = (result.get("text") or "").strip()
        if not text:
            continue
        metadata = result.get("metadata") or {}
        # Chunk-mode source banks group several messages in one document.  The
        # API-level occurred_start is therefore only the chunk/document index
        # time; precise per-message evidence time remains inside [timestamp]
        # markers in the quoted text and must take precedence.
        occurred = result.get("occurred_start") or result.get("mentioned_at") or "时间未知"
        document_id = result.get("document_id") or "unknown-document"
        source = metadata.get("source") or label or bank_id
        source_bank = metadata.get("source_bank") or bank_id
        source_document = metadata.get("source_document_id") or document_id
        lines.append(
            f"- [直接用户证据｜来源={source}｜来源库={source_bank}｜"
            f"索引时间={occurred}｜原文内[time]为证据时间｜来源文档={source_document}]\n{text}"
        )
    return "\n\n".join(lines)


def filter_direct_evidence_by_named_anchors(query: str, results: list[dict]) -> list[dict]:
    """Reject source snippets that miss named entities in an evidence request.

    With an RRF passthrough reranker an unrelated raw chunk may be ranked ahead
    of a source that actually proves a claim.  For provenance requests, a
    snippet lacking a named delivery surface or agent in the question is not
    evidence and must not suppress the explicit "insufficient evidence" state.
    This is a precision gate, not a substitute for semantic retrieval.
    """
    compact_query = re.sub(r"\s+", "", str(query or "")).casefold()
    anchors = [
        term for term in ("微信", "企业微信", "飞书", "小黛", "openclaw", "codex", "trainer")
        if term.casefold() in compact_query
    ]
    if not anchors:
        return results
    accepted = []
    for result in results:
        text = re.sub(r"\s+", "", str(result.get("text") or "")).casefold()
        if all(anchor.casefold() in text for anchor in anchors):
            accepted.append(result)
    return accepted


def filter_by_min_scores(results: list[dict], min_scores: dict, config: dict) -> list[dict]:
    """Drop recall results whose numeric scores are below configured floors."""
    if not min_scores:
        return results

    floors = {}
    for field, floor in min_scores.items():
        try:
            floors[field] = float(floor)
        except (TypeError, ValueError):
            debug_log(config, f"Ignoring invalid recallMinScores floor for '{field}': {floor!r}")
    if not floors:
        return results

    def passes_floors(result: dict) -> bool:
        scores = result.get("scores") or {}
        for field, floor in floors.items():
            value = scores.get(field)
            # Missing/None scores pass (fail-open): BM25-only hits lack semantic
            # scores, and passthrough rerankers report null.
            if isinstance(value, (int, float)) and value < floor:
                return False
        return True

    before_count = len(results)
    filtered = [result for result in results if passes_floors(result)]
    dropped_count = before_count - len(filtered)
    debug_log(config, f"Score floors dropped {dropped_count}/{before_count} results")
    return filtered


def specific_personal_attribute_anchor(query: str) -> str:
    """Extract a narrow personal-attribute anchor for negative calibration.

    This intentionally covers only explicit possessive questions (my shoe
    size, my mother's birthday, my favourite football team).  It is not a
    general keyword reranker, so broad or conceptual questions keep semantic
    recall and do not become brittle exact-match searches.
    """
    compact = re.sub(r"\s+", "", str(query or "")).casefold().strip("？?!！。")
    favourite = re.search(
        r"^我最喜欢(?:的)?(?:哪(?:一)?(?:个|支|种|类)?)?(.+)$", compact
    )
    if favourite:
        return favourite.group(1).strip("的")
    sized_item = re.match(
        r"^我(?:平时|通常|一般)?(?:穿|戴|用)?(?:多大号|多大|什么尺码|多少码)(?:的)?(.+)$",
        compact,
    )
    if sized_item:
        return sized_item.group(1).strip("的")
    possessive = re.match(
        r"^(?:我的|我本人的|我母亲的|我妈妈的|我父亲的|我爸爸的)(.+)$", compact
    )
    if not possessive:
        return ""
    remainder = possessive.group(1)
    # ``我的`` is also a normal discourse opener (``我的意思/理解/要求/看法``),
    # not a request for a personal attribute.  The old prefix-only matcher
    # treated the rest of any sentence as an attribute and the point-query
    # calibration then erased every otherwise relevant memory.  Require an
    # explicit attribute question form before entering this narrow lane; a
    # generic ``我的…`` sentence must remain in ordinary semantic recall.
    attribute_question_markers = (
        "是什么", "是多少", "有多少", "哪一个", "哪个", "哪种", "哪支", "哪天",
        "哪一天", "在哪里", "哪儿", "什么尺码", "多少码", "多大号", "叫什么",
    )
    if not any(marker in remainder for marker in attribute_question_markers):
        return ""
    anchor = re.split(
        r"(?:是什么|是多少|有多少|哪(?:一)?(?:个|位|种|支|天)|哪个|哪种|哪支|"
        r"哪一天|在哪里|哪儿|什么尺码|多少码|多大号|叫什么|为|多少|什么)",
        remainder,
        maxsplit=1,
    )[0]
    anchor = anchor.strip("的")
    # These are discourse/requirement nouns, not stable personal attributes;
    # keeping the exclusion generic prevents future ``我的…`` follow-ups from
    # activating the negative-calibration lane by accident.
    if anchor in {"意思", "理解", "要求", "看法", "想法", "建议", "问题", "目标", "需求", "感觉", "担心", "观点", "prompt"}:
        return ""
    return anchor if len(anchor) >= 2 else ""


def filter_specific_personal_attribute(
    query: str,
    results: list[dict],
    query_plan: Optional[dict],
) -> tuple[list[dict], str, bool]:
    """Keep only candidates that directly mention a narrow attribute.

    A high vector score alone is not proof that an absent personal fact exists;
    the local E5 space can rank “四足机器人” for “足球队” or long document
    rules for “鞋码”.  For a compact point query we therefore require the
    extracted attribute phrase to occur in the recalled evidence.  When it
    does not, emit a small explicit insufficiency receipt instead of injecting
    several kilobytes of unrelated memory.
    """
    if (query_plan or {}).get("primary_shape") != "point":
        return results, "", False
    anchor = specific_personal_attribute_anchor(query)
    if not anchor:
        return results, "", False
    compact_anchor = re.sub(r"\s+", "", anchor).casefold()
    # Question modifiers describe confidence, not a different attribute:
    # “正确姓名/真实姓名/当前手机号” must match the same evidence field as
    # “姓名/手机号”.  Removing only a small closed set keeps the precision
    # gate generic without growing a topic-specific keyword list.
    compact_anchor = re.sub(r"^(?:正确|真实|准确|当前|现在|本人)", "", compact_anchor)
    # Small linguistic normalizations prevent obvious false negatives without
    # turning this precision gate into a topic ontology.
    alias_groups = {
        "名字": ("名字", "姓名"),
        "姓名": ("姓名", "名字"),
        "生日": ("生日", "出生日期", "出生年月"),
        "鞋码": ("鞋码", "鞋号"),
        "足球队": ("足球队", "球队", "足球俱乐部"),
        "手机号": ("手机号", "手机号码", "联系电话"),
        "住址": ("住址", "家庭地址", "居住地址"),
        "地址": ("地址", "住址", "所在地"),
    }
    anchors = alias_groups.get(compact_anchor, (compact_anchor,))
    matched = [
        item for item in results
        if any(
            value in re.sub(
                r"\s+", "", str(item.get("text") or item.get("content") or "")
            ).casefold()
            for value in anchors
        )
    ]
    return matched, anchor, bool(results and not matched)


# Query-time relevance admission is deliberately independent from vector score.
# Vector similarity is a *candidate generator*: it is excellent at paraphrases,
# but a generic phrase such as "verify / answer / history" can also pull in an
# unrelated long project record.  Before a candidate crosses the prompt boundary
# it must show at least one (or, for richer questions, two) visible topic anchors.
# Direct literal evidence is exempt because its containment was verified upstream.
ADMISSION_GENERIC_TERMS = {
    "这个", "那个", "这些", "问题", "什么", "怎么", "如何", "为什么", "是不是",
    "没有", "还有", "很多", "一些", "之前", "现在", "历史", "聊天", "记录", "对应",
    "实际", "当前", "到底", "因为", "看到", "压根", "一个", "多找", "信息", "我们",
}
ADMISSION_SIGNAL_TERMS = (
    "hindsight", "agentmemory", "codex", "openclaw", "hermes", "trainer", "小黛", "飞书", "wps",
    "记忆", "注入", "召回", "检索", "读取", "回答", "相关", "上下文", "观察", "心智模型", "实体",
    "测试", "证据", "时间线", "项目", "方案", "预算", "报价", "费用", "图表", "表格", "word", "excel", "ppt",
    # Structural-delivery anchors are short in Chinese but highly meaningful
    # when they co-occur.  They prevent a real dependency such as “岗位三”
    # from being rejected merely because the stored memory omits the school
    # name repeated in the user request.
    "岗位", "角色", "方向", "职责", "模块", "功能", "课程", "实施", "验收", "成果", "联动", "关联", "完整",
)

def _admission_compact(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "")).casefold()


def _query_admission_signals(query: str) -> list[str]:
    compact = _admission_compact(query)
    signals = [term for term in ADMISSION_SIGNAL_TERMS if term in compact]
    # Proper names / identifiers are high-precision anchors.  Keep the rule
    # generic: this captures future services and project identifiers too.
    for term in re.findall(r"[a-z][a-z0-9_.-]{2,}", compact):
        if term not in signals and term not in ADMISSION_GENERIC_TERMS:
            signals.append(term)
    # A rare Chinese 3-character fragment is useful when the query has no
    # known technical term.  Common conversational fragments never qualify.
    cjk = "".join(re.findall(r"[\u3400-\u9fff]", compact))
    for index in range(max(0, len(cjk) - 2)):
        term = cjk[index:index + 3]
        if any(generic in term for generic in ADMISSION_GENERIC_TERMS):
            continue
        if term not in signals:
            signals.append(term)
    return signals[:32]


def admit_recall_results(query: str, results: list[dict], query_plan: Optional[dict]) -> tuple[list[dict], list[dict]]:
    """Re-check local additions without vetoing an already verified authority result.

    The controller has already performed relevance admission for its own rows.
    Separately, deterministic live authorities (current runtime, Coding Plan and
    identity correction) are constructed from their authoritative local source.
    A second generic text gate must not erase either class merely because their
    concise wording omits a query keyword.  Only unverified hook-side additions
    continue through the local admission gate.
    """
    plan = query_plan or {}
    authority_sources = {
        "identity-authority",
        "deterministic-authority",
        "local-runtime-authority",
        "runtime-authority",
        "coding-plan-runtime-authority",
        "backup-runtime-authority",
        "wps-sync-runtime-authority",
        "memory-pipeline-authority",
        "no-fixed-item-cap-authority",
        "continuation-policy-authority",
        "unknown-attribute-policy-authority",
        "smalltalk-memory-policy-authority",
        "local-edit-memory-policy-authority",
    }
    prequalified, candidates = [], []
    for item in results or []:
        metadata = dict(item.get("metadata") or {})
        # Controller receipts intentionally expose their audit explanation at
        # the top level (``admission``), whereas direct Bank rows keep it in
        # metadata.  Treating only the latter as prequalified caused the Hook
        # to run a stale second gate and silently drop Controller-approved
        # evidence before injection.  Accept it only when it carries this
        # controller's explicit policy/version marker; arbitrary upstream JSON
        # cannot grant itself prompt-boundary authority.
        admission = dict(metadata.get("_ccy_admission") or item.get("admission") or {})
        source = str(metadata.get("source") or "")
        # Controller has several semantically qualified decisions (for
        # example ``qualified_evolution_stage`` and
        # ``qualified_mechanism_stage``), not only the bare ``qualified``
        # string.  Re-running a narrower Hook gate for those variants recreates
        # the historical "Controller qualified, Packet zero" failure.  The
        # policy value is the issuer boundary: an arbitrary upstream row may
        # not bypass the Hook merely by adding a qualified-looking decision.
        decision = str(admission.get("decision") or "").casefold()
        controller_policy = str(admission.get("policy") or "")
        controller_qualified = (
            (decision == "qualified" or decision.startswith("qualified_"))
            and controller_policy == RELEVANCE_POLICY
        )
        if controller_qualified or source in authority_sources:
            if not metadata.get("_ccy_admission") and controller_qualified:
                item = dict(item)
                metadata["_ccy_admission"] = admission
                item["metadata"] = metadata
            prequalified.append(item)
        else:
            candidates.append(item)
    admitted, rejected = relevance_admit_items(
        query,
        candidates,
        deep=bool(plan.get("explicit_deep_recall") or plan.get("primary_shape") in {"inventory", "synthesis", "timeline"}),
        preserve_controller_decision=True,
    )
    return prequalified + admitted, rejected

def suppress_scope_free_inventory(query: str, results: list[dict], query_plan: Optional[dict]) -> tuple[list[dict], str]:
    """Prevent unanchored open-set wording from polluting a shared task context.

    The controller normally returns ``noop_long_term`` for these turns.  This
    hook-side guard is intentionally redundant: if the planner is temporarily
    unavailable, a phrase such as “还有哪些占比较大的空间” must still not inject
    unrelated rooms, model names, or historical projects into the answer.
    """
    compact = re.sub(r"\s+", "", str(query or "")).casefold()
    plan = query_plan or {}
    historical = ("历史", "以前", "之前", "上次", "当时", "记忆", "跨任务", "跨会话", "原话", "来源", "证据")
    vague = (
        len(compact) <= 36
        and any(marker in compact for marker in ("还有哪些", "还有什么", "哪些地方", "哪些方面", "占比较大", "还差什么"))
        and not any(marker in compact for marker in historical)
    )
    if vague and (plan.get("primary_shape") == "inventory" or not plan):
        return [], "问题缺少历史/项目/实体锚点；为避免无关记忆进入当前任务，候选仅保留在检索审计中，未注入。"
    return results, ""


def _recall_statement(text: str) -> str:
    """Canonicalize the claim while ignoring rendered provenance suffixes."""
    value = unicodedata.normalize("NFKC", str(text or ""))
    value = re.split(
        r"\s+\|\s+(?=(?:When|Involving|Context|Source|时间|涉及|来源)\s*[:：])",
        value,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    return re.sub(r"[\W_]+", "", value, flags=re.UNICODE).casefold()


def dedupe_recall_results(results: list[dict]) -> list[dict]:
    """Remove exact and near-identical claims while preserving ranked order.

    Independent evidence copies remain in PostgreSQL for provenance.  They do
    not need to occupy multiple slots in the prompt, so recall-time deduplication
    ignores document IDs and tolerates punctuation or tiny wording differences.
    """
    output = []
    canonical = []
    for result in results or []:
        statement = _recall_statement(result.get("text") or "")
        if not statement:
            continue
        duplicate = False
        for previous in canonical:
            if statement == previous:
                duplicate = True
                break
            shorter, longer = sorted((statement, previous), key=len)
            if len(shorter) >= 28 and shorter in longer and len(shorter) / len(longer) >= 0.82:
                duplicate = True
                break
            if (
                min(len(statement), len(previous)) >= 36
                and SequenceMatcher(None, statement, previous).ratio() >= 0.93
            ):
                duplicate = True
                break
        if duplicate:
            continue
        canonical.append(statement)
        output.append(result)
    return output


def filter_timeline_evidence(query: str, results: list[dict]) -> tuple[list[dict], list[dict]]:
    """Keep only independently usable evidence in an origin-to-now timeline.

    The controller governs its own recall lanes, but the Hook also merges
    optional shared/graph lanes.  Those rows must not bypass the same boundary:
    a prior *question about* the system, an assistant explanation, or a mere
    recommendation is not evidence that a system transition occurred.
    """
    compact_query = _admission_compact(query)
    # A standalone “阶段” is often incidental (“选题梳理阶段已结束”).  It
    # must not turn on the destructive timeline-evidence filter and erase
    # otherwise qualified literature/implementation records.  Activate only
    # for an explicit temporal relation or a compound stage relation; this
    # mirrors Controller.timeline_query_signal without importing the Controller
    # module (which would create a circular dependency).
    timeline_explicit = any(marker in compact_query for marker in (
        "时间线", "时间轴", "变迁", "演变", "演进", "历程", "历史变化",
        "先后", "前后", "起点到", "从官方", "官方以来", "发展历程",
        "版本演进", "版本变化", "状态变更", "状态变化", "替代关系",
        "最开始", "最初", "一开始", "起初", "后来", "接入后", "升级后",
    ))
    timeline_explicit = timeline_explicit or (
        "阶段" in compact_query
        and any(marker in compact_query for marker in (
            "阶段变化", "阶段演进", "阶段历程", "不同阶段", "各阶段",
            "阶段分别", "阶段有哪些", "按阶段", "分阶段", "阶段顺序",
        ))
    )
    if not timeline_explicit and operational_audit_query(query):
        # “之前总结的…现在” is an audit comparison, not an origin-to-now
        # history.  Keep the audit's structured candidates; do not apply the
        # timeline evidence filter merely because the prompt mentions a prior
        # summary.
        return list(results or []), []
    timeline_explicit = timeline_explicit or (
        "之前" in compact_query
        and any(marker in compact_query for marker in ("现在", "后来", "目前", "如今"))
    )
    date_signals = re.findall(
        # Do not treat quantity ranges such as “400-800 条” or “3-5 个” as
        # dates.  Only an explicit Chinese month, a full ISO date, a dotted or
        # slashed short date, or a relative period opens timeline filtering.
        r"(?:20\d{2}年)?\d{1,2}月\d{1,2}(?:日|号)?|"
        r"20\d{2}[./-]\d{1,2}[./-]\d{1,2}|"
        r"(?<!\d)\d{1,2}[./]\d{1,2}(?!\d)|"
        r"(?:今天|昨天|前天|本周|上周|本月|上月)",
        compact_query,
    )
    timeline_explicit = timeline_explicit or len(date_signals) >= 2 or (
        "分别" in compact_query and "之前" in compact_query
    )
    if not timeline_explicit:
        return list(results or []), []
    anchors = [value.casefold() for value in re.findall(r"[a-z][a-z0-9_.-]{2,}", compact_query)]
    if not anchors:
        # No explicit subject means this generic guard cannot safely distinguish
        # a system's history from a nearby project history.
        return list(results or []), []
    stage_terms = (
        "最初", "最开始", "一开始", "起初", "初版", "官方", "基础", "早期", "接入", "引入",
        "迁移", "重构", "升级", "替代", "切换", "改为", "扩展", "整合", "适配", "分层", "新增",
        "改动", "变更", "优化", "修复", "上线", "停用", "当前", "现在", "目前", "正式", "生产",
        "已启用", "已生效", "运行",
    )
    structural_terms = (
        "系统", "架构", "机制", "记忆", "bank", "实体", "关系", "召回", "检索", "retain", "recall",
        "reflect", "observation", "mentalmodel", "模型", "hook", "adapter", "controller", "api", "mcp",
        "配置", "数据库", "治理", "审计", "注入", "图谱", "工作流", "控制平面", "版本",
    )
    narrative_prefixes = (
        "助手解释", "助理解释", "助手建议", "助理建议", "助手澄清", "助理澄清", "回答用户",
        "示例用户询问", "示例用户请求", "用户询问", "用户要求", "用户希望", "用户提出", "用户指出",
        "用户认为", "用户担忧", "用户偏好", "用户决定", "用户设定", "对用户当前场景的判断", "当前问题",
    )
    realization_terms = ("已完成", "完成", "已实现", "实现", "已上线", "上线", "已启用", "启用", "已生效", "生效", "正式启用", "正式切换", "生产切换", "生产运行", "已切换", "切换成功", "部署完成", "验证通过", "测试通过", "落地", "运行中")
    kept, rejected = [], []
    for item in results or []:
        text = str(item.get("text") or item.get("content") or "")
        normalized = _recall_statement(text)
        is_narrative = any(normalized.startswith(prefix) for prefix in narrative_prefixes)
        realized = any(term in normalized and not any(f"未{term}" in normalized or f"没有{term}" in normalized or f"尚未{term}" in normalized for _ in (0,)) for term in realization_terms)
        qualifies = (
            any(anchor in normalized for anchor in anchors)
            and any(term in normalized for term in stage_terms)
            and any(term in normalized for term in structural_terms)
            and (not is_narrative or realized)
        )
        if qualifies:
            kept.append(item)
            continue
        blocked = dict(item)
        metadata = dict(blocked.get("metadata") or {})
        metadata["_ccy_admission"] = {
            "decision": "rejected_timeline_non_evidence",
            "reason": "本题要求从起点到当前的实际变迁；该候选未独立证明一个已发生阶段，不能因同题、解释或建议而注入。",
            "policy": RELEVANCE_POLICY,
            "fixed_item_limit": False,
        }
        blocked["metadata"] = metadata
        rejected.append(blocked)
    return kept, rejected


def filter_question_echo_evidence(query: str, results: list[dict]) -> tuple[list[dict], list[dict]]:
    """Remove historical prompt echoes that provide no answer-side evidence.

    The Bank legitimately retains a user's previous question as an experience.
    On a later, similarly worded question hybrid recall can rank that echo very
    highly.  It must not enter an answer packet merely because it repeats the
    nouns in the request: a question is not proof of its answer.  This filter
    is deliberately content-based, applies after all recall lanes merge, and
    keeps records that also state a concrete conclusion, outcome or verified
    decision.
    """
    def evidence_terms(value: str) -> set[str]:
        compact = _admission_compact(value)
        terms = set(re.findall(r"[a-z0-9]{3,}", compact))
        cjk = "".join(re.findall(r"[\u3400-\u9fff]", compact))
        # CJK has no whitespace segmentation.  Bigrams are used only for this
        # narrow echo detector, then require more than one overlap below.
        terms.update(cjk[index:index + 2] for index in range(max(0, len(cjk) - 1)))
        return terms

    query_terms = evidence_terms(query)
    if not query_terms:
        return list(results or []), []
    narrative_prefixes = (
        "用户询问", "用户提问", "用户问题", "用户请求", "用户要求", "用户希望",
        "示例用户询问", "示例用户提问", "示例用户请求", "示例用户要求", "当前问题",
    )
    # Do not use generic words such as "解决" or "验证" here: a historical
    # *question* can ask for a solution or verification and would then evade
    # the echo filter.  These are assertion-side markers only.
    assertion_markers = (
        "已于", "已升级至", "已上线", "已部署", "验证通过", "实测通过", "实际结果",
        "当前运行态", "当前正式架构运行态", "独立公司", "独立主体", "不是同一实体",
        "版本为", "版本号", "故障已修复", "修复已完成",
    )
    # A historical user-led sentence can start with “用户要求/示例用户要求”
    # and still contain a durable rule rather than merely echoing a question.
    # These governance markers describe an ordering, prohibition, scope or
    # expiry condition that independently answers the current request.  The
    # old echo filter ignored them and therefore dropped the real rule
    # “心智模型不作为事实库…与新事实冲突时自动失效” in Q13 even though the
    # Controller had already admitted it.
    governance_assertion_markers = (
        "正确优先级", "优先级为", "不作为事实", "不作为关键动作依据", "不能覆盖",
        "不得覆盖", "必须记录", "必须包含", "适用范围", "适用边界", "反例边界",
        "自动失效", "自动废弃", "超过期限自动降级", "规则：", "边界：",
        # User-led observations often state a durable admission contract in
        # negative or transport language rather than the exact “正确优先级”
        # wording above.  Treat those assertions as answer-side evidence when
        # they also carry a structural memory marker; otherwise a past rule
        # about not dropping useful injection was misclassified as a question
        # echo in the observation/model backcheck.
        "不得因", "误拒", "优先级必须低于", "优先级低于", "不能改写",
        "不能改变", "仅作为参考", "独立生成", "展示原始输入", "术语统一",
    )
    kept, rejected = [], []
    for item in results or []:
        text = str(item.get("text") or item.get("content") or "")
        normalized = _admission_compact(text)
        is_prompt_echo = any(normalized.startswith(prefix) for prefix in narrative_prefixes)
        text_terms = evidence_terms(normalized)
        overlap = len(query_terms & text_terms)
        # A direct, durable user policy is evidence about a standing rule, not
        # merely a paraphrase of an earlier question.
        is_direct_policy = "用户直接政策" in normalized
        independently_answers = any(marker in normalized for marker in assertion_markers)
        # Require at least one governance marker plus a second structural
        # marker.  This keeps a sentence that merely says “用户要求必须…”
        # from bypassing the echo guard, while preserving a substantive rule
        # with its evidence/expiry/scope language.
        governance_hits = sum(marker in normalized for marker in governance_assertion_markers)
        structural_hits = sum(marker in normalized for marker in ("心智模型", "观察", "事实", "证据", "记忆", "召回", "注入", "规则", "优先级"))
        if governance_hits >= 1 and structural_hits >= 1:
            independently_answers = True
        if not (is_prompt_echo and overlap >= 2 and not independently_answers and not is_direct_policy):
            kept.append(item)
            continue
        blocked = dict(item)
        metadata = dict(blocked.get("metadata") or {})
        metadata["_ccy_admission"] = {
            "decision": "rejected_question_echo",
            "reason": "该历史记录只复述了与本题高度相似的用户问题，未独立给出可验证结论、结果或规则，不能作为答案证据注入。",
            "policy": RELEVANCE_POLICY,
            "fixed_item_limit": False,
        }
        blocked["metadata"] = metadata
        rejected.append(blocked)
    return kept, rejected


def filter_generic_entity_governance_evidence(query: str, results: list[dict]) -> tuple[list[dict], list[dict]]:
    """Keep a generic entity-governance question from becoming an example dump.

    When the user names no concrete entity and asks how aliases/relations work,
    examples from unrelated customers or projects are not needed answer context.
    Retain only records that themselves explain at least two governance controls
    (identity/alias, relation/graph, time, or evidence); this is structural and
    applies to every domain, not a list of project names.
    """
    compact = _admission_compact(query)
    requested = sum(term in compact for term in ("别名", "简称", "实体", "图谱", "关系")) >= 2
    # A quoted or named concrete entity means examples can be direct evidence.
    has_named_anchor = bool(re.search(r"[A-Z][A-Za-z0-9_-]{2,}|[‘“][^’”]{2,24}[’”]", str(query)))
    if not requested or has_named_anchor:
        return list(results or []), []
    controls = ("规范", "别名", "实体", "关系", "图谱", "时间", "版本", "证据", "消歧", "置信", "来源")
    kept, rejected = [], []
    for item in results or []:
        text = _admission_compact(str(item.get("text") or item.get("content") or ""))
        hits = sum(term in text for term in controls)
        question_like = any(marker in text for marker in ("是否为同一实体", "是不是同一实体", "如何处理它们的关系"))
        if hits >= 3 and not question_like:
            kept.append(item)
            continue
        blocked = dict(item); metadata = dict(blocked.get("metadata") or {})
        metadata["_ccy_admission"] = {"decision": "rejected_generic_entity_example", "reason": "未点名实体的治理问题只注入可复用的别名/关系/时间/证据机制；项目实例或单一泛词命中不进入当前上下文。", "policy": RELEVANCE_POLICY, "fixed_item_limit": False}
        blocked["metadata"] = metadata; rejected.append(blocked)
    return kept, rejected


def suppress_domain_collisions(query: str, results: list[dict]) -> tuple[list[dict], list[dict]]:
    """Keep an overloaded industry word from crossing a personal-domain boundary.

    ``医疗`` can mean a user's health decision or a CRM market segment. The
    index may surface both; only the latter is forbidden from a personal-health
    question. This is a domain rule, not a record-id or project-name exception.
    """
    q = _admission_compact(query)
    personal_health = bool(
        any(marker in q for marker in ("健康", "治疗", "用药", "副作用", "诊疗", "疾病", "医疗建议"))
        and any(marker in q for marker in ("我", "我的", "建议", "应当", "如何", "风险", "时效"))
    )
    if not personal_health:
        return list(results or []), []
    commercial = ("crm", "客户", "甲方", "市场", "行业", "对象清单", "团队", "指标", "商机", "投标", "背书")
    personal = ("治疗", "用药", "药物", "副作用", "疗效", "检查结果", "疾病", "健康", "诊疗", "医生", "医疗回答")
    kept, rejected = [], []
    for item in results or []:
        text = _admission_compact(item.get("text") or item.get("content") or "")
        if any(marker in text for marker in commercial) and not any(marker in text for marker in personal):
            clone = dict(item)
            metadata = dict(clone.get("metadata") or {})
            metadata["_ccy_admission"] = {
                "decision": "rejected",
                "reason": "候选把医疗作为CRM/市场行业词，而当前问题是个人健康决策；同名领域词不能越过用途边界。",
                "source_class": str(metadata.get("source") or "unknown"),
                "policy": RELEVANCE_POLICY,
            }
            clone["metadata"] = metadata
            rejected.append(clone)
        else:
            kept.append(item)
    return kept, rejected


def prefer_explicit_current_constraint(query: str, results: list[dict]) -> tuple[list[dict], list[dict]]:
    """For an explicit user 'do not' preference, remove contrary old advice."""
    q = _admission_compact(query)
    asks_compliance = any(marker in q for marker in ("是否应当遵守", "应当遵守", "是否遵守", "不要再建议", "不再建议"))
    negative = any(marker in q for marker in ("不希望", "不要", "别再", "不再"))
    targets = set(re.findall(r"[a-z][a-z0-9_-]{2,}", q))
    if not (asks_compliance and negative and targets):
        return list(results or []), []
    constraint_markers = ("不希望", "不要", "不再", "别再", "拒绝", "除非", "保持关闭", "不主动建议")
    matching = [item for item in results or [] if any(target in _admission_compact(item.get("text") or item.get("content") or "") for target in targets)]
    aligned = [item for item in matching if any(marker in _admission_compact(item.get("text") or item.get("content") or "") for marker in constraint_markers)]
    if not aligned:
        return list(results or []), []
    kept, rejected = [], []
    for item in results or []:
        text = _admission_compact(item.get("text") or item.get("content") or "")
        # This is a named-current-constraint question, so generic preferences
        # (for example approval style) are not supporting evidence either.
        # Keep only the same named decision and its affirmative constraint.
        if not any(target in text for target in targets) or not any(marker in text for marker in constraint_markers):
            clone = dict(item)
            metadata = dict(clone.get("metadata") or {})
            metadata["_ccy_admission"] = {
                "decision": "rejected",
                "reason": "用户当前明确否定该命名选项；历史建议未证明仍有效，不能与现行约束并列注入。",
                "source_class": str(metadata.get("source") or "unknown"),
                "policy": RELEVANCE_POLICY,
            }
            clone["metadata"] = metadata
            rejected.append(clone)
        else:
            kept.append(item)
    return kept, rejected


def main():
    if sys.platform == "win32":
        sys.stdin = io.TextIOWrapper(sys.stdin.buffer, encoding='utf-8', errors='replace')
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

    config = load_config()

    entry_guidance_config = config.get("entryGuidance") or {}
    guidance_required = bool(entry_guidance_config.get("enabled", (config.get("guidanceV1") or {}).get("requirePreActionGuidance", True)))
    if not config.get("autoRecall") and not guidance_required:
        debug_log(config, "Auto-recall and entry guidance disabled, exiting")
        return

    # Read hook input from stdin
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        print("[Evolving Profile] Failed to read hook input", file=sys.stderr)
        return

    debug_log(config, f"Hook input keys: {list(hook_input.keys())}")

    # Extract user query — accept both "prompt" and "user_prompt" defensively
    raw_prompt = (hook_input.get("prompt") or hook_input.get("user_prompt") or "").strip()
    # One local identity connects capture, ingress and retrieval, not the turn:
    # the host can deliver multiple steering messages within the same turn.
    hook_invocation_id = uuid.uuid4().hex
    # Carry the single UserPromptSubmit identity into the forced guidance
    # entry check. Without this, run_entry_check generated a second ID and
    # the guidance receipt could never join the ingress row in 9998.
    hook_input['hook_invocation_id'] = hook_invocation_id
    if hook_input.get('session_id') in config.get('diagnosticSessionIds',[]) or hook_input.get('cwd') in config.get('diagnosticCwds',[]):
        hook_input=dict(hook_input,memory_prompt_origin='test_probe')
    if not is_shadow_replay():
        capture_result = ham_emit("UserPromptSubmit", hook_input, "user_request", "turn", raw_prompt, occurrence_id=hook_invocation_id)
        if capture_result.get('capture_durable') is False:
            print('[Evolving Profile] capture failed: ' + str(capture_result.get('error', 'unknown')), file=sys.stderr)
    from lib.input_origin import memory_query_text
    prompt = memory_query_text(extract_user_request(raw_prompt))
    if not prompt:
        debug_log(config, "Empty prompt, skipping recall")
        return

    # The user's memory boundary is classified before guidance selection,
    # directory navigation, semantic embedding, or raw Bank access. Historical
    # facts can be forbidden while non-factual collaboration preferences remain
    # available; an all-memory prohibition disables both lanes.
    memory_policy = classify_memory_policy(prompt)

    # Agent-owned mode supplies local navigation before deeper Agent choice;
    # legacy mode still runs the guidance selector. Neither authorizes actions
    # or forces historical recall or long-term publication.
    global ENTRY_GUIDANCE_CONTEXT, ENTRY_GUIDANCE_RECEIPT
    try:
        entry = (prepare_agent_owned_entry if config.get('agentOwnedGuidance',True) else run_entry_check)(
            hook_input,prompt,memory_policy=memory_policy["guidance_memory_policy"])
        ENTRY_GUIDANCE_CONTEXT = entry.get("context") or ""
        ENTRY_GUIDANCE_RECEIPT = entry.get("receipt") or {}
    except Exception as entry_error:
        from lib.instruction_entry import instruction_block
        ENTRY_GUIDANCE_CONTEXT = instruction_block('user-prompt-submit-fallback') + '\n' + (
            '<evolving_profile_guidance_entry version="unavailable" mode="forced_task_guidance_check">'
            "入口任务指导检查暂时不可用；不因此推断历史记忆为空，也不扩大授权。"
            "</evolving_profile_guidance_entry>"
        )
        ENTRY_GUIDANCE_RECEIPT = {"schema": "hindsight.guidance-entry-check.v1", "error": type(entry_error).__name__}

    if not memory_policy["history_allowed"]:
        emit_explicit_no_history_receipt(
            hook_input, prompt, config, invocation_id=hook_invocation_id, policy=memory_policy
        )
        return

    if config.get('agentOwnedGuidance',True) and config.get('autoRecall', False):
        task = ((ENTRY_GUIDANCE_RECEIPT or {}).get('request') or {}).get('task') or {}
        if not route_requires_ep_history(prompt, task):
            emit_bounded_system_probe(hook_input,prompt,config,hook_invocation_id)
            return
        # A required history route must execute the real EP pipeline. The
        # bounded probe remains available as a route hint, but it cannot count
        # as Recall/Research and must not be the terminal path.
        debug_log(config, 'Agent-owned guidance route requires EP history; continuing into the real retrieval pipeline')
    elif not config.get("autoRecall"):
        emit_bounded_system_probe(hook_input,prompt,config,hook_invocation_id)
        return

    # UserPromptSubmit is user-authored by default. Controlled replays and
    # adapters can state a different origin explicitly so observability never
    # labels an agent-generated probe as something the user typed.
    prompt_origin = str(hook_input.get("memory_prompt_origin") or "user_direct").strip().casefold()
    if prompt_origin not in {"user_direct", "agent_generated", "test_probe"}:
        prompt_origin = "user_direct"
    if is_shadow_replay():prompt_origin='test_probe'

    session_id = str(hook_input.get("session_id") or "unknown")
    # This identifies one concrete UserPromptSubmit delivery.  The controller
    # may reuse a recent read, but must still create a separate, inspectable
    # chain for this turn so 9998 never shows an unexplained missing route.
    turn_identity = feedback_turn_identity(
        prompt, hook_invocation_id, prompt_origin, turn_id=hook_input.get("turn_id")
    )
    # This happens before any source guard, timeout, or memory-off return so
    # trace coverage can be audited without treating a skipped recall as bad.
    capture_hook_cassette(config, hook_input, raw_prompt, prompt)
    if not is_shadow_replay():
        record_prompt_ingress(config, prompt, session_id, str(hook_input.get("cwd") or ""),turn_id=hook_input.get('turn_id'),hook_invocation_id=hook_invocation_id,prompt_origin=prompt_origin,
                              transcript_path=hook_input.get('transcript_path'), model=hook_input.get('model'),model_provider=hook_input.get('model_provider'))
        route_probe=record_memory_route_probe(hook_input, prompt, config, hook_invocation_id)
        ENTRY_GUIDANCE_CONTEXT += '\n'+format_memory_route_context(route_probe)
        reconcile_native_context_async(hook_input)
    # Exact opt-in scope while independent host acceptance remains pending.
    # Capture the original occurrence above, but do not run the legacy query
    # rewrite/admission stack and the new tool path as two semantic owners.
    from source_driven_hook import enabled as source_driven_enabled
    if source_driven_enabled(config,hook_input):
        from source_driven_hook import prepare,emit
        from profile_view import load_view
        from reference_audit import record_prompt_output
        report=prepare(hook_input,derive_bank_id(hook_input,config),load_view)
        report['invocation_id']=hook_invocation_id
        report['entry_guidance'] = dict(ENTRY_GUIDANCE_RECEIPT or {})
        report['context'] = _with_entry_guidance(report.get('context') or '')
        emit(report,sys.stdout,record_prompt_output)
        return
    previous_recall = read_state(session_recall_state_name(session_id), {}) or {}
    if (
        not is_shadow_replay()
        and
        config.get("memoryEffectivenessEnabled", True)
        and explicit_correction(prompt)
        and previous_recall.get("query_id")
        and previous_recall.get("injected_ids")
        and not previous_recall.get("correction_feedback_submitted")
    ):
        correction_payload = {
            "query_id": previous_recall["query_id"],
            "stage": "correction",
            "session_id": session_id,
            "corrected_ids": previous_recall.get("injected_ids") or [],
            "correction_preview": prompt[:240],
            "project_key": hashlib.sha256(str(hook_input.get("cwd") or "").encode("utf-8")).hexdigest()[:16] if hook_input.get("cwd") else "",
            "feedback_id": hashlib.sha256(
                f"correction:{previous_recall['query_id']}:{prompt}".encode("utf-8")
            ).hexdigest()[:24],
        }
        if post_memory_feedback(config, correction_payload):
            previous_recall["correction_feedback_submitted"] = True
            persist_hook_state(session_recall_state_name(session_id), previous_recall)

    # A shadow replay is an answer-quality experiment for the Hindsight Bank,
    # not an end-to-end continuation test.  Letting a live task handoff enter
    # this path made a previous evaluation look successful because a 4k-char
    # deterministic handoff supplied the answer while the Bank supplied little
    # or nothing.  Production behavior is unchanged; replay explicitly
    # excludes all non-Bank context lanes below.
    handoff_record = (
        select_handoff(
            prompt,
            current_session_id=str(hook_input.get("session_id") or ""),
            current_project=str(hook_input.get("cwd") or ""),
            max_age_seconds=int(config.get("taskHandoffMaxAgeSeconds", 604800)),
        )
        if not is_shadow_replay() and config.get("taskHandoffEnabled", True)
        else None
    )
    handoff_section = format_handoff(
        handoff_record,
        max_chars=int(config.get("taskHandoffMaxChars", 4200)),
    )

    iphone_strategy = consume_iphone_strategy(hook_input, prompt)
    config = apply_iphone_strategy(config, iphone_strategy)
    if iphone_strategy and iphone_strategy["max_tokens"] == 0:
        persist_hook_state(
            LAST_RECALL_STATE,
            {
                "context": "",
                "saved_at": time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
                ),
                "bank_id": config.get("bankId"),
                "result_count": 0,
                "iphone_memory_strategy": iphone_strategy,
                "skipped": "memory_off",
            },
        )
        emit_hook_output()
        return

    def _dbg(*a):
        debug_log(config, *a)

    try:
        api_url = get_api_url(config, debug_fn=_dbg, allow_daemon_start=False)
    except RuntimeError as e:
        print(f"[Evolving Profile] {e}", file=sys.stderr)
        # Cross-task continuity is local-first and remains usable while the
        # long-term memory service is restarting.
        if handoff_section:
            emit_hook_output(handoff_section)
        return

    api_token = config.get("evolvingProfileApiToken")
    memory_role, memory_client = resolve_client_adapter(
        hook_input, config.get("memoryRouterV4Role", "codex")
    )
    source_revision = registry_revision()
    admission_contract_mode = str(config.get("admissionContractMode") or "shadow").casefold()
    if admission_contract_mode not in {"legacy", "shadow", "enforce"}:
        admission_contract_mode = "shadow"
    try:
        client = HindsightClient(
            api_url,
            api_token,
            request_headers={
                "X-Memory-Role": memory_role,
                "X-Memory-Client": memory_client,
                **controller_turn_headers(hook_input, source_revision, admission_contract_mode),
                # The controller may expand a recall with previous turns.  Keep
                # this fingerprint of the *current* user message so the audit
                # can correlate it with UserPromptSubmit without pretending a
                # multi-turn query is an unrelated conversation.
                "X-Memory-User-Prompt-Fingerprint": hashlib.sha256(
                    " ".join(prompt.split()).encode("utf-8")
                ).hexdigest()[:16],
                "X-Memory-Invocation-Id": hook_invocation_id,
                "X-Memory-Execution-Mode": "shadow_replay" if is_shadow_replay() else "production",
                # Contains only a deployed-code digest; never user content.
                "X-Memory-Admission-Revision": ADMISSION_CONTRACT_REVISION,
            },
        )
    except ValueError as e:
        print(f"[Evolving Profile] Invalid API URL: {e}", file=sys.stderr)
        return
    client.request_headers["X-Memory-Prompt-Origin"] = prompt_origin
    evaluation_as_of = str(hook_input.get("memory_evaluation_as_of") or "").strip()
    if is_shadow_replay() and evaluation_as_of:
        # Test-only temporal boundary: do not let a retained response become
        # evidence for the historical question it answered.
        client.request_headers["X-Memory-Evaluation-As-Of"] = evaluation_as_of[:64]
    # Newer adapters can provide the agent-resolved Full Prompt.  Do not
    # manufacture one from a few transcript turns: without this explicit field
    # the Controller will honestly mark its semantic input as a fallback
    # context envelope.  HTTP headers have a transport ceiling, not a product
    # semantic ceiling; adapters with larger contracts should use the planned
    # body-side transport rather than silently truncate here.
    agent_full_prompt = str(hook_input.get("memory_full_prompt") or "").strip()
    resolved_full_prompt_source = ""
    if agent_full_prompt:
        resolved_full_prompt_source = str(hook_input.get("memory_full_prompt_source") or "agent_contract")[:80]
        # Small contracts keep the legacy Header path for old Controllers;
        # large contracts use the JSON body supplied to every recall call.
        # Never drop a valid Agent Full Prompt merely because a proxy header
        # has a finite size ceiling.
        if len(agent_full_prompt.encode("utf-8")) <= 48000:
            client.request_headers["X-Memory-Full-Prompt"] = urllib.parse.quote(agent_full_prompt, safe="")
        else:
            client.request_headers.pop("X-Memory-Full-Prompt", None)
        client.request_headers["X-Memory-Full-Prompt-Source"] = resolved_full_prompt_source

    bank_id = derive_bank_id(hook_input, config)
    if not is_shadow_replay():
        ensure_bank_mission(client, bank_id, config, debug_fn=_dbg)
    # Source paths and authority come from the raw envelope.  Semantic routing,
    # traces and Hindsight queries use only the user-authored request.
    project_runtime = apply_project_runtime_headers(client, hook_input, raw_prompt, session_id)

    # Multi-turn query composition
    recall_context_turns = config.get("recallContextTurns", 1)
    recall_max_query_chars = config.get("recallMaxQueryChars", 800)
    recall_roles = config.get("recallRoles", ["user", "assistant"])

    transcript_path = hook_input.get("transcript_path", "")
    # Do not scan a multi-GB active rollout on every UserPromptSubmit. The
    # recent tail is the online semantic window; durable older material comes
    # from Hindsight and the session index, not a blocking whole-file pass.
    live_tail_bytes = int(config.get("fullPromptLiveTranscriptTailBytes", 8 * 1024 * 1024) or 8 * 1024 * 1024)
    messages = (
        read_transcript(transcript_path, max_tail_bytes=live_tail_bytes)
        if (not is_shadow_replay() or allow_shadow_fixture_transcript(transcript_path)) else []
    )
    # A sidecar index is the durable context path.  It points to immutable raw
    # rollout offsets, but only its bounded evidence pack reaches the Full
    # Prompt resolver.  The first live call bootstraps from the tail; normal
    # background/backfill operation advances it incrementally afterwards.
    session_context_index = {}
    session_context_bundle = {
        "messages": [],
        "receipt": {"strategy": "not_available"},
    }
    if transcript_path and not is_shadow_replay():
        try:
            session_context_index = index_session_transcript(
                transcript_path,
                SESSION_CONTEXT_INDEX_PATH,
                bootstrap_tail_bytes=live_tail_bytes,
            )
            session_context_bundle = retrieve_context_bundle(
                prompt, SESSION_CONTEXT_INDEX_PATH, source_path=transcript_path,
                recent_limit=int(config.get("fullPromptSessionRecentTurns", 8)),
                older_limit=int(config.get("fullPromptSessionOlderMatches", 8)),
                max_bytes=int(config.get("fullPromptSessionEvidenceMaxBytes", 28000)),
            )
        except Exception as error:
            session_context_index = {"ok": False, "reason": "session_index_failed", "error": type(error).__name__}
    resolver_messages = list(session_context_bundle.get("messages") or messages)
    context_memory_profile = build_context_memory_profile(prompt, messages, config)
    contextual_intent = build_contextual_intent_envelope(prompt, resolver_messages or messages, config)
    # Native adapters may provide the agent-resolved Full Prompt.  The stock
    # Codex UserPromptSubmit payload currently does not, so a verified short
    # continuation receives the bounded resolver product instead.  This must
    # be set before the plan call as well as before recall; otherwise the plan
    # sees a bare follow-up and irreversibly routes it as a point query.
    # Every turn receives a Full Prompt contract.  Native contracts always win;
    # otherwise the same Coding Plan Qwen 3.7 Plus used by Hindsight resolves
    # the bounded, source-isolated evidence pack.  This deliberately includes
    # apparently self-contained prompts: only the resolver can distinguish a
    # genuinely complete short request from one whose missing scope is buried
    # much earlier in the task.  A raw==Full Prompt result is therefore a
    # model decision with an auditable source, never a heuristic shortcut.
    full_prompt_resolution = {"source": "native_agent_contract" if hook_input.get("memory_full_prompt") else "not_needed"}
    if not agent_full_prompt:
        # Full Prompt reconstruction consumes the complete active transcript
        # and is materially heavier than the controller's 3.5s route-shape
        # probe.  Keep the budgets distinct: this path is rare and quality
        # critical, while a timeout remains explicit and safely degradable.
        resolution = qwen_full_prompt_fallback(
            prompt,
            resolver_messages,
            timeout_ms=int(config.get("fullPromptResolverTimeoutMs", 7000)),
        )
        full_prompt_resolution = {"source": "qwen3.7-plus_context_resolution", **resolution}
        if resolution.get("ok"):
            agent_full_prompt = str(resolution["full_prompt"])
            resolved_full_prompt_source = "qwen3.7-plus_context_resolution"
            # The deterministic envelope intentionally only marks explicit
            # anaphora/continuations.  A native/Qwen Full Prompt resolver,
            # however, is also allowed to use earlier turns to disambiguate a
            # seemingly self-contained question.  Keep the receipt honest:
            # when a bounded prior context pack was actually supplied to that
            # resolver, expose that fact to Controller and the status page
            # instead of reporting ``used_context=false`` merely because the
            # lexical marker gate did not fire.
            if contextual_intent.get("context_items"):
                contextual_intent["used_context"] = True
                contextual_intent["full_prompt_context_used"] = True
                if contextual_intent.get("intent_mode") == "current_utterance":
                    contextual_intent["intent_mode"] = "full_prompt_context_resolution"
                contextual_intent["reason"] = (
                    "Qwen Full Prompt 解析实际接收了受控的前文证据包；"
                    "前文只用于补全语义背景，当前用户原话仍保持最高优先级。"
                )
            if len(agent_full_prompt.encode("utf-8")) <= 48000:
                client.request_headers["X-Memory-Full-Prompt"] = urllib.parse.quote(agent_full_prompt, safe="")
            else:
                client.request_headers.pop("X-Memory-Full-Prompt", None)
            client.request_headers["X-Memory-Full-Prompt-Source"] = resolved_full_prompt_source
    if not agent_full_prompt and not full_prompt_resolution.get("ok"):
        # The local resolver is deliberately a labelled fallback.  It keeps
        # the Hook useful when the provider is unavailable, but never masks a
        # failed Qwen resolution as a native model contract.
        agent_full_prompt = str(contextual_intent.get("full_prompt") or prompt).strip()
        if agent_full_prompt:
            resolved_full_prompt_source = "deterministic_context_hint_only"
            if len(agent_full_prompt.encode("utf-8")) <= 48000:
                client.request_headers["X-Memory-Full-Prompt"] = urllib.parse.quote(agent_full_prompt, safe="")
            else:
                client.request_headers.pop("X-Memory-Full-Prompt", None)
            client.request_headers["X-Memory-Full-Prompt-Source"] = resolved_full_prompt_source
        full_prompt_resolution["fallback"] = "deterministic_context_hint_only"
    # The Controller needs the interpretation, not a raw transcript. Keep the
    # transport receipt deliberately small so an HTTP header can never become a
    # second context channel or exceed local proxy limits.
    contextual_intent_transport = {
        **{key: contextual_intent.get(key) for key in ("schema", "used_context", "full_prompt_context_used", "intent_mode", "current_user_message", "resolved_subjects", "routing_hints", "guidance_requirements", "reason")},
        "context_items": [
            {"role": str(item.get("role") or ""), "content": str(item.get("content") or "")[:72]}
            for item in (contextual_intent.get("context_items") or [])[-4:]
            if isinstance(item, dict)
        ],
    }
    client.request_headers["X-Memory-Context-Intent"] = urllib.parse.quote(
        json.dumps(contextual_intent_transport, ensure_ascii=False, separators=(",", ":"))
    )
    if is_shadow_replay():
        # Do not use the current task's own prompt/history as a synthetic
        # memory hit during cleanroom evaluation.
        live_context_reactivations, live_context_metrics = [], {}
        live_context_reactivation_section = ""
    else:
        live_context_reactivations, live_context_metrics = select_live_context_reactivations(
            context_memory_profile, config
        )
        live_context_reactivation_section = format_live_context_reactivations(
            live_context_reactivations
        )
    if recall_context_turns > 1:
        debug_log(config, f"Multi-turn context: {recall_context_turns} turns, {len(messages)} messages")
        query = compose_recall_query(prompt, messages, recall_context_turns, recall_roles)
    else:
        query = prompt

    query = truncate_recall_query(query, prompt, recall_max_query_chars)
    if len(query) > recall_max_query_chars:
        query = query[:recall_max_query_chars]

    # Give the controller an unambiguous boundary.  Splitting a multi-paragraph
    # prompt on the last blank line can otherwise mistake only its final
    # paragraph for the user's intent.
    if query != prompt and "Prior context:" in query:
        suffix = "\n\n" + prompt
        if query.endswith(suffix):
            query = query[:-len(suffix)] + "\n\nLatest user message:\n" + prompt

    query = query.encode('utf-8', errors='ignore').decode('utf-8')
    if iphone_strategy and iphone_strategy.get("query_suffix"):
        query += iphone_strategy["query_suffix"]
    # Do not hide a real user turn merely because it is short.  The controller
    # can correctly decide "no long-term memory needed" for greetings, but the
    # status page still needs a zero-result receipt so its chain never appears
    # to stop halfway.  Only an actually empty payload skips the pipeline.
    if not query.strip():
        debug_log(config, "Prompt and available context are too short for recall, skipping")
        return

    # Retrieval may include a bounded amount of prior dialogue, but every
    # routing, source-access and prompt-admission decision is made from the
    # current user message only.  Prior assistant text, file paths and old task
    # terms are useful search hints, never the current intent.
    intent_query = prompt.encode('utf-8', errors='ignore').decode('utf-8')
    # Context changes the retrieval interpretation only for an explicitly
    # dependent audit turn. This bounded hint is never injected as an answer.
    # Hindsight must search the same resolved semantic question that the
    # Controller plans against.  Passing only the raw sentence here was a
    # silent split-brain path: Full Prompt appeared in the trace but Bank
    # retrieval still saw “继续/不要停” and could return almost nothing.
    query = build_contextual_recall_query(
        query,
        contextual_intent,
        resolved_full_prompt=agent_full_prompt,
    )
    # Every Hindsight-facing decision after this point must use the same
    # resolved semantic question.  ``intent_query`` remains the raw user
    # sentence for authority, permissions, current-source and audit identity;
    # it must not silently become a second relevance query.  Before this
    # boundary was explicit, coverage expansion, evidence routing, mental
    # model selection and Hook-side admission could still see only a terse
    # follow-up such as “继续/精简点”, undoing the Controller's Full Prompt
    # resolution and producing a misleadingly small Packet.
    semantic_query = query
    current_time = format_current_time()
    recall_settings = adaptive_recall_settings(semantic_query, config)
    # Propagate the caller-selected latency class into the controller.  Without
    # this, the hook could patiently wait for a deep recall while the inner
    # controller still killed it at the ordinary complex-query ceiling.
    client.request_headers["X-Memory-Recall-Profile"] = recall_settings["profile"]
    recall_started = time.monotonic()
    preamble = config.get("recallPromptPreamble", "")
    recall_timeout = int(recall_settings.get("timeout", config.get("recallTimeout", 10)))

    query_plan = None
    shared_bank = None
    # Error receipts are emitted before normal candidate/context coordination
    # is computed. Keep a truthful empty default so a timeout is observable in
    # 9998 instead of being followed by an UnboundLocalError that hides the
    # actual chain failure.
    context_memory_coordination = {
        "working_set_sufficient": False,
        "memory_request_skipped": False,
        "status": "not_computed_due_to_recall_failure",
    }
    if config.get("memoryRouterV4Enabled", False):
        try:
            policy_path = Path(
                os.path.expanduser(
                    config.get(
                        "memoryRouterV4PolicyPath",
                        "~/.evolving-profile/memory-access-policy-v3.json",
                    )
                )
            )
            query_policy = load_memory_policy_v4(policy_path)
            query_plan = build_memory_plan_v4(
                memory_role,
                intent_query,
                query_policy,
                controller_url=config.get(
                    "evolvingProfileControllerUrl", "http://127.0.0.1:12079"
                ),
                timeout=float(config.get("memoryQueryControllerPlanTimeout", 0.8)),
                client=memory_client,
                bank_id=bank_id,
                contextual_intent=contextual_intent,
                agent_plan=(hook_input.get("memory_agent_plan") if isinstance(hook_input.get("memory_agent_plan"), dict) else None),
                full_prompt=agent_full_prompt,
                full_prompt_source=resolved_full_prompt_source,
            )
            shared_bank = query_plan.get("shared_bank")
            recall_settings = reconcile_recall_settings_with_plan(
                recall_settings, query_plan, config
            )
            recall_timeout = int(recall_settings["timeout"])
            client.request_headers["X-Memory-Recall-Profile"] = str(
                recall_settings.get("controller_profile") or "normal"
            )
        except Exception as error:
            print(f"[Evolving Profile] V4 memory query planning failed: {error}", file=sys.stderr)

    noop_long_term = bool(query_plan and query_plan.get("memory_action") == "noop_long_term")

    mental_model_route = select_mental_model_route(semantic_query, config)
    if noop_long_term or (query_plan and query_plan.get("mental_model_policy") == "suppress"):
        mental_model_route = None
    # Asking for the user's original wording, timestamp and source is an
    # evidence job.  A mental model is an interpreted summary, so even a
    # topically related section must not be injected beside (or mistaken for)
    # source evidence.  Structured proposition leads remain separately
    # governed and are labelled as leads rather than original words.
    if explicit_user_source_request(semantic_query) or explicit_user_source_request(intent_query):
        mental_model_route = None
    # The available broad personal model currently mixes memory architecture
    # with unrelated bid/proposal chapters. Until every upstream model is
    # independently sectioned, factual Bank recall is the safer authority for
    # a direct memory/context architecture question.
    if any(term in re.sub(r"\s+", "", semantic_query + "\n" + intent_query).casefold() for term in ("hindsight", "agentmemory", "记忆", "上下文", "召回", "注入", "controller")):
        mental_model_route = None
    # A concrete cross-task handoff asks for factual task state, artifacts and
    # next actions.  A broad mental model may be true but cannot establish any
    # of those facts, and it previously displaced the actual migration records
    # in an otherwise clean Hindsight-only replay.  Keep models for questions
    # that explicitly ask for a model/strategy; suppress them for a state
    # restoration route.
    if bool((query_plan or {}).get("dimensions", {}).get("continuity")):
        mental_model_route = None
    historical_bank_required = requires_historical_bank_recall(
        semantic_query or intent_query
    )
    authority_query = (
        is_current_runtime_query(intent_query)
        or is_current_coding_plan_query(intent_query)
        or is_backup_runtime_query(intent_query)
        or is_wps_sync_current_query(intent_query)
        or is_unknown_attribute_policy_question(intent_query)
        or is_smalltalk_memory_policy_question(intent_query)
        or is_local_edit_memory_policy_question(intent_query)
    )
    backup_authority_query = is_backup_runtime_query(intent_query)
    unknown_attribute_policy_query = is_unknown_attribute_policy_question(intent_query)
    smalltalk_memory_policy_query = is_smalltalk_memory_policy_question(intent_query)
    local_edit_memory_policy_query = is_local_edit_memory_policy_question(intent_query)
    # A backup-policy question is answered by the live executable policy.  A
    # historical mental model may describe retention philosophy, but it must
    # not crowd the answer with unrelated business/cognition chapters or turn
    # a one-fact query into a broad profile injection.
    if backup_authority_query or unknown_attribute_policy_query or smalltalk_memory_policy_query or local_edit_memory_policy_query:
        mental_model_route = None
    # A live authority snapshot owns only its direct volatile proposition.  A
    # mixed question can ask for current runtime state *and* a broader mental
    # model (for example, current Coding Plan concurrency plus the user's AI
    # operating style).  In that case, keep semantic recall enabled and append
    # the live snapshot as the authority for the volatile fields.  Suppressing
    # all historical recall here used to erase the non-volatile half of such a
    # question whenever the routed mental-model cache was waiting for refresh.
    # A request may contain current-state words (“当前接管时核对哪些状态”)
    # while explicitly asking to reconstruct a prior cross-task operation.
    # The historical provenance boundary wins: keep the live snapshot as an
    # optional authority row, but never let it suppress the Bank recall.
    live_authority_only = authority_query and mental_model_route is None and not historical_bank_required
    if noop_long_term:
        # Still call the local controller once so the zero-recall decision is
        # auditable and visible in the actual-chain UI.  The controller returns
        # immediately without touching Hindsight.
        live_authority_only = False
        shared_bank = None
    evidence_route = None
    force_iphone_evidence = bool(
        iphone_strategy and iphone_strategy.get("force_evidence")
    )
    if (
        config.get("evidenceFallbackEnabled", False)
        and not noop_long_term
        and not live_authority_only
        and (
            asks_for_direct_evidence(semantic_query, config)
            or asks_for_direct_evidence(intent_query, config)
            or force_iphone_evidence
            or bool(query_plan and query_plan.get("requires_direct_evidence"))
        )
    ):
        evidence_route = select_evidence_bank(semantic_query, config)
        if not evidence_route and config.get("evidenceUnifiedEnabled", False):
            evidence_route = {
                "bankId": config.get("evidenceUnifiedBankId", "user-evidence-unified-v1"),
                "label": "跨来源统一用户原话证据",
                "unified": True,
            }
    mental_model_route, evidence_route = controller_owned_hook_lanes(
        admission_contract_mode, mental_model_route, evidence_route
    )

    def recall_primary():
        return client.recall(
            bank_id=bank_id,
            query=query,
            # Preserve the exact Hook payload for audit/UI. ``prompt`` is the
            # extracted user request used for semantics and may intentionally
            # remove attachment/ambient envelopes; it must not replace the
            # original wording in the raw/full-prompt comparison.
            raw_user_prompt=raw_prompt,
            full_prompt=agent_full_prompt,
            full_prompt_source=resolved_full_prompt_source,
            max_tokens=recall_settings["max_tokens"],
            budget=recall_settings["budget"],
            types=(query_plan or {}).get("types") or config.get("recallTypes"),
            prefer_observations=(
                (query_plan or {}).get("prefer_observations")
                if query_plan is not None
                else config.get("recallPreferObservations", False)
            ),
            timeout=recall_timeout,
        )

    def recall_shared():
        return client.recall(
            bank_id=shared_bank,
            query=query,
            raw_user_prompt=raw_prompt,
            full_prompt=agent_full_prompt,
            full_prompt_source=resolved_full_prompt_source,
            max_tokens=int((query_plan or {}).get("max_tokens") or 1400),
            budget=(query_plan or {}).get("budget") or "mid",
            types=(query_plan or {}).get("types") or config.get("recallTypes"),
            prefer_observations=(query_plan or {}).get(
                "prefer_observations", config.get("recallPreferObservations", False)
            ),
            timeout=recall_timeout,
        )

    def recall_model():
        cached = load_cached_mental_model(mental_model_route, config)
        if cached is not None:
            return cached
        mental_model_bank = mental_model_route.get("bankId") or bank_id
        return client.get_mental_model(
            mental_model_bank,
            mental_model_route["mentalModelId"],
            timeout=min(5, recall_timeout),
        )

    def recall_evidence(route):
        return client.recall(
            bank_id=route["bankId"],
            query=compose_evidence_query(semantic_query),
            raw_user_prompt=raw_prompt,
            full_prompt=agent_full_prompt,
            full_prompt_source=resolved_full_prompt_source,
            max_tokens=(
                config.get("evidenceUnifiedMaxTokens", 700)
                if route.get("unified")
                else config.get("evidenceFallbackMaxTokens", 3000)
            ),
            budget=config.get("evidenceFallbackBudget", "low"),
            types=["world"],
            prefer_observations=False,
            min_scores=config.get("evidenceFallbackMinScores")
            or {"semantic": 0.0, "reranker": 0.0, "final": 0.0},
            timeout=config.get("evidenceFallbackTimeout", recall_timeout),
        )

    # All calls below are local read-only Hindsight requests.  They do not use
    # Coding Plan and therefore do not consume the LLM concurrency budget.
    # A "current runtime / current Coding Plan" request is different: its only
    # source of truth is the deterministic live authority adapter below.  Do
    # not also search historical memory, otherwise a stale archive can leak
    # back into the response beside the current value.
    tasks = {}
    worker_count = 0
    if not live_authority_only:
        worker_count += 1
    if not live_authority_only and not noop_long_term and shared_bank and shared_bank != bank_id:
        worker_count += 1
    if mental_model_route:
        worker_count += 1
    if evidence_route:
        worker_count += 1
    response = {"results": []}
    # Deterministic current authority is intentionally not a semantic Bank
    # read.  Ask Controller only for a source-first execution receipt, so this
    # real Hook injection remains visible and joinable in 9998.
    if live_authority_only:
        client.request_headers["X-Memory-Authority-Only"] = "1"
        if smalltalk_memory_policy_query:
            client.request_headers["X-Memory-Authority-Kind"] = "smalltalk_memory_policy"
        elif unknown_attribute_policy_query:
            client.request_headers["X-Memory-Authority-Kind"] = "unknown_attribute_memory_policy"
        elif local_edit_memory_policy_query:
            client.request_headers["X-Memory-Authority-Kind"] = "local_edit_memory_policy"
    with ThreadPoolExecutor(max_workers=max(1, worker_count)) as executor:
        if not live_authority_only:
            tasks["primary"] = executor.submit(recall_primary)
        elif query_plan is not None:
            tasks["authority_audit"] = executor.submit(recall_primary)
        if not live_authority_only and not noop_long_term and shared_bank and shared_bank != bank_id:
            debug_log(
                config,
                f"V4 shared recall from '{shared_bank}', "
                f"shape={(query_plan or {}).get('primary_shape')}, "
                f"levels={(query_plan or {}).get('levels')}",
            )
            tasks["shared"] = executor.submit(recall_shared)
        if mental_model_route:
            tasks["mental"] = executor.submit(recall_model)
        if evidence_route:
            debug_log(config, f"Explicit evidence routed to '{evidence_route['bankId']}'")
            tasks["evidence"] = executor.submit(recall_evidence, evidence_route)

        if "primary" in tasks:
            try:
                response = tasks["primary"].result()
            except Exception as error:
                print(f"[Evolving Profile] Recall failed: {error}", file=sys.stderr)
                # A timeout must be visible as a completed Hook outcome, not
                # make the status-page chain disappear after Search.  This is
                # deliberately a zero *failed* receipt, never a fabricated
                # injection: the Bank is untouched and the model receives no
                # memory context for this turn.
                timeout_query_id = str(
                    (query_plan or {}).get("query_id")
                    or hashlib.sha256(intent_query.encode("utf-8")).hexdigest()[:16]
                )
                failure_payload = {
                    "query_id": timeout_query_id,
                    "stage": "injection",
                    "session_id": session_id,
                    **turn_identity,
                    "bank_id": bank_id,
                    **empty_injection_receipt_fields(
                        reason="Controller/Bank 读取在 Hook 时限内失败；没有可证明的 Packet 投递，不能解释为正常无相关记忆。",
                        receipt_state="failed_timeout",
                    ),
                    "injected_ids": [],
                    "injected_items": [],
                    "candidate_items": [],
                    "rejected_items": [],
                    "deferred_items": [],
                    "use_status": "not_applicable",
                    "memory_action": "recall_timeout",
                    "outcome": "failed",
                    "retrieval_failure": repr(error)[:600],
                    "scope_admission_note": "本次读取在 Hook 时限内没有返回；没有注入任何长期记忆。该失败会在状态页明确显示，不会被误写成‘没有相关记忆’。",
                    "context_chars": 0,
                    "context_memory_coordination": context_memory_coordination,
                    "session_context_index": session_context_index,
                    "session_context_bundle": session_context_bundle.get("receipt") or {},
                    "feedback_id": hashlib.sha256(f"injection:{timeout_query_id}:timeout:{session_id}".encode("utf-8")).hexdigest()[:24],
                }
                publish_injection("", failure_payload, config)
                failure_state = {
                    "context": "", "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "session_id": session_id, "query_id": timeout_query_id, "bank_id": bank_id,
                    "result_count": 0, "memory_action": "recall_timeout", "outcome": "failed",
                    "injection_receipt_state": failure_payload["injection_receipt_state"],
                    "injected_count": failure_payload["injected_count"],
                    "candidate_count": failure_payload["candidate_count"],
                    "rejected_count": failure_payload["rejected_count"],
                    "deferred_count": failure_payload["deferred_count"],
                    "packet_delivery": failure_payload["packet_delivery"],
                    "retrieval_failure": repr(error)[:600], "memory_query_plan": query_plan,
                    "injected_ids": [], "injected_items": [],
                    "context_memory_coordination": context_memory_coordination,
                    "session_context_index": session_context_index,
                    "session_context_bundle": session_context_bundle.get("receipt") or {},
                }
                persist_hook_state(LAST_RECALL_STATE, failure_state)
                persist_hook_state(session_recall_state_name(session_id), failure_state)
                return
        if "authority_audit" in tasks:
            try:
                # The response has no semantic results by contract; retain its
                # execution id for feedback/UI correlation only.
                response = tasks["authority_audit"].result()
            except Exception as error:
                print(f"[Evolving Profile] Authority trace failed: {error}", file=sys.stderr)
        start_ham_shadow_canary(hook_input, semantic_query, len(response.get("results") or []))
        shared_response = {}
        shared_results = []
        if "shared" in tasks:
            try:
                shared_response = tasks["shared"].result()
                shared_results = shared_response.get("results", [])
            except Exception as error:
                print(f"[Evolving Profile] V4 shared recall failed: {error}", file=sys.stderr)
        mental_model = None
        if "mental" in tasks:
            try:
                mental_model = tasks["mental"].result()
            except Exception as error:
                print(f"[Evolving Profile] Mental model lookup failed: {error}", file=sys.stderr)
        evidence_response = None
        if "evidence" in tasks:
            try:
                evidence_response = tasks["evidence"].result()
            except Exception as error:
                print(f"[Evolving Profile] Evidence fallback failed: {error}", file=sys.stderr)

    controller_receipt = (response.get("query_controller") or {}) if isinstance(response, dict) else {}
    controller_contract_context = {
        "session_id": session_id,
        "turn_id": str(hook_input.get("turn_id") or ""),
        "hook_invocation_id": hook_invocation_id,
        "execution_id": str(controller_receipt.get("execution_id") or controller_receipt.get("query_id") or ""),
        "prompt_sha256": str(turn_identity.get("prompt_fingerprint") or ""),
        "policy_version": str((controller_receipt.get("relevance_admission") or {}).get("policy") or RELEVANCE_POLICY),
        "source_revision": source_revision,
    }
    controller_contract_shadow_receipt = controller_contract_shadow(
        list(response.get("results") or []) if isinstance(response, dict) else [],
        controller_receipt,
        controller_contract_context,
    )
    if controller_receipt.get('memory_action')=='guidance_only':
        semantic_query=str(controller_receipt.get('guidance_query') or semantic_query)
    executed_query_plan = effective_query_plan(query_plan, controller_receipt)
    source_guard = controller_receipt.get("source_guard") or {}
    strict_source_first = bool(source_guard.get("active"))
    source_priority_guidance=controller_receipt.get('memory_action')=='guidance_only' and bool((controller_receipt.get('memory_needs') or {}).get('guidance',{}).get('needed'))
    controller_admission = controller_receipt.get("relevance_admission") or {}
    controller_rejected_candidates = list(controller_admission.get("rejected_items") or [])
    controller_deferred_candidates = list(controller_admission.get("deferred_items") or [])
    results = dedupe_recall_results(expand_returned_mental_model_sections(shared_results + response.get("results", [])))
    contract_enforcement = {
        "mode": admission_contract_mode,
        "state": "shadow" if admission_contract_mode == "shadow" else "legacy",
        "reason": "影子模式不改变 Hook 的现有候选处理。",
    }
    contract_enforced = False
    legacy_results_before_contract = list(results)
    enforce_disqualifiers = []
    if admission_contract_mode == "enforce":
        # The Controller already owns primary/shared Bank semantics.  Local
        # model and fallback lanes are migrated separately; until each returns
        # the same contract, fall back explicitly instead of silently mixing
        # two semantic owners in one Packet.
        if noop_long_term:
            enforce_disqualifiers.append("local_protocol_lane")
        primary_enforcement = enforce_controller_admission(
            list(response.get("results") or []) if isinstance(response, dict) else [],
            controller_receipt,
            controller_contract_context,
        )
        shared_receipt = (shared_response.get("query_controller") or {}) if isinstance(shared_response, dict) else {}
        shared_context = dict(controller_contract_context)
        shared_context["execution_id"] = str(shared_receipt.get("execution_id") or shared_receipt.get("query_id") or "")
        shared_enforcement = enforce_controller_admission(shared_results, shared_receipt, shared_context) if shared_results else {"state": "enforced", "items": [], "partition": {"admit": [], "reject": [], "defer": [], "unverified": []}}
        if primary_enforcement["state"] != "enforced":
            enforce_disqualifiers.append("primary_contract_gap")
        if shared_enforcement["state"] != "enforced":
            enforce_disqualifiers.append("shared_contract_gap")
        if not enforce_disqualifiers:
            results = dedupe_recall_results(primary_enforcement["items"] + shared_enforcement["items"])
            contract_enforced = True
            contract_enforcement = {
                "mode": "enforce",
                "state": "enforced",
                "reason": "主Bank与共享Bank候选均有同回合Controller合同；Hook不再重复主题相关性判断。",
                "primary": primary_enforcement["state"],
                "shared": shared_enforcement["state"],
            }
        else:
            contract_enforcement = {
                "mode": "enforce",
                "state": "legacy_fallback_required",
                "reason": "；".join(enforce_disqualifiers),
                "primary": primary_enforcement["state"],
                "shared": shared_enforcement["state"],
            }
    provenance_sidecar_rejected = []
    if explicit_user_source_request(semantic_query) or explicit_user_source_request(intent_query):
        retained = []
        for item in results:
            item_type = str(item.get("type") or "").casefold()
            source_class = str((item.get("metadata") or {}).get("source_class") or "").casefold()
            if item_type in {"mental_model", "direct_policy"} or source_class in {"mental_model", "direct_policy"}:
                blocked = dict(item)
                metadata = dict(blocked.get("metadata") or {})
                metadata["_ccy_admission"] = {
                    "decision": "rejected_provenance_derived_summary",
                    "reason": "当前问题要求用户原话、时间或来源；心智模型/直接政策是加工摘要，不能当作原话证据。",
                    "policy": RELEVANCE_POLICY,
                    "fixed_item_limit": False,
                }
                blocked["metadata"] = metadata
                provenance_sidecar_rejected.append(blocked)
            else:
                retained.append(item)
        results = retained
    if strict_source_first:
        # Defence in depth for an authority boundary.  A stale Controller
        # process or an optional sidecar must never turn a source-first receipt
        # into historical-memory injection. Preserve the Controller's own
        # executable authority row: it is the current source being inspected,
        # not a historical Bank fact.
        results = [
            item for item in results
            if str((item.get("metadata") or {}).get("source") or "").endswith("-authority")
            or (source_priority_guidance and item.get('type') in {'observation','mental_model','direct_policy'})
        ]
        shared_results = []
        evidence_response = None
        mental_model = None
    supplemental_results = []
    # Coverage expansion is an explicit, bounded second read for thin primary
    # recall.  It increases evidence breadth without accepting irrelevant rows:
    # the ordinary governance/admission pipeline below still evaluates every row.
    expansion = config.get("recallCoverageExpansion") or {}
    if (
        expansion.get("enabled")
        and not noop_long_term
        # A source-first authority turn already has the only permitted source.
        # Never let a generic “coverage” retry reopen the historical Bank: it
        # would reintroduce policy/model sidecars after the strict authority
        # filter and falsely inflate actual injection.
        and not live_authority_only
        and len(results) < int(expansion.get("minPrimaryResults", 4))
        # A small but coverage-complete first recall is already sufficient.
        # Do not pay for a second broad lookup merely to reach an item count:
        # completeness is a semantic receipt, count is not.
        and not bool(controller_receipt.get("coverage_complete"))
    ):
        supplemental_query = str(expansion.get("queryPrefix") or "当前任务需要补齐相关历史事实、已做决策、实施进度、未完成项和回退条件。") + "\n当前问题：" + semantic_query
        try:
            supplemental = client.recall(
                bank_id=bank_id, query=supplemental_query[:4000], raw_user_prompt=raw_prompt,
                full_prompt=agent_full_prompt, full_prompt_source=resolved_full_prompt_source,
                max_tokens=int(expansion.get("maxTokens", 2200)), budget=str(expansion.get("budget", "high")),
                types=(query_plan or {}).get("types") or config.get("recallTypes"),
                prefer_observations=True, timeout=min(int(expansion.get("timeout", 8)), recall_timeout),
            )
            supplemental_results = list(supplemental.get("results") or [])
            if contract_enforced:
                supplemental_receipt = (supplemental.get("query_controller") or {}) if isinstance(supplemental, dict) else {}
                supplemental_context = dict(controller_contract_context)
                supplemental_context["execution_id"] = str(supplemental_receipt.get("execution_id") or supplemental_receipt.get("query_id") or "")
                supplemental_enforcement = enforce_controller_admission(supplemental_results, supplemental_receipt, supplemental_context)
                if supplemental_enforcement["state"] == "enforced":
                    results = dedupe_recall_results(results + supplemental_enforcement["items"])
                    contract_enforcement["supplemental"] = "enforced"
                else:
                    contract_enforced = False
                    results = dedupe_recall_results(legacy_results_before_contract + supplemental_results)
                    contract_enforcement.update(state="legacy_fallback_required", reason="supplemental_contract_gap", supplemental=supplemental_enforcement["state"])
            else:
                results = dedupe_recall_results(results + supplemental_results)
        except Exception as error:
            debug_log(config, f"Coverage expansion failed: {error}")
    if not contract_enforced:
        results = filter_by_min_scores(results, config.get("recallMinScores") or {}, config)
    if noop_long_term:
        # A no-op continuation normally receives no historical facts.  A
        # conditional semantic-dependency contract is different: it carries no
        # stale project claim and makes a local edit check complete across the
        # current artifact.  Preserve only that contract when its explicit
        # trigger is present.
        results = semantic_dependency_contract_results(semantic_query, config)
    elif not contract_enforced:
        results = apply_recall_governance(semantic_query, results, config)
        # Governance may prepend deterministic authority facts.  Deduplicate
        # once more so an equivalent historical hit cannot crowd the prompt.
        results = dedupe_recall_results(results)
    if strict_source_first:
        # ``apply_recall_governance`` may legitimately add global policy rows
        # for ordinary questions.  They are not permitted to cross a strict
        # current-source boundary.  Preserve only an executable deterministic
        # authority snapshot (for example the live backup script) because it
        # is the source being checked, not a historical Bank memory.
        results = [
            item for item in results
            if str((item.get("metadata") or {}).get("source") or "").endswith("-authority") or (source_priority_guidance and item.get('type') in {'observation','mental_model','direct_policy'})
        ]
    elif live_authority_only:
        # The Controller has explicitly marked this turn source-first: the
        # Hook is holding the current executable authority, not asking for a
        # broad Hindsight explanation.  ``apply_recall_governance`` adds a
        # useful current-authority row, but it may also append a globally
        # relevant directive or a stable-model sidecar which happens to share
        # a platform word such as “Hindsight”.  Those additions are not part
        # of the requested current proposition and make the UI falsely report
        # a rich recall.  Keep only the authority class for this strict path.
        # The Controller normally returns ``source_guard.active`` too.  The
        # Hook-side rule is intentionally independently enforced: a restart or
        # a stale controller plan must never turn an unknown-attribute safety
        # answer into a bulk injection of the very unrelated memories it warns
        # against.
        # Some predicates intentionally overlap (a backup question also
        # contains the word Hindsight, so it can look like a generic runtime
        # question).  Select the *most specific* authority family instead of
        # merely allowing every current-source row.
        if backup_authority_query:
            current_authority_sources = {"backup-runtime-authority"}
        elif is_current_coding_plan_query(intent_query):
            current_authority_sources = {"coding-plan-runtime-authority"}
        elif is_identity_query(intent_query, config):
            current_authority_sources = {"identity-authority"}
        elif is_wps_sync_current_query(intent_query):
            current_authority_sources = {"wps-sync-runtime-authority"}
        elif unknown_attribute_policy_query:
            current_authority_sources = {"unknown-attribute-policy-authority"}
        elif smalltalk_memory_policy_query:
            current_authority_sources = {"smalltalk-memory-policy-authority"}
        elif local_edit_memory_policy_query:
            current_authority_sources = {"local-edit-memory-policy-authority"}
        else:
            current_authority_sources = {"local-runtime-authority"}
        results = [
            item for item in results
            if str((item.get("metadata") or {}).get("source") or "")
            in current_authority_sources
        ]
    recall_candidates = list(results)
    if contract_enforced:
        identity_subject_rejected=[]
        relevance_rejected=[]
        negative_anchor=[]
        negative_calibration=False
        scope_admission_note='controller_contract_enforced'
    else:
        results, identity_subject_rejected = filter_identity_subject_items(
            semantic_query,
            results,
            config,
            explicit_evidence=(explicit_user_source_request(semantic_query) or explicit_user_source_request(intent_query)),
        )
        results, relevance_rejected = admit_recall_results(semantic_query, results, query_plan)
        # Optional shared/graph recall lanes are merged above. Apply the
        # timeline evidence boundary before a side lane can reintroduce a
        # previous question as a historical stage.
        results, timeline_non_evidence_rejected = filter_timeline_evidence(semantic_query, results)
        relevance_rejected.extend(timeline_non_evidence_rejected)
        results, question_echo_rejected = filter_question_echo_evidence(semantic_query, results)
        relevance_rejected.extend(question_echo_rejected)
        results, generic_entity_rejected = filter_generic_entity_governance_evidence(semantic_query, results)
        relevance_rejected.extend(generic_entity_rejected)
        results, domain_collision_rejected = suppress_domain_collisions(semantic_query, results)
        results, explicit_constraint_rejected = prefer_explicit_current_constraint(semantic_query, results)
        relevance_rejected.extend(domain_collision_rejected)
        relevance_rejected.extend(explicit_constraint_rejected)
        results, negative_anchor, negative_calibration = filter_specific_personal_attribute(
            semantic_query, results, query_plan
        )
        results, scope_admission_note = suppress_scope_free_inventory(semantic_query, results, query_plan)
    if negative_calibration:
        # A point question such as "我的血型是什么？" is asking for one
        # directly evidenced attribute, not for a nearby general profile.  The
        # attribute gate has established that no candidate proves it; suppress
        # every semantic by-product (including a broad mental-model cache) and
        # return only the explicit insufficiency receipt below.  Otherwise the
        # status says "unknown" while still injecting unrelated material.
        results = []
        supplemental_results = []
        mental_model = None
    results, primary_context_suppressed, primary_context_metrics = coordinate_context_memory_items(
        results, context_memory_profile, config
    )
    # The supplemental call has already passed the Controller's same policy
    # and coverage admission.  Keep those independently selected items when
    # the local narrow-query gate would otherwise discard the whole coverage
    # lane merely because the original user wording is terse.
    if supplemental_results and not contract_enforced and (config.get("recallCoverageExpansion") or {}).get("trustControllerAdmission", False):
        approved_extra = supplemental_results
        approved_extra, expansion_context_suppressed, expansion_context_metrics = coordinate_context_memory_items(
            approved_extra, context_memory_profile, config
        )
        results = dedupe_recall_results(results + approved_extra)
        primary_context_suppressed.extend(expansion_context_suppressed)
        primary_context_metrics = merge_context_memory_metrics(primary_context_metrics, expansion_context_metrics)
    relevance_rejected.extend(identity_subject_rejected)
    relevance_rejected.extend(primary_context_suppressed)
    relevance_rejected.extend(provenance_sidecar_rejected)

    # If primary recall is weak, expand to the unified raw-evidence bank.  This
    # path is intentionally sequential because the primary result decides
    # whether the extra lookup is needed.
    if (
        evidence_response is None
        and not noop_long_term
        and config.get("evidenceFallbackEnabled", False)
        and config.get("evidenceUnifiedEnabled", False)
        and not live_authority_only
        and not negative_calibration
        and not contract_enforced
        and should_expand_evidence(
            intent_query,
            results,
            config,
            has_mental_model=bool(format_mental_model(mental_model or {})),
        )
    ):
        evidence_route = {
            "bankId": config.get("evidenceUnifiedBankId", "user-evidence-unified-v1"),
            "label": "跨来源统一用户原话证据",
            "unified": True,
        }
        debug_log(config, f"Weak primary recall; expanding evidence to '{evidence_route['bankId']}'")
        try:
            evidence_response = recall_evidence(evidence_route)
        except Exception as error:
            print(f"[Evolving Profile] Evidence fallback failed: {error}", file=sys.stderr)

    evidence_results = []
    evidence_bank_id = None
    evidence_label = None
    if evidence_route:
        evidence_bank_id = evidence_route["bankId"]
        evidence_label = evidence_route.get("label") or evidence_bank_id
        # The upstream token budget already bounds volume.  Do not impose a
        # second fixed item cap: an explicit source audit may legitimately need
        # many short, relevant quotations.
        evidence_results = list((evidence_response or {}).get("results") or [])
        # The secondary Bank is not an exemption from source/rule governance.
        # Regenerate query-dependent local authorities under the current policy.
        evidence_results = apply_recall_governance(semantic_query,evidence_results,config)
        if asks_for_direct_evidence(semantic_query, config) or asks_for_direct_evidence(intent_query, config):
            evidence_results = filter_direct_evidence_by_named_anchors(semantic_query, evidence_results)
        evidence_candidates = list(evidence_results)
        evidence_results, evidence_relevance_rejected = admit_recall_results(semantic_query, evidence_results, query_plan)
        evidence_results, evidence_context_suppressed, evidence_context_metrics = coordinate_context_memory_items(
            evidence_results, context_memory_profile, config
        )
        evidence_relevance_rejected.extend(evidence_context_suppressed)
    else:
        evidence_candidates = []
        evidence_relevance_rejected = []
        evidence_context_metrics = {}

    context_memory_coordination = merge_context_memory_metrics(
        primary_context_metrics, evidence_context_metrics, live_context_metrics
    )

    mental_model_candidates = []
    mental_model_rejected = []
    if mental_model and _invalid_mental_model(str(mental_model.get("content") or ""), mental_model, config):
        debug_log(config, "Discarded empty, stale, sourceless, or invalid mental model")
        mental_model = None
    if mental_model:
        # Keyword routing only chooses a mental-model *candidate*.  It is not
        # permission to inject the whole synthesized profile.  Previously a
        # broad keyword such as “决策” selected the business model for a query
        # about low-probability residual risk, bypassing the same relevance gate
        # applied to facts and observations.  Score heading-bounded sections
        # independently before any section crosses the Hook boundary.
        mental_content = str(mental_model.get("content") or "").strip()
        sections = split_mental_model_sections(mental_content)
        # A memory-architecture question must not inherit unrelated chapters
        # from a broad personal model just because they share the same parent
        # document.  This is a topical boundary, not a count limit: every
        # section that actually discusses memory/context/recall remains
        # eligible.
        compact_intent = re.sub(r"\s+", "", semantic_query).casefold()
        memory_architecture_query = any(term in compact_intent for term in ("hindsight", "agentmemory", "记忆", "上下文", "召回", "注入", "controller"))
        if memory_architecture_query:
            memory_terms = ("hindsight", "agentmemory", "记忆", "上下文", "召回", "注入", "hooks", "controller", "证据")
            sections = [section for section in sections if any(term in re.sub(r"\s+", "", section).casefold() for term in memory_terms)]
        scores, score_error = relevance_cross_encoder_scores(semantic_query, sections)
        admitted_sections = []
        base_id = mental_model.get("id") or f"mental:{mental_model_route.get('mentalModelId') if mental_model_route else 'unknown'}"
        for index, section in enumerate(sections):
            mental_item = {
                "id": f"{base_id}:section:{index + 1}",
                "type": "mental_model",
                "content": section,
                "metadata": {
                    "source": "mental-model",
                    "mental_model_route": (mental_model_route or {}).get("mentalModelId"),
                    "mental_model_parent_id": base_id,
                    "mental_model_section": index + 1,
                    "semantic_relevance_score": scores[index] if index < len(scores) else None,
                    "reranker_error": score_error,
                },
            }
            decision = relevance_admission_decision(semantic_query, mental_item, deep=True)
            mental_item["metadata"]["_ccy_admission"] = decision
            mental_model_candidates.append(mental_item)
            if decision.get("decision") == "qualified":
                admitted_sections.append(mental_item)
            else:
                mental_model_rejected.append(mental_item)
        if not admitted_sections:
            debug_log(config, "Rejected every routed mental-model section as unrelated")
            mental_model = None
        else:
            # Preserve every relevant section; volume is bounded by the model's
            # own finite section set rather than an arbitrary item-count cap.
            # Facts/observations still have their independent adaptive budget.
            mental_model = dict(mental_model)
            mental_model["content"] = "\n\n".join(item["content"] for item in admitted_sections)
            mental_model["_ccy_section_items"] = admitted_sections
    mental_model_formatted = format_mental_model(mental_model or {})
    source_guard_section = ""
    if not is_shadow_replay() and source_guard.get("active"):
        label = str(source_guard.get("source_label") or project_runtime.get("source_label") or "本轮当前来源")
        source_guard_section = (
            "\n<evolving_profile_current_source_guard>\n"
            f"当前来源优先：{label}。{source_guard.get('reason') or '先读取当前来源，再决定是否补充旧记忆。'} "
            "此轮不得把旧 Hindsight 记忆当作当前版本事实；旧记忆仅可在读取当前来源后作为背景或历史对照。\n"
            "</evolving_profile_current_source_guard>\n"
        )
    authority_snapshots = [] if (is_shadow_replay() or noop_long_term or source_guard.get("active")) else select_deterministic_authority_snapshots(semantic_query, config)
    authority_snapshot_section = format_deterministic_authority_snapshots(authority_snapshots)
    if not results and not evidence_results and not mental_model_formatted and not authority_snapshot_section and not source_guard_section and not live_context_reactivation_section:
        append_recall_audit(config, {
            "profile": recall_settings["profile"], "query_chars": len(query), "intent_query_chars": len(intent_query),
            "primary_results": 0, "evidence_results": 0, "mental_model": None,
            "sufficient": False, "elapsed_seconds": round(time.monotonic() - recall_started, 4),
        })
        if noop_long_term:
            context_memory_coordination = dict(context_memory_coordination)
            context_memory_coordination.update({
                "working_set_sufficient": True,
                "memory_request_skipped": True,
                "working_set_reason": "当前任务最近上下文已经足以继续，不再请求或重复注入长期记忆。",
            })
            query_id = str((query_plan or {}).get("query_id") or hashlib.sha256(intent_query.encode("utf-8")).hexdigest()[:16])
            feedback_payload = {
                "query_id": query_id,
                "stage": "injection",
                "session_id": session_id,
                **turn_identity,
                "bank_id": bank_id,
                **empty_injection_receipt_fields(
                    reason="当前任务工作集已覆盖；本轮明确跳过长期记忆，未构建可投递 Packet。",
                ),
                "injected_ids": [],
                "injected_items": [],
                "use_status": "not_applicable",
                "memory_action": "noop_long_term",
                "pipeline_stages": build_memory_pipeline_stages(
                    controller_receipt,
                    candidate_items=[],
                    hook_admitted_items=[],
                    hook_rejected_items=[],
                    deferred_items=[],
                    injected_items=[],
                ),
                "full_prompt_resolution": full_prompt_resolution,
                "session_context_index": session_context_index,
                "session_context_bundle": session_context_bundle.get("receipt") or {},
                "memory_needs": controller_receipt.get("memory_needs"),
                "guidance_sidecar": controller_receipt.get("guidance_sidecar"),
                "feedback_id": hashlib.sha256(f"injection:{query_id}:none".encode("utf-8")).hexdigest()[:24],
            }
            injection_submitted = publish_empty_injection(
                feedback_payload, config, controller_receipt, [], []
            )
            state = {
                "context": "",
                "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "session_id": session_id,
                "query_id": query_id,
                "bank_id": bank_id,
                "result_count": 0,
                "memory_action": "noop_long_term",
                "injection_receipt_state": feedback_payload["injection_receipt_state"],
                "injected_count": feedback_payload["injected_count"],
                "candidate_count": feedback_payload["candidate_count"],
                "rejected_count": feedback_payload["rejected_count"],
                "deferred_count": feedback_payload["deferred_count"],
                "packet_delivery": feedback_payload["packet_delivery"],
                "memory_query_plan": query_plan,
                "injected_ids": [],
                "injected_items": [],
                "pipeline_stages": feedback_payload["pipeline_stages"],
                "context_memory_coordination": context_memory_coordination,
                "full_prompt_resolution": full_prompt_resolution,
                "session_context_index": session_context_index,
                "session_context_bundle": session_context_bundle.get("receipt") or {},
                "memory_needs": feedback_payload["memory_needs"],
                "guidance_sidecar": feedback_payload["guidance_sidecar"],
                "guidance_receipt": feedback_payload["guidance_receipt"],
                "injection_feedback_submitted": injection_submitted,
            }
            persist_hook_state(LAST_RECALL_STATE, state)
            persist_hook_state(session_recall_state_name(session_id), state)
            debug_log(config, "Current working set is sufficient; long-term recall skipped")
            return
        if int(context_memory_coordination.get("recent_duplicate_suppressed_count") or 0) > 0:
            query_id = str((query_plan or {}).get("query_id") or hashlib.sha256(intent_query.encode("utf-8")).hexdigest()[:16])
            rejected_items = [feedback_item(item) for item in (relevance_rejected + evidence_relevance_rejected)]
            candidate_items = [feedback_item(item) for item in (recall_candidates + evidence_candidates)]
            pipeline_stages = build_memory_pipeline_stages(
                controller_receipt,
                candidate_items=candidate_items,
                hook_admitted_items=[],
                hook_rejected_items=rejected_items,
                deferred_items=[feedback_item(item) for item in controller_deferred_candidates],
                injected_items=[],
            )
            feedback_payload = {
                "query_id": query_id,
                "stage": "injection",
                "session_id": session_id,
                **turn_identity,
                "bank_id": bank_id,
                **empty_injection_receipt_fields(
                    candidate_items=candidate_items,
                    rejected_items=rejected_items,
                    deferred_items=controller_deferred_candidates,
                    reason="当前任务上下文已覆盖候选记忆；本轮明确抑制重复注入。",
                ),
                "injected_ids": [],
                "injected_items": [],
                "candidate_items": candidate_items,
                "rejected_items": rejected_items,
                "use_status": "not_applicable",
                "memory_action": "current_context_covered",
                "context_memory_coordination": context_memory_coordination,
                "pipeline_stages": pipeline_stages,
                "full_prompt_resolution": full_prompt_resolution,
                "session_context_index": session_context_index,
                "session_context_bundle": session_context_bundle.get("receipt") or {},
                "memory_needs": controller_receipt.get("memory_needs"),
                "guidance_sidecar": controller_receipt.get("guidance_sidecar"),
                "feedback_id": hashlib.sha256(f"injection:{query_id}:current-context-covered".encode("utf-8")).hexdigest()[:24],
            }
            injection_submitted = publish_empty_injection(
                feedback_payload, config, controller_receipt, candidate_items,
                [feedback_item(item) for item in controller_deferred_candidates],
            )
            state = {
                "context": "",
                "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "session_id": session_id,
                "query_id": query_id,
                "bank_id": bank_id,
                "result_count": 0,
                "memory_action": "current_context_covered",
                "injection_receipt_state": feedback_payload["injection_receipt_state"],
                "injected_count": feedback_payload["injected_count"],
                "candidate_count": feedback_payload["candidate_count"],
                "rejected_count": feedback_payload["rejected_count"],
                "deferred_count": feedback_payload["deferred_count"],
                "packet_delivery": feedback_payload["packet_delivery"],
                "memory_query_plan": query_plan,
                "injected_ids": [],
                "injected_items": [],
                "candidate_items": feedback_payload["candidate_items"],
                "rejected_items": rejected_items,
                "pipeline_stages": pipeline_stages,
                "context_memory_coordination": context_memory_coordination,
                "full_prompt_resolution": full_prompt_resolution,
                "session_context_index": session_context_index,
                "session_context_bundle": session_context_bundle.get("receipt") or {},
                "memory_needs": feedback_payload["memory_needs"],
                "guidance_sidecar": feedback_payload["guidance_sidecar"],
                "guidance_receipt": feedback_payload["guidance_receipt"],
                "injection_feedback_submitted": injection_submitted,
            }
            persist_hook_state(LAST_RECALL_STATE, state)
            persist_hook_state(session_recall_state_name(session_id), state)
            debug_log(config, "Recent live context already covers every admitted memory; skipped duplicate injection")
            return
        # Evidence requests and deterministic channel guards must still receive
        # an explicit insufficiency/authority result even when semantic recall
        # returns no candidates.
        if (
            not negative_calibration
            and not (asks_for_direct_evidence(semantic_query, config) or asks_for_direct_evidence(intent_query, config))
            and not delivery_channel_guard(intent_query)
        ):
            # A real zero-recall outcome is still a real user-turn outcome.
            # Previously this early return skipped the feedback receipt, so
            # 9998 could show a Controller/Search node but no Output node and
            # users could not tell whether “0” meant an intentional decision,
            # a timeout, or a broken link.  Persist the same auditable shape as
            # all other exits; no memory content crosses the prompt boundary.
            query_id = str(
                ((response or {}).get("query_controller") or {}).get("query_id")
                or (query_plan or {}).get("query_id")
                or hashlib.sha256(intent_query.encode("utf-8")).hexdigest()[:16]
            )
            zero_candidate_items = [feedback_item(item) for item in (
                recall_candidates + evidence_candidates + controller_rejected_candidates + controller_deferred_candidates
            )]
            zero_hook_rejected_items = [feedback_item(item) for item in (
                relevance_rejected + evidence_relevance_rejected + mental_model_rejected
            )]
            zero_rejected_items = zero_hook_rejected_items + [
                feedback_item(item) for item in controller_rejected_candidates
            ]
            zero_deferred_items = [feedback_item(item) for item in controller_deferred_candidates]
            zero_pipeline_stages = build_memory_pipeline_stages(
                controller_receipt,
                candidate_items=zero_candidate_items,
                hook_admitted_items=[],
                hook_rejected_items=zero_hook_rejected_items,
                deferred_items=zero_deferred_items,
                injected_items=[],
            )
            zero_payload = {
                "query_id": query_id,
                "stage": "injection",
                "session_id": session_id,
                **turn_identity,
                "bank_id": bank_id,
                **empty_injection_receipt_fields(
                    candidate_items=zero_candidate_items,
                    rejected_items=zero_rejected_items,
                    deferred_items=zero_deferred_items,
                    reason="检索完成但没有候选通过相关性准入；空 Packet 被明确记录为未投递。",
                ),
                "injected_ids": [],
                "injected_items": [],
                "candidate_items": zero_candidate_items,
                "rejected_items": zero_rejected_items,
                "deferred_items": zero_deferred_items,
                "pipeline_stages": zero_pipeline_stages,
                "full_prompt_resolution": full_prompt_resolution,
                "session_context_index": session_context_index,
                "session_context_bundle": session_context_bundle.get("receipt") or {},
                "use_status": "not_applicable",
                "memory_action": str(controller_receipt.get("memory_action") or (query_plan or {}).get("memory_action", "focused_recall")),
                "project_key": project_runtime.get("project_key"),
                "source_guard": source_guard,
                "scope_admission_note": scope_admission_note or "检索未找到可通过相关性准入的长期记忆；本轮没有注入。",
                "context_chars": 0,
                "context_memory_coordination": context_memory_coordination,
                "memory_needs": controller_receipt.get("memory_needs"),
                "guidance_sidecar": controller_receipt.get("guidance_sidecar"),
                "admission_contract_shadow": controller_contract_shadow_receipt,
                "admission_contract_enforcement": contract_enforcement,
                "admission_decisions": controller_receipt.get("admission_decisions") or [],
                "source_revision": source_revision,
                "admission_policy": controller_contract_context["policy_version"],
                "feedback_id": hashlib.sha256(f"injection:{query_id}:empty".encode("utf-8")).hexdigest()[:24],
            }
            # Empty is a deliberate Hook response too.  Going straight to the
            # feedback ledger made it impossible to join this user turn to an
            # output receipt, so 9998 had to guess whether zero meant failure.
            injection_submitted = publish_empty_injection(
                zero_payload, config, controller_receipt, zero_candidate_items, zero_deferred_items
            )
            state = {
                "context": "", "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "session_id": session_id, "query_id": query_id, "bank_id": bank_id,
                "result_count": 0, "memory_action": zero_payload["memory_action"],
                "injection_receipt_state": zero_payload["injection_receipt_state"],
                "injected_count": zero_payload["injected_count"],
                "candidate_count": zero_payload["candidate_count"],
                "rejected_count": zero_payload["rejected_count"],
                "deferred_count": zero_payload["deferred_count"],
                "packet_delivery": zero_payload["packet_delivery"],
                "memory_query_plan": query_plan, "injected_ids": [], "injected_items": [],
                "candidate_items": zero_payload["candidate_items"],
                "rejected_items": zero_payload["rejected_items"],
                "pipeline_stages": zero_pipeline_stages,
                "context_memory_coordination": context_memory_coordination,
                "full_prompt_resolution": full_prompt_resolution,
                "session_context_index": session_context_index,
                "session_context_bundle": session_context_bundle.get("receipt") or {},
                "memory_needs": zero_payload["memory_needs"],
                "guidance_sidecar": zero_payload["guidance_sidecar"],
                "guidance_receipt": zero_payload["guidance_receipt"],
                "injection_feedback_submitted": injection_submitted,
            }
            persist_hook_state(LAST_RECALL_STATE, state)
            persist_hook_state(session_recall_state_name(session_id), state)
            debug_log(config, "No memories found")
            return

    # The primary, coverage, evidence and mental-model lanes converge only at
    # this point.  Apply cross-lane safety gates once more here: otherwise a
    # late coverage retry or a synthesized model section could reintroduce a
    # candidate already rejected from the primary lane.
    mental_section_items = list((mental_model or {}).get("_ccy_section_items") or [])
    final_candidates = list(results) + list(evidence_results) + mental_section_items
    if contract_enforced:
        final_domain_rejected=[]
        final_constraint_rejected=[]
    else:
        final_candidates, final_domain_rejected = suppress_domain_collisions(semantic_query, final_candidates)
        final_candidates, final_constraint_rejected = prefer_explicit_current_constraint(semantic_query, final_candidates)
    final_ids = {str(item.get("id") or "") for item in final_candidates}
    results = [item for item in results if str(item.get("id") or "") in final_ids]
    evidence_results = [item for item in evidence_results if str(item.get("id") or "") in final_ids]
    if mental_model:
        retained_sections = [item for item in mental_section_items if str(item.get("id") or "") in final_ids]
        if retained_sections:
            mental_model = dict(mental_model)
            mental_model["_ccy_section_items"] = retained_sections
            mental_model["content"] = "\n\n".join(str(item.get("content") or item.get("text") or "") for item in retained_sections)
            mental_model_formatted = format_mental_model(mental_model)
        else:
            mental_model = None
            mental_model_formatted = ""
    relevance_rejected.extend(final_domain_rejected)
    relevance_rejected.extend(final_constraint_rejected)

    debug_log(config, f"Building Memory Packet from {len(results)} primary and {len(evidence_results)} evidence memories")

    # This replaces the old flat concatenation.  A claim bundle preserves all
    # supporting evidence IDs and version times, while Packet delivery makes
    # it impossible for 9998 to confuse a retrieved candidate with a context
    # block that actually crossed the Hook boundary.
    # Canonicalise hook-owned records before the Packet is built.  This is the
    # single identity boundary shared by rendered bundle, transport list and
    # post-Hook delivery receipt.
    results = [canonical_pipeline_item(item) for item in results]
    evidence_results = [canonical_pipeline_item(item) for item in evidence_results]
    mental_section_items = [canonical_pipeline_item(item) for item in mental_section_items]
    packet_source_items = list(results) + list(evidence_results) + list(mental_section_items)
    authority_packet_items = [
        {
            "id": f"authority:{snapshot.get('id') or hashlib.sha256(str(snapshot.get('text') or '').encode('utf-8')).hexdigest()[:16]}",
            "type": "authority_snapshot",
            "text": str(snapshot.get("text") or ""),
            "metadata": {"source": "deterministic-authority", "constraint": True},
        }
        for snapshot in authority_snapshots
        if str(snapshot.get("text") or "").strip()
    ]
    packet_source_items.extend(authority_packet_items)
    packet_full_prompt = str((executed_query_plan or {}).get("full_prompt") or query or intent_query)
    packet_source = str((executed_query_plan or {}).get("full_prompt_source") or "fallback_context_envelope")
    packet_max_chars=int(config.get("memoryPacketMaxChars", 24000))
    guidance_needed=bool((controller_receipt.get("memory_needs") or {}).get("guidance",{}).get("needed"))
    guidance_reserve_chars=(
        int(config.get("guidancePacketReserveChars", max(360,min(900,round(packet_max_chars*.18)))) )
        if guidance_needed else 0
    )
    guidance_preferred_types=["mental_model","observation"] if guidance_needed else []
    memory_packet = build_memory_packet(
        full_prompt=packet_full_prompt,
        items=packet_source_items,
        required_slots=list((executed_query_plan or {}).get("coverage_dimensions") or []),
        max_rendered_chars=packet_max_chars,
        guidance_reserve_chars=guidance_reserve_chars,
        guidance_preferred_types=guidance_preferred_types,
        full_prompt_source=packet_source,
        # Reuse the Controller's completed evidence receipt at the Packet
        # boundary.  Official Hindsight records generally have no HAM-OS slot
        # labels, so relying only on per-row ``fills_slots`` made every real
        # injection appear to miss all required dimensions in 9998.  The
        # Packet still refuses this bridge when the Controller reported a gap
        # or any admitted bundle was deferred by transport budget.
        validated_coverage=dict((controller_receipt or {}).get("coverage_receipt") or {}),
    )
    # Packet includes fact, evidence, graph and guidance sections in one
    # provenance-preserving structure.  Do not append the old formatter after
    # it, otherwise duplicate text can falsely inflate actual injection.
    memories_formatted = memory_packet["rendered_context"]
    evidence_section = ""
    mental_model_section = ""
    negative_calibration_section = ""
    if negative_calibration:
        negative_calibration_section = (
            "\n<memory_recall_status>"
            f"当前索引中没有直接提及“{negative_anchor}”的证据；已过滤仅有向量相似、"
            "但未出现该属性的候选。不得据此猜测具体答案。"
            "</memory_recall_status>\n"
        )

    evidence_status = ""
    explicit_evidence = (
        (asks_for_direct_evidence(semantic_query, config) or asks_for_direct_evidence(intent_query, config))
        or force_iphone_evidence
        or bool(query_plan and query_plan.get("requires_direct_evidence"))
    )
    sufficient = bool(results or evidence_results or mental_model_formatted or authority_snapshot_section or handoff_section or source_guard_section or live_context_reactivation_section)
    if config.get("evidenceSufficiencyEnabled", True) and explicit_evidence and not evidence_results:
        evidence_status = (
            "\n<evidence_status>当前召回未取得足以支持原话、来源或时间断言的直接证据；"
            "可以给出待核验线索，但不得把推断写成已证实事实。</evidence_status>\n"
        )

    query_receipt_section = ""
    if executed_query_plan and (
        executed_query_plan.get("coverage_required")
        or executed_query_plan.get("planning_probe_fallback")
    ):
        query_receipt_section = (
            "\n<memory_query_receipt>"
            f"shape={executed_query_plan.get('primary_shape')}｜"
            f"strategies={','.join(executed_query_plan.get('strategies') or [])}｜"
            f"coverage={','.join(executed_query_plan.get('coverage_dimensions') or [])}｜"
            f"scope={executed_query_plan.get('scope_claim', 'indexed_scope_only')}｜"
            f"planning_probe_fallback={str(bool(executed_query_plan.get('planning_probe_fallback'))).lower()}｜"
            "executed_by_controller=true"
            "</memory_query_receipt>\n"
        )

    delivery_guard = "" if is_shadow_replay() else delivery_channel_guard(intent_query)
    context_message = (
        f"<evolving_profile_memories>\n"
        f"{preamble}\n"
        f"Current time - {current_time}\n\n"
        f"{delivery_guard}\n"
        f"{handoff_section}{source_guard_section}{live_context_reactivation_section}\n{memories_formatted}{mental_model_section}"
        f"{evidence_section}{negative_calibration_section}{evidence_status}{query_receipt_section}\n"
        f"</evolving_profile_memories>"
    )

    context_message, memory_packet = fit_hook_context(context_message, memory_packet, {
        "full_prompt":packet_full_prompt, "items":packet_source_items,
        "required_slots":list((executed_query_plan or {}).get("coverage_dimensions") or []),
        "full_prompt_source":packet_source,
        "guidance_reserve_chars":guidance_reserve_chars,
        "guidance_preferred_types":guidance_preferred_types,
        "validated_coverage":dict((controller_receipt or {}).get("coverage_receipt") or {}),
    }, config)

    append_recall_audit(config, {
        "profile": recall_settings["profile"], "budget": recall_settings["budget"],
        "max_tokens": recall_settings["max_tokens"], "query_chars": len(query), "intent_query_chars": len(intent_query),
        "primary_results": len(results), "shared_results": len(shared_results),
        "evidence_results": len(evidence_results),
        "mental_model": (mental_model or {}).get("id"),
        "authority_snapshot_count": len(authority_snapshots),
        "handoff_used": bool(handoff_section),
        "handoff_source_session": (handoff_record or {}).get("session_id"),
        "negative_calibration": negative_calibration,
        "negative_anchor": negative_anchor,
        "scope_admission_note": scope_admission_note,
        "relevance_candidate_count": len(recall_candidates) + len(evidence_candidates),
        "relevance_admitted_count": len(results) + len(evidence_results),
        "relevance_rejected_count": len(relevance_rejected) + len(evidence_relevance_rejected),
        "relevance_deferred_for_token_budget_count": len(controller_deferred_candidates),
        "sufficient": sufficient,
        "memory_role": memory_role,
        "memory_client": memory_client,
        "query_router": executed_query_plan.get("router"),
        "query_shape": executed_query_plan.get("primary_shape"),
        "query_strategies": executed_query_plan.get("strategies"),
        "coverage_dimensions": executed_query_plan.get("coverage_dimensions"),
        "controller_fallback": executed_query_plan.get("controller_fallback"),
        "planning_probe_fallback": executed_query_plan.get("planning_probe_fallback"),
        "elapsed_seconds": round(time.monotonic() - recall_started, 4),
        "context_memory_coordination": context_memory_coordination,
    })

    query_id = str(
        ((response or {}).get("query_controller") or {}).get("query_id")
        or (query_plan or {}).get("query_id")
        or hashlib.sha256(intent_query.encode("utf-8")).hexdigest()[:16]
    )
    effective_memory_action = (
        "live_context_reactivation"
        if live_context_reactivation_section and not (results or evidence_results or mental_model_formatted or authority_snapshot_section)
        else str(controller_receipt.get("memory_action") or (query_plan or {}).get("memory_action", "focused_recall"))
    )
    transported_record_ids = set(memory_packet.get("transport_confirmed_record_ids") or [])
    packet_feedback_source_items = packet_delivery_source_items(
        results, evidence_results, mental_section_items, authority_packet_items,
    )
    transport_deferred_claim_ids=set(memory_packet.get("deferred_claim_ids") or [])
    transport_deferred_items=[
        feedback_item(item) for item in packet_feedback_source_items
        if str(item.get("claim_id") or item.get("id") or item.get("chunk_id") or "") in transport_deferred_claim_ids
    ]
    injected_items = [
        feedback_item(item) for item in packet_feedback_source_items
        if str(item.get("id") or item.get("chunk_id") or "") in transported_record_ids
    ]
    candidate_items = [feedback_item(item) for item in (recall_candidates + evidence_candidates + controller_rejected_candidates + controller_deferred_candidates)]
    rejected_items = [feedback_item(item) for item in (relevance_rejected + evidence_relevance_rejected + controller_rejected_candidates)]
    candidate_items.extend(feedback_item(item) for item in mental_model_candidates)
    rejected_items.extend(feedback_item(item) for item in mental_model_rejected)
    hook_rejected_items = [feedback_item(item) for item in (
        relevance_rejected + evidence_relevance_rejected + mental_model_rejected
    )]
    deferred_items = [feedback_item(item) for item in controller_deferred_candidates]
    injected_items = [item for item in injected_items if item.get("id")]
    hook_admitted_items = [feedback_item(item) for item in packet_feedback_source_items]
    pipeline_stages = build_memory_pipeline_stages(
        controller_receipt,
        candidate_items=candidate_items,
        hook_admitted_items=hook_admitted_items,
        hook_rejected_items=hook_rejected_items,
        deferred_items=deferred_items,
        injected_items=injected_items,
    )
    injected_ids = list(dict.fromkeys(item["id"] for item in injected_items))
    claim_delivery = build_claim_delivery_receipt(
        controller_receipt,
        injected_items=injected_items,
        candidate_items=candidate_items,
        pipeline_stages=pipeline_stages,
    )
    injection_payload = {
        "query_id": query_id,
        "stage": "injection",
        "session_id": session_id,
        **turn_identity,
        "bank_id": bank_id,
        "injected_ids": injected_ids,
        "injected_items": injected_items,
        # These are the authoritative counts used by the UI.  Do not infer
        # them later from a subset of controller results: Hook-owned authority
        # snapshots and governed identity facts are real injected context too.
        "injected_count": len(injected_items),
        "candidate_count": len(candidate_items),
        "rejected_count": len(rejected_items),
        "deferred_count": len(deferred_items),
        "candidate_items": candidate_items,
        "rejected_items": rejected_items,
        "deferred_items": deferred_items,
        "pipeline_stages": pipeline_stages,
        "memory_packet": memory_packet,
        "packet_delivery": {
            "state": "hook_context_prepared",
            "transport_record_ids": list(memory_packet.get("transport_confirmed_record_ids") or []),
            "deferred_claim_ids": list(memory_packet.get("deferred_claim_ids") or []),
            "reason": "Packet 已构建为本次 Hook additionalContext；Codex Hook 协议没有独立的模型接收确认，因此“实际注入”以该上下文边界计数，回答使用另行回传。",
        },
        "use_status": "unknown",
        "memory_action": effective_memory_action,
        "project_key": project_runtime.get("project_key"),
        "source_guard": source_guard,
        "scope_admission_note": scope_admission_note,
        "context_chars": len(context_message),
        "memory_needs": controller_receipt.get('memory_needs'),
        "guidance_sidecar": controller_receipt.get('guidance_sidecar'),
        "admission_contract_shadow": controller_contract_shadow_receipt,
        "admission_contract_enforcement": contract_enforcement,
        "admission_decisions": controller_receipt.get("admission_decisions") or [],
        "source_revision": source_revision,
        "admission_policy": controller_contract_context["policy_version"],
        "context_memory_coordination": context_memory_coordination,
        "full_prompt_resolution": full_prompt_resolution,
        "session_context_index": session_context_index,
        "session_context_bundle": session_context_bundle.get("receipt") or {},
        "claim_delivery": claim_delivery,
        "feedback_id": hashlib.sha256(
            f"injection:{query_id}:{','.join(injected_ids)}".encode("utf-8")
        ).hexdigest()[:24],
    }
    from lib.memory_needs import guidance_receipt
    injection_payload['guidance_receipt']=guidance_receipt(
        controller_receipt,candidate_items,injected_items,
        deferred_items=list(deferred_items)+transport_deferred_items,
    )
    injection_submitted = publish_injection(context_message, injection_payload, config)
    state = {
            "context": context_message,
            "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "session_id": session_id,
            "query_id": query_id,
            "bank_id": bank_id,
            "result_count": len(results),
            "evidence_bank_id": evidence_bank_id,
            "evidence_result_count": len(evidence_results),
            "memory_query_plan": executed_query_plan,
            "shared_result_count": len(shared_results),
            "iphone_memory_strategy": iphone_strategy,
            "memory_action": effective_memory_action,
            # Mirror the same authoritative counts written to the feedback
            # ledger.  The per-session state is consumed by local diagnostics
            # and self-tests; omitting them made a non-empty Packet look like
            # an unexplained zero even though ``context`` contained records.
            "injected_count": len(injected_items),
            "actual_injected_count": len(injected_items),
            "candidate_count": len(candidate_items),
            "rejected_count": len(rejected_items),
            "deferred_count": len(deferred_items),
            "injection_receipt_state": "verified_itemized" if injected_items else "verified_empty",
            "project_key": project_runtime.get("project_key"),
            "source_guard": source_guard,
            # This receipt is copied from the actual controller response, not
            # inferred from the fallback planning hint. It makes the Hook audit
            # show whether entities affected this concrete injection.
            "entity_resolution": dict(controller_receipt.get("entity_resolution") or {}),
            "controller_execution": {
                "version": controller_receipt.get("version"),
                "execution_complete": controller_receipt.get("execution_complete"),
                "coverage_complete": controller_receipt.get("coverage_complete"),
                "elapsed_deadline_ms": controller_receipt.get("deadline_ms"),
            },
            "claim_delivery": claim_delivery,
            "injected_ids": injected_ids,
            "injected_items": injected_items,
            "candidate_items": candidate_items,
            "rejected_items": rejected_items,
            "deferred_items": deferred_items,
            "pipeline_stages": pipeline_stages,
            "memory_packet": memory_packet,
            "packet_delivery": injection_payload["packet_delivery"],
            "injection_feedback_submitted": injection_submitted,
            "answer_feedback_submitted": False,
            "context_memory_coordination": context_memory_coordination,
            "guidance_receipt": injection_payload['guidance_receipt'],
            "admission_contract_shadow": controller_contract_shadow_receipt,
            "admission_contract_enforcement": contract_enforcement,
            "admission_decisions": controller_receipt.get("admission_decisions") or [],
            "source_revision": source_revision,
            "admission_policy": controller_contract_context["policy_version"],
            "memory_needs": controller_receipt.get('memory_needs'),
            "full_prompt_resolution": full_prompt_resolution,
            "session_context_index": session_context_index,
            "session_context_bundle": session_context_bundle.get("receipt") or {},
        }
    persist_hook_state(LAST_RECALL_STATE, state)
    persist_hook_state(session_recall_state_name(session_id), state)

    # Output was already flushed before its receipt was persisted.


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"[Evolving Profile] Unexpected error in recall: {e}", file=sys.stderr)
        try:
            from lib.config import load_config

            sys.exit(2 if load_config().get("debug") else 0)
        except Exception:
            sys.exit(0)
