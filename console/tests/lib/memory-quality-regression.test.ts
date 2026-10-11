import { describe, expect, it } from "vitest";
import { compareRegressionRuns, runQualityRegression } from "@/lib/memory-quality-regression";

describe("memory quality regression lab", () => {
  it("covers positive and negative route fixtures without LLM calls", () => {
    const results = runQualityRegression("deep_audit");
    expect(results).toHaveLength(5);
    expect(results.every((result) => result.passed)).toBe(true);
    expect(results.reduce((sum, result) => sum + result.llm_calls, 0)).toBe(0);
  });

  it("compares baseline and current result status", () => {
    const baseline = runQualityRegression("lightweight");
    const current = runQualityRegression("diagnostic");
    expect(compareRegressionRuns(baseline, current).every((item) => item.status_changed === false)).toBe(true);
  });
});
