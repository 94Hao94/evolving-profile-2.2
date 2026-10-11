import type { TopologyNode, TopologyNodeData } from "./topology-spec";
import type { FlowReceipt } from "./flow-receipt";
import { inlineUiText } from "./inline-i18n";

export function summarizeReceiptScopes(receipts:FlowReceipt[]) {
  const unique=[...new Map(receipts.map((receipt,index)=>[`${receipt.session_id || ""}:${receipt.turn_id || ""}:${receipt.trace_id || index}`,receipt])).values()];
  const sum=(group:FlowReceipt[],field:"returned_count"|"delivered_count")=>group.every(receipt=>typeof receipt[field]==="number") ? group.reduce((total,receipt)=>total+(receipt[field] as number),0) : null;
  const group=(rows:FlowReceipt[])=>({calls:rows.length,returned:sum(rows,"returned_count"),delivered:sum(rows,"delivered_count")});
  const navigation=unique.filter(receipt=>typeof receipt.source_navigation_returned_count==="number");
  return {candidatePack:group(unique.filter(receipt=>["user_preference","get_preference"].includes(receipt.stage))),unitReadback:group(unique.filter(receipt=>["read_preference_unit","read_agent_process_memory"].includes(receipt.stage))),all:group(unique),sourceNavigationReturned:navigation.length ? navigation.reduce((sum,receipt)=>sum+(receipt.source_navigation_returned_count || 0),0) : null};
}

const processRecords = new Set(["observe-trajectory", "capability-observation", "failure-events", "repair-mode", "reusable-process-strategy", "migration-revalidation"]);
export function nodePurpose(id: string): "description" | "record" | "retrieval" {
  if (["prompt", "binding", "contract", "fork"].includes(id) || id.includes("guidance-") || id.endsWith("compatibility-gate")) return "description";
  if ([...processRecords].some(s=>id===`agent-process-memory-${s}`)) return "record";
  return "retrieval";
}
export function isContextBranch(id: string) {
  return nodePurpose(id)==="retrieval" || /agent-process-memory-guidance-(hint|recommend|scaffold|guard)$/.test(id);
}
export function nodeMetric(id: string, data: TopologyNodeData, english = false): string | null {
  const purpose=nodePurpose(id);
  if (purpose==="description") return null;
  if (purpose==="record") {
    if (!data.receipts.length) return null;
    const good=data.receipts.filter(r=>r.status!=="failed");
    const records=good.reduce((n,r)=>n+(r.returned_count ?? 0),0);
    const ev=good.flatMap(r=>r.verification_evidence || []);
    const tests=Math.max(0,...ev.filter(e=>e.status==="passed" || e.exit_code===0).map(e=>typeof e.tests==="number"?e.tests:0));
    if (id.endsWith("migration-revalidation")) return tests ? (english?`Validated ${tests} tests`:`验证 ${tests} 项 · 通过`) : (english?`Revalidations ${good.length}`:`再验证 ${good.length} 次`);
    return english?`Records ${records}`:`记录 ${records}`;
  }
  const known=data.receipts.length || data.counts.returned || data.counts.delivered || data.counts.candidates;
  if (!known) return null;
  if(data.status==="disabled") return english?"Disabled":"已禁用";
  const measured=data.receipts.some(r=>typeof r.candidate_count==="number");
  const candidate=measured || !data.receipts.length ? data.counts.candidates : "—";
  const navigation=summarizeReceiptScopes(data.receipts).sourceNavigationReturned;
  return `${english?"Candidates":"候选"} ${candidate ?? "—"} · ${english?"Delivered":"送达"} ${data.counts.delivered ?? "—"}${navigation===null ? "" : ` · ${inlineUiText("原文导航返回数", english ? "en" : undefined)} ${navigation}`}`;
}
export function contextChildren(nodes: TopologyNode[], lane: string, root: string, packet: string) {
  return nodes.filter(n=>n.data.lane===lane && n.id!==root && n.id!==packet && n.data.kind!=="lane" && isContextBranch(n.id));
}

export type Anchor = {x:number;y:number;width:number;height:number};
// Only five approved problem frames change. All other calibrated coordinates,
// text overrides and deletions retain their v7 identities.
export const LOCAL_LAYOUT: Record<string, Anchor> = {
  "label-user-lane": {x:245/1672*100,y:259/941*100,width:200/1672*100,height:28/941*100},
  "label-agent-lane": {x:245/1672*100,y:479/941*100,width:215/1672*100,height:28/941*100},
  "label-rag-lane": {x:245/1672*100,y:755/941*100,width:200/1672*100,height:28/941*100},
  "agent-process-memory-observe-trajectory": {x:330/1672*100,y:543/941*100,width:154/1672*100,height:72/941*100},
  "rag-root": {x:330/1672*100,y:777/941*100,width:164/1672*100,height:72/941*100},
};
export function upgradeLocalLayout(saved: Record<string, Anchor>) { return {...saved,...LOCAL_LAYOUT}; }
