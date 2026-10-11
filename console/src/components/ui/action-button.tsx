"use client";

import * as React from "react";
import { Check, Loader2, X } from "lucide-react";
import { useTranslations } from "next-intl";
import { Button, type ButtonProps } from "@/components/ui/button";
import { DropdownMenuItem } from "@/components/ui/dropdown-menu";
import { useActionFeedback, type AsyncAction, type ActionStatus } from "@/lib/action-feedback";
import { cn } from "@/lib/utils";

interface FeedbackLabels {
  pendingLabel?: string;
  successLabel?: string;
  errorLabel?: string;
  /** Position the button and adjacent detail together, for example in an overlay. */
  wrapperClassName?: string;
}

export interface ActionButtonProps extends Omit<ButtonProps, "onClick">, FeedbackLabels {
  onAction: AsyncAction;
  resetKey?: unknown;
  preserveLabel?: boolean;
}

export interface FeedbackButtonProps extends ButtonProps, FeedbackLabels {
  status: ActionStatus;
  error?: string | null;
  preserveLabel?: boolean;
}

/** Controlled presentation for submit handlers using useActionFeedback. */
export const FeedbackButton = React.forwardRef<HTMLButtonElement, FeedbackButtonProps>(
  function FeedbackButton(
    {
      status,
      error,
      children,
      pendingLabel,
      successLabel,
      errorLabel,
      wrapperClassName,
      preserveLabel = false,
      disabled,
      className,
      size,
      type = "button",
      ...props
    },
    ref
  ) {
    const t = useTranslations("actionFeedback");
    const detailId = React.useId();
    const busy = status === "pending";
    const succeeded = status === "success";
    const failed = status === "error";
    const active = status !== "idle";
    const label = busy
      ? (pendingLabel ?? t("pending"))
      : succeeded
        ? (successLabel ?? t("success"))
        : (errorLabel ?? t("error"));
    const iconOnly = size === "icon";
    const actionName = props["aria-label"] ?? (typeof children === "string" ? children : undefined);
    const ariaLabel = active
      ? actionName
        ? `${actionName}: ${label}`
        : preserveLabel
          ? undefined
          : label
      : props["aria-label"];
    const describedBy =
      [props["aria-describedby"], active ? detailId : undefined].filter(Boolean).join(" ") ||
      undefined;
    const originalContent =
      props.asChild && React.isValidElement<{ children?: React.ReactNode }>(children)
        ? children.props.children
        : children;
    const feedbackContent = (
      <>
        {busy ? (
          <Loader2 className="animate-spin" aria-hidden="true" />
        ) : succeeded ? (
          <Check aria-hidden="true" />
        ) : failed ? (
          <X aria-hidden="true" />
        ) : null}
        {preserveLabel ? originalContent : null}
        <span
          className={cn(
            iconOnly ? "sr-only" : preserveLabel ? "text-xs font-medium" : undefined,
            preserveLabel && succeeded && "text-emerald-800 dark:text-emerald-200",
            preserveLabel && failed && "text-red-800 dark:text-red-200"
          )}
        >
          {label}
        </span>
      </>
    );
    const content = active
      ? props.asChild && React.isValidElement<{ children?: React.ReactNode }>(children)
        ? React.cloneElement(children, {}, feedbackContent)
        : feedbackContent
      : children;

    return (
      <span
        className={cn(
          "inline-flex max-w-full min-w-0 flex-col gap-1 align-top",
          className?.split(/\s+/).includes("w-full") && "w-full",
          iconOnly && "max-w-[min(20rem,100%)] items-end",
          wrapperClassName
        )}
      >
        <Button
          {...props}
          ref={ref}
          type={type}
          size={size}
          disabled={disabled || busy}
          aria-busy={busy}
          aria-label={ariaLabel}
          aria-describedby={describedBy}
          title={failed ? t("retry") : props.title}
          data-action-status={status}
          className={cn(
            className,
            succeeded &&
              (preserveLabel
                ? "border-emerald-300 bg-emerald-50 text-foreground hover:bg-emerald-100 dark:border-emerald-800 dark:bg-emerald-950 dark:hover:bg-emerald-900"
                : "border-emerald-600 bg-emerald-600 text-white hover:bg-emerald-700"),
            failed &&
              (preserveLabel
                ? "border-red-300 bg-red-50 text-foreground hover:bg-red-100 dark:border-red-800 dark:bg-red-950 dark:hover:bg-red-900"
                : "border-red-600 bg-red-600 text-white hover:bg-red-700")
          )}
        >
          {content}
        </Button>
        {active && (
          <span
            id={detailId}
            role={failed ? "alert" : "status"}
            aria-live={failed ? "assertive" : "polite"}
            className={cn(
              "max-w-80 whitespace-normal break-words text-xs",
              iconOnly && "max-w-full text-right",
              failed
                ? "text-red-600 dark:text-red-400"
                : succeeded
                  ? "text-emerald-700 dark:text-emerald-400"
                  : "text-muted-foreground"
            )}
          >
            {failed ? `${error ?? label} · ${t("retry")}` : label}
          </span>
        )}
      </span>
    );
  }
);

