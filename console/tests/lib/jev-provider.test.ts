import { describe, expect, it } from "vitest";
import { normalizeJevJudge } from "@/lib/jev-provider";

describe("independent JEV usage settings", () => {
  it("adds missing usage switches without enabling the master or risk switch", () => {
    expect(normalizeJevJudge({ enabled: false, mode_policy: "off" })).toMatchObject({ enabled: false, mode_policy: "off", scopes: { internal_memory: true, quality_diagnosis: true, external_rag: true }, risk_gate_enabled: false });
  });
  it("preserves explicit choices independently", () => {
    expect(normalizeJevJudge({ scopes: { internal_memory: false, external_rag: false } }).scopes).toEqual({ internal_memory: false, external_rag: false, quality_diagnosis: true });
  });
  it("rejects non-boolean scope values instead of interpreting a false string as on", () => {
    expect(() => normalizeJevJudge({ scopes: { internal_memory: "false" } })).toThrow();
  });
});
