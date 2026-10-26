import { Alert, Checkbox } from "antd";
import { useTranslation } from "react-i18next";

/** Explicit author declaration in the version draft; never saves code itself. */
export default function EventBatchCodeDeclaration({ value, disabled, onChange }: {
  value: string;
  disabled: boolean;
  onChange: (value: string) => void;
}) {
  const { t } = useTranslation("subscription");
  let config: Record<string, unknown> | null = null;
  try {
    const parsed: unknown = JSON.parse(value);
    if (parsed !== null && typeof parsed === "object" && !Array.isArray(parsed)) config = parsed as Record<string, unknown>;
  } catch { /* Keep invalid drafts intact; the existing save validation still applies. */ }
  return <div>
    <Checkbox checked={config?.event_batch_schema === "dlr.event.batch.v1"}
      disabled={disabled || config === null} onChange={event => {
        if (disabled || config === null) return;
        const next = { ...config };
        if (event.target.checked) next.event_batch_schema = "dlr.event.batch.v1";
        else delete next.event_batch_schema;
        onChange(JSON.stringify(next, null, 2));
      }}>{t("businessBatch.codeDeclaration")}</Checkbox>
    <p className="ux-muted">{t("businessBatch.declarationHint")}</p>
    {config === null && <Alert type="warning" message={t("businessBatch.invalidConfig")} />}
  </div>;
}
