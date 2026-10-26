import { Alert, Form, Select } from "antd";
import { useTranslation } from "react-i18next";

const example = `async def process_one(event):
    raise NotImplementedError("Add your idempotent business write")

async def handle(input):
    results = []
    blocked = False
    for member in input["members"]:
        result = {key: member[key] for key in (
            "receipt_id", "delivery_id", "delivery_ordinal", "member_index"
        )}
        if blocked:
            result.update(status="not_run", error_code="predecessor_failed")
        else:
            try:
                await process_one(member["input"])
            except Exception:
                blocked = True
                result.update(status="failed", error_code="business_error")
            else:
                result.update(status="succeeded", error_code=None)
        results.append(result)
    return {
        "schema": "dlr.event.batch.result.v1",
        "batch_id": input["batch_id"],
        "manifest_hash": input["manifest_hash"],
        "members": results,
    }`;

/** Used only by the two sources with explicit business-batch support. */
export default function EventBatchModeFields() {
  const { t } = useTranslation("subscription");
  const mode = Form.useWatch("mode");
  return <>
    <Form.Item name="mode" label={t("businessBatch.mode")} extra={t("businessBatch.modeHint")}>
      <Select options={[
        { value: "per_event", label: t("businessBatch.single") },
        { value: "business_batch", label: t("businessBatch.multiple") },
      ]} />
    </Form.Item>
    {mode === "business_batch" && <>
      <Alert type="warning" showIcon message={t("businessBatch.codeRequired")}
        description={t("businessBatch.savedOnly")} />
      <details className="event-form-advanced">
        <summary>{t("businessBatch.codeHelp")}</summary>
        <p>{t("businessBatch.markerHint")}</p>
        <pre className="subscription-json">{'{"event_batch_schema":"dlr.event.batch.v1"}'}</pre>
        <p>{t("businessBatch.exampleHint")}</p>
        <pre className="subscription-json">{example}</pre>
        <p>{t("businessBatch.orderHint")}</p>
      </details>
    </>}
  </>;
}
