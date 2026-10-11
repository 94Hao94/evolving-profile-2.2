import { describe, expect, it } from "vitest";
import * as api from "@/lib/operational-overview";
import type { OperationalIncident, OperationalLane, AttemptGroup } from "@/lib/operational-overview";

const functions = api;
const issue: OperationalIncident = { id: "retention:failed-memories", category: "retention", severity: "critical", title: "失败源记忆", titleKey: "failedMemories", count: 1, detail: "需恢复", at: null, source: "Bank /stats", action: "operations" };
const lane: OperationalLane = { id: "retention", label: "记忆加工", state: "critical", summary: "失败 1 条", detail: "待恢复", incidentCount: 1, lastObservedAt: null, checks: [{ label: "Retain", state: "healthy", detail: "完成", at: null }, { label: "Consolidation", state: "critical", detail: "待处理 100 条", at: null }] };
const attempt: AttemptGroup = { id: "attempt:r1", traceId: "trace-1", operation: "consolidation", scope: "consolidation", errorClass: "timeout", attemptCount: 1, requestIds: ["r1"], at: "2026-10-08T02:00:00Z", recovery: "unresolved", traceComplete: false, laterSuccessId: null };

describe("confirm a specific alert, never recover the underlying memory", () => {
  it("persists confirmation through reload while preserving true critical evidence", () => {
    const frozenLane = Object.freeze(lane);
    const frozenIssue = Object.freeze(issue);
    const ack = functions.acknowledgeOperationalLane?.({}, frozenLane, [frozenIssue], [attempt]);
    const restored = functions.parseOperationalAcknowledgements?.(JSON.stringify(ack));
    expect(functions.isOperationalLaneAcknowledged?.(lane, [issue], [attempt], restored)).toBe(true);
    expect(frozenLane.state).toBe("critical");
    expect(frozenIssue.count).toBe(1);
  });
  it("does not re-alert for polling time or a shrinking pending backlog", () => {
    const ack = functions.acknowledgeOperationalLane?.({}, lane, [issue], [attempt]);
    const next = { ...lane, lastObservedAt: "2026-10-08T03:00:00Z", checks: lane.checks.map(check => ({ ...check, detail: "待处理 90 条", at: "2026-10-08T03:00:00Z" })) };
    expect(functions.isOperationalLaneAcknowledged?.(next, [issue], [attempt], ack)).toBe(true);
  });
  it("re-alerts when the failed source count or a receipt identity changes", () => {
    const ack = functions.acknowledgeOperationalLane?.({}, lane, [issue], [attempt]);
    expect(functions.isOperationalLaneAcknowledged?.(lane, [{ ...issue, count: 2 }], [attempt], ack)).toBe(false);
    expect(functions.isOperationalLaneAcknowledged?.(lane, [issue, { ...issue, id: "operation:new", sourceId: "new" }], [attempt], ack)).toBe(false);
  });
  it("re-alerts on a new consolidation failure even if the aggregate count is still one", () => {
    const ack = functions.acknowledgeOperationalLane?.({}, lane, [issue], [attempt]);
    const retry = { ...attempt, requestIds: ["r1", "r2"], attemptCount: 2 };
    expect(functions.isOperationalLaneAcknowledged?.(lane, [issue], [retry], ack)).toBe(false);
    expect(functions.isOperationalLaneAcknowledged?.(lane, [issue], [], ack)).toBe(true);
  });
  it("clears confirmation after recovery even while pending work keeps the lane amber", () => {
    const ack = functions.acknowledgeOperationalLane?.({}, lane, [issue], [attempt]);
    const pending: OperationalLane = { ...lane, state: "warning", incidentCount: 0, checks: [{ label: "Consolidation", state: "warning", detail: "待处理 90 条", at: null }] };
    const next = functions.reconcileOperationalAcknowledgements?.(ack, [pending], []);
    expect(next).toEqual({});
    expect(functions.isOperationalLaneAcknowledged?.(lane, [issue], [attempt], next)).toBe(false);
  });
  it("never suppresses missing evidence and does not erase confirmation during an outage", () => {
    const ack = functions.acknowledgeOperationalLane?.({}, lane, [issue], [attempt]);
    const unknown: OperationalLane = { ...lane, state: "unknown", checks: [{ label: "Consolidation", state: "unknown", detail: "统计不可用", at: null }] };
    expect(functions.isOperationalLaneAcknowledged?.(unknown, [], [], ack)).toBe(false);
    expect(functions.reconcileOperationalAcknowledgements?.(ack, [unknown], [])).toEqual(ack);
  });
  it("keeps banks separate and fails open on corrupt stored confirmation", () => {
    expect(functions.operationalAcknowledgementStorageKey?.("bank-a")).not.toEqual(functions.operationalAcknowledgementStorageKey?.("bank-b"));
    expect(functions.parseOperationalAcknowledgements?.('not-json')).toEqual({});
    expect(functions.isOperationalLaneAcknowledged?.(lane, [issue], [], functions.parseOperationalAcknowledgements?.('{"retention":true}'))).toBe(false);
  });
});
