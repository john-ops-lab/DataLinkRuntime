import { useCallback, useEffect, useRef, useState } from "react";
import { Alert, Button } from "antd";
import { useTranslation } from "react-i18next";
import { api } from "../api";
import type { Adapter } from "../types";
import type {
  EventSubscription,
  SubscriptionCapabilities,
  SubscriptionDraftState,
} from "../subscription-types";
import { subscriptionErrorMessage } from "../subscription-message";
import { implementedSourceTypes } from "../subscription-types";
import SubscriptionConfigDialog from "./SubscriptionConfigDialog";
import SubscriptionWorkspace from "./SubscriptionWorkspace";

export default function KafkaSubscriptionPanel({
  adapter,
  readOnly,
  canManage,
  dirty,
  onChanged,
  onBindingChange,
  onDraftStateChange,
}: {
  adapter: Adapter;
  readOnly: boolean;
  canManage: boolean;
  dirty: boolean;
  onChanged?: () => Promise<void>;
  onBindingChange: (bound: boolean) => void;
  onDraftStateChange?: (state: SubscriptionDraftState) => void;
}) {
  const { t } = useTranslation("subscription");
  const [sub, setSub] = useState<EventSubscription | null>(null);
  const [capabilities, setCapabilities] = useState<SubscriptionCapabilities>({
    enabled: false,
    available_sources: [],
  });
  const [loading, setLoading] = useState(true);
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const generation = useRef(0);
  const load = useCallback(async () => {
    const requestGeneration = ++generation.current;
    try {
      const [page, caps] = await Promise.all([
        api.listSubscriptions({ adapter_id: adapter.id, limit: 1 }),
        api.subscriptionCapabilities(),
      ]);
      if (requestGeneration !== generation.current) return;
      setSub(page.items[0] ?? null);
      setCapabilities(caps);
      onBindingChange(page.items.length > 0);
      setError(null);
    } catch (reason) {
      if (requestGeneration === generation.current)
        setError(subscriptionErrorMessage(reason));
    } finally {
      if (requestGeneration === generation.current) setLoading(false);
    }
  }, [adapter.id, onBindingChange]);
  useEffect(() => {
    const timer = window.setTimeout(() => void load(), 0);
    const polling = window.setInterval(() => void load(), 5000);
    return () => {
      generation.current += 1;
      window.clearTimeout(timer);
      window.clearInterval(polling);
    };
  }, [load]);
  const availableSources = implementedSourceTypes.filter((source) =>
    capabilities.available_sources.includes(source),
  );
  const canCreate =
    !readOnly &&
    !dirty &&
    !adapter.runtime_locked &&
    capabilities.enabled &&
    availableSources.length > 0 &&
    adapter.run_mode === "manual";
  return (
    <section
      className="subscription-binding"
      data-testid="subscription-binding"
    >
      <h3>{t("title")}</h3>
      {error && (
        <Alert
          type="error"
          showIcon
          message={error}
          action={<Button onClick={() => void load()}>{t("refresh")}</Button>}
        />
      )}
      {sub ? (
        <SubscriptionWorkspace
          key={sub.id}
          id={sub.id}
          adapter={adapter}
          capabilities={capabilities}
          activationBlocked={dirty}
          onDraftStateChange={onDraftStateChange}
          onChanged={onChanged}
          onDeleted={() => {
            setSub(null);
            onBindingChange(false);
            void load();
          }}
        />
      ) : (
        <>
          {!loading && <p>{t("empty")}</p>}
          {!loading && !capabilities.enabled && (
            <Alert type="info" showIcon message={t("featureOff")} />
          )}
          {!loading && availableSources.length === 0 && (
            <Alert type="info" showIcon message={t("unavailable")} />
          )}
          {!readOnly && availableSources.length > 0 && (
            <Button disabled={!canCreate} onClick={() => setCreating(true)}>
              {t("create")}
            </Button>
          )}
        </>
      )}
      {creating && (
        <SubscriptionConfigDialog
          availableSources={availableSources}
          adapterId={adapter.id}
          canManage={canManage}
          onDraftStateChange={onDraftStateChange}
          onClose={() => setCreating(false)}
          onSave={async ({ name, description, config }) => {
            await api.createSubscription({
              adapter_id: adapter.id,
              name,
              description,
              config,
            });
            await load();
            await onChanged?.();
          }}
        />
      )}
    </section>
  );
}
