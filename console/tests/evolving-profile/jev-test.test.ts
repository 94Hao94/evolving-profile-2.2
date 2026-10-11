import { afterEach, describe, expect, it, vi } from "vitest";
import { POST } from "@/app/api/evolving-profile/jev-test/route";

function request() {
  return new Request("http://localhost/api/evolving-profile/jev-test", {
    method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ api_key: "test-fixture-not-a-real-key", locale: "en" }),
  });
}

afterEach(() => vi.unstubAllGlobals());

describe("JEV connection test evidence", () => {
  it("sends authentication only to the official TypeSafe endpoint even if the caller submits an old address", async () => {
    vi.stubGlobal("fetch", async (url: string | URL | Request, init?: RequestInit) => {
      if (String(url) !== "https://api.typesafe.ai/v1/systemone") return new Response("wrong credential issuer", { status: 401 });
      const payload = JSON.parse(String(init?.body));
      if (payload.model !== "jev-latest" || !payload.state || payload.questions?.ok?.type !== "noul") return new Response("invalid request", { status: 422 });
      return Response.json({ model: "jev-1.13.0", answers: { ok: { type: "noul", noul: 0.97 } }, usage: { input_tokens: 20, output_tokens: 2 } });
    });
    const response = await POST(new Request("http://localhost/api/evolving-profile/jev-test", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ api_key: "fixture-key", base_url: "https://jevmodel.org/v1/systemone" }),
    }));
    expect(await response.json()).toMatchObject({ ok: true, response_verified: true, model: "jev-1.13.0" });
  });

  it("does not claim connected for an HTTP 200 HTML page", async () => {
    vi.stubGlobal("fetch", async () => new Response("<html>login</html>", { status: 200 }));
    const response = await POST(request());
    const result = await response.json();
    expect(result.ok).toBe(false);
    expect(result.code).toBe("invalid_response");
  });

  it("does not claim connected for JSON without a valid decision", async () => {
    vi.stubGlobal("fetch", async () => Response.json({ status: "ok" }));
    const result = await (await POST(request())).json();
    expect(result.ok).toBe(false);
    expect(result.code).toBe("invalid_response");
  });

  it("returns verified success, duration and usage for a valid decision", async () => {
    vi.stubGlobal("fetch", async () => Response.json({ model: "jev-latest", answers: { ok: { type: "noul", noul: 0.99 } }, usage: { input_tokens: 12, output_tokens: 2 } }));
    const result = await (await POST(request())).json();
    expect(result).toMatchObject({ ok: true, code: "connected", response_verified: true, usage: { input_tokens: 12, output_tokens: 2 } });
    expect(result.latency_ms).toBeGreaterThanOrEqual(0);
  });

  it("distinguishes provider authentication failures from network failures", async () => {
    vi.stubGlobal("fetch", async () => new Response("unauthorized", { status: 401 }));
    const result = await (await POST(request())).json();
    expect(result).toMatchObject({ ok: false, code: "authentication_failed", status: 401 });
  });

  it("reports timeouts as timeout rather than an ambiguous failure", async () => {
    vi.stubGlobal("fetch", async () => { throw new DOMException("timeout", "TimeoutError"); });
    const result = await (await POST(request())).json();
    expect(result).toMatchObject({ ok: false, code: "timeout" });
  });
});
