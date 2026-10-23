import { useCallback, useEffect, useRef, useState } from "react";
import {
  Alert,
  Button,
  Descriptions,
  Input,
  Modal,
  Space,
  Table,
  Tag,
} from "antd";
import { useTranslation } from "react-i18next";
import { api } from "../api";
import type { Adapter } from "../types";
import type {
  EventSubscription,
  SourceCheckpoint,
  SubscriptionActivation,
  SubscriptionCapabilities,
  SubscriptionDraftState,
  SubscriptionEvent,
  SubscriptionEventPage,
} from "../subscription-types";
import { subscriptionDrained } from "../subscription-types";
import { subscriptionErrorMessage } from "../subscription-message";
import ExecutionHistoryPanel from "./ExecutionHistoryPanel";
import KafkaSubscriptionConfig from "./KafkaSubscriptionConfig";
import SubscriptionActionDialog, {
  type SubscriptionActionKind,
} from "./SubscriptionActionDialog";
import "./subscriptions.css";

export function SubscriptionState({ value }: { value: string }) {
  const { t } = useTranslation("subscription");
  return (
    <Tag
      color={
        ["failed", "blocked"].includes(value)
          ? "error"
          : ["listening", "succeeded"].includes(value)
            ? "success"
            : [
                  "pending",
                  "draining",
                  "reconnecting",
                  "stopping",
                  "repair",
                ].includes(value)
              ? "warning"
              : "default"
      }
    >
      {t(`states.${value}`, { defaultValue: value })}
    </Tag>
  );
}

