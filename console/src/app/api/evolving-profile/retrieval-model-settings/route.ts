import { NextResponse } from "next/server";
import { access, mkdir, readFile, readdir, rename, stat, unlink, writeFile } from "node:fs/promises";
import { randomUUID } from "node:crypto";
import path from "node:path";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { JEV_BASE_URL, JEV_MODEL, normalizeJevJudge } from "@/lib/jev-provider";
import { discoverModelProposal, modelIdentityMatches, parseModelEnvironment, resolveEffectiveEmbedding, maskRetrievalModels, restoreMaskedRetrievalKeys } from "@/lib/retrieval-model-identity";
import { EP_STATE_ROOT, EP_API_ENV, EP_MANAGED_MAC_HOST } from "@/lib/ep-state-paths";
import { homedir } from "node:os";

const ROOT = EP_STATE_ROOT;
const SETTINGS = path.join(ROOT, "config/runtime-settings.json");
const OVERLAY = path.join(ROOT, "config/retrieval-models.env");
const ENV = EP_API_ENV;
const execFileAsync = promisify(execFile);

function merge(base: any, value: any): any {
  if (!value || typeof value !== "object" || Array.isArray(value)) return base;
  const result = { ...base };
  for (const [key, item] of Object.entries(value)) result[key] = item && typeof item === "object" && !Array.isArray(item) && base[key] && typeof base[key] === "object" ? merge(base[key], item) : item;
  return result;
}

function mask(value: any) {
  return maskRetrievalModels(value);
}

function envValue(value: unknown) {
  const text = String(value ?? "");
  if (/[\r\n\0]/.test(text)) throw new Error("模型配置包含不允许的换行或空字符");
  return `"${text.replaceAll("\\", "\\\\").replaceAll('"', '\\"')}"`;
}

async function modelFiles(root: string, depth = 0): Promise<string[]> {
  if (depth > 3) return [];
  try {
    const entries = await readdir(root, { withFileTypes: true });
    const files: string[] = [];
    for (const entry of entries) {
      const full = path.join(root, entry.name);
      if (entry.isFile()) files.push(entry.name.toLowerCase());
      else if (entry.isDirectory() && !entry.name.startsWith(".")) files.push(...(await modelFiles(full, depth + 1)).map((item) => `${entry.name}/${item}`));
    }
    return files;
  } catch { return []; }
}

async function discoverLocalModelCandidates() {
  const roots = [path.join(ROOT,"models"), path.join(ROOT,"eval"), ...(EP_MANAGED_MAC_HOST ? [path.join(homedir(),".cache/chroma/onnx_models")] : [])];
  const candidates: any[] = [];
  async function walk(root: string, depth = 0): Promise<void> {
    if (depth > 4) return;
    let entries: any[] = [];
    try { entries = await readdir(root, { withFileTypes: true }); } catch { return; }
    const directFiles = entries.filter((entry) => entry.isFile()).map((entry) => entry.name.toLowerCase());
    const hasConfig = directFiles.includes("config.json") || await access(path.join(root, "encoder/config.json")).then(() => true).catch(() => false);
    const hasTokenizer = directFiles.some((file) => ["tokenizer.json", "tokenizer.model", "vocab.txt", "spiece.model"].includes(file)) || await access(path.join(root, "tokenizer/tokenizer.json")).then(() => true).catch(() => false);
    const hasWeights = directFiles.some((file) => /^(model\.onnx|pytorch_model.*\.(bin|safetensors)|model.*\.(bin|safetensors|gguf))$/.test(file)) || await access(path.join(root, "onnx/model.onnx")).then(() => true).catch(() => false);
    if (hasConfig && hasTokenizer && hasWeights) {
      let config: any = {};
      for (const name of ["config.json", "encoder/config.json"]) { try { config = JSON.parse(await readFile(path.join(root, name), "utf8")); break; } catch { /* inspect next known location */ } }
      const label = String(config._name_or_path || path.basename(root));
      const haystack = `${root} ${label}`.toLowerCase();
      const kind = /rerank|cross.encoder|bge-reranker/.test(haystack) ? "reranker" : "embedding";
      const modelRoot = path.basename(root).toLowerCase() === "onnx" ? path.dirname(root) : root;
      candidates.push({ kind, path: modelRoot, model: label, dimensions: Number(config.hidden_size || config.embedding_size || config.projection_dim || 0) || null, status: "ready" });
      return;
    }
    for (const entry of entries) if (entry.isDirectory() && !entry.name.startsWith(".")) await walk(path.join(root, entry.name), depth + 1);
  }
  for (const root of roots) await walk(root);
  return candidates.filter((item, index, all) => all.findIndex((candidate) => candidate.path === item.path) === index);
}

