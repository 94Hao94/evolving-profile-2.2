import type { MemoryQualityEvent } from "@/lib/memory-quality-event";

export type QualityFinding = { code: "missing_call" | "empty_route" | "delivery_gap" | "unverified"; severity: "info" | "warning" | "error"; event_id: string; message: string };

/** Deterministic evaluator for Diagnostic/Deep Audit. It never calls an LLM. */
export function evaluateMemoryQuality(events: MemoryQualityEvent[]): QualityFinding[] {
  return events.flatMap((event): QualityFinding[] => {
    if (event.status === "not_called") return [{ code: "missing_call", severity: "warning", event_id: event.event_id, message: `${event.route} was required but no bound call was observed.` }];
    if (event.status === "source_navigation_returned") return [{code:"unverified",severity:"info",event_id:event.event_id,message:`${event.route} returned source navigation; subject and claims still require original-source review.`}];
    if (event.status === "returned_zero") return [{ code: "empty_route", severity: "info", event_id: event.event_id, message: `${event.route} completed with an observed empty result.` }];
    if (event.returned_count !== null && event.delivered_count === null && event.status === "returned") return [{ code: "delivery_gap", severity: "warning", event_id: event.event_id, message: `${event.route} returned candidates but host delivery was not measured.` }];
    if (event.status === "verified" && event.verified_count === null) return [{ code: "unverified", severity: "warning", event_id: event.event_id, message: `${event.route} is labelled verified without a measured verification count.` }];
    return [];
  });
}
