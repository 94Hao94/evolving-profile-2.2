import { afterEach, describe, expect, it, vi } from "vitest";
import { client, type MentalModel } from "@/lib/api";
import { refreshMentalModelAndWait } from "@/components/mental-model-detail-modal";

const model: MentalModel = {
  id: "model-1", name: "Example", source_query: "example", content: "Before",
  bank_id: "bank-1", max_tokens: 2048,
  tags: [], created_at: "2026-10-01",
  last_refreshed_at: "2026-10-01", trigger: { refresh_after_consolidation: false },
};

afterEach(() => { vi.restoreAllMocks(); vi.useRealTimers(); });

describe("mental model refresh feedback completion", () => {
  it("stays pending until refreshed content arrives", async () => {
    vi.useFakeTimers();
    vi.spyOn(client, "refreshMentalModel").mockResolvedValue({ operation_id: "op-1" });
    vi.spyOn(client, "getMentalModel")
      .mockResolvedValueOnce(model)
      .mockResolvedValueOnce({ ...model, content: "After", last_refreshed_at: "2026-10-02" });
    let finished = false;
    const promise = refreshMentalModelAndWait("bank-1", model, "Refresh timed out")
      .then((result) => { finished = true; return result; });
    await vi.advanceTimersByTimeAsync(1000);
    expect(finished).toBe(false);
    await vi.advanceTimersByTimeAsync(1000);
    await expect(promise).resolves.toMatchObject({ content: "After" });
  });

  it("rejects failed polling so the initiating control shows an error", async () => {
    vi.useFakeTimers();
    vi.spyOn(client, "refreshMentalModel").mockResolvedValue({ operation_id: "op-1" });
    vi.spyOn(client, "getMentalModel").mockRejectedValue(new Error("Read failed"));
    const assertion = expect(refreshMentalModelAndWait("bank-1", model, "Refresh timed out"))
      .rejects.toThrow("Read failed");
    await vi.advanceTimersByTimeAsync(1000);
    await assertion;
  });

  it("rejects a bounded refresh that never completes", async () => {
    vi.useFakeTimers();
    vi.spyOn(client, "refreshMentalModel").mockResolvedValue({ operation_id: "op-1" });
    vi.spyOn(client, "getMentalModel").mockResolvedValue(model);
    const assertion = expect(refreshMentalModelAndWait("bank-1", model, "Refresh timed out"))
      .rejects.toThrow("Refresh timed out");
    await vi.runAllTimersAsync();
    await assertion;
  });
});
