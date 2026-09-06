/** Administrator-managed offline installation materials and Worker checks. */
import { useCallback, useEffect, useRef, useState } from "react";
import {
  Alert,
  Button,
  Input,
  InputNumber,
  Modal,
  Progress,
  Select,
  Space,
  Table,
  Tag,
  Typography,
  Upload,
} from "antd";
import type { UploadFile, UploadProps } from "antd";
import {
  DeleteOutlined,
  DownloadOutlined,
  ReloadOutlined,
  UploadOutlined,
} from "@ant-design/icons";
import { useTranslation } from "react-i18next";
import { api } from "../api";
import { userErrorMessage } from "../user-message";
import type {
  Adapter,
  BuiltinPackage,
  BuiltinPackageKind,
  BuiltinPackageLibrary,
  BuiltinCheck,
  Worker,
} from "../types";
import "./BuiltinPackagePanel.css";

const KINDS: BuiltinPackageKind[] = ["pypi", "npm", "maven"];
const LANGUAGE: Record<BuiltinPackageKind, string> = {
  pypi: "Python",
  npm: "JavaScript",
  maven: "Java",
};
const LANGUAGE_KIND: Record<string, BuiltinPackageKind> = {
  python: "pypi",
  javascript: "npm",
  java: "maven",
};
const ACTIVE = new Set(["queued", "running", "retry_wait"]);
function bytes(value: number): string {
  if (value < 1024) return `${value} B`;
  if (value < 1024 ** 2) return `${(value / 1024).toFixed(1)} KiB`;
  if (value < 1024 ** 3) return `${(value / 1024 ** 2).toFixed(1)} MiB`;
  return `${(value / 1024 ** 3).toFixed(2)} GiB`;
}

interface Props {
  onMutationStart?: () => void;
  onMutationEnd?: () => void;
  onDirtyChange?: (dirty: boolean) => void;
}

