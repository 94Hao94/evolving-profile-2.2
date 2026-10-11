import { describe, expect, it, vi } from "vitest";
import {
  buildRecoverySelection,
  createRecoveryClient,
  isRecoveryActive,
  recoveryErrorMessageKey,
} from "@/lib/memory-recovery";

describe("recovery request boundaries", () => {
  it("preserves a recoverable worker interruption without exposing process logs", async () => {
    const client = createRecoveryClient(async () =>
      Response.json(
        { error_code: "recovery_worker_interrupted", detail: "private worker logs" },
        { status: 503 }
      )
    );
    await expect(client.status("bank-a")).rejects.toThrow("recovery_worker_interrupted");
    expect(recoveryErrorMessageKey("recovery_worker_interrupted")).toBe("errors.workerInterrupted");
  });
  it("requests a fresh preview when the saved source cohort has changed", () => {
    expect(recoveryErrorMessageKey("plan_source_changed")).toBe("errors.planUnavailable");
  });
  it("interprets datetime-local values in the browser timezone before serialization", () => {
    vi.stubEnv("TZ", "Asia/Shanghai");
    try {
      expect(
        buildRecoverySelection(
          "bank-a",
          "date_range",
          ["facts"],
          "2026-10-08T09:30",
          "2026-10-08T11:30",
          ""
        )
      ).toMatchObject({ from: "2026-10-08T01:30:00.000Z", to: "2026-10-08T03:30:00.000Z" });
    } finally {
      vi.unstubAllEnvs();
    }
  });
  it("serializes explicit offsets to UTC without changing Bank, dimensions or sessions", () => {
    expect(
      buildRecoverySelection(
        "bank-a",
        "date_range",
        ["facts", "experiences"],
        "2026-10-08T09:30:00+08:00",
        "2026-10-08T11:30:00+08:00",
        "session-a, session-b"
      )
    ).toEqual({
      action: "preview",
      bank_id: "bank-a",
      mode: "date_range",
      dimensions: ["facts", "experiences"],
      from: "2026-10-08T01:30:00.000Z",
      to: "2026-10-08T03:30:00.000Z",
      session_ids: ["session-a", "session-b"],
    });
  });
  it("compares actual instants rather than local timestamp strings across offsets", () => {
    expect(() =>
      buildRecoverySelection(
        "bank-a",
        "date_range",
        ["facts"],
        "2026-10-08T01:00:00+08:00",
        "2026-10-07T20:00:00Z",
        ""
      )
    ).not.toThrow();
    expect(() =>
      buildRecoverySelection(
        "bank-a",
        "date_range",
        ["facts"],
        "2026-10-08T10:00:00Z",
        "2026-10-08T11:00:00+08:00",
        ""
      )
    ).toThrow("invalid_date_range");
  });
  it.each([{ error_code: "plan_not_found" }, { error: { code: "plan_not_found" } }])(
    "preserves a known public error code from either bridge shape",
    async (payload) => {
      const client = createRecoveryClient(async () => Response.json(payload, { status: 400 }));
      await expect(client.status("bank-a")).rejects.toThrow("plan_not_found");
      expect(recoveryErrorMessageKey("plan_not_found")).toBe("errors.planUnavailable");
    }
  );
  it("maps known provider failures to safe explanations and drops arbitrary code values", async () => {
    expect(recoveryErrorMessageKey("provider_unavailable")).toBe("errors.providerUnavailable");
    const client = createRecoveryClient(async () =>
      Response.json(
        {
          error_code: "sk-private-provider-credential",
          error: { code: "<script>secret</script>" },
        },
        { status: 500 }
      )
    );
    await expect(client.status("bank-a")).rejects.toThrow("recovery_request_failed");
    expect(recoveryErrorMessageKey("<script>secret</script>")).toBe("requestFailed");
  });
  it("defaults to pending work and preserves explicit session scope", () => {
    expect(
      buildRecoverySelection(
        "bank-a",
        "pending",
        ["facts"],
        "",
        "",
        "session-a, session-b\nsession-a"
      )
    ).toEqual({
      action: "preview",
      bank_id: "bank-a",
      mode: "pending",
      dimensions: ["facts"],
      from: null,
      to: null,
      session_ids: ["session-a", "session-b"],
    });
  });
  it("rejects inverted or incomplete dates before contacting a provider", () => {
    expect(() =>
      buildRecoverySelection("bank-a", "date_range", [], "2026-10-08", "2026-10-01", "")
    ).toThrow("invalid_date_range");
    expect(() => buildRecoverySelection("bank-a", "date_range", [], "2026-10-08", "", "")).toThrow(
      "invalid_date_range"
    );
  });
  it("reads durable status with GET and aborts superseded Bank requests", async () => {
    let signal: AbortSignal | undefined;
    const fetcher: typeof fetch = async (url, init) => {
      expect(String(url)).toContain("bank_id=bank-a");
      expect(init?.method).toBe("GET");
      expect(init?.cache).toBe("no-store");
      signal = init?.signal as AbortSignal;
      return new Promise((_, reject) =>
        signal?.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")))
      );
    };
    const client = createRecoveryClient(fetcher);
    const request = client.status("bank-a");
    client.abort();
    await expect(request).rejects.toMatchObject({ name: "AbortError" });
    expect(signal?.aborted).toBe(true);
  });
  it("returns accepted queued jobs without reporting completion and preserves idempotency", async () => {
    const client = createRecoveryClient(async (_url, init) => {
      expect(JSON.parse(String(init?.body))).toEqual({
        action: "start",
        bank_id: "bank-a",
        plan_id: "plan-a",
        idempotency_key: "same-key",
      });
      return Response.json({ job: { job_id: "job-a", bank_id: "bank-a", status: "queued" } });
    });
    const result = await client.action({
      action: "start",
      bank_id: "bank-a",
      plan_id: "plan-a",
      idempotency_key: "same-key",
    });
    expect(result.job?.status).toBe("queued");
    expect(isRecoveryActive(result.job?.status)).toBe(true);
    expect(isRecoveryActive("partial")).toBe(false);
    expect(isRecoveryActive("complete")).toBe(false);
  });
  it("does not expose provider response bodies on request failure", async () => {
    const client = createRecoveryClient(async () =>
      Response.json({ error: "secret provider traceback" }, { status: 500 })
    );
    await expect(client.status("bank-a")).rejects.toThrow("recovery_request_failed");
  });
  it("rejects a status response for a different Bank", async () => {
    const client = createRecoveryClient(async () =>
      Response.json({ job: { job_id: "job-b", bank_id: "bank-b", status: "running" } })
    );
    await expect(client.status("bank-a")).rejects.toThrow("recovery_scope_mismatch");
  });
  it("rejects delayed status even if the transport ignores cancellation", async () => {
    let finish!: (response: Response) => void;
    const client = createRecoveryClient(
      async () =>
        new Promise((resolve) => {
          finish = resolve;
        })
    );
    const request = client.status("bank-a");
    client.abort();
    finish(Response.json({ job: { bank_id: "bank-a", status: "running" } }));
    await expect(request).rejects.toMatchObject({ name: "AbortError" });
  });
});
