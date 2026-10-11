import { expect,it } from "vitest";
import { samePromptBinding } from "@/lib/prompt-binding";
const prompt={hook_invocation_id:"hook",session_id:"s",turn_id:"t"};
it.each([{hook_invocation_id:"hook",session_id:"other",turn_id:"t"},{hook_invocation_id:"hook",session_id:"s",turn_id:"other"},{prompt_binding:prompt,session_id:"other"},{prompt_binding:prompt,check_id:"wrong"}])("does not let a matching hook override explicit identity conflict %j",receipt=>{
 expect(samePromptBinding(prompt,receipt)).toBe(false);
});
it("allows an exact historical binding without inventing missing identities",()=>{
 expect(samePromptBinding(prompt,prompt)).toBe(true);
 expect(samePromptBinding(prompt,{hook_invocation_id:"hook"})).toBe(true);
 expect(samePromptBinding(prompt,{turn_id:"t"})).toBe(false);
});
