import { describe, expect, it } from "vitest";
import { mkdtemp, writeFile, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { assertRecoveryOrigin, validateRecoveryInput, runMemoryRecoveryCLI } from "../src/lib/memory-recovery-server";

describe("memory recovery server boundary", () => {
  it("rejects path injection and credentials instead of forwarding them", () => {
    expect(() => validateRecoveryInput({ action: "preview", bank_id: "../other-bank" })).toThrow();
    expect(() => validateRecoveryInput({ action: "preview", bank_id: "bank", api_key: "private-value" })).toThrow();
    expect(() => validateRecoveryInput({ action: "start", bank_id: "bank", plan_id: "../plan", idempotency_key: "not-a-uuid" })).toThrow();
  });

  it("rejects cross-origin mutations and preserves same-origin local requests", () => {
    expect(() => assertRecoveryOrigin(new Request("http://127.0.0.1:9999/api/recovery", { headers: { origin: "https://hostile.example" } }))).toThrow();
    expect(() => assertRecoveryOrigin(new Request("http://127.0.0.1:9999/api/recovery", { headers: { origin: "http://127.0.0.1:9999" } }))).not.toThrow();
    // Next's internal request URL can use localhost while the browser Host is
    // 127.0.0.1. The public authority remains the actual Host header.
    expect(() => assertRecoveryOrigin(new Request("http://localhost:10002/api/recovery", { headers: { host: "127.0.0.1:10002", origin: "http://127.0.0.1:10002" } }))).not.toThrow();
    expect(() => assertRecoveryOrigin(new Request("http://localhost:10002/api/recovery", { headers: { host: "127.0.0.1:10002", origin: "https://hostile.example" } }))).toThrow();
  });

  it("executes the real process boundary with JSON stdin, not shell interpolation", async () => {
    const root = await mkdtemp(path.join(os.tmpdir(), "ep-recovery-cli-test-"));
    try {
      const script = path.join(root, "fixture.py");
      await writeFile(script, "import sys,json\nr=json.load(sys.stdin)\nprint(json.dumps({'preview':{'bank_id':r['bank_id'],'estimated_tokens':None},'api_key':'must-not-escape'}))\n");
      const result = await runMemoryRecoveryCLI({ action: "preview", bank_id: "中文-bank" }, { pythonPath: "/usr/bin/python3", scriptPath: script, stateRoot: root });
      expect(result.preview.bank_id).toBe("中文-bank");
      expect(result.preview.estimated_tokens).toBeNull();
      expect(JSON.stringify(result)).not.toContain("must-not-escape");
    } finally { await rm(root, { recursive: true, force: true }); }
  });

  it("keeps a stable backend error code without exposing traceback", async () => {
    const root = await mkdtemp(path.join(os.tmpdir(), "ep-recovery-error-test-"));
    try {
      const script = path.join(root, "fixture.py");
      await writeFile(script, "import sys,json\njson.load(sys.stdin)\nprint(json.dumps({'error':{'code':'plan_source_changed'}}))\nsys.exit(2)\n");
      await expect(runMemoryRecoveryCLI({ action: "preview", bank_id: "bank" }, { pythonPath: "/usr/bin/python3", scriptPath: script, stateRoot: root })).rejects.toMatchObject({ code: "plan_source_changed", status: 409 });
    } finally { await rm(root, { recursive: true, force: true }); }
  });
});
