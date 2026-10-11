import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { NextIntlClientProvider } from "next-intl";
import { readFileSync } from "node:fs";
import { expect,it } from "vitest";
import { ReturnedContentDetails } from "@/components/returned-content-details";
it.each(["en","zh-CN","de"])("shows exact body, conditions and unknown-delivery boundary in %s",locale=>{
 const messages=JSON.parse(readFileSync(new URL(`../../src/messages/${locale}.json`,import.meta.url),"utf8"));
 const markup=renderToStaticMarkup(createElement(NextIntlClientProvider,{locale,messages,timeZone:"Asia/Shanghai",children:createElement(ReturnedContentDetails,{item:{id:"memory",text:"Actual returned body",applies_when:["UI task"],exceptions:["Not text-only"],text_truncated:true,item_index:0,snapshot:{ref:"a".repeat(64),check_id:"c",session_id:"s",turn_id:"t",tool_call_id:"call"}}})}));
 expect(markup).toContain("Actual returned body");expect(markup).toContain("UI task");expect(markup).toContain("Not text-only");
 expect(markup).toContain(messages.releaseUi.returnedSnapshotBoundary);expect(markup).toContain(messages.releaseUi.returnedSnapshotExpand);
 if(locale!=="zh-CN")expect(markup).not.toMatch(/[\u3400-\u9fff]/u);
});
