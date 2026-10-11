import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { NextIntlClientProvider } from "next-intl";
import { describe, expect, it } from "vitest";
import en from "@/messages/en.json";
import zh from "@/messages/zh-CN.json";
import { RecallPolicySettings, RagMinimumRelevanceField, updateRecallPolicy, updateRagMinimumRelevance, RelevanceAuditDetails } from "@/components/recall-policy-settings";

const render = (element: ReturnType<typeof createElement>) => renderToStaticMarkup(createElement(NextIntlClientProvider, { locale: "en", timeZone: "UTC", messages: en, children: element }));

describe("recall policy configuration", () => {
  it("shows the first-upgrade reconnect boundary beside the save control", () => {
    const html = render(createElement(RecallPolicySettings, { value: undefined, onChange: () => undefined, onSave: async () => undefined }));
    expect(html).toContain("For this upgrade, reconnect existing EP MCP connections");
    expect(html).toContain("Saving does not reload an older MCP process");
    expect(html).toContain("The receipt shows the relevance level actually used");
    expect(html).toContain("Reconnecting EP preserves your conversation context");
  });
  it("renders weak default and cumulative thresholds with advanced plane overrides", () => {
    const html = render(createElement(RecallPolicySettings, { value: undefined, onChange: () => undefined, onSave: async () => undefined }));
    expect(html).toContain("Retrieval and recall policy");
    expect(html).toContain('value="weak" selected');
    expect(html).toContain("Balanced");
    expect(html).toContain("Broad");
    expect(html).toContain("Strict");
    expect(html).toContain("Transferable methods");
    expect(html).toContain("Historical memory");
    expect(html).toContain("User memory");
    expect(html).toContain("Agent memory");
    expect(html).toContain("Relevance does not verify correctness");
  });
  it("submits merged policy values without deleting other settings", () => {
    const settings = { recall_policy: { default_min_relevance: "weak", user_memory: "inherit", agent_memory: "strong" }, rag: { enabled: false, minimum_relevance: "strong" }, providers: { primary: { model: "existing" } }, budgets: { total_tokens: 8000 } };
    const result = updateRecallPolicy(settings, { default_min_relevance: "medium" });
    expect(result.providers).toEqual(settings.providers);
    expect(result.rag).toEqual(settings.rag);
    expect(result.budgets).toEqual(settings.budgets);
    expect(result.recall_policy).toMatchObject({ default_min_relevance: "medium", user_memory: "inherit", agent_memory: "strong", advanced: { adaptive_enabled: true } });
  });
  it("applies an advanced patch to the latest policy without resetting other advanced choices", () => {
    const settings = { providers: { primary: "kept" }, recall_policy: { advanced: { allow_background: false, historical_mode: "current_only", adaptive_enabled: true } } };
    const result = updateRecallPolicy(settings, { advanced: { adaptive_enabled: false } });
    expect(result.providers).toEqual({ primary: "kept" });
    expect(result.recall_policy.advanced).toMatchObject({ allow_background: false, historical_mode: "current_only", adaptive_enabled: false });
  });
  it("updates the independent RAG threshold without enabling RAG or changing memory", () => {
    const settings = { recall_policy: { default_min_relevance: "strong" }, rag: { enabled: false, root_path: "/documents", minimum_relevance: "weak" }, routing: { external_rag_enabled: false } };
    expect(updateRagMinimumRelevance(settings, "medium")).toEqual({ ...settings, rag: { enabled: false, root_path: "/documents", minimum_relevance: "medium" } });
    const html = render(createElement(RagMinimumRelevanceField, { value: "medium", onChange: () => undefined }));
    expect(html).toContain("Minimum RAG relevance");
    expect(html).toContain('value="medium" selected');
  });
});

describe("relevance receipt details", () => {
  it("localizes native resolved inheritance while preserving raw machine fields", () => {
    const element = createElement(RelevanceAuditDetails, { audit: { effective_level: "strong", configured_level: "weak", plane_setting: "inherit", requested_level: null, plane: "user_memory", configuration_source: "global_default", reasons: { below_minimum: 2 } } });
    const html = renderToStaticMarkup(createElement(NextIntlClientProvider, { locale: "zh-CN", timeZone: "UTC", messages: zh, children: element }));
    const summary = html.slice(0, html.indexOf("原始审计"));
    expect(summary).toContain("有效最低相关度</dt><dd>强相关");
    expect(summary).toContain("配置的最低相关度</dt><dd>弱相关");
    expect(summary).toContain("记忆平面</dt><dd>用户记忆");
    expect(summary).toContain("配置来源</dt><dd>全局默认策略");
    expect(summary).toContain("本次请求覆盖</dt><dd>本次未覆盖");
    expect(summary).toContain("此记忆平面继承全局阈值");
    expect(html.slice(html.indexOf("原始审计"))).toContain("global_default");
  });
  it("keeps a missing request field unmeasured and localizes agent/RAG planes", () => {
    const absent = render(createElement(RelevanceAuditDetails, { audit: { configured_level: "weak", plane: "agent_memory", configuration_source: "plane_override" } }));
    expect(absent).toContain("Request override</dt><dd>Not measured");
    expect(absent).toContain("Memory plane</dt><dd>Agent memory");
    expect(absent).toContain("Configuration source</dt><dd>Memory plane override");
    const rag = render(createElement(RelevanceAuditDetails, { audit: { plane: "external_rag", configuration_source: "rag_setting", requested_level: "medium" } }));
    expect(rag).toContain("Memory plane</dt><dd>External RAG");
    expect(rag).toContain("Request override</dt><dd>Medium relevance");
  });
  it("explains inherited policy and per-request overrides using native audit fields", () => {
    const html = render(createElement(RelevanceAuditDetails, { audit: { effective_level: "strong", configured_level: "inherit", requested_level: "strong", plane: "agent_memory", kept_count: 2, reasons: { below_minimum: 7 } } }));
    expect(html).toContain("Configured minimum relevance");
    expect(html).toContain("Request override");
    expect(html).toContain("This memory plane inherits the global threshold");
    expect(html).toContain("below_minimum");
    expect(html).toContain("Kept by relevance filter</dt><dd>2");
  });
  it("labels bounded decisions as a sample without inventing totals", () => {
    const decisions = Array.from({ length: 22 }, (_, index) => ({ id: `candidate-${index}`, reason: `decision-${index}`, admitted: index % 2 === 0 }));
    const html = render(createElement(RelevanceAuditDetails, { audit: { effective_level: "weak", decisions, decisions_total: 62, decisions_truncated: true } }));
    expect(html).toContain("Decision sample");
    expect(html).toContain("20 shown / 62 evaluated");
    expect(html).toContain("Additional decisions omitted");
    const sample = html.slice(0, html.indexOf("Raw audit"));
    expect(sample).toContain("candidate-19");
    expect(sample).not.toContain("candidate-20");
    const unknown = render(createElement(RelevanceAuditDetails, { audit: { decisions: [{ id: "one" }] } }));
    expect(unknown).toContain("1 shown / Not measured evaluated");
  });
  it("keeps unmeasured counts unknown and exposes the original audit", () => {
    const html = render(createElement(RelevanceAuditDetails, { audit: { effective_level: "medium", plane: "user_memory", level_counts: { strong: 2, weak: 0 }, excluded_count: 3, exclusion_reasons: { below_minimum: 3 }, configuration_source: "global" } }));
    expect(html).toContain("Relevance audit");
    expect(html).toContain("Not measured");
    expect(html).toContain("below_minimum");
    expect(html).toContain("Raw audit");
    expect(html).toContain("global");
  });
});
