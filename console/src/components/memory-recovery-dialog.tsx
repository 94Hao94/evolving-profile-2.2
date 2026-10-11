"use client";

import { useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { RefreshCw } from "lucide-react";
import { ActionButton } from "@/components/ui/action-button";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  buildRecoverySelection,
  createRecoveryClient,
  isRecoveryActive,
  recoveryErrorMessageKey,
  RECOVERY_DIMENSIONS,
  RECOVERY_STAGES,
  RECOVERY_DEFAULT_MODE,
  type RecoveryJob,
  type RecoveryMode,
  type RecoveryPreview,
} from "@/lib/memory-recovery";

function useRecoveryLabels() {
  const t = useTranslations("memoryRecovery");
  return {
    t,
    count: (value: number | null | undefined) => (value == null ? t("unknown") : String(value)),
    status: (value: string) => (t.has(`statuses.${value}`) ? t(`statuses.${value}`) : t("unknown")),
    dimension: (value: string) => (t.has(`dimensions.${value}`) ? t(`dimensions.${value}`) : value),
    stage: (value: string) => (t.has(`stages.${value}`) ? t(`stages.${value}`) : value),
    error: (value: string) => t(recoveryErrorMessageKey(value)),
  };
}

export function RecoveryPreviewDetails({ preview }: { preview: RecoveryPreview }) {
  const { t, count, status, dimension, error } = useRecoveryLabels();
  return (
    <section className="space-y-3 rounded border p-3" data-testid="recovery-preview">
      <h3 className="font-medium">{t("previewTitle")}</h3>
      <p className="text-xs text-muted-foreground">
        {t("estimate", { tokens: count(preview.estimated_tokens) })} ·{" "}
        {preview.requires_provider ? t("providerRequired") : t("providerNotRequired")}
      </p>
      <ul className="space-y-2">
        {preview.dimensions.map((item) => (
          <li key={item.id} className="rounded border p-2 text-sm">
            <div className="flex flex-wrap justify-between gap-2">
              <span>{dimension(item.id)}</span>
              <span>{status(item.status)}</span>
            </div>
            <p className="text-xs text-muted-foreground">
              {t("counts", {
                pending: count(item.pending_count),
                source: count(item.source_count),
              })}
            </p>
            {item.error_code && (
              <p
                className={`break-all text-xs ${recoveryErrorMessageKey(item.error_code) === "previewInspectionHelp" ? "text-muted-foreground" : "text-destructive"}`}
              >
                {error(item.error_code)}
              </p>
            )}
          </li>
        ))}
      </ul>
      <p className="text-xs leading-5 text-muted-foreground">{t("previewHelp")}</p>
    </section>
  );
}

