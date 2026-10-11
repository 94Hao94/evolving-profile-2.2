import type { ReactElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ActionButton } from "@/components/ui/action-button";
import { MemoryRecoveryDialog, RecoveryProgress } from "@/components/memory-recovery-dialog";

// Node-only tests run the real control handlers and effects. Replace only the
// React subscription bridge, not the recovery client, actions, or rendering tree.
const harness = vi.hoisted(() => ({
  values: [] as any[],
  refs: [] as any[],
  stateIndex: 0,
  refIndex: 0,
  effects: [] as Array<() => (() => void) | void>,
  fetch: vi.fn(),
}));
vi.mock("react", async (original) => ({
  ...(await original<typeof import("react")>()),
  useState: (initial: any) => {
    const index = harness.stateIndex++;
    if (!(index in harness.values))
      harness.values[index] = typeof initial === "function" ? initial() : initial;
    return [
      harness.values[index],
      (value: any) => {
        harness.values[index] = typeof value === "function" ? value(harness.values[index]) : value;
      },
    ];
  },
  useRef: (initial: any) => {
    const index = harness.refIndex++;
    harness.refs[index] ??= { current: initial };
    return harness.refs[index];
  },
  useEffect: (effect: () => (() => void) | void) => {
    harness.effects.push(effect);
  },
}));
vi.mock("next-intl", () => ({
  useTranslations: () => Object.assign((key: string) => key, { has: () => true }),
}));

function find(
  node: unknown,
  predicate: (node: ReactElement<Record<string, any>>) => boolean
): ReactElement<Record<string, any>> | undefined {
  if (Array.isArray(node)) {
    for (const child of node) {
      const match = find(child, predicate);
      if (match) return match;
    }
  } else if (node && typeof node === "object") {
    const element = node as ReactElement<Record<string, any>>;
    if (predicate(element)) return element;
    return find(element.props?.children, predicate);
  }
}
function renderControls() {
  harness.stateIndex = 0;
  harness.refIndex = 0;
  harness.effects = [];
  const dialog = MemoryRecoveryDialog({
    bankId: "bank-a",
    open: true,
    onOpenChange: () => undefined,
  });
  const controls = find(
    dialog,
    (node) => typeof node.type === "function" && node.props?.bankId === "bank-a"
  )!;
  return (controls.type as (props: any) => ReactElement)(controls.props);
}
const button = (tree: ReactElement, name: string) =>
  find(tree, (node) => node.type === ActionButton && node.props.children === name)!.props;
const job = {
  job_id: "job-a",
  bank_id: "bank-a",
  status: "queued",
  stages: [],
  created_at: "now",
  updated_at: "now",
  can_resume: false,
  cancel_requested: false,
};
const preview = {
  plan_id: "plan-a",
  bank_id: "bank-a",
  source_revision: "rev",
  mode: "pending",
  dimensions: [],
  estimated_tokens: null,
  requires_provider: true,
};
let cleanup: (() => void) | undefined;
beforeEach(() => {
  harness.values = [];
  harness.refs = [];
  vi.clearAllMocks();
  vi.stubGlobal("fetch", harness.fetch);
  vi.useFakeTimers();
});
afterEach(() => {
  cleanup?.();
  cleanup = undefined;
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("recovery control actions", () => {
  it("uses the localized stale-plan explanation on a start failure", async () => {
    harness.fetch
      .mockResolvedValueOnce(Response.json({ preview }))
      .mockResolvedValueOnce(Response.json({ error_code: "plan_not_found" }, { status: 400 }));
    await button(renderControls(), "preview").onAction();
    await expect(button(renderControls(), "start").onAction()).rejects.toThrow(
      "errors.planUnavailable"
    );
  });
  it("updates accepted button feedback only when durable completion arrives", async () => {
    harness.fetch.mockResolvedValue(Response.json({ job: { ...job, status: "complete" } }));
    renderControls();
    cleanup = harness.effects[0]() as () => void;
    await vi.advanceTimersByTimeAsync(1);
    expect(button(renderControls(), "start").successLabel).toBe("statuses.complete");
  });
  it("reads status only when opened and aborts the read when closed", async () => {
    let signal!: AbortSignal;
    harness.fetch.mockImplementation((_url, init) => {
      signal = init.signal;
      return new Promise((_resolve, reject) =>
        signal.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")))
      );
    });
    const tree = renderControls();
    expect(button(tree, "start").disabled).toBe(true);
    cleanup = harness.effects[0]() as () => void;
    expect(harness.fetch.mock.calls[0][1].method).toBe("GET");
    cleanup();
    expect(signal.aborted).toBe(true);
    await vi.advanceTimersByTimeAsync(3000);
    expect(harness.fetch).toHaveBeenCalledTimes(1);
  });
  it("reuses the operation key after response loss and shows queued rather than complete", async () => {
    harness.fetch
      .mockResolvedValueOnce(Response.json({ preview }))
      .mockRejectedValueOnce(new Error("response lost"))
      .mockResolvedValueOnce(Response.json({ job }));
    await button(renderControls(), "preview").onAction();
    const start = button(renderControls(), "start");
    expect(start.disabled).toBe(false);
    await expect(start.onAction()).rejects.toThrow("requestFailed");
    await button(renderControls(), "start").onAction();
    const requests = harness.fetch.mock.calls
      .filter((call) => call[1]?.method === "POST")
      .map((call) => JSON.parse(call[1].body));
    expect(requests[1].idempotency_key).toBe(requests[2].idempotency_key);
    expect(button(renderControls(), "start").successLabel).toBe("accepted");
    const progress = find(renderControls(), (node) => node.type === RecoveryProgress)!;
    expect(progress.props.job.status).toBe("queued");
  });
  it("does not let an older status read erase a newly accepted job", async () => {
    let finishStatus!: (response: Response) => void;
    harness.fetch.mockImplementation((_url, init) =>
      init.method === "GET"
        ? new Promise((resolve) => {
            finishStatus = resolve;
          })
        : Promise.resolve(
            Response.json(JSON.parse(init.body).action === "preview" ? { preview } : { job })
          )
    );
    renderControls();
    cleanup = harness.effects[0]() as () => void;
    await button(renderControls(), "preview").onAction();
    await button(renderControls(), "start").onAction();
    finishStatus(Response.json({ job: null }));
    await vi.advanceTimersByTimeAsync(1);
    const progress = find(renderControls(), (node) => node.type === RecoveryProgress);
    expect(progress?.props.job.job_id).toBe("job-a");
  });
  it("offers cancellation for a job held on a provider, without claiming rollback", async () => {
    harness.fetch.mockResolvedValue(
      Response.json({ job: { ...job, status: "waiting_provider", can_resume: true } })
    );
    renderControls();
    cleanup = harness.effects[0]() as () => void;
    await vi.advanceTimersByTimeAsync(1);
    const tree = renderControls();
    expect(button(tree, "resume").disabled).toBe(false);
    expect(button(tree, "cancel").successLabel).toBe("cancelRequested");
  });
  it("does not restart a cleaned-up polling effect during Strict Mode replay", async () => {
    const finishes: Array<(response: Response) => void> = [];
    harness.fetch.mockImplementation(() => new Promise((resolve) => finishes.push(resolve)));
    renderControls();
    const effect = harness.effects[0];
    const firstCleanup = effect() as () => void;
    firstCleanup();
    cleanup = effect() as () => void;
    finishes[0](Response.json({ job: null }));
    await vi.advanceTimersByTimeAsync(3000);
    expect(harness.fetch).toHaveBeenCalledTimes(2);
  });
});
