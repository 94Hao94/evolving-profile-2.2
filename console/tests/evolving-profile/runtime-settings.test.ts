import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mkdtemp, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";

let root: string;
let route: typeof import("@/app/api/evolving-profile/runtime-settings/route");
const request = (body: unknown) => new Request("http://localhost/api/evolving-profile/runtime-settings", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) });

beforeEach(async () => {
  root = await mkdtemp(path.join(tmpdir(), "ep-policy-test-"));
  vi.stubEnv("EVOLVING_PROFILE_STATE_ROOT", root);
  vi.stubEnv("EVOLVING_PROFILE_API_ENV", path.join(root, "missing.env"));
  vi.resetModules();
  route = await import("@/app/api/evolving-profile/runtime-settings/route");
});
afterEach(async () => { vi.unstubAllEnvs(); await rm(root, { recursive: true, force: true }); });

describe("runtime recall policy settings", () => {
  it("normalizes legacy settings and exposes independent effective RAG policy without enabling it", async () => {
    const value = await (await route.GET()).json();
    expect(value.recall_policy).toEqual({ default_min_relevance: "weak", user_memory: "inherit", agent_memory: "inherit", advanced: { allow_transferable_methods: true, allow_background: true, historical_mode: "reference_only", scope_unknown_mode: "keep_navigation", adaptive_enabled: true } });
    expect(value.rag).toMatchObject({ minimum_relevance: "weak", enabled: false });
    expect(value.recall_policy_effective.user_memory.effective_level).toBe("weak");
    expect(value.recall_policy_effective.external_rag.effective_level).toBe("weak");
  });

  it("rejects invalid fields and container types without writing", async () => {
    for (const input of [[], null, { recall_policy: [] }, { recall_policy: { user_memory: "strict" } }, { recall_policy: { advanced: null } }, { recall_policy: { advanced: { allow_background: "false" } } }, { recall_policy: { advanced: { historical_mode: "all" } } }, { rag: [] }, { rag: { minimum_relevance: "inherit" } }, { providers: { fallbacks: {} } }]) {
      expect((await route.POST(request(input))).status).toBe(400);
    }
    await expect(readFile(path.join(root, "config/runtime-settings.json"))).rejects.toThrow();
  });

  it("persists policy-only changes immediately while preserving masked credentials and unrelated settings", async () => {
    await mkdir(path.join(root, "config"));
    await writeFile(path.join(root, "config/runtime-settings.json"), JSON.stringify({
      custom: { retained: 7 }, providers: { primary: { api_key: "primary-secret" }, fallbacks: [{ name: "backup", api_key: "fallback-secret" }] },
      retrieval_models: { judge: { api_key: "judge-secret" } },
    }));
    const response = await route.POST(request({ recall_policy: { default_min_relevance: "strong", agent_memory: "medium" }, rag: { minimum_relevance: "weak" },
      providers: { primary: { api_key: "••••cret" }, fallbacks: [{ name: "backup", api_key: "••••cret" }] }, retrieval_models: { judge: { api_key: "••••cret" } } }));
    expect(response.status).toBe(200);
    const value = await response.json();
    expect(value.recall_policy_effective).toMatchObject({ user_memory: { effective_level: "strong" }, agent_memory: { effective_level: "medium" }, external_rag: { effective_level: "weak" } });
    expect(value.providers.primary.api_key).toBe("••••cret");
    const saved = JSON.parse(await readFile(path.join(root, "config/runtime-settings.json"), "utf8"));
    expect(saved.providers.primary.api_key).toBe("primary-secret");
    expect(saved.providers.fallbacks[0].api_key).toBe("fallback-secret");
    expect(saved.retrieval_models.judge.api_key).toBe("judge-secret");
    expect(saved.custom).toEqual({ retained: 7 });
    expect(saved.recall_policy_effective).toBeUndefined();
    expect((await (await route.GET()).json()).recall_policy_effective.user_memory.effective_level).toBe("strong");
  });

  it("reports invalid persisted policy rather than silently broadening to defaults", async () => {
    await mkdir(path.join(root, "config"));
    await writeFile(path.join(root, "config/runtime-settings.json"), JSON.stringify({ recall_policy: { default_min_relevance: "strict" } }));
    expect((await route.GET()).status).toBe(400);
    expect((await route.POST(request({ budgets: { ep_total_tokens: 4000 } }))).status).toBe(400);
  });

  it("masks active and archived retrieval credentials and preserves masked edits by profile identity", async () => {
    await mkdir(path.join(root, "config"));
    const retrieval = { embedding: { api_key: "fixture-embedding-secret" }, reranker: { api_key: "fixture-reranker-secret" },
      embedding_profiles: [{ profile_id: "first", model: "fixture-first", api_key: "fixture-first-secret" }, { profile_id: "second", model: "fixture-second", api_key: "fixture-second-secret" }],
      reranker_profiles: [{ profile_id: "ranker", model: "fixture-ranker", api_key: "fixture-ranker-secret" }] };
    await writeFile(path.join(root, "config/runtime-settings.json"), JSON.stringify({ retrieval_models: retrieval }));
    const value = await (await route.GET()).json();
    for (const secret of [retrieval.embedding.api_key, retrieval.reranker.api_key, ...retrieval.embedding_profiles.map(p => p.api_key), ...retrieval.reranker_profiles.map(p => p.api_key)]) expect(JSON.stringify(value)).not.toContain(secret);
    value.retrieval_models.embedding_profiles.reverse();
    const response = await route.POST(request({ retrieval_models: value.retrieval_models }));
    expect(response.status).toBe(200);
    expect(JSON.stringify(await response.json())).not.toContain("fixture-embedding-secret");
    const saved = JSON.parse(await readFile(path.join(root, "config/runtime-settings.json"), "utf8")).retrieval_models;
    expect(saved.embedding.api_key).toBe("fixture-embedding-secret");
    expect(saved.reranker.api_key).toBe("fixture-reranker-secret");
    expect(saved.embedding_profiles.map((p: any) => [p.profile_id, p.api_key])).toEqual([["second", "fixture-second-secret"], ["first", "fixture-first-secret"]]);
    expect(saved.reranker_profiles[0].api_key).toBe("fixture-ranker-secret");
  });
});
