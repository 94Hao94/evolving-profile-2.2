import manifest from "../../../config/memory-recovery.json";
import { withBasePath } from "@/lib/base-path";

export const RECOVERY_DIMENSIONS = manifest.dimensions;
export const RECOVERY_STAGES = manifest.stages;
export type RecoveryMode = "pending" | "all" | "date_range";
export const RECOVERY_DEFAULT_MODE = manifest.default_mode as RecoveryMode;
export type RecoveryStage = {
  id: string;
  status: string;
  processed: number;
  total: number | null;
  failed: number;
  error_code?: string;
  detail?: string;
};
export type RecoveryDimension = {
  id: string;
  status: string;
  pending_count: number | null;
  source_count: number | null;
  error_code?: string;
  detail?: string;
  processed?: number;
  failed?: number;
};
export type RecoveryJob = {
  job_id: string;
  bank_id: string;
  status: string;
  stages: RecoveryStage[];
  dimensions?: RecoveryDimension[];
  created_at: string;
  updated_at: string;
  completed_at?: string | null;
  can_resume: boolean;
  cancel_requested: boolean;
  error_code?: string;
};
export type RecoveryPreview = {
  plan_id: string;
  bank_id: string;
  source_revision: string;
  mode: RecoveryMode;
  dimensions: RecoveryDimension[];
  estimated_tokens: number | null;
  requires_provider: boolean;
};
export type RecoveryResponse = { preview?: RecoveryPreview; job?: RecoveryJob | null };
export type RecoveryRequest =
  | {
      action: "preview";
      bank_id: string;
      mode: RecoveryMode;
      dimensions: string[];
      from: string | null;
      to: string | null;
      session_ids: string[];
    }
  | { action: "start"; bank_id: string; plan_id: string; idempotency_key: string }
  | { action: "resume" | "cancel"; bank_id: string; job_id: string };

export function isRecoveryActive(status?: string) {
  return status === "queued" || status === "running";
}

// Public protocol codes only. Arbitrary provider bodies and logs never become UI text.
const RECOVERY_ERROR_MESSAGES: Record<string, string> = {
  invalid_date_range: "invalidDateRange",
  invalid_range: "invalidDateRange",
  invalid_from: "invalidDateRange",
  invalid_to: "invalidDateRange",
  invalid_recovery_range: "invalidDateRange",
  invalid_recovery_date: "invalidDateRange",
  invalid_bank_id: "errors.invalidBank",
  recovery_scope_mismatch: "errors.invalidBank",
  invalid_session_ids: "errors.invalidSessions",
  invalid_recovery_sessions: "errors.invalidSessions",
  invalid_dimensions: "errors.invalidDimensions",
  invalid_recovery_dimensions: "errors.invalidDimensions",
  invalid_mode: "errors.invalidRequest",
  invalid_request: "errors.invalidRequest",
  invalid_action: "errors.invalidRequest",
  invalid_json: "errors.invalidRequest",
  request_too_large: "errors.invalidRequest",
  invalid_idempotency_key: "errors.invalidRequest",
  invalid_plan_id: "errors.invalidRequest",
  invalid_job_id: "errors.invalidRequest",
  invalid_recovery_request: "errors.invalidRequest",
  invalid_recovery_action: "errors.invalidRequest",
  invalid_recovery_start: "errors.invalidRequest",
  invalid_recovery_job: "errors.invalidRequest",
  plan_not_found: "errors.planUnavailable",
  plan_source_changed: "errors.planUnavailable",
  plan_stale: "errors.planUnavailable",
  stale_plan: "errors.planUnavailable",
  source_revision_changed: "errors.planUnavailable",
  job_not_found: "errors.jobUnavailable",
  idempotency_conflict: "errors.requestConflict",
  provider_unavailable: "errors.providerUnavailable",
  provider_configuration_unavailable: "errors.providerUnavailable",
  preference_provider_unavailable: "errors.providerUnavailable",
  mental_model_provider_unavailable: "errors.providerUnavailable",
  scenario_model_unavailable: "errors.providerUnavailable",
  remote_retain_failed: "errors.providerUnavailable",
  remote_consolidation_failed: "errors.providerUnavailable",
  async_acceptance_missing: "errors.providerUnavailable",
  recovery_backend_timeout: "errors.backendTimeout",
  recovery_backend_unavailable: "errors.backendUnavailable",
  recovery_backend_failed: "errors.backendUnavailable",
  recovery_unavailable: "errors.backendUnavailable",
  recovery_invalid_backend_response: "errors.backendUnavailable",
  recovery_response_too_large: "errors.backendUnavailable",
  recovery_worker_start_failed: "errors.workerUnavailable",
  recovery_worker_interrupted: "errors.workerInterrupted",
  recovery_origin_rejected: "errors.originRejected",
  verified_project_scope_missing: "errors.scopeReview",
  project_source_coverage_review_required: "errors.scopeReview",
  scenario_source_coverage_not_accepted: "errors.scopeReview",
  scenario_source_coverage_not_accepted_or_budget_exceeded: "errors.scopeReview",
  source_timestamp_unavailable: "errors.sourceUnknown",
  raw_gap_inventory_requires_background_source_inspection: "previewInspectionHelp",
  remote_inventory_not_read_in_preview: "previewInspectionHelp",
  retain_intent_source_missing: "errors.sourceUnknown",
  consolidation_inventory_unknown: "errors.sourceUnknown",
  retain_acknowledgement_conflict: "errors.requestConflict",
  shared_retention_worker_busy: "errors.workerBusy",
  shared_guidance_worker_busy: "errors.workerBusy",
};

