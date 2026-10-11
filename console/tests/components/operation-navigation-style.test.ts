import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ReactElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

const hooks = vi.hoisted(() => ({ index: 0, states: [] as unknown[] }));
vi.mock("react", async (original) => ({
  ...(await original<typeof import("react")>()),
  useEffect: () => undefined,
  useCallback: (callback: unknown) => callback,
  useMemo: (factory: () => unknown) => factory(),
  useRef: (initial: unknown) => ({ current: initial }),
  useState: (initial: unknown) => {
    const index = hooks.index++;
    return [index in hooks.states ? hooks.states[index] : typeof initial === "function" ? (initial as () => unknown)() : initial, () => undefined];
  },
}));
vi.mock("next-intl", () => ({ useTranslations: () => (key: string) => key }));
vi.mock("@/lib/bank-context", () => ({ useBank: () => ({ currentBank: "test-bank" }) }));
vi.mock("@/lib/api", () => ({ client: {} }));
vi.mock("@/components/constellation", () => ({ Stars: () => null }));
vi.mock("@/components/graph", () => ({ Graph: () => null }));

import { BankOperationsView } from "@/components/bank-operations-view";
import { AgentProcessView } from "@/components/agent-process-view";

function find(node: unknown, predicate: (element: ReactElement<Record<string, unknown>>) => boolean): ReactElement<Record<string, unknown>>[] {
  if (Array.isArray(node)) return node.flatMap((child) => find(child, predicate));
  if (!node || typeof node !== "object" || !("props" in node)) return [];
  const element = node as ReactElement<Record<string, unknown>>;
  return [...(predicate(element) ? [element] : []), ...find(element.props.children, predicate)];
}

beforeEach(() => { hooks.index = 0; hooks.states = []; });

describe("operation navigation visuals", () => {
  it("keeps operation status filters as navigation controls rather than completed actions", () => {
    const controls = find(BankOperationsView(), (element) => String(element.props.className).includes("px-3 py-1.5 text-sm font-medium"));
    expect(controls.length).toBeGreaterThan(1);
    expect(controls.every((control) => control.type === "button")).toBe(true);
  });

  it("renders idle process records with readable foreground and neutral background", () => {
    hooks.states = ["table"];
    const tree = AgentProcessView({ english: true, graph: {
      nodes: [{ id: "agent:a", type: "agent_trace", label: "Original source record" }], edges: [], timeline: [],
    } });
    const record = find(tree, (element) => typeof element.props.onAction === "function")[0];
    expect(record).toBeDefined();
    const markup = renderToStaticMarkup(record);
    const classes = markup.match(/<button[^>]*class="([^"]+)"/)?.[1].split(/\s+/) ?? [];
    expect(classes).toContain("text-foreground");
    expect(classes).not.toContain("bg-primary");
    expect(classes).not.toContain("text-primary-foreground");
  });
});
