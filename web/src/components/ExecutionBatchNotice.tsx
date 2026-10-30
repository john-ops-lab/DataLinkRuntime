import { Alert } from "antd";
import { useTranslation } from "react-i18next";
import type { Execution } from "../types";

/** Only the immutable input of a subscription Execution supplies this count. */
export default function ExecutionBatchNotice({ execution }: { execution: Pick<Execution, "trigger" | "input"> }) {
  const { t } = useTranslation("subscription");
  const input = execution.input;
  if (execution.trigger !== "subscription" || input === null || typeof input !== "object" || Array.isArray(input)) return null;
  const value = input as Record<string, unknown>;
  if (value.schema !== "dlr.event.batch.v1" || !Array.isArray(value.members) || value.members.length < 1 || value.members.length > 100) return null;
  return <Alert type="info" showIcon data-testid="execution-business-batch-notice"
    message={t("businessBatch.groupCount", { count: value.members.length })}
    description={t("businessBatch.cancelHint")} />;
}
