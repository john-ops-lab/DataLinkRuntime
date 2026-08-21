import type { ReactNode } from "react";
import { Button, Tooltip, type MenuProps } from "antd";
import {
  AppstoreOutlined,
  CodeOutlined,
  SettingOutlined,
  TeamOutlined,
  UserOutlined,
  LogoutOutlined,
} from "@ant-design/icons";
import { PageContainer, ProLayout } from "@ant-design/pro-components";
import { useTranslation } from "react-i18next";

import type { AccountPrincipal, Worker } from "../types";
import WorkerStatus from "./WorkerStatus";

export type ShellSection = "adapters" | "workbench";

interface ApplicationShellProps {
  healthText: string;
  healthDotClass: string;
  workers: Worker[];
  workersLoading: boolean;
  workersError: string | null;
  canManageUsers: boolean;
  accountPrincipal?: AccountPrincipal;
  onOpenUserManagement?: () => void;
  onOpenSystemSettings?: () => void;
  onOpenAccountProfile?: () => void;
  onAccountLogout?: () => Promise<void>;
  selectedAdapterName: string | null;
  section: ShellSection;
  onSectionChange: (section: ShellSection) => void;
  children: ReactNode;
}

function TopBar({
  healthText,
  healthDotClass,
  workers,
  workersLoading,
  workersError,
  canManageUsers,
  accountPrincipal,
  onOpenUserManagement,
  onOpenSystemSettings,
  onOpenAccountProfile,
  onAccountLogout,
}: Omit<ApplicationShellProps, "selectedAdapterName" | "section" | "onSectionChange" | "children">) {
  const { t } = useTranslation("common");

  return (
    <header className="app-header" data-testid="app-header">
      <div className="app-header-context">
        <span className="app-header-product">{t("product.tagline")}</span>
      </div>
      <div className="app-header-status">
        <span className="health-status">
          <span className={`health-dot ${healthDotClass}`.trim()} aria-hidden="true" />
          <span data-testid="control-status" aria-live="polite">{healthText}</span>
        </span>
        {canManageUsers && onOpenUserManagement && (
          <Tooltip title={t("actions.userManagement")} trigger={["hover", "focus"]}>
            <Button
              size="small"
              type="text"
              icon={<TeamOutlined aria-hidden="true" />}
              data-testid="user-management"
              aria-label={t("actions.userManagement")}
              onClick={onOpenUserManagement}
            >
              {t("actions.userManagement")}
            </Button>
          </Tooltip>
        )}
        {canManageUsers && onOpenSystemSettings && (
          <Tooltip title={t("actions.systemSettings")} trigger={["hover", "focus"]}>
            <Button
              size="small"
              type="text"
              icon={<SettingOutlined aria-hidden="true" />}
              data-testid="system-settings"
              aria-label={t("actions.systemSettings")}
              onClick={onOpenSystemSettings}
            >
              {t("actions.systemSettings")}
            </Button>
          </Tooltip>
        )}
        {accountPrincipal && onAccountLogout && (
          <>
            <span className="account-principal" data-testid="account-principal">
              {accountPrincipal.username} · {t(`auth.role.${accountPrincipal.role}`)}
            </span>
            {onOpenAccountProfile && (
              <Tooltip title={t("auth.profile")} trigger={["hover", "focus"]}>
                <Button
                  size="small"
                  type="text"
                  icon={<UserOutlined aria-hidden="true" />}
                  data-testid="account-profile"
                  aria-label={t("auth.profile")}
                  onClick={onOpenAccountProfile}
                >
                  {t("auth.profile")}
                </Button>
              </Tooltip>
            )}
            <Tooltip title={t("auth.logout")} trigger={["hover", "focus"]}>
              <Button
                size="small"
                type="text"
                icon={<LogoutOutlined aria-hidden="true" />}
                data-testid="account-logout"
                aria-label={t("auth.logout")}
                onClick={() => void onAccountLogout()}
              >
                {t("auth.logout")}
              </Button>
            </Tooltip>
          </>
        )}
        <WorkerStatus workers={workers} loading={workersLoading} error={workersError} />
      </div>
    </header>
  );
}

export default function ApplicationShell({
  healthText,
  healthDotClass,
  workers,
  workersLoading,
  workersError,
  canManageUsers,
  accountPrincipal,
  onOpenUserManagement,
  onOpenSystemSettings,
  onOpenAccountProfile,
  onAccountLogout,
  selectedAdapterName,
  section,
  onSectionChange,
  children,
}: ApplicationShellProps) {
  const { t } = useTranslation("common");
  const menuItems = [
    {
      key: "adapters",
      path: "/adapters",
      name: t("shell.adapters"),
      icon: <AppstoreOutlined aria-hidden="true" />,
    },
    {
      key: "workbench",
      path: "/workbench",
      name: t("shell.workbench"),
      icon: <CodeOutlined aria-hidden="true" />,
      disabled: selectedAdapterName === null,
    },
  ];
  const menuProps: MenuProps = {
    "aria-label": t("shell.navigation"),
    selectedKeys: [section],
    onClick: ({ key }) => {
      if (key === "adapters" || key === "workbench") {
        onSectionChange(key);
      }
    },
  };
  const breadcrumbItems = [
    { title: t("shell.console") },
    { title: t("shell.adapters") },
    ...(selectedAdapterName === null ? [] : [{ title: selectedAdapterName }]),
  ];

  return (
    <ProLayout
      className="dlr-app-layout"
      title={t("product.name")}
      logo={false}
      navTheme="light"
      layout="mix"
      siderWidth={208}
      fixSiderbar
      route={{ routes: menuItems }}
      location={{ pathname: section === "adapters" ? "/adapters" : "/workbench" }}
      menu={{ locale: false, type: "group" }}
      menuProps={menuProps}
      menuHeaderRender={() => (
        <div className="app-menu-brand" aria-label={t("product.name")}>
          <span className="app-menu-logo">DLR</span>
          <span className="app-menu-name">{t("product.name")}</span>
        </div>
      )}
      menuFooterRender={() => (
        <div className="app-menu-footer">{t("product.flow")}</div>
      )}
      headerRender={() => (
        <TopBar
          healthText={healthText}
          healthDotClass={healthDotClass}
          workers={workers}
          workersLoading={workersLoading}
          workersError={workersError}
          canManageUsers={canManageUsers}
          accountPrincipal={accountPrincipal}
          onOpenUserManagement={onOpenUserManagement}
          onOpenSystemSettings={onOpenSystemSettings}
          onOpenAccountProfile={onOpenAccountProfile}
          onAccountLogout={onAccountLogout}
        />
      )}
      footerRender={false}
      contentStyle={{ minHeight: 0, display: "flex", flexDirection: "column" }}
    >
      <PageContainer
        className="dlr-page-container"
        title={<span data-testid="page-title" className="app-page-title">{t("shell.workspaceTitle")}</span>}
        subTitle={selectedAdapterName ?? t("shell.workspaceSubtitle")}
        breadcrumb={{ items: breadcrumbItems }}
        childrenContentStyle={{ padding: 0, minHeight: 0, display: "flex", flexDirection: "column" }}
      >
        <div className="app-page-content">{children}</div>
      </PageContainer>
    </ProLayout>
  );
}
