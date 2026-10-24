import type { ReactNode } from "react";
import { Avatar, Badge, Button, Dropdown, type MenuProps } from "antd";
import {
  DownOutlined,
  LogoutOutlined,
  SettingOutlined,
  TeamOutlined,
  UserOutlined,
} from "@ant-design/icons";
import { useTranslation } from "react-i18next";

import { systemStatusBadgeStatus, type SystemStatusLevel } from "../system-status";
import type { PrimarySection } from "../history-route";
import type { AccountPrincipal } from "../types";

/** Retained as a compatibility type for callers that still describe the old view state. */
export type ShellSection = "adapters" | "workbench";

interface ApplicationShellProps {
  systemStatusLevel: SystemStatusLevel;
  systemStatusText: string;
  canManageUsers: boolean;
  accountPrincipal?: AccountPrincipal;
  onOpenUserManagement?: () => void;
  onOpenSystemSettings?: () => void;
  onOpenSystemStatus?: () => void;
  onOpenAccountProfile?: () => void;
  onAccountLogout?: () => Promise<void>;
  activeSection?: PrimarySection;
  navigationDisabled?: boolean;
  onPrimarySectionChange?: (section: PrimarySection) => void;
  /** Deprecated view props are ignored; the Adapter catalog is the only left navigation. */
  selectedAdapterName?: string | null;
  section?: ShellSection;
  onSectionChange?: (section: ShellSection) => void;
  children: ReactNode;
}

function TopBar({
  systemStatusLevel,
  systemStatusText,
  canManageUsers,
  accountPrincipal,
  onOpenUserManagement,
  onOpenSystemSettings,
  onOpenSystemStatus,
  onOpenAccountProfile,
  onAccountLogout,
  activeSection = "adapters",
  navigationDisabled = false,
  onPrimarySectionChange,
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
  const statusContent = (
    <span className="system-status-summary-content" aria-live="polite">
      <Badge status={systemStatusBadgeStatus(systemStatusLevel)} text={systemStatusText} />
    </span>
  );

  const primaryLinks: { section: PrimarySection; href: string; label: string }[] = [
    { section: "adapters", href: "/adapters", label: t("shell.adapters") },
    { section: "templates", href: "/templates", label: t("shell.templates") },
  ];

  return (
    <header className="app-header" data-testid="app-header">
      <div className="app-header-context">
        <span className="app-header-logo" aria-hidden="true">DLR</span>
        <span className="app-header-product">{t("product.name")}</span>
        <nav className="app-primary-nav" aria-label={t("shell.navigation")}>
          {primaryLinks.map((item) => (
            <a
              key={item.section}
              className={`app-primary-nav-link${activeSection === item.section ? " app-primary-nav-link-active" : ""}`}
              href={item.href}
              aria-current={activeSection === item.section ? "page" : undefined}
              aria-disabled={navigationDisabled || undefined}
              tabIndex={navigationDisabled ? -1 : undefined}
              onClick={(event) => {
                if (navigationDisabled) {
                  event.preventDefault();
                  return;
                }
                if (
                  onPrimarySectionChange !== undefined &&
                  event.button === 0 &&
                  !event.metaKey &&
                  !event.ctrlKey &&
                  !event.shiftKey &&
                  !event.altKey
                ) {
                  event.preventDefault();
                  onPrimarySectionChange(item.section);
                }
              }}
            >
              {item.label}
            </a>
          ))}
        </nav>
      </div>
      <div className="app-header-status">
        {canManageUsers && onOpenSystemStatus ? (
          <Button
            type="text"
            size="small"
            className={`system-status-summary system-status-summary-${systemStatusLevel}`}
            data-testid="system-status-summary"
            aria-label={t("systemStatus.open", { status: systemStatusText })}
            onClick={onOpenSystemStatus}
          >
            {statusContent}
          </Button>
        ) : (
          <span
            className={`system-status-summary system-status-summary-${systemStatusLevel}`}
            data-testid="system-status-summary"
          >
            {statusContent}
          </span>
        )}
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
    systemStatusLevel,
    systemStatusText,
    canManageUsers,
    accountPrincipal,
    onOpenUserManagement,
    onOpenSystemSettings,
    onOpenSystemStatus,
    onOpenAccountProfile,
    onAccountLogout,
    activeSection,
    navigationDisabled,
    onPrimarySectionChange,
    children,
  } = props;

  return (
    <div className="dlr-app-layout">
      <TopBar
        systemStatusLevel={systemStatusLevel}
        systemStatusText={systemStatusText}
        canManageUsers={canManageUsers}
        accountPrincipal={accountPrincipal}
        onOpenUserManagement={onOpenUserManagement}
        onOpenSystemSettings={onOpenSystemSettings}
        onOpenSystemStatus={onOpenSystemStatus}
        onOpenAccountProfile={onOpenAccountProfile}
        onAccountLogout={onAccountLogout}
        activeSection={activeSection}
        navigationDisabled={navigationDisabled}
        onPrimarySectionChange={onPrimarySectionChange}
      />
      <div className="app-shell-content">{children}</div>
    </div>
  );
}
