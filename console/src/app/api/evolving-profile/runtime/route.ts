import { epStatePath, EP_API_ENV, EP_MANAGED_MAC_HOST } from "@/lib/ep-state-paths";
import { dataplaneBankUrl, DATAPLANE_URL, getDataplaneHeaders } from "@/lib/evolving-client";
import { GET as getBackupSettings } from "../backup-settings/route";
import { NextResponse } from "next/server";
import { readdir, readFile, stat } from "node:fs/promises";
import { promisify } from "node:util";
import { execFile } from "node:child_process";
import path from "node:path";
import { parseCloudReadbackReceipt, summarizeCloudBackups, summarizeLocalBackups, type BackupFile, type CloudBackupSet } from "@/lib/backup-status";
import { readScenarioSnapshot, scenarioMetadata } from "@/lib/scenario-data";
import { readProcessSnapshot, processMetadata } from "@/lib/process-graph-data";
import { resolveConsoleOrigin } from "@/lib/console-origin";
import { normalizeJevJudge } from "@/lib/jev-provider";
import { parseModelEnvironment, resolveEffectiveEmbedding, maskRetrievalModels } from "@/lib/retrieval-model-identity";

const ENV_PATH = EP_API_ENV;
const PROFILE_PATH = epStatePath("codex.json");
const LOCAL_BACKUP_PATH = epStatePath("backups/managed/daily");
const WPS_CACHE_PATH = process.env.EVOLVING_PROFILE_CLOUD_MIRROR_ROOT || "";
const WPS_CLOUD_RECEIPT_PATH = epStatePath("runtime/wps-cloud-upload-receipt.json");
const CLOUD_MIRROR_PATH = WPS_CACHE_PATH;
const BACKUP_SETTINGS_PATH = epStatePath("config/backup-settings.json");
const GUIDANCE_SETTINGS_PATH = epStatePath("config/guidance-settings.json");
const RUNTIME_SETTINGS_PATH = epStatePath("config/runtime-settings.json");
const CONTEXT_PROGRESS_PATH = epStatePath("context/context-pipeline-progress.json");
const CONTEXT_AUDIT_PATH = epStatePath("context/context-audit.json");
const RELEASE_MANIFEST_PATH = epStatePath("config/release-manifest.json");
const RUNTIME_SETTINGS_DEFAULTS = {
  schema: "evolving-profile.runtime-settings.v1",
  modules: Object.fromEntries(["facts", "experiences", "entities", "preferences", "scenario_summary", "mental_models", "source_readback", "background_reflection", "agent_process_memory", "agent_process_trajectory", "agent_process_observation", "agent_process_failure_episode", "agent_process_repair_pattern", "agent_process_capability", "agent_process_strategy", "agent_process_revalidation"].map((name) => [name, { record: true, retrieve: true, inject: true }])),
  routing: { mode: "auto", ep_enabled: true, external_rag_enabled: false, allow_parallel: false, conflict_policy: "show_both" },
  budgets: { ep_total_tokens: 4000, rag_total_tokens: 4000, total_tokens: 6000, preference_tokens: 1200, scenario_tokens: 1200, source_tokens: 2400 },
  rag: { enabled: false, root_path: "", collection: "default", lexical_enabled: true, vector_enabled: true, fusion: "rrf", lexical_weight: 0.5, vector_weight: 0.5, rerank_enabled: true, rerank_provider: "local", rerank_model: "", top_k: 20, score_threshold: 0.35, max_chunks: 8, auto_index: false },
  retrieval_models: { embedding: { enabled: true, mode: "local", provider: "onnx", model: "intfloat/multilingual-e5-small", local_path: "", dimensions: 384, max_tokens: 512, device: "cpu", profile_id: "embedding-default", status: "configured" }, reranker: { enabled: true, mode: "local", provider: "local", model: "BAAI/bge-reranker-base", local_path: "", device: "cpu", profile_id: "reranker-default", status: "configured" }, embedding_profiles: [], reranker_profiles: [], fusion: { enabled: true, algorithm: "rrf", profile_id: "fusion-rrf" }, judge: { enabled: false, provider: "jev", mode: "api", base_url: "", model: "", api_key: "", timeout_ms: 5000, max_tokens: 600, mode_policy: "off", fallback: "rules", send_scope: "metadata_summary", risk_gate_enabled: false, status: "disabled" } },
  providers: { primary: { name: "", base_url: "", model: "", api_key: null }, fallbacks: [] },
};
const execFileAsync = promisify(execFile);

