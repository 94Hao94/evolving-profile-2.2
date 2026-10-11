import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { NextIntlClientProvider } from "next-intl";
import { expect,it } from "vitest";
import en from "@/messages/en.json";
import { ExecutionTopologyCanvas } from "@/components/execution-topology-canvas";
import { projectFlowAudit } from "@/lib/flow-projection";
it.each([true,false])("does not call prepared Hook preference delivered (audit=%s)",audit=>{
 const prompt={prompt_id:"p",at:"a",user_prompt:"x",guidance_receipt:{host_state:"hook_context_prepared",guidance_items:[{id:"pref",text:"prepared only"}]}};
 const markup=renderToStaticMarkup(createElement(NextIntlClientProvider,{locale:"en",messages:en,timeZone:"Asia/Shanghai",children:createElement(ExecutionTopologyCanvas,{promptId:"p",english:true,audit:audit?projectFlowAudit(prompt):undefined,guidanceReceipt:prompt.guidance_receipt})}));
 expect(markup).not.toContain("Delivered 1");expect(markup).toContain("Delivered —");
});
it("does not call unknown delivery zero for legacy historical audit",()=>{
 const prompt={prompt_id:"p",at:"a",user_prompt:"x",historical_audit:{route:"recall",state:"observed",candidate_count:2,returned_to_host_count:1,delivery_state:"not_measured"}};
 const markup=renderToStaticMarkup(createElement(NextIntlClientProvider,{locale:"en",messages:en,timeZone:"Asia/Shanghai",children:createElement(ExecutionTopologyCanvas,{promptId:"p",english:true,audit:projectFlowAudit(prompt as any)})}));
 expect(markup).not.toContain("Candidates 2 · Delivered 0");
});