export function RecoveryProgress({ job }: { job: RecoveryJob }) {
  const { t, count, status, stage, dimension, error } = useRecoveryLabels();
  return (
    <section
      className="space-y-3 rounded border p-3"
      data-testid="recovery-progress"
      aria-live="polite"
    >
      <div className="flex flex-wrap justify-between gap-2">
        <h3 className="font-medium">{t("progress")}</h3>
        <strong>{status(job.status)}</strong>
      </div>
      <p className="break-all text-xs text-muted-foreground">
        {t("jobId")}: {job.job_id} · {t("updated")}: {job.updated_at}
      </p>
      {job.cancel_requested && <p className="text-xs">{t("cancelRequested")}</p>}
      {job.error_code && (
        <p className="break-all text-xs text-destructive">{error(job.error_code)}</p>
      )}
      <ol className="space-y-2">
        {job.stages.map((item) => (
          <li key={item.id} className="rounded border p-2 text-sm">
            <div className="flex flex-wrap justify-between gap-2">
              <span>{stage(item.id)}</span>
              <span>{status(item.status)}</span>
            </div>
            <p>
              {count(item.processed)} / {count(item.total)} ·{" "}
              {t("failedCount", { count: item.failed })}
            </p>
            {item.error_code && (
              <p className="break-all text-xs text-destructive">{error(item.error_code)}</p>
            )}
          </li>
        ))}
      </ol>
      {job.dimensions && (
        <ul className="grid gap-2 text-xs sm:grid-cols-2">
          {job.dimensions.map((item) => (
            <li className="rounded border p-2" key={item.id}>
              {dimension(item.id)} · {status(item.status)}
              <p className="mt-1 text-muted-foreground">
                {t("counts", {
                  pending: count(item.pending_count),
                  source: count(item.source_count),
                })}
              </p>
              {item.failed != null && <p>{t("failedCount", { count: item.failed })}</p>}
              {item.error_code && (
                <p
                  className={`mt-1 break-words ${recoveryErrorMessageKey(item.error_code) === "previewInspectionHelp" ? "text-muted-foreground" : "text-destructive"}`}
                >
                  {error(item.error_code)}
                </p>
              )}
              {item.detail &&
                !item.error_code &&
                recoveryErrorMessageKey(item.detail) !== "requestFailed" && (
                  <p className="mt-1 text-muted-foreground">{error(item.detail)}</p>
                )}
            </li>
          ))}
        </ul>
      )}
      <p className="text-xs leading-5 text-muted-foreground">{t("progressHelp")}</p>
    </section>
  );
}

