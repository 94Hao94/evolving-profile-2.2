import { readProcessSnapshot, processGraphPage } from "@/lib/process-graph-data";
import { ScenarioRequestError } from "@/lib/scenario-data";
export async function GET(request: Request) {
  try { const params=new URL(request.url).searchParams;return Response.json(processGraphPage(await readProcessSnapshot(params.get("bankId")), params), { headers: { "cache-control": "no-store" } }); }
  catch (error) { return Response.json({ error: error instanceof ScenarioRequestError ? error.message : "process_graph_read_failed" }, { status: error instanceof ScenarioRequestError ? error.status : 500 }); }
}
