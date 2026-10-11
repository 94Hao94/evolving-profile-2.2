import { afterEach, describe, expect, it, vi } from "vitest";
vi.mock("@/app/api/evolving-profile/runtime/route", () => ({ GET: async () => Response.json({ services: [] }) }));
vi.mock("@/lib/evolving-client", () => ({ dataplaneBankUrl: (bank: string, path: string) => `http://bank/${bank}${path}`, getDataplaneHeaders: () => ({}) }));
vi.mock("node:fs/promises", () => ({ readFile: async () => { throw new Error("missing"); } }));
import { GET } from "@/app/api/evolving-profile/overview/[bankId]/route";

afterEach(() => vi.unstubAllGlobals());

describe("overview evidence route", () => {
  it("pages failures, reads exact run counts and bank backlog, and reconciles success by trace without returning prompts", async () => {
    const now = Date.now();
    const calls: URL[] = [];
    vi.stubGlobal("fetch", async (value: string) => {
      const url = new URL(value); calls.push(url);
      const q = url.searchParams;
      if (url.pathname.endsWith("/stats")) return Response.json({ failed_consolidation: 8, pending_consolidation: 17130 });
      if (url.pathname.endsWith("/operations")) return Response.json({ operations: [], total: 0 });
      if (q.get("group") === "true") return Response.json({ items: [], total: 41 });
      if (q.get("trace_id")) return Response.json({ total: 1, items: [{ id: "success-a", trace_id: "trace-a", operation: "consolidation", scope: "consolidation", status: "success", started_at: new Date(now - 1000).toISOString(), input: "PRIVATE DOCUMENT BODY" }] });
      if (q.get("status") === "success") return Response.json({ total: 1, items: [{ id: "unrelated", trace_id: "trace-b", operation: "retain", status: "success", started_at: new Date(now - 500).toISOString() }] });
      const offset = Number(q.get("offset") ?? 0);
      const count = Math.min(100, 190 - offset);
      return Response.json({ total: 190, items: Array.from({ length: Math.max(0, count) }, (_, i) => ({ id: `failure-${offset + i}`, trace_id: "trace-a", operation: "consolidation", scope: "consolidation", status: "error", started_at: new Date(now - 5000).toISOString(), error: "ValidationError: Field required", input: "PRIVATE DOCUMENT BODY" })) });
    });
    const response = await GET(new Request("http://console/overview/bank-a"), { params: Promise.resolve({ bankId: "bank-a" }) });
    const body = await response.json();
    expect(body.scan.llmFailures).toMatchObject({ returned: 190, total: 190, coverage: "complete" });
    expect(body.scan.llmFailureGroups.total).toBe(41);
    expect(body.pipeline.failedMemories).toBe(8);
    expect(body.attemptHistory[0].recovery).toBe("later_attempt_succeeded");
    expect(JSON.stringify(body)).not.toContain("PRIVATE DOCUMENT BODY");
    expect(calls.some((url) => url.searchParams.get("trace_id") === "trace-a")).toBe(true);
    expect(calls.every((url) => !url.pathname.includes("/documents"))).toBe(true);
    const traceReads = calls.filter((url) => url.pathname.endsWith("/llm-requests"));
    expect(traceReads.length).toBeGreaterThan(0);
    expect(traceReads.every((url) => url.searchParams.get("include_content") === "false")).toBe(true);
    const counter = traceReads.find((url) => url.searchParams.get("group") === "true");
    expect(counter?.searchParams.get("count_only")).toBe("true");
  });
  it("does not turn a malformed source payload into an observed empty source", async () => {
    vi.stubGlobal("fetch", async () => Response.json({ error: "no usable inventory" }));
    const response = await GET(new Request("http://console/overview/bank-a"), { params: Promise.resolve({ bankId: "bank-a" }) });
    const body = await response.json();
    expect(body.scan.llmFailures.coverage).toBe("unavailable");
    expect(body.pipeline.state).toBe("unknown");
  });
  it("keeps failure-page coverage visible when the latest-request source is unavailable", async () => {
    const at = new Date(Date.now() - 1000).toISOString();
    vi.stubGlobal("fetch", async (value: string) => {
      const url = new URL(value);
      if (url.pathname.endsWith("/stats")) return Response.json({ failed_consolidation: 0, pending_consolidation: 10 });
      if (url.pathname.endsWith("/operations")) return Response.json({ operations: [], total: 0 });
      if (!url.searchParams.has("status")) return new Response(null, { status: 503 });
      if (url.searchParams.get("status") === "success") return Response.json({ items: [], total: 0 });
      return Response.json({ items: [{ id: "failed-request", trace_id: "run-a", operation: "consolidation", status: "error", started_at: at, error: "ValidationError: Field required" }], total: 1 });
    });
    const response = await GET(new Request("http://console/overview/bank-a"), { params: Promise.resolve({ bankId: "bank-a" }) });
    const body = await response.json();
    expect(body.scan.llmFailures.coverage).toBe("complete");
    expect(body.lanes.find((lane: { id: string }) => lane.id === "model").state).toBe("unknown");
    expect(body.attemptHistory).toHaveLength(1);
  });
});