export function recoveryErrorMessageKey(code: unknown): string {
  return typeof code === "string" && Object.hasOwn(RECOVERY_ERROR_MESSAGES, code)
    ? RECOVERY_ERROR_MESSAGES[code]
    : "requestFailed";
}

export function buildRecoverySelection(
  bank: string,
  mode: RecoveryMode,
  dimensions: string[],
  from: string,
  to: string,
  sessions: string
): RecoveryRequest {
  if (
    mode === "date_range" &&
    (!from ||
      !to ||
      !Number.isFinite(Date.parse(from)) ||
      !Number.isFinite(Date.parse(to)) ||
      Date.parse(from) > Date.parse(to))
  )
    throw new Error("invalid_date_range");
  return {
    action: "preview",
    bank_id: bank,
    mode,
    dimensions,
    from: mode === "date_range" ? new Date(from).toISOString() : null,
    to: mode === "date_range" ? new Date(to).toISOString() : null,
    session_ids: [...new Set(sessions.split(/[\s,]+/).filter(Boolean))],
  };
}

/** One controller per mounted dialog: closing or switching Bank cancels every in-flight read/action. */
export function createRecoveryClient(fetcher: typeof fetch = (...args) => fetch(...args)) {
  const controllers = new Set<AbortController>();
  async function request(bank: string, init: RequestInit, query = ""): Promise<RecoveryResponse> {
    const controller = new AbortController();
    controllers.add(controller);
    try {
      const response = await fetcher(
        withBasePath(`/api/evolving-profile/memory-recovery${query}`),
        { ...init, cache: "no-store", signal: controller.signal }
      );
      if (controller.signal.aborted) throw new DOMException("Aborted", "AbortError");
      const result = await response.json().catch(() => null);
      if (controller.signal.aborted) throw new DOMException("Aborted", "AbortError");
      if (
        !response.ok ||
        result?.error ||
        result?.error_code ||
        !result ||
        typeof result !== "object"
      ) {
        const code = result?.error_code ?? result?.error?.code;
        throw new Error(
          recoveryErrorMessageKey(code) !== "requestFailed" ? code : "recovery_request_failed"
        );
      }
      if (
        (result.job && result.job.bank_id !== bank) ||
        (result.preview && result.preview.bank_id !== bank)
      )
        throw new Error("recovery_scope_mismatch");
      return result;
    } finally {
      controllers.delete(controller);
    }
  }
  return {
    abort() {
      for (const controller of controllers) controller.abort();
      controllers.clear();
    },
    status(bank: string, job?: string) {
      return request(
        bank,
        { method: "GET" },
        `?${new URLSearchParams({ bank_id: bank, ...(job ? { job_id: job } : {}) })}`
      );
    },
    action(input: RecoveryRequest) {
      return request(input.bank_id, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(input),
      });
    },
  };
}
