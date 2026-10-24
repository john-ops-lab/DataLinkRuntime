import { useCallback, useEffect, useRef, useState } from "react";
import { Alert, Button, Descriptions, Modal, Space, Table, Tag } from "antd";
import { useTranslation } from "react-i18next";
import { api } from "../api";
import { subscriptionErrorMessage } from "../subscription-message";
import { canAbandonCompletion } from "../subscription-types";
import type {
  EventSubscription,
  SubscriptionDraftState,
  SyncBatchDetail,
  SyncBatchPage,
  SyncBatchSummary,
} from "../subscription-types";
import SubscriptionAbandonDialog from "./SubscriptionAbandonDialog";

export default function SubscriptionBatchPanel({
  sub,
  canEdit,
  selectedId,
  onSelect,
  onEvent,
  onExecution,
  onChanged,
  onDraftStateChange,
}: {
  sub: EventSubscription;
  canEdit: boolean;
  selectedId: number | null;
  onSelect: (id: number | null) => void;
  onEvent: (id: number) => void;
  onExecution: (id: number) => void;
  onChanged: () => Promise<void>;
  onDraftStateChange?: (state: SubscriptionDraftState) => void;
}) {
  const { t } = useTranslation("subscription");
  const [page, setPage] = useState<SyncBatchPage>({
    items: [],
    total: 0,
    next_before_id: null,
  });
  const [before, setBefore] = useState<number>();
  const [detail, setDetail] = useState<SyncBatchDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [abandon, setAbandon] = useState<{
    sub: EventSubscription;
    batch: SyncBatchDetail;
  } | null>(null);
  const generation = useRef(0);
  const id = sub.id;
  const load = useCallback(async () => {
    const ticket = ++generation.current;
    try {
      const [rows, batch] = await Promise.all([
        api.subscriptionBatches(id, before),
        selectedId === null
          ? Promise.resolve(null)
          : api.subscriptionBatch(id, selectedId),
      ]);
      if (ticket !== generation.current) return;
      setPage(rows);
      setDetail(batch);
      setError(null);
    } catch (reason) {
      if (ticket === generation.current)
        setError(subscriptionErrorMessage(reason, "kubernetes"));
    }
  }, [id, before, selectedId]);
  useEffect(() => {
    const initial = window.setTimeout(() => void load(), 0);
    const timer = window.setInterval(() => void load(), 5000);
    return () => {
      generation.current += 1;
      window.clearTimeout(initial);
      window.clearInterval(timer);
    };
  }, [load]);
  const facts = (batch: SyncBatchSummary) => (
    <Space wrap>
      <Tag>{t(`batch.states.${batch.state}`)}</Tag>
      {batch.read_complete ? (
        <Tag>{t("batch.readComplete")}</Tag>
      ) : (
        <Tag>{t("batch.readIncomplete")}</Tag>
      )}
      {batch.completion_committed ? (
        <Tag>{t("batch.completionFrozen")}</Tag>
      ) : null}
      {batch.completion_abandoned ? (
        <Tag color="warning">{t("batch.unconfirmed")}</Tag>
      ) : null}
      <Tag>{t(batch.closed ? "batch.closed" : "batch.open")}</Tag>
    </Space>
  );
  return (
    <section data-testid="subscription-batches">
      <h3>{t("batch.title")}</h3>
      <p>{t("batch.separateFacts")}</p>
      {error ? <Alert type="error" showIcon message={error} /> : null}
      <Table<SyncBatchSummary>
        rowKey="id"
        size="small"
        pagination={false}
        scroll={{ x: 820 }}
        dataSource={page.items}
        columns={[
          {
            title: t("batch.id"),
            render: (_, row) => (
              <Button type="link" onClick={() => onSelect(row.id)}>
                #{row.id}
              </Button>
            ),
          },
          { title: t("state"), render: (_, row) => facts(row) },
          {
            title: t("batch.pendingControls"),
            render: (_, row) => row.control_pending.map(kind => t(`ux.phase.${kind.slice(5)}`)).join(", ") || "—",
          },
          {
            title: t("received"),
            render: (_, row) => new Date(row.created_at).toLocaleString(),
          },
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
      {selectedId !== null ? (
        <Modal
          open
          width={900}
          title={`${t("batch.id")} #${selectedId}`}
          onCancel={() => onSelect(null)}
          footer={
            <Space>
              <Button onClick={() => void load()}>{t("refresh")}</Button>
              {detail && detail.id === selectedId && canEdit ? (
                <Button
                  danger
                  disabled={!canAbandonCompletion(sub, detail)}
                  onClick={() =>
                    setAbandon({
                      sub: structuredClone(sub),
                      batch: structuredClone(detail),
                    })
                  }
                >
                  {t("batch.abandon")}
                </Button>
              ) : null}
              <Button onClick={() => onSelect(null)}>{t("cancel")}</Button>
            </Space>
          }
        >
          {detail && detail.id === selectedId ? (
            <>
              {facts(detail)}
              {detail.completion_abandoned ? (
                <Alert
                  type="warning"
                  showIcon
                  message={t("batch.unconfirmed")}
                  description={t("batch.abandonImpact")}
                />
              ) : null}
              {detail.completion_committed && detail.closed ? (
                <Alert type="info" message={t("batch.closedReplayNote")} />
              ) : null}
              <Descriptions
                size="small"
                column={1}
                items={[
                  {
                    key: "scope",
                    label: t("batch.scope"),
                    children: (
                      <code className="subscription-compare-value">
                        {detail.scope_fingerprint}
                      </code>
                    ),
                  },
                  {
                    key: "epoch",
                    label: t("epoch"),
                    children:
                      detail.source_epoch ?? t("batch.historicalUnknown"),
                  },
                  {
                    key: "config",
                    label: t("version"),
                    children:
                      detail.config_version ?? t("batch.historicalUnknown"),
                  },
                  {
                    key: "activation",
                    label: t("activation"),
                    children:
                      detail.activation_id ?? t("batch.historicalUnknown"),
                  },
                  {
                    key: "rv",
                    label: t("kubernetes.resourceVersion"),
                    children: (
                      <code className="subscription-compare-value">
                        {detail.resource_version ?? "—"}
                      </code>
                    ),
                  },
                  {
                    key: "counts",
                    label: t("batch.counts"),
                    children: t("batch.countValues", {
                      items: detail.item_count,
                      pages: detail.page_count,
                      bytes: detail.total_bytes,
                    }),
                  },
                  {
                    key: "pending",
                    label: t("batch.pendingControls"),
                    children: detail.control_pending.join(", ") || "—",
                  },
                  {
                    key: "reserve",
                    label: t("controlReserve"),
                    children: `${detail.remaining_reserve.count} / ${detail.remaining_reserve.bytes} B`,
                  },
                  {
                    key: "gap",
                    label: t("batch.gap"),
                    children: detail.pending_gap ? (
                      <>
                        <code>{detail.pending_gap.code}</code> ·{" "}
                        {t(
                          detail.pending_gap.after_completion_committed
                            ? "batch.gapAfterFrozen"
                            : "batch.gapBeforeFrozen",
                        )}{" "}
                        ·{" "}
                        {t(
                          detail.pending_gap.relist_required
                            ? "batch.relistRequired"
                            : "batch.noRelistPending",
                        )}
                      </>
                    ) : (
                      "—"
                    ),
                  },
                  {
                    key: "failure",
                    label: t("error"),
                    children: detail.failure_code ? (
                      <code>{detail.failure_code}</code>
                    ) : (
                      "—"
                    ),
                  },
                ]}
              />
              <Table
                rowKey="kind"
                size="small"
                pagination={false}
                dataSource={detail.controls}
                columns={[
                  { title: t("batch.control"), dataIndex: "kind" },
                  {
                    title: t("state"),
                    render: (_, row) =>
                      row.state === "abandoned"
                        ? t("batch.unconfirmed")
                        : row.state
                          ? t(`states.${row.state}`, {
                              defaultValue: row.state,
                            })
                          : "—",
                  },
                  {
                    title: t("batch.receipt"),
                    render: (_, row) => (
                      <Button
                        type="link"
                        onClick={() => {
                          onSelect(null);
                          onEvent(row.receipt_id);
                        }}
                      >
                        #{row.receipt_id}
                      </Button>
                    ),
                  },
                  { title: t("ordinal"), dataIndex: "latest_delivery_ordinal" },
                  {
                    title: t("activation"),
                    dataIndex: "effective_activation_id",
                  },
                  {
                    title: t("execution"),
                    render: (_, row) =>
                      row.execution_id === null ? (
                        t("batch.notDispatched")
                      ) : (
                        <Button
                          type="link"
                          onClick={() => {
                            onSelect(null);
                            onExecution(row.execution_id!);
                          }}
                        >
                          #{row.execution_id}
                        </Button>
                      ),
                  },
                ]}
              />
              {detail.abandonment ? (
                <p>
                  {t("batch.abandonAudit", {
                    request: detail.abandonment.request_id,
                    actor: detail.abandonment.actor_id ?? "—",
                  })}{" "}
                  · {detail.abandonment.reason}
                </p>
              ) : null}
            </>
          ) : (
            <p>{t("batch.loading")}</p>
          )}
        </Modal>
      ) : null}
      {abandon ? (
        <SubscriptionAbandonDialog
          sub={abandon.sub}
          batch={abandon.batch}
          onDraftStateChange={onDraftStateChange}
          onClose={() => setAbandon(null)}
          onDone={async () => {
            await Promise.all([load(), onChanged()]);
          }}
        />
      ) : null}
    </section>
  );
}
