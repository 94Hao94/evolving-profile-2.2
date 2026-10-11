"use client";

import { ActionButton } from "@/components/ui/action-button";
import { RelevanceAuditDetails } from "./recall-policy-settings";
import { useEffect, useMemo, useRef, useState } from "react";
import type { TopologyNode } from "@/lib/topology-spec";
import anchors from "@/lib/topology-anchors.json";
import { nodeMetric, nodePurpose, upgradeLocalLayout, summarizeReceiptScopes } from "@/lib/topology-presentation";
import { inlineUiText } from "@/lib/inline-i18n";
import { SourceNavigationDetails } from "./source-navigation-details";
import { ReturnedContentDetails } from "./returned-content-details";

type Props = { nodes: TopologyNode[]; edges?: unknown[]; english?: boolean };
type Anchor = { x: number; y: number; width: number; height: number };
const W = 1672,
  H = 941,
  STORAGE = "ep-topology-anchors-v7",
  TEXT_STORAGE = "ep-topology-frame-text-v7",
  DELETED_STORAGE = "ep-topology-frame-deleted-v7";
const POS: Record<string, [number, number, number, number]> = {
  prompt: [2.3, 8.7, 13.8, 8],
  binding: [18.9, 8.7, 12.7, 8],
  contract: [34.4, 8.7, 12.5, 8],
  fork: [49.5, 7.5, 8, 10],
  context: [69.4, 8.7, 13.3, 8],
  execution: [85.4, 8.7, 12.2, 8],
  "user-root": [21.4, 32.6, 9.6, 7.4],
  "user-memory-preference": [35.5, 27.3, 13.6, 5.8],
  "user-memory-recall": [35.5, 33.8, 13.6, 5.8],
  "user-memory-research": [35.5, 40, 13.6, 5.8],
  "user-memory-scenario-summary": [54.1, 29, 12.8, 6.5],
  "user-memory-read-source": [54.1, 37.3, 12.8, 6.5],
  "user-packet": [73.4, 32.6, 15.4, 7.4],
  "agent-process-memory-observe-trajectory": [20.7, 57.7, 7.4, 7.7],
  "agent-process-memory-capability-observation": [31.4, 48.8, 11.4, 4],
  "agent-process-memory-failure-events": [31.4, 53.4, 11.4, 4],
  "agent-process-memory-repair-mode": [31.4, 58, 11.4, 4],
  "agent-process-memory-reusable-process-strategy": [31.4, 62.6, 11.4, 4],
  "agent-process-memory-process-retrieval": [31.4, 67.2, 11.4, 4],
  "agent-process-memory-agent-research": [31.4, 71.8, 11.4, 4],
  "agent-process-memory-agent-scenario-summary": [46.4, 57.7, 6.5, 7.7],
  "agent-process-memory-migration-revalidation": [62.6, 48.5, 12.5, 4.2],
  "agent-process-memory-compatibility-gate": [54.6, 57.7, 6.6, 7.7],
  "agent-process-memory-guidance-hint": [62.6, 53.5, 12.5, 4.2],
  "agent-process-memory-guidance-recommend": [62.6, 58.5, 12.5, 4.2],
  "agent-process-memory-guidance-scaffold": [62.6, 63.5, 12.5, 4.2],
  "agent-process-memory-guidance-guard": [62.6, 68.5, 12.5, 4.2],
  "agent-packet": [79, 57.7, 10, 7.7],
  "rag-root": [21.4, 82.6, 7.5, 7.7],
  "external-rag-lexical": [32.4, 82.7, 9.7, 3.5],
  "external-rag-vector": [32.4, 87.1, 9.7, 6.2],
  "external-rag-fusion-rrf": [45.6, 82.7, 8.3, 7.4],
  "external-rag-rerank": [55.9, 82.7, 8.6, 7.4],
  "external-rag-jev-review": [66.7, 82.7, 8.9, 7.4],
  "rag-packet": [78, 82.7, 11, 7.4],
  "label-serial": [4.8, 3.2, 32, 3.2],
  "label-user-lane": [6.5, 35, 13, 4.2],
  "label-agent-lane": [6.5, 54, 16, 4.2],
  "label-rag-lane": [6.5, 82, 13, 4.2],
  "label-fork": [49.5, 16.5, 6, 3.5],
};
const status = (s: string, e: boolean) =>
  (
    ({
      not_observed: e ? "Not observed" : "未观测",
      observed: e ? "Observed" : "已观测",
      delivered: e ? "Delivered" : "已送达",
      candidate_returned: e ? "Candidates returned" : "候选已返回",
      available: e ? "Available" : "可用",
      disabled: e ? "Disabled" : "已禁用",
      failed: e ? "Failed" : "失败",
    }) as Record<string, string>
  )[s] ?? s;
