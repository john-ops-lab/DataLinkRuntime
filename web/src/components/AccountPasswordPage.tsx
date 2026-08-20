/** Forced password-change surface shown before an account can use the app. */

import { useState } from "react";
import { Button, Card, Input } from "antd";
import { useTranslation } from "react-i18next";

import { resolveSystemLocale } from "../i18n";
import { userErrorMessage } from "../user-message";

interface AccountPasswordPageProps {
  username: string;
  onSubmit: (currentPassword: string, newPassword: string) => Promise<void>;
  onLogout: () => Promise<void>;
}

export default function AccountPasswordPage({
  username,
  onSubmit,
  onLogout,
}: AccountPasswordPageProps) {
  const { i18n, t } = useTranslation("common");
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function handleSubmit() {
    if (busy || !currentPassword || !newPassword || newPassword !== confirmPassword) {
      if (newPassword !== confirmPassword) {
        setError(t("auth.passwordMismatch"));
      }
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await onSubmit(currentPassword, newPassword);
    } catch (err) {
      setError(
        userErrorMessage(
          err,
          t("auth.passwordChangeFailed"),
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
            <h1 className="login-card-title">{t("auth.forcePasswordTitle")}</h1>
            <p className="login-card-subtitle">
              {t("auth.forcePasswordSubtitle", { username })}
            </p>
            {error && (
              <p className="error-banner" role="alert" data-testid="account-password-error">
                {error}
              </p>
            )}
            <Input.Password
              data-testid="account-current-password-input"
              aria-label={t("auth.currentPasswordLabel")}
              placeholder={t("auth.currentPasswordPlaceholder")}
              value={currentPassword}
              disabled={busy}
              onChange={(event) => setCurrentPassword(event.target.value)}
            />
            <Input.Password
              data-testid="account-new-password-input"
              aria-label={t("auth.newPasswordLabel")}
              placeholder={t("auth.newPasswordPlaceholder")}
              value={newPassword}
              disabled={busy}
              onChange={(event) => setNewPassword(event.target.value)}
            />
            <Input.Password
              data-testid="account-confirm-password-input"
              aria-label={t("auth.confirmPasswordLabel")}
              placeholder={t("auth.confirmPasswordPlaceholder")}
              value={confirmPassword}
              disabled={busy}
              onChange={(event) => setConfirmPassword(event.target.value)}
              onPressEnter={() => void handleSubmit()}
            />
            <Button
              type="primary"
              block
              data-testid="account-password-submit"
              loading={busy}
              disabled={busy || !currentPassword || !newPassword || !confirmPassword}
              onClick={() => void handleSubmit()}
            >
              {t("auth.changePassword")}
            </Button>
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
      </section>
    </main>
  );
}
