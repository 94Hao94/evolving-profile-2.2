"use client";

import { useEffect, useState } from "react";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import type { FlowLaneSummary, FlowReceipt } from "@/lib/flow-receipt";

const LABELS: Record<string, { en: string; zh: string }> = {
  not_observed: { en: "Not observed", zh: "未观测" }, observed: { en: "Observed", zh: "已观测" }, delivered: { en: "Delivered", zh: "已送达" }, verified: { en: "Verified", zh: "已验证" }, candidate_returned: { en: "Candidates returned", zh: "已返回候选" }, unknown: { en: "Unknown", zh: "未知" },
};

export function AgentProcessFlowPanel({ promptId, english }: { promptId?: string | null; english: boolean }) {
  const [lane, setLane] = useState<FlowLaneSummary | null>(null);
  const [receipts, setReceipts] = useState<FlowReceipt[]>([]);
  const [open, setOpen] = useState(false);
  useEffect(() => {
    if (!promptId) { setLane(null); setReceipts([]); return; }
    const controller = new AbortController();
    fetch(`/api/evolving-profile/flow/${encodeURIComponent(promptId)}`, { cache: "no-store", signal: controller.signal })
      .then((response) => response.ok ? response.json() : null)
      .then((payload) => { setLane(payload?.lanes?.find((item: FlowLaneSummary) => item.lane === "agent_process") ?? null); setReceipts(payload?.receipts ?? []); })
      .catch((error) => { if (error?.name !== "AbortError") { setLane(null); setReceipts([]); } });
    return () => controller.abort();
  }, [promptId]);
  const status = LABELS[lane?.status ?? "not_observed"]?.[english ? "en" : "zh"] ?? (english ? "Unknown" : "未知");
  return <>
    <section className="mt-5 rounded-xl border border-violet-200 bg-violet-50/40 p-4 dark:border-violet-900/60 dark:bg-violet-950/20">
      <div className="flex items-center justify-between gap-3"><div><h2 className="text-sm font-semibold">{english ? "Agent process memory" : "智能体过程记忆"}</h2><p className="mt-1 text-xs text-muted-foreground">{english ? "A parallel lane for process observation, retrieval, intervention, verification, and writeback." : "与用户记忆并行的过程观察、检索、干预、验证和写回链路。"}</p></div><span className="rounded-full border border-violet-300 px-2 py-1 text-xs font-medium">{status}</span></div>
          <div className="mt-3 grid grid-cols-2 gap-2 sm:grid-cols-4"><Stat label={english ? "Candidates" : "候选"} value={lane ? lane.counts.candidates : "—"} /><Stat label={english ? "Returned" : "返回"} value={lane ? lane.counts.returned : "—"} /><Stat label={english ? "Delivered" : "送达"} value={lane ? lane.counts.delivered : "—"} /><Stat label="Token" value={lane ? lane.counts.tokens : "—"} /></div>
      <button type="button" onClick={() => setOpen(true)} className="mt-3 rounded border border-violet-300 px-3 py-1.5 text-xs font-medium hover:bg-violet-100 dark:hover:bg-violet-900/40">{english ? "View process receipts" : "查看过程回执"}</button>
    </section>
    <Dialog open={open} onOpenChange={setOpen}><DialogContent className="max-h-[85vh] max-w-3xl overflow-y-auto"><DialogTitle>{english ? "Agent process memory receipts" : "智能体过程记忆回执"}</DialogTitle><DialogDescription>{english ? "Candidates, delivery, verification, and writeback are shown separately." : "候选、送达、验证和写回状态分别展示。"}</DialogDescription><div className="space-y-2 text-sm">{!receipts.length && <p className="rounded border border-dashed p-4 text-muted-foreground">{english ? "No Prompt-bound process receipt was observed. This does not mean process memory is empty." : "本 Prompt 未观测到已绑定的过程回执；这不代表智能体记忆为空。"}</p>}{receipts.map((receipt) => <article key={receipt.trace_id} className="rounded border p-3"><div className="flex flex-wrap items-center justify-between gap-2"><strong>{receipt.stage}</strong><span className="rounded bg-muted px-2 py-0.5 text-xs">{LABELS[receipt.status]?.[english ? "en" : "zh"] ?? receipt.status}</span></div><p className="mt-1 text-xs text-muted-foreground">{receipt.summary}</p><dl className="mt-2 grid grid-cols-2 gap-1 text-xs"><dt>Trace ID</dt><dd className="break-all font-mono">{receipt.trace_id}</dd><dt>{english ? "Source" : "来源"}</dt><dd>{receipt.source_type ?? "—"}</dd><dt>{english ? "Delivered" : "送达"}</dt><dd>{receipt.delivered_count ?? "—"}</dd><dt>Token</dt><dd>{receipt.token_count ?? "—"}</dd></dl></article>)}</div></DialogContent></Dialog>
  </>;
}

function Stat({ label, value }: { label: string; value: number | string | null }) { return <div className="rounded border bg-background/70 p-2"><div className="text-[10px] text-muted-foreground">{label}</div><div className="mt-1 text-lg font-semibold tabular-nums">{value ?? "—"}</div></div>; }
