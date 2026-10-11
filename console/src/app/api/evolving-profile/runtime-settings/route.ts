import { epStatePath, EP_STATE_ROOT, EP_API_ENV, EP_HOST_SESSIONS } from "@/lib/ep-state-paths";
import { NextResponse } from "next/server";
import { mkdir, readFile, readdir, rename, unlink, writeFile } from "node:fs/promises";
import { randomUUID } from "node:crypto";
import path from "node:path";
import { homedir } from "node:os";
import { access, stat } from "node:fs/promises";
import { JEV_BASE_URL, JEV_MODEL, normalizeJevJudge } from "@/lib/jev-provider";
import { effectiveRecallPolicy, normalizeRecallSettings, RECALL_POLICY_DEFAULTS, RAG_MINIMUM_RELEVANCE_DEFAULT, validateRecallSettingsInput } from "@/lib/recall-policy";
import { maskRetrievalModels, restoreMaskedRetrievalKeys } from "@/lib/retrieval-model-identity";

const stateRoot = EP_STATE_ROOT;
const settingsPath = path.join(stateRoot, "config/runtime-settings.json");
const envPath = process.env.EVOLVING_PROFILE_API_ENV ?? path.join(stateRoot, "profiles/evolving-profile-api.env");
const actions = { record: true, retrieve: true, inject: true };
const agentProcessModuleNames = ["agent_process_trajectory", "agent_process_observation", "agent_process_failure_episode", "agent_process_repair_pattern", "agent_process_capability", "agent_process_strategy", "agent_process_revalidation"] as const;
const moduleNames = ["facts", "experiences", "entities", "preferences", "scenario_summary", "mental_models", "source_readback", "background_reflection", "agent_process_memory", ...agentProcessModuleNames] as const;
const defaults = {
  schema: "evolving-profile.runtime-settings.v1",
  recall_policy: { ...RECALL_POLICY_DEFAULTS },
  modules: Object.fromEntries(moduleNames.map((name) => [name, { ...actions }])) as Record<string, typeof actions>,
  routing: { mode: "auto", ep_enabled: true, external_rag_enabled: false, allow_parallel: false, conflict_policy: "show_both" },
  budgets: { ep_total_tokens: 4000, rag_total_tokens: 4000, total_tokens: 6000, preference_tokens: 1200, scenario_tokens: 1200, source_tokens: 2400 },
  rag: { enabled: false, minimum_relevance: RAG_MINIMUM_RELEVANCE_DEFAULT, root_path: "", collection: "default", lexical_enabled: true, vector_enabled: true, fusion: "rrf", lexical_weight: 0.5, vector_weight: 0.5, rerank_enabled: true, rerank_provider: "local", rerank_model: "", top_k: 20, score_threshold: 0.35, max_chunks: 8, auto_index: false },
    retrieval_models: {
    embedding: { enabled: true, mode: "local", provider: "onnx", model: "intfloat/multilingual-e5-small", local_path: "", dimensions: 384, max_tokens: 512, device: "cpu", profile_id: "embedding-default", status: "configured" },
    reranker: { enabled: true, mode: "local", provider: "local", model: "BAAI/bge-reranker-base", local_path: "", device: "cpu", profile_id: "reranker-default", status: "configured" },
    embedding_profiles: [], reranker_profiles: [],
    fusion: { enabled: true, algorithm: "rrf", profile_id: "fusion-rrf" },
    judge: { enabled: false, provider: "jev", mode: "systemone", base_url: JEV_BASE_URL, model: JEV_MODEL, api_key: "", timeout_ms: 5000, max_tokens: 600, mode_policy: "off", fallback: "rules", send_scope: "metadata_summary", risk_gate_enabled: false, status: "disabled" },
  },
  providers: { primary: { name: "", base_url: "", model: "", api_key: "" }, fallbacks: [] as Array<Record<string, unknown>> },
};

