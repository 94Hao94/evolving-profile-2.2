import { describe, expect, it } from "vitest";
import { projectToolBranches } from "@/lib/topology-mapping";

describe("Prompt-bound relevance audit projection", () => {
  it("retains the policy actually used by each tool, not the latest UI setting", () => {
    const map = projectToolBranches([
      { tool: "agent_recall", tool_call_id: "a", candidate_count: 6, returned_count: 2,
        relevance_audit: { effective_level: "medium", kept_count: 2, excluded_count: 4 } },
      { tool: "user_recall", tool_call_id: "u", candidate_count: 3, returned_count: 1,
        relevance_audit: { effective_level: "strong", kept_count: 1, excluded_count: 2 } },
    ], false);
    expect(map.get("agent-process-memory-process-retrieval")?.receipts[0]).toMatchObject({
      relevance_audit: { effective_level: "medium", kept_count: 2, excluded_count: 4 },
    });
    expect(map.get("user-memory-recall")?.receipts[0]).toMatchObject({
      relevance_audit: { effective_level: "strong", kept_count: 1, excluded_count: 2 },
    });
  });
});