function RecoveryControls({ bankId }: { bankId: string }) {
  const t = useTranslations("memoryRecovery");
  const [client] = useState(() => createRecoveryClient());
  const [mode, setMode] = useState<RecoveryMode>(RECOVERY_DEFAULT_MODE);
  const [dimensions, setDimensions] = useState<string[]>(() =>
    RECOVERY_DIMENSIONS.map((item) => item.id)
  );
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [sessions, setSessions] = useState("");
  const [preview, setPreview] = useState<RecoveryPreview | null>(null);
  const [job, setJob] = useState<RecoveryJob | null>(null);
  const [readError, setReadError] = useState<string | null>(null);
  const [readLoading, setReadLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const alive = useRef(true);
  const busyRef = useRef(false);
  const actionEpoch = useRef(0);
  const requestKey = useRef<{ plan: string; key: string } | null>(null);
  const selected = JSON.stringify([mode, dimensions, from, to, sessions]);
  const previewSelection = useRef<string | null>(null);
  const previewCurrent = preview !== null && previewSelection.current === selected;
  const active = isRecoveryActive(job?.status);

  useEffect(() => {
    alive.current = true;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const poll = async () => {
      const epoch = actionEpoch.current;
      try {
        const response = await client.status(bankId);
        if (stopped || !alive.current) return;
        if (epoch === actionEpoch.current && !busyRef.current) {
          setJob(response.job ?? null);
          setReadError(null);
          setReadLoading(false);
        }
      } catch (error) {
        if (!stopped && alive.current && epoch === actionEpoch.current && !busyRef.current) {
          setReadError(recoveryErrorMessageKey(error instanceof Error ? error.message : null));
          setReadLoading(false);
        }
      }
      if (!stopped && alive.current) timer = setTimeout(poll, 3000);
    };
    void poll();
    return () => {
      stopped = true;
      alive.current = false;
      if (timer) clearTimeout(timer);
      client.abort();
    };
  }, [bankId, client]);

  async function act(action: () => Promise<unknown>) {
    if (busyRef.current) return false;
    busyRef.current = true;
    actionEpoch.current += 1;
    setBusy(true);
    try {
      return await action();
    } catch (error) {
      if (!alive.current) return false;
      throw new Error(t(recoveryErrorMessageKey(error instanceof Error ? error.message : null)));
    } finally {
      busyRef.current = false;
      if (alive.current) setBusy(false);
    }
  }

  return (
    <div className="min-h-0 space-y-4 overflow-y-auto px-1 pb-1" data-testid="recovery-body">
      <p className="break-all text-sm">
        {t("bank")}: <strong>{bankId}</strong>
      </p>
      <fieldset disabled={busy || active} className="space-y-3 disabled:opacity-60">
        <label className="block space-y-1 text-sm">
          <span>{t("range")}</span>
          <select
            className="h-9 w-full rounded border bg-background px-2"
            value={mode}
            onChange={(event) => setMode(event.target.value as RecoveryMode)}
          >
            <option value="pending">{t("pendingRange")}</option>
            <option value="date_range">{t("dateRange")}</option>
            <option value="all">{t("allRange")}</option>
          </select>
        </label>
        {mode === "all" && <p className="text-xs text-muted-foreground">{t("allHelp")}</p>}
        {mode === "date_range" && (
          <div className="grid gap-3 sm:grid-cols-2">
            <label className="space-y-1 text-sm">
              <span>{t("from")}</span>
              <input
                className="h-9 w-full min-w-0 rounded border bg-background px-2"
                type="datetime-local"
                value={from}
                onChange={(event) => setFrom(event.target.value)}
              />
            </label>
            <label className="space-y-1 text-sm">
              <span>{t("to")}</span>
              <input
                className="h-9 w-full min-w-0 rounded border bg-background px-2"
                type="datetime-local"
                value={to}
                onChange={(event) => setTo(event.target.value)}
              />
            </label>
          </div>
        )}
        <label className="block space-y-1 text-sm">
          <span>{t("sessions")}</span>
          <textarea
            className="min-h-16 w-full rounded border bg-background p-2"
            value={sessions}
            onChange={(event) => setSessions(event.target.value)}
            placeholder={t("sessionsHelp")}
          />
        </label>
        <fieldset className="rounded border p-3">
          <legend className="px-1 text-sm">{t("dimensionSelection")}</legend>
          <div className="grid gap-3 sm:grid-cols-2">
            {RECOVERY_STAGES.map((stage) => (
              <fieldset key={stage.id} className="min-w-0 rounded border p-2">
                <legend className="px-1 text-xs text-muted-foreground">
                  {t(`stages.${stage.id}`)}
                </legend>
                <div className="space-y-2">
                  {RECOVERY_DIMENSIONS.filter((item) => item.stage_id === stage.id).map((item) => (
                    <label
                      key={item.id}
                      data-recovery-module={item.module}
                      className="flex items-start gap-2 text-sm"
                    >
                      <input
                        className="mt-1"
                        type="checkbox"
                        checked={dimensions.includes(item.id)}
                        onChange={(event) =>
                          setDimensions((current) =>
                            event.target.checked
                              ? [...current, item.id]
                              : current.filter((id) => id !== item.id)
                          )
                        }
                      />
                      <span>{t(`dimensions.${item.id}`)}</span>
                    </label>
                  ))}
                </div>
              </fieldset>
            ))}
          </div>
        </fieldset>
      </fieldset>
      <div className="flex flex-wrap gap-2">
        <ActionButton
          variant="outline"
          disabled={busy || active || dimensions.length === 0}
          resetKey={selected}
          onAction={() =>
            act(async () => {
              const response = await client.action(
                buildRecoverySelection(bankId, mode, dimensions, from, to, sessions)
              );
              if (!alive.current) return false;
              if (!response.preview || response.preview.bank_id !== bankId)
                throw new Error("invalid_preview");
              previewSelection.current = selected;
              setPreview(response.preview);
              requestKey.current = null;
            })
          }
          pendingLabel={t("previewing")}
          successLabel={t("previewReady")}
          errorLabel={t("requestFailed")}
        >
          {t("preview")}
        </ActionButton>
        <ActionButton
          disabled={busy || active || !previewCurrent}
          resetKey={preview?.plan_id}
          onAction={() =>
            act(async () => {
              if (!previewCurrent || !preview) return false;
              if (requestKey.current?.plan !== preview.plan_id)
                requestKey.current = { plan: preview.plan_id, key: crypto.randomUUID() };
              const response = await client.action({
                action: "start",
                bank_id: bankId,
                plan_id: preview.plan_id,
                idempotency_key: requestKey.current.key,
              });
              if (!alive.current) return false;
              if (!response.job || response.job.bank_id !== bankId) throw new Error("invalid_job");
              setJob(response.job);
            })
          }
          pendingLabel={t("starting")}
          successLabel={job?.status === "complete" ? t("statuses.complete") : t("accepted")}
          errorLabel={t("requestFailed")}
        >
          {t("start")}
        </ActionButton>
        {job?.can_resume && (
          <ActionButton
            variant="outline"
            disabled={busy || active}
            resetKey={job.job_id}
            onAction={() =>
              act(async () => {
                const response = await client.action({
                  action: "resume",
                  bank_id: bankId,
                  job_id: job.job_id,
                });
                if (alive.current) setJob(response.job ?? null);
              })
            }
            successLabel={job?.status === "complete" ? t("statuses.complete") : t("accepted")}
            errorLabel={t("requestFailed")}
          >
            {t("resume")}
          </ActionButton>
        )}
        {job && !["complete", "cancelled"].includes(job.status) && (
          <ActionButton
            variant="outline"
            disabled={busy || job.cancel_requested}
            resetKey={job.job_id}
            onAction={() =>
              act(async () => {
                const response = await client.action({
                  action: "cancel",
                  bank_id: bankId,
                  job_id: job.job_id,
                });
                if (alive.current) setJob(response.job ?? null);
              })
            }
            successLabel={t("cancelRequested")}
            errorLabel={t("requestFailed")}
          >
            {t("cancel")}
          </ActionButton>
        )}
      </div>
      {readError && (
        <p role="alert" className="text-sm text-destructive">
          {t("statusFailed")} {t(readError)}
        </p>
      )}
      {!job && readLoading && (
        <p role="status" className="text-xs text-muted-foreground">
          {t("statusReading")}
        </p>
      )}
      {!job && !readError && !readLoading && (
        <p className="text-xs text-muted-foreground">{t("noJob")}</p>
      )}
      {previewCurrent && <RecoveryPreviewDetails preview={preview} />}
      {job && <RecoveryProgress job={job} />}
    </div>
  );
}

export function MemoryRecoveryDialog({
  bankId,
  open,
  onOpenChange,
}: {
  bankId: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const t = useTranslations("memoryRecovery");
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="grid max-h-[90dvh] w-[calc(100%-1rem)] max-w-3xl grid-rows-[auto_minmax(0,1fr)] bg-background opacity-100 sm:w-full">
        <DialogHeader className="pr-6">
          <DialogTitle>{t("entry")}</DialogTitle>
          <DialogDescription>{t("description")}</DialogDescription>
        </DialogHeader>
        {open && <RecoveryControls key={bankId} bankId={bankId} />}
      </DialogContent>
    </Dialog>
  );
}

export function MemoryRecoveryEntry({
  bankId,
  location,
}: {
  bankId: string | null;
  location: "header" | "overview";
}) {
  const t = useTranslations("memoryRecovery");
  const [open, setOpen] = useState(false);
  return (
    <>
      <Button
        type="button"
        variant="outline"
        size="sm"
        disabled={!bankId}
        className="h-9 max-w-full gap-1.5 whitespace-normal text-xs sm:text-sm"
        data-memory-recovery-entry={location}
        onClick={() => setOpen(true)}
      >
        <RefreshCw className="h-4 w-4 shrink-0" aria-hidden="true" />
        <span>{t("entry")}</span>
      </Button>
      {bankId && (
        <MemoryRecoveryDialog key={bankId} bankId={bankId} open={open} onOpenChange={setOpen} />
      )}
    </>
  );
}
