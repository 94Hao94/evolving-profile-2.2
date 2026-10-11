export function needsProcessRevalidation(row: any) {
  return ["watch", "revalidation_required", "deprecated"].includes(row.drift_status || row.status)
    || ["candidate", "unknown"].includes(row.transfer_scope?.transfer_status || row.transfer_status);
}
export function processRecordTitle(row: any) {
  if (row.kind !== "capability_observation" || (row.text && row.text !== row.kind)) return row.text || row.kind;
  const tasks=Array.isArray(row.task_archetype) ? row.task_archetype.join(", ") : row.task_archetype;
  const metric=row.capability_metric || row.capability?.metric || row.metric || row.phase;
  const model=row.model_profile?.model || row.model_profile?.model_id || row.model_profile?.family;
  return [tasks,metric,model,row.updated_at || row.created_at,row.process_memory_id].filter(Boolean).join(" · ");
}
