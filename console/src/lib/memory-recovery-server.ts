import { spawn } from "node:child_process";
import { access } from "node:fs/promises";
import { EP_STATE_ROOT } from "@/lib/ep-state-paths";
import path from "node:path";

type RecoveryAction = "preview" | "start" | "status" | "resume" | "cancel";
export type RecoveryInput = {
  action: RecoveryAction; bank_id: string; mode?: "pending" | "all" | "date_range";
  dimensions?: string[]; session_ids?: string[]; from?: string | null; to?: string | null;
  plan_id?: string; job_id?: string; idempotency_key?: string;
};
type CLIOptions = { pythonPath?: string; scriptPath?: string; stateRoot?: string; timeoutMs?: number };
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const SAFE_ID = /^[A-Za-z0-9_-]{1,160}$/;
const ACTIONS = new Set(["preview", "start", "status", "resume", "cancel"]);
const FIELDS = new Set(["action", "bank_id", "mode", "dimensions", "session_ids", "from", "to", "plan_id", "job_id", "idempotency_key"]);

export class MemoryRecoveryError extends Error {
  constructor(public code: string, public status = 502) { super(code); }
}

export function validateRecoveryInput(value: unknown): RecoveryInput {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new MemoryRecoveryError("invalid_recovery_request", 400);
  const r = value as Record<string, unknown>;
  if (Object.keys(r).some(k => !FIELDS.has(k)) || !ACTIONS.has(String(r.action))) throw new MemoryRecoveryError("invalid_recovery_action", 400);
  if (typeof r.bank_id !== "string" || !r.bank_id.trim() || r.bank_id.length > 160 || /[\/\\\x00-\x1f]/.test(r.bank_id) || [".", ".."].includes(r.bank_id)) throw new MemoryRecoveryError("invalid_bank_id", 400);
  if (r.mode != null && !["pending", "all", "date_range"].includes(String(r.mode))) throw new MemoryRecoveryError("invalid_recovery_range", 400);
  if (r.dimensions != null && (!Array.isArray(r.dimensions) || r.dimensions.length > 64 || r.dimensions.some(x => typeof x !== "string" || !SAFE_ID.test(x)))) throw new MemoryRecoveryError("invalid_recovery_dimensions", 400);
  if (r.session_ids != null && (!Array.isArray(r.session_ids) || r.session_ids.length > 10000 || r.session_ids.some(x => typeof x !== "string" || !UUID.test(x)))) throw new MemoryRecoveryError("invalid_recovery_sessions", 400);
  for (const k of ["from", "to"] as const) if (r[k] != null && (typeof r[k] !== "string" || r[k].length > 40 || !/^\d{4}-\d{2}-\d{2}(T.*)?$/.test(r[k]) || !Number.isFinite(Date.parse(r[k])))) throw new MemoryRecoveryError("invalid_recovery_date", 400);
  if (r.from && r.to && Date.parse(String(r.from)) > Date.parse(String(r.to))) throw new MemoryRecoveryError("invalid_recovery_range", 400);
  if (r.action === "start" && (typeof r.plan_id !== "string" || !SAFE_ID.test(r.plan_id) || typeof r.idempotency_key !== "string" || !UUID.test(r.idempotency_key))) throw new MemoryRecoveryError("invalid_recovery_start", 400);
  if (["resume", "cancel"].includes(String(r.action)) && (typeof r.job_id !== "string" || !UUID.test(r.job_id))) throw new MemoryRecoveryError("invalid_recovery_job", 400);
  if (r.job_id != null && (typeof r.job_id !== "string" || !UUID.test(r.job_id))) throw new MemoryRecoveryError("invalid_recovery_job", 400);
  return r as RecoveryInput;
}

export function assertRecoveryOrigin(request: Request) {
  const origin = request.headers.get("origin");
  if (!origin) return;
  const internal = new URL(request.url);
  const host = request.headers.get("host");
  let expected = internal.origin;
  if (host) {
    try {
      const authority = new URL(`${internal.protocol}//${host}`);
      if (authority.username || authority.password || authority.pathname !== "/" || authority.search || authority.hash || authority.host !== host.toLowerCase()) throw new Error("invalid_host");
      expected = authority.origin;
    } catch { throw new MemoryRecoveryError("recovery_origin_rejected", 403); }
  }
  if (origin !== expected) throw new MemoryRecoveryError("recovery_origin_rejected", 403);
}

