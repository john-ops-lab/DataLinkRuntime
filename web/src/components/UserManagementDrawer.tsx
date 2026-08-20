import { useCallback, useEffect, useState } from "react";
import { Button, Drawer, Input, Select, Space, Table, Tag, Typography } from "antd";
import type { ColumnsType } from "antd/es/table";
import { useTranslation } from "react-i18next";

import { api } from "../api";
import { resolveSystemLocale } from "../i18n";
import type { AccountRole, AccountUser } from "../types";
import { userErrorMessage } from "../user-message";

interface UserManagementDrawerProps {
  open: boolean;
  onClose: () => void;
}

interface CreateForm {
  username: string;
  password: string;
  role: AccountRole;
}

function emptyCreateForm(): CreateForm {
  return { username: "", password: "", role: "user" };
}

export default function UserManagementDrawer({
  open,
  onClose,
}: UserManagementDrawerProps) {
  const { i18n, t } = useTranslation("common");
  const [users, setUsers] = useState<AccountUser[]>([]);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [form, setForm] = useState<CreateForm>(emptyCreateForm);
  const [resetTarget, setResetTarget] = useState<AccountUser | null>(null);
  const [resetPassword, setResetPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const locale = resolveSystemLocale(i18n.resolvedLanguage ?? i18n.language);

  const loadUsers = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setUsers(await api.listUsers());
    } catch (err) {
      setError(userErrorMessage(err, t("users.loadFailed"), locale));
    } finally {
      setLoading(false);
    }
  }, [locale, t]);

  useEffect(() => {
    if (!open) {
      return;
    }
    const task = window.setTimeout(() => void loadUsers(), 0);
    return () => window.clearTimeout(task);
  }, [loadUsers, open]);

  async function handleCreate() {
    if (busy || !form.username.trim() || !form.password) {
      return;
    }
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const created = await api.createUser({
        username: form.username.trim(),
        password: form.password,
        role: form.role,
      });
      setUsers((current) => [...current, created].sort((a, b) => a.username.localeCompare(b.username)));
      setForm(emptyCreateForm());
      setNotice(t("users.created"));
    } catch (err) {
      setError(userErrorMessage(err, t("users.createFailed"), locale));
    } finally {
      setBusy(false);
    }
  }

  async function handleEnabledChange(user: AccountUser) {
    const nextEnabled = !user.enabled;
    if (
      busy ||
      !window.confirm(
        nextEnabled
          ? t("users.confirmEnable", { username: user.username })
          : t("users.confirmDisable", { username: user.username }),
      )
    ) {
      return;
    }
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const updated = await api.updateUser(user.id, { enabled: nextEnabled });
      setUsers((current) => current.map((item) => (item.id === updated.id ? updated : item)));
      setNotice(nextEnabled ? t("users.enabled") : t("users.disabled"));
    } catch (err) {
      setError(userErrorMessage(err, t("users.updateFailed"), locale));
    } finally {
      setBusy(false);
    }
  }

  async function handleRoleChange(user: AccountUser, nextRole: AccountRole) {
    if (
      busy ||
      nextRole === user.role ||
      !window.confirm(t("users.confirmRole", { username: user.username, role: t(`users.role.${nextRole}`) }))
    ) {
      return;
    }
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const updated = await api.updateUser(user.id, { role: nextRole });
      setUsers((current) => current.map((item) => (item.id === updated.id ? updated : item)));
      setNotice(t("users.roleUpdated"));
    } catch (err) {
      setError(userErrorMessage(err, t("users.updateFailed"), locale));
    } finally {
      setBusy(false);
    }
  }

  async function handleResetPassword() {
    if (busy || resetTarget === null || !resetPassword) {
      return;
    }
    if (!window.confirm(t("users.confirmReset", { username: resetTarget.username }))) {
      return;
    }
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const updated = await api.resetUserPassword(resetTarget.id, resetPassword);
      setUsers((current) => current.map((item) => (item.id === updated.id ? updated : item)));
      setResetTarget(null);
      setResetPassword("");
      setNotice(t("users.resetNotice"));
    } catch (err) {
      setError(userErrorMessage(err, t("users.resetFailed"), locale));
    } finally {
      setBusy(false);
    }
  }

  const columns: ColumnsType<AccountUser> = [
    {
      title: t("users.username"),
      dataIndex: "username",
      key: "username",
      render: (username: string) => <Typography.Text>{username}</Typography.Text>,
    },
    {
      title: t("users.roleTitle"),
      key: "role",
      render: (_, user) => (
        <Select<AccountRole>
          aria-label={t("users.roleTitle")}
          value={user.role}
          disabled={busy}
          options={[
            { value: "admin", label: t("users.role.admin") },
            { value: "user", label: t("users.role.user") },
          ]}
          onChange={(value) => void handleRoleChange(user, value)}
        />
      ),
    },
    {
      title: t("users.status"),
      key: "enabled",
      render: (_, user) => (
        <Tag color={user.enabled ? "green" : "default"}>
          {user.enabled ? t("users.enabledLabel") : t("users.disabledLabel")}
        </Tag>
      ),
    },
    {
      title: t("users.actions"),
      key: "actions",
      render: (_, user) => (
        <Space wrap>
          <Button
            size="small"
            danger={user.enabled}
            disabled={busy}
            data-testid={`user-toggle-${user.id}`}
            onClick={() => void handleEnabledChange(user)}
          >
            {user.enabled ? t("users.disable") : t("users.enable")}
          </Button>
          <Button
            size="small"
            disabled={busy}
            data-testid={`user-reset-${user.id}`}
            onClick={() => {
              setResetTarget(user);
              setResetPassword("");
              setError(null);
            }}
          >
            {t("users.resetPassword")}
          </Button>
        </Space>
      ),
    },
  ];

  return (
    <Drawer
      title={t("users.title")}
      width={820}
      open={open}
      destroyOnHidden
      onClose={onClose}
      data-testid="user-management-drawer"
    >
      <div className="settings-panel user-management-panel">
        <Typography.Paragraph type="secondary">{t("users.subtitle")}</Typography.Paragraph>
        {error !== null && <p className="settings-panel-error" role="alert">{error}</p>}
        {notice !== null && <p className="settings-panel-success" role="status">{notice}</p>}
        <section className="user-create-section" aria-labelledby="user-create-title">
          <h3 id="user-create-title" className="settings-section-title">{t("users.createTitle")}</h3>
          <div className="settings-inline-form">
            <Input
              aria-label={t("users.username")}
              data-testid="user-create-username"
              placeholder={t("users.username")}
              value={form.username}
              disabled={busy}
              onChange={(event) => setForm((current) => ({ ...current, username: event.target.value }))}
            />
            <Input.Password
              aria-label={t("users.password")}
              data-testid="user-create-password"
              placeholder={t("users.password")}
              value={form.password}
              disabled={busy}
              onChange={(event) => setForm((current) => ({ ...current, password: event.target.value }))}
            />
            <Select<AccountRole>
              aria-label={t("users.roleTitle")}
              data-testid="user-create-role"
              value={form.role}
              disabled={busy}
              options={[
                { value: "admin", label: t("users.role.admin") },
                { value: "user", label: t("users.role.user") },
              ]}
              onChange={(role) => setForm((current) => ({ ...current, role }))}
            />
            <Button
              type="primary"
              data-testid="user-create-submit"
              loading={busy}
              disabled={!form.username.trim() || !form.password}
              onClick={() => void handleCreate()}
            >
              {t("users.create")}
            </Button>
          </div>
        </section>
        <div className="settings-panel-toolbar">
          <Button size="small" loading={loading} onClick={() => void loadUsers()}>
            {t("users.refresh")}
          </Button>
        </div>
        <Table<AccountUser>
          rowKey="id"
          loading={loading}
          columns={columns}
          dataSource={users}
          pagination={false}
          locale={{ emptyText: t("users.empty") }}
        />
      </div>
      <div className="user-reset-dialog">
        {resetTarget !== null && (
          <div role="dialog" aria-modal="true" className="user-reset-dialog-inner">
            <h3 className="settings-section-title">{t("users.resetTitle", { username: resetTarget.username })}</h3>
            <Input.Password
              aria-label={t("users.newPassword")}
              data-testid="user-reset-password"
              placeholder={t("users.newPassword")}
              value={resetPassword}
              disabled={busy}
              onChange={(event) => setResetPassword(event.target.value)}
            />
            <Space>
              <Button disabled={busy} onClick={() => setResetTarget(null)}>{t("actions.cancel")}</Button>
              <Button
                type="primary"
                loading={busy}
                disabled={!resetPassword}
                data-testid="user-reset-submit"
                onClick={() => void handleResetPassword()}
              >
                {t("users.resetPassword")}
              </Button>
            </Space>
          </div>
        )}
      </div>
    </Drawer>
  );
}
