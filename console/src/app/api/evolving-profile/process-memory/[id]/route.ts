import { NextResponse } from "next/server";
import { readProcessSnapshot,processRevisionReader } from "@/lib/process-graph-data";
import {ScenarioRequestError} from "@/lib/scenario-data";
import {bankScopeProjection} from "@/lib/bank-data-scope";

export async function GET(request: Request, context: { params: Promise<{ id: string }> }) {
  const { id } = await context.params;
  if (!/^pm_[a-z_]+_[a-f0-9]{32}$/.test(id)) return NextResponse.json({ error: "过程记录 ID 无效" }, { status: 400 });
  try {
    const snapshot = await readProcessSnapshot(new URL(request.url).searchParams.get("bankId"));
    if (snapshot.status === "unavailable") return NextResponse.json({ error:"过程记忆数据源不可用" },{status:503});
    const params=new URL(request.url).searchParams;
    const version=params.get("version");
    if (!params.has("nodeVersion") && version && version!==snapshot.version) return NextResponse.json({error:"stale_version_refresh_required"},{status:409});
    const rows = snapshot.records;
    if(snapshot.excludedRecordIds.has(id))return NextResponse.json({error:"process_source_bank_scope_denied"},{status:403});
    const record = rows.find((r: any) => r.process_memory_id === id);
    if (!record) return NextResponse.json({ error: "过程记录不存在" }, { status: 404 });
    const nodeRevision=processRevisionReader(snapshot),nodeVersion=nodeRevision(id);
    if(params.has("nodeVersion") && params.get("nodeVersion")!==nodeVersion)return NextResponse.json({error:"stale_node_revision_refresh_required"},{status:409});
    const ids: string[] = [...new Set<string>([...(record.derived_from || []), ...(record.source_trace_ids || [])])].slice(0, 64);
    const sources = rows.filter((r: any) => ids.includes(r.process_memory_id)).map((row:any)=>({process_memory_id:row.process_memory_id,nodeVersion:nodeRevision(row.process_memory_id),kind:row.kind,maturity:row.maturity,text:String(row.text||row.kind).slice(0,160)}));
    return NextResponse.json({ version:snapshot.version,nodeVersion,bankId:snapshot.bankId||null,bankScope:bankScopeProjection(record,snapshot.bankId),record, sources, unresolved_source_ids: ids.filter(i => !sources.some((r: any) => r.process_memory_id === i)), source_role: "process_evidence_not_user_fact" },{headers:{"cache-control":"no-store"}});
  } catch(error) { return NextResponse.json({ error: error instanceof ScenarioRequestError ? error.message:"过程记忆数据源不可用" }, { status:error instanceof ScenarioRequestError ? error.status:503 }); }
}
