import type { PropsWithChildren } from "react";
import { ConfigProvider, type ThemeConfig } from "antd";
import enUS from "antd/locale/en_US";
import zhCN from "antd/locale/zh_CN";
import { useTranslation } from "react-i18next";

import { resolveSystemLocale } from "./i18n";

/**
 * Wave A keeps the existing DLR surface values in one thin Ant Design entry.
 * Component-specific changes belong to a later, explicitly scoped Wave.
 */
export const DLR_ANT_DESIGN_THEME = {
  token: {
    colorBgLayout: "#f5f6f8",
    borderRadius: 4,
  },
} satisfies ThemeConfig;

export const ANT_DESIGN_LOCALES = {
  "zh-CN": zhCN,
  en: enUS,
} as const;

export default function DlrDesignSystemProvider({ children }: PropsWithChildren) {
  const { i18n } = useTranslation("common");
  const locale = resolveSystemLocale(i18n.resolvedLanguage ?? i18n.language);

  return (
    <ConfigProvider locale={ANT_DESIGN_LOCALES[locale]} theme={DLR_ANT_DESIGN_THEME}>
      {children}
    </ConfigProvider>
  );
}
