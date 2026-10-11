import { epStatePath, EP_STATE_ROOT, EP_API_ENV, EP_HOST_SESSIONS } from "@/lib/ep-state-paths";
import { NextRequest, NextResponse } from "next/server";
import { readdir, readFile } from "node:fs/promises";
import path from "node:path";
import { homedir } from "node:os";
import { readReplayPrompts, enrichHostToolReceipt } from "@/lib/host-tool-replay.server";
import { PromptOriginResolver } from "@/lib/prompt-origin.server";
import { promptPopulationProjection,selectPromptOccurrences } from "@/lib/prompt-origin";
import { EP_DEFAULT_STATE_ROOT } from "@/lib/ep-state-paths";

const RESOURCES = new Set(["units", "models", "candidates", "prompts", "memory-map", "memory-check"]);
const PROMPT_INGRESS_PATH = process.env.EVOLVING_PROFILE_PROMPT_INGRESS_PATH || epStatePath("audit/prompt-ingress.jsonl");
const MEMORY_ROUTE_RECEIPTS_PATH = process.env.EVOLVING_PROFILE_MEMORY_ROUTE_RECEIPTS_PATH || epStatePath("audit/memory-route-receipts");
const GUIDANCE_ENTRY_RECEIPTS_PATH = process.env.EVOLVING_PROFILE_GUIDANCE_ENTRY_RECEIPTS_PATH || epStatePath("audit/guidance-entry-receipts");

async function readJsonDirectory(directory: string) {
  try {
    const files = await readdir(directory);
    const rows = await Promise.all(files.filter((file) => file.endsWith(".json")).map(async (file) => {
      try { return JSON.parse(await readFile(path.join(directory, file), "utf8")); } catch { return null; }
    }));
    return rows.filter(Boolean) as any[];
  } catch { return []; }
}

import { samePromptBinding } from "@/lib/prompt-binding";

function guidanceReceiptFromRow(row: any) {
  const result = row?.result ?? row?.rendered_guidance ?? {};
  return {
    host_state: row?.host_visibility ?? null,
    coverage: row?.coverage ?? result.coverage ?? "not_observed",
    deferred_count: row?.deferred_count ?? result.deferred?.length ?? 0,
    guidance_items: result.included ?? row?.rendered_guidance?.included ?? [],
    model_sections: result.model_sections ?? row?.rendered_guidance?.model_sections ?? [],
    stable_profile_count: row?.stable_profile_count ?? result.stable_profile?.length ?? 0,
    preference_candidate_count: row?.preference_candidate_count ?? result.preference_candidates?.length ?? 0,
    stable_profile: result.stable_profile ?? row?.rendered_guidance?.stable_profile ?? [],
  };
}

