import { describe, expect, it } from "vitest";
import { buildOperationalOverview, classifyOperationalError } from "@/lib/operational-overview";

const now = Date.parse("2026-09-20T12:00:00Z");
const runtime = {
  model: { provider: "openai", model: "qwen3.7-plus", apiKey: "configured" },
  services: [{ name: "EP API", status: "healthy", detail: "ok", url: "http://localhost" }],
  backup: { local: { status: "healthy", setCount: 2, latestAt: "2026-09-20T03:25:00Z", latestBytes: 1_500_000_000, latestVerified: true, artifacts: { database: true, config: true, capture: true } }, job: { loaded: true, lastExitCode: 0, schedule: "每日 03:25" }, cloud: { status: "unverified", mirrorSetCount: 0, latestAt: null, ageHours: null } },
};

describe("operational overview", () => {
  it.each([
    ["6 validation errors for ConsolidationResponse\ndeletes.0.observation_id\n Field required [type=missing, input_value={'id': 'obs-1'}]", "schema_validation"],
    ["JSONDecodeError: Expecting ',' delimiter", "invalid_json"],
    ["1 validation error for ConsolidationResponse\nInvalid JSON: EOF while parsing [type=json_invalid]", "invalid_json"],
    ["finish_reason=length; max_completion_tokens reached", "output_truncated"],
    ["AuthenticationError: incorrect API key (401)", "authentication"],
  ])("classifies actual string errors without assigning schema failures to API keys", (error, expected) => {
    expect(classifyOperationalError(error)).toBe(expected);
  });

  const evidence = { now, runtime, backupEvents: [], map: { status: "ready", checked_at: "2026-09-20T11:59:00Z" }, operations: { status: "observed" as const, items: [] } };
  const failed = { id: "fail-1", trace_id: "run-a", scope: "consolidation", operation: "consolidation", status: "error", started_at: "2026-09-20T10:00:00Z", error: "ValidationError: deletes.0.observation_id Field required" };
  const success = { id: "success-1", trace_id: "run-a", scope: "consolidation", operation: "consolidation", status: "success", started_at: "2026-09-20T11:00:00Z" };

  it("groups failed attempts by trace and class, preserves locators and exact sample coverage", () => {
    const view = buildOperationalOverview({ ...evidence, llm: { status: "observed", total: 190, groupTotal: 41, items: [failed, { ...failed, id: "fail-2" }, success] } });
    expect(view.attemptHistory).toHaveLength(1);
    expect(view.attemptHistory[0]).toMatchObject({ attemptCount: 2, requestIds: ["fail-1", "fail-2"], errorClass: "schema_validation", recovery: "later_attempt_succeeded", traceComplete: false });
    expect(view.scan.llmFailures).toMatchObject({ returned: 2, total: 190, coverage: "partial" });
    expect(view.scan.llmFailureGroups.total).toBe(41);
    expect(view.lanes.find((lane) => lane.id === "model")?.state).toBe("healthy");
  });

  it("does not recover a trace using unrelated or differently scoped success", () => {
    const view = buildOperationalOverview({ ...evidence, llm: { status: "observed", items: [failed, { ...success, trace_id: "run-b" }, { ...success, id: "other-scope", scope: "reflect" }] } });
    expect(view.attemptHistory[0].recovery).toBe("unresolved");
    expect(view.attemptHistory[0].traceComplete).toBe(false);
  });

  it("keeps recovery unknown when trace IDs or success evidence are missing", () => {
    const view = buildOperationalOverview({ ...evidence, llm: { status: "observed", relatedSuccessStatus: "unavailable", items: [{ ...failed, trace_id: null }, success] } });
    expect(view.attemptHistory[0].recovery).toBe("unknown");
    expect(view.scan.relatedSuccess.coverage).toBe("unavailable");
  });

  it("keeps failed and pending source memories visible after an unrelated recent success", () => {
    const view = buildOperationalOverview({ ...evidence, bankStats: { status: "observed", failed_consolidation: 8, pending_consolidation: 17130, operations_by_status: { processing: 2, pending: 3 } }, llm: { status: "observed", items: [failed, { ...success, trace_id: "run-b" }] } });
    expect(view.pipeline).toMatchObject({ failedMemories: 8, pendingMemories: 17130, processingOperations: 2, queuedOperations: 3, state: "critical" });
    expect(view.lanes.find((lane) => lane.id === "retention")?.state).toBe("critical");
    expect(view.incidents.find((issue) => issue.id === "retention:failed-memories")?.count).toBe(8);
  });
  it("summarizes the pending backlog instead of claiming a fresh retain receipt is unverified", () => {
    const view = buildOperationalOverview({ ...evidence,
      operations: { status: "observed", items: [{ id: "fresh-retain", task_type: "retain", status: "completed", created_at: "2026-09-20T11:36:00Z" }] },
      bankStats: { status: "observed", failed_consolidation: 0, pending_consolidation: 20 },
      llm: { status: "observed", items: [success] },
    });
    const lane = view.lanes.find((entry) => entry.id === "retention");
    expect(lane?.state).toBe("warning");
    expect(lane?.summary).toBe("待处理 20 条源记忆");
    expect(lane).toMatchObject({ summaryKey: "pendingMemories", summaryCount: 20 });
    expect(lane?.detail).toContain("fresh-retain");
    expect(lane?.summary).not.toContain("未核验");
  });

  it("does not claim a complete audit when totals or bank statistics are unavailable", () => {
    const view = buildOperationalOverview({ ...evidence, llm: { status: "observed", items: [failed], total: null }, bankStats: { status: "unavailable" } });
    expect(view.scan.llmFailures.coverage).toBe("unknown");
    expect(view.pipeline.state).toBe("unknown");
    expect(view.sourceCoverage.sourceAudit).toBe("not_performed");
  });
  it("keeps raw validation payloads and credentials out of failed operation summaries", () => {
    const view = buildOperationalOverview({ ...evidence, operations: { status: "observed", items: [{ id: "op-private", task_type: "consolidation", status: "failed", created_at: "2026-09-20T11:00:00Z", error_message: "ValidationError input_value={'text':'PRIVATE SOURCE BODY', 'token':'SECRET'}" }] }, llm: { status: "observed", items: [] } });
    expect(JSON.stringify(view)).not.toContain("PRIVATE SOURCE BODY");
    expect(JSON.stringify(view)).not.toContain("SECRET");
    expect(view.incidents.find((issue) => issue.sourceId === "op-private")?.detailKey).toBe("advice.schema_validation");
  });
  it("shows yesterday's failed backup even when a later backup succeeded", () => {
    const view = buildOperationalOverview({ now, runtime, backupEvents: [
      { at: "2026-09-19T15:00:00Z", status: "failed", code: "backup_exit_1", detail: "数据库导出失败" },
      { at: "2026-09-20T03:25:00Z", status: "completed", code: "backup_ok", detail: "已完成" },
    ], llm: { status: "observed", items: [] }, operations: { status: "observed", items: [] }, map: { status: "ready", checked_at: "2026-09-20T11:59:00Z" } });
    expect(view.incidents.some((issue) => issue.category === "backup" && issue.detail.includes("数据库导出失败"))).toBe(true);
    expect(view.incidents.find((issue) => issue.category === "backup")?.at).toBe("2026-09-19T15:00:00Z");
  });

  it("does not call a configured API key connected without a successful request", () => {
    const view = buildOperationalOverview({ now, runtime, backupEvents: [], llm: { status: "unavailable", items: [] }, operations: { status: "observed", items: [] }, map: { status: "ready", checked_at: "2026-09-20T11:59:00Z" } });
    expect(view.lanes.find((lane) => lane.id === "model")?.state).toBe("unknown");
    expect(view.lanes.find((lane) => lane.id === "model")?.summary).toMatch(/未核验/);
  });

  it("reports recent retain and model errors with exact trace locators but no secret", () => {
    const view = buildOperationalOverview({ now, runtime, backupEvents: [], map: { status: "ready", checked_at: "2026-09-20T11:59:00Z" }, llm: { status: "observed", items: [{ id: "trace-1", operation: "retain", status: "error", started_at: "2026-09-20T10:00:00Z", error: { code: "auth_failed", message: "Bearer SECRET" } }] }, operations: { status: "observed", items: [{ id: "op-1", task_type: "retain", status: "failed", created_at: "2026-09-20T10:01:00Z", error_message: "retain timeout" }] } });
    expect(view.incidents.map((issue) => issue.sourceId)).toEqual(expect.arrayContaining(["trace-1", "op-1"]));
    expect(JSON.stringify(view)).not.toContain("SECRET");
  });

  it("distinguishes stale and unobserved sources from healthy", () => {
    const view = buildOperationalOverview({ now, runtime, backupEvents: [], map: { status: "failed", checked_at: "2026-09-20T10:00:00Z", error_type: "TimeoutError" }, llm: { status: "observed", items: [] }, operations: { status: "unavailable", items: [] } });
    expect(view.lanes.find((lane) => lane.id === "map")?.state).toBe("warning");
    expect(view.lanes.find((lane) => lane.id === "retention")?.state).toBe("unknown");
    expect(view.overall).not.toBe("healthy");
  });

  it("marks a successful directory check stale when it stops updating", () => {
    const view = buildOperationalOverview({ now, runtime, backupEvents: [], map: { status: "ready", checked_at: "2026-09-20T11:30:00Z", stale_after_seconds: 180, semantic_status: "pending_or_stale" }, llm: { status: "observed", items: [] }, operations: { status: "observed", items: [] } });
    expect(view.incidents.some((issue) => issue.category === "map" && issue.detail.includes("过期"))).toBe(true);
  });

  it("surfaces a failed semantic refresh while keeping the structure map available", () => {
    const view = buildOperationalOverview({ now, runtime, backupEvents: [], map: { status: "ready", checked_at: "2026-09-20T11:59:00Z", semantic_status: "fresh_with_pending_changes", semantic_worker_status: "failed", semantic_error_type: "ValueError" }, llm: { status: "observed", items: [] }, operations: { status: "observed", items: [] } });
    expect(view.incidents.some((issue) => issue.category === "map" && issue.title === "语义地图加工失败")).toBe(true);
    expect(view.incidents.find((issue) => issue.title === "语义地图加工失败")?.detail).toContain("结构目录仍可用");
  });

  it("splits local and cloud backup status and exposes concrete success evidence", () => {
    const view = buildOperationalOverview({ now, runtime, backupEvents: [], map: { status: "ready", checked_at: "2026-09-20T11:59:00Z", semantic_status: "ready", semantic_worker_status: "published" }, llm: { status: "observed", items: [{ id: "llm-ok", operation: "retain", status: "success", started_at: "2026-09-20T11:40:00Z" }] }, operations: { status: "observed", items: [{ id: "retain-ok", task_type: "retain", status: "completed", created_at: "2026-09-20T11:30:00Z" }] } });
    const backup = view.lanes.find((lane) => lane.id === "backup");
    expect(backup?.checks.map((check) => [check.label, check.state])).toEqual([["本地", "healthy"], ["云端", "warning"]]);
    expect(backup?.checks[0].detail).toContain("2 套");
    expect(view.lanes.find((lane) => lane.id === "model")?.detail).toContain("qwen3.7-plus");
    expect(view.lanes.find((lane) => lane.id === "retention")?.detail).toContain("retain-ok");
    expect(view.lanes.find((lane) => lane.id === "map")?.detail).toContain("19:59");
  });
});
