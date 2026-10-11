/**
 * Shared Evolving Profile API client instance for the control plane.
 * Configured to connect to the dataplane API server.
 */

import {
  EvolvingProfileClient,
  EvolvingProfileError,
  createClient,
  createConfig,
  sdk,
} from "@evolving-profile/client";
import { EP_STATE_ROOT, EP_DEFAULT_STATE_ROOT } from "@/lib/ep-state-paths";

// EP5.1's local shadow data plane is the service that owns the live bank API.
// Keep the environment override for packaged deployments, but do not fall
// back to the retired 8888 control-plane port on a fresh local install.
export const DATAPLANE_URL = process.env.EVOLVING_PROFILE_DATAPLANE_API_URL?.trim() || (EP_STATE_ROOT === EP_DEFAULT_STATE_ROOT ? "http://127.0.0.1:12088" : "");
const DATAPLANE_API_KEY = process.env.EVOLVING_PROFILE_DATAPLANE_API_KEY || "";
const endpointUnavailable = () => new EvolvingProfileError("dataplane_endpoint_unavailable", 503);
// A placeholder is used only to construct SDK Request objects. The guarded
// transport returns a local 503 without sending it to the network.
const clientBaseUrl = DATAPLANE_URL || "http://ep-dataplane-unconfigured.invalid";
const dataplaneFetch: typeof fetch = async (input, init) => DATAPLANE_URL
  ? fetch(input, init)
  : new Response(JSON.stringify({ error: "dataplane_endpoint_unavailable" }), { status: 503, headers: { "content-type": "application/json" } });

/**
 * Auth headers for direct fetch calls to the dataplane API.
 */
export function getDataplaneHeaders(extra?: Record<string, string>): Record<string, string> {
  if (!DATAPLANE_URL) throw endpointUnavailable();
  const headers: Record<string, string> = { ...extra };
  if (DATAPLANE_API_KEY) {
    headers["Authorization"] = `Bearer ${DATAPLANE_API_KEY}`;
  }
  return headers;
}

/**
 * Build a dataplane URL for a bank-scoped endpoint with the bank id properly encoded.
 * Bank ids may contain `:`, `/`, `%`, etc. (e.g. openclaw `agent::channel::user`),
 * which must be percent-encoded before being interpolated into a URL path.
 */
export function dataplaneBankUrl(bankId: string, suffix = ""): string {
  if (!DATAPLANE_URL) throw endpointUnavailable();
  return `${DATAPLANE_URL}/v1/default/banks/${encodeURIComponent(bankId)}${suffix}`;
}

/**
 * High-level client with convenience methods
 */
const configuredClient = new EvolvingProfileClient({
  baseUrl: clientBaseUrl,
  apiKey: DATAPLANE_API_KEY || undefined,
});
export const evolvingProfileClient = DATAPLANE_URL ? configuredClient : new Proxy(configuredClient, {
  get(target, property, receiver) {
    const value = Reflect.get(target, property, receiver);
    return typeof value === "function" ? () => Promise.reject(endpointUnavailable()) : value;
  },
});

/**
 * Low-level client for direct SDK access
 */
export const lowLevelClient = createClient(
  createConfig({
    baseUrl: clientBaseUrl,
    fetch: dataplaneFetch,
    headers: DATAPLANE_API_KEY ? { Authorization: `Bearer ${DATAPLANE_API_KEY}` } : undefined,
  })
);

/**
 * Export SDK functions for direct API access
 */
export { sdk };

/**
 * Export EvolvingProfileError for error handling
 */
export { EvolvingProfileError };
