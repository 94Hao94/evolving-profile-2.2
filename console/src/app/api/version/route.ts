import { NextResponse } from "next/server";
import { localizeApiErrorPayload } from "@/lib/i18n/api-errors";
import { sdk, lowLevelClient } from "@/lib/evolving-client";
import { readFile } from "node:fs/promises";
import { epStatePath } from "@/lib/ep-state-paths";

const RELEASE_MANIFEST_PATH = epStatePath("config/release-manifest.json");

export async function GET(request: Request) {
  try {
    const response = await sdk.getVersion({
      client: lowLevelClient,
    });

    if (response.error) {
      console.error("API error getting version:", response.error);
      return NextResponse.json(
        localizeApiErrorPayload(request, {
          error: "Failed to get version",
          errorKey: "api.errors.version.fetch",
        }),
        { status: 500 }
      );
    }

    const data = response.data as Record<string, unknown>;
    const features = (data.features ?? {}) as Record<string, boolean>;
    features.access_key_auth = !!process.env.EVOLVING_PROFILE_ACCESS_KEY;
    data.features = features;
    try { data.evolving_profile = JSON.parse(await readFile(RELEASE_MANIFEST_PATH, "utf8")); } catch { data.evolving_profile = { product_version: "5.1", release_channel: "development", build_id: "ep51-dev" }; }

    return NextResponse.json(data, { status: 200 });
  } catch (error) {
    console.error("Error getting version:", error);
    return NextResponse.json(
      localizeApiErrorPayload(request, {
        error: "Failed to get version",
        errorKey: "api.errors.version.fetch",
      }),
      { status: 500 }
    );
  }
}
