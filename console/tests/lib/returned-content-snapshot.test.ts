import { afterEach,expect,it } from "vitest";
import { mkdtemp,mkdir,writeFile,rm,symlink } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createHash } from "node:crypto";
import { readReturnedContentSnapshot } from "@/lib/returned-content-snapshot";

const dirs:string[]=[];
afterEach(async()=>{await Promise.all(dirs.splice(0).map(path=>rm(path,{recursive:true,force:true})));});
const binding={check_id:"check",session_id:"session",turn_id:"turn",tool_call_id:"call"};
async function fixture(text="实际返回正文😀",extra={}){
 const root=await mkdtemp(join(tmpdir(),"ep-returned-"));dirs.push(root);
 const dir=join(root,"audit/returned-content");await mkdir(dir,{recursive:true});
 const payload=JSON.stringify({schema:"evolving-profile.returned-content.v1",...binding,tool:"user_recall",items:[{id:"memory",item_index:0,text,total_chars:[...text].length,archive_truncated:false}],coverage:"complete",...extra});
 const ref=createHash("sha256").update(payload).digest("hex");await writeFile(join(dir,ref+".json"),payload);
 return {root,dir,ref,payload};
}
it("reads exactly the bound historical return without calling recall or read_source",async()=>{
 const f=await fixture();const result=await readReturnedContentSnapshot(f.ref,{...binding,item_index:0,offset:0},f.root);
 expect(result).toMatchObject({status:200,body:{text:"实际返回正文😀",id:"memory",complete:true,authority:"historical_return_snapshot_not_live_fact_or_answer_adoption"}});
});
it.each(["check_id","session_id","turn_id","tool_call_id"] as const)("refuses mismatched %s even if the reference is known",async(key)=>{
 const f=await fixture();expect((await readReturnedContentSnapshot(f.ref,{...binding,[key]:"other",item_index:0,offset:0},f.root)).status).toBe(403);
});
it("does not load arbitrary paths or other items",async()=>{
 const f=await fixture();expect((await readReturnedContentSnapshot("../../outside",{...binding,item_index:0,offset:0},f.root)).status).toBe(400);
 expect((await readReturnedContentSnapshot(f.ref,{...binding,item_index:1,offset:0},f.root)).status).toBe(404);
});
it("rejects altered bytes and symlink snapshots instead of serving mismatched text",async()=>{
 const f=await fixture();await writeFile(join(f.dir,f.ref+".json"),f.payload.replace("实际","伪造"));
 expect((await readReturnedContentSnapshot(f.ref,{...binding,item_index:0,offset:0},f.root)).status).toBe(409);
 const other=await fixture();const linkRef="a".repeat(64);await symlink(join(other.dir,other.ref+".json"),join(f.dir,linkRef+".json"));
 expect((await readReturnedContentSnapshot(linkRef,{...binding,item_index:0,offset:0},f.root)).status).toBe(403);
});
it("pages long text by Unicode code points and preserves partial archive status",async()=>{
 const f=await fixture("😀".repeat(16001),{coverage:"partial"});
 const first=await readReturnedContentSnapshot(f.ref,{...binding,item_index:0,offset:0},f.root);
 expect([...(first.body as any).text]).toHaveLength(16000);expect(first.body).toMatchObject({next_offset:16000,complete:false});
 const last=await readReturnedContentSnapshot(f.ref,{...binding,item_index:0,offset:16000},f.root);
 expect(last.body).toMatchObject({text:"😀",next_offset:null,complete:false});
});
