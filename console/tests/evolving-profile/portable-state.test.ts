import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { mkdtemp, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { NextRequest } from "next/server";
import { tmpdir } from "node:os";
import path from "node:path";

// The boundary double rejects host commands and out-of-scope writes before they
// can touch a real user. Files under the isolated root use the real filesystem.
let root = "";
const commands = vi.hoisted(() => vi.fn((..._args: unknown[]) => { throw new Error("host command forbidden in isolated state"); }));
vi.mock("node:child_process", () => ({ execFile: commands, spawn: commands }));
vi.mock("node:fs/promises", async (original) => {
  const actual = await original<typeof import("node:fs/promises")>();
  const scoped = (fn: (...args: any[]) => any) => (...args: any[]) => {
    if (!String(args[0]).startsWith(root + path.sep)) throw new Error("out_of_scope_path");
    return fn(...args);
  };
  return { ...actual, readFile: (...args: any[]) => String(args[0]).startsWith(root + path.sep) ? (actual.readFile as any)(...args) : Promise.reject(new Error("out_of_scope_path")),
    writeFile: scoped(actual.writeFile), mkdir: scoped(actual.mkdir) };
});
beforeEach(async () => {
  root = await mkdtemp(path.join(tmpdir(), "ep-portable-"));
  vi.stubEnv("EVOLVING_PROFILE_STATE_ROOT", root);
  vi.stubEnv("EVOLVING_PROFILE_API_ENV", path.join(root, "profiles/evolving-profile-api.env"));
  commands.mockClear(); vi.resetModules();
  vi.stubGlobal("fetch", vi.fn(async () => new Response("{}", { status: 503 })));
});
afterEach(async () => { vi.doUnmock("@/lib/ep-state-paths"); vi.unstubAllEnvs(); vi.unstubAllGlobals(); await rm(root, { recursive: true, force: true }); });
const request = (body: unknown) => new Request("http://localhost/api/settings", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) });

it("reads missing first-install model settings as unconfigured rather than a filesystem error", async () => {
  const { GET } = await import("@/app/api/evolving-profile/retrieval-model-settings/route");
  const response = await GET();
  expect(response.status).toBe(200);
  const value = await response.json();
  expect(value.configuration_status).toBe("unconfigured");
  expect(value.local_inventory).toEqual([]);
  expect(value.embedding.local_path).toBe("");
  expect(value.judge.api_key).toBe("");
  expect(commands).not.toHaveBeenCalled();
});

it("saves isolated backup policy and reports scheduler unsupported without a host command", async () => {
  const { GET, POST } = await import("@/app/api/evolving-profile/backup-settings/route");
  const value = await (await GET()).json();
  expect(value.local.root).toBe(path.join(root, "backups/managed"));
  const response = await POST(request(value));
  expect(response.status).toBe(200);
  expect(await response.json()).toMatchObject({ saved: true, applied: false, apply_status: "unsupported" });
  expect(JSON.parse(await readFile(path.join(root, "config/backup-settings.json"), "utf8"))).toMatchObject({ local: { root: path.join(root, "backups/managed") } });
  expect(commands).not.toHaveBeenCalled();
});

it("keeps isolated model edits pending without restarting a production daemon", async () => {
  await mkdir(path.join(root, "profiles"));
  await writeFile(path.join(root, "profiles/evolving-profile-api.env"), "EVOLVING_PROFILE_API_RERANKER_PROVIDER=rrf\n");
  const { GET, POST } = await import("@/app/api/evolving-profile/retrieval-model-settings/route");
  const value = await (await GET()).json();
  value.embedding.model = "fixture-model";
  const response = await POST(request(value));
  expect(response.status).toBe(200);
  expect(await response.json()).toMatchObject({ saved: true, applied: false, apply_status: "unsupported" });
  expect(JSON.parse(await readFile(path.join(root, "config/runtime-settings.json"), "utf8"))).toMatchObject({ retrieval_models: { embedding: { model: "fixture-model" } } });
  expect(commands).not.toHaveBeenCalled();
});

it("keeps judge-only isolated saves unapplied even when retrieval settings do not change", async () => {
  const { GET, POST } = await import("@/app/api/evolving-profile/retrieval-model-settings/route");
  const value = await (await GET()).json();
  const response = await POST(request({ judge: { ...value.judge, timeout_ms: 6000 } }));
  expect(response.status).toBe(200);
  expect(await response.json()).toMatchObject({ saved: true, applied: false, apply_status: "unsupported" });
  expect(JSON.parse(await readFile(path.join(root, "config/runtime-settings.json"), "utf8"))).toMatchObject({ retrieval_models: { judge: { timeout_ms: 6000 } } });
  expect(commands).not.toHaveBeenCalled();
});

