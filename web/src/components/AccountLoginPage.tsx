/** Minimal username/password login surface for the dedicated account entry. */

import { useState } from "react";
import { Button, Card, Input } from "antd";
import { ProForm } from "@ant-design/pro-components";
import { useTranslation } from "react-i18next";

import { useLoginLocale } from "../login-locale";
import { userErrorMessage } from "../user-message";
import LoginShell from "./LoginShell";

interface AccountLoginPageProps {
  notice: string | null;
  onSubmit: (username: string, password: string) => Promise<void>;
}

interface LoginValues {
  username: string;
  password: string;
}

export default function AccountLoginPage({ notice, onSubmit }: AccountLoginPageProps) {
  const [locale] = useLoginLocale();
  const { i18n } = useTranslation("common");
  const t = i18n.getFixedT(locale, "common");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function handleSubmit(values: LoginValues): Promise<boolean> {
    if (busy) {
      return false;
    }
    setBusy(true);
    setError(null);
    try {
      await onSubmit(values.username.trim(), values.password);
      return true;
    } catch (err) {
      setError(
        userErrorMessage(
          err,
          t("auth.accountLoginFailed"),
          locale,
        ),
      );
      return false;
    } finally {
      setBusy(false);
    }
  }

  return (
    <LoginShell testId="account-login-page">
      <Card className="auth-card">
        <div className="login-card-inner">
          <h1 className="login-card-title">{t("auth.accountLoginTitle")}</h1>
          <p className="login-card-subtitle">{t("auth.accountLoginSubtitle")}</p>
          {notice && (
            <p className="login-notice" data-testid="account-auth-notice">
              {notice}
            </p>
          )}
          {error && (
            <p className="error-banner" role="alert" data-testid="account-login-error">
              {error}
            </p>
          )}
          <ProForm<LoginValues>
            className="account-auth-form"
            layout="vertical"
            submitter={{
              render: () => [
                <Button
                  key="submit"
                  type="primary"
                  block
                  htmlType="submit"
                  data-testid="account-login-submit"
                  loading={busy}
                  disabled={busy}
                >
                  {t("auth.accountLogin")}
                </Button>,
              ],
            }}
            onFinish={handleSubmit}
          >
            <ProForm.Item
              name="username"
              label={t("auth.usernameLabel")}
              rules={[{ required: true, whitespace: true }]}
            >
              <Input
                data-testid="account-username-input"
                aria-label={t("auth.usernameLabel")}
                placeholder={t("auth.usernamePlaceholder")}
                autoComplete="username"
                disabled={busy}
              />
            </ProForm.Item>
            <ProForm.Item
              name="password"
              label={t("auth.passwordLabel")}
              rules={[{ required: true }]}
            >
              <Input.Password
                data-testid="account-password-input"
                aria-label={t("auth.passwordLabel")}
                placeholder={t("auth.passwordPlaceholder")}
                autoComplete="current-password"
                disabled={busy}
              />
            </ProForm.Item>
          </ProForm>
        </div>
      </Card>
    </LoginShell>
  );
}
