export function modelIdentityMatches(a: any,b: any) {
  return ["model","provider","mode","dimensions","local_path"].every(key=>String(a?.[key] ?? "")===String(b?.[key] ?? ""));
}
export function maskRetrievalModels(value:any) {
  const copy=structuredClone(value);
  const visit=(node:any)=>{if(!node || typeof node!=="object")return;for(const [key,item] of Object.entries(node)) {if(key==="api_key" && typeof item==="string" && item) node[key]=`••••${item.slice(-4)}`;else visit(item);}};
  visit(copy);return copy;
}
/** Masked UI secrets mean retain the matching stored model/profile key. */
export function restoreMaskedRetrievalKeys(incoming: any, current: any, next: any) {
  for (const kind of ["embedding", "reranker", "judge"]) {
    if (typeof incoming?.[kind]?.api_key === "string" && incoming[kind].api_key.startsWith("••••")) next[kind].api_key = current?.[kind]?.api_key ?? "";
  }
  for (const kind of ["embedding_profiles", "reranker_profiles"]) {
    if (!Array.isArray(incoming?.[kind])) continue;
    for (const profile of next[kind] ?? []) {
      if (typeof profile.api_key !== "string" || !profile.api_key.startsWith("••••")) continue;
      const original = current?.[kind]?.find((stored: any) => stored.profile_id === profile.profile_id);
      profile.api_key = original?.api_key ?? "";
    }
  }
}
export function parseModelEnvironment(source:string):Record<string,string> {
  return Object.fromEntries(source.split("\n").map(line=>line.trim()).filter(line=>line && !line.startsWith("#") && line.includes("=")).map(line=>{const i=line.indexOf("=");const raw=line.slice(i+1).trim();const value=(raw.startsWith('"') && raw.endsWith('"')) || (raw.startsWith("'") && raw.endsWith("'")) ? raw.slice(1,-1) : raw;return [line.slice(0,i),value];}));
}
/** Effective identity uses only the active provider's environment namespace. */
export function resolveEffectiveEmbedding(configured:any,env:Record<string,string>) {
  const prefix="EVOLVING_PROFILE_API_EMBEDDINGS_";
  const provider=env[`${prefix}PROVIDER`] || configured.provider || "onnx";
  const local=provider==="onnx" || provider==="local";
  const namespace=provider.toUpperCase().replaceAll("-","_");
  const modelKey=provider==="onnx" ? "ONNX_MODEL_ID" : `${namespace}_MODEL`;
  const requested=env[`${prefix}${modelKey}`];
  const sameProvider=configured.provider===provider;
  const model=requested || (sameProvider ? configured.model : "") || (provider==="onnx" ? "intfloat/multilingual-e5-small" : "");
  const sameIdentity=sameProvider && configured.model===model;
  const dimensionsKey=provider==="gemini" ? "GEMINI_OUTPUT_DIMENSIONALITY" : ["cohere","litellm-sdk"].includes(provider) ? `${namespace}_OUTPUT_DIMENSIONS` : `${namespace}_DIMENSIONS`;
  const rawDimensions=env[`${prefix}${dimensionsKey}`];
  const dimensions=rawDimensions && Number.isInteger(Number(rawDimensions)) && Number(rawDimensions)>0 ? Number(rawDimensions) : provider==="onnx" ? 384 : null;
  const onnxFile=provider==="onnx" ? env[`${prefix}ONNX_MODEL_PATH`] : undefined;
  const local_path=!local ? "" : onnxFile ? onnxFile.replace(/\/[^/]+$/,"").replace(/\/onnx$/,"") : provider==="local" && String(model).startsWith("/") ? model : sameIdentity ? configured.local_path || "" : "";
  const base_url=local ? "" : env[`${prefix}${namespace}_BASE_URL`] || env[`${prefix}${namespace}_API_BASE`] || (provider==="tei" ? env[`${prefix}TEI_URL`] : "") || (sameIdentity ? configured.base_url || "" : "");
  const api_key=local ? "" : env[`${prefix}${namespace}_API_KEY`] || (sameIdentity ? configured.api_key || "" : "");
  const max_tokens=provider==="onnx" ? Number(env[`${prefix}ONNX_MAX_TOKENS`] || 512) : sameIdentity ? configured.max_tokens : undefined;
  return {...configured,provider,mode:local ? "local" : "api",model,local_path,dimensions,max_tokens,base_url,api_key};
}
export function discoverModelProposal(model: any, inventory: any[], kind: string) {
  const proposals=inventory.filter(item=>item.kind===kind).map(item=>({...item,identity_match:String(item.model).toLowerCase()===String(model.model).toLowerCase(),dimensions_match:!model.dimensions || Number(item.dimensions)===Number(model.dimensions)}));
  return {...model,discovery_proposals:proposals,status:!model.enabled ? "configured_but_inactive" : model.mode==="local" && !model.local_path ? "model_path_missing" : model.status,auto_detected:false,index_compatibility:"not_verified",service_loaded:"not_verified"};
}
