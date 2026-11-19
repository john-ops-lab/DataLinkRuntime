import { useState } from "react";
import { Alert, Button, Modal, Select, Space, Typography } from "antd";
import { useTranslation } from "react-i18next";
import { api } from "../api";
import { userErrorMessage } from "../user-message";
import type { PoolRoute, RuntimeRoute, WorkerPool } from "../worker-pool-types";

export function RouteLabel({ route }: { route: RuntimeRoute | null | undefined }) {
  const { t } = useTranslation("runtime");
  if (route?.mode === "pool") return <>{t("pool.route", { id: route.pool_id, revision: route.pool_revision })}</>;
  if (route?.mode === "fixed") return <>{t("pool.fixedRoute", { id: route.worker_id })}</>;
  return <>{t("pool.unbound")}</>;
}

interface Props {
  route: PoolRoute | null;
  disabled: boolean;
  readOnly?: boolean;
  placementEnabled?: boolean;
  onChange: (route: PoolRoute | null) => void;
}

export default function PoolRoutePicker({ route, disabled, readOnly, placementEnabled, onChange }: Props) {
  const { t } = useTranslation(["runtime", "common"]);
  const [open, setOpen] = useState(false);
  const [pools, setPools] = useState<WorkerPool[]>([]);
  const [choice, setChoice] = useState<number>();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const selected = pools.find((pool) => pool.id === choice);

  async function show() {
    setChoice(route?.pool_id);
    setError(null);
    setLoading(true);
    setOpen(true);
    try {
      setPools(await api.listWorkerPools());
    } catch (err) {
      setError(userErrorMessage(err));
    } finally {
      setLoading(false);
    }
  }

  function choose() {
    if (!selected || disabled) return;
    const { revision, environment_class, placement_scope, selector } = selected.revision;
    onChange({
      schema_version: 1,
      mode: "pool",
      pool_id: selected.id,
      pool_revision: revision,
      environment_class,
      placement_scope,
      selector,
      failover: "compatible",
    });
    setOpen(false);
  }

  return (
    <Space direction="vertical" size="small">
      {route && <Typography.Text data-testid="configured-pool-route"><RouteLabel route={route} /></Typography.Text>}
      {route && !placementEnabled && <Typography.Text type="secondary">{t("pool.stoppedDraft")}</Typography.Text>}
      {!readOnly && <Space wrap>
        <Button disabled={disabled || loading} size="small" onClick={() => void show()}>{t("pool.choose")}</Button>
        {route && <Button disabled={disabled} size="small" onClick={() => onChange(null)}>{t("pool.useFixed")}</Button>}
      </Space>}
      <Modal
        title={t("pool.choose")}
        open={open}
        onCancel={() => setOpen(false)}
        onOk={choose}
        cancelButtonProps={{ autoInsertSpace: false }}
        okButtonProps={{ disabled: !selected || loading || disabled || error !== null }}
        okText={t("pool.useDraft")}
        cancelText={t("actions.cancel", { ns: "common" })}
      >
        {error && <Alert type="error" showIcon message={error} />}
        <Select
          aria-label={t("pool.title")}
          style={{ width: "100%" }}
          loading={loading}
          disabled={loading || error !== null}
          value={choice}
          placeholder={t("pool.choose")}
          onChange={setChoice}
          options={pools.map((pool) => ({
            value: pool.id,
            label: t("pool.choice", { name: pool.name, revision: pool.revision.revision,
              state: t(pool.enabled ? "pool.enabled" : "pool.disabled") }),
          }))}
        />
        {selected && <p>{t("pool.ruleSummary", { environment: selected.revision.environment_class,
          scope: selected.revision.placement_scope })}</p>}
      </Modal>
    </Space>
  );
}
