/** Account-entry session bootstrap and the smallest forced-change/app loop. */

import { useCallback, useEffect, useState } from "react";
import { Drawer, Skeleton } from "antd";
import { useTranslation } from "react-i18next";

import { ApiError, api, onUnauthorized, setAuthToken } from "./api";
import {
  applyUiLocale,
  cacheSystemLocale,
  isSystemLocale,
  readCachedSystemLocale,
  resolveSystemLocale,
} from "./i18n";
import { applyLoginLocalePreference } from "./login-locale";
import type { AccountPrincipal, SystemLocale } from "./types";
import { userErrorMessage } from "./user-message";
import { AdapterConsole } from "./App";
import AccountLoginPage from "./components/AccountLoginPage";
import AccountPasswordPage from "./components/AccountPasswordPage";
import AccountUserPage from "./components/AccountUserPage";

type AccountScreen = "loading" | "login" | "change-password" | "console";

export default function AccountApp() {
  const { i18n, t } = useTranslation("common");
  const [screen, setScreen] = useState<AccountScreen>("loading");
  const [principal, setPrincipal] = useState<AccountPrincipal | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [profileOpen, setProfileOpen] = useState(false);

  const refreshSystemLocale = useCallback(async (): Promise<SystemLocale | null> => {
    try {
      const response = await api.getSystemLocale();
      if (isSystemLocale(response.locale)) {
        cacheSystemLocale(response.locale);
        return response.locale;
      }
    } catch {
      // Keep the cached locale when the public bootstrap read is unavailable.
    }
    return null;
  }, []);

  const showPrincipal = useCallback((next: AccountPrincipal) => {
    setPrincipal(next);
    setNotice(null);
    setProfileOpen(false);
    setScreen(
      next.must_change_password
        ? "change-password"
        : "console",
    );
  }, []);

  const returnToLogin = useCallback((nextNotice: string | null) => {
    setPrincipal(null);
    setProfileOpen(false);
    setNotice(nextNotice);
    setScreen("login");
  }, []);

  useEffect(() => {
    setAuthToken(null);
    onUnauthorized(() => returnToLogin(i18n.t("auth.accountSessionRejected")));
    const systemLocalePromise = refreshSystemLocale();

    let cancelled = false;
    async function bootstrapAccountSession() {
      try {
        await api.getAccountCsrf();
        const response = await api.getAccountPrincipal();
        const serverLocale = await systemLocalePromise;
        if (!cancelled) {
          await applyUiLocale(serverLocale ?? readCachedSystemLocale());
          if (!cancelled) {
            showPrincipal(response.principal);
          }
        }
      } catch (error) {
        const serverLocale = await systemLocalePromise;
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
          await applyLoginLocalePreference(serverLocale ?? undefined);
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
    const serverLocale = await refreshSystemLocale();
    await applyUiLocale(serverLocale ?? readCachedSystemLocale());
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
    return (
      <main className="account-loading" aria-busy="true" aria-live="polite">
        <div className="account-loading-content">
          <Skeleton active title={{ width: "58%" }} paragraph={{ rows: 3 }} />
          <span>{t("auth.accountLoading")}</span>
        </div>
      </main>
    );
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
  return (
    <>
      <AdapterConsole
        accountPrincipal={principal}
        onAccountLogout={handleLogout}
        onOpenAccountProfile={() => setProfileOpen(true)}
      />
      <Drawer
        title={t("users.profileTitle")}
        width={460}
        open={profileOpen}
        destroyOnHidden
        onClose={() => setProfileOpen(false)}
      >
        <AccountUserPage
          principal={principal}
          onPrincipalChange={setPrincipal}
          onPasswordChanged={() => returnToLogin(t("users.passwordChanged"))}
          onLogout={handleLogout}
        />
      </Drawer>
    </>
  );
}
