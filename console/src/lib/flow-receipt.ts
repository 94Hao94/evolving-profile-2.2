export type FlowLane =
  | "ingress"
  | "guidance"
  | "user_memory"
  | "agent_process"
  | "external_rag"
  | "model_context"
  | "execution_writeback";

export type FlowStatus =
  | "disabled"
  | "not_applicable"
  | "not_requested"
  | "available"
  | "observed"
  | "candidate_returned"
  | "gated"
  | "delivered"
  | "verified"
  | "empty"
  | "unavailable"
  | "failed"
  | "not_observed"
  | "unknown";

export type FlowReceipt = {
  trace_id: string;
  parent_trace_id?: string | null;
  prompt_id?: string | null;
  turn_id?: string | null;
  session_id?: string | null;
  project_id?: string | null;
  lane: FlowLane;
  stage: string;
  status: FlowStatus;
  started_at?: string | null;
  finished_at?: string | null;
  candidate_count?: number | null;
  returned_count?: number | null;
  source_navigation_returned_count?: number | null;
  source_navigation_returned_ids?: string[];
  delivered_count?: number | null;
  token_count?: number | null;
  source_type?: string | null;
  source_ids?: string[];
  evidence_ids?: string[];
  summary?: string | null;
  error?: string | null;
  relevance_audit?: Record<string, unknown> | null;
  verification_evidence?: Array<Record<string, unknown>>;
};

export type FlowLaneSummary = {
  lane: FlowLane;
  status: FlowStatus;
  receipts: FlowReceipt[];
  counts: FlowCounts;
};

export type FlowCounts = { candidates: number | null; returned: number | null; delivered: number | null; tokens: number | null };

export function sumObservedCounts(values: Array<number | null | undefined>): number | null {
  return values.every(value=>typeof value === "number" && Number.isInteger(value) && value >= 0)
    ? values.reduce<number>((sum,value)=>sum+(value as number),0) : null;
}

export function sumFlowCounts(counts: FlowCounts[]): FlowCounts {
  return Object.fromEntries((["candidates","returned","delivered","tokens"] as const).map(key=>[key,sumObservedCounts(counts.map(count=>count[key]))])) as FlowCounts;
}

export const FLOW_LANES: FlowLane[] = [
  "ingress",
  "guidance",
  "user_memory",
  "agent_process",
  "external_rag",
  "model_context",
  "execution_writeback",
];

export function summarizeFlowLane(lane: FlowLane, receipts: FlowReceipt[]): FlowLaneSummary {
  const counts = sumFlowCounts(receipts.map(receipt=>({candidates:receipt.candidate_count ?? null,returned:receipt.returned_count ?? null,delivered:receipt.delivered_count ?? null,tokens:receipt.token_count ?? null})));
  const status: FlowStatus = receipts.length ? (receipts.some((r) => r.status === "failed") ? "failed" : receipts.some((r) => r.status === "delivered" || r.status === "verified") ? "delivered" : receipts.some((r) => r.status === "observed") ? "observed" : receipts[0].status) : "not_observed";
  return { lane, status, receipts, counts };
}
