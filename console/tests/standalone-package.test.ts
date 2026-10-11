import { describe, expect, it } from "vitest";
import { mkdtemp, mkdir, writeFile, readFile, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { packageStandalone } from "../scripts/package-standalone.mjs";

describe("standalone deployment packaging", () => {
  it("selects the current build and excludes nested stale releases", async () => {
    const root = await mkdtemp(path.join(os.tmpdir(), "ep-standalone-test-"));
    try {
      const build = path.join(root, ".next/standalone/console");
      await mkdir(path.join(build, "standalone"), { recursive: true });
      await mkdir(path.join(build, ".next"), { recursive: true });
      await mkdir(path.join(root, ".next/standalone/node_modules"), { recursive: true });
      await mkdir(path.join(root, ".next/static"), { recursive: true });
      await mkdir(path.join(root, "public"), { recursive: true });
      await writeFile(path.join(root, ".next/BUILD_ID"), "current");
      await writeFile(path.join(build, ".next/BUILD_ID"), "current");
      await writeFile(path.join(build, "server.js"), "current-server");
      await writeFile(path.join(build, "package.json"), "{}");
      await writeFile(path.join(build, "standalone/server.js"), "stale-server");
      await packageStandalone(root);
      expect(await readFile(path.join(root, "standalone/server.js"), "utf8")).toBe("current-server");
      expect(await readFile(path.join(root, "standalone/.next/BUILD_ID"), "utf8")).toBe("current");
      await expect(readFile(path.join(root, "standalone/standalone/server.js"))).rejects.toThrow();
    } finally { await rm(root, { recursive: true, force: true }); }
  });
});
