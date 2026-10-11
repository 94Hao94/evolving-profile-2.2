import { projectMemoryQualityEvents, type MemoryQualityEvent } from "@/lib/memory-quality-event";

export type QualityRegressionCase = {
  id: string;
  label: string;
  kind: "no_call" | "called_empty" | "preference_only" | "history_only" | "mixed";
  prompt: Parameters<typeof projectMemoryQualityEvents>[0];
  expected_statuses: string[];
};

export const QUALITY_REGRESSION_CASES: QualityRegressionCase[] = [
  { id: "no-call", label: "Required route without call with complete observation", kind: "no_call", prompt: { prompt_id: "fixture-no-call", at: "2026-10-03T00:00:00Z", user_prompt: "fixture", memory_route_receipt:{call_coverage:"exact_host_turn_complete"}, history_plan: { recommended_route: "recall", history_dependency: "likely", minimum_action: "recall_probe" } }, expected_statuses: ["not_called"] },
  { id: "called-empty", label: "Bound call returning empty", kind: "called_empty", prompt: { prompt_id: "fixture-called-empty", at: "2026-10-03T00:00:00Z", user_prompt: "fixture", memory_route_receipt: { tool_events: [{ tool: "recall", candidate_count: 4, returned_count: 0 }] } }, expected_statuses: ["returned_zero"] },
  { id: "preference-only", label: "Preference-only delivery", kind: "preference_only", prompt: { prompt_id: "fixture-preference", at: "2026-10-03T00:00:00Z", user_prompt: "fixture", memory_route_receipt: { tool_events: [{ tool: "get_preference", candidate_count: 3, returned_count: 3, delivery: { host_visibility: "observed" } }] } }, expected_statuses: ["delivered"] },
  { id: "history-only", label: "History-only retrieval", kind: "history_only", prompt: { prompt_id: "fixture-history", at: "2026-10-03T00:00:00Z", user_prompt: "fixture", memory_route_receipt: { tool_events: [{ tool: "recall", candidate_count: 5, returned_count: 2 }] } }, expected_statuses: ["returned"] },
  { id: "mixed", label: "Preference and history in parallel", kind: "mixed", prompt: { prompt_id: "fixture-mixed", at: "2026-10-03T00:00:00Z", user_prompt: "fixture", memory_route_receipt: { tool_events: [{ tool: "get_preference", candidate_count: 2, returned_count: 2, delivery: { host_visibility: "observed" } }, { tool: "recall", candidate_count: 6, returned_count: 3 }] } }, expected_statuses: ["delivered", "returned"] },
];

export function runQualityRegression(mode: "lightweight" | "diagnostic" | "deep_audit" = "lightweight") {
  return QUALITY_REGRESSION_CASES.map((testCase) => {
    const events = projectMemoryQualityEvents(testCase.prompt, mode);
    const actual = events.map((event) => event.status);
    return { id: testCase.id, label: testCase.label, kind: testCase.kind, mode, passed: JSON.stringify(actual) === JSON.stringify(testCase.expected_statuses), expected: testCase.expected_statuses, actual, llm_calls: events.reduce((sum, event) => sum + event.llm_calls, 0), events };
  });
}

export function compareRegressionRuns(left: ReturnType<typeof runQualityRegression>, right: ReturnType<typeof runQualityRegression>) {
  const byId = new Map(right.map((item) => [item.id, item]));
  return left.map((item) => ({ id: item.id, baseline_passed: item.passed, current_passed: byId.get(item.id)?.passed ?? false, status_changed: item.passed !== byId.get(item.id)?.passed }));
}

export function flattenRegressionEvents(results: ReturnType<typeof runQualityRegression>): MemoryQualityEvent[] {
  return results.flatMap((result) => result.events);
}
