import { epStatePath, EP_STATE_ROOT, EP_API_ENV, EP_HOST_SESSIONS } from "@/lib/ep-state-paths";
import { NextResponse } from "next/server";
import { mkdir, readFile, writeFile, rename, unlink } from "node:fs/promises";
import { randomUUID } from "node:crypto";
import path from "node:path";
import { homedir } from "node:os";

const stateRoot = EP_STATE_ROOT;
const settingsPath = path.join(stateRoot, "config/guidance-settings.json");
const defaults = { schema: "evolving-profile.guidance-settings.v1", max_candidates: 6, adaptive_budget: true, auto_probe: true, probe_max_tokens: 500 };

async function readSettings() {
  try {
    const value = JSON.parse(await readFile(settingsPath, "utf8"));
    return { ...defaults, ...value };
  } catch { return defaults; }
}

function validate(value: any) {
  if (!Number.isInteger(value.max_candidates) || value.max_candidates < 1 || value.max_candidates > 20) throw new Error("invalid_max_candidates");
  if (typeof value.adaptive_budget !== "boolean") throw new Error("invalid_adaptive_budget");
  if (typeof value.auto_probe !== "boolean") throw new Error("invalid_auto_probe");
  if (!Number.isInteger(value.probe_max_tokens) || value.probe_max_tokens < 300 || value.probe_max_tokens > 1200) throw new Error("invalid_probe_max_tokens");
}

export async function GET() { return NextResponse.json(await readSettings()); }

export async function POST(request: Request) {
  try {
    const value = { ...defaults, ...(await request.json()), updated_at: new Date().toISOString() };
    validate(value);
    await mkdir(path.dirname(settingsPath), { recursive: true });
    const temporary = settingsPath + "." + randomUUID() + ".tmp";
    try {
      await writeFile(temporary, JSON.stringify(value, null, 2) + "\n", { mode: 0o600, flag: "wx" });
      await rename(temporary, settingsPath);
    } finally {
      await unlink(temporary).catch(() => undefined);
    }
    return NextResponse.json({ ...value, applied: true });
  } catch (error) {
    return NextResponse.json({ error: error instanceof Error ? error.message : "guidance_settings_failed" }, { status: 400 });
  }
}
