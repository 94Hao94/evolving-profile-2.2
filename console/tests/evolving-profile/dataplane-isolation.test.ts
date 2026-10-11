import { afterEach, beforeEach, expect, it, vi } from "vitest";
const network = vi.fn(async (_request: any) => new Response("{}", { status: 200, headers: { "content-type": "application/json" } }));
beforeEach(() => {
  vi.stubEnv("EVOLVING_PROFILE_STATE_ROOT", "/tmp/isolated-ep-dataplane");
  vi.stubEnv("EVOLVING_PROFILE_DATAPLANE_API_URL", "");
  vi.stubEnv("NEXT_RUNTIME", "nodejs");
  vi.stubGlobal("fetch", network); network.mockClear(); vi.resetModules();
});
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllEnvs(); vi.unstubAllGlobals(); });
it("rejects isolated bank PATCH and DELETE before a production request can be made", async () => {
  const { PATCH, DELETE } = await import("@/app/api/banks/[bankId]/route");
  const context = { params: Promise.resolve({ bankId: "synthetic-bank" }) };
  const response = await PATCH(new Request("http://localhost/api/banks/synthetic-bank", { method: "PATCH", body: JSON.stringify({ name: "synthetic" }) }), context);
  expect(response.status).toBe(503);
  expect(await response.json()).toMatchObject({ error: "dataplane_endpoint_unavailable" });
  expect((await DELETE(new Request("http://localhost/api/banks/synthetic-bank", { method: "DELETE" }), context)).status).toBe(503);
  expect(network).not.toHaveBeenCalled();
});
it("rejects isolated direct and high-level clients without a configured endpoint", async () => {
  const { dataplaneBankUrl, evolvingProfileClient } = await import("@/lib/evolving-client");
  expect(() => dataplaneBankUrl("synthetic-bank")).toThrow("dataplane_endpoint_unavailable");
  await expect(evolvingProfileClient.listMemories("synthetic-bank")).rejects.toThrow("dataplane_endpoint_unavailable");
  expect(network).not.toHaveBeenCalled();
});
it("reports an unconfigured isolated health endpoint instead of a fallback service address", async () => {
  const { GET } = await import("@/app/api/health/route");
  const response = await GET();
  expect((await response.json()).dataplane).toMatchObject({ status: "disconnected", url: "", error: "dataplane_endpoint_unavailable" });
  expect(network).not.toHaveBeenCalled();
});
it("does not advertise a fallback dataplane at startup for an isolated root", async () => {
  const logs = vi.spyOn(console, "log").mockImplementation(() => undefined);
  const { register } = await import("@/instrumentation");
  await register();
  expect(logs.mock.calls.flat().join(" ")).toContain("unconfigured");
  expect(logs.mock.calls.flat().join(" ")).not.toContain("8888");
});
it("keeps Node-only dataplane startup registration outside the Edge runtime", async () => {
  vi.stubEnv("NEXT_RUNTIME", "edge");
  const logs = vi.spyOn(console, "log").mockImplementation(() => undefined);
  await (await import("@/instrumentation")).register();
  expect(logs).not.toHaveBeenCalled();
});
it("uses an explicit isolated endpoint for bank updates", async () => {
  vi.stubEnv("EVOLVING_PROFILE_DATAPLANE_API_URL", "http://127.0.0.1:18088");
  const { PATCH } = await import("@/app/api/banks/[bankId]/route");
  expect((await PATCH(new Request("http://localhost/api/banks/synthetic-bank", { method: "PATCH", body: "{}" }), { params: Promise.resolve({ bankId: "synthetic-bank" }) })).status).toBe(200);
  expect(network.mock.calls[0][0].url).toBe("http://127.0.0.1:18088/v1/default/banks/synthetic-bank");
});
it("preserves the default installation endpoint for its managed state root", async () => {
  vi.stubEnv("EVOLVING_PROFILE_STATE_ROOT", "");
  const { dataplaneBankUrl } = await import("@/lib/evolving-client");
  expect(dataplaneBankUrl("synthetic-bank")).toBe("http://127.0.0.1:12088/v1/default/banks/synthetic-bank");
});
