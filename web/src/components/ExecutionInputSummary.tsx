import { Descriptions, Empty, List, Typography } from "antd";
import { useTranslation } from "react-i18next";

import type {
  Execution,
  ExecutionInputArtifactSnapshot,
  ExecutionInputSnapshot,
  InputSourceType,
} from "../types";

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function snapshotFor(execution: Execution): ExecutionInputSnapshot | null {
  const candidate = execution.input_snapshot;
  if (!isRecord(candidate) || typeof candidate.source_type !== "string") {
    return null;
  }
  if (
    (candidate.source_type === "none" || candidate.source_type === "json" || candidate.source_type === "remote_files") &&
    typeof candidate.revision === "number"
  ) {
    return candidate as ExecutionInputSnapshot;
  }
  if (candidate.source_type !== "managed_files" || typeof candidate.revision !== "number") {
    return null;
  }
  if (!Array.isArray(candidate.artifacts)) {
    return { source_type: "managed_files", revision: candidate.revision, artifacts: [] };
  }
  const artifacts: ExecutionInputArtifactSnapshot[] = candidate.artifacts.filter((item): item is ExecutionInputArtifactSnapshot => {
    if (!isRecord(item)) {
      return false;
    }
    return (
      typeof item.ordinal === "number" &&
      typeof item.original_filename === "string" &&
      typeof item.content_type === "string" &&
      typeof item.size_bytes === "number" &&
      typeof item.sha256 === "string"
    );
  });
  return { source_type: "managed_files", revision: candidate.revision, artifacts };
}

function sourceTypeFor(execution: Execution, snapshot: ExecutionInputSnapshot | null): InputSourceType {
  if (snapshot !== null) {
    return snapshot.source_type;
  }
  if (execution.input_source_type !== undefined) {
    return execution.input_source_type;
  }
  return execution.input === null || execution.input === undefined ? "none" : "json";
}

export default function ExecutionInputSummary(props: { execution: Execution }) {
  const { t } = useTranslation("runtime");
  const snapshot = snapshotFor(props.execution);
  const sourceType = sourceTypeFor(props.execution, snapshot);
  const revision = snapshot?.revision ?? props.execution.input_config_revision;

  if (sourceType === "none") {
    return (
      <div data-testid="detail-input" className="execution-input-summary">
        <Typography.Text data-testid="detail-input-none">{t("history.inputNone")}</Typography.Text>
      </div>
    );
  }

  if (sourceType === "json") {
    return (
      <div data-testid="detail-input" className="execution-input-summary">
        <Typography.Text strong>{t("history.inputJson")}</Typography.Text>
        {revision !== undefined && (
          <Typography.Text type="secondary" data-testid="detail-input-revision">
            {t("history.inputRevision")}: {revision}
          </Typography.Text>
        )}
        <pre data-testid="detail-input-json" className="output-view">
          {JSON.stringify(props.execution.input, null, 2)}
        </pre>
      </div>
    );
  }

  if (sourceType === "remote_files") {
    return (
      <div data-testid="detail-input" className="execution-input-summary">
        <Typography.Text>{t("history.inputRemoteFiles")}</Typography.Text>
      </div>
    );
  }

  const artifacts = snapshot?.source_type === "managed_files" ? snapshot.artifacts : [];
  return (
    <div data-testid="detail-input" className="execution-input-summary">
      <Descriptions
        size="small"
        column={1}
        items={[
          { key: "source", label: t("history.inputSource"), children: t("history.inputSourceManagedFiles") },
          ...(revision !== undefined
            ? [{ key: "revision", label: t("history.inputRevision"), children: revision }]
            : []),
        ]}
      />
      <Typography.Text strong>{t("history.inputManagedFiles")}</Typography.Text>
      {artifacts.length === 0 ? (
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t("history.inputFilesEmpty")} />
      ) : (
        <List
          data-testid="detail-input-files"
          size="small"
          bordered
          dataSource={artifacts}
          renderItem={(artifact) => (
            <List.Item data-testid="detail-input-file">
              <Descriptions
                size="small"
                column={1}
                items={[
                  { key: "name", label: t("history.inputFileName"), children: artifact.original_filename },
                  { key: "type", label: t("history.inputFileType"), children: artifact.content_type },
                  { key: "size", label: t("history.inputFileSize"), children: artifact.size_bytes },
                  { key: "sha", label: t("history.inputFileSha256"), children: artifact.sha256 },
                ]}
              />
            </List.Item>
          )}
        />
      )}
    </div>
  );
}
