/** Adapter settings, sharing and lifecycle actions (M5.11 Wave C). */

import { forwardRef, useImperativeHandle, useRef, useState } from "react";
import {
  Alert,
  Button,
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
import type { PageLeaveGuardHandle } from "../page-leave-guard";
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

const AdapterSettingsDrawerContent = forwardRef<PageLeaveGuardHandle, Props>(function AdapterSettingsDrawerContent(props, ref) {
  const { t } = useTranslation(["adapter", "common"]);
  const [form] = Form.useForm<SettingsValues>();
  const adapter = props.adapter;
  const [view, setView] = useState<"settings" | "permissions">("settings");
  const [formDirty, setFormDirty] = useState(false);
  const [formValid, setFormValid] = useState(true);
  const permissionMutationCount = useRef(0);
  const archived = !!adapter?.archived_at;
  const accessLevel = adapter === null ? "read" : props.accessLevel ?? "admin";
  const canEdit = canEditAdapter(accessLevel);
  const canManage = canManageAdapter(accessLevel);

  function confirmLeave(): boolean {
    if (!props.open) {
      return true;
    }
    if (props.busy || permissionMutationCount.current > 0) {
      return false;
    }
    return !formDirty || window.confirm(t("settings.unsavedClose"));
  }

  function requestClose() {
    if (confirmLeave()) {
      props.onClose();
    }
  }

  useImperativeHandle(ref, () => ({ confirmLeave }));

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

  const title = view === "permissions" ? t("sharing.title") : t("settings.title");

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
          {t("settings.saveChanges")}
        </Button>
      </div>
    ) : null;

  return (
    <Drawer
      title={
        <div className="adapter-settings-title" data-testid="adapter-settings-title">
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
          onMutationStart={() => {
            permissionMutationCount.current += 1;
          }}
          onMutationEnd={() => {
            permissionMutationCount.current = Math.max(0, permissionMutationCount.current - 1);
          }}
        />
      )}

      {adapter !== null && (
        <Form
          id="adapter-settings-form"
          data-testid="adapter-settings-form"
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
          <div className="adapter-settings-summary" data-testid="adapter-settings-summary">
            <div className="adapter-settings-summary-main">
              <Typography.Text strong>{props.name || adapter.name}</Typography.Text>
              <div className="adapter-settings-summary-tags">
                <Tag>{t(`types.${adapter.adapter_type}`)}</Tag>
                <Tag>{LANGUAGE_LABELS[adapter.language]}</Tag>
                {adapter.runtime_locked === true && <Tag color="processing">{t("settings.active")}</Tag>}
              </div>
            </div>
            <Typography.Text type="secondary">{t("settings.summary")}</Typography.Text>
          </div>

          <section className="adapter-settings-section" data-testid="adapter-settings-section-basic">
            <h3 className="adapter-settings-section-title">{t("settings.basicInfo")}</h3>
            <Form.Item
              name="name"
              label={t("settings.name")}
              rules={[{ required: true, whitespace: true, message: t("settings.nameRequired") }]}
            >
              <Input data-testid="adapter-name" disabled={props.busy || archived || !canEdit} />
            </Form.Item>
            <Form.Item label={t("settings.language")}>
              <div
                className="adapter-readonly-field"
                data-testid="adapter-language"
                aria-label={`${t("settings.language")}: ${LANGUAGE_LABELS[adapter.language]}`}
                aria-readonly="true"
                role="status"
              >
                <Tag>{LANGUAGE_LABELS[adapter.language]}</Tag>
                <Typography.Text type="secondary">{t("settings.readOnly")}</Typography.Text>
              </div>
            </Form.Item>
            <Form.Item name="description" label={t("settings.description")}>
              <Input.TextArea
                data-testid="adapter-description"
                rows={4}
                autoSize={{ minRows: 3, maxRows: 4 }}
                disabled={props.busy || archived || !canEdit}
              />
            </Form.Item>
          </section>

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
            <section className="adapter-settings-section" data-testid="adapter-settings-section-permissions">
              <h3 className="adapter-settings-section-title">{t("settings.permissionsSection")}</h3>
              <Typography.Text type="secondary" className="adapter-settings-section-summary">
                {t("settings.accessSummary", {
                  level: t(
                    accessLevel === "owner"
                      ? "access.mine"
                      : accessLevel === "edit"
                        ? "access.sharedEdit"
                        : accessLevel === "read"
                          ? "access.sharedRead"
                          : "access.adminAll",
                  ),
                })}
              </Typography.Text>
              <Button
                icon={<TeamOutlined />}
                data-testid="open-adapter-permissions"
                aria-label={t("sharing.open")}
                onClick={() => setView("permissions")}
              >
                {t("sharing.open")}
              </Button>
            </section>
          )}

          {!archived && (
            <>
              <section className="adapter-settings-section" data-testid="adapter-settings-section-more">
                <h3 className="adapter-settings-section-title">{t("settings.moreActions")}</h3>
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
              </section>

              {canManage && (
                <section className="adapter-settings-section adapter-danger-zone" data-testid="adapter-danger-zone">
                  <h3 className="adapter-settings-section-title">{t("settings.dangerZone")}</h3>
                  <div className="adapter-danger-zone-copy">
                    <Typography.Text>{t("settings.deleteDescription")}</Typography.Text>
                    <Typography.Paragraph type="secondary">
                      {adapter.runtime_locked === true
                        ? t("settings.deleteWarningActive")
                        : t("settings.deleteWarningIdle")}
                    </Typography.Paragraph>
                  </div>
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
                </section>
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
});

const AdapterSettingsDrawer = forwardRef<PageLeaveGuardHandle, Props>(function AdapterSettingsDrawer(props, ref) {
  const adapterKey = props.adapter?.id ?? "none";
  const formKey = `${props.open ? "open" : "closed"}:${adapterKey}:${props.name}:${props.description}`;
  return <AdapterSettingsDrawerContent ref={ref} key={formKey} {...props} />;
});

export default AdapterSettingsDrawer;
