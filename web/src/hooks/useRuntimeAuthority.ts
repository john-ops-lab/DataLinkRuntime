import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError, api } from "../api";
import { RUNTIME_REFRESH_POLICY } from "../runtime-refresh-policy";
import type { Adapter, AdapterSchedule, AdapterWebhook } from "../types";

export type RuntimeTriggerSnapshot =
  | { adapterId: number; adapterType: "task"; loaded: boolean; value: AdapterSchedule | null | undefined }
  | { adapterId: number; adapterType: "webhook"; loaded: boolean; value: AdapterWebhook | undefined };

export interface RuntimeMutation {
  acceptAdapter: (adapter: Adapter) => boolean;
  acceptSchedule: (schedule: AdapterSchedule) => boolean;
  acceptWebhook: (webhook: AdapterWebhook) => boolean;
  finish: (onRefreshError?: (error: unknown) => void) => void;
}

export type RefreshReason = "initial" | "timer" | "focus" | "visible" | "explicit" | "conflict" | "mutation" | "terminal";

interface Session {
  adapterId: number;
  adapterType: "task" | "webhook";
  selectionEpoch: number;
  readEpoch: number;
  mutationCount: number;
  inFlight: Promise<void> | null;
  pending: boolean;
  mutationRefreshErrorHandlers: Array<(error: unknown) => void>;
}