async function validateLocalModel(model: any, kind: "embedding" | "reranker") {
  const localPath = String(model?.local_path ?? "").trim();
  if (!localPath) return;
  const files = await modelFiles(localPath);
  const hasConfig = files.some((file) => file === "config.json" || file.endsWith("/config.json"));
  const hasTokenizer = files.some((file) => ["tokenizer.json", "tokenizer.model", "vocab.txt", "spiece.model"].some((name) => file === name || file.endsWith(`/${name}`)));
  const hasWeights = files.some((file) => /(^|\/)(model\.onnx|onnx\/model\.onnx|pytorch_model.*\.(bin|safetensors)|model.*\.(bin|safetensors|gguf))$/.test(file));
  if (!hasConfig) throw new Error(`${kind === "embedding" ? "向量" : "重排"}模型目录缺少 config.json`);
  if (!hasTokenizer) throw new Error(`${kind === "embedding" ? "向量" : "重排"}模型目录缺少 tokenizer 文件`);
  if (!hasWeights) throw new Error(`${kind === "embedding" ? "向量" : "重排"}模型目录缺少可加载权重文件`);
  if (kind === "embedding" && Number.isInteger(model.dimensions)) {
    const configFile = files.find((file) => file === "config.json" || file.endsWith("/config.json"));
    if (configFile === "config.json") {
      const config = JSON.parse(await readFile(path.join(localPath, configFile), "utf8"));
      const detected = Number(config.hidden_size ?? config.embedding_size ?? config.projection_dim ?? 0);
      if (detected > 0 && Number(model.dimensions) !== detected) throw new Error(`向量维度不匹配：目录检测为 ${detected}，当前填写为 ${model.dimensions}`);
    }
  }
}

async function current() {
  const optionalEnv = async (filename: string) => readFile(filename, "utf8").catch((error) => {
    if (error.code === "ENOENT") return "";
    throw error;
  });
  const envText = (await optionalEnv(ENV)) + "\n" + await optionalEnv(OVERLAY);
  const env = parseModelEnvironment(envText);
  const base = {
    embedding: resolveEffectiveEmbedding({ enabled: true, mode: "local", provider: "onnx", model: "intfloat/multilingual-e5-small", local_path: "", dimensions: 384, max_tokens: 512, device: "cpu", profile_id: "embedding-default", status: "configured" },env),
    reranker: { enabled: env.EVOLVING_PROFILE_API_RERANKER_PROVIDER !== "rrf", mode: "local", provider: env.EVOLVING_PROFILE_API_RERANKER_PROVIDER || "rrf", model: env.EVOLVING_PROFILE_API_RERANKER_LOCAL_MODEL || "BAAI/bge-reranker-base", local_path: "", device: "cpu", profile_id: "reranker-default", status: env.EVOLVING_PROFILE_API_RERANKER_PROVIDER === "rrf" ? "configured_but_inactive" : "configured" },
    embedding_profiles: [], reranker_profiles: [],
    fusion: { enabled: true, algorithm: "rrf", profile_id: "fusion-rrf" },
    judge: { enabled: false, provider: "jev", mode: "systemone", base_url: JEV_BASE_URL, model: JEV_MODEL, api_key: "", timeout_ms: 5000, max_tokens: 600, mode_policy: "off", fallback: "rules", send_scope: "metadata_summary", risk_gate_enabled: false, status: "disabled" },
  };
  let settings: any = {};
  try { settings = JSON.parse(await readFile(SETTINGS, "utf8")).retrieval_models ?? {}; } catch { /* first-run uses environment defaults */ }
  const result = merge(base, settings);
  result.configuration_status = envText.trim() || Object.keys(settings).length ? "configured" : "unconfigured";
  if (result.configuration_status === "unconfigured") {
    result.embedding.status = "unconfigured";
    result.reranker.status = "unconfigured";
  }
  result.judge = normalizeJevJudge(result.judge);
  if (!Array.isArray(result.embedding_profiles) || result.embedding_profiles.length === 0) result.embedding_profiles = [{ ...result.embedding }];
  if (!Array.isArray(result.reranker_profiles) || result.reranker_profiles.length === 0) result.reranker_profiles = [{ ...result.reranker }];
  result.embedding.api_key = env.EVOLVING_PROFILE_API_EMBEDDINGS_OPENAI_API_KEY || "";
  result.embedding.base_url = env.EVOLVING_PROFILE_API_EMBEDDINGS_OPENAI_BASE_URL || "";
  result.reranker.api_key = env.EVOLVING_PROFILE_API_RERANKER_TEI_API_KEY || "";
  result.reranker.base_url = env.EVOLVING_PROFILE_API_RERANKER_TEI_URL || "";
  const local_inventory = await discoverLocalModelCandidates();
  result.local_inventory = local_inventory;
  for (const kind of ["embedding", "reranker"] as const) {
    const configured=result[kind];
    const identityKeys=["model","provider","mode","local_path","dimensions"];
    const savedIdentity=Object.fromEntries(identityKeys.map(key=>[key,configured[key]]));
    if(kind==="embedding") result[kind]=resolveEffectiveEmbedding(configured,env);
    else if(env.EVOLVING_PROFILE_API_RERANKER_PROVIDER==="rrf") result[kind]={...configured,enabled:false,provider:"rrf"};
    result[kind] = discoverModelProposal(result[kind],local_inventory,kind);
    const profile=result[`${kind}_profiles`].find((item:any)=>item.profile_id===result[kind].profile_id);
    result[kind].profile_identity=!result[kind].enabled ? "inactive" : profile && modelIdentityMatches(profile,result[kind]) ? "matches" : "drifted";
    result[kind].effective_configuration=Object.fromEntries(["model","provider","mode","local_path","dimensions","base_url","enabled"].map(key=>[key,result[kind][key]]));
    result[kind].configured_identity=savedIdentity;
  }
  return result;
}

