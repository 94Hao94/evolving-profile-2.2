import { epStatePath, EP_STATE_ROOT, EP_API_ENV, EP_HOST_SESSIONS } from "@/lib/ep-state-paths";
import { NextRequest, NextResponse } from "next/server";
import { readdir, readFile } from "node:fs/promises";
import path from "node:path";
import { homedir } from "node:os";
import { enrichHostToolReceipt, readReplayPrompts } from "@/lib/host-tool-replay.server";

const HOOK_RECEIPTS_PATH = process.env.EVOLVING_PROFILE_HOOK_RECEIPTS_PATH || epStatePath("audit/hook-output-receipts/production");
const PROMPT_INGRESS_PATH = process.env.EVOLVING_PROFILE_PROMPT_INGRESS_PATH || epStatePath("audit/prompt-ingress.jsonl");
const MEMORY_ROUTE_RECEIPTS_PATH = process.env.EVOLVING_PROFILE_MEMORY_ROUTE_RECEIPTS_PATH || epStatePath("audit/memory-route-receipts");
const GUIDANCE_ENTRY_RECEIPTS_PATH = process.env.EVOLVING_PROFILE_GUIDANCE_ENTRY_RECEIPTS_PATH || epStatePath("audit/guidance-entry-receipts");

async function readJsonDirectory(directory: string) {
  try {
    const files = await readdir(directory);
    const rows = await Promise.all(files.filter((file) => file.endsWith(".json")).map(async (file) => { try { return JSON.parse(await readFile(path.join(directory, file), "utf8")); } catch { return null; } }));
    return rows.filter(Boolean) as any[];
  } catch { return []; }
}

import { samePromptBinding } from "@/lib/prompt-binding";

function guidanceReceiptFromRow(row: any) {
  const result = row?.result ?? row?.rendered_guidance ?? {};
  return { host_state: row?.host_visibility ?? null, coverage: row?.coverage ?? result.coverage ?? "not_observed", deferred_count: row?.deferred_count ?? result.deferred?.length ?? 0, guidance_items: result.included ?? row?.rendered_guidance?.included ?? [], model_sections: result.model_sections ?? row?.rendered_guidance?.model_sections ?? [], stable_profile_count: row?.stable_profile_count ?? result.stable_profile?.length ?? 0, preference_candidate_count: row?.preference_candidate_count ?? result.preference_candidates?.length ?? 0, stable_profile: result.stable_profile ?? row?.rendered_guidance?.stable_profile ?? [] };
}

async function localPromptDetail(promptId: string) {
  const fingerprint = promptId.split(":", 1)[0];
  try {
    const raw=(await readFile(PROMPT_INGRESS_PATH,"utf8")).trim().split("\n").map(line=>JSON.parse(line));
    const ingress=[...raw,...await readReplayPrompts()].findLast(row=>`${row.prompt_fingerprint}:${row.at}`===promptId);
    if (!ingress?.turn_id) return null;
    const files = await readdir(HOOK_RECEIPTS_PATH);
    const hooks = await Promise.all(files.filter((file) => file.endsWith(".json")).map(async (file) => { try { return JSON.parse(await readFile(path.join(HOOK_RECEIPTS_PATH, file), "utf8")); } catch { return null; } }));
    const hook = hooks.find((row: any) => samePromptBinding(ingress,row));
    const routeReceipts = await readJsonDirectory(MEMORY_ROUTE_RECEIPTS_PATH);
    const guidanceReceipts = await readJsonDirectory(GUIDANCE_ENTRY_RECEIPTS_PATH);
    const route = routeReceipts.filter((receipt) => samePromptBinding(ingress, receipt)).sort((a, b) => String(b.updated_at ?? b.at ?? "").localeCompare(String(a.updated_at ?? a.at ?? "")))[0] ?? null;
    const guidance = guidanceReceipts.filter((receipt) => samePromptBinding(ingress, receipt)).sort((a, b) => String(b.at ?? "").localeCompare(String(a.at ?? "")))[0] ?? null;
    if (!hook && !route && !guidance && ingress.source!=="codex_host_replay") return null;
    const injected = Array.isArray(hook?.injected_items) ? hook.injected_items : [];
    const agentProcess = hook?.entry_guidance?.agent_process_memory ?? null;
    return {
      schema: "evolving-profile.guidance-prompt-local-fallback.v1",
      prompt_id: promptId,
      session_id: ingress.session_id,
      turn_id: ingress.turn_id,
      user_prompt: ingress.prompt_preview ?? "",
      memory_route_receipt: route,
      guidance_receipt: guidance ? guidanceReceiptFromRow(guidance) : null,
      historical_audit: { route: route?.recommended_route ?? "not_observed", state: route?.tool_events?.length ? "observed" : "not_observed", candidate_count: route?.tool_events?.reduce((sum: number, event: any) => sum + Number(event.candidate_count ?? 0), 0) ?? 0, returned_to_host_count: route?.returned_count ?? route?.tool_events?.reduce((sum: number, event: any) => sum + Number(event.returned_count ?? 0), 0) ?? 0, delivery_state: route?.delivery_state ?? "not_observed", items: [] },
      hook_receipts: agentProcess ? [{ at: hook?.at, agent_process_memory: agentProcess }] : [],
    };
  } catch { return null; }
}

export async function GET(
  request: NextRequest,
  context: { params: Promise<{ promptId: string }> }
) {
  const { promptId } = await context.params;
  const statusUrl = process.env.EVOLVING_PROFILE_STATUS_API_URL || "http://127.0.0.1:9998";
  const upstream = new URL(`/api/guidance/prompts/${encodeURIComponent(promptId)}`, statusUrl);
  request.nextUrl.searchParams.forEach((value, key) => upstream.searchParams.append(key, value));
  let response: Response;
  try { response = await fetch(upstream, { cache: "no-store", signal: AbortSignal.timeout(3000) }); }
  catch { const fallback = await localPromptDetail(promptId); return fallback ? NextResponse.json(await enrichHostToolReceipt(promptId, fallback)) : NextResponse.json({ error: "guidance_unavailable" }, { status: 503 }); }
  if (!response.ok) { const fallback = await localPromptDetail(promptId); return fallback ? NextResponse.json(await enrichHostToolReceipt(promptId, fallback)) : new NextResponse(await response.text(), { status: response.status, headers: { "Content-Type": response.headers.get("Content-Type") ?? "application/json" } }); }
  return NextResponse.json(await enrichHostToolReceipt(promptId, await response.json()));
}
