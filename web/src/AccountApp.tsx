/** Account-entry session bootstrap and the smallest forced-change/app loop. */

import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { ApiError, api, onUnauthorized, setAuthToken } from "./api";
import { applySystemLocale, isSystemLocale, resolveSystemLocale } from "./i18n";
import type { AccountPrincipal } from "./types";
import { userErrorMessage } from "./user-message";
import { AdapterConsole } from "./App";
import AccountLoginPage from "./components/AccountLoginPage";
import AccountPasswordPage from "./components/AccountPasswordPage";
import AccountUserPage from "./components/AccountUserPage";

type AccountScreen = "loading" | "login" | "change-password" | "console" | "user-profile";

export default function AccountApp() {
  const { i18n, t } = useTranslation("common");
  const [screen, setScreen] = useState<AccountScreen>("loading");
  const [principal, setPrincipal] = useState<AccountPrincipal | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const refreshSystemLocale = useCallback(async () => {
    try {
      const response = await api.getSystemLocale();
      if (isSystemLocale(response.locale)) {
        await applySystemLocale(response.locale);
      }
    } catch {
      // Keep the cached locale when the public bootstrap read is unavailable.
    }
  }, []);

  const showPrincipal = useCallback((next: AccountPrincipal) => {
    setPrincipal(next);
    setNotice(null);
    setScreen(
      next.must_change_password
        ? "change-password"
        : next.role === "admin"
          ? "console"
          : "user-profile",
    );
  }, []);

  const returnToLogin = useCallback((nextNotice: string | null) => {
    setPrincipal(null);
    setNotice(nextNotice);
    setScreen("login");
  }, []);

  useEffect(() => {
    setAuthToken(null);
    onUnauthorized(() => returnToLogin(i18n.t("auth.accountSessionRejected")));
    void refreshSystemLocale();

    let cancelled = false;
    async function bootstrapAccountSession() {
      try {
        await api.getAccountCsrf();
        const response = await api.getAccountPrincipal();
        if (!cancelled) {
          showPrincipal(response.principal);
        }
      } catch (error) {
        if (!cancelled && (!(error instanceof ApiError) || error.status !== 401)) {
          setNotice(
            userErrorMessage(
              error,
              i18n.t("auth.accountUnavailable"),
              resolveSystemLocale(i18n.language),
            ),
          );
        }
        if (!cancelled) {
          setScreen("login");
        }
      }
    }
    void bootstrapAccountSession();
    return () => {
      cancelled = true;
    };
  }, [i18n, refreshSystemLocale, returnToLogin, showPrincipal]);

  async function handleLogin(username: string, password: string) {
    await api.getAccountCsrf();
    const response = await api.loginAccount({ username, password });
    showPrincipal(response.principal);
  }

  async function handleChangePassword(currentPassword: string, newPassword: string) {
    await api.changeAccountPassword({
      current_password: currentPassword,
      new_password: newPassword,
    });
    await api.getAccountCsrf();
    returnToLogin(t("auth.passwordChangedNotice"));
  }

  async function handleLogout() {
    try {
      await api.logoutAccount();
    } finally {
      await api.getAccountCsrf().catch(() => undefined);
      returnToLogin(t("auth.logoutNotice"));
    }
  }

  if (screen === "loading") {
    return <main className="account-loading" aria-busy="true">{t("auth.accountLoading")}</main>;
  }
  if (screen === "login") {
    return <AccountLoginPage notice={notice} onSubmit={handleLogin} />;
  }
  if (screen === "change-password" && principal !== null) {
    return (
      <AccountPasswordPage
        username={principal.username}
        onSubmit={handleChangePassword}
        onLogout={handleLogout}
      />
    );
  }
  if (principal === null) {
    return <AccountLoginPage notice={notice} onSubmit={handleLogin} />;
  }
  if (screen === "user-profile") {
    return (
      <AccountUserPage
        principal={principal}
        onPrincipalChange={setPrincipal}
        onPasswordChanged={() => returnToLogin(t("users.passwordChanged"))}
        onLogout={handleLogout}
      />
    );
  }
  return <AdapterConsole accountPrincipal={principal} onAccountLogout={handleLogout} />;
}
