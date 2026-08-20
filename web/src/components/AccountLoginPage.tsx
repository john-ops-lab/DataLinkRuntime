/** Minimal username/password login surface for the dedicated account entry. */

import { useState } from "react";
import { Button, Card, Input } from "antd";
import { useTranslation } from "react-i18next";

import { resolveSystemLocale } from "../i18n";
import { userErrorMessage } from "../user-message";

interface AccountLoginPageProps {
  notice: string | null;
  onSubmit: (username: string, password: string) => Promise<void>;
}

export default function AccountLoginPage({ notice, onSubmit }: AccountLoginPageProps) {
  const { i18n, t } = useTranslation("common");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function handleSubmit() {
    if (busy || !username.trim() || !password) {
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await onSubmit(username.trim(), password);
    } catch (err) {
      setError(
        userErrorMessage(
          err,
          t("auth.accountLoginFailed"),
          resolveSystemLocale(i18n.resolvedLanguage ?? i18n.language),
        ),
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="account-login-page">
      <section className="account-auth-card">
        <Card>
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
            <Input
              data-testid="account-username-input"
              aria-label={t("auth.usernameLabel")}
              placeholder={t("auth.usernamePlaceholder")}
              value={username}
              disabled={busy}
              onChange={(event) => setUsername(event.target.value)}
            />
            <Input.Password
              data-testid="account-password-input"
              aria-label={t("auth.passwordLabel")}
              placeholder={t("auth.passwordPlaceholder")}
              value={password}
              disabled={busy}
              onChange={(event) => setPassword(event.target.value)}
              onPressEnter={() => void handleSubmit()}
            />
            <Button
              type="primary"
              block
              data-testid="account-login-submit"
              loading={busy}
              disabled={busy || !username.trim() || !password}
              onClick={() => void handleSubmit()}
            >
              {t("auth.accountLogin")}
            </Button>
            <p className="login-card-subtitle" style={{ margin: 0 }}>
              {t("auth.accountSessionNotice")}
            </p>
          </div>
        </Card>
      </section>
    </main>
  );
}
