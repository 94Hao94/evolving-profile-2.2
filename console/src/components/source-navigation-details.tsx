import { useTranslations } from "next-intl";
import type { SourceNavigation } from "@/lib/tool-receipt";

/** Render a returned locator without implying a fact, body or answer adoption. */
export function SourceNavigationDetails({ locator }: {locator: SourceNavigation}) {
  const t = useTranslations("releaseUi");
  return <div className="mt-2 rounded border bg-background p-3 text-sm break-words" data-testid="source-navigation-details">
    <p className="font-semibold">{t("sourceNavigationUnverified")}</p>
    <p className="mt-1 text-muted-foreground">{t("sourceNavigationBoundary")}</p>
    <dl className="mt-2 space-y-1" data-i18n-ignore="true">
      <div><dt className="inline">memory_id: </dt><dd className="inline break-all">{locator.memory_id}</dd></div>
      {locator.document_id ? <div><dt className="inline">document_id: </dt><dd className="inline break-all">{locator.document_id}</dd></div> : null}
      {locator.chunk_id ? <div><dt className="inline">chunk_id: </dt><dd className="inline break-all">{locator.chunk_id}</dd></div> : null}
      {locator.source_revision ? <div><dt className="inline">source_revision: </dt><dd className="inline break-all">{locator.source_revision}</dd></div> : null}
    </dl>
    {locator.next_action ? <p className="mt-2" data-testid="source-navigation-read-hint">{t("sourceNavigationReadHint")} <code data-i18n-ignore="true">{JSON.stringify(locator.next_action)}</code></p> : null}
  </div>;
}
