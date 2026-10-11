import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ReactElement } from "react";

const harness = vi.hoisted(() => ({ stateIndex: 0, states: [] as unknown[], recall: vi.fn(), reflect: vi.fn(), health: vi.fn() }));
vi.mock("react", async (original) => ({
  ...(await original<typeof import("react")>()),
  useRef: () => ({ current: null }),
  useEffect: () => undefined,
  useState: (initial: unknown) => {
    const index = harness.stateIndex++;
    const value = index in harness.states ? harness.states[index] : typeof initial === "function" ? (initial as () => unknown)() : initial;
    return [value, () => undefined];
  },
}));
vi.mock("next-intl", () => ({ useLocale: () => "en", useTranslations: () => (key: string) => key }));
vi.mock("@/lib/bank-context", () => ({ useBank: () => ({ currentBank: "test-bank" }) }));
vi.mock("@/lib/api", () => ({ client: { recall: harness.recall, reflect: harness.reflect, testBankLlm: harness.health } }));
vi.mock("@/components/memory-detail-panel", () => ({ MemoryDetailPanel: () => null }));
vi.mock("@/components/memory-detail-modal", () => ({ MemoryDetailModal: () => null }));
vi.mock("@/components/mental-model-detail-modal", () => ({ MentalModelDetailModal: () => null }));

import { SearchDebugView } from "@/components/search-debug-view";
import { ThinkView } from "@/components/think-view";
import { LlmHealthDialog } from "@/components/llm-health-dialog";

function findOperation(node: unknown): () => Promise<unknown> {
  if (Array.isArray(node)) {
    for (const child of node) {
      try { return findOperation(child); } catch { /* continue through siblings */ }
    }
  } else if (node && typeof node === "object") {
    const props = (node as ReactElement<Record<string, unknown>>).props;
    if (props?.className === "h-12 px-8" || props?.className === "gap-1.5") return (props.onAction ?? props.onClick) as () => Promise<unknown>;
    if (props?.children) return findOperation(props.children);
  }
  throw new Error("Query operation button is missing");
}

beforeEach(() => {
  harness.stateIndex = 0;
  harness.states = ["meaningful query"];
  vi.clearAllMocks();
});

describe("LLM connectivity operation outcomes", () => {
  it("rejects an HTTP-success result when a configured probe failed authentication", async () => {
    harness.states = [];
    harness.health.mockResolvedValue({ bank_id: "test-bank", operations: [
      { operation: "retain", status: "auth_failed", ok: false, latency_ms: null },
      { operation: "consolidation", status: "connected", ok: true, latency_ms: 12 },
      { operation: "reflect", status: "connected", ok: true, latency_ms: 15 },
    ] });
    const action = findOperation(LlmHealthDialog({ bankId: "test-bank", open: true, onOpenChange: () => undefined }));
    await expect(action()).rejects.toThrow("llmAuthFailed");
  });
  it("cancels a health check result with no configured operations", async () => {
    harness.states = [];
    harness.health.mockResolvedValue({ bank_id: "test-bank", operations: [
      { operation: "retain", status: "not_configured", ok: false, latency_ms: null },
      { operation: "consolidation", status: "not_configured", ok: false, latency_ms: null },
      { operation: "reflect", status: "not_configured", ok: false, latency_ms: null },
    ] });
    const action = findOperation(LlmHealthDialog({ bankId: "test-bank", open: true, onOpenChange: () => undefined }));
    await expect(action()).resolves.toBe(false);
  });
});

describe("query operation failure contracts", () => {
  it("lets the recall button observe a server failure instead of announcing success", async () => {
    harness.recall.mockRejectedValue(new Error("Recall backend unavailable"));
    await expect(findOperation(SearchDebugView())()).rejects.toThrow("Recall backend unavailable");
  });
  it("lets the reflection button observe a server failure instead of announcing success", async () => {
    harness.reflect.mockRejectedValue(new Error("Reflection backend unavailable"));
    await expect(findOperation(ThinkView())()).rejects.toThrow("Reflection backend unavailable");
  });
  it("rejects a recall with no fact types before submitting an invalid query", async () => {
    harness.states[1] = [];
    await expect(findOperation(SearchDebugView())()).rejects.toThrow("errorSelectFactType");
    expect(harness.recall).not.toHaveBeenCalled();
  });
});
