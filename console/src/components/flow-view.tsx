"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { ActionButton } from "@/components/ui/action-button";
import { useLocale, useTranslations } from "next-intl";
import { projectFlowAudit, type FlowEvidenceItem, type FlowPrompt } from "@/lib/flow-projection";
import { Dialog, DialogContent, DialogTitle, DialogDescription } from "@/components/ui/dialog";
import { RelevanceAuditDetails } from "./recall-policy-settings";
import { NavigationTopicBrowser } from "@/components/navigation-topic-browser";
import { CandidateAuditBrowser } from "@/components/candidate-audit-browser";
import { useParams } from "next/navigation";
import {
  ArrowDown,
  BookOpen,
  Check,
  GitBranch,
  History,
  Network,
  Search,
  Sparkles,
  TerminalSquare,
  RefreshCw,
} from "lucide-react";

import { inlineUiText } from "@/lib/inline-i18n";
import { ExecutionTopologyCanvas } from "@/components/execution-topology-canvas";
import { SourceNavigationDetails } from "./source-navigation-details";
import {PromptSourceSummary,type PromptSourceCounts} from "./prompt-source-summary";
type NodeId = "entry" | "map" | "guidance" | "history" | "answer";
type ToolStatus = "observed" | "window_observed" | "not_observed";
type MemoryMapNode = {
  id: string;
  label: string;
  lane: string;
  count?: number | null;
  route: string;
  freshness: string;
  summary: string;
};
type CatalogHint = {
  topic_id?: string;
  title?: string;
  abstract?: string;
  overview?: string;
  source_count?: number | null;
  source_count_semantics?: string;
  coverage?: { sampled?: number; total?: number | null; semantics?: string } | { kind?: string };
  pending_changes?: number | null;
  conflicts?: string[] | null;
  pending_changes_status?: string;
  conflict_status?: string;
  content_status?: string;
  review?: { state?: string; reason?: string };
  boundary?: string;
  memory_id?: string;
  type?: string;
  topic?: string;
  mentioned_at?: string | null;
  occurred_start?: string | null;
  occurred_end?: string | null;
  state?: string;
};
type RouteDecision = {
  decision: string;
  recommended_route: string;
  reason: string;
  confidence?: number | null;
  confidence_semantics?: string;
  matched_nodes: string[];
  catalog_probe?: {
    status?: string;
    candidate_count?: number | null;
    matched_entities?: string[];
    catalog_coverage?: string;
  };
  catalog_hints?: CatalogHint[];
  agent_may_override?: boolean;
};
const FLOW_COPY = {
  zh: {
    intro: "左侧选择用户 Prompt，中间查看本轮路径，右侧查看节点真实回执。",
    prompts: "用户 Prompt",
    empty: "尚无可展示的用户 Prompt。",
    decision: "Agent 结合原问题和前文判断：是否缺少历史依据？",
    entry: "入口提供使用说明与 L0 轻量地图；完整偏好和历史证据由 Agent 按任务需要读取。",
    answer:
      "当前记录展示入口说明、指导包和 recall/research 的实际回执；已返回并送达的内容视为本轮 Agent 可见输入，页面不再单独追踪回答侧注意力。",
    select: "选择一条 Prompt 后显示可观测回执。",
    deferredExplanation:
      "条相关偏好可按需补读。它们没有被丢弃，也没有一次性塞进上下文；只有后续行动确实依赖某项条件时，Agent 才会继续读取。",
  },
  en: {
    intro:
      "Select a user prompt on the left, inspect its path in the center, and review observable receipts on the right.",
    prompts: "User prompts",
    empty: "No user prompts are available yet.",
    decision:
      "The Agent evaluates the original question and context: is historical evidence missing?",
    entry:
      "The entry provides usage instructions and applicable preferences. The Agent decides whether historical retrieval is needed.",
    answer:
      "This record shows entry instructions, guidance packets, and recall or research receipts. Returned and delivered content is treated as visible input for the turn; the UI does not separately track answer-side attention.",
    select: "Select a prompt to view observable receipts.",
    deferredExplanation:
      "related preferences are available on demand. They were not discarded or injected all at once; the Agent reads one only when a later action depends on it.",
  },
} as const;

function displayEvidence(item: FlowEvidenceItem) {
  return item.text || item.title || item.id;
}

function displayToolLabel(tool?: string) {
  if (tool?.includes("user_preference") || tool?.includes("get_preference") || tool?.includes("get_task_guidance")) return "User Preference";
  if (tool?.includes("user_recall")) return "User Recall";
  if (tool?.includes("user_research")) return "User Research";
  if (tool?.includes("agent_recall")) return "Agent Recall";
  if (tool?.includes("agent_research")) return "Agent Research";
  if (tool?.includes("read_preference_unit") || tool?.includes("read_guidance_unit")) return "Read Preference Unit";
  if (tool?.includes("read_preference") || tool?.includes("read_guidance")) return "Read Preference";
  if (tool?.includes("search_scenario_summary") || tool?.includes("search_scenario_contexts")) return "Search Scenario Summary";
  if (tool === "read_scenario_summary") return "Scenario Summary";
  if (tool === "scenario_gate") return "Scenario Gate";
  return tool ?? inlineUiText("工具");
}

export function scenarioFreshnessLabel(status?: string | null) {
  if (status === "span_current_parent_revision_changed")
    return inlineUiText("本段来源未变 · Session 其他内容已更新");
  if (status === "stale_source_changed") return inlineUiText("来源范围已变化 · 暂不显示缓存摘要");
  if (status === "episode_source_unavailable") return inlineUiText("原始来源不可读取 · 暂不显示缓存摘要");
  if (status === "episode_not_found") return inlineUiText("Episode 定位不存在");
  if (status === "current") return inlineUiText("来源范围已核对");
  return inlineUiText("情景来源状态待核实");
}

export function observationWindowMinutes(value?: number | null) {
  return value ?? 2;
}

export function routeConfidenceLabel(route: {
  recommended_route?: string;
  confidence?: number | null;
}, english = false) {
  return typeof route.confidence === "number"
    ? (english ? `Route signal ${Math.round(route.confidence * 100)}/100 · heuristic` : `路线信号 ${Math.round(route.confidence * 100)}/100 · 启发式`)
    : inlineUiText("由当前 Agent 判断 · 不评分");
}

