import {createElement} from "react";
import {renderToStaticMarkup} from "react-dom/server";
import {NextIntlClientProvider} from "next-intl";
import {expect,it} from "vitest";
import en from "@/messages/en.json";
import zh from "@/messages/zh-CN.json";
import de from "@/messages/de.json";
import {ContextMemoryView,ContextTableNodeButton} from "@/components/context-memory-view";

it.each([
 {locale:"en",messages:en,source:"View source node",target:"View target node"},
 {locale:"zh-CN",messages:zh,source:"查看来源节点",target:"查看目标节点"},
 {locale:"de",messages:de,source:"View source node",target:"View target node"},
])("localizes both real table node buttons and preserves explicit English fallback in $locale",({locale,messages,source,target})=>{
 for(const role of ["source","target"] as const) {
  const nodeId=`${role}:one`,label=role==="source" ? source : target;
  const markup=renderToStaticMarkup(createElement(NextIntlClientProvider,{locale,messages,timeZone:"Asia/Shanghai",children:createElement(ContextTableNodeButton,{nodeId,role,onSelect:()=>{}})}));
  expect(markup).toContain(`aria-label="${label} ${nodeId}"`);expect(markup).toContain(`title="${label}"`);
  if(locale!=="zh-CN") expect(markup).not.toMatch(/[\u3400-\u9fff]/u);
 }
});

it.each([
 {locale:"en",messages:en,label:"Session scenario entries",legend:"Purple: workspace clues (not verified projects); green: Sessions; blue: Bank records.",hint:"Navigation entries include canonical summaries, pending processing and raw-source fallbacks; check each entry's details for its status."},
 {locale:"zh-CN",messages:zh,label:"Session 情景条目",legend:"紫色是工作目录线索（非已核实项目），绿色是 Session，蓝色是 Bank 记录。",hint:"导航条目包含规范摘要、待处理项和原文降级；具体状态以条目详情为准。"},
])("describes scenario entry count and workspace legend accurately in $locale",({locale,messages,label,legend,hint})=>{
 const markup=renderToStaticMarkup(createElement(NextIntlClientProvider,{locale,messages,timeZone:"Asia/Shanghai",children:createElement(ContextMemoryView,{bankId:"fixture"})}));
 expect(markup).toContain(label);expect(markup).toContain(legend);expect(markup.replaceAll("&#x27;","'")).toContain(hint);
});