it("reads process evidence from the isolated state rather than the user's home", async () => {
  const id = "pm_trace_" + "a".repeat(32);
  await mkdir(path.join(root, "process-memory"));
  await writeFile(path.join(root, "process-memory/records.json"), JSON.stringify({ records: [{ process_memory_id: id, text: "isolated evidence" }] }));
  const { GET } = await import("@/app/api/evolving-profile/process-memory/[id]/route");
  const response = await GET(new Request("http://localhost"), { params: Promise.resolve({ id }) });
  expect(response.status).toBe(200);
  expect((await response.json()).record.text).toBe("isolated evidence");
});

it("projects isolated runtime settings and scenario data without inspecting a production daemon", async () => {
  await mkdir(path.join(root, "config")); await mkdir(path.join(root, "context"));
  await writeFile(path.join(root, "codex.json"), JSON.stringify({ bankId: "isolated-bank" }));
  await writeFile(path.join(root, "config/runtime-settings.json"), JSON.stringify({ providers: { primary: { name: "isolated-provider", model: "fixture" } } }));
  await writeFile(path.join(root, "context/context-index.json"), JSON.stringify({ status: "ready", sessions: [], projects: [] }));
  const { GET } = await import("@/app/api/evolving-profile/runtime/route");
  const response = await GET(new Request("http://localhost/api/runtime?bankId=isolated-bank"));
  const value = await response.json();
  expect(value.runtimeSettings.providers.primary.name).toBe("isolated-provider");
  expect(value.backup.settings.local?.root).toBe(path.join(root, "backups/managed"));
  expect(JSON.stringify(value)).not.toContain("memory-core-v2-shadow");
  expect(commands).not.toHaveBeenCalled();
});

it("reads local quality reviews only from the isolated root", async () => {
  await mkdir(path.join(root, "audit/jev"), { recursive: true });
  await writeFile(path.join(root, "audit/jev/reviews.jsonl"), JSON.stringify({ id: "isolated-review", calls: 0 }) + "\n");
  const { GET } = await import("@/app/api/evolving-profile/quality/events/route");
  const value = await (await GET(new NextRequest("http://localhost/api/quality/events"))).json();
  expect(value.judge_reviews).toEqual([{ id: "isolated-review", calls: 0 }]);
});

it("reports missing correction runtime as unsupported without invoking another installation", async () => {
  const { POST } = await import("@/app/api/evolving-profile/guidance/manual-correction/route");
  const response = await POST(request({ id: "fixture", text: "fixture" }));
  expect(response.status).toBe(503);
  expect(await response.json()).toMatchObject({ code: "runtime_unavailable" });
  expect(commands).not.toHaveBeenCalled();
});

it("does not invoke a home-bound quality worker for an isolated installation", async () => {
  const { POST } = await import("@/app/api/evolving-profile/quality/judge/route");
  const response = await POST(new NextRequest("http://localhost/api/quality/judge", { method: "POST" }));
  expect(response.status).toBe(503);
  expect(await response.json()).toMatchObject({ code: "runtime_unavailable", calls: 0, memory_mutated: false });
  expect(commands).not.toHaveBeenCalled();
});

it("reports a failed managed scheduler apply separately from its completed policy write", async () => {
  await writeFile(path.join(root, "fixture.plist"), "fixture");
  await writeFile(path.join(root, "fixture-python"), "fixture");
  vi.doMock("@/lib/ep-state-paths", async (original) => ({ ...await original<typeof import("@/lib/ep-state-paths")>(), EP_MANAGED_MAC_HOST: true, EP_BACKUP_PLIST: path.join(root, "fixture.plist"), EP_RUNTIME_PYTHON: path.join(root, "fixture-python") }));
  const { GET, POST } = await import("@/app/api/evolving-profile/backup-settings/route");
  const response = await POST(request(await (await GET()).json()));
  expect(response.status).toBe(200);
  expect(await response.json()).toMatchObject({ saved: true, applied: false, apply_status: "failed" });
  expect(JSON.parse(await readFile(path.join(root, "config/backup-settings.json"), "utf8"))).toMatchObject({ schema: "evolving-profile.backup-settings.v1" });
});
