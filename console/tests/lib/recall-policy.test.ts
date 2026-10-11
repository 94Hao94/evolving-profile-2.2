import { describe, expect, it } from "vitest";
import { execFileSync } from "node:child_process";
import path from "node:path";
import { effectiveRecallPolicy, normalizeRecallPolicy } from "@/lib/recall-policy";

describe("recall policy configuration provenance", () => {
  it("normalizes independent advanced controls from shared defaults and rejects malformed values", () => {
    expect(normalizeRecallPolicy({}).advanced).toEqual({ allow_transferable_methods: true, allow_background: true, historical_mode: "reference_only", scope_unknown_mode: "keep_navigation", adaptive_enabled: true });
    expect(normalizeRecallPolicy({ advanced: { allow_background: false } }).advanced?.allow_background).toBe(false);
    for (const advanced of [null, [], { allow_background: "false" }, { adaptive_enabled: 1 }, { historical_mode: "all" }, { scope_unknown_mode: "discard" }]) {
      expect(() => normalizeRecallPolicy({ advanced })).toThrow();
    }
  });
  it("reports inherited origin separately from resolved configured level and independent RAG minimum", () => {
    const result = effectiveRecallPolicy({ recall_policy: { default_min_relevance: "strong", user_memory: "inherit", agent_memory: "medium" }, rag: { minimum_relevance: "weak" } });
    expect(result.user_memory).toMatchObject({ effective_level: "strong", configured_level: "strong", configuration_source: "global_default", global_level: "strong", plane_setting: "inherit" });
    expect(result.agent_memory).toMatchObject({ effective_level: "medium", configured_level: "medium", configuration_source: "plane_override", global_level: "strong", plane_setting: "medium" });
    expect(result.external_rag).toMatchObject({ effective_level: "weak", configured_level: "weak", configuration_source: "external_rag", global_level: "strong", plane_setting: "weak" });
  });

  it("matches the Python resolver for legacy, inherited, plane override and RAG settings", () => {
    const fixtures = [ {}, { recall_policy: { default_min_relevance: "strong" } },
      { recall_policy: { default_min_relevance: "medium", user_memory: "strong", agent_memory: "weak" }, rag: { minimum_relevance: "strong" } } ];
    const script = "import json,sys; from lib.recall_relevance import resolve_min_relevance; print(json.dumps([{plane: resolve_min_relevance(settings,plane) for plane in ('user_memory','agent_memory','external_rag')} for settings in json.loads(sys.argv[1])]))";
    const output = execFileSync("python3", ["-c", script, JSON.stringify(fixtures)], { cwd: path.resolve(process.cwd(), "../host-adapter"), encoding: "utf8" });
    const python = JSON.parse(output);
    for (let i = 0; i < fixtures.length; i++) {
      const result = effectiveRecallPolicy(fixtures[i]);
      for (const plane of ["user_memory", "agent_memory", "external_rag"] as const) {
        const expected = Object.fromEntries(["effective_level", "configured_level", "configuration_source", "global_level", "plane_setting", "policy_version"].map((field) => [field, python[i][plane][field]]));
        expect(result[plane]).toEqual(expected);
      }
    }
  });
});
