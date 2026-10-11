"use client";

import { useState, useEffect, useRef } from "react";
import { useTranslations } from "next-intl";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { ActionButton } from "@/components/ui/action-button";
import { Input } from "@/components/ui/input";

interface InvalidateMemoryDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onConfirm: (reason?: string) => Promise<unknown>;
  busy?: boolean;
}

// Confirmation dialog for invalidating (soft-retiring) a memory: explains what
// invalidation does and collects an optional reason.
export function InvalidateMemoryDialog({
  open,
  onOpenChange,
  onConfirm,
  busy,
}: InvalidateMemoryDialogProps) {
  const t = useTranslations("memoryDetailPanel");
  const [reason, setReason] = useState("");
  const confirmButton = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!open) setReason("");
  }, [open]);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>{t("curationInvalidateTitle")}</DialogTitle>
          <DialogDescription>{t("curationInvalidateExplain")}</DialogDescription>
        </DialogHeader>
        <div className="space-y-1.5">
          <label className="text-xs font-medium text-muted-foreground">
            {t("curationReasonPlaceholder")}
          </label>
          <Input
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            placeholder={t("curationReasonPlaceholder")}
            autoFocus
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                confirmButton.current?.click();
              }
            }}
          />
        </div>
        <DialogFooter>
          <Button variant="ghost" disabled={busy} onClick={() => onOpenChange(false)}>
            {t("curationCancel")}
          </Button>
          <ActionButton
            ref={confirmButton}
            resetKey={`${open}:${reason}`}
            variant="destructive"
            disabled={busy}
            onAction={() => onConfirm(reason.trim() || undefined)}
          >
            {t("curationInvalidate")}
          </ActionButton>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
