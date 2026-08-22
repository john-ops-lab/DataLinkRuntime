import type { ReactNode } from "react";
import { Avatar, Button, Dropdown, Tooltip, type MenuProps } from "antd";
import {
  DownOutlined,
  LogoutOutlined,
  SettingOutlined,
  TeamOutlined,
  UserOutlined,
} from "@ant-design/icons";
import { useTranslation } from "react-i18next";

import type { AccountPrincipal, Worker } from "../types";
import WorkerStatus from "./WorkerStatus";

/** Retained as a compatibility type for callers that still describe the old view state. */
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
  /** Deprecated view props are ignored; the Adapter catalog is the only left navigation. */
  selectedAdapterName?: string | null;
  section?: ShellSection;
  onSectionChange?: (section: ShellSection) => void;
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
  const menuLabel = (testId: string, label: string) => (
    <span data-testid={testId} aria-label={label}>{label}</span>
  );
  const menuItems: MenuProps["items"] = [
    ...(onOpenAccountProfile
      ? [{ key: "profile", icon: <UserOutlined aria-hidden="true" />, label: menuLabel("account-profile", t("auth.profile")) }]
      : []),
    ...(canManageUsers && onOpenUserManagement
      ? [{ key: "users", icon: <TeamOutlined aria-hidden="true" />, label: menuLabel("user-management", t("actions.userManagement")) }]
      : []),
    ...(canManageUsers && onOpenSystemSettings
      ? [{ key: "settings", icon: <SettingOutlined aria-hidden="true" />, label: menuLabel("system-settings", t("actions.systemSettings")) }]
      : []),
    ...(onAccountLogout
      ? [
          { type: "divider" as const },
          { key: "logout", icon: <LogoutOutlined aria-hidden="true" />, label: menuLabel("account-logout", t("auth.logout")) },
        ]
      : []),
  ];

  function handleMenuClick({ key }: { key: string }) {
    if (key === "profile") {
      onOpenAccountProfile?.();
    } else if (key === "users") {
      onOpenUserManagement?.();
    } else if (key === "settings") {
      onOpenSystemSettings?.();
    } else if (key === "logout") {
      void onAccountLogout?.();
    }
  }

  const principalLabel = accountPrincipal === undefined
    ? t("auth.superadmin")
    : `${accountPrincipal.username} · ${t(`auth.role.${accountPrincipal.role}`)}`;
  const healthIsAlert = healthDotClass === "health-dot-degraded" || healthDotClass === "health-dot-unreachable";

  return (
    <header className="app-header" data-testid="app-header">
      <div className="app-header-context">
        <span className="app-header-logo" aria-hidden="true">DLR</span>
        <span className="app-header-product">{t("product.name")}</span>
      </div>
      <div className="app-header-status">
        <Tooltip title={healthText} trigger={["hover", "focus"]}>
          <span
            className={`health-status${healthIsAlert ? " health-status-alert" : ""}`}
            data-testid="control-status"
            aria-live="polite"
          >
            <span className={`health-dot ${healthDotClass}`.trim()} aria-hidden="true" />
            <span className="health-status-label">{healthText}</span>
          </span>
        </Tooltip>
        <WorkerStatus workers={workers} loading={workersLoading} error={workersError} />
        {menuItems.length > 0 && (
          <Dropdown
            trigger={["click"]}
            placement="bottomRight"
            menu={{ items: menuItems, onClick: handleMenuClick }}
          >
            <Button
              type="text"
              className="app-user-menu"
              data-testid="user-menu"
              aria-label={t("auth.accountMenu")}
            >
              <Avatar size={28} icon={<UserOutlined aria-hidden="true" />} />
              <span
                className="app-user-menu-name"
                data-testid={accountPrincipal ? "account-principal" : undefined}
              >
                {principalLabel}
              </span>
              <DownOutlined aria-hidden="true" />
            </Button>
          </Dropdown>
        )}
      </div>
    </header>
  );
}

export default function ApplicationShell(props: ApplicationShellProps) {
  const {
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
    children,
  } = props;

  return (
    <div className="dlr-app-layout">
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
      <div className="app-shell-content">{children}</div>
    </div>
  );
}