function mergeSettings(base: any, value: any): any {
  if (!value || typeof value !== "object" || Array.isArray(value)) return base;
  const result = { ...base };
  for (const [key, item] of Object.entries(value)) result[key] = item && typeof item === "object" && !Array.isArray(item) && base[key] && typeof base[key] === "object" ? mergeSettings(base[key], item) : item;
  return result;
}

function parseEnv(source: string): Record<string, string> {
  return Object.fromEntries(
    source
      .split("\n")
      .map((line) => line.trim())
      .filter((line) => line && !line.startsWith("#") && line.includes("="))
      .map((line) => {
        const index = line.indexOf("=");
        return [line.slice(0, index), line.slice(index + 1)];
      })
  );
}

function maskSecret(value: string | undefined): string | null {
  if (!value) return null;
  return value.length <= 4 ? "••••" : `••••${value.slice(-4)}`;
}

async function serviceState(url: string) {
  try {
    const response = await fetch(url, { cache: "no-store", signal: AbortSignal.timeout(1200) });
    const body = await response.json().catch(() => ({}));
    return { url, status: response.ok ? "healthy" : "attention", detail: body.functional_status ?? body.status ?? response.status };
  } catch {
    return { url, status: "unavailable", detail: "unreachable" };
  }
}

async function liveBankRecordTotal(bankId: string): Promise<number | null> {
  if (!bankId) return null;
  try {
    const response = await fetch(dataplaneBankUrl(bankId, "/memories/list?limit=0"), {
      headers: getDataplaneHeaders(), cache: "no-store", signal: AbortSignal.timeout(1500),
    });
    if (!response.ok) return null;
    const total = Number((await response.json()).total);
    return Number.isFinite(total) ? total : null;
  } catch { return null; }
}

async function flatFiles(root: string): Promise<BackupFile[]> {
  try {
    const entries = await readdir(root, { withFileTypes: true });
    return await Promise.all(entries.filter((entry) => entry.isFile()).map(async (entry) => {
      const info = await stat(path.join(root, entry.name));
      return { name: entry.name, bytes: info.size, modifiedMs: info.mtimeMs };
    }));
  } catch { return []; }
}

async function wpsCloudReadback() {
  try {
    return parseCloudReadbackReceipt(JSON.parse(await readFile(WPS_CLOUD_RECEIPT_PATH, "utf8")));
  } catch { return { observed: false }; }
}

async function backupJobState() {
  if (!EP_MANAGED_MAC_HOST) return { loaded: false, running: false, lastExitCode: null, schedule: "", apply_status: "unsupported" };
  try {
    const { stdout } = await execFileAsync("launchctl", ["print", `gui/${process.getuid?.() ?? 501}/com.evolving-profile.backup`], { timeout: 1500 });
    return {
      loaded: true,
      running: stdout.includes("state = running"),
      lastExitCode: Number(stdout.match(/last exit code = (\d+)/)?.[1] ?? 0),
      schedule: "每日 03:25",
    };
  } catch { return { loaded: false, running: false, lastExitCode: null, schedule: "每日 03:25" }; }
}