function safeResponse(value: any): any {
  if (Array.isArray(value)) return value.map(safeResponse);
  if (value && typeof value === "object") return Object.fromEntries(Object.entries(value)
    .filter(([k]) => !["api_key", "apikey", "authorization", "password", "secret", "intents", "raw_sources", "raw_text", "raw_content", "source_text", "config", "provider_config"].includes(k.toLowerCase()))
    .map(([k, v]) => [k, safeResponse(v)]));
  if (typeof value === "string") return value.replace(/apikey_[A-Za-z0-9_]+|sk-[A-Za-z0-9_-]{14,}/g, "[credential omitted]");
  return value;
}

async function runtimePaths(options: CLIOptions = {}) {
  const stateRoot = options.stateRoot ?? EP_STATE_ROOT;
  let scriptPath = options.scriptPath ?? process.env.EVOLVING_PROFILE_RECOVERY_SCRIPT ?? path.join(stateRoot, "runtime/host-adapter/memory_recovery.py");
  if (!options.scriptPath && !process.env.EVOLVING_PROFILE_RECOVERY_SCRIPT) {
    try { await access(scriptPath); } catch { scriptPath = path.resolve(process.cwd(), "../host-adapter/memory_recovery.py"); }
  }
  let pythonPath = options.pythonPath ?? process.env.EVOLVING_PROFILE_PYTHON ?? path.join(stateRoot, "runtime/python-3.11/bin/python");
  if (!options.pythonPath && !process.env.EVOLVING_PROFILE_PYTHON) {
    try { await access(pythonPath); } catch { pythonPath = "python3"; }
  }
  return { stateRoot, scriptPath, pythonPath };
}

export async function runMemoryRecoveryCLI(input: RecoveryInput, options: CLIOptions = {}): Promise<Record<string, any>> {
  const value = validateRecoveryInput(input);
  const paths = await runtimePaths(options);
  return await new Promise((resolve, reject) => {
    const child = spawn(paths.pythonPath, [paths.scriptPath, "--state-root", paths.stateRoot], { shell: false, stdio: ["pipe", "pipe", "pipe"] });
    let output = ""; let done = false;
    const finish = (error?: Error, result?: Record<string, any>) => {
      if (done) return; done = true; clearTimeout(timer);
      if (error) reject(error); else resolve(result!);
    };
    const timer = setTimeout(() => { child.kill("SIGTERM"); finish(new MemoryRecoveryError("recovery_backend_timeout", 504)); }, options.timeoutMs ?? 30000);
    child.on("error", () => finish(new MemoryRecoveryError("recovery_backend_unavailable")));
    child.stdout.on("data", data => { output += data.toString(); if (output.length > 1000000) { child.kill("SIGTERM"); finish(new MemoryRecoveryError("recovery_response_too_large")); } });
    child.stderr.on("data", () => { /* Never return raw process logs or credentials. */ });
    child.on("close", code => {
      try {
        const result = JSON.parse(output.trim());
        if (!result || typeof result !== "object" || Array.isArray(result)) throw new Error("protocol");
        const safe = safeResponse(result);
        const reportedCode = safe.error_code ?? safe.error?.code;
        if (code !== 0 || reportedCode) {
          const errorCode = typeof reportedCode === "string" && /^[a-z0-9_:-]{1,100}$/i.test(reportedCode) ? reportedCode : "recovery_backend_failed";
          const status = /plan_source_changed|conflict/.test(errorCode) ? 409 : /job_not_found|plan_not_found/.test(errorCode) ? 404 : /invalid|missing|scope|plan|bank/.test(errorCode) ? 400 : 502;
          finish(new MemoryRecoveryError(errorCode, status));
        } else finish(undefined, safe);
      } catch { finish(new MemoryRecoveryError("recovery_invalid_backend_response")); }
    });
    child.stdin.on("error", () => finish(new MemoryRecoveryError("recovery_backend_unavailable")));
    child.stdin.end(JSON.stringify(value));
  });
}

export async function launchRecoveryWorker(job: { job_id?: unknown; status?: unknown }, options: CLIOptions = {}) {
  if (typeof job.job_id !== "string" || !UUID.test(job.job_id) || !["queued", "running"].includes(String(job.status))) return;
  const paths = await runtimePaths(options);
  const child = spawn(paths.pythonPath, [paths.scriptPath, "--state-root", paths.stateRoot, "--run-job", job.job_id], { detached: true, stdio: "ignore", shell: false });
  await new Promise<void>((resolve, reject) => { child.once("spawn", resolve); child.once("error", () => reject(new MemoryRecoveryError("recovery_worker_start_failed"))); });
  child.unref();
}
