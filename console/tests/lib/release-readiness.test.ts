import { expect,it } from "vitest";
import { needsProcessRevalidation, processRecordTitle } from "@/lib/process-presentation";
import { discoverModelProposal, modelIdentityMatches, parseModelEnvironment } from "@/lib/retrieval-model-identity";
import { inlineUiText, enumUiText } from "@/lib/inline-i18n";
it("keeps transfer candidates in the same migration predicate as drift records",()=>{
  expect(needsProcessRevalidation({transfer_scope:{transfer_status:"candidate"}})).toBe(true);
  expect(needsProcessRevalidation({transfer_status:"unknown",status:"stable"})).toBe(true);
  expect(needsProcessRevalidation({status:"stable",transfer_status:"verified"})).toBe(false);
});
it("gives capability records distinct task/model/metric titles",()=>{
  expect(processRecordTitle({kind:"capability_observation",task_archetype:["configuration"],model_profile:{model:"m1"},capability_metric:"source_readback",created_at:"2026-10-08"})).toContain("configuration");
});
it("never swaps same-dimension foreign models during discovery",()=>{
  const current={model:"intfloat/multilingual-e5-small",dimensions:384,local_path:"",profile_id:"embedding-default"};
  const out=discoverModelProposal(current,[{kind:"embedding",model:"sentence-transformers/all-MiniLM-L6-v2",dimensions:384,path:"/models/minilm"}],"embedding");
  expect(out.model).toBe(current.model);expect(out.local_path).toBe("");expect(out.discovery_proposals).toHaveLength(1);
  expect(modelIdentityMatches(current,{...current,model:"sentence-transformers/all-MiniLM-L6-v2"})).toBe(false);
});
it("uses overlay precedence and removes shell quotes from model identity",()=>{
 expect(parseModelEnvironment('MODEL=e5\nMODEL="MiniLM"\nRERANKER="rrf"\n')).toEqual({MODEL:"MiniLM",RERANKER:"rrf"});
});
it("uses readable known labels and preserves unknown source text",()=>{
  expect(inlineUiText("本地模型目录","en")).toBe("Local model directory");
  expect(inlineUiText("原文中未收录的中文句子","en")).toBe("原文中未收录的中文句子");
});
it("localizes host replay scope as interface explanation rather than memory content",()=>{
 const source="同一 Prompt / 宿主原始工具结果回放（不代表回答采用）";
 expect(inlineUiText(source,"en")).toBe("Same Prompt / retained host tool results (does not imply answer adoption)");
 expect(inlineUiText(source,"zh-CN")).toBe(source);
});
it("localizes saved inactive model status rather than exposing a raw enum",()=>{
 expect(enumUiText("configured_but_inactive","en")).toBe("Configured, inactive");
 expect(enumUiText("configured_but_inactive","zh-CN")).toBe("已配置但未启用");
 expect(enumUiText("disabled_saved","en")).toBe("Saved, disabled");
 expect(enumUiText("unconfigured","en")).toBe("Not configured");
 expect(enumUiText("unconfigured","zh-CN")).toBe("未配置");
});