function scheduleLabel(settings: Record<string, any>) {
  const schedule = settings.schedule ?? {};
  const time = `${String(schedule.hour ?? 3).padStart(2, "0")}:${String(schedule.minute ?? 25).padStart(2, "0")}`;
  if (schedule.mode === "weekly") return `每周${["日", "一", "二", "三", "四", "五", "六"][schedule.weekday ?? 1]} ${time}`;
  if (schedule.mode === "monthly") return `每月 ${schedule.day ?? 1} 日 ${time}`;
  return `每日 ${time}`;
}

export async function GET(request?: Request) {
  const requestedBank = request ? new URL(request.url).searchParams.get("bankId") : null;
  let env: Record<string, string> = {};
  let profile: Record<string, unknown> = {};
  let backupSettings: Record<string, unknown> = {};
  let guidanceSettings: Record<string, unknown> = {};
  let runtimeSettings: Record<string, any> = {};
  let releaseManifest: Record<string, any> = { product_version: "5.1", release_channel: "development", build_id: "ep51-dev" };
  let contextProgress: Record<string, any> = { status: "unavailable", total: 0, queued: 0, running: 0, retrying: 0, succeeded: 0, failed: 0 };
  let contextAudit: Record<string, any> = { status: "not_run", error_count: null, warning_count: null };
  try {
    env = parseModelEnvironment((await readFile(ENV_PATH, "utf8")) + "\n" + await readFile(path.join(path.dirname(RUNTIME_SETTINGS_PATH),"retrieval-models.env"),"utf8").catch(()=>""));
  } catch {
    // The page remains truthful when the operator-managed env file is unavailable.
  }
  try {
    profile = JSON.parse(await readFile(PROFILE_PATH, "utf8"));
  } catch {
    // The page remains truthful when the operator-managed profile is unavailable.
  }
  const configuredBank = String(process.env.EVOLVING_PROFILE_BANK_ID || profile.bankId || "");
  const scenarioSnapshot = await readScenarioSnapshot(requestedBank).catch(() => null);
  backupSettings = await (await getBackupSettings()).json();
  const guidanceDefaults = { schema: "evolving-profile.guidance-settings.v1", max_candidates: 6, adaptive_budget: true, auto_probe: true, probe_max_tokens: 500 };
  try { guidanceSettings = { ...guidanceDefaults, ...JSON.parse(await readFile(GUIDANCE_SETTINGS_PATH, "utf8")) }; } catch { guidanceSettings = guidanceDefaults; }
  try { runtimeSettings = mergeSettings(RUNTIME_SETTINGS_DEFAULTS, JSON.parse(await readFile(RUNTIME_SETTINGS_PATH, "utf8"))); } catch { runtimeSettings = RUNTIME_SETTINGS_DEFAULTS; }
  try { releaseManifest = JSON.parse(await readFile(RELEASE_MANIFEST_PATH, "utf8")); } catch { /* use safe fallback */ }
  runtimeSettings.retrieval_models.embedding = resolveEffectiveEmbedding(runtimeSettings.retrieval_models.embedding,env);
  if(runtimeSettings.retrieval_models.embedding.api_key) runtimeSettings.retrieval_models.embedding.api_key=`••••${String(runtimeSettings.retrieval_models.embedding.api_key).slice(-4)}`;
  runtimeSettings.retrieval_models.reranker = { ...runtimeSettings.retrieval_models.reranker, enabled: env.EVOLVING_PROFILE_API_RERANKER_PROVIDER === "rrf" ? false : runtimeSettings.retrieval_models.reranker.enabled, provider: env.EVOLVING_PROFILE_API_RERANKER_PROVIDER === "rrf" ? "rrf" : env.EVOLVING_PROFILE_API_RERANKER_PROVIDER || runtimeSettings.retrieval_models.reranker.provider, model: env.EVOLVING_PROFILE_API_RERANKER_LOCAL_MODEL || runtimeSettings.retrieval_models.reranker.model };
  runtimeSettings.retrieval_models.judge = normalizeJevJudge(runtimeSettings.retrieval_models.judge);
  if (runtimeSettings.retrieval_models.judge.api_key) runtimeSettings.retrieval_models.judge.api_key = `••••${String(runtimeSettings.retrieval_models.judge.api_key).slice(-4)}`;
  runtimeSettings.retrieval_models=maskRetrievalModels(runtimeSettings.retrieval_models);
  const effectiveProvider = {
    name: String(runtimeSettings.providers?.primary?.name || (env.EVOLVING_PROFILE_API_LLM_BASE_URL?.includes("127.0.0.1:3211") ? "Coding Plan" : env.EVOLVING_PROFILE_API_LLM_PROVIDER || "")),
    base_url: String(runtimeSettings.providers?.primary?.base_url || env.EVOLVING_PROFILE_API_LLM_BASE_URL || ""),
    model: String(runtimeSettings.providers?.primary?.model || env.EVOLVING_PROFILE_API_LLM_MODEL || ""),
    api_key: runtimeSettings.providers?.primary?.api_key || env.EVOLVING_PROFILE_API_LLM_API_KEY || "",
  };
  runtimeSettings = { ...runtimeSettings, providers: { ...(runtimeSettings.providers || {}), primary: effectiveProvider } };
  try { contextProgress = JSON.parse(await readFile(CONTEXT_PROGRESS_PATH, "utf8")); } catch { /* progress remains unavailable */ }
  try { contextAudit = JSON.parse(await readFile(CONTEXT_AUDIT_PATH, "utf8")); } catch { /* audit remains unavailable */ }
  const processMemory = await readProcessSnapshot(requestedBank).then(processMetadata).catch(()=>({status:"not_available_for_bank",version:"unavailable",bankId:null,record_count:0,by_kind:{},skill_candidates:0,profiles:0,revalidation_queue:0,updated_at:null,recent:[]}));

  const [api, controller, localFiles, cloudReadback, backupJob, liveBankTotal] = await Promise.all([
    serviceState(`${DATAPLANE_URL}/health`),
    serviceState(`${process.env.EVOLVING_PROFILE_CONTROLLER_API_URL || "http://127.0.0.1:12079"}/health`),
    flatFiles(path.join(String((backupSettings.local as any)?.root || epStatePath("backups/managed")), "daily")), wpsCloudReadback(), backupJobState(), liveBankRecordTotal(configuredBank),
  ]);
  backupJob.schedule = scheduleLabel(backupSettings);
  const localBackup = summarizeLocalBackups(localFiles);
  const mirrorSets: CloudBackupSet[] = cloudReadback.observed && cloudReadback.fileName ? [{
    setName: cloudReadback.fileName.replace(/\.zip$/i, ""), bytes: cloudReadback.fileSize ?? 0,
    modifiedMs: cloudReadback.completedAt ? Date.parse(cloudReadback.completedAt) : Date.now(), encryptedFiles: 1, plaintextFiles: 0,
  }] : [];
  // Do not touch WPS's placeholder cache on the request path. It can block
  // filesystem workers, and cache presence is not proof of a completed upload.
  const wpsCache = { present: false, fileCount: 0, timedOut: false, probeSkipped: true };
  const cloudBackup = summarizeCloudBackups(mirrorSets, wpsCache, Date.now(), cloudReadback);

  return NextResponse.json({
    schema: "evolving-profile.runtime.v1",
    release: releaseManifest,
    model: {
      provider: effectiveProvider.name || env.EVOLVING_PROFILE_API_LLM_PROVIDER || "not_configured",
      model: effectiveProvider.model || env.EVOLVING_PROFILE_API_LLM_MODEL || "not_configured",
      baseUrl: effectiveProvider.base_url || env.EVOLVING_PROFILE_API_LLM_BASE_URL || "not_configured",
      apiKey: maskSecret(effectiveProvider.api_key || env.EVOLVING_PROFILE_API_LLM_API_KEY),
    },
    services: [
      { name: "Evolving Profile API", ...api },
      { name: "Query Controller", ...controller },
      { name: "Recovery Service", url: "http://127.0.0.1:12079", status: controller.status, detail: controller.status === "healthy" ? "Controller recovery lanes + runtime guidance refresh" : "recovery unavailable" },
      { name: "Console", url: resolveConsoleOrigin(request?.url ?? "http://127.0.0.1:9999", process.env.PORT), status: "healthy", detail: "current request origin" },
    ],
    hostIntegration: {
      hosts: ["Codex", "Claude Code", "Hermes"],
      hookStages: ["SessionStart", "UserPromptSubmit", "PostToolUse", "PreCompact", "Stop"],
      mcpServer: "evolving-profile-controller-mcp",
      entry: "get_preference",
    },
    behavior: {
      guidance: "入口指导检查只选择已有多维度偏好与融合心智模型，不创建长期模型。",
      retrieval: profile.autoRecall === false
        ? "入口可执行独立的一次有界候选探测；单点问题主动 Recall，综合盘点可直接 Research，关键结论读取 read_source 的原文。探测候选不代表问题已覆盖。"
        : "自动历史召回仍开启；Codex 也可按需使用 recall、research 与 read_source。",
      controller: "Controller 保留为可选检索编排、关系扩展、预算建议和审计服务；默认 MCP 候选读取不经过其语义准入。",
      background: "长期偏好与融合心智模型的提炼、归并和更新只在后台加工，前台查询不重复调用后台模型。",
      backup: `${scheduleLabel(backupSettings)} 生成已选的本地备份制品。`,
    },
    backup: {
      settings: backupSettings,
      local: { ...localBackup, retentionDays: Number((backupSettings.local as any)?.retention_days ?? 14), location: String((backupSettings.local as any)?.root ?? LOCAL_BACKUP_PATH) },
      cloud: { ...cloudBackup, expectedRetentionSets: Number((backupSettings.cloud as any)?.retention_sets ?? 2), location: CLOUD_MIRROR_PATH, encryption: String((backupSettings.cloud as any)?.encryption ?? "AES-256-CBC + PBKDF2-SHA256") },
      job: backupJob,
    },
    guidanceSettings,
    runtimeSettings: { ...runtimeSettings, providers: { ...runtimeSettings.providers, primary: { ...runtimeSettings.providers?.primary, api_key: runtimeSettings.providers?.primary?.api_key ? `••••${String(runtimeSettings.providers.primary.api_key).slice(-4)}` : null }, fallbacks: (runtimeSettings.providers?.fallbacks ?? []).map((provider: any) => ({ ...provider, api_key: provider.api_key ? `••••${String(provider.api_key).slice(-4)}` : null })) } },
    processMemory,
    payloadMode: "status_only",
    dataEndpoints: { scenarioGraph: "/api/evolving-profile/scenario/graph", scenarioDetail: "/api/evolving-profile/scenario/detail", scenarioEpisodes: "/api/evolving-profile/scenario/episodes", processGraph: "/api/evolving-profile/process-memory/graph" },
    context: scenarioSnapshot ? {
      ...scenarioMetadata(scenarioSnapshot, liveBankTotal),
      executionOwner: "deterministic_projection_pending_model_review", externalEpModel: effectiveProvider.model,
      progress: contextProgress, audit: contextAudit,
    } : {
      status: "not_available_for_bank", schema: "evolving-profile.context-index.v1",
      sessionCount: 0, projectCount: 0, pendingReview: 0, qualityStatus: "unavailable",
      pipeline: "当前 Bank 未建立情景摘要索引", sourceOfTruth: "not_available_for_bank", evidenceRole: "none", updatedAt: null,
      graphStats: { nodes: 0, edges: 0, timeline: 0, sessions: 0, workspaces: 0, verifiedProjects: 0, bankRecords: 0, unit: "indexed_nodes_and_snapshot_relations" },
    },
  });
}