async function effectiveDefaults() {
  try {
    const env = Object.fromEntries((await readFile(envPath, "utf8")).split("\n").filter((line) => line && !line.startsWith("#") && line.includes("=")).map((line) => { const index = line.indexOf("="); return [line.slice(0, index), line.slice(index + 1)]; }));
    return merge(defaults, { retrieval_models: { embedding: { provider: env.EVOLVING_PROFILE_API_EMBEDDINGS_PROVIDER || "onnx", model: env.EVOLVING_PROFILE_API_EMBEDDINGS_ONNX_MODEL_ID || "intfloat/multilingual-e5-small", dimensions: Number(env.EVOLVING_PROFILE_API_EMBEDDINGS_ONNX_DIMENSIONS || 384), max_tokens: Number(env.EVOLVING_PROFILE_API_EMBEDDINGS_ONNX_MAX_TOKENS || 512) }, reranker: { provider: env.EVOLVING_PROFILE_API_RERANKER_PROVIDER === "rrf" ? "local" : env.EVOLVING_PROFILE_API_RERANKER_PROVIDER || "local", model: env.EVOLVING_PROFILE_API_RERANKER_LOCAL_MODEL || "BAAI/bge-reranker-base" } }, providers: { primary: { name: env.EVOLVING_PROFILE_API_LLM_BASE_URL?.includes("127.0.0.1:3211") ? "Coding Plan" : env.EVOLVING_PROFILE_API_LLM_PROVIDER || "", base_url: env.EVOLVING_PROFILE_API_LLM_BASE_URL || "", model: env.EVOLVING_PROFILE_API_LLM_MODEL || "", api_key: env.EVOLVING_PROFILE_API_LLM_API_KEY || "" } } });
  } catch { return merge(defaults, {}); }
}

function merge(base: any, value: any): any {
  if (!value || typeof value !== "object" || Array.isArray(value)) return base;
  const result = { ...base };
  for (const [key, item] of Object.entries(value)) result[key] = item && typeof item === "object" && !Array.isArray(item) && base[key] && typeof base[key] === "object" ? merge(base[key], item) : item;
  return result;
}

async function readSettings() {
  const base = await effectiveDefaults();
  let stored;
  try {
    stored = JSON.parse(await readFile(settingsPath, "utf8"));
  } catch (error) {
    if (error instanceof Error && "code" in error && error.code === "ENOENT") return normalizeRecallSettings(base);
    throw error;
  }
  validateRecallSettingsInput(stored);
  const value = normalizeRecallSettings(merge(base, stored));
  value.retrieval_models.judge = normalizeJevJudge(value.retrieval_models.judge);
  return value;
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
  if (kind === "embedding" && model.dimensions != null) {
    try {
      const configFile = files.find((file) => file === "config.json" || file.endsWith("/config.json"));
      if (configFile === "config.json") {
        const config = JSON.parse(await readFile(path.join(localPath, configFile), "utf8"));
        const detected = Number(config.hidden_size ?? config.embedding_size ?? config.projection_dim ?? 0);
        if (detected > 0 && Number(model.dimensions) !== detected) throw new Error(`向量维度不匹配：目录检测为 ${detected}，当前填写为 ${model.dimensions}`);
      }
    } catch (error) {
      if (error instanceof Error && /向量维度不匹配/.test(error.message)) throw error;
    }
  }
}

async function validate(value: any) {
  validateRecallSettingsInput(value);
  if (!['auto', 'ep', 'external_rag', 'both_isolated'].includes(value.routing?.mode)) throw new Error("invalid_routing_mode");
  if (typeof value.routing?.ep_enabled !== "boolean" || typeof value.routing?.external_rag_enabled !== "boolean") throw new Error("invalid_routing_switch");
  for (const name of moduleNames) for (const action of Object.keys(actions)) if (typeof value.modules?.[name]?.[action] !== "boolean") throw new Error(`invalid_module_${name}_${action}`);
  for (const name of agentProcessModuleNames) if (value.modules[name].inject && !value.modules[name].retrieve) throw new Error(`agent_module_inject_requires_retrieve:${name}`);
  if (!Number.isInteger(value.budgets?.ep_total_tokens) || value.budgets.ep_total_tokens < 500 || value.budgets.ep_total_tokens > 20000) throw new Error("invalid_ep_budget");
  if (!Number.isInteger(value.budgets?.rag_total_tokens) || value.budgets.rag_total_tokens < 0 || value.budgets.rag_total_tokens > 20000) throw new Error("invalid_rag_budget");
  if (!Number.isInteger(value.budgets?.total_tokens) || value.budgets.total_tokens < 500 || value.budgets.total_tokens > 30000) throw new Error("invalid_total_budget");
  if (typeof value.rag?.enabled !== "boolean" || typeof value.rag?.root_path !== "string") throw new Error("invalid_rag_config");
  if (!value.retrieval_models?.embedding?.model || !value.retrieval_models?.reranker?.model) throw new Error("invalid_retrieval_model_config");
  if (value.rag?.embedding_profile_id && value.rag.embedding_profile_id !== value.retrieval_models.embedding.profile_id) throw new Error("RAG 绑定的 Embedding 配置不存在");
  if (value.rag?.reranker_profile_id && value.rag.reranker_profile_id !== value.retrieval_models.reranker.profile_id) throw new Error("RAG 绑定的 Rerank 配置不存在");
  await validateLocalModel(value.retrieval_models.embedding, "embedding");
  await validateLocalModel(value.retrieval_models.reranker, "reranker");
  if (!['off', 'shadow', 'assist', 'enforce'].includes(value.retrieval_models?.judge?.mode_policy)) throw new Error("invalid_judge_mode");
  if (value.retrieval_models?.judge?.enabled && value.retrieval_models?.judge?.mode_policy === "off") throw new Error("启用 JEV 后请选择影子评估、辅助判断或强制门控");
  if (typeof value.retrieval_models?.judge?.risk_gate_enabled !== "boolean") throw new Error("invalid_risk_gate");
  if (value.rag.root_path) {
    const info = await stat(value.rag.root_path).catch(() => null);
    if (!info?.isDirectory()) throw new Error("rag_root_path_not_directory");
    await access(value.rag.root_path).catch(() => { throw new Error("rag_root_path_not_readable"); });
  }
  if (!Number.isInteger(value.rag?.top_k) || value.rag.top_k < 1 || value.rag.top_k > 100) throw new Error("invalid_rag_top_k");
  if (!Number.isInteger(value.rag?.max_chunks) || value.rag.max_chunks < 1 || value.rag.max_chunks > 50) throw new Error("invalid_rag_max_chunks");
  if (value.rag?.enabled && !value.routing?.external_rag_enabled) throw new Error("rag_requires_external_route");
  if (value.budgets.total_tokens < value.budgets.ep_total_tokens) throw new Error("total_budget_below_ep_budget");
}

