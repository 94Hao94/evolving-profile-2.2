import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { NextIntlClientProvider } from "next-intl";
import { expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { ContextDetails } from "@/components/context-memory-view";
import { projectSessionContextNode, selectContextEpisode } from "@/lib/context-node";
import productionRows from "../fixtures/context-coverage-prod.json";

const render=(node:any,locale="en")=>{
 const messages=JSON.parse(readFileSync(new URL(`../../src/messages/${locale}.json`,import.meta.url),"utf8"));
 const markup=renderToStaticMarkup(createElement(NextIntlClientProvider,{locale,messages,timeZone:"Asia/Shanghai",children:createElement(ContextDetails,{node,typeName:()=>"Session"})}));
 return {markup,messages};
};
it.each(["en","zh-CN","zh-TW","yue-Hant","de","es","fr","ja","ko","pt"])("renders actual automatic coverage and preview omission in %s without claiming fact verification",locale=>{
 const node=projectSessionContextNode({...productionRows[0],summary:{compact:"Preview…"}} as any);
 const {markup,messages}=render(node,locale);
 expect(markup).toContain(messages.releaseUi.automatedSourceCoverageReviewed);
 expect(markup).toContain("8/8");expect(markup).toContain(messages.releaseUi.summaryPreviewOmitted);
 expect(markup).not.toContain(messages.releaseUi.summaryBudgetTruncated);
 if(locale==="en") expect(markup).not.toMatch(/[\u3400-\u9fff]/u);
 if(!/^(zh|yue)/.test(locale)) expect(markup).not.toContain("自动来源覆盖已复核");
});
it("keeps real budget truncation visible and exposes both actual episode choices",()=>{
 const row=productionRows.find(row=>row.status==="episode_directory_ready")!;
 const node=projectSessionContextNode({...row,summary:{compact:"Overview"},summary_budget:{compact:{truncated:true}}} as any);
 const {markup,messages}=render(node);
 expect(markup).toContain(messages.releaseUi.summaryBudgetTruncated);
 expect(markup).toContain(messages.releaseUi.scenarioEpisodeSelect);
 for(const episode of node.episodes) expect(markup).toContain(`value="${episode.id}"`);
 expect(markup).not.toContain("not completed semantic review");
});
it("preserves manual coverage and raw fallback semantics",()=>{
 const manual=projectSessionContextNode({context_id:"manual",session_id:"manual",source_message_count:2,manual_source_coverage:{scope_verdict:"whole_session_scope_acceptable",reviewed_source_message_count:2},summary:{compact:"Original"}});
 const {markup,messages}=render(manual);
 expect(markup).toContain("Whole session reviewed");expect(markup).not.toContain(messages.releaseUi.automatedSourceCoverageReviewed);
 const fallback=render(projectSessionContextNode({context_id:"raw",session_id:"raw",status:"raw_available",summary:{compact:"Original"}}));
 expect(fallback.markup).not.toContain(messages.releaseUi.automatedSourceCoverageReviewed);
});
it("renders the actual source file and message offsets for both selected episode details",()=>{
 const row=productionRows.find(row=>row.status==="episode_directory_ready")!;
 const node=projectSessionContextNode(row as any);
 for(const episode of row.episodes) {
  const selected=selectContextEpisode(node,episode.episode_id);
  const {markup}=render(selected);
  expect(selected.sourceIds).toHaveLength(1);
  expect(markup).toContain(episode.source_file_ids[0]);
  expect(markup).toContain("context-source-offsets");
  expect(markup).toContain(`byte_offset: ${episode.source_offsets[0].byte_offset}`);
  expect(markup).toContain(episode.source_offsets[0].message_id);
 }
});
