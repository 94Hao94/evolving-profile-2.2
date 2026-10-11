import { epStatePath, EP_STATE_ROOT, EP_API_ENV, EP_HOST_SESSIONS } from "@/lib/ep-state-paths";
import { NextRequest, NextResponse } from "next/server";
import { readdir, readFile } from "node:fs/promises";
import path from "node:path";
import { homedir } from "node:os";
import { FLOW_LANES, summarizeFlowLane, type FlowReceipt } from "@/lib/flow-receipt";

const PROCESS_MEMORY_PATH = epStatePath("process-memory/records.json");
const HOOK_RECEIPTS_PATH = process.env.EVOLVING_PROFILE_HOOK_RECEIPTS_PATH || epStatePath("audit/hook-output-receipts/production");
const PROMPT_INGRESS_PATH = process.env.EVOLVING_PROFILE_PROMPT_INGRESS_PATH || epStatePath("audit/prompt-ingress.jsonl");

async function localHookBinding(promptId: string) {
  const fingerprint = promptId.split(":", 1)[0];
  try {
    const lines = (await readFile(PROMPT_INGRESS_PATH, "utf8")).trim().split("\n").reverse();
    for (const line of lines) {
      const row = JSON.parse(line);
      if (row.prompt_fingerprint === fingerprint) return { turn_id: row.turn_id, session_id: row.session_id, hook_invocation_id: row.hook_invocation_id };
    }
  } catch { /* fallback remains unavailable */ }
  return null;
}

async function localHookReceipts(promptId: string) {
  const binding = await localHookBinding(promptId);
  if (!binding?.turn_id) return [];
  try {
    const files = await readdir(HOOK_RECEIPTS_PATH);
    const rows = await Promise.all(files.filter((file) => file.endsWith(".json")).map(async (file) => {
      try { return JSON.parse(await readFile(path.join(HOOK_RECEIPTS_PATH, file), "utf8")); } catch { return null; }
    }));
    return rows.filter((row: any) => row?.turn_id === binding.turn_id || row?.hook_invocation_id === binding.hook_invocation_id).flatMap((hook: any) => {
      const receipt = hook?.entry_guidance?.agent_process_memory;
      if (!receipt || receipt.required !== true) return [];
      const base = { prompt_id: promptId, session_id: binding.session_id ?? null, project_id: null, lane: "agent_process" as const, source_type: "agent_process_memory", started_at: hook.at ?? null, finished_at: hook.at ?? null };
      return (receipt.records?.length ? receipt.records : [{ id: `agent-process:${promptId}`, kind: "process-retrieval", text: "智能体过程记忆已完成检索。" }]).map((record: any, index: number) => ({ ...base, trace_id: String(record.id || `agent-process:${promptId}:${index}`), stage: String(record.kind || "process-retrieval"), status: receipt.delivery_state === "delivered" ? "delivered" : receipt.delivery_state === "no_candidate" ? "empty" : "observed", candidate_count: Number(receipt.candidate_count || 0) > 0 ? 1 : 0, returned_count: 1, delivered_count: receipt.delivery_state === "delivered" ? 1 : 0, source_ids: record.evidence_links || [], summary: String(record.text || record.kind || "智能体过程记忆候选").slice(0, 500), error: null }));
    });
  } catch { return []; }
}

