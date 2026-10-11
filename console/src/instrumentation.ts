/**
 * Next.js instrumentation file - runs exactly once at server startup.
 * https://nextjs.org/docs/app/building-your-application/optimizing/instrumentation
 */
export async function register() {
  if (process.env.NEXT_RUNTIME === "edge") return;
  const { DATAPLANE_URL: dataplaneUrl } = await import("@/lib/evolving-client");
  const apiKey = process.env.EVOLVING_PROFILE_DATAPLANE_API_KEY || "";

  console.log(dataplaneUrl ? `[Control Plane] Connecting to dataplane at: ${dataplaneUrl}` : "[Control Plane] Dataplane endpoint unconfigured");
  if (apiKey) {
    console.log("[Control Plane] Using API key authentication");
  } else {
    console.log("[Control Plane] No API key configured (public access)");
  }
}
