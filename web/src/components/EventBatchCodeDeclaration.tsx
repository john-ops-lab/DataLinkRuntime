import { Alert, Checkbox } from "antd";
import { useTranslation } from "react-i18next";
import { parseRuntimeConfigDraft } from "../runtime-config";

/** Explicit author declaration in the version draft; never saves code itself. */
export default function EventBatchCodeDeclaration({ value, disabled, unsafeNumbers = false, onChange }: {
  value: string;
  disabled: boolean;
  unsafeNumbers?: boolean;
  onChange: (value: string) => void;
}) {
  const { t } = useTranslation("subscription");
  const parsed = parseRuntimeConfigDraft(value);
  const config = parsed.kind === "invalid" ? null : parsed.value;
  const unsafe = unsafeNumbers || parsed.kind === "unsafe_number";
  return <div>
    <Checkbox checked={config?.event_batch_schema === "dlr.event.batch.v1"}
      disabled={disabled || config === null || unsafe} onChange={event => {
        if (disabled || config === null || unsafe) return;
        const next = { ...config };
        if (event.target.checked) next.event_batch_schema = "dlr.event.batch.v1";
        else delete next.event_batch_schema;
        onChange(JSON.stringify(next, null, 2));
      }}>{t("businessBatch.codeDeclaration")}</Checkbox>
    <p className="ux-muted">{t("businessBatch.declarationHint")}</p>
    {config === null && <Alert type="warning" message={t("businessBatch.invalidConfig")} />}
    {unsafe && <Alert type="warning" message={t("validation.runtimeConfigUnsafeNumber", { ns: "common" })} />}
  </div>;
}
