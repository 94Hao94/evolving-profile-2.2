import { beforeEach, afterEach, expect, it, vi } from "vitest";
import { mkdtemp, mkdir, writeFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";

let root = "";
vi.mock("node:child_process", () => ({ execFile: vi.fn((_file, _args, _options, callback) => callback?.(new Error("host commands forbidden"))) }));
beforeEach(async () => {
  root = await mkdtemp(path.join(tmpdir(), "ep-scenario-lazy-"));
  vi.stubEnv("EVOLVING_PROFILE_STATE_ROOT", root);
  vi.stubEnv("EVOLVING_PROFILE_BANK_ID", "fixture-bank");
  vi.stubEnv("EVOLVING_PROFILE_API_ENV", path.join(root, "env"));
  vi.stubEnv("EVOLVING_PROFILE_DATAPLANE_API_URL", "http://fixture.invalid");
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({ total: 600 }, { status: 200 })));
  vi.resetModules();
  await mkdir(path.join(root, "context"));
  await writeFile(path.join(root, "codex.json"), JSON.stringify({ bankId: "fixture-bank" }));
  await writeIndex();
});
afterEach(async () => { vi.unstubAllEnvs(); vi.unstubAllGlobals(); await rm(root, { recursive: true, force: true }); });
async function writeIndex() {
  await writeFile(path.join(root, "context/context-index.json"), JSON.stringify({ schema: "evolving-profile.context-index.v3", status: "ready", updated_at: "2026-10-09", projects: [{ context_id: "project:dir", project_key: "dir", identity_status: "unverified_workspace_bucket", session_ids: Array.from({ length: 500 }, (_, i) => `s${i}`), summary: { full: "workspace evidence" } }], sessions: Array.from({ length: 500 }, (_, i) => ({ context_id: `session:s${i}`, session_id: `s${i}`, source_revision: `revision-${i}`, source_ids: ["local-source"], status: "model_reviewed", source_message_count: 8, summary: { compact: "compact evidence", standard: "standard evidence", full: "f".repeat(30000) }, episodes: Array.from({ length: i === 0 ? 110 : 2 }, (_, j) => ({ episode_id: `episode:${i}:${j}`, title: `Part ${j}`, source_message_count: 2, summary: { compact: `episode ${j}`, full: "e".repeat(10000) } })) })) }));
  await writeFile(path.join(root, "context/context-bank-associations.json"), JSON.stringify({ linked_records: 1, scanned_records: 500, indexed_at: "2026-10-08", links: [{ record_id: "record-1", record_type: "world", project_key: "dir", session_ids: ["s0"] }] }));
}
const req = (query = "") => new Request(`http://localhost/api/evolving-profile/scenario/graph?${query}`);

