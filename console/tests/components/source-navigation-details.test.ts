import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { NextIntlClientProvider } from "next-intl";
import { expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { SourceNavigationDetails } from "@/components/source-navigation-details";
import { navigation } from "../fixtures/source-navigation";

it.each(["en","zh-CN","zh-TW","yue-Hant","de","es","fr","ja","ko","pt"])("renders the returned locator card and read hint in %s without treating it as a fact body",locale=>{
 const messages=JSON.parse(readFileSync(new URL(`../../src/messages/${locale}.json`,import.meta.url),"utf8"));
 const markup=renderToStaticMarkup(createElement(NextIntlClientProvider,{locale,messages,timeZone:"Asia/Shanghai",children:createElement(SourceNavigationDetails,{locator:{...navigation,text:"Unrelated private body"} as any})}));
 expect(markup).toContain(messages.releaseUi.sourceNavigationUnverified);
 expect(markup).toContain(navigation.memory_id);expect(markup).toContain(navigation.document_id);expect(markup).toContain(navigation.chunk_id);
 expect(markup).toContain("read_source");expect(markup).toContain("source-navigation-read-hint");
 expect(markup).not.toContain("Unrelated private body");
 if(!/^(zh|yue)/.test(locale)) expect(markup).not.toMatch(/[\u3400-\u9fff]/u);
});
