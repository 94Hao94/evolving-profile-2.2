import { NextResponse } from "next/server";
import { assertRecoveryOrigin, validateRecoveryInput, runMemoryRecoveryCLI, launchRecoveryWorker, MemoryRecoveryError } from "@/lib/memory-recovery-server";

export const runtime = "nodejs";
const headers = { "Cache-Control": "no-store" };
function failure(error: unknown) {
  return NextResponse.json({ error_code: error instanceof MemoryRecoveryError ? error.code : "recovery_request_failed" }, { status: error instanceof MemoryRecoveryError ? error.status : 500, headers });
}

export async function GET(request: Request) {
  try {
    const url = new URL(request.url);
    const input = validateRecoveryInput({ action: "status", bank_id: url.searchParams.get("bank_id"), ...(url.searchParams.get("job_id") ? { job_id: url.searchParams.get("job_id") } : {}) });
    return NextResponse.json(await runMemoryRecoveryCLI(input), { headers });
  } catch (error) { return failure(error); }
}

export async function POST(request: Request) {
  try {
    assertRecoveryOrigin(request);
    if (!(request.headers.get("content-type") ?? "").includes("application/json")) throw new MemoryRecoveryError("recovery_json_required", 415);
    let body: unknown;
    try { body = await request.json(); } catch { throw new MemoryRecoveryError("invalid_recovery_json", 400); }
    const input = validateRecoveryInput(body);
    const response = await runMemoryRecoveryCLI(input);
    if (["start", "resume"].includes(input.action) && response.job) await launchRecoveryWorker(response.job);
    return NextResponse.json(response, { headers });
  } catch (error) { return failure(error); }
}