it("runtime status excludes all graph and summary bodies while retaining exact statistical units", async () => {
  const { GET } = await import("@/app/api/evolving-profile/runtime/route");
  const response = await GET(req());
  const value = await response.json();
  expect(value.context.graph).toBeUndefined();
  expect(value.processMemory.graph).toBeUndefined();
  expect(value.context.graphStats).toMatchObject({ nodes: 502, sessions: 500, workspaces: 1, verifiedProjects: 0, bankRecords: 1 });
  expect(JSON.stringify(value)).not.toContain("compact evidence");
  expect(JSON.stringify(value).length).toBeLessThan(40000);
});
it("graph responses clamp the window, strip summaries and nested episodes, and make both edge ends selectable", async () => {
  const { GET } = await import("@/app/api/evolving-profile/scenario/graph/route");
  const value = await (await GET(req("limit=99999"))).json();
  expect(value.graph.nodes.length).toBe(240);
  expect(value.page).toMatchObject({ limit: 240, total: 502, nextOffset: 240 });
  const ids = new Set(value.graph.nodes.map((node: any) => node.id));
  expect(value.graph.edges.every((edge: any) => ids.has(edge.source) && ids.has(edge.target))).toBe(true);
  expect(JSON.stringify(value)).not.toContain("compact evidence");
  expect(value.graph.nodes.every((node: any) => !node.episodes && !node.summary && !node.sessionIds)).toBe(true);
  expect(JSON.stringify(value).length).toBeLessThan(160000);
  const next = await (await GET(req(`offset=240&version=${encodeURIComponent(value.version)}`))).json();
  expect(next.graph.nodes[0].id).toBe("session:s239");
});
it("detail reads one chosen tier and one chosen episode with source revision and workspace scope", async () => {
  const graph = await import("@/app/api/evolving-profile/scenario/graph/route");
  const snapshot = await (await graph.GET(req())).json();
  const { GET } = await import("@/app/api/evolving-profile/scenario/detail/route");
  const response = await GET(req(`id=session:s0&tier=standard&version=${encodeURIComponent(snapshot.version)}`));
  const value = await response.json();
  expect(value.node).toMatchObject({ id: "session:s0", sourceRevision: "revision-0", summary: { standard: "standard evidence" } });
  expect(value.node.summary.full).toBeUndefined();
  expect(value.node.episodes).toBeUndefined();
  const episode = await (await GET(req("id=session:s0&episodeId=episode:0:2&tier=compact"))).json();
  expect(episode.node.summary).toEqual({ compact: "episode 2" });
  const workspace = await (await GET(req("id=project:dir"))).json();
  expect(workspace.node).toMatchObject({ type: "workspace", identityStatus: "unverified_workspace_bucket" });
});
it("episode selector pages metadata without sending any summary bodies", async () => {
  const { GET } = await import("@/app/api/evolving-profile/scenario/episodes/route");
  const value = await (await GET(req("id=session:s0&limit=9999"))).json();
  expect(value.items.length).toBe(100);
  expect(value.page).toMatchObject({ total: 110, nextOffset: 100 });
  expect(value.items[2]).toMatchObject({ id: "episode:0:2", label: "Part 2" });
  expect(JSON.stringify(value)).not.toContain("episode 2");
});
it("rejects stale snapshots, malformed pagination and IDs, unknown nodes, and a different Bank", async () => {
  const { GET } = await import("@/app/api/evolving-profile/scenario/graph/route");
  const snapshot = await (await GET(req())).json();
  await writeFile(path.join(root, "context/context-bank-associations.json"), JSON.stringify({ links: [] }));
  expect((await GET(req(`version=${encodeURIComponent(snapshot.version)}`))).status).toBe(409);
  expect((await GET(req("offset=-1"))).status).toBe(400);
  expect((await GET(req("limit=no"))).status).toBe(400);
  expect((await GET(req("bankId=other"))).status).toBe(403);
  const detail = await import("@/app/api/evolving-profile/scenario/detail/route");
  expect((await detail.GET(req("id=../../codex.json"))).status).toBe(400);
  expect((await detail.GET(req("id=session:absent"))).status).toBe(404);
  expect((await detail.GET(req("id=session:s0&tier=unknown"))).status).toBe(400);
  expect((await detail.GET(req("id=session:s0&episodeId=episode:999:1"))).status).toBe(404);
});
it("missing indexes produce explicit unavailable metadata and an empty bounded graph", async () => {
  await rm(path.join(root, "context/context-index.json"));
  const { GET } = await import("@/app/api/evolving-profile/scenario/graph/route");
  const value = await (await GET(req())).json();
  expect(value.context.status).toBe("unavailable");
  expect(value.graph.nodes).toEqual([]);
  expect(value.page.total).toBe(0);
});
it("process graph windows remain bounded and filters expose old derived records without a recent-trace cutoff", async () => {
  await mkdir(path.join(root, "process-memory"));
  const records = Array.from({ length: 700 }, (_, i) => ({ process_memory_id: `pm_trace_${String(i).padStart(32,"0")}`, kind: i < 300 ? "skill" : "trace", text: `process ${i}`, maturity: "observed", status: "candidate", created_at: String(i) }));
  await writeFile(path.join(root, "process-memory/records.json"), JSON.stringify({ records }));
  const { GET } = await import("@/app/api/evolving-profile/process-memory/graph/route");
  const value = await (await GET(req("kind=skill&limit=9999"))).json();
  expect(value.graph.nodes.length).toBe(240);
  expect(value.page).toMatchObject({ total: 300, nextOffset: 240 });
  expect(value.graph.nodes[0].label).toBe("process 0");
  expect(value.processMemory.record_count).toBe(700);
  expect(value.processMemory.graph).toBeUndefined();
});
it("relation pages retain endpoints and do not drop edges from later windows", async () => {
  const { GET } = await import("@/app/api/evolving-profile/scenario/graph/route");
  const value = await (await GET(req("mode=relations"))).json();
  expect(value.page).toMatchObject({ unit:"relations", total:502, limit:120, nextOffset:120 });
  expect(value.graph.edges.length).toBe(120);
  expect(value.graph.nodes.length).toBeLessThanOrEqual(240);
  const second = await (await GET(req(`mode=relations&offset=120&version=${value.version}`))).json();
  expect(second.graph.edges[0]).toMatchObject({ source:"project:dir", target:"session:s120" });
});
it("summary tiers page large text without losing it or inheriting a sibling source revision", async () => {
  const { readFile } = await import("node:fs/promises");
  const file = path.join(root,"context/context-index.json");
  const index = JSON.parse(await readFile(file,"utf8"));
  index.sessions[0].summary.full = "源".repeat(300000);
  await writeFile(file,JSON.stringify(index));
  const { GET } = await import("@/app/api/evolving-profile/scenario/detail/route");
  const value = await (await GET(req("id=session:s0&tier=full"))).json();
  expect(value.node.summary.full.length).toBe(32768);
  expect(value.node.summaryPage).toMatchObject({ total:300000,nextOffset:32768 });
  expect(JSON.stringify(value).length).toBeLessThan(40000);
  const end = await (await GET(req(`id=session:s0&tier=full&textOffset=294912&version=${value.version}`))).json();
  expect(end.node.summary.full.length).toBe(5088);
  expect(end.node.summaryPage.nextOffset).toBeNull();
  expect((await GET(req("id=session:s0&sourceRevision=revision-1"))).status).toBe(409);
});
it("process detail rejects an expired graph version and returns source locators instead of sibling bodies", async () => {
  await mkdir(path.join(root,"process-memory"));
  const sourceId="pm_trace_"+"a".repeat(32), targetId="pm_skill_"+"b".repeat(32);
  const file=path.join(root,"process-memory/records.json");
  await writeFile(file,JSON.stringify({records:[{process_memory_id:sourceId,kind:"trace",text:"x".repeat(20000),verification_evidence:[{body:"private source body"}]},{process_memory_id:targetId,kind:"skill",text:"selected strategy",derived_from:[sourceId]}]}));
  const graph=await import("@/app/api/evolving-profile/process-memory/graph/route");
  const snapshot=await (await graph.GET(req("kind=skill"))).json();
  const { GET }=await import("@/app/api/evolving-profile/process-memory/[id]/route");
  const response=await GET(req(`version=${snapshot.version}`),{params:Promise.resolve({id:targetId})});
  const value=await response.json();
  expect(value.record.text).toBe("selected strategy");
  expect(value.sources[0].text.length).toBe(160);
  expect(value.sources[0].verification_evidence).toBeUndefined();
  await writeFile(file,JSON.stringify({records:[]}));
  expect((await GET(req(`version=${snapshot.version}`),{params:Promise.resolve({id:targetId})})).status).toBe(409);
});
it("rejects declared cross-Bank snapshots and distinguishes verified projects from directory buckets", async () => {
  const {readFile}=await import("node:fs/promises");
  const file=path.join(root,"context/context-index.json");
  const index=JSON.parse(await readFile(file,"utf8"));
  index.projects.push({context_id:"project:verified",project_key:"verified",identity_status:"verified_project",session_ids:[]});
  await writeFile(file,JSON.stringify(index));
  const { GET }=await import("@/app/api/evolving-profile/scenario/graph/route");
  const value=await (await GET(req())).json();
  expect(value.context).toMatchObject({workspaceCount:1,verifiedProjectCount:1});
  expect(value.graph.bankRecordLinks).toMatchObject({liveTotal:600,unscanned:100,coverage:"stale",snapshotOnly:true});
  index.bank_id="a-different-bank";
  await writeFile(file,JSON.stringify(index));
  expect((await GET(req())).status).toBe(403);
});
it("the process overview keeps derived evidence in the first sample while raw trajectories remain pageable", async () => {
  await mkdir(path.join(root,"process-memory"));
  const records=Array.from({length:400},(_,i)=>({process_memory_id:`pm_trace_${String(i).padStart(32,"0")}`,kind:"trace",text:`trace ${i}`}));
  records.push({process_memory_id:"pm_skill_"+"a".repeat(32),kind:"skill",text:"Visible reusable evidence"});
  await writeFile(path.join(root,"process-memory/records.json"),JSON.stringify({records}));
  const {GET}=await import("@/app/api/evolving-profile/process-memory/graph/route");
  const value=await (await GET(req())).json();
  expect(value.graph.nodes[0]).toMatchObject({type:"agent_skill",label:"Visible reusable evidence"});
  expect(value.page.total).toBe(401);
  expect(value.page.nextOffset).toBe(240);
});
