import { describe, expect, it } from "vitest";
import { createActionFeedback } from "@/lib/action-feedback";

describe("async action feedback", () => {
  it("locks immediately and exposes pending until the actual action completes", async () => {
    const feedback = createActionFeedback();
    let complete!: () => void;
    let calls = 0;
    const action = () => {
      calls += 1;
      return new Promise<void>((resolve) => {
        complete = resolve;
      });
    };
    const first = feedback.run(action);
    expect(feedback.getSnapshot()).toEqual({ status: "pending", error: null });
    expect(await feedback.run(action)).toBe(false);
    expect(calls).toBe(1);
    complete();
    expect(await first).toBe(true);
    expect(feedback.getSnapshot()).toEqual({ status: "success", error: null });
  });

  it("exposes a rejected error beside the action and allows a successful retry", async () => {
    const feedback = createActionFeedback();
    expect(
      await feedback.run(async () => {
        throw new Error("Provider is unavailable");
      })
    ).toBe(false);
    expect(feedback.getSnapshot()).toEqual({ status: "error", error: "Provider is unavailable" });
    expect(await feedback.run(async () => "saved")).toBe(true);
    expect(feedback.getSnapshot()).toEqual({ status: "success", error: null });
  });

  it("handles synchronous throws without leaving the action busy", async () => {
    const feedback = createActionFeedback();
    expect(
      await feedback.run(() => {
        throw new Error("Invalid endpoint");
      })
    ).toBe(false);
    expect(feedback.getSnapshot()).toEqual({ status: "error", error: "Invalid endpoint" });
  });

  it("does not report success when the caller cancels", async () => {
    const feedback = createActionFeedback();
    expect(await feedback.run(async () => false)).toBe(false);
    expect(feedback.getSnapshot()).toEqual({ status: "idle", error: null });
  });

  it("cannot unlock an in-flight action by resetting its feedback", async () => {
    const feedback = createActionFeedback();
    let complete!: () => void;
    const pending = feedback.run(
      () =>
        new Promise<void>((resolve) => {
          complete = resolve;
        })
    );
    feedback.reset();
    expect(feedback.getSnapshot().status).toBe("pending");
    expect(await feedback.run(async () => true)).toBe(false);
    complete();
    await pending;
    feedback.reset();
    expect(feedback.getSnapshot()).toEqual({ status: "idle", error: null });
  });

  it("does not publish stale success when inputs change during the request", async () => {
    const feedback = createActionFeedback();
    let complete!: () => void;
    const pending = feedback.run(
      () =>
        new Promise<void>((resolve) => {
          complete = resolve;
        })
    );
    feedback.reset();
    expect(feedback.getSnapshot()).toEqual({ status: "pending", error: null });
    complete();
    expect(await pending).toBe(true);
    expect(feedback.getSnapshot()).toEqual({ status: "idle", error: null });
    expect(await feedback.run(async () => true)).toBe(true);
    expect(feedback.getSnapshot()).toEqual({ status: "success", error: null });
  });

  it("still reports a failed request after inputs change while it is pending", async () => {
    const feedback = createActionFeedback();
    let reject!: (reason: Error) => void;
    const pending = feedback.run(
      () =>
        new Promise<void>((_resolve, rejectRequest) => {
          reject = rejectRequest;
        })
    );
    feedback.reset();
    reject(new Error("Save rejected"));
    expect(await pending).toBe(false);
    expect(feedback.getSnapshot()).toEqual({ status: "error", error: "Save rejected" });
  });

  it("notifies subscribers with each new state and supports unsubscribing", async () => {
    const feedback = createActionFeedback();
    const states: string[] = [];
    const unsubscribe = feedback.subscribe(() => states.push(feedback.getSnapshot().status));
    await feedback.run(async () => true);
    unsubscribe();
    feedback.reset();
    expect(states).toEqual(["pending", "success"]);
  });

  it("preserves string errors while leaving unknown errors to the localized fallback", async () => {
    const feedback = createActionFeedback();
    await feedback.run(async () => {
      throw "Request timed out";
    });
    expect(feedback.getSnapshot()).toEqual({ status: "error", error: "Request timed out" });
    await feedback.run(async () => {
      throw null;
    });
    expect(feedback.getSnapshot()).toEqual({ status: "error", error: null });
  });
});
