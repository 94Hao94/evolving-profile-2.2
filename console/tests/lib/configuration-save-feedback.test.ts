import { expect, it } from "vitest";
import { configurationSaveFeedback } from "@/lib/configuration-save-feedback";
it("distinguishes a saved policy from an applied service change", () => {
  expect(configurationSaveFeedback(true, { saved: true, applied: false }, "en")).toContain("not applied");
  expect(configurationSaveFeedback(true, { saved: true, applied: false }, "zh-CN")).toContain("尚未应用");
  expect(configurationSaveFeedback(true, { saved: true, applied: true }, "en")).toContain("applied");
  expect(configurationSaveFeedback(true, { saved: true }, "en")).not.toContain("applied");
});
it("fails an unsuccessful write even when its response includes an applied field", () => {
  expect(() => configurationSaveFeedback(false, { applied: true, error: "fixture-error" }, "en")).toThrow("fixture-error");
  expect(() => configurationSaveFeedback(true, { saved: false }, "en")).toThrow();
});