function mask(value: any) {
  const copy = JSON.parse(JSON.stringify(value));
  const redact = (provider: any) => { if (provider && provider.api_key) provider.api_key = `••••${String(provider.api_key).slice(-4)}`; };
  redact(copy.providers?.primary); for (const item of copy.providers?.fallbacks ?? []) redact(item);
  copy.retrieval_models = maskRetrievalModels(copy.retrieval_models);
  return copy;
}

function embeddingSignature(model: any) {
  return [model?.profile_id || "", model?.mode || "", model?.model || "", model?.local_path || "", model?.dimensions || ""].join("|");
}

export async function GET() {
  try {
    const value = await readSettings();
    return NextResponse.json({ ...mask(value), recall_policy_effective: effectiveRecallPolicy(value) });
  } catch (error) { return NextResponse.json({ error: error instanceof Error ? error.message : "runtime_settings_failed" }, { status: 400 }); }
}

export async function POST(request: Request) {
  try {
    const current = await readSettings();
    const incoming = await request.json();
    validateRecallSettingsInput(incoming);
    const value = normalizeRecallSettings(merge(current, incoming));
    delete value.recall_policy_effective;
    value.retrieval_models.judge = normalizeJevJudge(value.retrieval_models.judge);
    value.rag.embedding_profile_id = value.rag.embedding_profile_id || value.retrieval_models.embedding.profile_id;
    value.rag.reranker_profile_id = value.rag.reranker_profile_id || value.retrieval_models.reranker.profile_id;
    const previousSignature = embeddingSignature(current.retrieval_models?.embedding);
    const nextSignature = embeddingSignature(value.retrieval_models?.embedding);
    if (previousSignature && previousSignature !== nextSignature) {
      value.rag.index_status = "rebuild_required";
      value.rag.index_notice = "Embedding 配置已变化，外部 RAG 索引需要重建后才能保证向量维度一致。";
      value.rag.index_signature = nextSignature;
    } else if (!value.rag.index_signature) {
      value.rag.index_signature = nextSignature;
      value.rag.index_status = "ready";
    }
    // Masked values from the UI mean "keep the existing secret".
    if (incoming.providers?.primary?.api_key?.startsWith("••••")) value.providers.primary.api_key = current.providers.primary.api_key;
    for (let i = 0; i < (incoming.providers?.fallbacks ?? []).length; i++) if (incoming.providers.fallbacks[i]?.api_key?.startsWith("••••")) value.providers.fallbacks[i].api_key = current.providers.fallbacks[i]?.api_key ?? "";
    restoreMaskedRetrievalKeys(incoming.retrieval_models, current.retrieval_models, value.retrieval_models);
    await validate(value);
    value.updated_at = new Date().toISOString();
    await mkdir(path.dirname(settingsPath), { recursive: true });
    const temporary = `${settingsPath}.${randomUUID()}.tmp`;
    try { await writeFile(temporary, JSON.stringify(value, null, 2) + "\n", { mode: 0o600, flag: "wx" }); await rename(temporary, settingsPath); }
    finally { await unlink(temporary).catch(() => undefined); }
    return NextResponse.json({ ...mask(value), recall_policy_effective: effectiveRecallPolicy(value), applied: true });
  } catch (error) { return NextResponse.json({ error: error instanceof Error ? error.message : "runtime_settings_failed" }, { status: 400 }); }
}
