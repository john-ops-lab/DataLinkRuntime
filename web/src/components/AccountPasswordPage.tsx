/** Forced password-change surface shown before an account can use the app. */

import { useState } from "react";
import { Button, Card, Input } from "antd";
import { ProForm } from "@ant-design/pro-components";
import { useTranslation } from "react-i18next";

import { useLoginLocale } from "../login-locale";
import { userErrorMessage } from "../user-message";
import LoginShell from "./LoginShell";

interface AccountPasswordPageProps {
  username: string;
  onSubmit: (currentPassword: string, newPassword: string) => Promise<void>;
  onLogout: () => Promise<void>;
}

interface PasswordValues {
  currentPassword: string;
  newPassword: string;
  confirmPassword: string;
}

export default function AccountPasswordPage({
  username,
  onSubmit,
  onLogout,
}: AccountPasswordPageProps) {
  const [locale] = useLoginLocale(false);
  const { i18n } = useTranslation("common");
  const t = i18n.getFixedT(locale, "common");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function handleSubmit(values: PasswordValues): Promise<boolean> {
    if (busy) {
      return false;
    }
    if (values.newPassword !== values.confirmPassword) {
      setError(t("auth.passwordMismatch"));
      return false;
    }
    setBusy(true);
    setError(null);
    try {
      await onSubmit(values.currentPassword, values.newPassword);
      return true;
    } catch (err) {
      setError(
        userErrorMessage(
          err,
          t("auth.passwordChangeFailed"),
          locale,
        ),
      );
      return false;
    } finally {
      setBusy(false);
    }
  }

  return (
    <LoginShell testId="account-password-page" loginSurface={false}>
      <Card className="auth-card">
        <div className="login-card-inner">
          <h1 className="login-card-title">{t("auth.forcePasswordTitle")}</h1>
          <p className="login-card-subtitle">
            {t("auth.forcePasswordSubtitle", { username })}
          </p>
          {error && (
            <p className="error-banner" role="alert" data-testid="account-password-error">
              {error}
            </p>
          )}
          <ProForm<PasswordValues>
            className="account-auth-form"
            layout="vertical"
            submitter={{
              render: () => [
                <Button
                  key="submit"
                  type="primary"
                  block
                  htmlType="submit"
                  data-testid="account-password-submit"
                  loading={busy}
                  disabled={busy}
                >
                  {t("auth.changePassword")}
                </Button>,
              ],
            }}
            onFinish={handleSubmit}
          >
            <ProForm.Item
              name="currentPassword"
              label={t("auth.currentPasswordLabel")}
              rules={[{ required: true }]}
            >
              <Input.Password
                data-testid="account-current-password-input"
                aria-label={t("auth.currentPasswordLabel")}
                placeholder={t("auth.currentPasswordPlaceholder")}
                autoComplete="current-password"
                disabled={busy}
              />
            </ProForm.Item>
            <ProForm.Item
              name="newPassword"
              label={t("auth.newPasswordLabel")}
              rules={[{ required: true }]}
            >
              <Input.Password
                data-testid="account-new-password-input"
                aria-label={t("auth.newPasswordLabel")}
                placeholder={t("auth.newPasswordPlaceholder")}
                autoComplete="new-password"
                disabled={busy}
              />
            </ProForm.Item>
            <ProForm.Item
              name="confirmPassword"
              label={t("auth.confirmPasswordLabel")}
              rules={[{ required: true }]}
            >
              <Input.Password
                data-testid="account-confirm-password-input"
                aria-label={t("auth.confirmPasswordLabel")}
                placeholder={t("auth.confirmPasswordPlaceholder")}
                autoComplete="new-password"
                disabled={busy}
              />
            </ProForm.Item>
          </ProForm>
          <Button
            block
            data-testid="account-password-logout"
            disabled={busy}
            onClick={() => void onLogout()}
          >
            {t("auth.logout")}
          </Button>
        </div>
      </Card>
    </LoginShell>
  );
}
