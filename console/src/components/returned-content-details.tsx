"use client";
import { useState } from "react";
import { useTranslations } from "next-intl";
import { ActionButton } from "./ui/action-button";
import type { ReturnedContentItem } from "@/lib/returned-content-snapshot";

export function ReturnedContentDetails({item}:{item:ReturnedContentItem}){
 const t=useTranslations("releaseUi");
 const [body,setBody]=useState(item.text),[nextOffset,setNextOffset]=useState<number|null>(0),[expanded,setExpanded]=useState(false),[complete,setComplete]=useState(false);
 const canExpand=item.text_truncated&&item.snapshot&&typeof item.item_index==="number"&&nextOffset!==null;
 const read=async()=>{
  const snapshot=item.snapshot!;
  const query=new URLSearchParams({check_id:snapshot.check_id,session_id:snapshot.session_id,turn_id:snapshot.turn_id,tool_call_id:snapshot.tool_call_id,item_index:String(item.item_index),offset:String(nextOffset)});
  const response=await fetch(`/api/evolving-profile/returned-content/${snapshot.ref}?${query}`,{cache:"no-store"});
  if(!response.ok)throw new Error(t("returnedSnapshotFailure"));
  const result=await response.json();
  if(result.id!==item.id||typeof result.text!=="string"||item.text_sha256&&result.text_sha256!==item.text_sha256)throw new Error(t("returnedSnapshotFailure"));
  setBody(previous=>expanded?previous+result.text:result.text);setNextOffset(result.next_offset);setExpanded(true);setComplete(result.complete===true);
 };
 return <section className="my-3 rounded-lg border bg-card p-3 text-sm" data-testid="returned-content-details">
  <p className="font-semibold">{t("returnedSnapshotTitle")}</p><p className="mt-1 text-xs text-muted-foreground">{t("returnedSnapshotBoundary")}</p>
  <code className="mt-2 block break-all text-xs" data-i18n-ignore="true">{item.id}</code>
  <div className="mt-2 max-h-80 overflow-y-auto whitespace-pre-wrap break-words leading-6" data-i18n-ignore="true">{body}</div>
  {Array.isArray(item.applies_when)&&item.applies_when.length ? <p className="mt-2"><b>{t("returnedSnapshotApplies")}: </b><span data-i18n-ignore="true">{item.applies_when.filter(value=>typeof value==="string").join(" · ")}</span></p>:null}
  {Array.isArray(item.exceptions)&&item.exceptions.length ? <p className="mt-1"><b>{t("returnedSnapshotExceptions")}: </b><span data-i18n-ignore="true">{item.exceptions.filter(value=>typeof value==="string").join(" · ")}</span></p>:null}
  {canExpand||complete?<ActionButton className="mt-2" size="sm" variant="outline" disabled={complete} onAction={read} successLabel={t("returnedSnapshotDone")} resetKey={item.snapshot?.ref}>{complete?t("returnedSnapshotDone"):expanded?t("returnedSnapshotMore"):t("returnedSnapshotExpand")}</ActionButton>:null}
  {item.text_truncated&&(!item.snapshot||expanded&&nextOffset===null&&!complete)?<p className="mt-2 text-xs text-muted-foreground">{t("returnedSnapshotPartial")}</p>:null}
 </section>;
}