async function localPromptList(request: NextRequest) {
  const requestedLimit = Number(request.nextUrl.searchParams.get("limit") || 20);
  const requestedCursor = Number(request.nextUrl.searchParams.get("cursor") || 0);
  const limit = Number.isFinite(requestedLimit) ? Math.max(1, Math.min(Math.trunc(requestedLimit), 50)) : 20;
  const cursor = Number.isFinite(requestedCursor) ? Math.max(Math.trunc(requestedCursor), 0) : 0;
  const host = request.nextUrl.searchParams.get("host") || "all";
  const terms = (request.nextUrl.searchParams.get("q") || "").trim().split(/\s+/).filter(Boolean);
  const promptSource = request.nextUrl.searchParams.get("prompt_source") || "natural";
  try {
    const raw=(await readFile(PROMPT_INGRESS_PATH,"utf8")).trim().split("\n").slice(-2000).flatMap(line=>{try{const row=JSON.parse(line);return row && typeof row==='object' && !Array.isArray(row) ? [row] : []}catch{return[]}});
    const resolver = new PromptOriginResolver([EP_HOST_SESSIONS, process.env.EVOLVING_PROFILE_HOST_ARCHIVED_SESSIONS_ROOT || (EP_STATE_ROOT === EP_DEFAULT_STATE_ROOT ? path.join(homedir(), '.codex/archived_sessions') : epStatePath('archived-host-sessions'))]);
    const candidates = selectPromptOccurrences([...raw, ...await readReplayPrompts()], ['codex-userpromptsubmit', 'codex_host_replay']);
    const originRows=await resolver.resolveRows(candidates,{pageOffset:cursor,pageLimit:limit,host,queryText:terms.join(' '),promptSource});
    const classified = originRows.filter(row =>
      (host.toLowerCase() === 'all' || String(row.host_id || 'codex').toLowerCase() === host.toLowerCase()) &&
      (!terms.length || terms.every(term => row.prompt_preview.includes(term))));
    const { rows, ...sourceProjection } = promptPopulationProjection(classified, promptSource);
    const [routeReceipts, guidanceReceipts] = await Promise.all([readJsonDirectory(MEMORY_ROUTE_RECEIPTS_PATH), readJsonDirectory(GUIDANCE_ENTRY_RECEIPTS_PATH)]);
    const items = await Promise.all(rows.slice(cursor, cursor + limit).map(async (row: any) => {
      const route = routeReceipts.filter((receipt) => samePromptBinding(row, receipt)).sort((a, b) => String(b.updated_at ?? b.at ?? "").localeCompare(String(a.updated_at ?? a.at ?? "")))[0] ?? null;
      const guidance = guidanceReceipts.filter((receipt) => samePromptBinding(row, receipt)).sort((a, b) => String(b.at ?? "").localeCompare(String(a.at ?? "")))[0] ?? null;
      const item = {
        prompt_id: `${row.prompt_fingerprint}:${row.at}`,
        at: row.at,
        user_prompt: row.prompt_preview || "",
        source: row.host_id || "codex",
        origin_kind: row.origin_kind,
        origin_status: row.origin_status,
        origin_evidence: row.origin_evidence,
        routes: {},
        candidate_groups: [],
        memory_route_receipt: route,
        guidance_receipt: guidance ? guidanceReceiptFromRow(guidance) : null,
        local_fallback: true,
        audit_source_scope: route || guidance ? "local_prompt_ingress_joined_audit" : "local_prompt_ingress_only",
      };
      const joined=await enrichHostToolReceipt(item.prompt_id,item);
      // List is metadata only; bodies remain in the on-demand detail response.
      if(joined.memory_route_receipt?.tool_events) joined.memory_route_receipt.tool_events=joined.memory_route_receipt.tool_events.map((e:any)=>{const {mapping_items,scenario_summary_text,...meta}=e;return meta;});
      return joined;
    }));
    const nextCursor = cursor + items.length < rows.length ? String(cursor + items.length) : null;
    return NextResponse.json({ schema: "evolving-profile.guidance-prompts-local-fallback.v2", items, total: rows.length, count: items.length, has_more: nextCursor !== null, next_cursor: nextCursor, cursor, host, ...sourceProjection, source_scope: "local_prompt_ingress_fallback" });
  } catch { return null; }
}

export async function GET(
  request: NextRequest,
  context: { params: Promise<{ resource: string }> }
) {
  const { resource } = await context.params;
  if (!RESOURCES.has(resource)) {
    return NextResponse.json({ error: "Unknown Evolving Profile guidance resource" }, { status: 404 });
  }

  const statusUrl = process.env.EVOLVING_PROFILE_STATUS_API_URL || "http://127.0.0.1:9998";
  const upstream = new URL(`/api/guidance/${resource}`, statusUrl);
  request.nextUrl.searchParams.forEach((value, key) => upstream.searchParams.append(key, value));
  let response: Response;
  try { response = await fetch(upstream, { cache: "no-store", signal: AbortSignal.timeout(3000) }); }
  catch { return resource === "prompts" ? (await localPromptList(request)) ?? NextResponse.json({ error: "guidance_unavailable" }, { status: 503 }) : NextResponse.json({ error: "guidance_unavailable" }, { status: 503 }); }
  if (!response.ok && resource === "prompts") return (await localPromptList(request)) ?? new NextResponse(await response.text(), { status: response.status, headers: { "Content-Type": response.headers.get("Content-Type") ?? "application/json" } });
  const body = await response.text();
  return new NextResponse(body, {
    status: response.status,
    headers: { "Content-Type": response.headers.get("Content-Type") ?? "application/json" },
  });
}
