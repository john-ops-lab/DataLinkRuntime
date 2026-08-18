import type { ReactNode } from "react";
import { Tooltip } from "antd";
import { useTranslation } from "react-i18next";

interface ActionWithReasonProps {
  label: string;
  reason: string | null;
  children: ReactNode;
}

/**
 * Disabled native controls cannot receive focus. The wrapper keeps a concrete
 * reason available to mouse, keyboard and assistive-technology users without
 * changing the control's click semantics.
 */
export default function ActionWithReason({
  label,
  reason,
  children,
}: ActionWithReasonProps) {
  const { t } = useTranslation("common");
  const action = (
    <span
      className="action-with-reason"
      data-disabled-reason={reason ?? undefined}
      title={reason ?? undefined}
      tabIndex={reason === null ? undefined : 0}
      aria-label={reason === null ? undefined : t("accessibility.unavailable", { label, reason })}
    >
      {children}
    </span>
  );
  return reason === null ? action : (
    <Tooltip title={reason} trigger={["hover", "focus"]}>
      {action}
    </Tooltip>
  );
}
