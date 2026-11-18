/** Explicit administrator intent; polling and remounts only perform GETs. */
import { Alert, Button, Modal } from "antd";
import { useCallback, useEffect, useId, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { ApiError, api } from "../api";
import { useOverlayFocus } from "../hooks/useOverlayFocus";
import { REQUIRED_ISOLATION_CAPABILITIES, restrictedWorkerCapabilities } from "../system-status";
import type { Worker, WorkerMaintenanceAction, WorkerMaintenanceReceipt, WorkerMaintenanceSnapshot } from "../types";
import {
  parseWorkerMaintenanceReceipt, WORKER_MAINTENANCE_TIMEOUT_MS,
} from "../worker-maintenance";
import {
  clearWorkerMaintenanceIntent, persistWorkerMaintenanceIntent, readWorkerMaintenanceIntent,
  type WorkerMaintenanceIntent,
} from "../worker-maintenance-intent";

type Notice = "unknown" | "draining" | "committed" | "replaced" | "conflict" | "permission" | "changed" | "storage" | "unavailable" | "mismatch";
type Confirmation = { intent: WorkerMaintenanceIntent; observed: WorkerMaintenanceSnapshot; retry: boolean };

function canActivate(snapshot: WorkerMaintenanceSnapshot): boolean {
  return snapshot.scheduling_state === "disabled" && snapshot.enrolled && snapshot.online
    && snapshot.capacity_state === "ready" && snapshot.isolation_preflight_status === "passed"
    && snapshot.unsettled_attempts === 0 && !snapshot.has_unreconciled_attempts
    && restrictedWorkerCapabilities(snapshot.capabilities)
    && REQUIRED_ISOLATION_CAPABILITIES.every((key) => snapshot.isolation_capabilities[key] === true);
}

function canConfirmDrain(snapshot: WorkerMaintenanceSnapshot, intent: WorkerMaintenanceIntent): boolean {
  const current = snapshot.current_drain_operation;
  return snapshot.scheduling_state === "draining" && snapshot.unsettled_attempts === 0
    && current !== null && current.same_actor && current.request_id === intent.request_id
    && current.expected_version === intent.expected_version && current.result_version === snapshot.maintenance_version;
}

function canConfirmRecoveredDrain(snapshot: WorkerMaintenanceSnapshot): boolean {
  return snapshot.scheduling_state === "draining" && snapshot.current_maintenance_action === "reconcile"
    && snapshot.capacity_state === "ready" && snapshot.enrolled
    && snapshot.unsettled_attempts === 0 && !snapshot.has_unreconciled_attempts;
}

function canStartAction(snapshot: WorkerMaintenanceSnapshot, action: WorkerMaintenanceAction): boolean {
  switch (action) {
    case "drain": return snapshot.scheduling_state === "active";
    case "activate": return canActivate(snapshot);
    case "force_stop":
      return snapshot.enrolled && snapshot.scheduling_state === "draining"
        && snapshot.current_maintenance_action === "drain" && snapshot.force_stop_available === true;
  }
}

function sameReceipt(receipt: WorkerMaintenanceReceipt, intent: WorkerMaintenanceIntent): boolean {
  return receipt.operation.action === intent.action && receipt.operation.same_actor
    && receipt.operation.expected_version === intent.expected_version
    && receipt.current.worker_identity_id === intent.worker_identity_id;
}

export default function WorkerMaintenanceActions({ worker, onRefresh, onRead }: {
  worker: Worker;
  onRefresh: () => Promise<void>;
  onRead: (workerId: number) => Promise<WorkerMaintenanceSnapshot>;
}) {
  const { t } = useTranslation("common");
  const view = worker.maintenance;
  const snapshot = view?.snapshot;
  const scope = view?.scope ?? null;
  const identity = snapshot?.worker_identity_id ?? null;
  const [pending, setPending] = useState<WorkerMaintenanceIntent | null>(null);
  const [receipt, setReceipt] = useState<WorkerMaintenanceReceipt | null>(null);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [storageBlocked, setStorageBlocked] = useState(false);
  const [busy, setBusy] = useState(false);
  const [confirmation, setConfirmation] = useState<Confirmation | null>(null);
  const busyRef = useRef(false);
  const generation = useRef(0);
  const controller = useRef<AbortController | null>(null);
  const refreshRef = useRef(onRefresh);
  const descriptionId = useId();
  const restoreFocus = useOverlayFocus(confirmation !== null, '[data-testid="system-status-refresh"], [data-testid="worker-maintenance-recheck"]');

  useEffect(() => { refreshRef.current = onRefresh; }, [onRefresh]);
  // A separate health refresh failure does not undo an observed committed
  // operation. The shared catalog keeps its own failed/expired read state.
  const refresh = useCallback(async () => {
    try { await refreshRef.current(); }
    catch { /* Keep the separately observed commit; catalog reports read errors. */ }
  }, []);

  const call = useCallback(async <T,>(
    send: (signal: AbortSignal) => Promise<T>,
  ): Promise<T> => {
    const active = new AbortController();
    controller.current = active;
    const timeout = setTimeout(() => active.abort(), WORKER_MAINTENANCE_TIMEOUT_MS);
    try { return await send(active.signal); }
    finally {
      clearTimeout(timeout);
      if (controller.current === active) controller.current = null;
    }
  }, []);

  const lookup = useCallback(async (intent: WorkerMaintenanceIntent, current: number, firstDispatchRejected = false) => {
    let observed: WorkerMaintenanceReceipt | null = null;
    try {
      const raw = await call((signal) => api.getWorkerMaintenanceOperation(intent.worker_id, intent.request_id, signal));
      observed = parseWorkerMaintenanceReceipt(raw, intent.worker_id, intent.request_id);
    } catch (error) {
      if (generation.current !== current) return;
      if (error instanceof ApiError && error.status === 403 && error.code === "account_admin_required") {
        clearWorkerMaintenanceIntent(intent);
        setPending(null);
        setNotice("permission");
        return;
      }
      if (firstDispatchRejected && error instanceof ApiError && error.status === 404) {
        clearWorkerMaintenanceIntent(intent);
        setPending(null);
        setNotice("conflict");
        await refresh();
        return;
      }
    }
    if (generation.current !== current) return;
    if (!observed) { setNotice("unknown"); return; }
    if (!sameReceipt(observed, intent)) { setNotice("mismatch"); return; }
    setReceipt(observed);
    if (!observed.applies_to_current_version) {
      clearWorkerMaintenanceIntent(intent);
      setPending(null);
      setNotice("replaced");
    } else if (intent.action === "drain" && observed.current.scheduling_state === "draining") {
      // The immutable row proves the initial drain, not a later confirmation.
      // Keep the tuple until disabled is observed in a committed snapshot.
      setPending(intent);
      setNotice("draining");
    } else if (observed.current.scheduling_state === (intent.action === "activate" ? "active" : "disabled")) {
      clearWorkerMaintenanceIntent(intent);
      setPending(null);
      setNotice("committed");
    } else {
      setNotice("unknown");
    }
    await refresh();
  }, [call, refresh]);

  useEffect(() => {
    const current = ++generation.current;
    busyRef.current = false;
    // Storage/session bootstrap is asynchronous; keyed mounts keep a previous
    // Principal's state out of the new view while this external read starts.
    void Promise.resolve().then(() => {
    if (generation.current !== current) return;
    setBusy(false);
    setPending(null);
    setReceipt(null);
    setConfirmation(null);
    setNotice(null);
    setStorageBlocked(false);
    if (scope !== null) {
      try {
        const intent = readWorkerMaintenanceIntent(scope, worker.id);
        if (intent !== null) {
          setPending(intent);
          if (intent.worker_id !== worker.id || (identity !== null && intent.worker_identity_id !== identity)) {
            setNotice("mismatch");
          } else {
            busyRef.current = true;
            setBusy(true);
            // Refresh/remount never sends a management POST.
            void lookup(intent, current).finally(() => {
              if (generation.current === current) { busyRef.current = false; setBusy(false); }
            });
          }
        }
      } catch {
        setStorageBlocked(true);
        setNotice("storage");
      }
    }
    });
    return () => { generation.current += 1; controller.current?.abort(); };
  }, [identity, lookup, scope, worker.id]);

  const readFresh = async () => {
    return onRead(worker.id);
  };

  const confirmedDrain = pending?.action === "drain" && receipt?.applies_to_current_version
    && receipt.current.scheduling_state === "draining" && sameReceipt(receipt, pending)
    ? pending : undefined;

  const prepare = async (action: WorkerMaintenanceAction, reuse?: WorkerMaintenanceIntent, retry = false) => {
    if (busyRef.current || scope === null || storageBlocked) return;
    busyRef.current = true;
    setBusy(true);
    const current = generation.current;
    try {
      const fresh = await readFresh();
      if (generation.current !== current) return;
      if ((identity !== null && fresh.worker_identity_id !== identity)
        || (reuse && fresh.worker_identity_id !== reuse.worker_identity_id)
        || (!retry && (reuse ? !canConfirmDrain(fresh, reuse)
          : !canStartAction(fresh, action) && !(action === "drain" && canConfirmRecoveredDrain(fresh))))) {
        setNotice("changed");
        await refresh();
        return;
      }
      setConfirmation({
        observed: fresh, retry,
        intent: reuse ?? {
          schema: 1, scope, origin: window.location.origin,
          worker_id: worker.id, worker_identity_id: fresh.worker_identity_id,
          action, expected_version: fresh.maintenance_version, request_id: crypto.randomUUID(),
        },
      });
    } catch { if (generation.current === current) setNotice("unavailable"); }
    finally { if (generation.current === current) { busyRef.current = false; setBusy(false); } }
  };

  const submit = async () => {
    if (busyRef.current || confirmation === null) return;
    const { intent, observed, retry } = confirmation;
    busyRef.current = true;
    setBusy(true);
    const current = generation.current;
    try {
      const fresh = await readFresh();
      if (generation.current !== current) return;
      const continuingDrain = intent.action === "drain" && observed.scheduling_state === "draining"
        && observed.current_maintenance_action === "drain";
      const recoveredConfirmation = intent.action === "drain" && observed.scheduling_state === "draining"
        && observed.current_maintenance_action === "reconcile";
      if (fresh.worker_identity_id !== intent.worker_identity_id
        || (!retry && (fresh.maintenance_version !== observed.maintenance_version
          || fresh.instance_id !== observed.instance_id || fresh.instance_generation !== observed.instance_generation
          || (continuingDrain ? !canConfirmDrain(fresh, intent)
            : recoveredConfirmation ? !canConfirmRecoveredDrain(fresh)
            : !canStartAction(fresh, intent.action))))) {
        setConfirmation(null);
        setNotice("changed");
        await refresh();
        return;
      }
      try { persistWorkerMaintenanceIntent(intent, !retry && intent.action === "force_stop" ? confirmedDrain : undefined); }
      catch {
        setStorageBlocked(true);
        setNotice("storage");
        setConfirmation(null);
        return; // Nothing was sent.
      }
      setPending(intent);
      setReceipt(null);
      // A previous operation's success must not describe this new in-flight
      // intent, including while its response is delayed or lost.
      setNotice("unknown");
      let rejected = false;
      try {
        // The POST body stays frozen even after a timeout or page re-entry.
        await call((signal) => api.maintainWorker(worker.id, intent.action, {
          request_id: intent.request_id, expected_version: intent.expected_version,
        }, signal));
      } catch (error) {
        rejected = error instanceof ApiError && error.status === 409;
      }
      if (generation.current !== current) return;
      setConfirmation(null);
      setNotice("unknown");
      // A retry's 409 only rejects this dispatch; it cannot fence an earlier
      // unknown request that may still commit after this 404 snapshot.
      await lookup(intent, current, rejected && !retry && !continuingDrain);
    } catch { if (generation.current === current) setNotice("unavailable"); }
    finally { if (generation.current === current) { busyRef.current = false; setBusy(false); } }
  };

  const recheck = async () => {
    if (!pending || busyRef.current) return;
    const current = generation.current;
    busyRef.current = true;
    setBusy(true);
    try { await lookup(pending, current); }
    finally { if (generation.current === current) { busyRef.current = false; setBusy(false); } }
  };

  const fresh = snapshot !== null && snapshot !== undefined && view?.fresh && view.error === null;
  const blocked = busy || storageBlocked || !fresh;
  const currentDrain = snapshot?.current_drain_operation;
  const resume: WorkerMaintenanceIntent | null = snapshot && scope !== null && currentDrain?.same_actor ? {
    schema: 1, origin: window.location.origin, scope, worker_id: worker.id,
    worker_identity_id: snapshot.worker_identity_id, action: "drain",
    request_id: currentDrain.request_id, expected_version: currentDrain.expected_version,
  } : null;
  const continuation = pending?.action === "drain" ? pending : resume;
  const otherPending = pending !== null && (pending.action !== "drain" || pending.request_id !== currentDrain?.request_id);
  const forceStopConfirmation = confirmation?.intent.action === "force_stop";
  const confirmTitle = forceStopConfirmation ? "forceStopTitle"
    : confirmation?.intent.action === "activate" ? "activateTitle" : "drainTitle";
  return (
    <div className="worker-maintenance-actions" data-testid="worker-maintenance-actions" aria-busy={busy}>
      <div className="worker-maintenance-buttons" aria-describedby={descriptionId}>
        {snapshot?.scheduling_state === "active" && (
          <Button disabled={blocked || pending !== null} onClick={() => void prepare("drain")}>{t("worker.maintenance.drain")}</Button>
        )}
        {snapshot?.scheduling_state === "disabled" && (
          <Button type="primary" disabled={blocked || pending !== null || !canActivate(snapshot)} onClick={() => void prepare("activate")}>{t("worker.maintenance.activate")}</Button>
        )}
        {snapshot?.scheduling_state === "draining" && snapshot.current_maintenance_action !== "reconcile" && (
          <Button disabled={blocked || otherPending || continuation === null || !canConfirmDrain(snapshot, continuation)}
            onClick={() => continuation && void prepare("drain", continuation)}>{t("worker.maintenance.confirmDrain")}</Button>
        )}
        {snapshot && canStartAction(snapshot, "force_stop") && (
          <Button danger disabled={blocked || (pending !== null && !confirmedDrain)}
            onClick={() => void prepare("force_stop")}>{t("worker.maintenance.forceStop")}</Button>
        )}
        {snapshot?.scheduling_state === "draining" && snapshot.current_maintenance_action === "reconcile" && (
          <Button disabled={blocked || pending !== null || !canConfirmRecoveredDrain(snapshot)}
            onClick={() => void prepare("drain")}>{t("worker.maintenance.confirmRecoveredDrain")}</Button>
        )}
      </div>
      <p id={descriptionId} className="worker-maintenance-hint">{t("worker.maintenance.actionBoundary")}</p>
      {snapshot?.scheduling_state === "disabled" && !canActivate(snapshot) && (
        <p className="worker-maintenance-hint">{t("worker.maintenance.activationRequirements")}</p>
      )}
      {snapshot?.current_maintenance_action === "force_stop" && snapshot.unsettled_attempts > 0 && (
        <p className="worker-maintenance-hint">{t("worker.maintenance.forceStopPending")}</p>
      )}
      {snapshot?.scheduling_state === "draining" && snapshot.current_maintenance_action !== "reconcile"
        && (currentDrain?.same_actor !== true || snapshot.unsettled_attempts !== 0) && (
        <p className="worker-maintenance-hint">{t(currentDrain?.same_actor === false ? "worker.maintenance.otherActor" : "worker.maintenance.drainRequirements")}</p>
      )}
      {snapshot?.scheduling_state === "draining" && snapshot.current_maintenance_action === "reconcile" && !canConfirmRecoveredDrain(snapshot) && (
        <p className="worker-maintenance-hint">{t("worker.maintenance.recoveredDrainRequirements")}</p>
      )}
      {notice && <Alert showIcon role={notice === "committed" || notice === "draining" ? "status" : "alert"}
        type={notice === "committed" ? "success" : "warning"}
        message={t(`worker.maintenance.notice.${notice}`)}
        description={receipt ? t("worker.maintenance.receiptCurrent", {
          version: receipt.current.maintenance_version,
          state: t(`worker.maintenance.state.${receipt.current.scheduling_state}`),
        }) : undefined} />}
      {pending && (
        <div className="worker-maintenance-pending">
          <p>{t("worker.maintenance.pendingRequest")} <code>{pending.request_id}</code> · {pending.expected_version}</p>
          <Button data-testid="worker-maintenance-recheck" disabled={busy || notice === "mismatch"} onClick={() => void recheck()}>{t("worker.maintenance.lookup")}</Button>
          <Button disabled={blocked || notice === "mismatch"} onClick={() => void prepare(pending.action, pending, true)}>{t("worker.maintenance.retrySame")}</Button>
        </div>
      )}
      <Modal open={confirmation !== null} title={t(`worker.maintenance.${confirmation?.retry ? "retryTitle" : confirmTitle}`)}
        onOk={() => void submit()} onCancel={() => { if (!busyRef.current) setConfirmation(null); }}
        afterClose={() => restoreFocus(false)} confirmLoading={busy}
        maskClosable={false} keyboard={!busy} cancelButtonProps={{ disabled: busy, autoFocus: true }}
        okText={t("worker.maintenance.confirmAction")} cancelText={t("worker.maintenance.cancelAction")}
        okButtonProps={{ "aria-describedby": descriptionId, danger: forceStopConfirmation }}>
        <p>{t("worker.maintenance.confirmTarget", { name: worker.name, version: confirmation?.intent.expected_version })}</p>
        <p>{t(confirmation?.retry ? "worker.maintenance.retryDescription"
          : forceStopConfirmation ? "worker.maintenance.forceStopDescription" : "worker.maintenance.confirmDescription")}</p>
        {confirmation && <p>{t("worker.maintenance.pendingRequest")} <code>{confirmation.intent.request_id}</code></p>}
      </Modal>
    </div>
  );
}
