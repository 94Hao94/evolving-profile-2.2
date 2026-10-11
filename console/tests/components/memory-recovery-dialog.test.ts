import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { NextIntlClientProvider } from "next-intl";
import { describe, expect, it } from "vitest";
import en from "@/messages/en.json";
import zh from "@/messages/zh-CN.json";
import {
  MemoryRecoveryEntry,
  RecoveryProgress,
  RecoveryPreviewDetails,
} from "@/components/memory-recovery-dialog";
import type { RecoveryJob, RecoveryPreview } from "@/lib/memory-recovery";

const render = (element: ReturnType<typeof createElement>, chinese = false) =>
  renderToStaticMarkup(
    createElement(NextIntlClientProvider, {
      locale: chinese ? "zh-CN" : "en",
      timeZone: "UTC",
      messages: chinese ? zh : en,
      children: element,
    })
  );
describe("manual recovery presentation", () => {
  it("presents deliberately unread inventory as an informational notice rather than a source failure", () => {
    const preview: RecoveryPreview = {
      plan_id: "p",
      bank_id: "bank-a",
      source_revision: "rev",
      mode: "pending",
      estimated_tokens: null,
      requires_provider: true,
      dimensions: [
        {
          id: "observations",
          status: "inventory_pending",
          pending_count: null,
          source_count: null,
          error_code: "remote_inventory_not_read_in_preview",
        },
      ],
    };
    const html = render(createElement(RecoveryPreviewDetails, { preview }));
    expect(html).toContain("Awaiting inventory");
    expect(html).toContain("Preview does not call a model");
    expect(html).not.toContain("Source unavailable");
    expect(html).not.toContain("text-destructive");
  });
  it("retains per-dimension warning explanations and unknown counts after reopening a saved job", () => {
    const job: RecoveryJob = {
      job_id: "job-a",
      bank_id: "bank-a",
      status: "partial",
      stages: [],
      dimensions: [
        {
          id: "project_summaries",
          status: "review_pending",
          pending_count: 2,
          source_count: null,
          error_code: "verified_project_scope_missing",
        },
      ],
      created_at: "now",
      updated_at: "now",
      can_resume: true,
      cancel_requested: false,
    };
    const html = render(createElement(RecoveryProgress, { job }));
    expect(html).toContain("Pending: 2");
    expect(html).toContain("Sources: Unknown");
    expect(html).toContain("Verified project scope or source coverage is missing");
  });
  it("explains known provider errors and suppresses unknown provider strings", () => {
    const base: RecoveryJob = {
      job_id: "job-a",
      bank_id: "bank-a",
      status: "waiting_provider",
      stages: [],
      created_at: "now",
      updated_at: "now",
      can_resume: true,
      cancel_requested: false,
    };
    expect(
      render(
        createElement(RecoveryProgress, { job: { ...base, error_code: "provider_unavailable" } })
      )
    ).toContain("Check model and API configuration");
    expect(
      render(
        createElement(RecoveryProgress, { job: { ...base, error_code: "provider_unavailable" } }),
        true
      )
    ).toContain("检查模型与 API 配置");
    const unsafe = render(
      createElement(RecoveryProgress, {
        job: { ...base, error_code: "sk-private-provider-secret" },
      })
    );
    expect(unsafe).not.toContain("sk-private-provider-secret");
  });
  it("offers the same visible localized entry at header and overview", () => {
    for (const location of ["header", "overview"] as const) {
      expect(render(createElement(MemoryRecoveryEntry, { bankId: "bank-a", location }))).toContain(
        "Recover and refine memory"
      );
      expect(
        render(createElement(MemoryRecoveryEntry, { bankId: "bank-a", location }), true)
      ).toContain("补录与提炼");
    }
  });
  it("shows unknown preview counts and skipped/unsupported evidence without inventing success", () => {
    const preview: RecoveryPreview = {
      plan_id: "p",
      bank_id: "bank-a",
      source_revision: "rev",
      mode: "pending",
      estimated_tokens: null,
      requires_provider: true,
      dimensions: [
        { id: "facts", status: "disabled", pending_count: null, source_count: null },
        {
          id: "observations",
          status: "unsupported_scope",
          pending_count: null,
          source_count: null,
        },
      ],
    };
    const html = render(createElement(RecoveryPreviewDetails, { preview }));
    expect(html).toContain("Unknown");
    expect(html).toContain("Disabled");
    expect(html).toContain("Unsupported scope");
    expect(html).not.toContain("Completed");
  });
  it("distinguishes provider waiting, review, failure and complete, with durable stage progress", () => {
    const job: RecoveryJob = {
      job_id: "job-a",
      bank_id: "bank-a",
      status: "waiting_provider",
      stages: [{ id: "retain", status: "waiting_provider", processed: 2, total: 7, failed: 1 }],
      created_at: "2026-10-08T00:00:00Z",
      updated_at: "2026-10-08T00:01:00Z",
      can_resume: true,
      cancel_requested: false,
    };
    const html = render(createElement(RecoveryProgress, { job }));
    expect(html).toContain("Waiting for provider");
    expect(html).toContain("2 / 7");
    expect(html).toContain("Failed: 1");
    expect(html).not.toContain("Completed");
    for (const [status, label] of [
      ["review_pending", "Awaiting review"],
      ["failed", "Failed"],
      ["complete", "Completed"],
    ])
      expect(
        render(createElement(RecoveryProgress, { job: { ...job, status, stages: [] } }))
      ).toContain(label);
  });
});
