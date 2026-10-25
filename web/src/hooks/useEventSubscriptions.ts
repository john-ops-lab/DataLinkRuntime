import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api";
import type { EventSubscription, SubscriptionCapabilities } from "../subscription-types";
import { subscriptionErrorMessage } from "../subscription-message";

/** Load every authorized page before publishing a coherent catalog snapshot. */
export async function loadEventSubscriptionCatalog(): Promise<Map<number, EventSubscription>> {
  const result = new Map<number, EventSubscription>();
  const seen = new Set<number>();
  let before: number | undefined;
  do {
    const page = await api.listSubscriptions({ limit: 100, before_id: before });
    for (const sub of page.items) result.set(sub.adapter_id, sub);
    if (page.next_before_id === null) return result;
    if (seen.has(page.next_before_id)) throw new Error("Invalid subscription pagination cursor");
    seen.add(page.next_before_id);
    before = page.next_before_id;
  } while (before !== undefined);
  return result;
}

export function useEventSubscriptions() {
  const [items, setItems] = useState(new Map<number, EventSubscription>());
  const [capabilities, setCapabilities] = useState<SubscriptionCapabilities>({ enabled: false, available_sources: [] });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const generation = useRef(0);
  const reload = useCallback(async () => {
    const request = ++generation.current;
    try {
      const [next, caps] = await Promise.all([loadEventSubscriptionCatalog(), api.subscriptionCapabilities()]);
      if (request !== generation.current) return;
      setItems(next);
      setCapabilities(caps);
      setError(null);
    } catch (reason) {
      if (request === generation.current) setError(subscriptionErrorMessage(reason));
    } finally {
      if (request === generation.current) setLoading(false);
    }
  }, []);
  useEffect(() => {
    const initial = window.setTimeout(() => void reload(), 0);
    const timer = window.setInterval(() => void reload(), 5000);
    return () => { generation.current += 1; window.clearTimeout(initial); window.clearInterval(timer); };
  }, [reload]);
  return { items, capabilities, loading, error, reload };
}
