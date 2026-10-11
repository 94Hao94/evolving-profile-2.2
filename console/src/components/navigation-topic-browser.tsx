"use client";

import { ActionButton } from "@/components/ui/action-button";

import { useEffect, useState } from "react";

import { inlineUiText } from "@/lib/inline-i18n";
type SourceRef = { memory_id: string; document_id?: string; preview?: string; source_status?: string };
type Topic = {
  topic_id: string; title: string; level?: string; navigation_summary?: string; overview?: string;
  memory_count?: number; source_count?: number; entities?: string[];
  child_previews?: Array<{ topic_id: string; title: string; navigation_summary?: string; memory_count?: number }>;
  source_locators?: SourceRef[];
  evidence_page?: { items: SourceRef[]; total: number; offset: number; next_offset: number | null };
};

export function NavigationTopicBrowser({ topicId, bankId }: { topicId: string; bankId: string }) {
  const [path, setPath] = useState<string[]>([topicId]);
  const [topic, setTopic] = useState<Topic | null>(null);
  const [offset, setOffset] = useState(0);
  const [error, setError] = useState("");
  const [source, setSource] = useState<{ id: string; text?: string; original?: string; document_id?: string } | null>(null);
  const [reading, setReading] = useState(false);
  useEffect(() => { setPath([topicId]); setOffset(0); setSource(null); }, [topicId]);
  const active = path[path.length - 1];
  useEffect(() => {
    const abort = new AbortController();
    setTopic(null); setError(""); setSource(null);
    fetch(`/api/evolving-profile/guidance/topics/${encodeURIComponent(active)}?offset=${offset}`, { signal: abort.signal })
      .then(r => { if (!r.ok) throw new Error(inlineUiText("读取目录失败")); return r.json(); })
      .then(value => { if (!value.topic) throw new Error(inlineUiText("该目录已更新或不存在，可回到上一级重新选择")); setTopic(value.topic); })
      .catch(e => { if (e.name !== "AbortError") setError(e.message); });
    return () => abort.abort();
  }, [active, offset]);
  async function readSource(ref: SourceRef) {
    setReading(true); setError(""); setSource(null);
    try {
      const response = await fetch(`/api/memories/${encodeURIComponent(ref.memory_id)}?bank_id=${encodeURIComponent(bankId)}`);
      if (!response.ok) throw new Error(inlineUiText("来源记录暂时不可读"));
      const memory = await response.json();
      let original: string | undefined;
      if (memory.chunk_id) {
        const chunk = await fetch(`/api/chunks/${encodeURIComponent(memory.chunk_id)}`);
        if (!chunk.ok) throw new Error(inlineUiText("记录已读取，但原文片段暂时不可读"));
        const body = await chunk.json(); original = body.chunk_text ?? body.text ?? body.content;
      }
      setSource({ id: ref.memory_id, text: memory.text, original, document_id: memory.document_id });
    } catch (e) { setError(e instanceof Error ? e.message : inlineUiText("读取失败")); throw e; }
    finally { setReading(false); }
  }
  return <section className="min-w-0 space-y-3 rounded-lg border border-cyan-200 bg-cyan-50/30 p-4 dark:border-cyan-900 dark:bg-cyan-950/20">
    <div className="flex flex-wrap items-center justify-between gap-2">
      <h3 className="font-semibold">{inlineUiText("目录下钻 · 最新版本")}</h3>
      {path.length > 1 && <button className="rounded border px-3 py-1 text-xs" onClick={() => { setPath(v => v.slice(0, -1)); setOffset(0); }}>{inlineUiText("返回上一级")}</button>}
    </div>
    <p className="text-xs text-muted-foreground">{inlineUiText("这是你正在浏览的目录，不表示该历史回合的 Agent 已经读取。")}</p>
    {error && <p role="alert" className="text-sm text-red-700">{error}</p>}
    {!topic && !error && <p className="text-sm text-muted-foreground">{inlineUiText("正在读取目录…")}</p>}
    {topic && <>
      <div><h4 className="font-medium">{topic.level ?? inlineUiText("目录")} · {topic.title}</h4><p className="mt-1 text-sm leading-6">{topic.navigation_summary}</p>
        <p className="mt-1 text-xs text-muted-foreground">{topic.memory_count ?? "—"} {inlineUiText("条记录 ·")} {topic.source_count ?? "—"} {inlineUiText("个来源文档")}</p></div>
      {topic.child_previews?.length ? <div className="grid gap-2 sm:grid-cols-2">{topic.child_previews.map(child => <button key={child.topic_id} className="min-w-0 rounded border bg-background p-3 text-left hover:border-cyan-500 focus-visible:outline-cyan-600" onClick={() => { setPath(v => [...v, child.topic_id]); setOffset(0); }}>
        <div className="text-sm font-medium">{child.title}</div><p className="mt-1 text-xs leading-5 text-muted-foreground">{child.navigation_summary}</p><span className="mt-2 block text-xs text-cyan-800 dark:text-cyan-200">{child.memory_count} {inlineUiText("条 · 查看 L1 与来源 →")}</span>
      </button>)}</div> : <>
        <p className="whitespace-pre-wrap text-sm leading-6">{topic.overview}</p>
        {topic.entities?.length ? <p className="text-xs text-muted-foreground">{inlineUiText("搜索线索：")}{topic.entities.join("、")}</p> : null}
        <h4 className="text-sm font-medium">{inlineUiText("L2 · 来源入口")}</h4>
        <p className="text-xs text-muted-foreground">{inlineUiText("这些是导航归属和来源定位；事实、主体及时间需读原文确认。")}</p>
        {(topic.source_locators ?? []).map((ref, index) => <ActionButton preserveLabel variant="outline" disabled={reading} key={`sample:${ref.memory_id}`} className="block h-auto w-full min-w-0 whitespace-normal rounded border bg-background px-3 py-2 text-left text-xs text-foreground hover:border-cyan-500" onAction={() => readSource(ref)}><span className="line-clamp-2 leading-5">{ref.preview || ref.document_id || ref.memory_id}</span><span className="mt-1 block text-cyan-800 dark:text-cyan-200">{inlineUiText("查看来源")} {index + 1} · {ref.source_status || inlineUiText("原文待回读")}</span></ActionButton>)}
        {topic.evidence_page && <details><summary className="cursor-pointer text-sm">{inlineUiText("全部来源定位 ·")} {topic.evidence_page.total} 条</summary><div className="mt-2 space-y-2">{topic.evidence_page.items.map(ref => <ActionButton preserveLabel variant="outline" key={ref.memory_id} disabled={reading} className="block h-auto w-full truncate rounded border px-3 py-2 text-left text-xs text-foreground" onAction={() => readSource(ref)}>{ref.document_id || ref.memory_id}</ActionButton>)}</div>
          <div className="mt-3 flex gap-2"><button disabled={offset === 0} className="rounded border px-2 py-1 text-xs disabled:opacity-40" onClick={() => setOffset(Math.max(0, offset - 8))}>{inlineUiText("上一页")}</button><button disabled={topic.evidence_page.next_offset == null} className="rounded border px-2 py-1 text-xs disabled:opacity-40" onClick={() => setOffset(topic.evidence_page?.next_offset ?? offset)}>{inlineUiText("下一页")}</button></div>
        </details>}
      </>}
      {reading && <p className="text-sm">{inlineUiText("正在回读来源…")}</p>}
      {source && <div className="min-w-0 space-y-2 rounded border bg-background p-3 [overflow-wrap:anywhere]"><h4 className="font-medium">{inlineUiText("L2 记录与原文")}</h4><p className="text-xs text-muted-foreground">{inlineUiText("来源：")}{source.document_id} {inlineUiText("· 历史记录，未自动核实为当前事实。")}</p><p className="whitespace-pre-wrap text-sm leading-6">{source.text}</p><details open><summary className="cursor-pointer text-xs font-medium">{inlineUiText("原文片段")}</summary><p className="mt-2 max-h-72 overflow-y-auto whitespace-pre-wrap text-xs leading-6">{source.original || inlineUiText("该记录未提供可回读的原文片段。")}</p></details></div>}
    </>}
  </section>;
}
