/** Low-frequency Adapter metadata, clone and soft-delete actions. */

import { Alert, Button, Divider, Drawer, Input, Space } from "antd";
import { useTranslation } from "react-i18next";

import { LANGUAGE_LABELS } from "../languages";
import type { Adapter } from "../types";
import ActionWithReason from "./ActionWithReason";

interface Props {
  open: boolean;
  adapter: Adapter | null;
  name: string;
  description: string;
  busy: boolean;
  contentReady: boolean;
  onClose: () => void;
  onNameChange: (value: string) => void;
  onDescriptionChange: (value: string) => void;
  onUpdate: () => void;
  onDelete: () => void;
  onClone: () => void;
}

export default function AdapterSettingsDrawer(props: Props) {
  const { t } = useTranslation(["adapter", "common"]);
  const adapter = props.adapter;
  const archived = !!adapter?.archived_at;
  return (
    <Drawer
      title={adapter?.adapter_type === "webhook" ? t("settings.webhookTitle") : t("settings.taskTitle")}
      width={400}
      open={props.open}
      destroyOnHidden
      onClose={props.onClose}
    >
      {adapter !== null && (
        <div className="settings-form">
          <label className="settings-field">
            <span className="settings-field-label">{t("settings.name")}</span>
            <Input data-testid="adapter-name" value={props.name} disabled={props.busy || archived} onChange={(event) => props.onNameChange(event.target.value)} />
          </label>
          <label className="settings-field">
            <span className="settings-field-label">{t("settings.language")}</span>
            <Input data-testid="adapter-language" value={LANGUAGE_LABELS[adapter.language]} disabled />
          </label>
          <label className="settings-field">
            <span className="settings-field-label">{t("settings.description")}</span>
            <Input data-testid="adapter-description" value={props.description} disabled={props.busy || archived} onChange={(event) => props.onDescriptionChange(event.target.value)} />
          </label>
          <Button type="primary" data-testid="update-details" disabled={props.busy || !props.contentReady || archived} onClick={props.onUpdate}>{t("settings.update")}</Button>
          <Divider />
          {archived ? (
            <Alert
              type="info"
              showIcon
              data-testid="archived-settings-readonly"
               message={t("settings.archivedMessage")}
               description={t("settings.archivedDescription")}
            />
          ) : (
            <Space direction="vertical" className="settings-lifecycle-actions">
               <Button data-testid="clone-adapter" disabled={props.busy} onClick={props.onClone}>{t("settings.clone")}</Button>
              <ActionWithReason
                 label={t("settings.delete")}
                 reason={adapter.runtime_locked === true ? t("settings.deleteReasonLocked") : props.busy ? t("settings.deleteReasonBusy") : null}
              >
                <Button danger data-testid="delete-adapter" disabled={props.busy || adapter.runtime_locked === true} onClick={props.onDelete}>{t("settings.delete")}</Button>
              </ActionWithReason>
            </Space>
          )}
          {adapter.runtime_locked === true && (
           <Alert type="warning" showIcon message={adapter.adapter_type === "webhook" ? t("settings.deleteWarningWebhook") : t("settings.deleteWarningTask")} />
          )}
           {!archived && <p className="settings-danger-hint">{t("settings.dangerHint")}</p>}
        </div>
      )}
    </Drawer>
  );
}
