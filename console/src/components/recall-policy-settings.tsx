"use client";

import { useTranslations } from "next-intl";
import { ActionButton } from "@/components/ui/action-button";
import { normalizeRecallPolicy, RECALL_POLICY_DEFAULTS, RAG_MINIMUM_RELEVANCE_DEFAULT, type RecallRelevanceLevel, type RecallPolicy } from "@/lib/recall-policy";

export type MinimumRelevance = RecallRelevanceLevel;
export type RecallPolicyValue = ReturnType<typeof normalizeRecallPolicy>;
export type RecallPolicyPatch = Omit<Partial<RecallPolicyValue>, "advanced"> & { advanced?: Partial<RecallPolicyValue["advanced"]> };

// Parent views apply these patches to their latest state; this card owns no copy.
export function updateRecallPolicy<T extends { recall_policy?: unknown }>(settings: T, patch: RecallPolicyPatch) {
  const current = normalizeRecallPolicy(settings.recall_policy);
  return { ...settings, recall_policy: normalizeRecallPolicy({ ...current, ...patch, advanced: { ...current.advanced, ...patch.advanced } }) };
}
export function updateRagMinimumRelevance<T extends { rag: Record<string, unknown> }>(settings: T, level: MinimumRelevance) {
  return { ...settings, rag: { ...settings.rag, minimum_relevance: level } };
}

function RelevanceSelect({ value, onChange, label, inherit = false }: { value: string; onChange: (value: string) => void; label: string; inherit?: boolean }) {
  const t = useTranslations("recallPolicy");
  return <label className="block space-y-1 text-sm"><span>{label}</span><select className="h-9 w-full rounded border bg-background px-2" value={value} onChange={(event) => onChange(event.target.value)}>
    {inherit && <option value="inherit">{t("inherit")}</option>}
    <option value="strong">{t("strict")}</option><option value="medium">{t("balanced")}</option><option value="weak">{t("broad")}</option>
  </select></label>;
}

export function RecallPolicySettings({ value, onChange, onSave }: { value?: Partial<RecallPolicyValue>; onChange: (patch: RecallPolicyPatch) => void; onSave: () => Promise<unknown> }) {
  const t = useTranslations("recallPolicy");
  const policy = normalizeRecallPolicy(value);
  return <article id="ep-recall-policy" className="space-y-4 rounded-lg border p-4">
    <div><h3 className="text-sm font-semibold">{t("title")}</h3><p className="mt-1 text-xs text-muted-foreground">{t("description")}</p></div>
    <div className="max-w-md"><RelevanceSelect label={t("globalMinimum")} value={policy.default_min_relevance} onChange={(level) => onChange({ default_min_relevance: level as MinimumRelevance })} /></div>
    <p className="text-xs text-muted-foreground">{t("relevanceHelp")}</p>
    <details className="rounded border p-3"><summary className="cursor-pointer text-sm">{t("advanced")}</summary><div className="mt-3 grid gap-4 md:grid-cols-2">
      <RelevanceSelect inherit label={t("userMemory")} value={policy.user_memory} onChange={(level) => onChange({ user_memory: level as RecallPolicyValue["user_memory"] })} />
      <RelevanceSelect inherit label={t("agentMemory")} value={policy.agent_memory} onChange={(level) => onChange({ agent_memory: level as RecallPolicyValue["agent_memory"] })} />
    </div><p className="mt-2 text-xs text-muted-foreground">{t("overrideHelp")}</p>
      <div className="mt-4 grid gap-4 md:grid-cols-2">
        {(["allow_transferable_methods", "allow_background", "adaptive_enabled"] as const).map((field) => <label key={field} className="flex items-start gap-2 text-sm"><input type="checkbox" className="mt-1" checked={policy.advanced[field]} onChange={(event) => onChange({ advanced: { [field]: event.target.checked } })} /><span>{t(field)}</span></label>)}
        <label className="block space-y-1 text-sm"><span>{t("historicalMode")}</span><select className="h-9 w-full rounded border bg-background px-2" value={policy.advanced.historical_mode} onChange={(event) => onChange({ advanced: { historical_mode: event.target.value as RecallPolicyValue["advanced"]["historical_mode"] } })}><option value="reference_only">{t("historicalReference")}</option><option value="current_only">{t("historicalCurrent")}</option></select></label>
        <label className="block space-y-1 text-sm"><span>{t("scopeUnknownMode")}</span><select className="h-9 w-full rounded border bg-background px-2" value={policy.advanced.scope_unknown_mode} onChange={(event) => onChange({ advanced: { scope_unknown_mode: event.target.value as RecallPolicyValue["advanced"]["scope_unknown_mode"] } })}><option value="keep_navigation">{t("scopeNavigation")}</option><option value="require_verified">{t("scopeVerified")}</option></select></label>
      </div><p className="mt-3 text-xs leading-5 text-muted-foreground">{t("advancedHelp")}</p>
    </details>
    <p className="rounded border bg-muted/30 p-3 text-xs leading-5 text-muted-foreground" data-testid="recall-policy-upgrade-note">{t("upgradeReconnect")}</p>
    <ActionButton resetKey={JSON.stringify(policy)} onAction={onSave} pendingLabel={t("saving")} successLabel={t("saved")} errorLabel={t("saveFailed")}>{t("save")}</ActionButton>
  </article>;
}

