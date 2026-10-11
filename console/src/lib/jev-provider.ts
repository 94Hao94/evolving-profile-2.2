import provider from "../../../config/jev-provider.json";

// Shared by the connection test, settings API and user-visible configuration.
export const JEV_BASE_URL = provider.endpoint;
export const JEV_MODEL = provider.model;
export const JEV_SCOPE_DEFAULTS = provider.scopes;

export function normalizeJevJudge(judge: any = {}) {
  for (const flag of Object.values(judge.scopes || {})) if (typeof flag !== "boolean") throw new Error("invalid_judge_scope");
  return { ...judge, base_url: JEV_BASE_URL, model: JEV_MODEL, provider: "jev", mode: "systemone", scopes: { ...JEV_SCOPE_DEFAULTS, ...(judge.scopes || {}) }, risk_gate_enabled: judge.risk_gate_enabled ?? false };
}
