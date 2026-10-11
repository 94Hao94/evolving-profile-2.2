import { describe, expect, it } from "vitest";
import {
  catalogStatusLabel,
  entryGuidanceLabel,
  guidanceNodeValue,
  historyEmptyStateLabel,
  historyToolStatus,
  observationWindowMinutes,
  routeConfidenceLabel,
  scenarioFreshnessLabel,
  uniqueCatalogHints,
} from "@/components/flow-view";

describe("FlowView route confidence", () => {
  it("explains a parent Session update separately from a stale selected episode", () => {
    expect(scenarioFreshnessLabel("span_current_parent_revision_changed")).toBe(
      "本段来源未变 · Session 其他内容已更新"
    );
    expect(scenarioFreshnessLabel("stale_source_changed")).toBe(
      "来源范围已变化 · 暂不显示缓存摘要"
    );
  });

  it("defaults prompt-window labels to two minutes but preserves observed values", () => {
    expect(observationWindowMinutes(undefined)).toBe(2);
    expect(observationWindowMinutes(3)).toBe(3);
  });

  it("does not turn agent_decides null confidence into a zero score", () => {
    expect(routeConfidenceLabel({ recommended_route: "agent_decides", confidence: null })).toBe(
      "由当前 Agent 判断 · 不评分"
    );
  });

  it("keeps numeric confidence for deterministic boundaries", () => {
    expect(routeConfidenceLabel({ recommended_route: "skip", confidence: 1 })).toBe(
      "路线信号 100/100 · 启发式"
    );
  });

  it("collapses repeated navigation hints with the same visible meaning", () => {
    const hints = uniqueCatalogHints([
      { memory_id: "m1", topic: "Evolving Profile 相关记录", type: "memory" },
      { memory_id: "m2", topic: "Evolving Profile 相关记录", type: "memory" },
      { memory_id: "m3", topic: "备份相关经历", type: "experience" },
    ]);
    expect(hints.map((hint) => hint.topic)).toEqual(["Evolving Profile 相关记录", "备份相关经历"]);
  });

  it("distinguishes unknown catalog state from a computed zero", () => {
    expect(
      catalogStatusLabel({
        pending_changes: null,
        pending_changes_status: "not_computed",
        conflicts: null,
        conflict_status: "not_computed",
        content_status: "entity_navigation_only",
      })
    ).toContain("更新状态未计算");
    expect(
      catalogStatusLabel({
        pending_changes: 0,
        pending_changes_status: "computed",
        conflicts: [],
        conflict_status: "computed",
        content_status: "reviewed_topic_overview",
      })
    ).toContain("待更新 0");
  });

  it("labels agent-owned guidance as pending instead of already checked", () => {
    expect(entryGuidanceLabel(true, "agent_decision_pending")).toBe(
      "核心说明已生成 · 私人偏好待 Agent 判断"
    );
    expect(entryGuidanceLabel(true, "complete_active_set")).toBe("核心说明已生成 · 偏好已检查");
  });

  it("does not describe an unobserved guidance receipt as a Codex decision", () => {
    expect(guidanceNodeValue(null)).toBe("本轮未形成可核验的读取回执");
  });

  it("keeps missing tool receipts distinct from confirmed non-use", () => {
    expect(historyToolStatus("agent_decides", "recall")).toBe("not_observed");
    expect(historyToolStatus("agent_mcp_recall", "recall")).toBe("observed");
    expect(historyToolStatus("agent_mcp_recall", "research")).toBe("not_observed");
    expect(historyToolStatus("unknown", "read_source", {
      state: "observed",
      by_tool: { read_source: { calls: 1, returned: 2 } },
    })).toBe("window_observed");
    expect(historyToolStatus("agent_decides", "read_scenario_summary", undefined, [
      { tool: "read_scenario_summary", returned_count: 1 },
    ])).toBe("observed");
  });

  it("explains a bound recall with zero returned items instead of saying the receipt is missing", () => {
    expect(historyEmptyStateLabel({
      decision: "needed",
      timeWindowActivity: { by_tool: { recall: { calls: 2, returned: 0 } } },
    })).toContain("已记录 Recall 回执");
    expect(historyEmptyStateLabel({ decision: "unknown" })).toContain("缺少 recall / research / read_source 回执");
  });
  it("describes returned navigation without saying the backend omitted candidate bodies",()=>{
    const label=historyEmptyStateLabel({routeReceipt:{tool_events:[{tool:"user_recall",returned_count:0,source_navigation_returned_count:1}]},timeWindowActivity:{by_tool:{recall:{calls:1,returned:0}}}},true);
    expect(label).toContain("Source navigation was returned");
    expect(label).not.toMatch(/no candidate body|missing|unavailable|empty/i);
  });

  it("never labels a system probe route as an Agent MCP call", () => {
    expect(historyToolStatus("system_probe_recall", "recall")).toBe("not_observed");
  });
});
