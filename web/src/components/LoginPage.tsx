/** 登录页：品牌区 + Token 登录卡片（M3.1 §6，认证合同仍完全沿用 M2）。 */

import { useState } from "react";
import { Button, Card, Input } from "antd";
import { ProForm } from "@ant-design/pro-components";
import { useTranslation } from "react-i18next";

import { resolveSystemLocale } from "../i18n";
import { userErrorMessage } from "../user-message";

interface LoginPageProps {
  notice: string | null;
  onSubmit: (token: string) => Promise<void>;
}

interface LoginValues {
  token: string;
}

export default function LoginPage(props: LoginPageProps) {
  const { i18n, t } = useTranslation("common");
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
          resolveSystemLocale(i18n.resolvedLanguage ?? i18n.language),
        ),
      );
      return false;
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="login-page">
      <div className="login-layout">
        <section className="login-brand">
          <div>
            <h1 className="login-brand-logo">DLR</h1>
            <div className="login-brand-name">{t("product.name")}</div>
          </div>
          {/* M5.8-011：品牌区收敛为已确认的产品文案，不再堆叠功能列表。 */}
          <p className="login-brand-tagline">{t("product.headline")}</p>
          <p className="login-brand-sub">{t("product.intro1")}</p>
          <p className="login-brand-sub login-brand-intro">{t("product.intro2")}</p>
          <p className="login-brand-flow">{t("product.flow")}</p>
          <p className="login-copyright">{t("product.copyright")}</p>
        </section>

        <section className="login-side">
          <Card className="login-card">
            <div className="login-card-inner">
              <h2 className="login-card-title">{t("auth.loginTitle")}</h2>
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
        </section>
      </div>
    </main>
  );
}
