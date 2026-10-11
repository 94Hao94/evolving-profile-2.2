import { scenarioResponse } from "@/lib/scenario-data";
export async function GET(request: Request) { return scenarioResponse(request, "graph"); }
