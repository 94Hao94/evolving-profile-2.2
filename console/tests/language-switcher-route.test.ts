import { describe, expect, it } from "vitest";
import { localeSwitchTarget } from "../src/lib/locale-location";

describe("language switch location preservation", () => {
  it("keeps the current configuration panel and section on locale switch", () => {
    expect(localeSwitchTarget("/banks/current", "?view=profile&bankConfigTab=configuration", "#ep-recall-policy"))
      .toBe("/banks/current?view=profile&bankConfigTab=configuration#ep-recall-policy");
  });
});
