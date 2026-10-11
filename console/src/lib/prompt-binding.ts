/** Existing missing identities remain partial; any explicit contradiction rejects. */
export function samePromptBinding(row:any,receipt:any):boolean{
 const sources=[receipt?.prompt_binding,receipt?.task_state,receipt].filter(value=>value&&typeof value==="object");
 for(const source of sources){
   for(const key of ["session_id","turn_id"]){if(source[key]&&row[key]&&source[key]!==row[key])return false;}
   for(const key of ["hook_invocation_id","check_id"]){if(source[key]&&row.hook_invocation_id&&source[key]!==row.hook_invocation_id)return false;}
 }
 return sources.some(source=>Boolean(row.hook_invocation_id&&source.hook_invocation_id===row.hook_invocation_id||row.session_id&&row.turn_id&&source.session_id===row.session_id&&source.turn_id===row.turn_id));
}
