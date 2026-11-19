import { useState } from "react";
import type { Adapter } from "./types";
import type { FixedRoute, RuntimeRoute, RuntimeRoutePatch } from "./worker-pool-types";

export function fixedRoute(workerId: number): FixedRoute {
  return { schema_version: 1, mode: "fixed", worker_id: workerId, failover: "none" };
}

function adapterRoute(adapter: Adapter): RuntimeRoute | null {
  return adapter.route ?? (adapter.runtime_worker_id == null ? null : fixedRoute(adapter.runtime_worker_id));
}

function routeKey(route: RuntimeRoute | null): string {
  if (route === null) return "";
  return route.mode === "fixed" ? `fixed:${route.worker_id}` : `pool:${route.pool_id}:${route.pool_revision}`;
}

export function useRuntimeRouteDraft(adapter: Adapter) {
  const [submitted, setSubmitted] = useState<{ prior: Adapter; saved: Adapter }>();
  const [edit, setEdit] = useState<{ adapterId: number; route: RuntimeRoute | null; version: number | undefined }>();
  const saved = submitted?.prior === adapter ? submitted.saved : adapter;
  const baseline = adapterRoute(saved);
  const draft = edit?.adapterId === adapter.id ? edit : undefined;
  const value = draft === undefined ? baseline : draft.route;

  function select(route: RuntimeRoute | null) {
    if (routeKey(route) === routeKey(baseline)) {
      setEdit(undefined);
      return;
    }
    setEdit({ adapterId: adapter.id, route, version: draft?.version ?? saved.route_version });
  }

  function patch(): RuntimeRoutePatch {
    const version = draft?.version ?? saved.route_version;
    const expectation = version === undefined ? {} : { expected_route_version: version };
    if (value?.mode === "fixed") return { runtime_worker_id: value.worker_id, ...expectation };
    return draft === undefined ? {} : { route: value, ...expectation };
  }

  function accept(next: Adapter, resetDraft: boolean) {
    setSubmitted({ prior: adapter, saved: next });
    if (resetDraft) setEdit(undefined);
  }

  return { value, select, patch, accept, dirty: draft !== undefined };
}
