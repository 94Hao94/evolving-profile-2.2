import { NextRequest, NextResponse } from "next/server";
import { spawn } from "node:child_process";
import { access } from "node:fs/promises";
import { epStatePath, EP_RUNTIME_PYTHON, EP_STATE_ROOT, EP_DEFAULT_STATE_ROOT } from "@/lib/ep-state-paths";
import path from "node:path";
import { GET as qualityEvents } from "../events/route";

export async function POST(request: NextRequest) {
  // The installed worker currently uses Path.home() for receipts. Until it
  // supports an explicit root, isolated installations must not invoke it.
  if (EP_STATE_ROOT !== EP_DEFAULT_STATE_ROOT) return NextResponse.json({ status: "unavailable", code: "runtime_unavailable", apply_status: "unsupported", fallback: "rules", calls: 0, memory_mutated: false }, { status: 503 });
  try {
    const response = await qualityEvents(request);
    const payload = await response.json();
    const events = (payload.events || []).slice(-12).map((row: any) => ({ tool: row.route, status: row.status, evidence: row.evidence, returned_count: row.returned_count, delivered_count: row.delivered_count, prompt_id: row.prompt_id }));
    if (!events.length) return NextResponse.json({ status: "no_samples", calls: 0 });
    const runtime = epStatePath("runtime");
    if (!await access(EP_RUNTIME_PYTHON).then(() => true).catch(() => false) || !await access(path.join(runtime, "host-adapter/lib/jev_judge.py")).then(() => true).catch(() => false)) return NextResponse.json({ status: "unavailable", code: "runtime_unavailable", fallback: "rules", calls: 0, memory_mutated: false }, { status: 503 });
    const result = await new Promise<any>((resolve, reject) => {
      const child = spawn(EP_RUNTIME_PYTHON, [path.join(runtime, "host-adapter/lib/jev_judge.py")], { stdio: ["pipe", "pipe", "pipe"], env: { ...process.env, EVOLVING_PROFILE_STATE_ROOT: epStatePath() } });
      let output = "";
      const timeout = setTimeout(() => { child.kill(); reject(new Error("judge_timeout")); }, 11000);
      child.stdout.on("data", (data) => { output += data.toString(); if (output.length > 80000) child.kill(); });
      child.stderr.on("data", () => undefined);
      child.on("error", (error) => { clearTimeout(timeout); reject(error); });
      child.on("close", (code) => { clearTimeout(timeout); try { code === 0 ? resolve(JSON.parse(output)) : reject(new Error("judge_worker_unavailable")); } catch { reject(new Error("judge_invalid_response")); } });
      child.stdin.on("error", () => undefined);
      child.stdin.end(JSON.stringify({ purposes: ["quality_diagnosis"], state: { tool: "quality_engine_audit", events, binding_summary: payload.binding_summary } }));
    });
    return NextResponse.json(result);
  } catch { return NextResponse.json({ status: "unavailable", fallback: "rules", calls: 0, memory_mutated: false }, { status: 503 }); }
}
