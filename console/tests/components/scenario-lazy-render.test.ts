import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { NextIntlClientProvider } from "next-intl";
import { expect, it } from "vitest";
import en from "@/messages/en.json";
import zh from "@/messages/zh-CN.json";
import { ContextDetails } from "@/components/context-memory-view";

it.each([["en",en],["zh-CN",zh]])("a remote detail starts with honest loading instead of declaring an unread summary empty in %s", (locale,messages) => {
  const markup = renderToStaticMarkup(createElement(NextIntlClientProvider, { locale: locale as string, messages: messages as any, timeZone:"Asia/Shanghai",
    children: createElement(ContextDetails, { node: { id:"session:fixture", type:"session", label:"Fixture",status:"model_reviewed" }, typeName:()=>"Session", remote: {bankId:"fixture-bank",version:"fixture-version",onRefresh:()=>{}} } as any) }));
  expect(markup).toContain(locale === "en" ? "Loading scenario data…" : "正在读取情景数据…");
  expect(markup).not.toContain("Current node has no displayable summary.");
  expect(markup).not.toContain("当前节点没有可显示的摘要");
});
