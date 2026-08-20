import { useState } from "react";
import { Button, Card, Input, Space, Typography } from "antd";
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

export default function AccountUserPage({
  principal,
  onPrincipalChange,
  onPasswordChanged,
  onLogout,
}: AccountUserPageProps) {
  const { i18n, t } = useTranslation("common");
  const [username, setUsername] = useState(principal.username);
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const locale = resolveSystemLocale(i18n.resolvedLanguage ?? i18n.language);

  async function saveProfile() {
    if (busy || !username.trim() || username.trim() === principal.username) {
      return;
    }
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const updated = await api.updateUser(principal.id, { username: username.trim() });
      onPrincipalChange({
        ...principal,
        username: updated.username,
        role: updated.role,
        enabled: updated.enabled,
        must_change_password: updated.must_change_password,
      });
      setUsername(updated.username);
      setNotice(t("users.profileSaved"));
    } catch (err) {
      setError(userErrorMessage(err, t("users.profileSaveFailed"), locale));
    } finally {
      setBusy(false);
    }
  }

  async function changePassword() {
    if (busy || !currentPassword || !newPassword || newPassword !== confirmPassword) {
      if (newPassword !== confirmPassword) {
        setError(t("users.passwordMismatch"));
      }
      return;
    }
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      await api.changeAccountPassword({
        current_password: currentPassword,
        new_password: newPassword,
      });
      setCurrentPassword("");
      setNewPassword("");
      setConfirmPassword("");
      onPasswordChanged();
    } catch (err) {
      setError(userErrorMessage(err, t("users.passwordChangeFailed"), locale));
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="account-user-page">
      <Card className="account-user-card">
        <Space direction="vertical" size="large" style={{ width: "100%" }}>
          <div>
            <Typography.Title level={3}>{t("users.profileTitle")}</Typography.Title>
            <Typography.Paragraph type="secondary">{t("users.profileSubtitle")}</Typography.Paragraph>
          </div>
          {error !== null && <p className="error-banner" role="alert">{error}</p>}
          {notice !== null && <p className="settings-panel-success" role="status">{notice}</p>}
          <label className="settings-field">
            <span className="settings-field-label">{t("users.username")}</span>
            <Input
              data-testid="account-profile-username"
              value={username}
              disabled={busy}
              onChange={(event) => setUsername(event.target.value)}
            />
          </label>
          <Button
            type="primary"
            data-testid="account-profile-save"
            loading={busy}
            disabled={!username.trim() || username.trim() === principal.username}
            onClick={() => void saveProfile()}
          >
            {t("users.saveProfile")}
          </Button>
          <section className="account-user-password" aria-labelledby="account-user-password-title">
            <Typography.Title id="account-user-password-title" level={4}>{t("users.passwordTitle")}</Typography.Title>
            <label className="settings-field">
              <span className="settings-field-label">{t("users.currentPassword")}</span>
              <Input.Password
                data-testid="account-user-current-password"
                value={currentPassword}
                disabled={busy}
                onChange={(event) => setCurrentPassword(event.target.value)}
              />
            </label>
            <label className="settings-field">
              <span className="settings-field-label">{t("users.newPassword")}</span>
              <Input.Password
                data-testid="account-user-new-password"
                value={newPassword}
                disabled={busy}
                onChange={(event) => setNewPassword(event.target.value)}
              />
            </label>
            <label className="settings-field">
              <span className="settings-field-label">{t("users.confirmPassword")}</span>
              <Input.Password
                data-testid="account-user-confirm-password"
                value={confirmPassword}
                disabled={busy}
                onChange={(event) => setConfirmPassword(event.target.value)}
              />
            </label>
            <Button
              data-testid="account-user-password-submit"
              loading={busy}
              disabled={!currentPassword || !newPassword || !confirmPassword}
              onClick={() => void changePassword()}
            >
              {t("users.changePassword")}
            </Button>
          </section>
          <Button data-testid="account-user-logout" onClick={() => void onLogout()}>
            {t("auth.logout")}
          </Button>
        </Space>
      </Card>
    </main>
  );
}