async function validate(models: any) {
  for (const name of ["embedding", "reranker"]) {
    const model = models?.[name];
    if (!model || !["local", "api"].includes(model.mode) || typeof model.enabled !== "boolean" || !String(model.model ?? "").trim()) throw new Error(`检索模型配置无效：${name}`);
    if (model.mode === "api" && !String(model.base_url ?? "").trim()) throw new Error(`${name === "embedding" ? "向量模型" : "重排模型"}使用线上 API 时必须填写 API 地址`);
    if (model.local_path) {
      const path = String(model.local_path);
      if (!path.startsWith("/") || path.includes("..")) throw new Error("本地模型目录必须是有效的绝对路径");
      const info = await stat(path).catch(() => null);
      if (!info?.isDirectory()) throw new Error("本地模型目录不存在或不是目录");
      await access(path).catch(() => { throw new Error("本地模型目录不可读"); });
    }
    await validateLocalModel(model, name as "embedding" | "reranker");
  }
  for (const [kind, profiles] of [["embedding", models.embedding_profiles], ["reranker", models.reranker_profiles]] as const) {
    if (!Array.isArray(profiles)) throw new Error(`${kind} 配置档案必须是数组`);
    const ids = new Set<string>();
    for (const profile of profiles) {
      if (!profile?.profile_id || ids.has(String(profile.profile_id))) throw new Error(`${kind} 配置档案 ID 重复或为空`);
      ids.add(String(profile.profile_id));
      if (!String(profile.model ?? "").trim()) throw new Error(`${kind} 配置档案缺少模型名称`);
      await validateLocalModel(profile, kind);
    }
  }
  if (!Number.isInteger(models.embedding.dimensions) || models.embedding.dimensions < 1 || models.embedding.dimensions > 65536) throw new Error("向量维度无效");
  if (!["off", "shadow", "assist", "enforce"].includes(models.judge.mode_policy)) throw new Error("JEV 运行模式无效");
  if (models.judge.base_url !== JEV_BASE_URL || models.judge.model !== JEV_MODEL) throw new Error("JEV 地址和模型由系统固定管理");
  if (!models.judge.enabled && models.judge.risk_gate_enabled) throw new Error("须先启用 JEV，才能启用风险确认门");
}

