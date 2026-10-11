import { epStatePath, EP_STATE_ROOT, EP_API_ENV, EP_HOST_SESSIONS } from "@/lib/ep-state-paths";
import { NextResponse } from "next/server";
import { readFile } from "node:fs/promises";

const ENV_PATH = EP_API_ENV;

function friendlyError(error: unknown) {
  const message = error instanceof Error ? error.message : String(error ?? "");
  if (/ByteString|greater than 255|character at index/i.test(message)) {
    return "连接测试失败：页面中的掩码密钥不能直接发送，请重新输入真实 API Key。";
  }
  if (/ECONNREFUSED|ENOTFOUND|EAI_AGAIN|fetch failed|network|timeout|timed out|aborted/i.test(message)) {
    return "连接测试失败：无法连接到该 Provider，请检查 Base URL、代理服务和端口。";
  }
  if (/401|403|unauthorized|forbidden|invalid.*(key|token)|authentication/i.test(message)) {
    return "连接测试失败：API Key 无效或没有访问权限，请检查密钥和模型配置。";
  }
  return "连接测试失败：Provider 返回了无法识别的错误，请检查配置后重试。";
}

function statusError(status: number) {
  if (status === 401 || status === 403) return "连接测试失败：API Key 无效或没有访问权限，请检查密钥和模型配置。";
  if (status === 404) return "连接测试失败：Provider 未找到模型接口，请检查 Base URL 是否包含正确的 /v1 路径。";
  if (status >= 500) return "连接测试失败：Provider 服务暂时不可用，请稍后重试。";
  return "连接测试失败：Provider 拒绝了连接请求，请检查配置。";
}

async function effectiveApiKey() {
  try {
    const text = await readFile(ENV_PATH, "utf8");
    const row = text.split(/\r?\n/).find((line) => line.startsWith("EVOLVING_PROFILE_API_LLM_API_KEY="));
    return row?.slice(row.indexOf("=") + 1) || "";
  } catch { return ""; }
}

export async function POST(request: Request) {
  try {
    const value = await request.json();
    const baseUrl = String(value.base_url || "").replace(/\/$/, "");
    const model = String(value.model || "");
    if (!baseUrl || !model) return NextResponse.json({ ok: false, error: "需要填写 Base URL 和模型名称" }, { status: 400 });
    const headers: Record<string, string> = { "content-type": "application/json" };
    const suppliedKey = String(value.api_key || "");
    const apiKey = suppliedKey.startsWith("••••") ? await effectiveApiKey() : suppliedKey;
    if (apiKey && [...apiKey].some((character) => character.charCodeAt(0) > 255)) {
      return NextResponse.json({ ok: false, error: "API Key 含有非 ASCII 字符，请填写真实 API Key；页面中的掩码不能直接发送。" }, { status: 400 });
    }
    if (apiKey) headers.authorization = `Bearer ${apiKey}`;
    const response = await fetch(`${baseUrl}/models`, { headers, cache: "no-store", signal: AbortSignal.timeout(8000) });
    if (!response.ok) {
      return NextResponse.json({ ok: false, status: response.status, model, error: statusError(response.status) }, { status: 502 });
    }
    return NextResponse.json({ ok: true, status: response.status, model, detail: "Provider 连接正常" }, { status: 200 });
  } catch (error) {
    return NextResponse.json({ ok: false, error: friendlyError(error) }, { status: 502 });
  }
}
