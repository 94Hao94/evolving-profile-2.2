"use client";
import { useEffect, useState } from "react";

export function useWindowedGraph(endpoint: string, query = "", enabled = true) {
  const [window, setWindow] = useState({ key: "", offset: 0, version: "" });
  const [revision, setRevision] = useState(0);
  const [result, setResult] = useState<{ key: string; value: any }>({ key: "", value: null });
  const [failure, setFailure] = useState<{ key: string; error: string } | null>(null);
  const key = `${endpoint}?${query}`;
  const offset = window.key === key ? window.offset : 0;
  const version = window.key === key ? window.version : "";
  const requestKey = `${key}:${offset}:${revision}`;
  useEffect(() => {
    if (!enabled) return;
    const controller = new AbortController();
    setFailure(null);
    const params = new URLSearchParams(query);
    params.set("offset", String(offset));
    if (version) params.set("version", version);
    void fetch(`${endpoint}?${params}`, { cache: "no-store", signal: controller.signal }).then(async response => {
      const value = await response.json();
      if (!response.ok) throw new Error(response.status === 409 ? "stale" : response.status === 403 ? "scope" : "failed");
      if (!controller.signal.aborted) { setResult({ key: requestKey, value }); setWindow({ key, offset, version: value.version }); }
    }).catch(error => { if (!controller.signal.aborted) { setResult({ key: requestKey, value: null }); setFailure({ key: requestKey, error: error.message }); } });
    return () => controller.abort();
    // Version comes from the response for this exact window; changing it alone
    // must not trigger a second request. It is sent when the user changes page.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [endpoint, query, offset, revision, enabled]);
  const data = enabled && result.key === requestKey ? result.value : null;
  const error = failure?.key === requestKey ? failure.error : null;
  return { data, error, loading: enabled && !data && !error,
    go: (offset: number) => setWindow({ key, offset, version: data?.version || version }),
    refresh: () => { setWindow({ key, offset: 0, version: "" }); setRevision(value => value + 1); } };
}
