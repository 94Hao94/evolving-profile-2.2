import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { NextIntlClientProvider } from "next-intl";
import { beforeEach, describe, expect, it, vi } from "vitest";
import en from "@/messages/en.json";
import { createActionFeedback } from "@/lib/action-feedback";
import { ActionButton, FeedbackButton, ActionMenuItem } from "@/components/ui/action-button";
import * as DropdownMenuPrimitive from "@radix-ui/react-dropdown-menu";

// Node-only Vitest has no DOM. Render the real button at snapshots from the
// real controller; only React's subscription bridge needs substitution.
let feedback: ReturnType<typeof createActionFeedback>;
vi.mock("@/lib/action-feedback", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/action-feedback")>();
  return {
    ...actual,
    useActionFeedback: () => ({
      ...feedback.getSnapshot(),
      run: feedback.run,
      reset: feedback.reset,
    }),
  };
});

function renderButton(icon = false) {
  return renderToStaticMarkup(
    createElement(NextIntlClientProvider, {
      locale: "en",
      messages: en,
      children: createElement(
        ActionButton,
        { onAction: async () => true, ...(icon ? { size: "icon", "aria-label": "Refresh" } : {}) },
        icon ? createElement("svg", { "aria-hidden": true }) : "Save"
      ),
    })
  );
}

function renderMenu() {
  return renderToStaticMarkup(
    createElement(NextIntlClientProvider, {
      locale: "en",
      messages: en,
      children: createElement(
        DropdownMenuPrimitive.Root,
        { open: true },
        createElement(DropdownMenuPrimitive.Trigger, null, "Actions"),
        createElement(
          DropdownMenuPrimitive.Content,
          { forceMount: true },
          createElement(
            ActionMenuItem,
            { onAction: async () => true },
            createElement("svg", { "aria-hidden": true }),
            "Rebuild model"
          )
        )
      ),
    })
  );
}

