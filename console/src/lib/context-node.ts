export type ManualSourceCoverage = {
  scopeVerdict?: string;
  reviewedSourceMessageCount?: number;
  episodeScopeVerdict?: string;
};

export type AutomatedSourceCoverage = {
  status: "reviewed" | "unverified";
  reviewedSourceMessageCount: number;
  coverageProtocol?: string;
  hostValidationVersion?: string;
  reviewTransport?: "native_agent_review_not_provider_http";
};
export type SummaryBudget = {truncated?: boolean; source_already_truncated?: boolean};
export type ContextSourceOffset = {sourcePath:string;byteOffset:number;messageId?:string};
export type ScenarioProcessing = {state:"failed"|"source_not_applicable"|"waiting_source"|"protected"|"pending";
  errorCode?:string;reason?:string;violations?:Array<{field:string;reason:string;actual?:number;minimum?:number;maximum?:number}>;
  totalViolations?:number;truncated?:boolean};
function processingStatus(row:{status?:string;error_code?:unknown;summary_failure_detail?:unknown}):ScenarioProcessing|undefined {
  const match=/^raw_available_summary_(failed|source_not_applicable|waiting_source|protected|pending)$/.exec(row.status || "");
  if(!match)return undefined;
  const result:ScenarioProcessing={state:match[1] as ScenarioProcessing["state"]};
  if(typeof row.error_code==="string" && /^(?:scenario_(?:state_invalid|state_quote_invalid|state_role_invalid|evidence_id_invalid|user_claim_not_verbatim|summary_budget_or_empty|source_incomplete|source_no_user_messages|provider_unavailable|knowledge_write_prohibited)|automated_source_(?:coverage_incomplete|coverage_budget_exceeded|revision_changed))$/.test(row.error_code)) result.errorCode=row.error_code;
  const detail=row.summary_failure_detail;
  if(!detail || typeof detail!=="object" || Array.isArray(detail))return result;
  const raw=detail as Record<string,unknown>;
  const reasons=new Set(["state_contract_violations","user_intent_field_attribution_invalid","user_intent_source_quote_invalid","user_intent_omitted","semantic_coverage_rejected","source_binding_mismatch","model_response_invalid","model_output_truncated"]);
  if(typeof raw.reason==="string" && reasons.has(raw.reason))result.reason=raw.reason;
  if(Array.isArray(raw.violations)){
    const allowed=new Set(["state_shape_invalid","claim_shape_invalid","text_invalid","text_over_limit","array_invalid","array_over_limit","message_ids_invalid","message_ids_count_invalid"]);
    const items:NonNullable<ScenarioProcessing["violations"]>=[];
    for(const item of raw.violations.slice(0,516)){
      if(!item || typeof item!=="object" || Array.isArray(item))continue;
      if(typeof item.field!=="string" || !/^(?:state|(?:subject|goal)\/(?:text|message_ids)|(?:constraints|corrections|assistant_reports|unresolved)(?:\/(?:0|[1-9][0-9]{0,2})\/(?:text|message_ids))?)$/.test(item.field) || typeof item.reason!=="string" || !allowed.has(item.reason))continue;
      const checked:NonNullable<ScenarioProcessing["violations"]>[number]={field:item.field,reason:item.reason};
      for(const key of ["actual","minimum","maximum"] as const)if(Number.isSafeInteger(item[key]) && item[key]>=0 && item[key]<=1e9)checked[key]=item[key];
      items.push(checked);
    }
    if(items.length)result.violations=items;
  }
  if(Number.isSafeInteger(raw.total_violations) && Number(raw.total_violations)>=0)result.totalViolations=Number(raw.total_violations);
  if(raw.truncated===true)result.truncated=true;
  return result;
}
type RawSourceOffset = {source_path:string;byte_offset:number;message_id?:string};
function episodeSourceLocators(episode:{source_file_ids?:string[];source_ids?:string[];source_offsets?:RawSourceOffset[]}) {
  const offsets=(episode.source_offsets || []).filter(offset=>typeof offset.source_path==="string" && offset.source_path.length>0 && Number.isSafeInteger(offset.byte_offset) && offset.byte_offset>=0);
  const declared=episode.source_file_ids?.length ? episode.source_file_ids : episode.source_ids?.length ? episode.source_ids : offsets.map(offset=>offset.source_path);
  const sourceIds=[...new Set(declared.filter(id=>typeof id==="string" && id.length>0))];
  const sourceOffsets:ContextSourceOffset[]=offsets.filter(offset=>sourceIds.includes(offset.source_path)).map(offset=>({sourcePath:offset.source_path,byteOffset:offset.byte_offset,...(typeof offset.message_id==="string" ? {messageId:offset.message_id} : {})}));
  return {sourceIds,sourceOffsets};
}
export function summaryDisplayStatus(summary: string, budget?: SummaryBudget) {
  if (budget?.truncated || budget?.source_already_truncated) return "budget_truncated";
  return summary.trimEnd().endsWith("…") ? "preview_omitted" : "complete";
}
export function selectContextEpisode<T extends {id:string;episodes?: ReadonlyArray<{id:string}>}>(node:T, episodeId:string):T {
  const episode=node.episodes?.find(row=>row.id===episodeId);
  return episode ? {...node,...episode,episodes:node.episodes} : node;
}

const EPISODE_SCOPE_LABELS: Record<string, string> = {
  single_coherent_task: "单一连贯任务",
  multiple_topics: "多主题，需拆分",
  uncertain: "范围未决",
  unresolved: "范围未决",
};

