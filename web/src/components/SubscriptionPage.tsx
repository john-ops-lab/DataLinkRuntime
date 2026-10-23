import { useCallback, useEffect, useRef, useState } from "react";
import { Alert, Button, Select, Space, Table } from "antd";
import { useTranslation } from "react-i18next";
import { api } from "../api";
import type { Adapter } from "../types";
import type {
  EventSubscription,
  SubscriptionCapabilities,
  SubscriptionDraftState,
  SubscriptionPage as Page,
} from "../subscription-types";
import { subscriptionErrorMessage } from "../subscription-message";
import SubscriptionWorkspace, {
  SubscriptionState,
} from "./SubscriptionWorkspace";

export default function SubscriptionPage({
  id,
  adapters,
  onOpen,
  onBack,
  onOpenAdapter,
  onDraftStateChange,
}: {
  id: number | null;
  adapters: Adapter[];
  onOpen: (id: number) => void;
  onBack: () => void;
  onOpenAdapter: (adapter: Adapter) => void;
  onDraftStateChange: (state: SubscriptionDraftState) => void;
}) {
  const { t } = useTranslation("subscription");
  const [page, setPage] = useState<Page>({
    items: [],
    total: 0,
    next_before_id: null,
  });
  const [caps, setCaps] = useState<SubscriptionCapabilities>({
    enabled: false,
    available_sources: [],
  });
  const [detailAdapter, setDetailAdapter] = useState<Adapter | null>(null);
  const [adapterFilter, setAdapterFilter] = useState<number | undefined>();
  const [connection, setConnection] = useState<string | undefined>();
  const [source, setSource] = useState<string | undefined>();
  const [delivery, setDelivery] = useState<string | undefined>();
  const [before, setBefore] = useState<number | undefined>();
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [draft, setDraft] = useState<SubscriptionDraftState>({
    dirty: false,
    busy: false,
  });
  const updateDraft = useCallback(
    (state: SubscriptionDraftState) => {
      setDraft(state);
      onDraftStateChange(state);
    },
    [onDraftStateChange],
  );
  useEffect(() => {
    if (!draft.dirty && !draft.busy) return;
    const warn = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [draft.dirty, draft.busy]);
  const generation = useRef(0);
  const load = useCallback(async () => {
    const requestGeneration = ++generation.current;
    setLoading(true);
    try {
      const [next, capabilities] = await Promise.all([
        api.listSubscriptions({
          adapter_id: adapterFilter,
          source_type: source,
          connection_state: connection,
          delivery_state: delivery,
          before_id: before,
          limit: 20,
        }),
        api.subscriptionCapabilities(),
      ]);
      if (requestGeneration !== generation.current) return;
      setPage(next);
      setCaps(capabilities);
      if (id !== null) {
        const sub = await api.getSubscription(id);
        const nextAdapter = await api.getAdapter(sub.adapter_id);
        if (requestGeneration !== generation.current) return;
        setDetailAdapter(nextAdapter);
      }
      setError(null);
    } catch (reason) {
      if (requestGeneration === generation.current)
        setError(subscriptionErrorMessage(reason));
    } finally {
      if (requestGeneration === generation.current) setLoading(false);
    }
  }, [adapterFilter, source, connection, delivery, before, id]);
  useEffect(() => {
    const timer = window.setTimeout(() => void load(), 0);
    const polling = window.setInterval(() => void load(), 5000);
    return () => {
      generation.current += 1;
      window.clearTimeout(timer);
      window.clearInterval(polling);
    };
  }, [load]);
  if (id !== null)
    return (
      <div className="subscription-page">
        <Button onClick={onBack}>{t("back")}</Button>
        {error && <Alert type="error" message={error} />}
        {detailAdapter && (
          <>
            <Button type="link" onClick={() => onOpenAdapter(detailAdapter)}>
              {detailAdapter.name}
            </Button>
            <SubscriptionWorkspace
              key={id}
              id={id}
              adapter={detailAdapter}
              capabilities={caps}
              onDraftStateChange={updateDraft}
              onDeleted={onBack}
            />
          </>
        )}
      </div>
    );
  return (
    <section className="subscription-page" data-testid="subscriptions-page">
      <header className="subscription-header">
        <div>
          <h1>{t("title")}</h1>
          <p>{t("subtitle")}</p>
        </div>
        <Button onClick={() => void load()}>{t("refresh")}</Button>
      </header>
      {error && <Alert type="error" showIcon message={error} />}
      {(!caps.enabled || !caps.available_sources.includes("kafka")) && (
        <Alert type="info" showIcon message={t("unavailable")} />
      )}
      <Space wrap>
        <Select
          aria-label={t("adapter")}
          placeholder={t("adapter")}
          allowClear
          style={{ minWidth: 200 }}
          value={adapterFilter}
          options={adapters.map((item) => ({
            value: item.id,
            label: item.name,
          }))}
          onChange={(value: number | undefined) => {
            setAdapterFilter(value);
            setBefore(undefined);
          }}
        />
        <Select
          aria-label={t("source")}
          placeholder={t("source")}
          value={source}
          allowClear
          style={{ minWidth: 120 }}
          options={caps.available_sources
            .filter((value) => value === "kafka")
            .map((value) => ({ value, label: "Kafka" }))}
          onChange={(value: string | undefined) => {
            setSource(value);
            setBefore(undefined);
          }}
        />
        <Select
          aria-label={t("connection")}
          placeholder={t("connection")}
          value={connection}
          allowClear
          style={{ minWidth: 160 }}
          options={[
            "starting",
            "listening",
            "reconnecting",
            "blocked",
            "stopping",
            "stopped",
          ].map((value) => ({ value, label: t(`states.${value}`) }))}
          onChange={(value: string | undefined) => {
            setConnection(value);
            setBefore(undefined);
          }}
        />
        <Select
          aria-label={t("delivery")}
          placeholder={t("delivery")}
          value={delivery}
          allowClear
          style={{ minWidth: 160 }}
          options={[
            "idle",
            "pending",
            "processing",
            "blocked",
            "draining",
            "repair",
          ].map((value) => ({ value, label: t(`states.${value}`) }))}
          onChange={(value: string | undefined) => {
            setDelivery(value);
            setBefore(undefined);
          }}
        />
      </Space>
      <Table<EventSubscription>
        rowKey="id"
        loading={loading}
        pagination={false}
        scroll={{ x: 950 }}
        dataSource={page.items}
        columns={[
          {
            title: t("name"),
            render: (_, row) => (
              <Button type="link" onClick={() => onOpen(row.id)}>
                {row.name || `#${row.id}`}
              </Button>
            ),
          },
          {
            title: t("adapter"),
            render: (_, row) =>
              adapters.find((item) => item.id === row.adapter_id)?.name ??
              `#${row.adapter_id}`,
          },
          {
            title: t("desired"),
            render: (_, row) => <SubscriptionState value={row.desired_state} />,
          },
          {
            title: t("connection"),
            render: (_, row) => (
              <SubscriptionState value={row.connection_state} />
            ),
          },
          {
            title: t("delivery"),
            render: (_, row) => (
              <SubscriptionState value={row.delivery_state} />
            ),
          },
          {
            title: t("quota"),
            render: (_, row) =>
              `${row.quota.pending_count ?? 0} / ${row.quota.pending_bytes ?? 0} B`,
          },
          { title: t("error"), dataIndex: "last_error_code" },
        ]}
      />
      <Space>
        <span>{t("total", { count: page.total })}</span>
        <Button
          disabled={before === undefined}
          onClick={() => setBefore(undefined)}
        >
          {t("first")}
        </Button>
        <Button
          disabled={page.next_before_id === null}
          onClick={() => setBefore(page.next_before_id ?? undefined)}
        >
          {t("next")}
        </Button>
      </Space>
    </section>
  );
}