const candidatesFor = (n: TopologyNode) =>
  n.data.receipts.length && n.data.receipts.every((r) => r.candidate_count == null)
    ? "—"
    : n.data.counts.candidates;
const logicFor = (id: string, english: boolean) => {
  const zh: Record<string, string> = {
    prompt: "接收当前 Prompt，建立本次链路的入口上下文。",
    binding: "绑定宿主、Hook、Skill 与运行环境，确定可用工具边界。",
    contract: "把请求解析为任务、授权和执行约束，供三条泳道共同使用。",
    fork: "将任务契约分发到用户记忆、智能体过程记忆和外部 RAG 三条并行泳道。",
    context: "把 A/B/C 三个数据包按来源边界组装成当前执行上下文。",
    execution: "显示上下文送达累计次数；不据此推断回答采用或长期记忆写入。",
    "user-root": "用户记忆入口，承载偏好、历史事实和情境信息。",
    "user-memory-preference": "读取条件化用户偏好，为当前回答提供个性化约束。",
    "user-memory-recall": "从用户记忆中召回与当前 Prompt 相关的候选。",
    "user-memory-research": "跨多条用户历史或时间线进行研究式检索。",
    "user-memory-scenario-summary": "在多个候选 Session 或情境之间做范围定位和摘要。",
    "user-memory-read-source": "回读原始来源，核对候选摘要中的关键事实。",
    "user-packet": "汇总用户记忆分支，形成可交付的用户记忆包",
    "agent-root": "智能体过程记忆入口，承载观察、过程经验和干预策略。",
    "agent-process-memory-observe-trajectory": "记录当前 Agent 的轨迹、步骤、动作和过程状态。",
    "agent-process-memory-capability-observation": "从当前过程提取能力、工具和执行表现。",
    "agent-process-memory-failure-events": "记录失败、异常和需要关注的过程事件。",
    "agent-process-memory-repair-mode": "识别或执行针对当前问题的修复模式。",
    "agent-process-memory-reusable-process-strategy": "提取可复用的过程策略、脚本和操作模式。",
    "agent-process-memory-process-retrieval": "召回与当前任务相似的 Agent 过程记忆和历史修复经验。",
    "agent-process-memory-agent-research": "跨任务、跨项目比较 Agent 过程经验，必要时升级研究。",
    "agent-process-memory-agent-scenario-summary":
      "整理 Agent 过程记忆涉及的 Session、阶段和竞争情境。",
    "agent-process-memory-migration-revalidation": "检查过程经验迁移到当前任务后是否仍然成立。",
    "agent-process-memory-compatibility-gate": "过滤与当前任务不兼容、未核验或风险过高的过程经验。",
    "agent-process-memory-guidance-hint": "提供轻量提示，不改变主任务执行边界。",
    "agent-process-memory-guidance-recommend": "给出可采用的过程建议和下一步动作。",
    "agent-process-memory-guidance-scaffold": "提供结构化脚手架，帮助 Agent 按步骤执行。",
    "agent-process-memory-guidance-guard": "提供防护条件，阻止错误路由或越界操作。",
    "agent-packet": "汇总智能体过程记忆，形成过程记忆包。",
    "rag-root": "外部 RAG 入口，承载不写入用户记忆的外部资料。",
    "external-rag-lexical": "按关键词和词法匹配检索外部文档。",
    "external-rag-vector": "按语义向量相似度检索外部文档。",
    "external-rag-fusion-rrf": "融合词法与向量结果，降低单一路径偏差。",
    "external-rag-rerank": "对候选文档进行相关性重排。",
    "external-rag-jev-review": "进行 JEV 审查或外部知识核验。",
    "rag-packet": "汇总外部资料，形成 RAG 知识包。",
  };
  const en: Record<string, string> = {
    prompt: "Receives the current Prompt and establishes the entry context.",
    binding: "Binds the host, hooks, skills and runtime tool boundary.",
    contract: "Parses the request into task, authorization and execution constraints.",
    fork: "Fans the contract into User Memory, Agent Process Memory and External RAG lanes.",
    context: "Assembles the A/B/C packets into the execution context.",
    execution: "Shows cumulative context delivery; answer adoption and knowledge writeback remain unmeasured.",
    "agent-process-memory-process-retrieval":
      "Recalls relevant Agent process memories and prior repair experience.",
    "agent-process-memory-agent-research":
      "Compares Agent process experience across tasks and projects.",
    "agent-process-memory-agent-scenario-summary":
      "Summarizes Sessions, phases and competing contexts in process memory.",
  };
  return (
    (english ? en[id] : zh[id]) ||
    (english ? "Role in the execution topology." : "该节点在执行拓扑中的职责。")
  );
};
export function TopologyEngineLab({ nodes, english = false }: Props) {
  const [zoom,setZoom]=useState(1);
  const [selected, setSelected] = useState<TopologyNode | null>(null),
    [editMode, setEditMode] = useState(false),
    [editing, setEditing] = useState<string | null>(null),
    [message, setMessage] = useState("");
  const [draft, setDraft] = useState<Record<string, Anchor>>(() => {
    if (typeof window === "undefined") return upgradeLocalLayout(anchors);
    try {
      const saved=JSON.parse(localStorage.getItem(STORAGE) || "null") || anchors;
      return localStorage.getItem("ep-topology-layout-v8") ? saved : upgradeLocalLayout(saved);
    } catch {
      return upgradeLocalLayout(anchors);
    }
  });
  const [texts, setTexts] = useState<Record<string, string>>(() => {
    try {
      return JSON.parse(localStorage.getItem(TEXT_STORAGE) || "{}") || {};
    } catch {
      return {};
    }
  });
  const [deleted, setDeleted] = useState<string[]>(() => {
    try {
      return JSON.parse(localStorage.getItem(DELETED_STORAGE) || "[]") || [];
    } catch {
      return [];
    }
  });
  const drag = useRef<{
    id: string;
    mode: "move" | "resize";
    sx: number;
    sy: number;
    origin: Anchor;
  } | null>(null);
  useEffect(
    () => setEditMode(new URLSearchParams(window.location.search).get("anchorEdit") === "1"),
    []
  );
  useEffect(()=>{
    if(localStorage.getItem("ep-topology-layout-v8")) return;
    const original=localStorage.getItem(STORAGE);
    if(original) localStorage.setItem("ep-topology-anchors-v7-before-layout-v8",original);
    localStorage.setItem(STORAGE,JSON.stringify(draft));
    localStorage.setItem("ep-topology-layout-v8","1");
  },[]);
  const labels = useMemo(() => {
    const d = {
      "label-serial": english
        ? "SERIAL · Prompt → Context → Delivery"
        : "串行 · Prompt → 上下文 → 送达",
      "label-user-lane": english ? "A · User Memory" : "A · 用户记忆",
      "label-agent-lane": english ? "B · Agent Memory" : "B · 智能体记忆",
      "label-rag-lane": english ? "C · External RAG" : "C · 外部 RAG",
      "label-fork": english ? "FORK" : "分支",
    };
    return Object.entries(d)
      .map(([id, label]) => ({ id, label: texts[id] ?? label }))
      .filter((x) => !deleted.includes(x.id));
  }, [english, texts, deleted]);
  const frames = useMemo(
    () =>
      nodes
        .filter((n) => n.data.kind !== "lane" && n.id !== "agent-root" && !deleted.includes(n.id))
        .map((n) => ({ id: n.id, node: n, label: texts[n.id] ?? n.data.label })),
    [nodes, texts, deleted]
  );
  const begin = (e: React.PointerEvent, id: string, mode: "move" | "resize") => {
    if (!editMode) return;
    const a = draft[id] || {
      x: POS[id]?.[0] ?? 0,
      y: POS[id]?.[1] ?? 0,
      width: POS[id]?.[2] ?? 10,
      height: POS[id]?.[3] ?? 6,
    };
    (e.currentTarget as HTMLElement).setPointerCapture?.(e.pointerId);
    drag.current = { id, mode, sx: e.clientX, sy: e.clientY, origin: a };
  };
  const move = (e: React.PointerEvent) => {
    const d = drag.current;
    if (!d) return;
    const svg = (e.currentTarget as HTMLElement).closest("svg") as SVGSVGElement | null,
      inv = svg?.getScreenCTM()?.inverse();
    if (!svg || !inv) return;
    const p = svg.createSVGPoint(),
      s = svg.createSVGPoint();
    p.x = e.clientX;
    p.y = e.clientY;
    s.x = d.sx;
    s.y = d.sy;
    const n = p.matrixTransform(inv),
      o = s.matrixTransform(inv),
      dx = ((n.x - o.x) / W) * 100,
      dy = ((n.y - o.y) / H) * 100;
    setDraft((v) => {
      const a = { ...d.origin };
      if (d.mode === "move") {
        a.x = Math.max(0, Math.min(100 - a.width, a.x + dx));
        a.y = Math.max(0, Math.min(100 - a.height, a.y + dy));
      } else {
        a.width = Math.max(2, Math.min(100 - a.x, a.width + dx));
        a.height = Math.max(2, Math.min(100 - a.y, a.height + dy));
      }
      return { ...v, [d.id]: a };
    });
  };
  const save = async () => {
    localStorage.setItem(STORAGE, JSON.stringify(draft));
    localStorage.setItem(TEXT_STORAGE, JSON.stringify(texts));
    localStorage.setItem(DELETED_STORAGE, JSON.stringify(deleted));
    setMessage(english ? "Saved" : "已保存");
    setTimeout(() => setMessage(""), 2200);
  };
  const reset = async () => {
    setDraft(upgradeLocalLayout(anchors));
    setTexts({});
    setDeleted([]);
    setEditing(null);
    localStorage.removeItem(STORAGE);
    localStorage.removeItem(TEXT_STORAGE);
    localStorage.removeItem(DELETED_STORAGE);
    setMessage(english ? "Reset" : "已重置");
  };
  const frame = (id: string, label: string, node?: TopologyNode) => {
    const a = draft[id] || {
        x: POS[id]?.[0] ?? 0,
        y: POS[id]?.[1] ?? 0,
        width: POS[id]?.[2] ?? 10,
        height: POS[id]?.[3] ?? 6,
      },
      x = (a.x * W) / 100,
      y = (a.y * H) / 100,
      w = (a.width * W) / 100,
      h = (a.height * H) / 100,
      compact = Boolean(node) && (a.height < 5.2 || a.width < 10.5);
    return (
      <foreignObject data-node-id={id} key={id} x={x} y={y} width={w} height={h}>
        <div
          className={`reference-unified-frame ${editMode ? "is-editable" : ""} ${id==="rag-root" || id==="agent-process-memory-observe-trajectory" ? "reference-expanded-root" : ""} ${/^label-(user|agent|rag)-lane$/.test(id)?"reference-lane-title-frame":""}`}
          onPointerDown={(e) => {
            if (!editMode) return;
            const target = e.target as HTMLElement;
            begin(e, id, target.classList.contains("reference-anchor-resize") ? "resize" : "move");
          }}
          onDoubleClick={(e) => {
            if (editMode) {
              e.stopPropagation();
              setEditing(id);
            }
          }}
          onPointerMove={move}
          onPointerUp={() => {
            drag.current = null;
          }}
        >
          <div className="reference-unified-content">
            {editMode && editing === id ? (
              <input
                autoFocus
                className="reference-unified-input"
                value={label}
                onChange={(e) => setTexts((v) => ({ ...v, [id]: e.target.value }))}
                onPointerDown={(e) => e.stopPropagation()}
                onBlur={() => setEditing(null)}
              />
            ) : (
              <button
                type="button"
                aria-label={node ? `${english ? "Open receipt: " : "打开回执："}${label}` : label}
                title={label}
                className={`reference-unified-node ${node ? `reference-svg-node-${node.data.lane}` : "reference-label-node"} ${compact ? "is-compact" : ""}`}
                onDoubleClick={(e) => {
                  e.stopPropagation();
                  setEditing(id);
                }}
                onClick={() => !editMode && node && setSelected(node)}
              >
                {node ? (
                  <>
                    <strong>{label}</strong>
                    {nodeMetric(id,node.data,english) && <span>{nodeMetric(id,node.data,english)}</span>}
                  </>
                ) : (
                  label
                )}
              </button>
            )}
            {editMode && (
              <button
                type="button"
                className="reference-unified-delete"
                onPointerDown={(e) => e.stopPropagation()}
                onClick={() => setDeleted((v) => [...v, id])}
              >
                ×
              </button>
            )}
          </div>
          {editMode && (
            <span
              className="reference-anchor-resize"
              onPointerDown={(e) => {
                e.preventDefault();
                e.stopPropagation();
                begin(e, id, "resize");
              }}
              onPointerMove={move}
              onPointerUp={() => {
                drag.current = null;
              }}
            />
          )}
        </div>
      </foreignObject>
    );
  };
  return (
    <section className="reference-topology-shell" data-testid="topology-engine-lab">
      <div className="reference-topology-head">
        <div>
          <p className="topology-kicker">{english ? "EXECUTION TOPOLOGY" : "执行拓扑"}</p>
          <h2>{english ? "Full Chain" : "全链路"}</h2>
          <p>
            {editMode
              ? english
                ? "Drag, resize, double-click to edit, delete."
                : "拖动、缩放、双击改字、删除。"
              : english
                ? "Live projection"
                : "实时投影"}
          </p>
        </div>
        <div className="reference-topology-actions">
          <span>
            {editMode
              ? english
                ? "ANCHOR EDIT"
                : "锚点编辑"
              : english
                ? "Live projection"
                : "实时投影"}
          </span>
          <a
            className="reference-anchor-link"
            href={editMode ? "?view=flow" : "?view=flow&anchorEdit=1"}
          >
            {editMode ? "↩" : "⚙"}
          </a>
          {editMode && (
            <>
              <ActionButton
                variant="outline"
                type="button"
                resetKey={JSON.stringify({ draft, texts, deleted })}
                onAction={save}
              >
                保存锚点
              </ActionButton>
              {message && <span className="reference-save-feedback">{message}</span>}
              <ActionButton
                variant="outline"
                type="button"
                resetKey={JSON.stringify({ draft, texts, deleted })}
                onAction={reset}
              >
                重置
              </ActionButton>
            </>
          )}
        </div>
      </div>
      <div className="flex items-center gap-2 border-b px-4 py-2 text-sm" role="toolbar" aria-label={english ? "Topology zoom" : "链路图缩放"}>
        {[1,1.5,2].map(value=><button key={value} type="button" aria-pressed={zoom===value} className="rounded border px-3 py-1 hover:bg-muted focus-visible:outline-2" onClick={()=>setZoom(value)}>{value===1 ? (english ? "Fit" : "全图") : `${Math.round(value*100)}%`}</button>)}
        <details className="min-w-0" data-testid="topology-node-index"><summary className="cursor-pointer">{english ? "Keyboard node index" : "键盘节点入口"}</summary><div className="absolute z-20 grid max-h-80 max-w-xl gap-1 overflow-auto rounded border bg-card p-3 shadow-lg">{frames.map(({id,label,node})=><button key={id} type="button" aria-label={`${english ? "Open receipt: " : "打开回执："}${label}`} className="rounded border px-3 py-2 text-left text-sm hover:bg-muted" onClick={()=>setSelected(node)}>{label}</button>)}</div></details>
      </div>
      <div className="reference-topology-stage" style={{overflow:"auto",maxHeight:zoom===1 ? "none" : undefined}}>
        <svg style={{width:`${zoom*100}%`,minWidth:zoom>1 ? W : undefined,height:"auto",maxWidth:"none"}} viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="xMidYMin meet" aria-label={english ? "Execution topology with receipt nodes" : "执行链路与回执节点"}>
          <image
            href="/assets/topology-reference-design-v17.png"
            x="0"
            y="0"
            width={W}
            height={H}
            preserveAspectRatio="none"
          />
          <g pointerEvents="none" aria-hidden="true">
            {/* The bitmap is unchanged. These two local vector cards provide
                actual text space and reconnect the existing arrow segments. */}
            <defs><marker id="local-root-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto"><path d="M0 0 L10 5 L0 10Z" fill="#6010ff" /></marker></defs>
            <rect x="330" y="543" width="154" height="72" rx="8" fill="#f6f0ff" stroke="#ba8aff" strokeWidth="2" />
            <path d="M344 580 H355 L361 564 L368 597 L374 576 H385" fill="none" stroke="#7418ee" strokeWidth="5" strokeLinecap="round" strokeLinejoin="round" />
            <path d="M320 580 H330" stroke="#6010ff" strokeWidth="2" markerEnd="url(#local-root-arrow)" />
            <rect x="330" y="777" width="164" height="72" rx="8" fill="#ecf8ff" stroke="#3495ff" strokeWidth="2" />
            <path d="M350 826 L366 800 L383 826" fill="none" stroke="#088cff" strokeWidth="3" />
            <g fill="#088cff"><circle cx="350" cy="826" r="8"/><circle cx="366" cy="800" r="8"/><circle cx="383" cy="826" r="8"/></g>
            <path d="M320 813 H330" stroke="#6010ff" strokeWidth="2" markerEnd="url(#local-root-arrow)" />
            <rect x="242" y="565" width="71" height="31" rx="4" fill="#e6dcfa" />
            <rect x="242" y="800" width="71" height="31" rx="4" fill="#c7e9ff" />
          </g>
          <g className="reference-unified-frames">
            {frames.map((f) => frame(f.id, f.label, f.node))}
            {labels.map((l) => frame(l.id, l.label))}
          </g>
        </svg>
      </div>
      {selected && (
        <div
          className="topology-modal"
          role="dialog"
          aria-modal="true"
          onClick={() => setSelected(null)}
        >
          <div className="topology-modal-card" onClick={(e) => e.stopPropagation()}>
            <button
              type="button"
              className="topology-modal-close"
              onClick={() => setSelected(null)}
            >
              ×
            </button>
            <p className="topology-kicker">{english ? "RECEIPT DETAILS" : "回执详情"}</p>
            <h3>{selected.data.label}</h3>
            <div className="topology-detail-status">{status(selected.data.status, english)}</div>
            <p className="topology-detail-section">{english ? "FUNCTION" : "功能说明"}</p>
            <p>{selected.data.description}</p>
            <p className="topology-detail-section">{english ? "LOGIC" : "逻辑说明"}</p>
            <p>{logicFor(selected.id, english)}</p>
            <p className="topology-detail-section">{english ? "MAPPING DATA" : "映射数据"}</p>
            <p>{inlineUiText(selected.data.sourceScope,english ? "en" : "zh-CN")}</p>
            {["user-memory-preference","user-root","user-packet","context","execution"].includes(selected.id) && selected.data.receipts.length>0 && (()=>{
              const scope=summarizeReceiptScopes(selected.data.receipts);
              const display=(count:number|null)=>count ?? "—";
              return <div className="mt-2 rounded border bg-muted/30 p-3 text-sm" data-testid="receipt-count-scope">
                {selected.id==="user-memory-preference" ? <p>{english ? "Candidate packets: " : "候选包："}{scope.candidatePack.calls}{english ? " calls · returns " : " 次 · 累计返回 "}{display(scope.candidatePack.returned)}{english ? " · deliveries " : " · 累计送达 "}{display(scope.candidatePack.delivered)}</p> : <p>{english ? "Node totals cover candidate packets and history/source receipts, excluding preference and process-unit readbacks." : "本节点汇总候选包与历史/来源累计，不含偏好和过程条目补读。"}</p>}
                <p className="mt-1">{english ? "Unit readbacks (excluded from node totals): " : "条目补读（不计入节点总数）："}{scope.unitReadback.calls}{english ? " calls · returns " : " 次 · 累计返回 "}{display(scope.unitReadback.returned)}{english ? " · deliveries " : " · 累计送达 "}{display(scope.unitReadback.delivered)}</p>
                <p className="mt-1">{english ? "All visible tool returns in this card: " : "本弹卡范围内全部工具累计返回："}{display(scope.all.returned)}{english ? " across " : "，共 "}{scope.all.calls}{english ? " recorded calls. Repeated items may overlap; this is not a unique-memory count or proof of answer adoption." : " 次已记录调用。内容可能重复；这不是去重记忆数，也不证明回答采用。"}</p>
              </div>;
            })()}
            {selected.data.status === "not_observed" &&
            !selected.data.receipts.length &&
            !selected.data.items.length &&
            !selected.data.counts.candidates &&
            !selected.data.counts.returned &&
            !selected.data.counts.delivered ? (
              <p>
                {english
                  ? "No bound receipt; counts are not inferred as zero."
                  : "未绑定真实回执，数量不推断为 0。"}
              </p>
            ) : nodePurpose(selected.id)!=="retrieval" ? (
              <p>{nodeMetric(selected.id,selected.data,english) || (english?"Explanation node; no retrieval count.":"说明节点，不使用检索条数。")}</p>
            ) : (
              <>
                <p>
                  {english ? "Scanned candidates (overlap possible) " : "扫描候选累计（可重复） "}
                  {candidatesFor(selected)}
                  {english ? " · Return occurrences " : " · 累计返回 "}
                  {selected.data.counts.returned ?? "—"}
                  {english ? " · Delivery occurrences " : " · 累计送达 "}
                  {selected.data.counts.delivered ?? "—"}
                </p>
                <p>
                  {english ? "Receipts " : "回执 "}
                  {selected.data.receipts.length}
                  {english ? "" : " 条"}
                </p>
              </>
            )}
            {selected.data.receipts.map((receipt, index) => (
              <RelevanceAuditDetails
                key={`relevance-${index}`}
                audit={
                  (receipt as unknown as { relevance_audit?: Record<string, unknown> })
                    .relevance_audit
                }
              />
            ))}
            {selected.data.receipts.some(receipt=>typeof receipt.source_navigation_returned_count==="number") ? <p data-testid="source-navigation-count">
              {inlineUiText("原文导航返回数", english ? "en" : undefined)} {summarizeReceiptScopes(selected.data.receipts).sourceNavigationReturned}
            </p> : null}
            {selected.data.items.length ? (
              selected.data.items.map((i, n) => (
                i.source_navigation ? <SourceNavigationDetails key={n} locator={i.source_navigation} />
                : i.returned_item ? <ReturnedContentDetails key={`${i.returned_item.snapshot?.ref || i.returned_item.text_sha256 || i.returned_item.id}:${i.returned_item.item_index ?? n}`} item={i.returned_item}/>
                : <p key={n} data-i18n-ignore="true"><b>{i.kind}</b> {i.text}</p>
              ))
            ) : (
              <p>
                {english
                  ? "No concrete mapping items were returned."
                  : "当前节点没有具体映射条目，可能是未调用、返回为空或结果被过滤。"}
              </p>
            )}
          </div>
        </div>
      )}
    </section>
  );
}
