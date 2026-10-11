import { describe, expect, it } from "vitest";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { TopologyEngineLab } from "@/components/topology-engine-lab";
import { buildTopologySpec, type TopologyBranchSummary } from "@/lib/topology-spec";
import {summarizeReceiptScopes} from "@/lib/topology-presentation";

const branch = (stage: string, count: number, extra = {}): TopologyBranchSummary => ({
  status: "delivered", counts: { candidates: 0, returned: count, delivered: count, tokens: 0 },
  receipts: [{ trace_id: stage, stage, lane: "agent_process", status: "delivered", returned_count: count, ...extra }], items: [],
});
const make = (branches = new Map<string, TopologyBranchSummary>()) => buildTopologySpec({ english: false, summaries: new Map(), sourceScope: "test", branchSummaries: branches });
const button = (markup: string, id: string) => markup.split(`data-node-id="${id}"`)[1]?.split("</foreignObject>")[0] || "";

describe("topology node purpose", () => {
  it.each([false,true])("opens the entire topology at fit width by default (english=%s)", (english) => {
    const markup=renderToStaticMarkup(createElement(TopologyEngineLab,{nodes:make().nodes,english}));
    const fitLabel=english ? "Fit" : "全图";
    const fitButton=markup.match(/<button\b[^>]*>[^<]*<\/button>/g)?.find(button=>button.endsWith(`>${fitLabel}</button>`));
    expect(fitButton).toContain('aria-pressed="true"');
    const diagram=markup.match(/<svg[^>]*aria-label="(?:Execution topology with receipt nodes|执行链路与回执节点)"[^>]*>/)?.[0];
    expect(diagram).toContain('width:100%');
    expect(diagram).not.toContain('min-width:1672px');
    const stage=markup.match(/<div\b[^>]*class="reference-topology-stage"[^>]*>/)?.[0];
    expect(stage).toContain('max-height:none');
  });
  it("distinguishes preference packets, unit readbacks and history/source without repeated-call double counting",()=>{
    const pref=[{trace_id:"p1",stage:"user_preference",lane:"user_memory",status:"delivered",returned_count:9,delivered_count:9},{trace_id:"p2",stage:"user_preference",lane:"user_memory",status:"delivered",returned_count:9,delivered_count:9},...Array.from({length:8},(_,i)=>({trace_id:`u${i}`,stage:"read_preference_unit",lane:"user_memory",status:"delivered",returned_count:1,delivered_count:1}))] as any;
    expect(summarizeReceiptScopes(pref)).toMatchObject({candidatePack:{calls:2,returned:18,delivered:18},unitReadback:{calls:8,returned:8,delivered:8},all:{calls:10,returned:26,delivered:26}});
    const all=[...pref,pref[0],{trace_id:"r",stage:"user_research",lane:"user_memory",status:"delivered",returned_count:6,delivered_count:6},{trace_id:"s1",stage:"read_source",lane:"user_memory",status:"delivered",returned_count:1,delivered_count:1},{trace_id:"s2",stage:"read_source",lane:"user_memory",status:"delivered",returned_count:1,delivered_count:1}];
    expect(summarizeReceiptScopes(all).all).toEqual({calls:13,returned:34,delivered:34});
  });
  it("provides readable HTML node buttons and explicit zoom controls alongside the SVG",()=>{
    const markup=renderToStaticMarkup(createElement(TopologyEngineLab,{nodes:buildTopologySpec({english:true,summaries:new Map(),sourceScope:"test"}).nodes,english:true}));
    expect(markup).toContain('aria-label="Topology zoom"');
    expect(markup).toContain('data-testid="topology-node-index"');
    expect(markup).toContain('aria-label="Open receipt: User Recall"');
  });
  it("renders an unexecuted descriptive intervention without placeholder counts", () => {
    const markup = renderToStaticMarkup(createElement(TopologyEngineLab, { nodes: make().nodes }));
    expect(button(markup, "agent-process-memory-guidance-hint")).not.toContain("候选");
    expect(button(markup, "agent-process-memory-guidance-scaffold")).not.toContain("送达");
    expect(button(markup, "fork")).not.toContain("候选");
  });
  it("uses record and validation units instead of retrieval counts", () => {
    const b = new Map([
      ["agent-process-memory-observe-trajectory", branch("record_agent_trajectory", 2)],
      ["agent-process-memory-migration-revalidation", branch("revalidate_agent_process_memory", 1, { verification_evidence: [{status:"passed", tests:10, scope:"current_python"}] })],
    ]);
    const markup = renderToStaticMarkup(createElement(TopologyEngineLab, {nodes:make(b).nodes}));
    expect(button(markup,"agent-process-memory-observe-trajectory")).toContain("记录 2");
    expect(button(markup,"agent-process-memory-migration-revalidation")).toContain("验证 10 项");
  });
  it("does not add write and validation receipts to retrieved context totals", () => {
    const b = new Map([
      ["agent-process-memory-observe-trajectory", branch("record_agent_trajectory",2)],
      ["agent-process-memory-capability-observation", branch("record_agent_capability_observation",1)],
      ["agent-process-memory-migration-revalidation", branch("revalidate_agent_process_memory",1)],
      ["agent-process-memory-process-retrieval", branch("agent_recall",3)],
    ]);
    const nodes=make(b).nodes;
    expect(nodes.find(n=>n.id==="agent-root")?.data.counts.delivered).toBe(3);
    expect(nodes.find(n=>n.id==="agent-packet")?.data.counts.delivered).toBe(3);
  });
});