async function readPromptHookProcessReceipt(promptId: string) {
  const statusUrl = (process.env.EVOLVING_PROFILE_STATUS_API_URL || "http://127.0.0.1:12098").replace(/\/$/, "");
  try {
    const response = await fetch(`${statusUrl}/api/guidance/prompts/${encodeURIComponent(promptId)}`, { cache: "no-store", signal: AbortSignal.timeout(3000) });
    if (!response.ok) return await localHookReceipts(promptId);
    const detail = await response.json();
    const hooks = Array.isArray(detail?.hook_receipts) ? detail.hook_receipts : [];
    return hooks.flatMap((hook: any) => {
      const receipt = hook?.agent_process_memory;
      if (!receipt || receipt.required !== true) return [];
      const records = Array.isArray(receipt.records) ? receipt.records : [];
      const base = {
        prompt_id: promptId,
        session_id: detail?.session_id ?? null,
        project_id: null,
        lane: "agent_process" as const,
        source_type: "agent_process_memory",
        started_at: hook?.at ? new Date(Number(hook.at) * 1000).toISOString() : null,
        finished_at: hook?.at ? new Date(Number(hook.at) * 1000).toISOString() : null,
      };
      if (!records.length) return [{
        ...base,
        trace_id: `agent-process:${promptId}`,
        stage: "process-retrieval",
        status: receipt.delivery_state === "unavailable" ? "unavailable" : receipt.delivery_state === "no_candidate" ? "empty" : "observed",
        candidate_count: Number(receipt.candidate_count || 0),
        returned_count: Number(receipt.candidate_count || 0),
        delivered_count: Number(receipt.delivered_count || 0),
        source_ids: [],
        summary: receipt.delivery_state === "no_candidate" ? "本轮过程记忆检索完成，但没有满足验证门的候选。" : "智能体过程记忆已完成检索。",
        error: null,
      }];
      return records.map((record: any, index: number) => ({
        ...base,
        trace_id: String(record.id || record.process_memory_id || `agent-process:${promptId}:${index}`),
        stage: String(record.kind || record.phase || "process-retrieval"),
        status: receipt.delivery_state === "delivered" ? "delivered" : receipt.delivery_state === "unavailable" ? "unavailable" : "candidate_returned",
        candidate_count: 1,
        returned_count: 1,
        delivered_count: receipt.delivery_state === "delivered" ? 1 : 0,
        source_ids: Array.isArray(record.evidence_links) ? record.evidence_links : [],
        summary: String(record.text || record.applicable_when || record.kind || "智能体过程记忆候选").slice(0, 500),
        error: null,
      }));
    });
  } catch {
    return await localHookReceipts(promptId);
  }
}

export async function GET(request: NextRequest, context: { params: Promise<{ promptId: string }> }) {
  const { promptId } = await context.params;
  let records: any[] = [];
  try {
    const data = JSON.parse(await readFile(PROCESS_MEMORY_PATH, "utf8"));
    records = Array.isArray(data.records) ? data.records : [];
  } catch {
    records = [];
  }
  const matched = records.filter((row) => {
    const context = row?.primary_context ?? {};
    return context.prompt_id === promptId || context.turn_id === promptId || (Array.isArray(row?.source_trace_ids) && row.source_trace_ids.includes(promptId));
  });
  const storedReceipts: FlowReceipt[] = matched.map((row) => ({
    trace_id: String(row.process_memory_id ?? row.source_trace_ids?.[0] ?? `process:${row.kind}`),
    prompt_id: promptId,
    session_id: row.primary_context?.session_id ?? null,
    project_id: row.primary_context?.project_id ?? row.primary_context?.task_id ?? null,
    lane: "agent_process",
    stage: String(row.phase ?? row.kind ?? "observe"),
    status: row.verification_evidence?.length ? "verified" : row.kind === "trace" || row.kind === "event" ? "observed" : row.status === "candidate" ? "candidate_returned" : "unknown",
    started_at: row.created_at ?? null,
    finished_at: row.updated_at ?? row.created_at ?? null,
    candidate_count: row.kind === "pattern" || row.kind === "skill" ? 1 : 0,
    returned_count: 1,
    delivered_count: row.selection_exposure?.selected ? 1 : 0,
    source_type: "agent_process_memory",
    source_ids: Array.isArray(row.source_trace_ids) ? row.source_trace_ids : [],
    summary: row.text ? String(row.text).slice(0, 240) : String(row.kind ?? "process record"),
    error: null,
  }));
  const hookReceipts = await readPromptHookProcessReceipt(promptId);
  const receipts = hookReceipts.length ? hookReceipts : storedReceipts;
  const lanes = FLOW_LANES.map((lane) => summarizeFlowLane(lane, lane === "agent_process" ? receipts : []));
  return NextResponse.json({ schema: "evolving-profile.flow-receipt.v1", prompt_id: promptId, lanes, receipts, source_scope: hookReceipts.length ? "prompt_bound_hook_receipt" : matched.length ? "bound_process_records" : "no_bound_process_receipt", user_data_untouched: true });
}
