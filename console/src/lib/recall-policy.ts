import manifest from "../../../config/recall-policy.json";

export type RecallRelevanceLevel = "strong" | "medium" | "weak";
export type RecallPlaneOverride = RecallRelevanceLevel | "inherit";
export type RecallAdvancedPolicy = { allow_transferable_methods: boolean; allow_background: boolean; historical_mode: "reference_only" | "current_only"; scope_unknown_mode: "keep_navigation" | "require_verified"; adaptive_enabled: boolean };
export type RecallPolicyAdvanced = RecallAdvancedPolicy;
export type RecallPolicy = { default_min_relevance: RecallRelevanceLevel; user_memory: RecallPlaneOverride; agent_memory: RecallPlaneOverride; advanced?: RecallAdvancedPolicy };
export const RECALL_RELEVANCE_LEVELS = manifest.levels as RecallRelevanceLevel[];
export const RECALL_POLICY_DEFAULTS = manifest.defaults.recall_policy as RecallPolicy;
export const RECALL_ADVANCED_DEFAULTS = manifest.defaults.recall_policy.advanced as RecallAdvancedPolicy;
export const RAG_MINIMUM_RELEVANCE_DEFAULT = manifest.defaults.rag.minimum_relevance as RecallRelevanceLevel;

function object(value: unknown, field: string): asserts value is Record<string, any> {
  if (value === null || typeof value !== "object" || Array.isArray(value)) throw new Error(`invalid_${field}_object`);
}

export function normalizeRecallAdvancedPolicy(value: unknown = {}): RecallAdvancedPolicy {
  object(value, "recall_advanced");
  for (const key of Object.keys(value)) if (!(key in RECALL_ADVANCED_DEFAULTS)) throw new Error("invalid_recall_advanced_field");
  const advanced = { ...RECALL_ADVANCED_DEFAULTS, ...value } as RecallAdvancedPolicy;
  for (const key of ["allow_transferable_methods", "allow_background", "adaptive_enabled"] as const) {
    if (typeof advanced[key] !== "boolean") throw new Error(`invalid_recall_advanced_${key}`);
  }
  for (const key of ["historical_mode", "scope_unknown_mode"] as const) {
    if (!(manifest.advanced_enums[key] as string[]).includes(advanced[key])) throw new Error(`invalid_recall_advanced_${key}`);
  }
  return advanced;
}

export function normalizeRecallPolicy(value: unknown = {}): RecallPolicy & { advanced: RecallAdvancedPolicy } {
  object(value, "recall_policy");
  const policy = { ...RECALL_POLICY_DEFAULTS, ...value } as RecallPolicy;
  if (!manifest.levels.includes(policy.default_min_relevance)) throw new Error("invalid_recall_policy_default_min_relevance");
  for (const plane of ["user_memory", "agent_memory"] as const) {
    if (!manifest.plane_overrides.includes(policy[plane])) throw new Error(`invalid_recall_policy_${plane}`);
  }
  return { ...policy, advanced: normalizeRecallAdvancedPolicy("advanced" in value ? value.advanced : {}) };
}

export function validateRecallSettingsInput(value: unknown): asserts value is Record<string, any> {
  object(value, "runtime_settings");
  for (const field of ["modules", "routing", "budgets", "rag", "providers", "retrieval_models"]) {
    if (field in value) object(value[field], field);
  }
  if ("recall_policy" in value) normalizeRecallPolicy(value.recall_policy);
  if (value.rag && "minimum_relevance" in value.rag && !manifest.levels.includes(value.rag.minimum_relevance)) throw new Error("invalid_rag_minimum_relevance");
  if (value.providers) {
    if ("primary" in value.providers) object(value.providers.primary, "providers_primary");
    if ("fallbacks" in value.providers) {
      if (!Array.isArray(value.providers.fallbacks)) throw new Error("invalid_providers_fallbacks_array");
      for (const provider of value.providers.fallbacks) object(provider, "providers_fallback");
    }
  }
  if (value.retrieval_models) {
    for (const field of ["embedding", "reranker", "fusion", "judge"]) if (field in value.retrieval_models) object(value.retrieval_models[field], `retrieval_models_${field}`);
    for (const field of ["embedding_profiles", "reranker_profiles"]) if (field in value.retrieval_models) {
      if (!Array.isArray(value.retrieval_models[field])) throw new Error(`invalid_${field}_array`);
      for (const profile of value.retrieval_models[field]) object(profile, field);
    }
  }
}

export function normalizeRecallSettings<T extends Record<string, any>>(value: T) {
  validateRecallSettingsInput(value);
  return { ...value, recall_policy: normalizeRecallPolicy(value.recall_policy), rag: { ...(value.rag ?? {}), minimum_relevance: value.rag?.minimum_relevance ?? RAG_MINIMUM_RELEVANCE_DEFAULT } };
}

export function effectiveRecallPolicy(value: Record<string, any>) {
  const settings = normalizeRecallSettings(value);
  const globalLevel = settings.recall_policy.default_min_relevance;
  const result = {} as Record<"user_memory" | "agent_memory" | "external_rag", { effective_level: RecallRelevanceLevel; configured_level: RecallRelevanceLevel; policy_version: string; configuration_source: "global_default" | "plane_override" | "external_rag"; global_level: RecallRelevanceLevel; plane_setting: RecallPlaneOverride }>;
  for (const plane of ["user_memory", "agent_memory", "external_rag"] as const) {
    const planeSetting = plane === "external_rag" ? settings.rag.minimum_relevance : settings.recall_policy[plane];
    const configured = planeSetting === "inherit" ? globalLevel : planeSetting;
    result[plane] = { effective_level: configured, configured_level: configured, policy_version: manifest.policy_version,
      configuration_source: plane === "external_rag" ? "external_rag" : planeSetting === "inherit" ? "global_default" : "plane_override", global_level: globalLevel, plane_setting: planeSetting };
  }
  return result;
}
