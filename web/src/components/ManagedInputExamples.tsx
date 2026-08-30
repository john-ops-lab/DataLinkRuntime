import { useState } from "react";
import { Button, Card, Space, Tag, Tooltip, Typography } from "antd";
import { CopyOutlined } from "@ant-design/icons";
import { useTranslation } from "react-i18next";

import { managedInputExample } from "../managed-input-examples";
import type { AdapterLanguage } from "../types";

export default function ManagedInputExamples(props: {
  language: AdapterLanguage;
  ready: boolean;
  disabledReason: string | null;
}) {
  const { t } = useTranslation("runtime");
  const [copyState, setCopyState] = useState<"idle" | "success" | "failed">("idle");
  const code = managedInputExample(props.language);

  async function copyExample(): Promise<void> {
    if (!props.ready) {
      return;
    }
    try {
      if (navigator.clipboard?.writeText === undefined) {
        throw new Error("clipboard unavailable");
      }
      await navigator.clipboard.writeText(code);
      setCopyState("success");
    } catch {
      setCopyState("failed");
    }
  }

  const copyButton = (
    <Button
      data-testid="managed-input-example-copy"
      icon={<CopyOutlined aria-hidden="true" />}
      disabled={!props.ready}
      aria-label={t("task.input.examples.copy")}
      onClick={() => void copyExample()}
    >
      {t("task.input.examples.copy")}
    </Button>
  );

  return (
    <Card
      size="small"
      className="managed-input-examples"
      data-testid="managed-input-examples"
      title={t("task.input.examples.title")}
    >
      <Space direction="vertical" size="small" className="managed-input-examples-content">
        <Typography.Text type="secondary">{t("task.input.examples.description")}</Typography.Text>
        <Tag data-testid="managed-input-example-language">
          {t(`task.input.examples.languages.${props.language}`)}
        </Tag>
        <pre
          className="managed-input-example-code"
          data-testid="managed-input-example-code"
          aria-label={t("task.input.examples.codeLabel")}
          tabIndex={0}
        >
          {code}
        </pre>
        <Tooltip
          title={props.ready ? undefined : props.disabledReason ?? t("task.input.examples.notReady")}
          trigger={["hover", "focus"]}
        >
          <span
            className="action-with-reason"
            data-testid="managed-input-example-copy-wrapper"
            tabIndex={props.ready ? -1 : 0}
            aria-disabled={!props.ready}
          >
            {copyButton}
          </span>
        </Tooltip>
        {copyState !== "idle" && (
          <Typography.Text
            type={copyState === "success" ? "success" : "danger"}
            data-testid="managed-input-example-copy-status"
            role="status"
          >
            {copyState === "success"
              ? t("task.input.examples.copied")
              : t("task.input.examples.copyFailed")}
          </Typography.Text>
        )}
      </Space>
    </Card>
  );
}
