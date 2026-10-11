import { describe, expect, it } from "vitest";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { appendMemoryQualityEvents, readMemoryQualityEvents } from "@/lib/memory-quality-ledger";
import { projectMemoryQualityEvents } from "@/lib/memory-quality-event";

describe("memory quality append-only ledger", () => {
  it("appends and reads canonical events without an LLM", async () => {
    const dir = mkdtempSync(path.join(tmpdir(), "ep51-quality-"));
    const ledger = path.join(dir, "events.jsonl");
    try {
      const events = projectMemoryQualityEvents({ prompt_id: "ledger-p", at: "2026-10-03T00:00:00Z", user_prompt: "x", memory_route_receipt: { tool_events: [{ tool: "recall", returned_count: 0 }] } });
      expect((await appendMemoryQualityEvents(events, ledger)).appended).toBe(1);
      expect((await readMemoryQualityEvents(ledger)).map((event) => event.event_id)).toEqual([events[0].event_id]);
    } finally { rmSync(dir, { recursive: true, force: true }); }
  });
});
