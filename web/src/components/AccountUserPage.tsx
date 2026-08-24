import { useEffect, useState } from "react";
import { Button, Input, Typography } from "antd";
import { ProForm } from "@ant-design/pro-components";
import { useTranslation } from "react-i18next";

import { api } from "../api";
import { resolveSystemLocale } from "../i18n";
import type { AccountPrincipal } from "../types";
import { userErrorMessage } from "../user-message";

interface AccountUserPageProps {
  principal: AccountPrincipal;
  onPrincipalChange: (principal: AccountPrincipal) => void;
  onPasswordChanged: () => void;
  onLogout: () => Promise<void>;
}

interface ProfileValues {
  username: string;
}

interface PasswordValues {
  currentPassword: string;
  newPassword: string;
  confirmPassword: string;
}

// Issue #117 Batch 8：账号资料复用系统设置/工作台的 settings-panel 基线。
// 抽屉标题由 AccountApp 承载，本页只保留说明、分区标题、表单和反馈区域。
export default function AccountUserPage({
  principal,
  onPrincipalChange,
  onPasswordChanged,
  onLogout,
}: AccountUserPageProps) {
  const { i18n, t } = useTranslation("common");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [profileForm] = ProForm.useForm<ProfileValues>();
  const [passwordForm] = ProForm.useForm<PasswordValues>();
  const locale = resolveSystemLocale(i18n.resolvedLanguage ?? i18n.language);

  useEffect(() => {
    profileForm.setFieldsValue({ username: principal.username });
  }, [principal.username, profileForm]);

  async function saveProfile(values: ProfileValues): Promise<boolean> {
    const username = values.username.trim();
    if (busy || !username || username === principal.username) {
      return false;
    }
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const updated = await api.updateUser(principal.id, { username });
      onPrincipalChange({
        ...principal,
        username: updated.username,
        role: updated.role,
        enabled: updated.enabled,
        must_change_password: updated.must_change_password,
      });
      profileForm.setFieldsValue({ username: updated.username });
      setNotice(t("users.profileSaved"));
      return true;
    } catch (err) {
      setError(userErrorMessage(err, t("users.profileSaveFailed"), locale));
      return false;
    } finally {
      setBusy(false);
    }
  }

  async function changePassword(values: PasswordValues): Promise<boolean> {
    if (busy) {
      return false;
    }
    if (values.newPassword !== values.confirmPassword) {
      setError(t("users.passwordMismatch"));
      return false;
    }
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      await api.changeAccountPassword({
        current_password: values.currentPassword,
        new_password: values.newPassword,
      });
      passwordForm.resetFields();
      onPasswordChanged();
      return true;
    } catch (err) {
      setError(userErrorMessage(err, t("users.passwordChangeFailed"), locale));
      return false;
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="settings-panel account-user-panel">
      <Typography.Paragraph type="secondary">{t("users.profileSubtitle")}</Typography.Paragraph>
      {error !== null && (
        <p className="settings-panel-error" role="alert" data-testid="account-profile-error">{error}</p>
      )}
      {notice !== null && (
        <p className="settings-panel-success" role="status" data-testid="account-profile-notice">{notice}</p>
      )}
      <ProForm<ProfileValues>
        form={profileForm}
        className="account-user-form"
        layout="vertical"
        initialValues={{ username: principal.username }}
        submitter={{
          render: () => [
            <Button
              key="submit"
              type="primary"
              htmlType="submit"
              data-testid="account-profile-save"
              loading={busy}
              disabled={busy}
            >
              {t("users.saveProfile")}
            </Button>,
          ],
        }}
        onFinish={saveProfile}
      >
        <ProForm.Item
          name="username"
          label={t("users.username")}
          rules={[{ required: true, whitespace: true }]}
        >
          <Input
            data-testid="account-profile-username"
            disabled={busy}
            autoComplete="username"
          />
        </ProForm.Item>
      </ProForm>
      <section className="account-user-password" aria-labelledby="account-user-password-title">
        <h3 id="account-user-password-title" className="settings-section-title">{t("users.passwordTitle")}</h3>
        <ProForm<PasswordValues>
          form={passwordForm}
          className="account-user-form"
          layout="vertical"
          submitter={{
            render: () => [
              <Button
                key="submit"
                htmlType="submit"
                data-testid="account-user-password-submit"
                loading={busy}
                disabled={busy}
              >
                {t("users.changePassword")}
              </Button>,
            ],
          }}
          onFinish={changePassword}
        >
          <ProForm.Item
            name="currentPassword"
            label={t("users.currentPassword")}
            rules={[{ required: true }]}
          >
            <Input.Password
              data-testid="account-user-current-password"
              disabled={busy}
              autoComplete="current-password"
            />
          </ProForm.Item>
          <ProForm.Item
            name="newPassword"
            label={t("users.newPassword")}
            rules={[{ required: true }]}
          >
            <Input.Password
              data-testid="account-user-new-password"
              disabled={busy}
              autoComplete="new-password"
            />
          </ProForm.Item>
          <ProForm.Item
            name="confirmPassword"
            label={t("users.confirmPassword")}
            rules={[{ required: true }]}
          >
            <Input.Password
              data-testid="account-user-confirm-password"
              disabled={busy}
              autoComplete="new-password"
            />
          </ProForm.Item>
        </ProForm>
      </section>
      <div className="account-user-footer">
        <Button data-testid="account-user-logout" onClick={() => void onLogout()}>
          {t("auth.logout")}
        </Button>
      </div>
    </div>
  );
}
