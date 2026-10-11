import { NextResponse } from "next/server";
import { spawn } from "node:child_process";
import { access } from "node:fs/promises";
import { epStatePath, EP_RUNTIME_PYTHON } from "@/lib/ep-state-paths";

export async function POST(request: Request) {
  const origin = request.headers.get("origin");
  if (origin && origin !== new URL(request.url).origin) return NextResponse.json({ error: "禁止跨站修改" }, { status: 403 });
  try {
    const body = await request.text();
    if (body.length > 24000) return NextResponse.json({ error: "修改内容过长" }, { status: 413 });
    JSON.parse(body);
    const script = epStatePath("runtime/host-adapter/manual_preference_correction.py");
    if (!await access(EP_RUNTIME_PYTHON).then(() => true).catch(() => false) || !await access(script).then(() => true).catch(() => false)) return NextResponse.json({ code: "runtime_unavailable", error: "Correction runtime is unavailable for this installation." }, { status: 503 });
    const result = await new Promise<{code: number | null; value: any}>((resolve, reject) => {
      const child = spawn(EP_RUNTIME_PYTHON, [script, epStatePath("guidance-v1/guidance-v1.json")], { timeout: 8000, env: { ...process.env, EVOLVING_PROFILE_STATE_ROOT: epStatePath() } });
      let output = "";
      child.stdout.on("data", (chunk) => { output += chunk; });
      child.on("error", reject);
      child.on("close", (code) => { try { resolve({ code, value: JSON.parse(output) }); } catch { reject(new Error("修正服务没有返回有效回执")); } });
      child.stdin.end(body);
    });
    return NextResponse.json(result.value, { status: result.code === 0 ? 200 : result.value.error === "revision_conflict" ? 409 : 400 });
  } catch {
    return NextResponse.json({ error: "修正服务失败，请保留内容并重试" }, { status: 500 });
  }
}
