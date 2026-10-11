import { epStatePath, EP_STATE_ROOT, EP_API_ENV, EP_HOST_SESSIONS } from "@/lib/ep-state-paths";
import { appendFile, mkdir, readFile } from "node:fs/promises";
import path from "node:path";
import type { MemoryQualityEvent } from "@/lib/memory-quality-event";

export const DEFAULT_MEMORY_QUALITY_LEDGER = epStatePath("audit/memory-quality-events.jsonl");

export async function appendMemoryQualityEvents(events: MemoryQualityEvent[], ledgerPath = process.env.EP_MEMORY_QUALITY_LEDGER_PATH || DEFAULT_MEMORY_QUALITY_LEDGER) {
  if (!events.length) return { appended: 0, path: ledgerPath };
  await mkdir(path.dirname(ledgerPath), { recursive: true });
  const payload = events.map((event) => JSON.stringify(event)).join("\n") + "\n";
  await appendFile(ledgerPath, payload, "utf8");
  return { appended: events.length, path: ledgerPath };
}

export async function readMemoryQualityEvents(ledgerPath = process.env.EP_MEMORY_QUALITY_LEDGER_PATH || DEFAULT_MEMORY_QUALITY_LEDGER, limit = 500) {
  try {
    const text = await readFile(ledgerPath, "utf8");
    const rows = text.split("\n").filter(Boolean).slice(-Math.max(1, Math.min(5000, limit)));
    return rows.flatMap((row) => { try { const value = JSON.parse(row); return value?.schema === "evolving-profile.memory-quality-event.v1" ? [value as MemoryQualityEvent] : []; } catch { return []; } });
  } catch { return []; }
}
