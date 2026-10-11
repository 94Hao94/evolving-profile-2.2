import { epStatePath, EP_STATE_ROOT, EP_API_ENV, EP_HOST_SESSIONS } from "@/lib/ep-state-paths";
import { NextRequest, NextResponse } from "next/server";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { homedir } from "node:os";
import { appendMemoryQualityEvents, readMemoryQualityEvents } from "@/lib/memory-quality-ledger";
import { projectMemoryQualityEvents, type MemoryQualityEvent } from "@/lib/memory-quality-event";
import type { FlowPrompt } from "@/lib/flow-projection";
import { enrichHostToolReceipt, readReplayPrompts } from "@/lib/host-tool-replay.server";

const PROMPT_INGRESS_PATH = process.env.EVOLVING_PROFILE_PROMPT_INGRESS_PATH || epStatePath("audit/prompt-ingress.jsonl");
const MCP_ACTIVITY_PATH = process.env.EVOLVING_PROFILE_MCP_ACTIVITY_PATH || epStatePath("audit/mcp-tool-activity.jsonl");

async function readJsonl(filePath: string, limit = 5000): Promise<any[]> {
  try {
    const lines = (await readFile(filePath, "utf8")).split("\n").filter(Boolean).slice(-limit);
    return lines.flatMap((line) => { try { const value = JSON.parse(line); return value && typeof value === "object" ? [value] : []; } catch { return []; } });
  } catch { return []; }
}

function boundToPrompt(activity: any, prompt: any): boolean {
  const promptHook = String(prompt?.hook_invocation_id || "");
  const promptSession = String(prompt?.session_id || "");
  const promptTurn = String(prompt?.turn_id || "");
  const hook = String(activity?.hook_invocation_id || activity?.check_id || "");
  return Boolean((hook && promptHook && hook === promptHook) ||
    (promptSession && promptTurn && activity?.session_id === promptSession && activity?.turn_id === promptTurn));
}

function promptForQuality(row: any, activities: any[]): FlowPrompt {
  return {
    prompt_id: `${row.prompt_fingerprint}:${row.at}`,
    at: row.at,
    user_prompt: row.prompt_preview || "",
    memory_route_receipt: { tool_events: activities },
  };
}

function activitySummary(rows: any[]) {
  const byTool: Record<string, { calls: number; returned: number; candidates: number; latest_at: string | null }> = {};
  for (const row of rows) {
    const tool = String(row.tool || "unknown");
    const current = byTool[tool] || { calls: 0, returned: 0, candidates: 0, latest_at: null };
    current.calls += 1;
    current.returned += typeof row.returned_count === "number" ? row.returned_count : 0;
    current.candidates += typeof row.candidate_count === "number" ? row.candidate_count : 0;
    current.latest_at = !current.latest_at || String(row.at || "") > current.latest_at ? String(row.at || "") : current.latest_at;
    byTool[tool] = current;
  }
  return { state: rows.length ? "observed" : "not_observed", event_count: rows.length, by_tool: byTool,
    boundary: "活动只有在 hook_invocation_id/check_id 或 session+turn 同时匹配时才会进入 Prompt 绑定事件；其余仅作未归因活动。" };
}

export async function GET(request: NextRequest) {
  const limit = Number(request.nextUrl.searchParams.get("limit") || 500);
  const events = await readMemoryQualityEvents(undefined, Number.isFinite(limit) ? limit : 500);
  const [ingress, activities, judgeReviews, replayPrompts] = await Promise.all([readJsonl(PROMPT_INGRESS_PATH, 500), readJsonl(MCP_ACTIVITY_PATH, 5000), readJsonl(epStatePath("audit/jev/reviews.jsonl"), 100), readReplayPrompts()]);
  const prompts=[...ingress,...replayPrompts].filter((row,index,all)=>all.findIndex(item=>item.prompt_fingerprint===row.prompt_fingerprint && item.at===row.at)===index);
  const generated: MemoryQualityEvent[] = [];
  const boundActivity = new Set<any>();
  for (const prompt of prompts) {
    const matching = activities.filter((activity) => boundToPrompt(activity, prompt));
    matching.forEach((activity) => boundActivity.add(activity));
    const bound=promptForQuality(prompt, matching.map(activity=>({...activity,source_type:"mcp_tool_activity"})));
    const enriched=await enrichHostToolReceipt(bound.prompt_id,bound);
    generated.push(...projectMemoryQualityEvents(enriched,"lightweight"));
  }
  const merged = [...events, ...generated].filter((event, index, all) => all.findIndex((candidate) => candidate.event_id === event.event_id) === index).slice(-Math.max(1, Math.min(5000, Number.isFinite(limit) ? limit : 500)));
  const unbound = activities.filter((activity) => !boundActivity.has(activity));
  return NextResponse.json({ schema: "evolving-profile.memory-quality-ledger.v1", events: merged, count: merged.length, source: "append_only_local_ledger_plus_runtime_activity",
    binding_summary: { prompt_bound_events: generated.length, ledger_events: events.length, unattributed_activity: unbound.length, prompt_samples: prompts.length },
    coverage: { runtime_stages: [...new Set(generated.map(event=>event.stage))], ledger_stages: [...new Set(events.map(event=>event.stage))], source_readback: generated.some(event=>event.route==="user_read_source") ? "observed" : "unknown", complete_pipeline: false, boundary: "Runtime receipts and host replay cover observed calls; absent stages have unknown coverage." },
    unattributed_activity: activitySummary(unbound), judge_reviews: judgeReviews.slice(-20), judge_calls: judgeReviews.reduce((total, row) => total + (row.calls || 0), 0) });
}

export async function POST(request: NextRequest) {
  try {
    const body = await request.json();
    const events = Array.isArray(body) ? body : body?.events;
    if (!Array.isArray(events) || events.some((event) => event?.schema !== "evolving-profile.memory-quality-event.v1")) return NextResponse.json({ error: "invalid_memory_quality_events" }, { status: 400 });
    const result = await appendMemoryQualityEvents(events as MemoryQualityEvent[]);
    return NextResponse.json({ schema: "evolving-profile.memory-quality-ledger.v1", ...result });
  } catch { return NextResponse.json({ error: "memory_quality_ledger_write_failed" }, { status: 500 }); }
}
