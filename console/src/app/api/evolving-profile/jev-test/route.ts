import { epStatePath, EP_STATE_ROOT, EP_API_ENV, EP_HOST_SESSIONS } from "@/lib/ep-state-paths";
import { NextResponse } from "next/server";
import { readFile } from "node:fs/promises";
import { JEV_BASE_URL, JEV_MODEL } from "@/lib/jev-provider";

const SETTINGS = epStatePath("config/runtime-settings.json");

export async function POST(request: Request) {
  const started = Date.now();
  const failure = (code: string, status: number | string, error: string, httpStatus = 502) => NextResponse.json({ ok: false, code, status, error, latency_ms: Date.now() - started, checked_at: new Date().toISOString() }, { status: httpStatus });
  try {
    const incoming = await request.json();
    let saved: any = {};
    try { saved = JSON.parse(await readFile(SETTINGS, "utf8"))?.retrieval_models?.judge ?? {}; } catch { /* unsaved test uses the request body */ }
    const suppliedKey = String(incoming.api_key || "");
    const apiKey = suppliedKey.startsWith("••••") ? String(saved.api_key || "") : suppliedKey || String(saved.api_key || "");
    if (!apiKey.trim()) return failure("not_configured", "not_configured", "请填写 JEV API Key 后再测试。", 400);
    if (!/^[\x21-\x7e]+$/.test(apiKey)) return failure("invalid_key", "invalid_key", "API Key 格式无效，请检查是否包含空格或非英文字符。", 400);
    const response = await fetch(JEV_BASE_URL, {
      method: "POST",
      headers: { "content-type": "application/json", authorization: `Bearer ${apiKey}` },
      body: JSON.stringify({ model: JEV_MODEL, state: "EP connection test", questions: { ok: { type: "noul", instructions: "Is this a connectivity test?", criteria: { "true": "yes", "false": "no" } } } }),
      cache: "no-store",
      redirect: "error",
      signal: AbortSignal.timeout(8000),
    });
    if (!response.ok) return failure(response.status === 401 || response.status === 403 ? "authentication_failed" : response.status === 402 ? "insufficient_credits" : response.status === 429 ? "rate_limited" : "provider_error", response.status, response.status === 401 || response.status === 403 ? "JEV API Key 无效或无权限。" : `JEV 服务返回 HTTP ${response.status}。`);
    const result = await response.json().catch(() => null);
    const probability = result?.answers?.ok?.noul;
    if (typeof probability !== "number" || !Number.isFinite(probability) || probability < 0 || probability > 1) return failure("invalid_response", response.status, "服务已响应，但没有返回有效的 JEV 判断结果；连接尚未核验。" );
    const usage = result?.usage;
    return NextResponse.json({ ok: true, code: "connected", response_verified: true, status: response.status, model: typeof result.model === "string" ? result.model : JEV_MODEL, detail: "JEV 连接正常，已收到有效判断响应。", latency_ms: Date.now() - started, checked_at: new Date().toISOString(), usage: { input_tokens: Number.isFinite(usage?.input_tokens) ? usage.input_tokens : null, output_tokens: Number.isFinite(usage?.output_tokens) ? usage.output_tokens : null } });
  } catch (error) {
    const timeout = error instanceof Error && (error.name === "TimeoutError" || error.name === "AbortError");
    return failure(timeout ? "timeout" : "network_error", "unavailable", timeout ? "JEV 测试超时，请稍后重试。" : "JEV 连接失败，请检查网络、代理和服务状态。" );
  }
}
