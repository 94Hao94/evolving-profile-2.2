"use client";
import { useTranslations } from "next-intl";
export function GraphWindowControls({ windowed }: { windowed: { data: any;error:string|null;loading:boolean;go:(offset:number)=>void;refresh:()=>void } }) {
  const t = useTranslations("releaseUi");
  const data = windowed.data;
  return <div className="mb-3 flex flex-wrap items-center gap-2 text-xs">
    {windowed.loading ? <p role="status">{t("scenarioLoading")}</p> : null}
    {windowed.error ? <p role="alert">{t(windowed.error === "stale" ? "scenarioStaleVersion" : "scenarioLoadError")}</p> : null}
    {data ? <><span>{t("scenarioSampleCount", { shown:data.graph.nodes.length,total:data.page.total,indexed:data.page.total,unit:t("scenarioWindowNodes") })}</span>
      <button type="button" className="rounded border px-2 py-1 disabled:opacity-40" disabled={data.page.offset===0} onClick={()=>windowed.go(Math.max(0,data.page.offset-data.page.limit))}>{t("scenarioPrevious")}</button>
      <button type="button" className="rounded border px-2 py-1 disabled:opacity-40" disabled={data.page.nextOffset==null} onClick={()=>{if(data.page.nextOffset!=null)windowed.go(data.page.nextOffset);}}>{t("scenarioNext")}</button></> : null}
    <button type="button" className="rounded border px-2 py-1" onClick={windowed.refresh}>{t("scenarioRefresh")}</button>
    {data ? <span>{t("scenarioEdgeWindow")}</span> : null}
  </div>;
}
