import {createElement} from "react";
import {renderToStaticMarkup} from "react-dom/server";
import {NextIntlClientProvider} from "next-intl";
import {expect,it} from "vitest";
import en from "@/messages/en.json";
import zh from "@/messages/zh-CN.json";
import {FlowView} from "@/components/flow-view";

it.each([
 {locale:"en",messages:en,label:"Refresh user Prompt flow"},
 {locale:"zh-CN",messages:zh,label:"刷新用户 Prompt 链路"},
])("localizes the real refresh button accessible name and title in $locale",({locale,messages,label})=>{
 const markup=renderToStaticMarkup(createElement(NextIntlClientProvider,{locale,messages,timeZone:"Asia/Shanghai",children:createElement(FlowView)}));
 expect(markup).toContain(`aria-label="${label}"`);
 expect(markup).toContain(`title="${label}"`);
});