export function useRuntimeAuthority(options: {
  adapterId: number | null;
  adapterType: "task" | "webhook" | null;
  onAdapterAccepted: (adapter: Adapter) => void;
  onExplicitError: (message: string) => void;
  errorMessage: (error: unknown) => string;
}) {
  const [triggerSnapshot, setTriggerSnapshot] = useState<RuntimeTriggerSnapshot | null>(null);
  const [synchronizingAfterMutation, setSynchronizingAfterMutation] = useState(false);
  const selectionEpochRef = useRef(0);
  const sessionRef = useRef<Session | null>(null);
  const startRefreshRef = useRef<(reason: RefreshReason) => Promise<void>>(
    async () => undefined,
  );
  const onAdapterAcceptedRef = useRef(options.onAdapterAccepted);
  const onExplicitErrorRef = useRef(options.onExplicitError);
  const errorMessageRef = useRef(options.errorMessage);

  useEffect(() => {
    onAdapterAcceptedRef.current = options.onAdapterAccepted;
    onExplicitErrorRef.current = options.onExplicitError;
    errorMessageRef.current = options.errorMessage;
  });

  const requestRuntimeRefresh = useCallback(
    (reason: RefreshReason = "explicit") => startRefreshRef.current(reason),
    [],
  );

  const beginRuntimeMutation = useCallback((): RuntimeMutation | null => {
    const session = sessionRef.current;
    if (session === null) {
      return null;
    }
    const { adapterId, adapterType, selectionEpoch } = session;
    session.readEpoch += 1;
    session.mutationCount += 1;
    session.pending = true;
    setSynchronizingAfterMutation(true);
    let finished = false;
    const isCurrent = () => {
      const current = sessionRef.current;
      return current !== null
        && current.adapterId === adapterId
        && current.adapterType === adapterType
        && current.selectionEpoch === selectionEpoch;
    };
    return {
      acceptAdapter(adapter) {
        if (!isCurrent() || adapter.id !== adapterId) return false;
        session.readEpoch += 1;
        onAdapterAcceptedRef.current(adapter);
        return true;
      },
      acceptSchedule(schedule) {
        if (!isCurrent() || adapterType !== "task" || schedule.adapter_id !== adapterId) return false;
        session.readEpoch += 1;
        setTriggerSnapshot({ adapterId, adapterType: "task", loaded: true, value: schedule });
        return true;
      },
      acceptWebhook(webhook) {
        if (!isCurrent() || adapterType !== "webhook" || webhook.adapter_id !== adapterId) return false;
        session.readEpoch += 1;
        setTriggerSnapshot({ adapterId, adapterType: "webhook", loaded: true, value: webhook });
        return true;
      },
      finish(onRefreshError) {
        if (finished) return;
        finished = true;
        const current = sessionRef.current;
        if (!isCurrent() || current === null) return;
        current.readEpoch += 1;
        current.mutationCount = Math.max(0, current.mutationCount - 1);
        current.pending = true;
        if (onRefreshError !== undefined) current.mutationRefreshErrorHandlers.push(onRefreshError);
        if (current.mutationCount === 0 && current.inFlight === null) {
          void startRefreshRef.current("mutation");
        }
      },
    };
  }, []);

  useEffect(() => {
    const adapterId = options.adapterId;
    const adapterType = options.adapterType;
    if (adapterId === null || adapterType === null) {
      sessionRef.current = null;
      // eslint-disable-next-line react-hooks/set-state-in-effect -- selection teardown resets the authority snapshot synchronously
      setTriggerSnapshot(null);
      setSynchronizingAfterMutation(false);
      return;
    }
    const selectionEpoch = ++selectionEpochRef.current;
    const session: Session = {
      adapterId,
      adapterType,
      selectionEpoch,
      readEpoch: 0,
      mutationCount: 0,
      inFlight: null,
      pending: false,
      mutationRefreshErrorHandlers: [],
    };
    sessionRef.current = session;
    setSynchronizingAfterMutation(false);
    setTriggerSnapshot(adapterType === "task"
      ? { adapterId, adapterType, loaded: false, value: undefined }
      : { adapterId, adapterType, loaded: false, value: undefined });
    let disposed = false;
    let timeoutId: ReturnType<typeof setTimeout> | null = null;
    const isCurrent = (readEpoch?: number) => {
      const current = sessionRef.current;
      return !disposed
        && current === session
        && current.selectionEpoch === selectionEpoch
        && (readEpoch === undefined || current.readEpoch === readEpoch);
    };
    const scheduleNext = () => {
      if (!isCurrent() || document.visibilityState === "hidden" || timeoutId !== null) return;
      timeoutId = setTimeout(() => {
        timeoutId = null;
        void startRefresh("timer");
      }, RUNTIME_REFRESH_POLICY.pollIntervalMs);
    };
    const startRefresh = (reason: RefreshReason): Promise<void> => {
      if (!isCurrent()) return Promise.resolve();
      if (reason === "timer" && document.visibilityState === "hidden") return Promise.resolve();
      if (session.mutationCount > 0) {
        session.pending = true;
        return Promise.resolve();
      }
      if (session.inFlight !== null) {
        session.pending = true;
        return session.inFlight;
      }
      if (timeoutId !== null) {
        clearTimeout(timeoutId);
        timeoutId = null;
      }
      const readEpoch = ++session.readEpoch;
      session.pending = false;
      let request: Promise<void> = Promise.resolve();
      request = (async () => {
        let failureReported = false;
        const reportReadFailure = (error: unknown) => {
          if (
            !failureReported
            && isCurrent(readEpoch)
            && (reason === "explicit" || reason === "initial")
          ) {
            failureReported = true;
            onExplicitErrorRef.current(errorMessageRef.current(error));
            setTriggerSnapshot((current) => current?.adapterId === adapterId
              ? { ...current, loaded: true }
              : current);
          }
        };
        try {
          const triggerPromise = adapterType === "task"
            ? api.getSchedule(adapterId).catch((error: unknown) => {
                if (error instanceof ApiError && error.status === 404 && error.code === "schedule_not_configured") {
                  return null;
                }
                throw error;
              })
            : api.getWebhook(adapterId);
          const adapterPromise = api.getAdapter(adapterId).catch((error: unknown) => {
            reportReadFailure(error);
            throw error;
          });
          const observedTriggerPromise = triggerPromise.catch((error: unknown) => {
            reportReadFailure(error);
            throw error;
          });
          const [adapterResult, triggerResult] = await Promise.allSettled([
            adapterPromise,
            observedTriggerPromise,
          ]);
          if (!isCurrent(readEpoch)) return;
          if (adapterResult.status === "rejected") throw adapterResult.reason;
          if (triggerResult.status === "rejected") throw triggerResult.reason;
          const adapter = adapterResult.value;
          const trigger = triggerResult.value;
          onAdapterAcceptedRef.current(adapter);
          setTriggerSnapshot(adapterType === "task"
            ? { adapterId, adapterType, loaded: true, value: trigger as AdapterSchedule | null }
            : { adapterId, adapterType, loaded: true, value: trigger as AdapterWebhook });
          setSynchronizingAfterMutation(false);
          session.mutationRefreshErrorHandlers = [];
        } catch (error) {
          if (isCurrent(readEpoch) && reason === "mutation") {
            const handlers = session.mutationRefreshErrorHandlers;
            session.mutationRefreshErrorHandlers = [];
            for (const handler of handlers) handler(error);
          }
          reportReadFailure(error);
        } finally {
          if (isCurrent()) {
            if (session.inFlight === request) session.inFlight = null;
            if (session.mutationCount === 0 && session.pending) {
              session.pending = false;
              void startRefresh("mutation");
            } else {
              scheduleNext();
            }
          }
        }
      })();
      session.inFlight = request;
      return request;
    };
    startRefreshRef.current = startRefresh;
    const handleFocus = () => void startRefresh("focus");
    const handleVisibility = () => {
      if (document.visibilityState === "hidden") {
        if (timeoutId !== null) clearTimeout(timeoutId);
        timeoutId = null;
      } else {
        void startRefresh("visible");
      }
    };
    window.addEventListener("focus", handleFocus);
    document.addEventListener("visibilitychange", handleVisibility);
    void startRefresh("initial");
    return () => {
      disposed = true;
      session.readEpoch += 1;
      if (timeoutId !== null) clearTimeout(timeoutId);
      window.removeEventListener("focus", handleFocus);
      document.removeEventListener("visibilitychange", handleVisibility);
      if (sessionRef.current === session) sessionRef.current = null;
    };
  }, [options.adapterId, options.adapterType]);

  return {
    triggerSnapshot,
    synchronizingAfterMutation,
    requestRuntimeRefresh,
    beginRuntimeMutation,
  };
}
