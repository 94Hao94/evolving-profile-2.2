import type { ReactElement } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
const harness = vi.hoisted(() => ({ stateIndex: 0, state: null as any, fetch: vi.fn() }));
vi.mock("react", async (original) => ({ ...(await original<typeof import("react")>()), useEffect: () => undefined, useCallback: (fn: unknown) => fn, useState: (initial: unknown) => {
  const index = harness.stateIndex++;
  return [index === 0 ? harness.state : initial, index === 0 ? (value: any) => { harness.state = typeof value === "function" ? value(harness.state) : value; } : () => undefined];
} }));
vi.mock("next-intl", () => ({ useLocale: () => "en", useTranslations: () => (key: string) => key }));
import { EvolvingProfileRuntimeView } from "@/components/evolving-profile-runtime-view";
import { RuntimeSectionsView } from "@/components/runtime-sections-view";
import { RecallPolicySettings } from "@/components/recall-policy-settings";

function findPolicy(node: unknown): Record<string, any> {
  if (Array.isArray(node)) { for (const child of node) { const found = findPolicy(child); if (found.onSave) return found; } }
  else if (node && typeof node === "object") { const element = node as ReactElement<Record<string, any>>; if (element.type === RecallPolicySettings) return element.props; if (element.props?.children) return findPolicy(element.props.children); }
  return {};
}
beforeEach(() => {
  harness.stateIndex = 0; vi.clearAllMocks(); vi.stubGlobal("fetch", harness.fetch);
  harness.state = { model: {}, services: [], hostIntegration: { hosts: [], hookStages: [] }, behavior: {}, backup: { local: { artifacts: {} }, cloud: { wpsCache: {} }, job: {} }, context: {}, runtimeSettings: { modules: {}, routing: {}, budgets: {}, rag: { enabled: false, minimum_relevance: "strong" }, providers: {}, recall_policy: { default_min_relevance: "weak", user_memory: "inherit", agent_memory: "inherit" } } };
});
describe("runtime policy operations", () => {
  it("exposes the policy save control in the live BankConfig runtime section", async () => {
    const props = findPolicy(RuntimeSectionsView({ section: "runtime" }));
    expect(props.onSave).toBeTypeOf("function");
    props.onChange({ user_memory: "strong" });
    expect(harness.state.runtimeSettings.recall_policy.user_memory).toBe("strong");
    harness.stateIndex = 0;
    harness.fetch.mockResolvedValue({ ok: true, json: async () => ({ saved: false, error: "Policy save rejected" }) });
    await expect(findPolicy(RuntimeSectionsView({ section: "runtime" })).onSave()).rejects.toThrow("Policy save rejected");
  });
  it.each(["legacy", "sections"] as const)("%s saves only recall policy and keeps other panel drafts", async (view) => {
    const renderView = () => view === "legacy" ? EvolvingProfileRuntimeView() : RuntimeSectionsView({ section: "runtime" });
    const props = findPolicy(renderView());
    expect(props.onChange).toBeTypeOf("function");
    harness.state.runtimeSettings.providers = { primary: { model: "concurrent edit" } };
    props.onChange({ default_min_relevance: "medium" });
    harness.stateIndex = 0;
    const changed = findPolicy(renderView());
    harness.fetch.mockResolvedValue({ ok: true, json: async () => ({ saved: true }) });
    await changed.onSave();
    const submitted = JSON.parse(harness.fetch.mock.calls[0][1].body);
    expect(submitted.recall_policy).toEqual({ default_min_relevance: "medium", user_memory: "inherit", agent_memory: "inherit", advanced: { allow_transferable_methods: true, allow_background: true, historical_mode: "reference_only", scope_unknown_mode: "keep_navigation", adaptive_enabled: true } });
    expect(Object.keys(submitted)).toEqual(["recall_policy"]);
    expect(harness.state.runtimeSettings.providers.primary.model).toBe("concurrent edit");
    expect(harness.state.runtimeSettings.rag).toEqual({ enabled: false, minimum_relevance: "strong" });
  });
  it.each(["legacy", "sections"] as const)("%s merges only the returned saved policy into latest drafts", async (view) => {
    const renderView = () => view === "legacy" ? EvolvingProfileRuntimeView() : RuntimeSectionsView({ section: "runtime" });
    const props = findPolicy(renderView());
    let complete!: (value: any) => void;
    harness.fetch.mockImplementation(() => new Promise((resolve) => { complete = resolve; }));
    const saving = props.onSave();
    harness.state = { ...harness.state, runtimeSettings: { ...harness.state.runtimeSettings, providers: { primary: { model: "new unsaved provider" } }, rag: { enabled: false, root_path: "/new-unsaved-directory" }, retrieval_models: { embedding: { model: "draft embedding" } } } };
    const savedPolicy = { default_min_relevance: "medium", user_memory: "inherit", agent_memory: "inherit" };
    complete({ ok: true, json: async () => ({ recall_policy: savedPolicy, providers: { primary: { model: "server model" } }, rag: { enabled: true }, retrieval_models: { embedding: { model: "server embedding" } } }) });
    await saving;
    expect(harness.state.runtimeSettings.recall_policy).toMatchObject(savedPolicy);
    expect(harness.state.runtimeSettings.providers.primary.model).toBe("new unsaved provider");
    expect(harness.state.runtimeSettings.rag).toEqual({ enabled: false, root_path: "/new-unsaved-directory" });
    expect(harness.state.runtimeSettings.retrieval_models.embedding.model).toBe("draft embedding");
  });
  it.each(["legacy", "sections"] as const)("%s does not overwrite recall edits made while saving", async (view) => {
    const props = findPolicy(view === "legacy" ? EvolvingProfileRuntimeView() : RuntimeSectionsView({ section: "runtime" }));
    let complete!: (value: any) => void;
    harness.fetch.mockImplementation(() => new Promise((resolve) => { complete = resolve; }));
    const saving = props.onSave();
    props.onChange({ advanced: { allow_background: false } });
    complete({ ok: true, json: async () => ({ recall_policy: { default_min_relevance: "weak", user_memory: "inherit", agent_memory: "inherit" } }) });
    await saving;
    expect(harness.state.runtimeSettings.recall_policy.advanced.allow_background).toBe(false);
  });
  it("applies advanced controls to the latest parent settings without losing concurrent edits", () => {
    const props = findPolicy(EvolvingProfileRuntimeView());
    harness.state.runtimeSettings.recall_policy.advanced = { allow_background: false, historical_mode: "current_only" };
    harness.state.runtimeSettings.budgets.total_tokens = 4200;
    props.onChange({ advanced: { adaptive_enabled: false } });
    expect(harness.state.runtimeSettings.recall_policy.advanced).toEqual({ allow_background: false, historical_mode: "current_only", allow_transferable_methods: true, scope_unknown_mode: "keep_navigation", adaptive_enabled: false });
    expect(harness.state.runtimeSettings.budgets.total_tokens).toBe(4200);
  });
  it("rejects an HTTP success with saved false so the button cannot show success", async () => {
    const props = findPolicy(EvolvingProfileRuntimeView());
    expect(props.onSave).toBeTypeOf("function");
    harness.fetch.mockResolvedValue({ ok: true, json: async () => ({ saved: false, error: "Policy save rejected" }) });
    await expect(props.onSave()).rejects.toThrow("Policy save rejected");
  });
});
