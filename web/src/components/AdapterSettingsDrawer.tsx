/** Adapter settings, sharing and lifecycle actions (M5.11 Wave C). */

import { useState } from "react";
import {
  Alert,
  Button,
  Descriptions,
  Divider,
  Drawer,
  Form,
  Input,
  Space,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import {
  ArrowLeftOutlined,
  CopyOutlined,
  DeleteOutlined,
  SaveOutlined,
  TeamOutlined,
} from "@ant-design/icons";
import { useTranslation } from "react-i18next";

import { canEditAdapter, canManageAdapter } from "../adapter-access";
import { LANGUAGE_LABELS } from "../languages";
import type { Adapter, AdapterAccessLevel } from "../types";
import ActionWithReason from "./ActionWithReason";
import AdapterPermissionsPanel from "./AdapterPermissionsPanel";

interface SettingsValues {
  name: string;
  description: string;
}

interface Props {
  open: boolean;
  adapter: Adapter | null;
  name: string;
  description: string;
  busy: boolean;
  contentReady: boolean;
  onClose: () => void;
  onUpdate: (name: string, description: string) => Promise<boolean>;
  onDelete: (stopAndDelete: boolean) => void;
  onClone: () => void;
  accessLevel?: AdapterAccessLevel;
  onPermissionsChanged?: () => void;
}

export default function AdapterSettingsDrawer(props: Props) {
  const adapterKey = props.adapter?.id ?? "none";
  const formKey = `${props.open ? "open" : "closed"}:${adapterKey}:${props.name}:${props.description}`;
  return <AdapterSettingsDrawerContent key={formKey} {...props} />;
}

function AdapterSettingsDrawerContent(props: Props) {
  const { t } = useTranslation(["adapter", "common"]);
  const [form] = Form.useForm<SettingsValues>();
  const adapter = props.adapter;
  const [view, setView] = useState<"settings" | "permissions">("settings");
  const [formDirty, setFormDirty] = useState(false);
  const [formValid, setFormValid] = useState(true);
  const archived = !!adapter?.archived_at;
  const accessLevel = adapter === null ? "read" : props.accessLevel ?? "admin";
  const canEdit = canEditAdapter(accessLevel);
  const canManage = canManageAdapter(accessLevel);

  function requestClose() {
    if (formDirty && !window.confirm(t("settings.unsavedClose"))) {
      return;
    }
    props.onClose();
  }

  async function submit(values: SettingsValues) {
    if (!formDirty || props.busy || !props.contentReady || archived || !canEdit) {
      return;
    }
    const saved = await props.onUpdate(values.name, values.description);
    if (saved) {
      setFormDirty(false);
      form.setFieldsValue(values);
    }
  }

  const title =
    view === "permissions"
      ? t("sharing.title")
      : adapter?.adapter_type === "webhook"
        ? t("settings.webhookTitle")
        : t("settings.taskTitle");

  const footer =
    view === "settings" && adapter !== null ? (
      <div className="adapter-settings-footer">
        <Button onClick={requestClose}>{t("actions.cancel", { ns: "common" })}</Button>
        <Button
          type="primary"
          icon={<SaveOutlined />}
          htmlType="submit"
          form="adapter-settings-form"
          data-testid="update-details"
          loading={props.busy}
          disabled={
            props.busy ||
            !props.contentReady ||
            archived ||
            !canEdit ||
            !formDirty ||
            !formValid
          }
        >
          {t("settings.save")}
        </Button>
      </div>
    ) : null;

  return (
    <Drawer
      title={
        <div className="adapter-settings-title">
          {view === "permissions" && (
            <Tooltip title={t("sharing.back")}>
              <Button
                type="text"
                icon={<ArrowLeftOutlined />}
                aria-label={t("sharing.back")}
                data-testid="adapter-permissions-back"
                onClick={() => setView("settings")}
              />
            </Tooltip>
          )}
          <span>{title}</span>
        </div>
      }
      className="adapter-settings-drawer"
      width="min(520px, calc(100vw - 16px))"
      open={props.open}
      destroyOnHidden
      footer={footer}
      onClose={requestClose}
    >
      {adapter !== null && view === "permissions" && canManage && !archived && (
        <AdapterPermissionsPanel
          adapterId={adapter.id}
          ownerLabel={adapter.owner_username ?? t("access.systemOwner")}
          onChanged={props.onPermissionsChanged}
        />
      )}

      {adapter !== null && (
        <Form
          id="adapter-settings-form"
          form={form}
          initialValues={{ name: props.name, description: props.description }}
          className="settings-form adapter-settings-form"
          style={{ display: view === "settings" ? undefined : "none" }}
          layout="vertical"
          onValuesChange={(_, values: SettingsValues) => {
            setFormDirty(
              values.name !== props.name || values.description !== props.description,
            );
            setFormValid(values.name.trim() !== "");
          }}
          onFinish={(values) => void submit(values)}
          onFinishFailed={() => setFormValid(false)}
        >
          <Alert
            type="info"
            showIcon
            message={t("settings.summary")}
            description={
              <Descriptions
                size="small"
                column={1}
                items={[
                  { key: "type", label: t("settings.type"), children: t(`types.${adapter.adapter_type}`) },
                  { key: "language", label: t("settings.language"), children: LANGUAGE_LABELS[adapter.language] },
                  {
                    key: "revision",
                    label: t("settings.revision"),
                    children: adapter.latest_version_id == null ? t("settings.notSaved") : `#${adapter.latest_version_id}`,
                  },
                ]}
              />
            }
          />

          <Divider orientation="left" plain>{t("settings.basicInfo")}</Divider>
          <Form.Item
            name="name"
            label={t("settings.name")}
            rules={[{ required: true, whitespace: true, message: t("settings.nameRequired") }]}
          >
            <Input data-testid="adapter-name" disabled={props.busy || archived || !canEdit} />
          </Form.Item>
          <Form.Item label={t("settings.language")}>
            <Input data-testid="adapter-language" value={LANGUAGE_LABELS[adapter.language]} readOnly disabled />
          </Form.Item>
          <Form.Item name="description" label={t("settings.description")}>
            <Input.TextArea
              data-testid="adapter-description"
              rows={4}
              autoSize={{ minRows: 3, maxRows: 4 }}
              disabled={props.busy || archived || !canEdit}
            />
          </Form.Item>

          {!canEdit && (
            <Alert
              type="info"
              showIcon
              data-testid="adapter-access-read-only"
              message={t("settings.readOnlyAccess")}
            />
          )}
          {canEdit && !canManage && (
            <Alert
              type="info"
              showIcon
              data-testid="adapter-access-edit"
              message={t("settings.editAccess")}
            />
          )}

          {canManage && !archived && (
            <>
              <Divider orientation="left" plain>{t("settings.permissionsSection")}</Divider>
              <Button
                icon={<TeamOutlined />}
                data-testid="open-adapter-permissions"
                aria-label={t("sharing.open")}
                onClick={() => setView("permissions")}
              >
                {t("sharing.open")}
              </Button>
            </>
          )}

          {!archived && (
            <>
              <Divider orientation="left" plain>{t("settings.moreActions")}</Divider>
              <Space wrap className="settings-lifecycle-actions">
                {canEdit && (
                  <Button
                    icon={<CopyOutlined />}
                    data-testid="clone-adapter"
                    aria-label={t("settings.clone")}
                    disabled={props.busy}
                    onClick={props.onClone}
                  >
                    {t("settings.clone")}
                  </Button>
                )}
              </Space>

              {canManage && (
                <>
                  <Divider orientation="left" plain>{t("settings.dangerZone")}</Divider>
                  <Alert
                    type="warning"
                    showIcon
                    message={t("settings.deleteDescription")}
                    description={
                      adapter.runtime_locked === true
                        ? t("settings.deleteWarningActive")
                        : t("settings.deleteWarningIdle")
                    }
                  />
                  <ActionWithReason
                    label={
                      adapter.runtime_locked === true
                        ? t("settings.stopAndDelete")
                        : t("settings.delete")
                    }
                    reason={props.busy ? t("settings.deleteReasonBusy") : null}
                  >
                    <Button
                      danger
                      icon={<DeleteOutlined />}
                      data-testid="delete-adapter"
                      aria-label={
                        adapter.runtime_locked === true
                          ? t("settings.stopAndDelete")
                          : t("settings.delete")
                      }
                      disabled={props.busy}
                      onClick={() => props.onDelete(adapter.runtime_locked === true)}
                    >
                      {adapter.runtime_locked === true
                        ? t("settings.stopAndDelete")
                        : t("settings.delete")}
                    </Button>
                  </ActionWithReason>
                  <Typography.Paragraph className="settings-danger-hint">
                    <Tag color="error">{t("settings.irreversible")}</Tag> {t("settings.dangerHint")}
                  </Typography.Paragraph>
                </>
              )}
            </>
          )}

          {archived && (
            <Alert
              type="info"
              showIcon
              data-testid="archived-settings-readonly"
              message={t("settings.archivedMessage")}
              description={t("settings.archivedDescription")}
            />
          )}
        </Form>
      )}
    </Drawer>
  );
}