function modelEnv(models: any) {
  const embedding = models.embedding;
  const reranker = models.reranker;
  const values: Record<string, string> = {};
  if (embedding.mode === "local") {
    values.EVOLVING_PROFILE_API_EMBEDDINGS_PROVIDER = embedding.provider === "local" ? "local" : "onnx";
    if (embedding.provider === "local") values.EVOLVING_PROFILE_API_EMBEDDINGS_LOCAL_MODEL = embedding.model;
    else {
      values.EVOLVING_PROFILE_API_EMBEDDINGS_ONNX_MODEL_ID = embedding.model;
      values.EVOLVING_PROFILE_API_EMBEDDINGS_ONNX_DIMENSIONS = String(embedding.dimensions);
      values.EVOLVING_PROFILE_API_EMBEDDINGS_ONNX_MAX_TOKENS = String(embedding.max_tokens || 512);
      if (embedding.local_path) {
        values.EVOLVING_PROFILE_API_EMBEDDINGS_ONNX_MODEL_PATH = path.join(embedding.local_path, "onnx/model.onnx");
        values.EVOLVING_PROFILE_API_EMBEDDINGS_ONNX_TOKENIZER_NAME_OR_PATH = path.join(embedding.local_path, "onnx");
      }
    }
  } else {
    values.EVOLVING_PROFILE_API_EMBEDDINGS_PROVIDER = embedding.provider || "openai";
    values.EVOLVING_PROFILE_API_EMBEDDINGS_OPENAI_MODEL = embedding.model;
    values.EVOLVING_PROFILE_API_EMBEDDINGS_OPENAI_BASE_URL = embedding.base_url;
    values.EVOLVING_PROFILE_API_EMBEDDINGS_OPENAI_API_KEY = embedding.api_key || "";
    if (embedding.dimensions) values.EVOLVING_PROFILE_API_EMBEDDINGS_OPENAI_DIMENSIONS = String(embedding.dimensions);
  }
  if (!reranker.enabled) values.EVOLVING_PROFILE_API_RERANKER_PROVIDER = "rrf";
  else if (reranker.mode === "local") {
    values.EVOLVING_PROFILE_API_RERANKER_PROVIDER = "local";
    values.EVOLVING_PROFILE_API_RERANKER_LOCAL_MODEL = reranker.local_path || reranker.model;
  } else {
    values.EVOLVING_PROFILE_API_RERANKER_PROVIDER = "tei";
    values.EVOLVING_PROFILE_API_RERANKER_TEI_URL = reranker.base_url;
    values.EVOLVING_PROFILE_API_RERANKER_TEI_API_KEY = reranker.api_key || "";
  }
  return Object.entries(values).map(([key, value]) => `${key}=${envValue(value)}`).join("\n") + "\n";
}

export async function GET() {
  try { return NextResponse.json(mask(await current())); }
  catch (error) { return NextResponse.json({ error: error instanceof Error ? error.message : "模型配置读取失败" }, { status: 500 }); }
}

export async function POST(request: Request) {
  let settingsCurrent: any = {};
  try { settingsCurrent = JSON.parse(await readFile(SETTINGS, "utf8")); } catch { /* first-run */ }
  const incoming = await request.json();
  const oldModels = await current();
  const models = merge(oldModels, incoming);
  models.judge = normalizeJevJudge(models.judge);
  restoreMaskedRetrievalKeys(incoming, oldModels, models);
  try {
    await validate(models);
    const retrievalChanged = modelEnv(models) !== modelEnv(oldModels);
    const nextSettings = { ...settingsCurrent, retrieval_models: models, updated_at: new Date().toISOString() };
    await mkdir(path.dirname(SETTINGS), { recursive: true });
    const settingsTemp = `${SETTINGS}.${randomUUID()}.tmp`;
    const envTemp = `${OVERLAY}.${randomUUID()}.tmp`;
    try {
      await writeFile(settingsTemp, JSON.stringify(nextSettings, null, 2) + "\n", { mode: 0o600, flag: "wx" });
      await writeFile(envTemp, modelEnv(models), { mode: 0o600, flag: "wx" });
      await rename(envTemp, OVERLAY);
      await rename(settingsTemp, SETTINGS);
    } finally {
      await unlink(settingsTemp).catch(() => undefined);
      await unlink(envTemp).catch(() => undefined);
    }
    if (!EP_MANAGED_MAC_HOST) return NextResponse.json({ ...mask(models), saved: true, applied: false, apply_status: "unsupported", message: "Configuration saved. Automatic service restart is unavailable for this installation; apply it using this installation's service manager." });
    if (!retrievalChanged) return NextResponse.json({ ...mask(models), saved: true, applied: true, message: "JEV 设置已保存，新调用生效；检索模型未变化，无需重启 API。" });
    let applied = false;
    let applyMessage = "模型设置已保存；API 服务重启后应用。";
    try {
      await execFileAsync("launchctl", ["kickstart", "-k", `gui/${process.getuid?.() ?? 501}/com.evolving-profile.api-shadow`], { timeout: 15000 });
      const deadline = Date.now() + 30000;
      let health: Response | null = null;
      while (Date.now() < deadline) {
        try {
          health = await fetch("http://127.0.0.1:12088/health", { cache: "no-store", signal: AbortSignal.timeout(2500) });
          if (health.ok) break;
        } catch { /* API is still loading the selected local model. */ }
        await new Promise((resolve) => setTimeout(resolve, 750));
      }
      if (!health) throw new Error("api_health_timeout");
      applied = health.ok;
      applyMessage = applied ? "模型设置已应用，EP API 已恢复健康。" : "模型设置已保存，但 API 健康检查未通过；请查看服务状态。";
    } catch {
      applyMessage = "模型设置已保存，但自动重启或健康检查未确认；请查看服务状态。";
    }
    return NextResponse.json({ ...mask(models), saved: true, applied, message: applyMessage });
  } catch (error) {
    return NextResponse.json({ error: error instanceof Error ? error.message : "检索模型配置失败" }, { status: 400 });
  }
}
