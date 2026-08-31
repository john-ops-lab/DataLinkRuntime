/** 登录页：品牌区 + Token 登录卡片（M3.1 §6，认证合同仍完全沿用 M2）。 */

import { useState } from "react";
import { Button, Card, Input } from "antd";
import { ProForm } from "@ant-design/pro-components";
import { useTranslation } from "react-i18next";

import { useLoginLocale } from "../login-locale";
import { userErrorMessage } from "../user-message";
import LoginShell from "./LoginShell";

interface LoginPageProps {
  notice: string | null;
  onSubmit: (token: string) => Promise<void>;
}

interface LoginValues {
  token: string;
}

export default function LoginPage(props: LoginPageProps) {
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
      await props.onSubmit(values.token.trim());
      return true;
    } catch (err) {
      setError(
        userErrorMessage(
          err,
          t("auth.loginFailed"),
          locale,
        ),
      );
      return false;
    } finally {
      setBusy(false);
    }
  }

  return (
    <LoginShell testId="token-login-page">
      <Card className="auth-card">
        <div className="login-card-inner">
          <h1 className="login-card-title">{t("auth.loginTitle")}</h1>
          <p className="login-card-subtitle">{t("auth.loginSubtitle")}</p>
          {props.notice && (
            <p className="login-notice" data-testid="auth-notice">
              {props.notice}
            </p>
          )}
          {error && (
            <p className="error-banner" role="alert" data-testid="login-error">
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
                  data-testid="admin-token-submit"
                  loading={busy}
                  disabled={busy}
                >
                  {t("auth.login")}
                </Button>,
              ],
            }}
            onFinish={handleSubmit}
          >
            <ProForm.Item
              name="token"
              label={t("auth.tokenLabel")}
              rules={[{ required: true, whitespace: true }]}
            >
              <Input.Password
                data-testid="admin-token-input"
                aria-label={t("auth.tokenLabel")}
                placeholder={t("auth.tokenPlaceholder")}
                autoComplete="current-password"
                disabled={busy}
              />
            </ProForm.Item>
          </ProForm>
          <p className="login-card-subtitle" style={{ margin: 0 }}>
            {t("auth.tokenStorageNotice")}
          </p>
        </div>
      </Card>
    </LoginShell>
  );
}
