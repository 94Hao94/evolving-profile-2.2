import { renderToStaticMarkup } from "react-dom/server";
import { expect, it, vi } from "vitest";
const pending = "Configuration saved, not applied.";
const harness = vi.hoisted(() => ({ index: 0 }));
vi.mock("react", async (original) => ({ ...(await original<typeof import("react")>()), useEffect: () => undefined, useCallback: (fn: unknown) => fn,
  useState: (initial: unknown) => { const index = harness.index++; return [index === 0 ? { backup: { settings: {} } } : index === 1 ? "Configuration saved, not applied." : initial, () => undefined]; } }));
vi.mock("next-intl", () => ({ useLocale: () => "en", useTranslations: () => (key: string) => key }));
import { RuntimeSectionsView } from "@/components/runtime-sections-view";
it("renders saved-but-unapplied backup feedback in the visible panel", () => {
  harness.index = 0;
  expect(renderToStaticMarkup(RuntimeSectionsView({ section: "backup" }))).toContain(pending);
});
