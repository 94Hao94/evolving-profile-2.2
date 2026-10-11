import { NextResponse } from "next/server";
import { readReturnedContentSnapshot } from "@/lib/returned-content-snapshot";
export async function GET(request:Request,{params}:{params:Promise<{ref:string}>}){
 const {ref}=await params;const query=new URL(request.url).searchParams;
 const result=await readReturnedContentSnapshot(ref,{check_id:query.get("check_id")||"",session_id:query.get("session_id")||"",turn_id:query.get("turn_id")||"",tool_call_id:query.get("tool_call_id")||"",item_index:Number(query.get("item_index")??"invalid"),offset:Number(query.get("offset")??0)});
 return NextResponse.json(result.body,{status:result.status,headers:{"Cache-Control":"no-store"}});
}
