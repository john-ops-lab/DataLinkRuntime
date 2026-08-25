import { useCallback, useEffect, useMemo, useState } from "react";
import { Button, Drawer, Form, Input, Select, Space, Tag, Typography } from "antd";
import {
  ModalForm,
  ProForm,
  ProTable,
} from "@ant-design/pro-components";
import type { ProColumns } from "@ant-design/pro-components";
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

interface UserFilters {
  keyword: string;
  role: "all" | AccountRole;
  status: "all" | "enabled" | "disabled";
}

interface ResetForm {
  password: string;
}

const EMPTY_FILTERS: UserFilters = {
  keyword: "",
  role: "all",
  status: "all",
};

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
  const [selectedUserIds, setSelectedUserIds] = useState<number[]>([]);
  const [filters, setFilters] = useState<UserFilters>(EMPTY_FILTERS);
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

  async function handleCreate(values: CreateForm): Promise<boolean> {
    if (busy) {
      return false;
    }
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const created = await api.createUser({
        username: values.username.trim(),
        password: values.password,
        role: values.role,
      });
      setUsers((current) =>
        [...current, created].sort((a, b) => a.username.localeCompare(b.username)),
      );
      setForm(emptyCreateForm());
      setNotice(t("users.created"));
    } catch (err) {
      setError(userErrorMessage(err, t("users.createFailed"), locale));
    } finally {
      setBusy(false);
    }
    return false;
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
      !window.confirm(
        t("users.confirmRole", {
          username: user.username,
          role: t(`users.role.${nextRole}`),
        }),
      )
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

  async function handleBulkEnabledChange(enabled: boolean) {
    if (busy || selectedUserIds.length === 0) {
      return;
    }
    const targets = users.filter((user) => selectedUserIds.includes(user.id));
    const changedTargets = targets.filter((user) => user.enabled !== enabled);
    if (changedTargets.length === 0) {
      setSelectedUserIds([]);
      return;
    }
    if (
      !window.confirm(
        enabled
          ? t("users.confirmBulkEnable", { count: changedTargets.length })
          : t("users.confirmBulkDisable", { count: changedTargets.length }),
      )
    ) {
      return;
    }
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      // The current API exposes one-user PATCH only. Keep the bulk action as a
      // small client-side batch of those established calls; do not invent a
      // new backend route or weaken the existing role/CSRF contract.
      for (const user of changedTargets) {
        const updated = await api.updateUser(user.id, { enabled });
        setUsers((current) => current.map((item) => (item.id === updated.id ? updated : item)));
      }
      setSelectedUserIds([]);
      setNotice(
        t(enabled ? "users.bulkEnabled" : "users.bulkDisabled", {
          count: changedTargets.length,
        }),
      );
    } catch (err) {
      setError(userErrorMessage(err, t("users.updateFailed"), locale));
    } finally {
      setBusy(false);
    }
  }

  async function handleResetPassword(values: ResetForm): Promise<boolean> {
    if (busy || resetTarget === null || !values.password) {
      return false;
    }
    if (!window.confirm(t("users.confirmReset", { username: resetTarget.username }))) {
      return false;
    }
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const updated = await api.resetUserPassword(resetTarget.id, values.password);
      setUsers((current) => current.map((item) => (item.id === updated.id ? updated : item)));
      setResetTarget(null);
      setResetPassword("");
      setNotice(t("users.resetNotice"));
      return true;
    } catch (err) {
      setError(userErrorMessage(err, t("users.resetFailed"), locale));
      return false;
    } finally {
      setBusy(false);
    }
  }

  const filteredUsers = useMemo(() => {
    const keyword = filters.keyword.trim().toLowerCase();
    return users.filter((user) => {
      if (keyword !== "" && !user.username.toLowerCase().includes(keyword)) {
        return false;
      }
      if (filters.role !== "all" && user.role !== filters.role) {
        return false;
      }
      if (filters.status === "enabled" && !user.enabled) {
        return false;
      }
      if (filters.status === "disabled" && user.enabled) {
        return false;
      }
      return true;
    });
  }, [filters, users]);

  const columns: ProColumns<AccountUser>[] = [
    {
      title: t("users.username"),
      dataIndex: "username",
      key: "username",
      render: (username) => <Typography.Text>{username}</Typography.Text>,
    },
    {
      title: t("users.roleTitle"),
      key: "role",
      render: (_: unknown, user: AccountUser) => (
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
      render: (_: unknown, user: AccountUser) => (
        <Tag color={user.enabled ? "green" : "default"}>
          {user.enabled ? t("users.enabledLabel") : t("users.disabledLabel")}
        </Tag>
      ),
    },
    {
      title: t("users.actions"),
      key: "actions",
      render: (_: unknown, user: AccountUser) => (
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
          <ProForm<CreateForm>
            key={`${open}-${notice === null}`}
            className="wave-c-form"
            layout="vertical"
            initialValues={form}
            submitter={{
              render: () => [
                <Button
                  key="submit"
                  type="primary"
                  htmlType="submit"
                  data-testid="user-create-submit"
                  loading={busy}
                  disabled={busy}
                >
                  {t("users.create")}
                </Button>,
              ],
            }}
            onValuesChange={(_, values) => setForm(values)}
            onFinish={handleCreate}
          >
            <ProForm.Item
              name="username"
              label={t("users.username")}
              rules={[{ required: true, whitespace: true }]}
            >
              <Input
                aria-label={t("users.username")}
                data-testid="user-create-username"
                placeholder={t("users.username")}
                disabled={busy}
              />
            </ProForm.Item>
            <ProForm.Item
              name="password"
              label={t("users.password")}
              rules={[{ required: true }]}
            >
              <Input.Password
                aria-label={t("users.password")}
                data-testid="user-create-password"
                placeholder={t("users.password")}
                autoComplete="new-password"
                disabled={busy}
              />
            </ProForm.Item>
            <ProForm.Item name="role" label={t("users.roleTitle")}>
              <Select<AccountRole>
                aria-label={t("users.roleTitle")}
                data-testid="user-create-role"
                disabled={busy}
                options={[
                  { value: "admin", label: t("users.role.admin") },
                  { value: "user", label: t("users.role.user") },
                ]}
              />
            </ProForm.Item>
          </ProForm>
        </section>
        <Form<UserFilters>
          className="wave-c-query-filter user-filter-form"
          data-testid="user-filter-form"
          layout="vertical"
          initialValues={EMPTY_FILTERS}
          onValuesChange={(_, values) =>
            setFilters({
              keyword: values.keyword ?? "",
              role: values.role ?? "all",
              status: values.status ?? "all",
            })
          }
        >
          <Form.Item className="user-filter-keyword" name="keyword" label={t("users.filterKeyword")}>
            <Input allowClear aria-label={t("users.filterKeyword")} />
          </Form.Item>
          <Form.Item className="user-filter-role" name="role" label={t("users.filterRole")}>
            <Select<"all" | AccountRole>
              aria-label={t("users.filterRole")}
              options={[
                { value: "all", label: t("users.filterAll") },
                { value: "admin", label: t("users.role.admin") },
                { value: "user", label: t("users.role.user") },
              ]}
            />
          </Form.Item>
          <Form.Item className="user-filter-status" name="status" label={t("users.filterStatus")}>
            <Select<"all" | "enabled" | "disabled">
              aria-label={t("users.filterStatus")}
              options={[
                { value: "all", label: t("users.filterAll") },
                { value: "enabled", label: t("users.enabledLabel") },
                { value: "disabled", label: t("users.disabledLabel") },
              ]}
            />
          </Form.Item>
        </Form>
        <div
          className="settings-panel-toolbar user-management-toolbar"
          data-testid="user-management-toolbar"
        >
          <Button size="small" loading={loading} onClick={() => void loadUsers()}>
            {t("users.refresh")}
          </Button>
          <Space className="user-bulk-actions" wrap>
            <Typography.Text type="secondary">
              {t("users.bulkSelected", { count: selectedUserIds.length })}
            </Typography.Text>
            <Button
              size="small"
              data-testid="users-bulk-enable"
              disabled={busy || selectedUserIds.length === 0}
              onClick={() => void handleBulkEnabledChange(true)}
            >
              {t("users.bulkEnable")}
            </Button>
            <Button
              size="small"
              danger
              data-testid="users-bulk-disable"
              disabled={busy || selectedUserIds.length === 0}
              onClick={() => void handleBulkEnabledChange(false)}
            >
              {t("users.bulkDisable")}
            </Button>
          </Space>
        </div>
        <ProTable<AccountUser>
          rowKey="id"
          loading={loading}
          columns={columns}
          dataSource={filteredUsers}
          search={false}
          options={false}
          rowSelection={{
            selectedRowKeys: selectedUserIds,
            onChange: (keys) => setSelectedUserIds(keys.map((key) => Number(key))),
          }}
          pagination={{ pageSize: 8, showSizeChanger: true }}
          locale={{ emptyText: t("users.empty") }}
          scroll={{ x: 620 }}
        />
      </div>
      <ModalForm<ResetForm>
        title={resetTarget === null ? t("users.resetPassword") : t("users.resetTitle", { username: resetTarget.username })}
        open={resetTarget !== null}
        modalProps={{ destroyOnHidden: true, onCancel: () => setResetTarget(null) }}
        submitter={{
          render: (submitterProps) => [
            <Button key="cancel" onClick={() => setResetTarget(null)} disabled={busy}>
              {t("actions.cancel")}
            </Button>,
            <Button
              key="submit"
              type="primary"
              data-testid="user-reset-submit"
              loading={busy}
              disabled={busy}
              onClick={submitterProps.submit}
            >
              {t("users.resetPassword")}
            </Button>,
          ],
        }}
        onFinish={handleResetPassword}
      >
        <ProForm.Item
          name="password"
          label={t("users.newPassword")}
          rules={[{ required: true }]}
        >
          <Input.Password
            aria-label={t("users.newPassword")}
            data-testid="user-reset-password"
            placeholder={t("users.newPassword")}
            autoComplete="new-password"
            disabled={busy}
            value={resetPassword}
            onChange={(event) => setResetPassword(event.target.value)}
          />
        </ProForm.Item>
      </ModalForm>
    </Drawer>
  );
}
