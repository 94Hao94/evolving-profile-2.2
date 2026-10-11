import {createElement} from "react";
import {renderToStaticMarkup} from "react-dom/server";
import {NextIntlClientProvider} from "next-intl";
import {expect,it} from "vitest";
import en from "@/messages/en.json";
import zh from "@/messages/zh-CN.json";
import {FlowView} from "@/components/flow-view";
import {PromptSourceSummary} from "@/components/prompt-source-summary";

it.each([
  {locale:"en",messages:en,label:"Verified natural users / current audit window"},
  {locale:"zh-CN",messages:zh,label:"已核验自然用户 / 当前审计窗口"},
])("the real flow page does not imply complete source coverage before metadata loads in $locale",({locale,messages,label})=>{
  const markup=renderToStaticMarkup(createElement(NextIntlClientProvider,{locale,messages,timeZone:"Asia/Shanghai",children:createElement(FlowView)}));
  expect(markup).toContain(label);
  expect(markup).toContain('data-source-verification="unknown"');
  expect(markup).not.toContain('data-source-verification="complete"');
});

function renderSummary(locale:string,messages:any,counts:any){
  return renderToStaticMarkup(createElement(NextIntlClientProvider,{locale,messages,timeZone:"Asia/Shanghai",children:createElement(PromptSourceSummary,{counts})}));
}

it.each([
  {locale:"en",messages:en,pending:"Pending verification: 1510"},
  {locale:"zh-CN",messages:zh,pending:"待核验：1510"},
])("zero verified natural results retain partial coverage and the actual pending count in $locale",({locale,messages,pending})=>{
  const markup=renderSummary(locale,messages,{natural:0,audit:1997,verification:"partial",pending:1510});
  expect(markup).toContain('data-source-verification="partial"');
  expect(markup).toContain(pending);
  expect(markup).toContain("0 / 1997");
  expect(markup).not.toContain("No user prompts");
});

it("complete source attribution is still explicitly confined to the current audit window",()=>{
  const markup=renderSummary("en",en,{natural:11,audit:20,verification:"complete",pending:0});
  expect(markup).toContain('data-source-verification="complete"');
  expect(markup).toContain("11 / 20");
  expect(markup).toContain("Only verified natural messages in this window are counted");
});

it("old responses without verification metadata remain unknown",()=>{
  const markup=renderSummary("en",en,{natural:3,audit:20});
  expect(markup).toContain('data-source-verification="unknown"');
  expect(markup).toContain("coverage is unknown");
});

it("a nonzero pending count cannot be presented as completed attribution",()=>{
  const markup=renderSummary("en",en,{natural:2,audit:20,verification:"complete",pending:7});
  expect(markup).toContain('data-source-verification="partial"');
  expect(markup).toContain("Pending verification: 7");
});

it("malformed counts stay unknown rather than being coerced into known zero or displayed as NaN",()=>{
  const markup=renderSummary("en",en,{natural:NaN,audit:-4,verification:"partial",pending:null});
  expect(markup).toContain("— / —");
  expect(markup).toContain("Pending verification: —");
  expect(markup).not.toContain("NaN");
});