export function RagMinimumRelevanceField({ value, onChange }: { value?: MinimumRelevance; onChange: (value: MinimumRelevance) => void }) {
  const t = useTranslations("recallPolicy");
  return <div className="space-y-2"><RelevanceSelect label={t("ragMinimum")} value={value ?? RAG_MINIMUM_RELEVANCE_DEFAULT} onChange={(level) => onChange(level as MinimumRelevance)} /><p className="text-xs text-muted-foreground">{t("ragHelp")}</p></div>;
}

export function RelevanceAuditDetails({ audit }: { audit?: Record<string, unknown> | null }) {
  const t = useTranslations("recallPolicy");
  if (!audit) return null;
  const display = (value: unknown): string | number => value == null ? t("notMeasured") : typeof value === "string" || typeof value === "number" ? value : JSON.stringify(value);
  const levelLabel = (value: unknown) => value === "inherit" ? t("inherit") : typeof value === "string" && ["strong", "medium", "weak", "none", "unknown"].includes(value) ? t(`category_${value}`) : display(value);
  const planeLabel = (value: unknown) => value === "user_memory" ? t("userMemory") : value === "agent_memory" || value === "agent_process" ? t("agentMemory") : value === "external_rag" ? t("externalRag") : display(value);
  const sourceLabel = (value: unknown) => {
    const keys: Record<string, string> = { global: "sourceGlobal", global_default: "sourceGlobal", plane_override: "sourcePlane", request_override: "sourceRequest", rag_setting: "sourceRag", rag_default: "sourceRagDefault", external_rag_setting: "sourceRag", external_rag_default: "sourceRagDefault" };
    return typeof value === "string" && Object.hasOwn(keys, value) ? t(keys[value]) : display(value);
  };
  const requestLabel = Object.hasOwn(audit, "requested_level") && audit.requested_level === null ? t("noRequestOverride") : levelLabel(audit.requested_level);
  const level = audit.effective_level ?? audit.effective_min_relevance;
  const counts = audit.level_counts as Record<string, unknown> | undefined;
  const decisions = Array.isArray(audit.decisions) ? audit.decisions.slice(0, 20) as Array<Record<string, unknown>> : [];
  return <section className="mt-3 space-y-2 rounded border p-3 text-xs" data-testid="relevance-audit"><h4 className="font-semibold">{t("auditTitle")}</h4>
    <dl className="grid grid-cols-[minmax(0,1fr)_minmax(0,1fr)] gap-2 break-words">
      <dt>{t("effectiveMinimum")}</dt><dd>{levelLabel(level)}</dd><dt>{t("plane")}</dt><dd>{planeLabel(audit.plane)}</dd><dt>{t("configurationSource")}</dt><dd>{sourceLabel(audit.configuration_source)}</dd>
      <dt>{t("configuredMinimum")}</dt><dd>{levelLabel(audit.configured_level)}</dd><dt>{t("requestOverride")}</dt><dd>{requestLabel}</dd>
      <dt>{t("policyVersion")}</dt><dd>{display(audit.policy_version)}</dd><dt>{t("returned")}</dt><dd>{display(audit.returned_count)}</dd><dt>{t("excluded")}</dt><dd>{display(audit.excluded_count)}</dd>
      <dt>{t("kept")}</dt><dd>{display(audit.kept_count)}</dd>
      {(["strong", "medium", "weak", "none", "unknown"] as const).map((category) => <div key={category} className="contents"><dt>{t(`category_${category}`)}</dt><dd>{display(counts?.[category])}</dd></div>)}
      <dt>{t("exclusionReasons")}</dt><dd>{display(audit.exclusion_reasons ?? audit.reasons)}</dd><dt>{t("reason")}</dt><dd>{display(audit.reason)}</dd>
    </dl>{(audit.configured_level === "inherit" || audit.plane_setting === "inherit" || audit.configuration_source === "global_default") && <p className="text-muted-foreground">{t("inheritanceExplanation")}</p>}<p className="text-muted-foreground">{t("relevanceHelp")}</p>
    {Array.isArray(audit.decisions) && <details><summary className="cursor-pointer">{t("decisionSample")}</summary><p className="mt-2 text-muted-foreground">{t("sampleCounts", { shown: decisions.length, total: String(display(audit.decisions_total)) })}</p>{(audit.decisions_truncated === true || audit.decisions.length > 20) && <p className="text-muted-foreground">{t("decisionsTruncated")}</p>}
      <ul className="mt-2 max-h-64 space-y-2 overflow-auto">{decisions.map((decision, index) => <li key={index} className="rounded border p-2 break-words"><div>{display(decision.id ?? decision.candidate_id ?? decision.memory_id)} · {levelLabel(decision.level ?? decision.relevance_level ?? decision.relevance)}</div><div className="text-muted-foreground">{display(decision.reason ?? decision.reasons)}</div></li>)}</ul>
    </details>}
    <details><summary className="cursor-pointer">{t("rawAudit")}</summary><pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap break-words">{JSON.stringify(audit, null, 2)}</pre></details>
  </section>;
}