describe("ActionButton local feedback", () => {
  beforeEach(() => {
    feedback = createActionFeedback();
  });

  it("disables the button and shows a visible busy state while its request is unresolved", async () => {
    let complete!: () => void;
    const pending = feedback.run(
      () =>
        new Promise<void>((resolve) => {
          complete = resolve;
        })
    );
    const html = renderButton();
    expect(html).toContain('aria-busy="true"');
    expect(html).toContain('disabled=""');
    expect(html).toContain("Working");
    complete();
    await pending;
  });

  it("shows green success after completion", async () => {
    await feedback.run(async () => true);
    const html = renderButton();
    expect(html).toContain('data-action-status="success"');
    expect(html).toContain("emerald");
    expect(html).toContain("Completed");
    expect(html).not.toContain('disabled=""');
  });

  it("shows the actual failure adjacent to a red retryable button", async () => {
    await feedback.run(async () => {
      throw new Error("Endpoint refused the request");
    });
    const html = renderButton();
    expect(html).toContain('data-action-status="error"');
    expect(html).toContain("red");
    expect(html).toContain('role="alert"');
    expect(html).toContain("Endpoint refused the request");
    expect(html).toContain("Retry");
    expect(html).not.toContain('disabled=""');
  });

  it("keeps an icon-only action name in its accessible completed label", async () => {
    await feedback.run(async () => true);
    const html = renderButton(true);
    expect(html).toContain('aria-label="Refresh: Completed"');
    expect(html).toContain("sr-only");
  });

  it("preserves descriptive source content alongside completion feedback", async () => {
    await feedback.run(async () => true);
    const html = renderToStaticMarkup(
      createElement(NextIntlClientProvider, {
        locale: "en",
        messages: en,
        children: createElement(
          ActionButton,
          { onAction: async () => true, preserveLabel: true },
          createElement("span", null, "Budget policy source"),
          createElement("small", null, "Document 42")
        ),
      })
    );
    expect(html).toContain("Budget policy source");
    expect(html).toContain("Document 42");
    expect(html).toContain("Completed");
    expect(html).not.toContain('aria-label="Completed"');
  });

  it("keeps rich source labels readable on a light success background", () => {
    const html = renderToStaticMarkup(
      createElement(NextIntlClientProvider, {
        locale: "en",
        messages: en,
        children: createElement(
          FeedbackButton,
          { status: "success", preserveLabel: true },
          createElement("span", { className: "text-blue-700" }, "Evidence"),
          createElement("span", { className: "text-foreground" }, "Budget policy source")
        ),
      })
    );
    expect(html).toContain("bg-emerald-50");
    expect(html).not.toContain("bg-emerald-600");
    expect(html).toContain('class="text-foreground">Budget policy source');
    expect(html).toMatch(/<span class="[^"]*text-emerald-800[^"]*">Completed/);
  });

  it("keeps preserved failure labels on a light error background", () => {
    const html = renderToStaticMarkup(
      createElement(NextIntlClientProvider, {
        locale: "en",
        messages: en,
        children: createElement(
          FeedbackButton,
          { status: "error", preserveLabel: true, error: "Source unavailable" },
          createElement("span", { className: "text-foreground" }, "Read policy source")
        ),
      })
    );
    expect(html).toContain("bg-red-50");
    expect(html).not.toContain("bg-red-600");
    expect(html).toMatch(/<span class="[^"]*text-red-800[^"]*">Failed/);
  });

  it("keeps asChild source contents without nesting its original element", () => {
    const html = renderToStaticMarkup(
      createElement(NextIntlClientProvider, {
        locale: "en",
        messages: en,
        children: createElement(
          FeedbackButton,
          { status: "success", preserveLabel: true, asChild: true },
          createElement("button", null, "Read source")
        ),
      })
    );
    expect(html).toContain("Read source");
    expect(html).toContain("Completed");
    expect((html.match(/<button/g) ?? []).length).toBe(1);
  });

  it("renders controlled form failures with local detail and submit semantics", () => {
    const html = renderToStaticMarkup(
      createElement(NextIntlClientProvider, {
        locale: "en",
        messages: en,
        children: createElement(
          FeedbackButton,
          { status: "error", error: "Save was rejected", type: "submit" },
          "Save settings"
        ),
      })
    );
    expect(html).toContain('type="submit"');
    expect(html).toContain('role="alert"');
    expect(html).toContain("Save was rejected");
  });

  it("preserves asChild composition when busy feedback replaces a child label", () => {
    const html = renderToStaticMarkup(
      createElement(NextIntlClientProvider, {
        locale: "en",
        messages: en,
        children: createElement(
          FeedbackButton,
          { status: "pending", asChild: true },
          createElement("button", null, "Save")
        ),
      })
    );
    expect(html).toContain('aria-busy="true"');
    expect(html).toContain("Working");
  });

  it("positions an icon overlay and its detail through the outer wrapper", () => {
    const html = renderToStaticMarkup(
      createElement(NextIntlClientProvider, {
        locale: "en",
        messages: en,
        children: createElement(
          FeedbackButton,
          {
            status: "error",
            size: "icon",
            error: "Copy refused",
            wrapperClassName: "absolute right-2 top-2",
          },
          createElement("svg")
        ),
      })
    );
    expect(html).toMatch(/^<span class="[^"]*absolute right-2 top-2/);
    expect(html).toContain("items-end");
  });

  it("renders an async menu action as one native menu item with its original name", async () => {
    await feedback.run(async () => true);
    const html = renderMenu();
    expect(html).toContain('role="menuitem"');
    expect(html).toContain("Rebuild model");
    expect(html).toContain("Completed");
    expect(html).toContain("emerald");
    expect((html.match(/<button/g) ?? []).length).toBe(1);
  });

  it("shows an async menu failure beside its still-identifiable action", async () => {
    await feedback.run(async () => {
      throw new Error("Model rebuild rejected");
    });
    const html = renderMenu();
    expect(html).toContain('role="alert"');
    expect(html).toContain("Model rebuild rejected");
    expect(html).toContain("Rebuild model");
    expect(html).toContain("red");
  });
});
