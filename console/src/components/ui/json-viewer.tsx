"use client";

import { ActionButton } from "@/components/ui/action-button";
import { useTranslations } from "next-intl";
import { Check, Copy } from "lucide-react";

interface JsonViewerProps {
  /** The value to display. Objects/arrays are pretty-printed; strings render as-is. */
  value: unknown;
  /**
   * Classes for the <pre> box (background, max-height, scroll, text color).
   * Defaults to `bg-muted`. Layout/wrap classes are always applied.
   */
  className?: string;
}

function toDisplayText(value: unknown): string {
  if (typeof value === "string") return value;
  // Unescape newlines inside string values so multi-line content (e.g. prompts)
  // renders as real line breaks under `whitespace-pre-wrap` instead of literal "\n".
  return JSON.stringify(value, null, 2).replace(/\\n/g, "\n");
}

/**
 * Read-only viewer for JSON (or plain text) with word-wrapping and a copy button.
 * Used in detail dialogs to show request/response/input/output/metadata payloads.
 */
export function JsonViewer({ value, className = "bg-muted" }: JsonViewerProps) {
  const t = useTranslations("common");
  const text = toDisplayText(value);

  const handleCopy = async () => {
    await navigator.clipboard.writeText(text);
  };

  return (
    <div className="relative group">
      <div className="absolute top-1.5 right-1.5 max-w-64">
        <ActionButton
          type="button"
          onAction={handleCopy}
          resetKey={text}
          size="icon"
          variant="outline"
          title={t("copy")}
          aria-label={t("copy")}
          className="h-7 w-7"
        >
          <Copy className="w-3.5 h-3.5" />
        </ActionButton>
      </div>
      <pre className={`p-3 pr-10 rounded-md text-xs whitespace-pre-wrap break-words ${className}`}>
        {text}
      </pre>
    </div>
  );
}