export function uniqueCatalogHints(hints: CatalogHint[] = []) {
  const seen = new Set<string>();
  return hints.filter((hint) => {
    const key = [hint.title ?? hint.topic ?? "", hint.abstract ?? hint.type ?? ""].join("\u0000");
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

export function catalogStatusLabel(hint: CatalogHint) {
  const values = [];
  values.push(hint.content_status === "entity_navigation_only" ? inlineUiText("结构导航") : inlineUiText("主题概览"));
  values.push(
    hint.pending_changes_status === "not_computed" || hint.pending_changes == null
      ? inlineUiText("更新状态未计算")
      : `待更新 ${hint.pending_changes}`
  );
  values.push(
    hint.conflict_status === "not_computed" || hint.conflicts == null
      ? inlineUiText("冲突未计算")
      : `冲突 ${hint.conflicts.length}`
  );
  return values.join(" · ");
}

export function entryGuidanceLabel(hasInstruction: boolean, coverage?: string) {
  if (!hasInstruction) return inlineUiText("历史记录：说明送达待核对");
  return coverage === "agent_decision_pending"
    ? inlineUiText("核心说明已生成 · 私人偏好待 Agent 判断")
    : inlineUiText("核心说明已生成 · 偏好已检查");
}

export function guidanceNodeValue(audit: ReturnType<typeof projectFlowAudit> | null, english = false) {
  if (!audit) return english ? "No verifiable read receipt was formed for this turn" : inlineUiText("本轮未形成可核验的读取回执");
  const visible = audit.guidance.count;
  const deferred = audit.guidance.deferred;
  if (visible > 0) return english ? `${visible} preference summaries injected${deferred ? ` · ${deferred} items expandable` : ""} · delivered to Agent context` : `已注入 ${visible} 项偏好摘要${deferred ? ` · ${deferred} 项可展开完整条件` : ""} · 已送达 Agent 上下文`;
  if (deferred > 0) return english ? `Candidates identified this turn · ${deferred} items expandable` : `本轮已识别候选 · ${deferred} 项可展开完整条件`;
  return english ? "No verifiable read receipt was formed for this turn" : inlineUiText("本轮未形成可核验的读取回执");
}

export function historyToolStatus(
  route: string | undefined,
  tool: "recall" | "research" | "read_source" | "search_scenario_summary" | "read_scenario_summary" | "scenario_gate",
  windowActivity?: FlowPrompt["time_window_activity"],
  toolEvents?: NonNullable<FlowPrompt["memory_route_receipt"]>["tool_events"],
): ToolStatus {
  if (toolEvents?.some((event) => event.tool === tool)) return "observed";
  if (route?.includes(tool) && !route.startsWith("system_probe")) return "observed";
  if (windowActivity?.by_tool?.[tool]?.calls)
    return windowActivity.boundary === "same_prompt_binding_only" ? "observed" : "window_observed";
  return "not_observed";
}

export function historyEmptyStateLabel(history: {
  decision?: string;
  routeReceipt?: FlowPrompt["memory_route_receipt"];
  timeWindowActivity?: FlowPrompt["time_window_activity"];
}, english = false) {
  const activity = history.timeWindowActivity;
  const navigation=history.routeReceipt?.tool_events?.reduce((count,event)=>count+(event.source_navigation_returned_count || 0),0) || 0;
  if (navigation) return inlineUiText("已返回原文导航；可回读来源核对主体与断言。",english ? "en" : undefined);
  const recall = activity?.by_tool?.recall;
  if (recall?.calls) {
    const candidates = activity?.candidate_count ?? recall.candidates ?? 0;
    return english ? `Recall receipt recorded: ${recall.calls} call(s), ${recall.returned ?? 0} returned; ${candidates} candidate(s) found${recall.returned === 0 ? ", no candidate body was returned to the Agent" : ", content preview unavailable"}.` : `已记录 Recall 回执：调用 ${recall.calls} 次，返回 ${recall.returned ?? 0} 条；候选发现 ${candidates} 条${recall.returned === 0 ? inlineUiText("，没有候选正文返回给 Agent") : inlineUiText("，内容预览尚未取得")}。`;
  }
  if (history.decision === "agent_decides") return english ? "No host-level call receipt is available, so Codex history use cannot be determined." : inlineUiText("当前页面没有宿主级调用回执，无法判断 Codex 是否调用或读取了历史结果。");
  if (history.decision === "unknown") return english ? "Recall / research / read_source receipts are missing; call status cannot be determined." : inlineUiText("当前缺少 recall / research / read_source 回执，无法判断是未调用还是回执未投影。");
  return english ? "No history-tool receipt was obtained for this turn; this does not mean the history store is empty." : inlineUiText("页面未取得本轮历史工具回执；这不等于历史库为空。");
}

function EvidenceList({
  items,
  onSelect,
}: {
  items: FlowEvidenceItem[];
  onSelect: (item: FlowEvidenceItem) => void;
}) {
  if (!items.length)
    return <p className="text-sm text-muted-foreground">{inlineUiText("本轮没有可逐项展示的回执内容。")}</p>;
  return (
    <ul className="space-y-2 text-sm">
      {items.slice(0, 8).map((item) => (
        <li key={item.id} className="border-l-2 border-border pl-3 leading-6">
          <button onClick={() => onSelect(item)} className="w-full text-left hover:text-primary">
            {displayEvidence(item)}
            {typeof item.score === "number" && (
              <span className="ml-2 text-xs text-muted-foreground">{item.score.toFixed(2)}</span>
            )}
          </button>
        </li>
      ))}
      {items.length > 8 && (
        <li className="text-xs text-muted-foreground">
          {inlineUiText("其余")} {items.length - 8} {inlineUiText("项已保留在本次回执中。")}
        </li>
      )}
    </ul>
  );
}

function ToolRail({
  history,
}: {
  history?: {
    route?: string;
    state?: string;
    decision?: string;
    metrics?: { candidates?: number | null; returned?: number | null; unread?: number | null };
    items?: FlowEvidenceItem[];
    routeReceipt?: FlowPrompt["memory_route_receipt"];
    timeWindowActivity?: FlowPrompt["time_window_activity"];
    timeWindowGuidanceActivity?: FlowPrompt["time_window_guidance_activity"];
    preferenceItems?: FlowEvidenceItem[];
    preferenceCount?: number;
  };
}) {
  const locale = useLocale();
  const english = !locale.startsWith("zh");
  const route = history?.route ?? "";
  const items = history?.items ?? [];
  const toolEvents = history?.routeReceipt?.tool_events ?? [];
  const windowActivity = history?.timeWindowActivity;
  const windowGuidanceActivity = history?.timeWindowGuidanceActivity;
  const preferenceItems = history?.preferenceItems ?? [];
  const candidates = history?.metrics?.candidates ?? null;
  const returned = history?.metrics?.returned ?? null;
  const typeCounts = items.reduce<Record<string, number>>((counts, item) => {
    const key = item.type ?? inlineUiText("未分类");
    counts[key] = (counts[key] ?? 0) + 1;
    return counts;
  }, {});
  const normalized = route ?? "";
  const tools = [
    { id: "recall" as const, label: "recall", caption: inlineUiText("候选召回"), icon: Search },
    { id: "research" as const, label: "research", caption: inlineUiText("复杂关联"), icon: GitBranch },
    { id: "read_source" as const, label: "read_source", caption: inlineUiText("原文回读"), icon: BookOpen },
    { id: "search_scenario_summary" as const, label: "search_scenario_summary", caption: inlineUiText("情景定位"), icon: Network },
    { id: "read_scenario_summary" as const, label: "read_scenario_summary", caption: inlineUiText("情景摘要下钻"), icon: Network },
    { id: "scenario_gate" as const, label: "scenario_gate", caption: inlineUiText("情景判断"), icon: GitBranch },
  ];
  const statusText: Record<ToolStatus, string> = {
    observed: inlineUiText("调用回执已记录"),
    window_observed: inlineUiText("时间窗观测到活动"),
    not_observed: inlineUiText("未取得调用回执"),
  };
  return (
    <div className="mt-4 rounded-xl border border-emerald-200/80 bg-emerald-50/60 p-3 dark:border-emerald-900/70 dark:bg-emerald-950/20">
      <div className="mb-2 flex items-center justify-between gap-3">
        <div className="flex items-center gap-2 text-xs font-semibold uppercase tracking-[0.12em] text-emerald-800 dark:text-emerald-300">
          <TerminalSquare className="h-3.5 w-3.5" />
          {inlineUiText("工具轨道")}
        </div>
        <span className="text-[11px] text-emerald-700/80 dark:text-emerald-300/80">
          {inlineUiText("历史可补充依据时主动读取")}
        </span>
      </div>
      {toolEvents.length > 0 && (
        <div className="mb-3 rounded-lg border border-emerald-200/80 bg-background/70 p-2.5 text-[11px] dark:border-emerald-900/60">
          <div className="font-semibold text-emerald-800 dark:text-emerald-300">{inlineUiText("已记录的工具调用")}</div>
          <div className="mt-1 space-y-1.5">
            {toolEvents.slice(-6).map((event, index) => (
              <div key={`${event.tool}-${event.at ?? index}`} className="flex flex-wrap gap-x-2 gap-y-0.5 text-muted-foreground">
                <span className="font-medium text-foreground">{displayToolLabel(event.tool)}</span>
                {event.at && <span>{new Date(event.at).toLocaleString(english ? "en-US" : locale)}</span>}
                {event.returned_count != null && <span>{inlineUiText("返回")} {event.returned_count} 条</span>}
                {event.source_navigation_returned_count != null ? <span>{inlineUiText("原文导航返回数")} {event.source_navigation_returned_count}</span> : null}
                {event.candidate_count != null && <span>{inlineUiText("发现候选")} {event.candidate_count} 条</span>}
                {event.memory_id && <span className="break-all">memory_id {event.memory_id}</span>}
                {event.memory_ids?.length ? <span className="break-all">{inlineUiText("读取")} {event.memory_ids.slice(0, 6).join("、")}</span> : null}
                {event.scenario_ids?.length ? <span className="break-all">{inlineUiText("情景")} {event.scenario_ids.slice(0, 4).join("、")}</span> : null}
                {event.scenario_episode_count != null && <span>Episode {event.scenario_episode_count} 段</span>}
                {event.scenario_episodes?.length ? <span className="break-words">{inlineUiText("目录")} {event.scenario_episodes.slice(0, 4).map((episode) => episode.title || episode.episode_id).filter(Boolean).join("、")}</span> : null}
                {event.scenario_episode_id && <span className="break-all">{inlineUiText("已读")} {event.scenario_episode_title || event.scenario_episode_id} · {event.scenario_tier || "compact"}</span>}
                {event.scenario_summary_status && event.scenario_summary_status !== "current" && <span>{scenarioFreshnessLabel(event.scenario_summary_status)}</span>}
                {event.scenario_summary_text && (
                  <details className="basis-full border-t border-border/70 pt-1.5">
                    <summary className="cursor-pointer select-none text-emerald-800 dark:text-emerald-300">
                      {inlineUiText("查看已读取情景内容 ·")} {event.scenario_tier || "compact"}
                    </summary>
                    <div className="mt-1 whitespace-pre-wrap break-words leading-5 text-muted-foreground">
                      {event.scenario_summary_text}
                    </div>
                    {event.scenario_summary_truncated && <div className="mt-1 text-muted-foreground">{inlineUiText("回执正文已达到显示上限。")}</div>}
                  </details>
                )}
                {event.scenario_navigation_roles?.length ? <span>{inlineUiText("候选角色")} {event.scenario_navigation_roles.slice(0, 3).map(([id, role]) => `${role}:${id}`).join("、")}</span> : null}
                {event.scope_hypothesis_count != null && <span>{inlineUiText("竞争假设")} {event.scope_hypothesis_count} 个</span>}
                {event.scope_route_policy?.defer_bank_retrieval_until_scope_check && <span className="font-medium text-amber-700 dark:text-amber-300">{inlineUiText("先核对情景，再检索Bank")}</span>}
                {event.scenario_decision ? <span>{inlineUiText("建议")} {event.scenario_decision}</span> : null}
                {event.source_navigation?.length ? <div className="basis-full">{event.source_navigation.map(locator=><SourceNavigationDetails key={locator.memory_id} locator={locator}/>)}</div> : null}
              </div>
            ))}
          </div>
        </div>
      )}
      {windowActivity?.state === "observed" && (
        <div className="mb-3 rounded-lg border border-amber-200/80 bg-amber-50/70 p-2.5 text-[11px] dark:border-amber-900/60 dark:bg-amber-950/20">
          <div className="font-semibold text-amber-900 dark:text-amber-200">
            {inlineUiText("Prompt 后时间窗观测 ·")} {observationWindowMinutes(windowActivity.window_minutes)} {inlineUiText("分钟内")}
          </div>
          <div className="mt-1 text-muted-foreground">
            {windowActivity.boundary === "same_prompt_binding_only" ? inlineUiText("以下调用回执已通过 check_id 绑定到当前 Prompt。") : inlineUiText("仅表示该时间段内观察到的活动，不归因于当前 Prompt。")}
          </div>
          {windowActivity.start && windowActivity.end && (
            <div className="mt-1 text-muted-foreground">
              {new Date(windowActivity.start).toLocaleTimeString(english ? "en-US" : locale)} – {new Date(windowActivity.end).toLocaleTimeString(english ? "en-US" : locale)}
            </div>
          )}
          <div className="mt-2 flex flex-wrap gap-1.5">
            {Object.entries(windowActivity.by_tool ?? {}).map(([tool, summary]) => (
              <span key={tool} className="rounded-full bg-amber-100 px-2 py-0.5 text-amber-900 dark:bg-amber-900/60 dark:text-amber-100">
                {tool} {summary.calls ?? 0} {inlineUiText("次 · 返回")} {summary.returned ?? 0} 条
              </span>
            ))}
          </div>
          {windowActivity.unattributed_activity?.event_count ? (
            <div className="mt-2 rounded-md border border-amber-300/80 bg-amber-100/60 px-2 py-1.5 text-amber-900 dark:border-amber-800 dark:bg-amber-900/30 dark:text-amber-100">
              {inlineUiText("另有")} {windowActivity.unattributed_activity.event_count} {inlineUiText("次活动无法绑定到当前 Prompt；只计数，不展示候选内容。")}
            </div>
          ) : null}
          {windowActivity.items?.length ? <div className="mt-2 space-y-1.5">{windowActivity.items.slice(0, 8).map((item) => <div key={item.id} className="rounded-md bg-background/80 px-2 py-1.5 leading-4 text-muted-foreground"><span className="mr-1 font-medium text-foreground">{item.type ?? "memory"}</span>{displayEvidence(item)}</div>)}</div> : null}
          {windowActivity.candidate_queries?.map((query, index) => <div key={index} className="mt-2 border-t border-amber-200 pt-2 leading-5 break-words"><div className="font-medium">{inlineUiText("查询")} {index + 1} {inlineUiText("· 发现")} {query.candidate_count ?? inlineUiText("未知")} {inlineUiText("条候选")}</div><div>{query.query}</div>{query.anchors?.length ? <div className="text-muted-foreground">{inlineUiText("当时的过滤锚点：")}{query.anchors.join("、")}</div> : null}</div>)}
          {windowActivity.candidate_items?.length ? (
            <div className="mt-2 rounded-md border border-amber-300/80 bg-amber-100/50 p-2 dark:border-amber-800 dark:bg-amber-900/20">
              <div className="font-medium text-amber-900 dark:text-amber-100">{inlineUiText("后台检索候选预览 · 不属于本次已返回正文（")}{windowActivity.candidate_count ?? windowActivity.candidate_items.length} {inlineUiText("条去重候选）")}</div>
              <div className="mt-1 text-[10px] text-amber-800/80 dark:text-amber-200/80">{inlineUiText("这里只显示检索候选，不代表已注入或被采用。")}</div>
              <div className="mt-1.5 space-y-1.5">{windowActivity.candidate_items.slice(0, 8).map((item) => <div key={item.id} className="rounded-md bg-background/80 px-2 py-1.5 leading-4 break-words text-muted-foreground"><span className="mr-1 font-medium text-foreground">{item.type ?? "memory"}</span>{displayEvidence(item)}</div>)}</div>
            </div>
          ) : null}
        </div>
      )}
      {windowActivity?.state === "not_observed" && (
        <div className="mb-3 rounded-lg border border-dashed border-slate-300 bg-slate-50/70 p-2.5 text-[11px] text-muted-foreground dark:border-slate-700 dark:bg-slate-900/30">
          {inlineUiText("Prompt 后")} {observationWindowMinutes(windowActivity.window_minutes)} {inlineUiText("分钟内未观测到工具回执；这不等于工具一定没有调用。")}
        </div>
      )}
      {windowGuidanceActivity?.state === "observed" && (
        <div className="mb-3 rounded-lg border border-violet-200/80 bg-violet-50/70 p-2.5 text-[11px] dark:border-violet-900/60 dark:bg-violet-950/20">
          <div className="font-semibold text-violet-900 dark:text-violet-200">{inlineUiText("Prompt 后 Get Preference 观测 ·")} {observationWindowMinutes(windowGuidanceActivity.window_minutes)} {inlineUiText("分钟内")}</div>
          <div className="mt-1 text-muted-foreground">{windowGuidanceActivity.boundary === "same_prompt_binding_only" ? inlineUiText("本轮已绑定调用") : inlineUiText("时间窗内调用")}{inlineUiText("累计返回")} {windowGuidanceActivity.returned_count ?? 0} {inlineUiText("项指导；分页重复项未去重。")}</div>
          {windowGuidanceActivity.unattributed_activity?.event_count ? <div className="mt-1 rounded-md border border-violet-300/80 bg-violet-100/60 px-2 py-1.5 text-violet-900 dark:border-violet-800 dark:bg-violet-900/30 dark:text-violet-100">{inlineUiText("另有")} {windowGuidanceActivity.unattributed_activity.event_count} {inlineUiText("次指导活动无法绑定到当前 Prompt；只计数，不展示条目。")}</div> : null}
          {preferenceItems.length ? <div className="mt-2 space-y-1.5">{preferenceItems.slice(0, 6).map((item) => <div key={item.id} className="rounded-md bg-background/80 px-2 py-1.5 leading-4 text-muted-foreground">{displayEvidence(item)}</div>)}</div> : null}
        </div>
      )}
      <div className="grid grid-cols-2 gap-2 lg:grid-cols-4">
        {tools.map(({ id, label, caption, icon: Icon }) => {
          const state = historyToolStatus(normalized, id, windowActivity, toolEvents);
          const windowSummary = windowActivity?.by_tool?.[id];
          const boundEvents = toolEvents.filter((event) => event.tool === id);
          const count =
            boundEvents.length
              ? `${boundEvents.length} 次 · 返回 ${boundEvents.reduce((sum, event) => sum + (event.returned_count ?? 0), 0)} 条`
              : windowSummary?.calls
                ? `${windowSummary.calls} 次 · 返回 ${windowSummary.returned ?? 0} 条`
              : state === "observed"
              ? candidates != null
                ? `${candidates} 个候选`
                : `${returned ?? items.length} 条预览`
              : state === "window_observed"
                ? `${windowSummary?.calls ?? 0} 次（时间窗）`
              : inlineUiText("无法确认是否调用");
          return (
            <div
              key={id}
              className="rounded-lg border border-emerald-200/80 bg-background/90 p-2.5 dark:border-emerald-900/60"
            >
              <div className="flex items-center gap-1.5 text-xs font-semibold">
                <Icon className="h-3.5 w-3.5 text-emerald-600" />
                {label}
              </div>
              <div className="mt-1 text-[11px] text-muted-foreground">{caption}</div>
              <div className="mt-1 text-[11px] font-medium tabular-nums text-foreground/80">
                {count}
              </div>
              <div
                className={`mt-2 inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-medium ${state === "observed" ? "bg-emerald-600 text-white" : state === "window_observed" ? "bg-amber-500 text-white" : "bg-emerald-100 text-emerald-800 dark:bg-emerald-900/70 dark:text-emerald-200"}`}
              >
                {state === "observed" || state === "window_observed" ? (
                  <Check className="h-3 w-3" />
                ) : (
                  <span className="h-1.5 w-1.5 rounded-full bg-current opacity-70" />
                )}
                {statusText[state]}
              </div>
            </div>
          );
        })}
      </div>
      {items.length > 0 ? (
        <div className="mt-3 border-t border-emerald-200/80 pt-2 dark:border-emerald-900/60">
          <div className="mb-1 text-[10px] font-semibold uppercase tracking-[0.12em] text-emerald-800 dark:text-emerald-300">
            {inlineUiText("已返回内容预览 ·")} {items.length} 条
          </div>
          <div className="mb-2 flex flex-wrap gap-1">
            {Object.entries(typeCounts).map(([type, count]) => (
              <span
                key={type}
                className="rounded-full bg-emerald-100/70 px-2 py-0.5 text-[10px] text-emerald-800 dark:bg-emerald-950/60 dark:text-emerald-200"
              >
                {type} {count}
              </span>
            ))}
          </div>
          <div className="space-y-1.5">
            {items.slice(0, 2).map((item) => (
              <div
                key={item.id}
                className="line-clamp-2 rounded-md bg-background/70 px-2 py-1.5 text-[11px] leading-4 text-muted-foreground"
              >
                {displayEvidence(item)}
              </div>
            ))}
          </div>
          {items.length > 2 && (
            <div className="mt-1 text-[10px] text-muted-foreground">
              {inlineUiText("其余")} {items.length - 2} {inlineUiText("条可在右侧详情查看")}
            </div>
          )}
        </div>
      ) : (
        <div className="mt-3 rounded-md border border-dashed border-emerald-200/80 bg-background/50 px-2.5 py-2 text-[11px] leading-4 text-muted-foreground dark:border-emerald-900/60">
          {historyEmptyStateLabel({ decision: history?.decision, routeReceipt:history?.routeReceipt, timeWindowActivity: windowActivity }, english)}
        </div>
      )}
    </div>
  );
}

export function FlowView() {
  const flowText=useTranslations("releaseUi");
  const locale = useLocale();
  const english = !locale.startsWith("zh");
  const copy = FLOW_COPY[locale.startsWith("zh") ? "zh" : "en"];
  const [rows, setRows] = useState<FlowPrompt[]>([]);
  const [selected, setSelected] = useState<FlowPrompt | null>(null);
  const [selectedDetail, setSelectedDetail] = useState<FlowPrompt | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [active, setActive] = useState<NodeId>("entry");
  const [selectedEvidence, setSelectedEvidence] = useState<FlowEvidenceItem | null>(null);
  const [manualOpen, setManualOpen] = useState(false);
  const [mapOpen, setMapOpen] = useState(false);
  const [nodeOpen, setNodeOpen] = useState(false);
  const [latestNavigation, setLatestNavigation] = useState<FlowPrompt["navigation_map"] | null>(null);
  const [mapMode, setMapMode] = useState<"recorded" | "latest">("recorded");
  const [browseTopic, setBrowseTopic] = useState<string | null>(null);
  const params = useParams<{ bankId: string }>();
  const [cursor, setCursor] = useState(0);
  const [query, setQuery] = useState("");
  const [host, setHost] = useState("all");
  const [promptSource,setPromptSource]=useState("natural");
  const [sourceCounts,setSourceCounts]=useState<PromptSourceCounts>({natural:null,audit:null});
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [memoryMap, setMemoryMap] = useState<MemoryMapNode[]>([]);
  const [routeDecision, setRouteDecision] = useState<RouteDecision | null>(null);
  // The former legacy chain is intentionally hidden; the receipt-aware topology is the only flow surface.
  const showLegacyFlow = false;
  // The prompt-list response is only a navigation preview. Do not project its
  // partial counts as actual memory delivery while the prompt-bound detail
  // receipt is still loading; otherwise an older/summary candidate can appear
  // as a real injection for the newly selected prompt.
  const currentRow = selectedDetail?.prompt_id === selected?.prompt_id ? selectedDetail : null;
  const navigation = mapMode === "latest" ? latestNavigation : currentRow?.navigation_map;
  useEffect(() => {
    if (!mapOpen) return;
    const abort = new AbortController();
    fetch("/api/evolving-profile/guidance/memory-map", { signal: abort.signal })
      .then(r => r.json()).then(value => setLatestNavigation(value.navigation ?? null))
      .catch(() => setLatestNavigation(null));
    return () => abort.abort();
  }, [mapOpen]);

  const loadPrompts = useCallback(async (signal?: AbortSignal) => {
    setLoading(true);
    setError(null);
    const search = query.trim() ? `&q=${encodeURIComponent(query.trim())}` : "";
    try {
      const response = await fetch(
      `/api/evolving-profile/guidance/prompts?limit=20&cursor=${cursor}&host=${encodeURIComponent(host)}&prompt_source=${encodeURIComponent(promptSource)}${search}`,
      { cache: "no-store", signal }
      );
      if (!response.ok) throw new Error("prompt_list_unavailable");
      const payload = await response.json();
      const nextRows: FlowPrompt[] = payload.items ?? [];
        setRows(nextRows);
        setSelected((previous) => nextRows.find((row: FlowPrompt) => row.prompt_id === previous?.prompt_id) ?? nextRows[0] ?? null);
        setHasMore(Boolean(payload.has_more));
        setSourceCounts({natural:typeof payload.natural_total==="number"?payload.natural_total:null,audit:typeof payload.audit_total==="number"?payload.audit_total:null,
          verification:payload.source_verification_status==="partial" || payload.source_verification_status==="complete" ? payload.source_verification_status:null,
          pending:typeof payload.source_verification_pending_total==="number"?payload.source_verification_pending_total:null});
      return nextRows;
    } catch (cause) {
      if (!(cause instanceof Error && cause.name === "AbortError")) setError(inlineUiText("链路记录暂时不可读取。请刷新后重试。"));
      throw cause;
    } finally { if (!signal?.aborted) setLoading(false); }
  }, [cursor, query, host,promptSource]);

  const refresh = async () => {
    const nextRows = await loadPrompts();
    const prompt = nextRows.find((row) => row.prompt_id === selected?.prompt_id) ?? nextRows[0];
    if (!prompt) return;
    setDetailLoading(true);
    try {
      const response = await fetch(`/api/evolving-profile/guidance/prompts/${encodeURIComponent(prompt.prompt_id)}`, { cache: "no-store" });
      if (!response.ok) throw new Error("prompt_detail_unavailable");
      const detail = await response.json();
      if (detail?.prompt_id !== prompt.prompt_id) throw new Error("prompt_detail_mismatch");
      setSelectedDetail(detail);
    } finally { setDetailLoading(false); }
  };

  useEffect(() => {
    const controller = new AbortController();
    void loadPrompts(controller.signal).catch(() => undefined);
    return () => controller.abort();
  }, [loadPrompts]);

  useEffect(() => {
    const controller = new AbortController();
    fetch("/api/evolving-profile/guidance/memory-map", {
      cache: "no-store",
      signal: controller.signal,
    })
      .then((response) => (response.ok ? response.json() : null))
      .then((payload) => setMemoryMap(payload?.nodes ?? []))
      .catch((cause) => {
        if (cause.name !== "AbortError") setMemoryMap([]);
      });
    return () => controller.abort();
  }, []);

  useEffect(() => {
    if (!currentRow?.user_prompt) {
      setRouteDecision(null);
      return;
    }
    const recorded = currentRow.memory_route_receipt;
    if (recorded?.recommended_route && recorded.reason) {
      setRouteDecision({
        decision: recorded.decision ?? recorded.recommended_route,
        recommended_route: recorded.recommended_route,
        reason: recorded.reason,
        confidence: recorded.confidence,
        confidence_semantics: recorded.confidence_semantics,
        matched_nodes: recorded.matched_nodes ?? [],
        catalog_probe: recorded.catalog_probe,
        catalog_hints: recorded.catalog_hints ?? [],
        agent_may_override: recorded.agent_may_override,
      });
      return;
    }
    const controller = new AbortController();
    fetch(
      `/api/evolving-profile/guidance/memory-check?q=${encodeURIComponent(currentRow.user_prompt)}`,
      { cache: "no-store", signal: controller.signal }
    )
      .then((response) => (response.ok ? response.json() : null))
      .then((payload) => setRouteDecision(payload ?? null))
      .catch((cause) => {
        if (cause.name !== "AbortError") setRouteDecision(null);
      });
    return () => controller.abort();
  }, [currentRow?.user_prompt, currentRow?.memory_route_receipt]);

  useEffect(() => {
    if (!selected?.prompt_id) { setDetailLoading(false); return; }
    const controller = new AbortController();
    setDetailLoading(true);
    setSelectedDetail(null);
    fetch(`/api/evolving-profile/guidance/prompts/${encodeURIComponent(selected.prompt_id)}`, {
      cache: "no-store",
      signal: controller.signal,
    })
      .then((response) => (response.ok ? response.json() : null))
      .then((payload) => {
        if (payload?.prompt_id === selected.prompt_id) setSelectedDetail(payload);
      })
      .catch((cause) => {
        if (cause.name !== "AbortError") setSelectedDetail(null);
      })
      .finally(() => { if (!controller.signal.aborted) setDetailLoading(false); });
    return () => controller.abort();
  }, [selected?.prompt_id]);

  const audit = useMemo(() => (currentRow ? projectFlowAudit(currentRow) : null), [currentRow]);
  const entryStateLabel = (value?: string) =>
    value === "observed_entry_adapter" ? inlineUiText("入口已检查") : inlineUiText("入口状态待确认");
  const historyStateLabel = (value?: string) =>
    value?.startsWith("system_probe") ? inlineUiText("后台系统探测") : value?.includes("recall") || value?.includes("research") ? inlineUiText("已记录历史调用") : inlineUiText("未观测历史工具");
  const controllerLabel = (value?: string) =>
    ({
      same_turn_host_receipt: inlineUiText("同回合宿主回执"),
      admission_applied: inlineUiText("入口准入"),
      not_in_candidate_path: inlineUiText("Agent 自主调用"),
      system_probe_direct: inlineUiText("系统有界探测"),
      not_used: inlineUiText("本轮未调用"),
    })[value ?? ""] ?? inlineUiText("状态待确认");
  const coverageLabel = (value?: string) =>
    value?.includes("deferred")
      ? inlineUiText("部分内容可按需补读")
      : value === "complete_active_set"
        ? inlineUiText("本轮所需指导已提供")
        : inlineUiText("指导覆盖待确认");
  const historyValue =
    audit?.history.calls
      ? (english ? `Observed · ${audit.history.calls} call(s) · ${audit.history.metrics.returned ?? "—"} returned` : `已观测 · ${audit.history.calls} 次调用 · 返回 ${audit.history.metrics.returned ?? "—"} 条`) + (audit.history.routeAudit.source_navigation_returned_count != null ? ` · ${flowText("sourceNavigationReturned")} ${audit.history.routeAudit.source_navigation_returned_count}` : "")
    : audit?.systemProbe?.calls
      ? (english ? `System probe observed · ${audit.systemProbe.calls} call(s) · ${audit.systemProbe.candidate_count ?? "unknown"} candidate(s)` : `已观测系统探测 · ${audit.systemProbe.calls} 次 · 候选 ${audit.systemProbe.candidate_count ?? inlineUiText("未知")} 条`)
    : !audit || audit.history.decision === "agent_decides"
      ? inlineUiText("未观测 · 点击查看回执")
    : audit.history.value === "unknown"
      ? inlineUiText("未核验 · 缺少历史回执")
    : audit.history.value === "not_observed"
      ? inlineUiText("已确认未调用历史工具")
    : audit.history.value === "executed_empty"
      ? inlineUiText("已调用 · 返回 0 条候选")
    : audit.history.value === "executed_no_result"
      ? inlineUiText("已执行 · 未形成结果回执")
      : (english ? `Observed · ${audit.history.value} · ${audit.history.metrics.returned ?? "—"} returned` : `已观测 · ${audit.history.value} · 返回 ${audit.history.metrics.returned ?? "—"} 条`);
  const isLegacyAutoHistory = audit?.history.mode === "hook_auto_recall";
  const historyTitle = isLegacyAutoHistory ? inlineUiText("历史记录 · Hook 自动召回") : inlineUiText("历史读取 · 系统探测与 Agent 下钻");
  const nodes: Array<{ id: NodeId; title: string; value: string; tone: string }> = [
    {
      id: "entry",
      title: inlineUiText("UserPromptSubmit · 说明与偏好入口"),
      value: entryGuidanceLabel(Boolean(audit?.entry.instruction), audit?.entry.coverage),
      tone: "border-sky-400",
    },
    {
      id: "map",
      title: inlineUiText("Memory Map · L0 总览"),
      value: audit?.map.l0.status === "observed"
        ? (english ? `${audit.map.l0.preferenceCount ?? "—"} preferences · ${audit.map.l0.topicCount ?? "—"} Bank topics · ${audit.map.l0.contextChars ?? "—"} characters` : `${audit.map.l0.preferenceCount ?? "—"} 条偏好 · ${audit.map.l0.topicCount ?? "—"} 个 Bank 主题 · ${audit.map.l0.contextChars ?? "—"} 字符`)
        : inlineUiText("该回合未保存 L0 地图回执"),
      tone: "border-cyan-500",
    },
    {
      id: "guidance",
      title: "Get Preference · L1",
      value: guidanceNodeValue(audit, english),
      tone: "border-amber-400",
    },
    { id: "history", title: historyTitle, value: historyValue, tone: "border-emerald-400" },
    {
      id: "answer",
      title: inlineUiText("Agent 判断与回答"),
      value: inlineUiText("已返回内容视为本轮可见输入 · 不追踪回答侧注意力"),
      tone: "border-rose-400",
    },
  ];
  const activeNode = nodes.find((node) => node.id === active) ?? nodes[0];
  const stageLabels = [inlineUiText("入口"), inlineUiText("L0 总览"), inlineUiText("L1 目录"), inlineUiText("L2 证据"), inlineUiText("回答")];
  const catalogHints = uniqueCatalogHints(routeDecision?.catalog_hints ?? []);

  return (
    <section className="space-y-5">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <div className="flex items-center gap-2">
            <div className="flex h-9 w-9 items-center justify-center rounded-xl bg-slate-900 text-white shadow-sm dark:bg-slate-100 dark:text-slate-900">
              <Sparkles className="h-4 w-4" />
            </div>
            <div>
              <div className="flex items-center gap-2">
                <h1 className="text-2xl font-semibold tracking-tight">{english ? "Flow" : inlineUiText("链路")}</h1>
                <span className="rounded-full border border-sky-200 bg-sky-50 px-2 py-0.5 text-[10px] font-semibold text-sky-700 dark:border-sky-900/70 dark:bg-sky-950/30 dark:text-sky-300">
                  {english ? "EP 5.1 development" : inlineUiText("EP 5.1 开发态")}
                </span>
              </div>
              <p className="mt-0.5 text-sm text-muted-foreground">{english ? "Observable evidence path from prompt to answer" : inlineUiText("从 Prompt 到回答的可观测证据图")}</p>
            </div>
          </div>
        </div>
        <div className="flex flex-wrap items-center justify-end gap-2">
          <label className="flex items-center gap-2 rounded-full border bg-background px-3 py-1.5 text-xs text-muted-foreground">
            <span>{flowText("promptSourceFilter")}</span>
            <select aria-label={flowText("promptSourceFilter")} value={promptSource} onChange={event=>{setCursor(0);setPromptSource(event.target.value);}} className="bg-transparent font-medium text-foreground outline-none">
              {[['natural','promptSourceNatural'],['all','promptSourceAll'],['subagent','promptSourceSubagent'],['automation','promptSourceAutomation'],['memory-maintenance','promptSourceMaintenance'],['unknown','promptSourceUnknown']].map(([value,key])=><option key={value} value={value}>{flowText(key)}</option>)}
            </select>
          </label>
          <label className="flex items-center gap-2 rounded-full border bg-background px-3 py-1.5 text-xs text-muted-foreground shadow-sm">
            <span>{english ? "Host" : inlineUiText("宿主")}</span>
            <select
              value={host}
              onChange={(event) => {
                setCursor(0);
                setHost(event.target.value);
              }}
              className="bg-transparent font-medium text-foreground outline-none"
            >
              <option value="all">{english ? "All" : inlineUiText("全部")}</option>
              <option value="codex">Codex</option>
              <option value="hermes">Hermes</option>
              <option value="claude-code">{inlineUiText("Claude Code")}</option>
              <option value="codex-cli">{inlineUiText("Codex CLI")}</option>
            </select>
          </label>
          <PromptSourceSummary counts={sourceCounts}>
            {loading ? (english ? "Syncing" : inlineUiText("正在同步")) : `${rows.length} ${english ? "items" : inlineUiText("条")} / ${english ? "page" : inlineUiText("当前页")} ${Math.floor(cursor / 20) + 1}`}
          </PromptSourceSummary>
        </div>
      </div>

      <div className="grid min-w-0 gap-4 lg:grid-cols-[240px_minmax(0,1fr)]">
        <aside className="min-w-0 overflow-hidden rounded-2xl border bg-background shadow-sm">
          <div className="border-b bg-muted/20 px-3 py-3">
            <div className="flex items-center justify-between text-xs font-medium text-muted-foreground">
              <span className="flex items-center gap-2">
                {english ? copy.prompts : inlineUiText(copy.prompts)}
                <ActionButton
                  variant="outline"
                  type="button"
                  size="icon"
                  aria-label={flowText("refreshPromptFlow")}
                  title={flowText("refreshPromptFlow")}
                  onAction={refresh}
                  className="inline-flex h-6 w-6 items-center justify-center rounded-md border bg-background text-foreground hover:bg-muted"
                >
                  <RefreshCw className="h-3.5 w-3.5" />
                </ActionButton>
              </span>
              <span>{english ? `Total ${rows.length || "-"} items` : `共 ${rows.length || "-"} 条`}</span>
            </div>
            <input
              value={query}
              onChange={(event) => {
                setCursor(0);
                setQuery(event.target.value);
              }}
              placeholder={inlineUiText("搜索用户 Prompt")}
              className="mt-2 h-9 w-full rounded-lg border bg-background px-3 text-sm outline-none transition focus:border-primary focus:ring-2 focus:ring-primary/20"
            />
          </div>
          <div className="max-h-[68vh] overflow-y-auto">
            {loading && <div className="p-3 text-sm text-muted-foreground">{inlineUiText("正在读取链路记录…")}</div>}
            {error && <div className="p-3 text-sm text-destructive">{error}</div>}
            {!loading && !error && rows.length === 0 && (
              <div className="p-3 text-sm text-muted-foreground">{flowText(promptSource==="natural" ? "promptSourceEmptyNatural":"promptSourceEmptyFiltered")}</div>
            )}
            {!loading &&
              !error &&
              rows.map((row) => (
                <button
                  key={row.prompt_id}
                  onClick={() => {
                    setSelected(row);
                    setActive("entry");
                  }}
                  className={`w-full border-b p-3 text-left text-sm transition-colors hover:bg-muted/60 ${selected?.prompt_id === row.prompt_id ? "bg-muted" : ""}`}
                >
                  <div className="text-xs text-muted-foreground">
                    {new Date(row.at).toLocaleString(english ? "en-US" : locale)}
                  </div>
                  <div className="mt-1 line-clamp-3 leading-5">{row.user_prompt}</div>
                  <div className="mt-2 flex gap-2 text-[11px] text-muted-foreground">
                    <span>{entryStateLabel(row.routes?.entry_guidance)}</span>
                    <span>
                      {(row.prompt_id === currentRow?.prompt_id ? currentRow : row)?.memory_route_receipt?.tool_events?.length
                        ? (english ? "Recorded tool calls" : "已记录工具调用")
                        : historyStateLabel(
                        row.prompt_id === currentRow?.prompt_id
                          ? projectFlowAudit(currentRow).history.value
                          : projectFlowAudit(row).history.value
                      )}
                    </span>
                  </div>
                </button>
              ))}
          </div>
          <div className="flex justify-between border-t p-3 text-sm">
            <button
              disabled={cursor === 0}
              onClick={() => setCursor(Math.max(0, cursor - 20))}
              className="disabled:opacity-40"
            >
              {inlineUiText("上一页")}
            </button>
            <button
              disabled={!hasMore}
              onClick={() => setCursor(cursor + 20)}
              className="disabled:opacity-40"
            >
              {inlineUiText("下一页")}
            </button>
          </div>
        </aside>

        <main className="min-w-0 rounded-2xl border bg-gradient-to-b from-slate-50/80 to-background p-2 shadow-sm sm:p-3 dark:from-slate-950/50">
          <div>
            <div className="mb-5">
              {detailLoading && selected && !currentRow ? (
                <div className="flex min-h-[280px] items-center justify-center rounded-xl border border-dashed bg-background/70 p-8 text-center" role="status" aria-live="polite">
                  <div className="space-y-3 text-sm text-muted-foreground"><RefreshCw className="mx-auto h-6 w-6 animate-spin text-primary" /><p>{english ? "Loading this prompt's bound receipts…" : "正在读取本条 Prompt 的绑定回执…"}</p><p className="text-xs">{english ? "This is loading, not an empty result." : "当前仍在读取中，不代表没有记忆或没有调用。"}</p></div>
                </div>
              ) : currentRow ? <ExecutionTopologyCanvas promptId={currentRow.prompt_id} promptText={currentRow.user_prompt} english={english} audit={audit} guidanceReceipt={currentRow.guidance_receipt} /> : <div className="flex min-h-[280px] items-center justify-center rounded-xl border border-dashed bg-background/70 p-8 text-center text-sm text-muted-foreground">{english ? "Select a user prompt to view its bound receipts." : "请选择一条用户 Prompt 查看绑定回执。"}</div>}
            </div>
            {showLegacyFlow && <div className="legacy-flow-detail rounded-xl border border-dashed border-slate-300 p-3 dark:border-slate-700">
            <div className="mb-5 flex flex-wrap items-center justify-center gap-1.5 text-[11px] text-muted-foreground">
              {stageLabels.map((label, index) => (
                <div key={label} className="flex items-center gap-1.5">
                  <span className="flex h-5 w-5 items-center justify-center rounded-full bg-slate-200 font-semibold text-slate-700 dark:bg-slate-800 dark:text-slate-200">
                    {index + 1}
                  </span>
                  <span>{label}</span>
                  {index < stageLabels.length - 1 && (
                    <span className="mx-0.5 text-slate-300">→</span>
                  )}
                </div>
              ))}
            </div>
            <div className="w-full space-y-3">
              <button
                onClick={() => setActive("entry")}
                className={`w-full rounded-xl border-l-4 ${nodes[0].tone} bg-background p-4 text-left shadow-sm transition hover:-translate-y-0.5 hover:shadow-md`}
              >
                <div className="flex items-center gap-2 font-medium">
                  <span className="rounded-full bg-sky-100 px-2 py-0.5 text-[10px] font-semibold text-sky-800 dark:bg-sky-950 dark:text-sky-200">
                    01 · ENTRY
                  </span>
                  {nodes[0].title}
                </div>
                <div className="mt-2 text-sm text-muted-foreground">{nodes[0].value}</div>
              </button>
              <div className="flex justify-center text-muted-foreground">
                <ArrowDown className="h-4 w-4" />
              </div>
              <div className="text-center text-xs font-medium text-muted-foreground">
                {inlineUiText("入口提供说明与地图，随后进入三条同级判断路径")}
              </div>
              <div className="grid gap-3 md:grid-cols-3">
                <button
                  onClick={() => {
                    setActive("entry");
                    setManualOpen(true);
                  }}
                  className="min-h-28 rounded-xl border-l-4 border-sky-400 bg-background p-4 text-left shadow-sm transition hover:-translate-y-0.5 hover:shadow-md"
                >
                  <div className="font-medium">{inlineUiText("记忆使用说明书")}</div>
                  <div className="mt-2 break-words text-sm text-muted-foreground">
                    {audit?.entry.instruction?.instruction_version ?? inlineUiText("旧记录未保存说明版本")}
                  </div>
                </button>
                <button
                  onClick={() => {
                    setActive("map");
                    setMapOpen(true);
                  }}
                  className="min-h-28 rounded-xl border-l-4 border-cyan-500 bg-background p-4 text-left shadow-sm transition hover:-translate-y-0.5 hover:shadow-md"
                >
                  <div className="font-medium">{nodes[1].title}</div>
                  <div className="mt-2 text-sm text-muted-foreground">{nodes[1].value}</div>
                  <div className="mt-2 text-xs text-cyan-700 dark:text-cyan-300">{inlineUiText("点击查看本轮 L0，并预览 L1 / L2 下钻状态")}</div>
                </button>
              </div>
              <div className="flex justify-center text-muted-foreground">
                <ArrowDown className="h-4 w-4" />
              </div>
              <p className="rounded-xl border border-dashed bg-background/70 p-3 text-center text-sm shadow-sm">
                {english ? copy.decision : inlineUiText(copy.decision)}
              </p>
              <div className="grid items-start gap-3 md:grid-cols-3">
                <div className="self-start rounded-xl border border-dashed bg-background/50 p-4 text-sm text-muted-foreground">
                  <div className="mb-2 text-[10px] font-semibold uppercase tracking-[0.12em]">
                    {inlineUiText("路径 A")}
                  </div>
                  {inlineUiText("已有可靠证据或任务自足 → 回答或执行")}
                </div>
                <button
                  onClick={() => { setActive("history"); setNodeOpen(true); }}
                  className="rounded-xl border-l-4 border-emerald-400 bg-background p-4 text-left shadow-sm transition hover:-translate-y-0.5 hover:shadow-md"
                >
                  <div className="flex items-center gap-2 font-medium">
                    <span className="rounded-full bg-emerald-100 px-2 py-0.5 text-[10px] font-semibold text-emerald-800 dark:bg-emerald-950 dark:text-emerald-200">
                      {inlineUiText("路径 B")}
                    </span>
                    {nodes[3].title}
                  </div>
                  <div className="mt-2 text-sm text-muted-foreground">{nodes[3].value}</div>
                    <div className="mt-2 text-xs text-muted-foreground">
                    {english ? "Focused history uses Recall · broad reviews may use Research · key conclusions require source readback" : inlineUiText("单点 Recall · 综合盘点可直接 Research · 关键结论回读原文")}
                  </div>
                  {audit?.systemProbe && <div className="mt-2 rounded border border-emerald-200 p-2 text-xs">
                    <div>{english ? "System probe" : inlineUiText("系统探测")}：{english ? ({ returned: "candidates returned", empty: "empty", unavailable: "unavailable", skipped: "skipped" } as Record<string,string>)[audit.systemProbe.state ?? ""] ?? "unknown" : ({ returned: inlineUiText("已返回候选"), empty: inlineUiText("本次为空"), unavailable: inlineUiText("不可用"), skipped: inlineUiText("已跳过") } as Record<string,string>)[audit.systemProbe.state ?? ""] ?? inlineUiText("未知")}</div>
                    <div>{audit.systemProbe.calls ?? 0} {inlineUiText("次请求 · 送出")} {audit.systemProbe.returned_count ?? 0} 条{audit.systemProbe.context_tokens != null ? ` · ${audit.systemProbe.context_tokens}/${audit.systemProbe.max_tokens} token` : ""}</div>
                    {audit.systemProbe.admission && <div className="mt-1 text-muted-foreground">{inlineUiText("候选门控：")}{audit.systemProbe.admission.mode ?? "—"} {inlineUiText("· 通过")} {audit.systemProbe.admission.admitted_count ?? 0} {inlineUiText("· 排除")} {audit.systemProbe.admission.rejected_count ?? 0}</div>}
                    <div className="mt-1 text-muted-foreground">{english ? "Agent-initiated calls are shown separately; candidates do not prove coverage." : inlineUiText("Agent 主动调用单独显示；候选不代表问题已覆盖。")}</div>
                  </div>}
                  {audit?.historyPlan && <div className="mt-2 rounded border border-sky-200 bg-sky-50/50 p-2 text-xs dark:border-sky-900/60 dark:bg-sky-950/20">
                    <div className="font-semibold text-sky-800 dark:text-sky-200">{inlineUiText("任务形状路由：")}{audit.historyPlan.recommended_route ?? "unknown"} · {audit.historyPlan.history_dependency ?? "unknown"}</div>
                    <div className="mt-1 text-muted-foreground">{audit.historyPlan.reason ?? inlineUiText("未记录路由理由")}</div>
                    {!!audit.historyPlan.required_slots?.length && <div className="mt-1">{inlineUiText("证据槽位：")}{audit.historyPlan.required_slots.join(" · ")}</div>}
                    {audit.historyPlan.fallback_route && <div className="mt-1 text-muted-foreground">{inlineUiText("空结果或范围不足时升级：")}{audit.historyPlan.fallback_route}</div>}
                  </div>}
                  {audit && (
                    <div className="mt-3 space-y-1.5 border-t border-emerald-200/70 pt-2 dark:border-emerald-900/60">
                      <div className="flex flex-wrap gap-1.5 text-[10px]">
                        {Object.entries(audit.timeWindowActivity?.by_tool ?? {}).map(([tool, summary]) => (
                          <span key={tool} className="rounded-full bg-emerald-100 px-2 py-0.5 text-emerald-800 dark:bg-emerald-900/60 dark:text-emerald-200">{tool} {summary.calls ?? 0} {inlineUiText("次 ·")} {summary.returned ?? 0} 条</span>
                        ))}
                      </div>
                      {(audit.timeWindowActivity?.items ?? audit.history.items).slice(0, 2).map((item) => (
                        <div key={item.id} role="button" tabIndex={0} onClick={(event) => { event.stopPropagation(); setSelectedEvidence(item); }} onKeyDown={(event) => { if (event.key === "Enter") setSelectedEvidence(item); }} className="line-clamp-2 rounded-md bg-emerald-50/70 px-2 py-1.5 text-[11px] leading-4 text-muted-foreground hover:text-primary dark:bg-emerald-950/20">{displayEvidence(item)}</div>
                      ))}
                    </div>
                  )}
                </button>
                <button
                  onClick={() => { setActive("guidance"); setNodeOpen(true); }}
                  className="rounded-xl border-l-4 border-amber-400 bg-background p-4 text-left shadow-sm transition hover:-translate-y-0.5 hover:shadow-md"
                >
                  <div className="flex items-center gap-2 font-medium">
                    <span className="rounded-full bg-amber-100 px-2 py-0.5 text-[10px] font-semibold text-amber-800 dark:bg-amber-950 dark:text-amber-200">
                      {inlineUiText("偏好路径")}
                    </span>
                    {nodes[2].title}
                  </div>
                  <div className="mt-2 text-sm text-muted-foreground">{nodes[2].value}</div>
                  <div className="mt-2 text-xs text-muted-foreground">
                    {inlineUiText("当前任务先看候选偏好，需要时按 ID 补读")}
                  </div>
                  {audit?.guidance.items.length ? (
                    <div className="mt-3 space-y-1.5 border-t border-amber-200/70 pt-2 dark:border-amber-900/60">
                      {audit.guidance.items.slice(0, 4).map((item) => (
                        <div key={item.id} className="rounded-md bg-amber-50/70 px-2 py-1.5 text-[11px] leading-4 text-muted-foreground dark:bg-amber-950/30">
                          {displayEvidence(item)}
                        </div>
                      ))}
                    </div>
                  ) : null}
                </button>
              </div>
              <div className="flex justify-center text-muted-foreground">
                <ArrowDown className="h-4 w-4" />
              </div>
              <button
                onClick={() => { setActive("answer"); setNodeOpen(true); }}
                className={`w-full rounded-xl border-l-4 ${nodes[4].tone} bg-background p-4 text-left shadow-sm transition hover:-translate-y-0.5 hover:shadow-md`}
              >
                <div className="flex items-center gap-2 font-medium">
                  <span className="rounded-full bg-rose-100 px-2 py-0.5 text-[10px] font-semibold text-rose-800 dark:bg-rose-950 dark:text-rose-200">
                    05 · OUTPUT
                  </span>
                  {nodes[4].title}
                </div>
                <div className="mt-2 text-sm text-muted-foreground">{nodes[4].value}</div>
              </button>
            </div>
            </div>}
          </div>
        </main>

        <aside className="hidden">
          <div className="flex items-center gap-2">
            <div className="flex h-7 w-7 items-center justify-center rounded-lg bg-muted">
              <GitBranch className="h-3.5 w-3.5" />
            </div>
            <h2 className="text-sm font-semibold">{inlineUiText("节点详情")}</h2>
          </div>
          <div className="mt-3 border-t pt-3">
            <div className="font-medium">{activeNode.title}</div>
            <div className="mt-1 text-sm text-muted-foreground">{activeNode.value}</div>
          </div>
          {active === "entry" && audit && (
            <div className="mt-4 space-y-4 text-sm">
              <p>{english ? copy.entry : inlineUiText(copy.entry)}</p>
              <dl className="space-y-3">
                <div>
                  <dt className="text-xs text-muted-foreground">{inlineUiText("说明版本")}</dt>
                  <dd className="break-all">
                    {audit.entry.instruction?.instruction_version ?? inlineUiText("该记录未保存")}
                  </dd>
                </div>
                <div>
                  <dt className="text-xs text-muted-foreground">{inlineUiText("当前任务投影")}</dt>
                  <dd>
                    {audit.entry.taskState?.current_objective ?? inlineUiText("旧记录未保存")}
                    {audit.entry.taskState?.continuation ? inlineUiText(" · 续接") : ""}
                  </dd>
                </div>
                <div>
                  <dt className="text-xs text-muted-foreground">{inlineUiText("偏好筛选覆盖")}</dt>
                  <dd>{coverageLabel(audit.entry.coverage)}</dd>
                </div>
                <div>
                  <dt className="text-xs text-muted-foreground">{inlineUiText("说明生成记录")}</dt>
                  <dd>{audit.entry.instruction ? inlineUiText("已生成至 Hook 上下文") : inlineUiText("未记录")}</dd>
                </div>
                <div>
                  <dt className="text-xs text-muted-foreground">{inlineUiText("模型实际收到／遵循")}</dt>
                  <dd>{inlineUiText("需独立宿主证据，不能由生成记录推断")}</dd>
                </div>
              </dl>
              <button
                onClick={() => setManualOpen(true)}
                className="rounded border px-3 py-2 hover:bg-muted"
              >
                {inlineUiText("查看本轮说明书")}
              </button>
            </div>
          )}
          {active === "guidance" && audit && (
            <div className="mt-4 space-y-3">
              <p className="text-sm text-muted-foreground">
                {inlineUiText("页面展示的是已收到并送达 Agent 上下文的指导回执；这可以说明 Agent 看到了这些内容， 但页面不再声称知道它最终如何使用。")}
              </p>
              {audit.guidance.deferred > 0 && (
                <p className="border-l-2 border-amber-400 pl-3 text-sm leading-6 text-muted-foreground">
                  {audit.guidance.deferred} {inlineUiText("项指导已识别但尚未展开完整条件；需要时可按 ID 读取。")}
                </p>
              )}
              <EvidenceList items={audit.guidance.items} onSelect={setSelectedEvidence} />
            </div>
          )}
          {active === "map" && audit && (
            <div className="mt-4 space-y-3 text-sm">
              <p className="leading-6 text-muted-foreground">
                {inlineUiText("L0 是该 Prompt 当时收到的固定预算导航；L1 和 L2 只显示页面实际收到的读取回执。")}
              </p>
              <dl className="grid grid-cols-[92px_1fr] gap-x-3 gap-y-2 text-xs">
                <dt className="text-muted-foreground">{inlineUiText("L0 总览")}</dt><dd>{audit.map.l0.contextChars ?? "—"} {inlineUiText("字符 ·")} {audit.map.l0.preferenceCount ?? "—"} {inlineUiText("条偏好 ·")} {audit.map.l0.topicCount ?? "—"} {inlineUiText("个主题")}</dd>
                <dt className="text-muted-foreground">{inlineUiText("L1 目录")}</dt><dd>{audit.map.l1.previewEntities ?? "—"} {inlineUiText("个轻量预览 ·")} {audit.map.l1.searchableEntities ?? "—"} {inlineUiText("个实体名称可搜索")}</dd>
                <dt className="text-muted-foreground">{inlineUiText("L2 证据")}</dt><dd>{audit.map.l2.actualRoute === "not_observed" || audit.map.l2.actualRoute === "unknown" ? inlineUiText("本轮未观测到证据下钻") : `${audit.map.l2.actualRoute} · 返回 ${audit.map.l2.returned ?? "—"} 条`}</dd>
              </dl>
              <button onClick={() => setMapOpen(true)} className="rounded border px-3 py-2 hover:bg-muted">{inlineUiText("查看本轮 L0 / L1 / L2")}</button>
            </div>
          )}
          {active === "history" && audit && (
            <div className="mt-4 space-y-4">
              <ToolRail
                history={{
                  route: audit.history.value,
                  state: audit.history.state,
                  decision: audit.history.decision,
                  metrics: audit.history.metrics,
                  items: audit.history.items,
                  routeReceipt: audit.history.routeReceipt,
                  timeWindowActivity: audit.timeWindowActivity,
                  timeWindowGuidanceActivity: audit.timeWindowGuidanceActivity,
                  preferenceItems: audit.guidance.items,
                  preferenceCount: audit.guidance.count,
                }}
              />
              {audit.history.routeReceipt && (
                <div className="rounded-xl border border-sky-200 bg-sky-50/50 p-3 dark:border-sky-900/60 dark:bg-sky-950/20">
                  <div className="text-xs font-semibold text-sky-900 dark:text-sky-200">
                    {inlineUiText("目录路由回执")}
                  </div>
                  <div className="mt-1 text-xs text-muted-foreground">
                    {inlineUiText("建议：")}
                    {audit.history.routeReceipt.recommended_route ??
                      audit.history.routeReceipt.decision ??
                      "—"}{" "}
                    {inlineUiText("· 置信度")}{" "}
                    {audit.history.routeReceipt.confidence == null
                      ? "—"
                      : `${Math.round(audit.history.routeReceipt.confidence * 100)}%`}
                  </div>
                  <div className="mt-1 text-xs leading-5 text-muted-foreground">
                    {audit.history.routeReceipt.reason ?? inlineUiText("未记录理由")}
                  </div>
                  {audit.history.routeAudit.status === "ep_history_verification_incomplete" && (
                    <div className="mt-3 rounded-lg border border-amber-300 bg-amber-100/80 px-3 py-2 text-xs font-medium text-amber-900 dark:border-amber-800 dark:bg-amber-950/40 dark:text-amber-100">
                      {inlineUiText("EP 历史核验未完成：本轮要求调用")} {audit.history.routeReceipt.recommended_route ?? "Recall / Research"}{inlineUiText("，但尚未观测到实际 EP 工具调用。任何本地文件搜索或候选提示都不计为历史核验。")}
                    </div>
                  )}
                  {audit.history.routeAudit.status === "ep_history_tool_called_empty" && (
                    <div className="mt-3 rounded-lg border border-amber-300 bg-amber-100/80 px-3 py-2 text-xs text-amber-900 dark:border-amber-800 dark:bg-amber-950/40 dark:text-amber-100">
                      {inlineUiText("EP 工具已调用，但返回 0 条；这表示已完成一次 EP 检索，不能据此断言历史中不存在相关内容。")}
                    </div>
                  )}
                  {audit.history.routeAudit.status === "ep_history_source_navigation_returned" ? <div className="mt-3 rounded-lg border bg-background px-3 py-2 text-xs">{flowText("sourceNavigationReceipt")}</div> : null}
                  {audit.history.routeReceipt.tool_events?.length ? (
                    <div className="mt-3 space-y-2">
                      {audit.history.routeReceipt.tool_events.map((event, index) => (
                        <div
                          key={`${event.tool}-${index}`}
                          className="rounded-lg border bg-background/80 p-2 text-xs"
                        >
                          <div className="font-medium">
                            {event.tool} · {event.route ?? "—"}
                          </div>
                          <div className="mt-1 text-muted-foreground">
                            {inlineUiText("候选")} {event.candidate_count ?? "—"} {inlineUiText("· 返回")} {event.returned_count ?? "—"}{" "}
                            {event.source_navigation_returned_count != null ? <span>· {flowText("sourceNavigationReturned")} {event.source_navigation_returned_count} </span> : null}
                            ·{" "}
                            {event.next_offset == null
                              ? inlineUiText("分页结束或未分页")
                              : `下一页 ${event.next_offset}`}
                          </div>
                          <div className="mt-1 text-muted-foreground">
                            {inlineUiText("宿主可见：")}{event.delivery?.host_visibility ?? inlineUiText("未测量")} {inlineUiText("· 回答使用：")}
                            {event.delivery?.answer_use ?? inlineUiText("未测量")}
                          </div>
                        </div>
                      ))}
                    </div>
                  ) : (
                    <div className="mt-2 text-xs text-muted-foreground">
                      {inlineUiText("尚未记录同一绑定 ID 的历史工具事件。")}
                    </div>
                  )}
                </div>
              )}
              <div className="grid grid-cols-2 gap-3 text-sm">
                <div>
                  <div className="text-xs text-muted-foreground">{inlineUiText("读取模式")}</div>
                  <div className="mt-1 font-medium break-words">{audit.history.mode}</div>
                </div>
                <div>
                  <div className="text-xs text-muted-foreground">{inlineUiText("回执来源")}</div>
                  <div className="mt-1 font-medium break-words">
                    {controllerLabel(audit.history.controller)}
                  </div>
                </div>
                <div>
                  <div className="text-xs text-muted-foreground">{inlineUiText("候选索引")}</div>
                  <div className="mt-1 font-medium tabular-nums">
                    {audit.history.metrics.candidates ?? "—"}
                  </div>
                </div>
                <div>
                  <div className="text-xs text-muted-foreground">{inlineUiText("本页候选预览")}</div>
                  <div className="mt-1 font-medium tabular-nums">
                    {audit.history.metrics.returned ?? "—"}
                  </div>
                </div>
                <div>
                  <div className="text-xs text-muted-foreground">{inlineUiText("待展开候选")}</div>
                  <div className="mt-1 font-medium tabular-nums">
                    {audit.history.metrics.unread ?? "—"}
                  </div>
                </div>
                <div>
                  <div className="text-xs text-muted-foreground">{inlineUiText("自动链路准入排除")}</div>
                  <div className="mt-1 font-medium tabular-nums">
                    {audit.history.metrics.rejected ?? "—"}
                  </div>
                </div>
              </div>
              {audit.history.decision === "agent_decides" && (
                <div className="rounded-lg border border-dashed bg-sky-50/70 px-3 py-2 text-xs leading-5 text-sky-900 dark:bg-sky-950/20 dark:text-sky-200">
                  {inlineUiText("自动历史召回已关闭；当前页面没有宿主级工具回执，无法判断 Codex 是否调用或读取了历史结果。")}
                </div>
              )}
              {audit.history.state === "unknown" && audit.history.decision !== "agent_decides" && (
                <div className="rounded-lg border border-dashed bg-amber-50/60 px-3 py-2 text-xs leading-5 text-amber-900 dark:bg-amber-950/20 dark:text-amber-200">
                  {inlineUiText("历史链路未核实：当前没有足够回执判断 Codex 是主动跳过，还是 recall/research 回执未被投影。")}
                </div>
              )}
              {audit.history.state === "not_observed" &&
                audit.history.decision !== "agent_decides" && (
                  <div className="rounded-lg border border-dashed bg-muted/30 px-3 py-2 text-xs leading-5 text-muted-foreground">
                    {inlineUiText("本轮未调用历史工具，所以候选数量和内容为空；这不是加载失败，也不代表历史库为空。")}
                  </div>
                )}
              <p className="text-sm leading-6 text-muted-foreground">
                {isLegacyAutoHistory
                  ? inlineUiText("这条旧记录发生在自动历史召回仍开启时：Controller 先做准入，Hook 再尝试投递。它只用于解释历史行为，不代表当前默认链路。")
                  : inlineUiText("候选和原文由同回合 MCP 工具回执确认返回；页面把已返回内容作为本轮可见输入展示。")}
              </p>
              <p className="text-sm text-muted-foreground">
                {inlineUiText("宿主送达：")}{audit.history.delivered ?? inlineUiText("未测量")} {inlineUiText("· 状态：")}{audit.history.delivery}
              </p>
              <EvidenceList items={audit.history.items} onSelect={setSelectedEvidence} />
              {currentRow?.historical_audit?.admission_items?.length ? (
                <>
                  <p className="text-xs font-medium text-muted-foreground">{inlineUiText("历史自动链路准入样本")}</p>
                  <EvidenceList
                    items={currentRow.historical_audit.admission_items}
                    onSelect={setSelectedEvidence}
                  />
                </>
              ) : null}
            </div>
          )}
          {active === "answer" && (
            <p className="mt-4 text-sm leading-6 text-muted-foreground">{english ? copy.answer : inlineUiText(copy.answer)}</p>
          )}
          {!audit && <p className="mt-4 text-sm text-muted-foreground">{english ? copy.select : inlineUiText(copy.select)}</p>}
        </aside>
      </div>
      <Dialog open={nodeOpen} onOpenChange={setNodeOpen}>
        <DialogContent className="max-h-[88vh] min-w-0 overflow-y-auto [overflow-wrap:anywhere] sm:max-w-3xl" style={{ width: "calc(100vw - 2rem)" }}>
          <DialogTitle>{activeNode.title}</DialogTitle>
          <DialogDescription>{activeNode.value}</DialogDescription>
          {active === "entry" && audit?.entry.instruction ? (
            <div className="whitespace-pre-wrap text-sm leading-7">{audit.entry.instruction.core_text}</div>
          ) : null}
          {active === "guidance" && audit ? (
            <div className="space-y-3 text-sm">
              <p className="text-muted-foreground">{inlineUiText("以下为本轮已返回的偏好候选；deferred 项需要按 ID 继续补读。")}</p>
              <EvidenceList items={audit.guidance.items} onSelect={(item) => { setNodeOpen(false); setSelectedEvidence(item); }} />
            </div>
          ) : null}
          {active === "history" && audit ? (
            <ToolRail history={{ route: audit.history.value, state: audit.history.state, decision: audit.history.decision, metrics: audit.history.metrics, items: audit.history.items, routeReceipt: audit.history.routeReceipt, timeWindowActivity: audit.timeWindowActivity, timeWindowGuidanceActivity: audit.timeWindowGuidanceActivity, preferenceItems: audit.guidance.items, preferenceCount: audit.guidance.count }} />
          ) : null}
          {active === "history" && currentRow?.candidate_groups?.length ? <CandidateAuditBrowser key={currentRow.prompt_id} promptId={currentRow.prompt_id} groups={currentRow.candidate_groups} /> : null}
          {active === "history" && audit?.history.routeReceipt?.tool_events?.map((event, index) => <RelevanceAuditDetails key={`relevance-${index}`} audit={(event as unknown as { relevance_audit?: Record<string, unknown> }).relevance_audit} />)}
          {active === "answer" ? <p className="text-sm leading-6 text-muted-foreground">{english ? copy.answer : inlineUiText(copy.answer)}</p> : null}
        </DialogContent>
      </Dialog>
      <Dialog open={manualOpen} onOpenChange={setManualOpen}>
        <DialogContent
          className="min-w-0 max-h-[85vh] overflow-y-auto [overflow-wrap:anywhere]"
          style={{ width: "calc(100vw - 2rem)", maxWidth: "42rem" }}
        >
          <DialogTitle>{inlineUiText("本轮记忆使用说明")}</DialogTitle>
          <DialogDescription>{inlineUiText("展示该次入口回执保存的说明版本与正文。")}</DialogDescription>
          {audit?.entry.instruction ? (
            <div className="space-y-4 text-sm">
              <p className="break-all font-mono text-xs">
                {audit.entry.instruction.instruction_version}
              </p>
              <p className="whitespace-pre-wrap leading-7">{audit.entry.instruction.core_text}</p>
              <dl className="space-y-2 text-xs text-muted-foreground">
                <dt>{inlineUiText("来源文件")}</dt>
                <dd className="break-all">{audit.entry.instruction.source_file}</dd>
                <dt>{inlineUiText("内容 SHA-256")}</dt>
                <dd className="break-all">{audit.entry.instruction.content_sha256}</dd>
              </dl>
            </div>
          ) : (
            <p className="text-sm">{inlineUiText("旧回执没有保存说明正文，不能用当前版本替代当时的送达证据。")}</p>
          )}
        </DialogContent>
      </Dialog>
      <Dialog open={mapOpen} onOpenChange={setMapOpen}>
        <DialogContent className="max-h-[88vh] min-w-0 overflow-y-auto [overflow-wrap:anywhere] sm:max-w-3xl" style={{ width: "calc(100vw - 2rem)" }}>
          <DialogTitle>{mapMode === "latest" ? inlineUiText("最新 Memory Map") : inlineUiText("本轮 Memory Map")}</DialogTitle>
          <DialogDescription>{mapMode === "latest" ? inlineUiText("查看当前目录与覆盖状态；不替代所选历史回合的地图回执。") : inlineUiText("展示该 Prompt 当时保存的地图。旧回合不会被最新目录改写。")}</DialogDescription>
          <div className="flex flex-wrap gap-2"><button className={`rounded border px-3 py-2 text-xs ${mapMode === "recorded" ? "bg-primary text-primary-foreground" : ""}`} onClick={() => { setMapMode("recorded"); setBrowseTopic(null); }}>{inlineUiText("本轮地图快照")}</button><button className={`rounded border px-3 py-2 text-xs ${mapMode === "latest" ? "bg-primary text-primary-foreground" : ""}`} onClick={() => { setMapMode("latest"); setBrowseTopic(null); }}>{inlineUiText("浏览最新地图与来源")}</button></div>
          {navigation ? <div className="space-y-5 text-sm">
            <section>
              <div className="flex items-baseline justify-between gap-3"><h3 className="font-semibold text-cyan-800 dark:text-cyan-200">{inlineUiText("L0 · 每轮总览")}</h3><span className="text-xs text-muted-foreground">{navigation.context_chars ?? "—"} {inlineUiText("字符")}</span></div>
              <details className="mt-3"><summary className="cursor-pointer text-sm text-muted-foreground">{inlineUiText("五维偏好入口 ·")} {navigation.preferences?.approved_count ?? "—"} {inlineUiText("条 · 展开场景")}</summary><div className="mt-3 space-y-2">
                {(navigation.preferences?.dimensions ?? []).map((dimension) => <div key={dimension.id ?? dimension.label} className="border-l-2 border-amber-400 pl-3">
                  <div className="font-medium">{dimension.label ?? dimension.id} · {dimension.count ?? "—"} 条</div>
                  <div className="mt-1 text-xs leading-5 text-muted-foreground">{(dimension.scopes ?? []).map((scope) => scope.scope).filter(Boolean).join(" / ") || inlineUiText("场景预览未保存")}</div>
                </div>)}
              </div></details>
              {navigation.bank?.hierarchy_coverage?.total_memory_count != null && <div className="mt-4 rounded border border-cyan-200 bg-cyan-50 p-3 text-xs leading-6 dark:border-cyan-900 dark:bg-cyan-950"><strong>{inlineUiText("Bank 全库导航")}</strong> · {navigation.bank.hierarchy_coverage.indexed_memory_count} / {navigation.bank.hierarchy_coverage.total_memory_count} {inlineUiText("条已归组 ·")} {navigation.bank.hierarchy_coverage.unassigned_memory_count} {inlineUiText("条待整理")}<p>{inlineUiText("跨领域记录可出现在多个入口，各领域数量不可直接相加。摘要经来源样本检查，尚不代表逐条事实或语义归类都已核实。")}</p></div>}
              <div className="mt-2 text-[10px] text-muted-foreground">{inlineUiText("以下为 L0 导航摘要，不是该主题的完整正文；进入目录后再看 L1 与来源。")}</div>
              <div className="mt-3 grid gap-2 sm:grid-cols-2">
                {(navigation.bank?.topics ?? []).map((topic) => <button key={topic.topic_id ?? topic.title} className="min-w-0 rounded border bg-muted/20 p-3 text-left hover:border-cyan-500 focus-visible:outline-cyan-600" onClick={() => setBrowseTopic(topic.topic_id ?? null)}>
                  <div className="font-medium">{topic.title ?? topic.topic_id}</div>
                  <div className="mt-1 line-clamp-4 text-xs leading-5 text-muted-foreground">{topic.navigation_summary ?? inlineUiText("导航摘要未保存")}</div>
                  <div className="mt-2 text-xs text-cyan-800 dark:text-cyan-200">{topic.memory_count != null ? `${topic.memory_count} 条 · ${topic.children?.length ?? 0} 个 L1 · ` : ""}{topic.source_count != null ? `${topic.source_count_semantics === "unique_documents_in_full_membership" ? "" : inlineUiText("样本至少 ")}${topic.source_count} 个文档 · ` : ""}{inlineUiText("进入目录 →")}</div>
                </button>)}
              </div>
            </section>
            {browseTopic && <NavigationTopicBrowser topicId={browseTopic} bankId={params.bankId} />}
            <section className="border-t pt-4">
              <h3 className="font-semibold">{inlineUiText("L1 · 目录预览")}</h3>
              <p className="mt-2 text-sm text-muted-foreground">{navigation.bank?.hierarchy_coverage?.leaf_count ? `${navigation.bank.hierarchy_coverage.leaf_count} 个具体主题覆盖已归组记录。` : inlineUiText("此快照尚无全库分层目录。")} {inlineUiText("名称索引")} {navigation.bank?.searchable_entity_count ?? "—"} {inlineUiText("项；另保留")} {navigation.bank?.entity_count ?? "—"} {inlineUiText("个实体详情预览。点击上方领域进入最新 L1，可查看来源与原文。")}</p>
            </section>
            {navigation.context_text && <details className="border-t pt-3"><summary className="cursor-pointer text-sm font-medium">{inlineUiText("查看实际提供给 Agent 的 L0 文本")}</summary><pre className="mt-3 whitespace-pre-wrap text-xs leading-6">{navigation.context_text}</pre></details>}
            <section className="border-t pt-4">
              <h3 className="font-semibold">{inlineUiText("L2 · 所选回合的实际读取")}</h3>
              <p className="mt-2 text-sm text-muted-foreground">{!audit || ["not_observed", "unknown", "agent_decides"].includes(audit.map.l2.actualRoute) ? inlineUiText("当前页面没有可核验的 recall / research / read_source 回执，无法展示 Agent 实际看到的内容。") : `页面收到路线回执 ${audit.map.l2.actualRoute}；候选 ${audit.map.l2.candidates ?? "—"}；返回 ${audit.map.l2.returned ?? "—"}；未读 ${audit.map.l2.unread ?? "—"}。`}</p>
              {!!audit?.map.l2.items.length && <div className="mt-3"><EvidenceList items={audit.map.l2.items.slice(0, 6)} onSelect={setSelectedEvidence} /></div>}
            </section>
          </div> : <p className="text-sm">{inlineUiText("该历史回执未保存本轮地图，不能用当前地图替代。")}</p>}
        </DialogContent>
      </Dialog>
      {selectedEvidence && (
        <div
          className="fixed inset-0 z-50 flex items-end justify-center bg-black/35 p-4 sm:items-center"
          role="dialog"
          aria-modal="true"
          aria-label={inlineUiText("候选详情")}
        >
          <div className="max-h-[75vh] w-full max-w-2xl overflow-y-auto rounded-lg border bg-background p-5 shadow-xl">
            <div className="flex items-start justify-between gap-4">
              <div>
                <p className="text-xs text-muted-foreground">
                  {selectedEvidence.type ?? "evidence"}
                </p>
                <h3 className="mt-1 font-semibold">{inlineUiText("候选详情")}</h3>
              </div>
              <button
                onClick={() => setSelectedEvidence(null)}
                className="rounded border px-2 py-1 text-sm hover:bg-muted"
              >
                {inlineUiText("关闭")}
              </button>
            </div>
            <dl className="mt-5 space-y-3 text-sm">
              <div>
                <dt className="text-xs text-muted-foreground">{inlineUiText("记录 ID")}</dt>
                <dd className="mt-1 break-all font-mono text-xs">{selectedEvidence.id}</dd>
              </div>
              {typeof selectedEvidence.score === "number" && (
                <div>
                  <dt className="text-xs text-muted-foreground">{inlineUiText("分数")}</dt>
                  <dd className="mt-1">{selectedEvidence.score.toFixed(3)}</dd>
                </div>
              )}
              <div>
                <dt className="text-xs text-muted-foreground">{inlineUiText("候选预览")}</dt>
                <dd className="mt-1 whitespace-pre-wrap leading-6">
                  {displayEvidence(selectedEvidence)}
                </dd>
              </div>
            </dl>
          </div>
        </div>
      )}
    </section>
  );
}