export function describeManualSourceCoverage(
  coverage?: ManualSourceCoverage,
  sourceMessageCount?: number,
  locale = "zh-CN",
): string | null {
  if (!coverage) return null;
  const parts: string[] = [];
  const english=!/^(zh|yue)/i.test(locale);
  if (coverage.scopeVerdict === "whole_session_scope_acceptable") {
    parts.push(english ? "Whole session reviewed" : "全会话已复核");
  } else if (coverage.scopeVerdict) {
    parts.push(english ? "Scope reviewed" : "范围复核");
  }
  if (typeof coverage.reviewedSourceMessageCount === "number") {
    const total = typeof sourceMessageCount === "number" ? sourceMessageCount : "?";
    parts.push(`${coverage.reviewedSourceMessageCount}/${total} ${english ? "messages" : "条消息"}`);
  }
  if (coverage.episodeScopeVerdict) {
    const englishLabels:Record<string,string>={single_coherent_task:"Single coherent task",multiple_topics:"Multiple topics; split required",uncertain:"Scope unresolved",unresolved:"Scope unresolved"};
    parts.push(english ? englishLabels[coverage.episodeScopeVerdict] || "Unverified" : EPISODE_SCOPE_LABELS[coverage.episodeScopeVerdict] ?? "待核实");
  }
  return parts.length ? parts.join(" · ") : null;
}

type SessionContextRecord = {
  context_id: string;
  session_id: string;
  project_key?: string;
  status?: string;
  error_code?: unknown;
  summary_failure_detail?: unknown;
  review_scope?: string;
  raw_source_files?: string[];
  source_message_count?: number;
  source_revision?: string;
  automated_source_coverage?: {
    accept?: boolean; whole_source_topics_covered?: boolean; corrections_preserved?: boolean;
    assistant_claims_labeled?: boolean; partition_exact?: boolean; source_revision?: string;
    reviewed_message_ids?: string[]; reviewed_episode_ids?: string[]; coverage_protocol?: string;
    model_review_receipt?: {host_validation_version?: string; input_clipped?: boolean; source_revision?: string;schema?:string;transport?:string};
  } | null;
  episodes?: Array<{episode_id:string;title?:string;status?:string;review_scope?:string;source_message_count?:number;summary?:Record<string,string>;summary_budget?:Record<string,SummaryBudget>;source_ids?:string[];source_file_ids?:string[];source_offsets?:RawSourceOffset[]}>;
  manual_source_coverage?: {
    scope_verdict?: string;
    reviewed_source_message_count?: number;
    episode_scope_verdict?: string;
  } | null;
  summary?: Record<string, string>;
  summary_budget?: Record<string, { truncated?: boolean; source_already_truncated?: boolean }>;
  source_ids?: string[];
};

export function projectSessionContextNode(row: SessionContextRecord) {
  const coverage = row.manual_source_coverage;
  const automated=row.automated_source_coverage;
  const receipt=automated?.model_review_receipt;
  const reviewedMessageCount=new Set(automated?.reviewed_message_ids || []).size;
  const automatedSourceCoverage:AutomatedSourceCoverage|undefined=automated ? {
    status:["model_reviewed","episode_directory_ready"].includes(row.status || "") && automated.accept===true && automated.whole_source_topics_covered===true && automated.corrections_preserved===true
      && automated.assistant_claims_labeled===true && automated.partition_exact===true
      && reviewedMessageCount===row.source_message_count && Boolean(row.source_revision) && automated.source_revision===row.source_revision
      && receipt?.source_revision===row.source_revision && receipt?.input_clipped===false
      && automated.coverage_protocol==="per_user_source_to_state_and_summary.v2" && receipt?.host_validation_version==="source-coverage-primary-context.v3" ? "reviewed" : "unverified",
    reviewedSourceMessageCount:reviewedMessageCount,coverageProtocol:automated.coverage_protocol,hostValidationVersion:receipt?.host_validation_version,
    ...(receipt?.schema==="evolving-profile.native-source-coverage-receipt.v1" && receipt?.transport==="native_agent_review_not_provider_http" ? {reviewTransport:"native_agent_review_not_provider_http" as const}:{}),
  } : undefined;
  return {
    id: row.context_id,
    type: "session" as const,
    label: row.session_id,
    projectKey: row.project_key,
    status: row.status,
    processing:processingStatus(row),
    reviewScope: row.review_scope,
    rawSourceCount: (row.raw_source_files ?? []).length,
    sourceMessageCount: row.source_message_count,
    automatedSourceCoverage,
    manualSourceCoverage: coverage
      ? {
          scopeVerdict: coverage.scope_verdict,
          reviewedSourceMessageCount: coverage.reviewed_source_message_count,
          episodeScopeVerdict: coverage.episode_scope_verdict,
        }
      : undefined,
    summary: row.summary ?? {},
    summaryBudget: row.summary_budget ?? {},
    sourceIds: row.source_ids ?? [],
    sourceOffsets:[] as ContextSourceOffset[],
    episodes:(row.episodes || []).map(episode=>({
      id:episode.episode_id,type:"session" as const,label:episode.title || episode.episode_id,status:episode.status,reviewScope:episode.review_scope,
      sourceMessageCount:episode.source_message_count,summary:episode.summary || {},summaryBudget:episode.summary_budget || {},...episodeSourceLocators(episode),
      manualSourceCoverage:undefined,
      automatedSourceCoverage:automatedSourceCoverage ? {...automatedSourceCoverage,
        status:automatedSourceCoverage.status==="reviewed" && episode.status==="model_reviewed" && automated?.reviewed_episode_ids?.includes(episode.episode_id) ? "reviewed" as const : "unverified" as const,
        reviewedSourceMessageCount:episode.source_message_count ?? 0} : undefined,
    })),
  };
}
