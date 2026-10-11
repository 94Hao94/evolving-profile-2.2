import type {ReactNode} from "react";
import {useTranslations} from "next-intl";

export type PromptSourceCounts={natural:number|null;audit:number|null;verification?:"partial"|"complete"|null;pending?:number|null};

const knownCount=(value:unknown):value is number=>typeof value==="number" && Number.isSafeInteger(value) && value>=0;
const countLabel=(value:unknown)=>knownCount(value)?value:"—";

export function PromptSourceSummary({counts,children}:{counts:PromptSourceCounts;children?:ReactNode}){
  const text=useTranslations("releaseUi");
  const status=counts.verification==="partial" || (knownCount(counts.pending) && counts.pending>0) ? "partial"
    : counts.verification==="complete" && counts.pending===0 && knownCount(counts.natural) && knownCount(counts.audit) ? "complete":"unknown";
  return <div className="max-w-full rounded-xl border bg-background px-3 py-1.5 text-xs text-muted-foreground shadow-sm" data-source-verification={status} role="status">
    <div className="flex flex-wrap items-center gap-x-2">
      <span>{text("promptSourceAuditCounts")}: {countLabel(counts.natural)} / {countLabel(counts.audit)}</span>
      {children}
    </div>
    <p className="mt-1 max-w-prose leading-4">{text("promptSourceScope")}</p>
    {status==="partial" ? <p className="mt-1 max-w-prose leading-4">{text("promptSourceCoveragePartial",{pending:countLabel(counts.pending)})}</p>
      : status==="unknown" ? <p className="mt-1 max-w-prose leading-4">{text("promptSourceCoverageUnknown")}</p>:null}
  </div>;
}