/** Callers must throw failures and may return false to cancel an action. */
export const ActionButton = React.forwardRef<HTMLButtonElement, ActionButtonProps>(
  function ActionButton({ onAction, resetKey, disabled, ...props }, ref) {
    const feedback = useActionFeedback();
    React.useEffect(() => {
      feedback.reset();
    }, [resetKey, feedback.reset]);
    return (
      <FeedbackButton
        {...props}
        ref={ref}
        disabled={disabled}
        status={feedback.status}
        error={feedback.error}
        onClick={() => {
          if (!disabled) void feedback.run(onAction);
        }}
      />
    );
  }
);

export interface ActionMenuItemProps
  extends
    Omit<React.ComponentPropsWithoutRef<typeof DropdownMenuItem>, "onSelect" | "onClick">,
    FeedbackLabels {
  onAction: AsyncAction;
  resetKey?: unknown;
}

/** Radix's selection event covers pointer clicks and keyboard activation. */
export const ActionMenuItem = React.forwardRef<
  React.ElementRef<typeof DropdownMenuItem>,
  ActionMenuItemProps
>(function ActionMenuItem(
  {
    onAction,
    resetKey,
    children,
    pendingLabel,
    successLabel,
    errorLabel,
    wrapperClassName,
    className,
    disabled,
    ...props
  },
  ref
) {
  const t = useTranslations("actionFeedback");
  const feedback = useActionFeedback();
  const detailId = React.useId();
  React.useEffect(() => {
    feedback.reset();
  }, [resetKey, feedback.reset]);
  const busy = feedback.status === "pending";
  const succeeded = feedback.status === "success";
  const failed = feedback.status === "error";
  const active = feedback.status !== "idle";
  const label = busy
    ? (pendingLabel ?? t("pending"))
    : succeeded
      ? (successLabel ?? t("success"))
      : (errorLabel ?? t("error"));
  const statusContent = active ? (
    <>
      {busy ? (
        <Loader2 className="animate-spin" aria-hidden="true" />
      ) : succeeded ? (
        <Check aria-hidden="true" />
      ) : (
        <X aria-hidden="true" />
      )}
      <span className="ml-auto text-xs">{label}</span>
    </>
  ) : null;
  const content =
    props.asChild && React.isValidElement<{ children?: React.ReactNode }>(children) ? (
      React.cloneElement(children, {}, children.props.children, statusContent)
    ) : (
      <>
        {children}
        {statusContent}
      </>
    );

  return (
    <div className={cn("max-w-full min-w-0", wrapperClassName)}>
      <DropdownMenuItem
        {...props}
        ref={ref}
        disabled={disabled || busy}
        aria-busy={busy}
        aria-label={
          active && props["aria-label"] ? `${props["aria-label"]}: ${label}` : props["aria-label"]
        }
        aria-describedby={
          [props["aria-describedby"], failed ? detailId : undefined].filter(Boolean).join(" ") ||
          undefined
        }
        data-action-status={feedback.status}
        className={cn(
          className,
          succeeded &&
            "text-emerald-700 focus:text-emerald-700 dark:text-emerald-400 dark:focus:text-emerald-400",
          failed && "text-red-600 focus:text-red-600 dark:text-red-400 dark:focus:text-red-400"
        )}
        onSelect={(event) => {
          event.preventDefault();
          if (!disabled) void feedback.run(onAction);
        }}
      >
        {content}
      </DropdownMenuItem>
      {failed && (
        <div
          id={detailId}
          role="alert"
          aria-live="assertive"
          className="max-w-80 whitespace-normal break-words px-2 pb-2 text-xs text-red-600 dark:text-red-400"
        >
          {feedback.error ?? label} · {t("retry")}
        </div>
      )}
    </div>
  );
});
