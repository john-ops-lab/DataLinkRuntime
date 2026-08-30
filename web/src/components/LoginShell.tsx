import type { ReactNode } from "react";
import { Select } from "antd";
import { useTranslation } from "react-i18next";

import { useLoginLocale } from "../login-locale";
import type { SystemLocale } from "../types";

interface LoginShellProps {
  children: ReactNode;
  testId?: string;
  /** Authenticated account surfaces follow the backend locale, not login preference. */
  loginSurface?: boolean;
}

export default function LoginShell({ children, testId, loginSurface = true }: LoginShellProps) {
  const [locale, selectLocale] = useLoginLocale(loginSurface);
  const { i18n } = useTranslation("common");
  // Bind the first render to the login preference. The global i18n instance
  // may still carry the deployment locale until useLoginLocale's effect runs.
  const t = i18n.getFixedT(locale, "common");

  return (
    <main className="auth-page" data-testid={testId}>
      <div className="auth-shell">
        <section className="auth-brand" aria-label={t("product.name")}>
          <div>
            <div className="auth-brand-logo">DLR</div>
            <div className="auth-brand-name">{t("product.name")}</div>
          </div>
          <p className="auth-brand-tagline">{t("product.headline")}</p>
          <p className="auth-brand-sub">{t("product.intro1")}</p>
          <p className="auth-brand-sub auth-brand-intro login-brand-intro">{t("product.intro2")}</p>
          <p className="auth-brand-flow">{t("product.flow")}</p>
          <p className="auth-copyright">{t("product.copyright")}</p>
        </section>

        <section className="auth-form-column">
          <div className="auth-language-picker">
            <span id="auth-language-label">{t("auth.languageLabel")}</span>
            <Select<SystemLocale>
              aria-labelledby="auth-language-label"
              data-testid="login-locale-select"
              value={locale}
              disabled={!loginSurface}
              options={[
                { value: "zh-CN", label: t("auth.languageZh") },
                { value: "en", label: t("auth.languageEn") },
              ]}
              onChange={loginSurface ? selectLocale : undefined}
            />
          </div>
          {children}
        </section>
      </div>
    </main>
  );
}
