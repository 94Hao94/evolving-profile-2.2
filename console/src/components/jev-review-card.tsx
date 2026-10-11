"use client";
import { useTranslations } from "next-intl";

export function JevReviewCard({ review }: { review: any }) {
  const t = useTranslations("jevScopes");
  const label = (key: string) => t.has(key) ? t(key) : key;
  return <div role="status" className="mt-3 rounded-lg border bg-card p-4 text-xs">
    <div className="font-semibold">{t("status")}: {label(review?.status || "noReview")}{review?.cache_hit ? ` · ${t("cached")}` : ""}</div>
    <p className="mt-1 text-muted-foreground">{label(review?.effect || "advisory")}</p>
    <dl className="mt-2 space-y-1">{Object.entries(review?.answers || {}).map(([key, answer]: [string, any]) => <div key={key} className="flex gap-2"><dt>{label(key)}:</dt><dd>{label(answer.choice || "unknown")}</dd></div>)}</dl>
    <div className="mt-2 text-muted-foreground">{typeof review?.latency_ms === "number" ? `${review.latency_ms} ms · ` : ""}{t("calls")}: {review?.calls ?? 0}{typeof review?.usage?.input_tokens === "number" ? ` · ${review.usage.input_tokens} Token` : ""}</div>
  </div>;
}
