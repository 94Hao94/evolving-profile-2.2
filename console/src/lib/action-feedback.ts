"use client";

import { useRef, useSyncExternalStore } from "react";

export type ActionStatus = "idle" | "pending" | "success" | "error";
export type AsyncAction = () => Promise<unknown>;
export interface ActionFeedbackState {
  status: ActionStatus;
  error: string | null;
}

const idleState: ActionFeedbackState = { status: "idle", error: null };

/** A synchronous lock protects against repeated clicks before React renders. */
export function createActionFeedback() {
  let state = idleState;
  let locked = false;
  let invalidated = false;
  const listeners = new Set<() => void>();
  const update = (next: ActionFeedbackState) => {
    state = next;
    listeners.forEach((listener) => listener());
  };

  return {
    getSnapshot: () => state,
    subscribe(listener: () => void) {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
    async run(action: AsyncAction): Promise<boolean> {
      if (locked) return false;
      locked = true;
      invalidated = false;
      update({ status: "pending", error: null });
      try {
        const result = await action();
        if (result === false) {
          update(idleState);
          return false;
        }
        update(invalidated ? idleState : { status: "success", error: null });
        return true;
      } catch (error) {
        const message =
          error instanceof Error ? error.message : typeof error === "string" ? error : null;
        update({ status: "error", error: message?.trim() || null });
        return false;
      } finally {
        locked = false;
      }
    },
    reset() {
      if (locked) invalidated = true;
      else update(idleState);
    },
  };
}

/** Actions must reject failures; returning false denotes cancellation. */
export function useActionFeedback() {
  const controllerRef = useRef<ReturnType<typeof createActionFeedback> | null>(null);
  if (!controllerRef.current) controllerRef.current = createActionFeedback();
  const controller = controllerRef.current;
  const state = useSyncExternalStore(controller.subscribe, controller.getSnapshot, () => idleState);
  return { ...state, run: controller.run, reset: controller.reset };
}