export default function SubscriptionWorkspace({
  id,
  adapter,
  capabilities,
  activationBlocked = false,
  onChanged,
  onDeleted,
  onDraftStateChange,
}: {
  id: number;
  adapter: Adapter;
  capabilities: SubscriptionCapabilities;
  activationBlocked?: boolean;
  onChanged?: () => Promise<void>;
  onDeleted: () => void;
  onDraftStateChange?: (state: SubscriptionDraftState) => void;
}) {
  const { t } = useTranslation("subscription");
  const [sub, setSub] = useState<EventSubscription | null>(null);
  const [events, setEvents] = useState<SubscriptionEventPage>({
    items: [],
    total: 0,
    next_before_id: null,
  });
  const [checkpoints, setCheckpoints] = useState<SourceCheckpoint[]>([]);
  const [checkpointBefore, setCheckpointBefore] = useState<
    number | undefined
  >();
  const [checkpointNext, setCheckpointNext] = useState<number | null>(null);
  const [before, setBefore] = useState<number | undefined>();
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [configuration, setConfiguration] = useState<EventSubscription | null>(
    null,
  );
  const [metadata, setMetadata] = useState<{
    name: string;
    description: string;
  } | null>(null);
  const [metadataBusy, setMetadataBusy] = useState(false);
  const [metadataDirty, setMetadataDirty] = useState(false);
  const [configDraft, setConfigDraft] = useState<SubscriptionDraftState>({
    dirty: false,
    busy: false,
  });
  const [actionDraft, setActionDraft] = useState<SubscriptionDraftState>({
    dirty: false,
    busy: false,
  });
  const draftDirty = metadataDirty || configDraft.dirty || actionDraft.dirty;
  const draftBusy = metadataBusy || configDraft.busy || actionDraft.busy;
  useEffect(() => {
    onDraftStateChange?.({ dirty: draftDirty, busy: draftBusy });
  }, [draftDirty, draftBusy, onDraftStateChange]);
  useEffect(
    () => () => onDraftStateChange?.({ dirty: false, busy: false }),
    [onDraftStateChange],
  );
  const [selectedEvent, setSelectedEvent] = useState<SubscriptionEvent | null>(
    null,
  );
  const [activation, setActivation] = useState<SubscriptionActivation | null>(
    null,
  );
  const [executionId, setExecutionId] = useState<number | null>(null);
  const [diagnostic, setDiagnostic] = useState<string | null>(null);
  const [testing, setTesting] = useState(false);
  const [action, setAction] = useState<{
    kind: SubscriptionActionKind;
    event?: SubscriptionEvent;
    checkpoint?: SourceCheckpoint;
    code?: string;
    requirements?: string;
  } | null>(null);
  const loadingRef = useRef(false);
  const generation = useRef(0);
  const mounted = useRef(true);
  const reload = useCallback(async () => {
    if (loadingRef.current) return;
    loadingRef.current = true;
    const requestGeneration = generation.current;
    try {
      const [next, page, checkpointPage] = await Promise.all([
        api.getSubscription(id),
        api.listSubscriptionEvents(id, before),
        api.subscriptionCheckpoints(id, checkpointBefore),
      ]);
      if (mounted.current && requestGeneration === generation.current) {
        setSub(next);
        setEvents(page);
        setCheckpoints(checkpointPage.items);
        setCheckpointNext(checkpointPage.next_before_id);
        setError(null);
      }
    } catch (reason) {
      if (mounted.current && requestGeneration === generation.current)
        setError(subscriptionErrorMessage(reason));
    } finally {
      if (requestGeneration === generation.current) {
        loadingRef.current = false;
        if (mounted.current) setLoading(false);
      }
    }
  }, [id, before, checkpointBefore]);
  useEffect(() => {
    mounted.current = true;
    const initial = window.setTimeout(() => void reload(), 0);
    const timer = window.setInterval(() => void reload(), 5000);
    return () => {
      mounted.current = false;
      generation.current += 1;
      loadingRef.current = false;
      window.clearTimeout(initial);
      window.clearInterval(timer);
    };
  }, [reload]);
  const selectedEventId = selectedEvent?.id;
  const eventGeneration = useRef(0);
  const refreshSelectedEvent = useCallback(async () => {
    if (selectedEventId === undefined) return;
    const requestGeneration = eventGeneration.current;
    try {
      const value = await api.getSubscriptionEvent(id, selectedEventId);
      if (mounted.current && requestGeneration === eventGeneration.current)
        setSelectedEvent(value);
    } catch (reason) {
      if (mounted.current && requestGeneration === eventGeneration.current)
        setError(subscriptionErrorMessage(reason));
    }
  }, [id, selectedEventId]);
  useEffect(() => {
    if (selectedEventId === undefined) return;
    const initial = window.setTimeout(() => void refreshSelectedEvent(), 0);
    const timer = window.setInterval(() => void refreshSelectedEvent(), 5000);
    return () => {
      eventGeneration.current += 1;
      window.clearTimeout(initial);
      window.clearInterval(timer);
    };
  }, [refreshSelectedEvent, selectedEventId]);
  const canEdit =
    (sub?.access_level ?? adapter.access_level ?? "read") !== "read";
  const canManage = ["admin", "owner"].includes(
    sub?.access_level ?? adapter.access_level ?? "read",
  );
  const drained = sub !== null && subscriptionDrained(sub);
  const canEnable =
    sub?.available === true &&
    capabilities.enabled &&
    capabilities.available_sources.includes("kafka");
  async function changed() {
    await reload();
    await onChanged?.();
  }
  async function testConnection() {
    if (!sub) return;
    setTesting(true);
    setError(null);
    setDiagnostic(null);
    try {
      const result = await api.testSubscriptionConnection(
        id,
        sub.config_version,
      );
      setDiagnostic(
        `${t("testPassed")} ${t("testResult", { cluster: result.cluster_id, topics: result.topic_count ?? "—", partitions: result.partition_count ?? "—" })}`,
      );
    } catch (reason) {
      setError(subscriptionErrorMessage(reason));
    } finally {
      setTesting(false);
    }
  }
  async function viewActivation(activationId: number) {
    try {
      setActivation(await api.subscriptionActivation(id, activationId));
    } catch (reason) {
      setError(subscriptionErrorMessage(reason));
    }
  }
  async function repairForm() {
    if (!sub) return;
    try {
      const original = sub.activation_id
        ? await api.subscriptionActivation(id, sub.activation_id)
        : null;
      const version = original
        ? await api.getVersion(
            adapter.id,
            sub.repair_revision_id ?? original.revision_id,
          )
        : null;
      setAction({
        kind: "repairSave",
        code: version?.code ?? "",
        requirements: version?.requirements ?? "",
      });
    } catch (reason) {
      setError(subscriptionErrorMessage(reason));
    }
  }
  if (!sub)
    return (
      <div aria-busy={loading}>
        {error && <Alert type="error" message={error} />}
        <Button onClick={() => void reload()}>{t("refresh")}</Button>
      </div>
    );
  return (
    <div
      className="subscription-workspace"
      data-testid="subscription-workspace"
    >
      <header className="subscription-header">
        <div>
          <h2>{sub.name || `#${sub.id}`}</h2>
          <p>{sub.description}</p>
        </div>
        <Button onClick={() => void reload()}>{t("refresh")}</Button>
      </header>
      {error && <Alert type="error" showIcon role="alert" message={error} />}
      {!capabilities.enabled && (
        <Alert type="warning" showIcon message={t("featureOff")} />
      )}
      {!canEdit && <Alert type="info" message={t("readOnly")} />}
      {sub.last_error_code && (
        <Alert
          type="warning"
          showIcon
          message={
            <>
              <code>{sub.last_error_code}</code> ·{" "}
              {t(`errors.${sub.last_error_code}`, {
                defaultValue: t("operationFailed"),
              })}
            </>
          }
        />
      )}
      <Descriptions
        size="small"
        column={{ xs: 1, md: 3 }}
        items={[
          { key: "adapter", label: t("adapter"), children: adapter.name },
          { key: "source", label: t("source"), children: sub.source_type },
          { key: "version", label: t("version"), children: sub.config_version },
          {
            key: "desired",
            label: t("desired"),
            children: <SubscriptionState value={sub.desired_state} />,
          },
          {
            key: "connection",
            label: t("connection"),
            children: <SubscriptionState value={sub.connection_state} />,
          },
          {
            key: "delivery",
            label: t("delivery"),
            children: <SubscriptionState value={sub.delivery_state} />,
          },
          {
            key: "epoch",
            label: t("epoch"),
            children: <code>{sub.source_epoch}</code>,
          },
          {
            key: "activation",
            label: t("activation"),
            children: sub.activation_id ? (
              <Button
                type="link"
                onClick={() => void viewActivation(sub.activation_id!)}
              >
                #{sub.activation_id}
              </Button>
            ) : (
              "—"
            ),
          },
          {
            key: "oldest",
            label: t("oldest"),
            children: sub.oldest_pending_at
              ? new Date(sub.oldest_pending_at).toLocaleString()
              : "—",
          },
        ]}
      />
      {canEdit && (
        <Space wrap>
          <Button
            onClick={() =>
              setMetadata({ name: sub.name, description: sub.description })
            }
          >
            {t("metadata")}
          </Button>
          {sub.source_type === "kafka" && (
            <Button
              disabled={!drained}
              onClick={() => setConfiguration(structuredClone(sub))}
            >
              {t("configure")}
            </Button>
          )}
          <Button
            loading={testing}
            disabled={!capabilities.enabled || testing}
            onClick={() => void testConnection()}
          >
            {t("test")}
          </Button>
          {sub.desired_state === "disabled" ? (
            <Button
              type="primary"
              disabled={!canEnable || !drained || activationBlocked}
              onClick={() => setAction({ kind: "enable" })}
            >
              {t("enable")}
            </Button>
          ) : (
            <Button onClick={() => setAction({ kind: "disable" })}>
              {t("disable")}
            </Button>
          )}
          {sub.desired_state === "enabled" &&
            sub.connection_state === "blocked" && (
              <Button
                disabled={!canEnable}
                onClick={() => setAction({ kind: "resume" })}
              >
                {t("resume")}
              </Button>
            )}
          <Button
            danger
            disabled={!drained}
            onClick={() => setAction({ kind: "reset" })}
          >
            {t("reset")}
          </Button>
          <Button
            danger
            disabled={!drained}
            onClick={() => setAction({ kind: "delete" })}
          >
            {t("delete")}
          </Button>
          {sub.delivery_state === "repair" ? (
            <>
              <Button
                disabled={(sub.event_counts.processing ?? 0) > 0}
                onClick={() => void repairForm()}
              >
                {t("repairSave")}
              </Button>
              <Button
                disabled={
                  (sub.event_counts.processing ?? 0) > 0 ||
                  (sub.event_counts.failed ?? 0) > 0
                }
                onClick={() => setAction({ kind: "repairExit" })}
              >
                {t("repairExit")}
              </Button>
            </>
          ) : (
            <Button
              disabled={
                sub.desired_state !== "disabled" ||
                sub.connection_state !== "stopped" ||
                sub.activation_id === null
              }
              onClick={() => setAction({ kind: "repairEnter" })}
            >
              {t("repairEnter")}
            </Button>
          )}
        </Space>
      )}
      {diagnostic && <Alert type="success" showIcon message={diagnostic} />}
      {!drained && <Alert type="info" message={t("locked")} />}
      <section>
        <h3>{t("quota")}</h3>
        <div className="subscription-metrics">
          {[
            "pending_count",
            "pending_bytes",
            "history_count",
            "history_bytes",
            "dedupe_count",
          ].map((key) => (
            <div key={key}>
              <span>{t(key)}</span>
              <strong>
                {(sub.quota[key] ?? 0).toLocaleString()} /{" "}
                {sub.limits?.[key]?.toLocaleString() ?? "—"}
              </strong>
            </div>
          ))}
        </div>
        <p>
          {t("controlReserve")}: {sub.quota.reserved_control_count ?? 0} /{" "}
          {sub.quota.reserved_control_bytes ?? 0} B
        </p>
      </section>
      <section>
        <h3>{t("counts")}</h3>
        <Space wrap>
          {Object.entries(sub.event_counts).map(([key, count]) => (
            <Tag key={key}>
              {t(`states.${key}`, { defaultValue: key })}: {count}
            </Tag>
          ))}
        </Space>
      </section>
      <section>
        <h3>{t("checkpoints")}</h3>
        <p>{t("gapNote")}</p>
        <Table<SourceCheckpoint>
          rowKey="id"
          size="small"
          pagination={false}
          scroll={{ x: 720 }}
          dataSource={checkpoints}
          columns={[
            { title: t("shard"), dataIndex: "shard" },
            {
              title: t("position"),
              render: (_, row) => <code>{JSON.stringify(row.position)}</code>,
            },
            {
              title: t("confirmed"),
              render: (_, row) => (
                <code>{JSON.stringify(row.confirmed_position)}</code>
              ),
            },
            {
              title: t("sourceLag"),
              render: (_, row) =>
                row.source_lag === null || row.source_lag === undefined
                  ? t("notCollected")
                  : row.source_lag,
            },
            {
              title: t("error"),
              render: (_, row) =>
                row.blocked_unaccepted ? (
                  <>
                    <code>{row.blocked_unaccepted.code}</code>
                    {canEdit && (
                      <Button
                        danger
                        size="small"
                        disabled={!row.blocked_unaccepted.source_event_id}
                        onClick={() =>
                          setAction({ kind: "skipUnaccepted", checkpoint: row })
                        }
                      >
                        {t("skipUnaccepted")}
                      </Button>
                    )}
                  </>
                ) : row.blocked_receipt_id ? (
                  `Receipt #${row.blocked_receipt_id}`
                ) : (
                  "—"
                ),
            },
          ]}
        />
        <Space>
          <Button
            disabled={checkpointBefore === undefined}
            onClick={() => setCheckpointBefore(undefined)}
          >
            {t("first")}
          </Button>
          <Button
            disabled={checkpointNext === null}
            onClick={() => setCheckpointBefore(checkpointNext ?? undefined)}
          >
            {t("next")}
          </Button>
        </Space>
      </section>
      <section>
        <h3>{t("events")}</h3>
        <p>{t("originalState")}</p>
        <Table<SubscriptionEvent>
          rowKey="id"
          size="small"
          loading={loading}
          pagination={false}
          scroll={{ x: 750 }}
          dataSource={events.items}
          columns={[
            {
              title: t("event"),
              render: (_, row) => (
                <Button
                  type="link"
                  className="subscription-event-id"
                  onClick={() => setSelectedEvent(row)}
                >
                  {row.source_event_id}
                </Button>
              ),
            },
            { title: t("shard"), dataIndex: "shard" },
            {
              title: t("state"),
              render: (_, row) => <SubscriptionState value={row.state} />,
            },
            {
              title: t("received"),
              render: (_, row) =>
                new Date(row.first_received_at).toLocaleString(),
            },
            {
              title: t("execution"),
              render: (_, row) => (
                <Space wrap>
                  {row.deliveries
                    .filter((item) => item.execution_id !== null)
                    .map((item) => (
                      <Button
                        key={item.id}
                        type="link"
                        onClick={() => setExecutionId(item.execution_id)}
                      >{`#${item.execution_id} · ${item.ordinal}`}</Button>
                    ))}
                </Space>
              ),
            },
          ]}
        />
        <Space>
          <span>{t("total", { count: events.total })}</span>
          <Button
            disabled={before === undefined}
            onClick={() => setBefore(undefined)}
          >
            {t("first")}
          </Button>
          <Button
            disabled={events.next_before_id === null}
            onClick={() => setBefore(events.next_before_id ?? undefined)}
          >
            {t("next")}
          </Button>
        </Space>
      </section>
      {executionId !== null && (
        <section>
          <Button onClick={() => setExecutionId(null)}>
            {t("closeHistory")}
          </Button>
          <ExecutionHistoryPanel
            adapterId={adapter.id}
            autoOpenExecutionId={executionId}
            onAutoOpenHandled={() => {}}
            canCancel={false}
          />
        </section>
      )}
      {configuration && (
        <KafkaSubscriptionConfig
          adapterId={adapter.id}
          subscription={configuration}
          canManage={canManage}
          onDraftStateChange={setConfigDraft}
          onClose={() => setConfiguration(null)}
          onSave={async (value) => {
            await api.updateSubscription(id, {
              ...value,
              expected_config_version: configuration.config_version,
            });
            await changed();
          }}
        />
      )}
      {metadata && (
        <Modal
          open
          title={t("metadata")}
          onCancel={() => {
            if (!metadataBusy) {
              setMetadata(null);
              setMetadataDirty(false);
            }
          }}
          confirmLoading={metadataBusy}
          okText={t("save")}
          cancelText={t("cancel")}
          onOk={() => {
            setMetadataBusy(true);
            void api
              .updateSubscription(id, metadata)
              .then(async () => {
                await changed();
                setMetadata(null);
                setMetadataDirty(false);
              })
              .catch((reason: unknown) =>
                setError(subscriptionErrorMessage(reason)),
              )
              .finally(() => setMetadataBusy(false));
          }}
        >
          <label>
            {t("name")}
            <Input
              value={metadata.name}
              maxLength={128}
              onChange={(e) => {
                setMetadataDirty(true);
                setMetadata({ ...metadata, name: e.target.value });
              }}
            />
          </label>
          <label>
            {t("description")}
            <Input.TextArea
              value={metadata.description}
              maxLength={4096}
              onChange={(e) => {
                setMetadataDirty(true);
                setMetadata({ ...metadata, description: e.target.value });
              }}
            />
          </label>
        </Modal>
      )}
      {selectedEvent && (
        <Modal
          open
          width={800}
          title={`${t("event")} #${selectedEvent.id}`}
          onCancel={() => setSelectedEvent(null)}
          footer={
            <Space>
              <Button onClick={() => void refreshSelectedEvent()}>
                {t("refresh")}
              </Button>
              {canEdit && (
                <>
                  <Button
                    disabled={
                      selectedEvent.payload_expired ||
                      ["pending", "processing"].includes(selectedEvent.state)
                    }
                    onClick={() =>
                      setAction({ kind: "replay", event: selectedEvent })
                    }
                  >
                    {t("replay")}
                  </Button>
                  <Button
                    danger
                    disabled={[
                      "succeeded",
                      "skipped",
                      "superseded",
                      "processing",
                    ].includes(selectedEvent.state)}
                    onClick={() =>
                      setAction({ kind: "skip", event: selectedEvent })
                    }
                  >
                    {t("skip")}
                  </Button>
                </>
              )}
              <Button onClick={() => setSelectedEvent(null)}>
                {t("cancel")}
              </Button>
            </Space>
          }
        >
          <p>
            <code>{selectedEvent.source_event_id}</code>
          </p>
          <p>
            {t("epoch")}: {selectedEvent.source_epoch}
          </p>
          <Button
            onClick={() => void viewActivation(selectedEvent.activation_id)}
          >
            {t("activation")} #{selectedEvent.activation_id}
          </Button>
          <p>{t("originalState")}</p>
          <Table
            rowKey="id"
            size="small"
            pagination={false}
            dataSource={selectedEvent.deliveries}
            columns={[
              { title: t("ordinal"), dataIndex: "ordinal" },
              {
                title: t("state"),
                render: (_, row) => <SubscriptionState value={row.state} />,
              },
              {
                title: t("revision"),
                render: (_, row) =>
                  row.revision_override_id
                    ? `#${row.revision_override_id}`
                    : t("original"),
              },
              {
                title: t("activation"),
                render: (_, row) => (
                  <Button
                    type="link"
                    onClick={() =>
                      void viewActivation(row.effective_activation_id)
                    }
                  >
                    #{row.effective_activation_id}
                  </Button>
                ),
              },
              {
                title: t("execution"),
                render: (_, row) =>
                  row.execution_id ? (
                    <Button
                      type="link"
                      onClick={() => {
                        setExecutionId(row.execution_id);
                        setSelectedEvent(null);
                      }}
                    >
                      {t("viewExecution", { id: row.execution_id })}
                    </Button>
                  ) : (
                    "—"
                  ),
              },
            ]}
          />
          {selectedEvent.payload_expired ? (
            <Alert type="warning" message={t("payloadExpired")} />
          ) : (
            <details>
              <summary>{t("payload")}</summary>
              <pre className="subscription-json">
                {JSON.stringify(selectedEvent.envelope, null, 2)}
              </pre>
            </details>
          )}
        </Modal>
      )}
      {activation && (
        <Modal
          open
          title={`${t("activation")} #${activation.id}`}
          onCancel={() => setActivation(null)}
          footer={
            <Button onClick={() => setActivation(null)}>{t("cancel")}</Button>
          }
        >
          <Alert
            type="info"
            message={t("versionComparison", {
              original: activation.config_version,
              current: sub.config_version,
            })}
          />
          <Descriptions
            column={1}
            items={[
              {
                key: "revision",
                label: t("revision"),
                children: `#${activation.revision_id}`,
              },
              {
                key: "worker",
                label: t("worker"),
                children: `#${activation.worker_id}`,
              },
              {
                key: "config",
                label: t("version"),
                children: activation.config_version,
              },
              {
                key: "epoch",
                label: t("epoch"),
                children: activation.source_epoch,
              },
              {
                key: "source-credential",
                label: t("sourceCredential"),
                children:
                  activation.source_credential_id === null
                    ? "—"
                    : `#${activation.source_credential_id}`,
              },
            ]}
          />
          <h4>{t("credentialRefs")}</h4>
          <pre className="subscription-json">
            {JSON.stringify(activation.credential_refs, null, 2)}
          </pre>
        </Modal>
      )}
      {action && (
        <SubscriptionActionDialog
          key={`${action.kind}-${action.event?.id ?? action.checkpoint?.id ?? "sub"}`}
          action={action.kind}
          onDraftStateChange={setActionDraft}
          sub={sub}
          event={action.event}
          checkpoint={action.checkpoint}
          initialCode={action.code}
          initialRequirements={action.requirements}
          onClose={() => setAction(null)}
          onDone={async () => {
            setSelectedEvent(null);
            if (action.kind === "delete") {
              await onChanged?.();
              onDeleted();
            } else await changed();
          }}
        />
      )}
    </div>
  );
}