export default function BuiltinPackagePanel({
  onMutationStart,
  onMutationEnd,
  onDirtyChange,
}: Props) {
  const { t } = useTranslation("settings");
  const [library, setLibrary] = useState<BuiltinPackageLibrary | null>(null);
  const [adapters, setAdapters] = useState<Adapter[]>([]);
  const [workers, setWorkers] = useState<Worker[]>([]);
  const [checks, setChecks] = useState<BuiltinCheck[]>([]);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [keyword, setKeyword] = useState("");
  const [kind, setKind] = useState<BuiltinPackageKind | "all">("all");
  const [uploadOpen, setUploadOpen] = useState(false);
  const [uploadKind, setUploadKind] = useState<BuiltinPackageKind>("pypi");
  const [files, setFiles] = useState<UploadFile[]>([]);
  const [mavenPrefix, setMavenPrefix] = useState("");
  const [uploadResults, setUploadResults] = useState<string[]>([]);
  const [deleteItem, setDeleteItem] = useState<BuiltinPackage | null>(null);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [capacityOpen, setCapacityOpen] = useState(false);
  const [capacityError, setCapacityError] = useState<string | null>(null);
  const [quotaMiB, setQuotaMiB] = useState<number | null>(1024);
  const [adapterId, setAdapterId] = useState<number>();
  const [workerId, setWorkerId] = useState<number>();

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [nextLibrary, nextAdapters, nextWorkers, nextChecks] =
        await Promise.all([
          api.listBuiltinPackages(),
          api.listAdapters(),
          api.listWorkers(),
          api.listBuiltinChecks(),
        ]);
      setLibrary(nextLibrary);
      setAdapters(nextAdapters);
      setWorkers(nextWorkers);
      setChecks(nextChecks);
    } catch (cause) {
      setError(userErrorMessage(cause));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);
  const hasActiveCheck = checks.some((check) => ACTIVE.has(check.status));
  useEffect(() => {
    if (!hasActiveCheck) return;
    let active = true;
    const timer = window.setInterval(() => {
      void api
        .listBuiltinChecks()
        .then((next) => {
          if (active) setChecks(next);
        })
        .catch((cause) => {
          if (active) setError(userErrorMessage(cause));
        });
    }, 3000);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, [hasActiveCheck]);

  async function mutate(action: () => Promise<void>) {
    if (busyRef.current) return;
    busyRef.current = true;
    setBusy(true);
    setError(null);
    setNotice(null);
    onMutationStart?.();
    try {
      await action();
    } catch (cause) {
      setError(userErrorMessage(cause));
    } finally {
      busyRef.current = false;
      setBusy(false);
      onMutationEnd?.();
      await load();
    }
  }

  const visibleFiles =
    library?.files.filter(
      (file) =>
        (kind === "all" || file.kind === kind) &&
        `${file.name} ${file.filename} ${file.version}`
          .toLowerCase()
          .includes(keyword.trim().toLowerCase()),
    ) ?? [];
  const selectedAdapter = adapters.find((adapter) => adapter.id === adapterId);
  const compatibleWorkers = workers.filter(
    (worker) =>
      !selectedAdapter ||
      worker.capabilities.includes(selectedAdapter.language),
  );
  const uploadProps: UploadProps = {
    multiple: true,
    fileList: files,
    disabled: busy,
    accept:
      uploadKind === "pypi"
        ? ".whl"
        : uploadKind === "npm"
          ? ".tgz,.tar.gz"
          : ".jar,.pom,.xml",
    beforeUpload: () => false,
    onChange: ({ fileList }) => {
      setFiles(fileList);
      onDirtyChange?.(fileList.length > 0);
    },
    onRemove: () => !busy,
  };

  async function performUploads() {
    await mutate(async () => {
      setUploadResults([]);
      for (const item of files) {
        const file = item.originFileObj;
        if (!file) continue;
        const relativePath = file.webkitRelativePath;
        const repositoryPath =
          uploadKind !== "maven"
            ? ""
            : relativePath
              ? relativePath.split("/").slice(1).join("/")
              : `${mavenPrefix.replace(/\/+$/, "")}/${file.name}`;
        try {
          const result = await api.uploadBuiltinPackage(
            file,
            uploadKind,
            repositoryPath,
          );
          setUploadResults((current) => [
            ...current,
            `${file.name}：${t(result.already_exists ? "builtin.exists" : "builtin.uploaded")}`,
          ]);
          setFiles((current) =>
            current.filter((entry) => entry.uid !== item.uid),
          );
        } catch (cause) {
          setUploadResults((current) => [
            ...current,
            `${file.name}：${userErrorMessage(cause)}`,
          ]);
        }
      }
    });
  }

  async function download(file: BuiltinPackage) {
    try {
      const blob = await api.downloadBuiltinPackage(file.id);
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = file.filename;
      anchor.click();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (cause) {
      setError(userErrorMessage(cause));
    }
  }

  return (
    <div
      className="builtin-package-panel"
      data-testid="builtin-package-panel"
      aria-busy={busy || loading}
    >
      <Alert
        role="note"
        type="info"
        showIcon
        message={t("builtin.intro")}
        description={t("builtin.uploadIsNotInstallable")}
      />
      {error && <Alert type="error" showIcon message={error} role="alert" />}
      {notice && <p role="status">{notice}</p>}
      <div className="builtin-capacity">
        <div className="builtin-capacity-heading">
          <Typography.Text strong>{t("builtin.capacity")}</Typography.Text>
          <Button
            size="small"
            disabled={busy || !library}
            onClick={() => {
              setQuotaMiB(
                (library?.capacity.quota_bytes ?? 1073741824) / 1024 ** 2,
              );
              setCapacityError(null);
              setCapacityOpen(true);
            }}
          >
            {t("builtin.adjustCapacity")}
          </Button>
        </div>
        {library && (
          <>
            <Progress
              percent={Math.min(
                100,
                Math.round(
                  (100 *
                    (library.capacity.used_bytes +
                      library.capacity.reserved_bytes)) /
                    library.capacity.quota_bytes,
                ),
              )}
              showInfo={false}
              aria-label={t("builtin.capacity")}
            />
            <Typography.Text>
              {t("builtin.usage", {
                used: bytes(library.capacity.used_bytes),
                reserved: bytes(library.capacity.reserved_bytes),
                quota: bytes(library.capacity.quota_bytes),
              })}
            </Typography.Text>
          </>
        )}
      </div>
      <div className="builtin-toolbar">
        <Input
          allowClear
          value={keyword}
          onChange={(event) => setKeyword(event.target.value)}
          placeholder={t("builtin.search")}
          aria-label={t("builtin.search")}
        />
        <Select
          value={kind}
          onChange={setKind}
          aria-label={t("builtin.kind")}
          options={[
            { value: "all", label: t("builtin.allKinds") },
            ...KINDS.map((value) => ({ value, label: LANGUAGE[value] })),
          ]}
        />
        <Button
          type="primary"
          icon={<UploadOutlined aria-hidden="true" />}
          disabled={busy}
          onClick={() => {
            setUploadOpen(true);
            setUploadResults([]);
          }}
        >
          {t("builtin.upload")}
        </Button>
        <Button
          icon={<ReloadOutlined aria-hidden="true" />}
          loading={loading}
          disabled={busy}
          onClick={() => void load()}
        >
          {t("builtin.refresh")}
        </Button>
      </div>
      <Table<BuiltinPackage>
        size="small"
        rowKey="id"
        loading={loading}
        dataSource={visibleFiles}
        pagination={{ pageSize: 10, showSizeChanger: true }}
        scroll={{ x: 1040 }}
        columns={[
          {
            title: t("builtin.package"),
            dataIndex: "name",
            width: 170,
            render: (value: string, file) => (
              <div>
                <strong>{value}</strong>
                <br />
                <Typography.Text type="secondary">
                  {file.version}
                </Typography.Text>
              </div>
            ),
          },
          {
            title: t("builtin.kind"),
            dataIndex: "kind",
            width: 100,
            render: (value: BuiltinPackageKind) => LANGUAGE[value],
          },
          {
            title: t("builtin.environment"),
            dataIndex: "environment",
            width: 180,
            render: (value: string) => (
              <span className="builtin-wrap">
                {value === "{}" ? t("builtin.unspecified") : value}
              </span>
            ),
          },
          {
            title: t("builtin.filename"),
            dataIndex: "filename",
            width: 210,
            render: (value: string) => (
              <span className="builtin-wrap">{value}</span>
            ),
          },
          {
            title: t("builtin.size"),
            dataIndex: "size_bytes",
            width: 95,
            render: bytes,
          },
          {
            title: t("builtin.uploadedAt"),
            dataIndex: "created_at",
            width: 160,
            render: (value: string) => new Date(value).toLocaleString(),
          },
          {
            title: t("builtin.status"),
            dataIndex: "status",
            width: 130,
            render: (value: string) => (
              <Tag color={value === "deleting" ? "error" : "default"}>
                {t(
                  value === "deleting"
                    ? "builtin.deleting"
                    : "builtin.uploaded",
                )}
              </Tag>
            ),
          },
          {
            title: t("builtin.actions"),
            fixed: "right",
            width: 95,
            render: (_, file) => (
              <Space size={0}>
                <Button
                  type="text"
                  aria-label={t("builtin.downloadFile", {
                    name: file.filename,
                  })}
                  icon={<DownloadOutlined aria-hidden="true" />}
                  disabled={file.status !== "uploaded"}
                  onClick={() => void download(file)}
                />
                <Button
                  type="text"
                  danger
                  aria-label={t("builtin.deleteFile", { name: file.filename })}
                  icon={<DeleteOutlined aria-hidden="true" />}
                  disabled={busy}
                  onClick={() => {
                    setDeleteItem(file);
                    setDeleteError(null);
                  }}
                />
              </Space>
            ),
          },
        ]}
      />
      {Boolean(library?.uploads.length) && (
        <div>
          <Typography.Title level={5}>
            {t("builtin.pendingUploads")}
          </Typography.Title>
          {library?.uploads.map((item) => (
            <div className="builtin-pending" key={item.id}>
              <span>
                {item.filename} · {bytes(item.size_bytes)}
              </span>
              <Button
                size="small"
                disabled={busy}
                onClick={() =>
                  void mutate(async () => {
                    await api.cancelBuiltinUpload(item.id);
                  })
                }
              >
                {t("builtin.releaseUpload")}
              </Button>
            </div>
          ))}
        </div>
      )}
      <section className="builtin-checks" aria-label={t("builtin.checkTitle")}>
        <Typography.Title level={5}>{t("builtin.checkTitle")}</Typography.Title>
        <Typography.Paragraph type="secondary">
          {t("builtin.checkHint")}
        </Typography.Paragraph>
        <div className="builtin-check-form">
          <Select
            showSearch
            optionFilterProp="label"
            aria-label={t("builtin.adapter")}
            placeholder={t("builtin.adapter")}
            value={adapterId}
            onChange={(id) => {
              setAdapterId(id);
              setWorkerId(undefined);
            }}
            options={adapters
              .filter((adapter) => adapter.latest_version_id != null)
              .map((adapter) => ({
                value: adapter.id,
                label: `${adapter.name} · ${LANGUAGE[LANGUAGE_KIND[adapter.language]]}`,
              }))}
          />
          <Select
            aria-label={t("builtin.worker")}
            placeholder={t("builtin.worker")}
            value={workerId}
            onChange={setWorkerId}
            options={compatibleWorkers.map((worker) => ({
              value: worker.id,
              label: worker.name,
            }))}
          />
          <Button
            disabled={busy || !adapterId || !workerId}
            onClick={() =>
              void mutate(async () => {
                if (adapterId && workerId) {
                  const result = await api.createBuiltinCheck(
                    adapterId,
                    workerId,
                  );
                  setNotice(t("builtin.checkAccepted", { id: result.id }));
                }
              })
            }
          >
            {t("builtin.startCheck")}
          </Button>
        </div>
        <Table<BuiltinCheck>
          size="small"
          rowKey="id"
          dataSource={checks}
          pagination={{ pageSize: 5, showSizeChanger: false }}
          scroll={{ x: 750 }}
          columns={[
            {
              title: t("builtin.checkRecord"),
              width: 180,
              render: (_, check) => (
                <div>
                  #{check.id} · Revision #{check.version_id}
                  <br />
                  <Typography.Text type="secondary">
                    {new Date(check.created_at).toLocaleString()}
                  </Typography.Text>
                </div>
              ),
            },
            {
              title: t("builtin.adapter"),
              dataIndex: "adapter_id",
              render: (id: number) =>
                adapters.find((adapter) => adapter.id === id)?.name ?? `#${id}`,
            },
            {
              title: t("builtin.worker"),
              dataIndex: "target_worker_id",
              render: (id: number) =>
                workers.find((worker) => worker.id === id)?.name ?? `#${id}`,
            },
            {
              title: t("builtin.checkResult"),
              render: (_, check) => (
                <span>
                  {t(
                    check.status === "succeeded"
                      ? "builtin.installable"
                      : ACTIVE.has(check.status)
                        ? "builtin.checking"
                        : check.error_code === "builtin_environment_mismatch"
                          ? "builtin.environmentMismatch"
                          : check.error_code === "builtin_dependency_missing"
                            ? "builtin.missingDependencies"
                            : "builtin.checkFailed",
                  )}
                  {check.error && (
                    <Typography.Paragraph type="danger">
                      {check.error}
                    </Typography.Paragraph>
                  )}
                </span>
              ),
            },
          ]}
        />
      </section>
      <Modal
        title={t("builtin.upload")}
        open={uploadOpen}
        width={640}
        onCancel={() => {
          if (!busy) {
            setUploadOpen(false);
            onDirtyChange?.(false);
          }
        }}
        confirmLoading={busy}
        okText={t("builtin.startUpload")}
        cancelText={t("builtin.close")}
        okButtonProps={{ disabled: files.length === 0 }}
        onOk={() => void performUploads()}
        maskClosable={!busy}
        closable={!busy}
        cancelButtonProps={{ disabled: busy }}
      >
        <Space
          direction="vertical"
          size="middle"
          className="builtin-upload-form"
        >
          <Select
            aria-label={t("builtin.kind")}
            value={uploadKind}
            disabled={busy || files.length > 0}
            onChange={setUploadKind}
            options={KINDS.map((value) => ({ value, label: LANGUAGE[value] }))}
          />
          <Typography.Text>
            {t(`builtin.uploadHint.${uploadKind}`)}
          </Typography.Text>
          {uploadKind === "maven" && (
            <>
              <Input
                aria-label={t("builtin.mavenPrefix")}
                placeholder="com/example/sdk/1.0"
                value={mavenPrefix}
                disabled={busy}
                onChange={(event) => setMavenPrefix(event.target.value)}
              />
              <Upload {...uploadProps} directory showUploadList={false}>
                <Button disabled={busy}>{t("builtin.chooseFolder")}</Button>
              </Upload>
            </>
          )}
          <Upload {...uploadProps}>
            <Button
              disabled={busy}
              icon={<UploadOutlined aria-hidden="true" />}
            >
              {t("builtin.chooseFiles")}
            </Button>
          </Upload>
          <div role="status">
            {uploadResults.map((result, index) => (
              <p key={index}>{result}</p>
            ))}
          </div>
        </Space>
      </Modal>
      <Modal
        title={t("builtin.adjustCapacity")}
        open={capacityOpen}
        confirmLoading={busy}
        onCancel={() => !busy && setCapacityOpen(false)}
        onOk={() =>
          void mutate(async () => {
            if (quotaMiB != null && quotaMiB > 0) {
              try {
                await api.setBuiltinPackageCapacity(
                  Math.round(quotaMiB * 1024 ** 2),
                );
                setCapacityOpen(false);
              } catch (cause) {
                setCapacityError(userErrorMessage(cause));
                throw cause;
              }
            }
          })
        }
        okButtonProps={{ disabled: quotaMiB == null || quotaMiB <= 0 }}
      >
        <Typography.Paragraph>{t("builtin.capacityHint")}</Typography.Paragraph>
        <InputNumber
          min={1}
          precision={0}
          aria-label={t("builtin.quotaMiB")}
          suffix="MiB"
          value={quotaMiB}
          onChange={setQuotaMiB}
        />
        {capacityError && (
          <Alert type="error" message={capacityError} role="alert" />
        )}
      </Modal>
      <Modal
        title={t("builtin.confirmDelete")}
        open={deleteItem != null}
        confirmLoading={busy}
        okText={t("builtin.deletePermanently")}
        okButtonProps={{ danger: true }}
        cancelText={t("builtin.cancel")}
        onCancel={() => !busy && setDeleteItem(null)}
        onOk={() =>
          void mutate(async () => {
            if (!deleteItem) return;
            try {
              await api.deleteBuiltinPackage(deleteItem.id);
              setDeleteItem(null);
              setNotice(t("builtin.deleted"));
            } catch (cause) {
              setDeleteError(userErrorMessage(cause));
              throw cause;
            }
          })
        }
      >
        <Typography.Paragraph strong>
          {deleteItem?.filename}
        </Typography.Paragraph>
        <Typography.Paragraph>
          {t("builtin.deleteWarning")}
        </Typography.Paragraph>
        {deleteError && (
          <Alert type="error" message={deleteError} role="alert" />
        )}
      </Modal>
    </div>
  );
}
