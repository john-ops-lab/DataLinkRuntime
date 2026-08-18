/** 系统设置抽屉：凭据管理 + 三语言依赖源（M3.3，全局平台配置）。 */

import { useCallback, useEffect, useState } from "react";
import {
  Button,
  Checkbox,
  Drawer,
  Empty,
  Input,
  Select,
  Space,
  Spin,
  Table,
  Tabs,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { useTranslation } from "react-i18next";

import { api } from "../api";
import {
  CREDENTIAL_TYPE_FIELDS,
  credentialFieldLabel,
  credentialFields,
  credentialTypeLabel,
} from "../credential-fields";
import { notifyCredentialCatalogChanged, subscribeCredentialCatalog } from "../credential-catalog";
import { applySystemLocale, isSystemLocale, resolveSystemLocale } from "../i18n";
import { packageSourceKindLabel, packageSourcePresetLabel } from "../package-source-catalog";
import type {
  Credential,
  CredentialType,
  PackageSource,
  PackageSourceDefaults,
  SystemLocale,
} from "../types";
import { userErrorMessage } from "../user-message";
import AiModelSettingsPanel from "./AiModelSettingsPanel";

function errorMessage(error: unknown): string {
  return userErrorMessage(error);
}

// --- 凭据管理 ---------------------------------------------------------------

// M5.5.7：四类凭据的常驻用户说明，帮助用户在新建前选择正确的类型。
// 与 credential-fields.ts / 后端 CREDENTIAL_FIELDS 保持一致。
const CREDENTIAL_TYPE_GUIDE: readonly CredentialType[] = [
  "password",
  "token",
  "access_key",
  "secret",
];

interface CredentialFormState {
  /** null = 新建；数字 = 更新该凭据的名称/值。 */
  editingId: number | null;
  name: string;
  type: CredentialType;
  fields: Record<string, string>;
}

function emptyForm(): CredentialFormState {
  return { editingId: null, name: "", type: "password", fields: {} };
}

function CredentialsPanel(props: { onError: (message: string) => void }) {
  const { t } = useTranslation(["settings", "common"]);
  const { onError } = props;
  const [credentials, setCredentials] = useState<Credential[]>([]);
  const [loading, setLoading] = useState(true);
  const [formOpen, setFormOpen] = useState(false);
  const [form, setForm] = useState<CredentialFormState>(emptyForm);
  const [submitting, setSubmitting] = useState(false);
  const [panelError, setPanelError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const fail = useCallback(
    (message: string) => {
      setPanelError(message);
      setNotice(null);
      onError(message);
    },
    [onError],
  );

  const load = useCallback(async (): Promise<boolean> => {
    setLoading(true);
    try {
      setCredentials(await api.listCredentials());
      setPanelError(null);
      return true;
    } catch (error) {
      fail(errorMessage(error));
      return false;
    } finally {
      setLoading(false);
    }
  }, [fail]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- 挂载时拉取凭据列表的初始加载是有意的异步同步
    void load();
  }, [load]);

  function openCreate() {
    setNotice(null);
    setForm(emptyForm());
    setFormOpen(true);
  }

  function openUpdate(credential: Credential) {
    setNotice(null);
    setForm({
      editingId: credential.id,
      name: credential.name,
      type: credential.type as CredentialType,
      fields: {},
    });
    setFormOpen(true);
  }

  function handleTypeChange(type: CredentialType) {
    setForm((current) => ({ ...current, type, fields: {} }));
  }

  async function handleSubmit() {
    if (submitting) {
      return;
    }
    const name = form.name.trim();
    if (name === "") {
      fail(t("credentials.nameRequired"));
      return;
    }
    const required = credentialFields(form.type);
    const fields: Record<string, string> = {};
    for (const key of required) {
      const value = (form.fields[key] ?? "").trim();
      if (value === "") {
        fail(t("credentials.fieldRequired", { field: credentialFieldLabel(key) }));
        return;
      }
      fields[key] = value;
    }
    // M5.5.7：创建前一次性明文提醒。保存后 Secret 真值无法再通过浏览器
    // 查看，因此必须在最终提交前明确告知用户先妥善保存或复制。
    if (form.editingId === null) {
      const confirmed = window.confirm(
        t("confirm.createCredential", { ns: "common" }),
      );
      if (!confirmed) {
        return;
      }
    }
    setNotice(null);
    setSubmitting(true);
    try {
      setPanelError(null);
      if (form.editingId === null) {
        await api.createCredential({ name, type: form.type, fields });
      } else {
        await api.updateCredential(form.editingId, { name, fields });
      }
      setFormOpen(false);
      const operation = form.editingId === null ? t("credentials.created") : t("credentials.updated");
      if (await load()) {
        setNotice(operation);
      } else {
        setPanelError(t("credentials.createRefreshFailed", { operation }));
      }
      // 跨设置同步（UX-003）：让 AI 模型 / 绑定 / 依赖源等选择器无需 F5 即可
      // 看到新凭据。只通知变化，不携带任何 Secret 数据。
      notifyCredentialCatalogChanged();
    } catch (error) {
      fail(errorMessage(error));
    } finally {
      setSubmitting(false);
    }
  }

  async function handleDelete(credential: Credential) {
    if (!window.confirm(t("confirm.deleteCredential", { name: credential.name, ns: "common" }))) {
      return;
    }
    try {
      setPanelError(null);
      setNotice(null);
      await api.deleteCredential(credential.id);
      if (await load()) {
        setNotice(t("credentials.deleted"));
      } else {
        setPanelError(t("credentials.deleteRefreshFailed"));
      }
      // 删除同样属于凭据元数据变化，通知所有选择器失效旧引用。
      notifyCredentialCatalogChanged();
    } catch (error) {
      fail(errorMessage(error));
    }
  }

  const formFieldKeys = credentialFields(form.type);

  return (
    <div className="settings-panel" data-testid="credentials-panel">
      <div className="credential-type-guide" data-testid="credential-type-guide">
         <Typography.Title level={5}>{t("credentialGuide.title")}</Typography.Title>
        <Typography.Paragraph type="secondary">
           {t("credentialGuide.description")}
        </Typography.Paragraph>
        <ul className="credential-type-guide-list">
           {CREDENTIAL_TYPE_GUIDE.map((type) => (
             <li key={type} data-testid={`credential-type-guide-${type}`}>
               <strong>{credentialTypeLabel(type)}</strong>（{t("credentialGuide.fields")}：
               {t(`credentialGuide.items.${type}.fields`)}）：{t("credentialGuide.scenarios")} {t(`credentialGuide.items.${type}.scenarios`)}。{t(`credentialGuide.${type === "password" ? "createHint" : type === "token" ? "tokenHint" : type === "access_key" ? "accessKeyHint" : "secretHint"}`)}。
            </li>
          ))}
        </ul>
      </div>
      <Space className="settings-panel-toolbar">
        <Button type="primary" data-testid="new-credential" onClick={openCreate}>
           {t("credentials.new")}
        </Button>
        <Button
          data-testid="refresh-credentials"
          loading={loading}
          onClick={() => {
            setNotice(null);
             void load().then((ok) => ok && setNotice(t("credentials.refreshList")));
          }}
        >
          {t("actions.refresh", { ns: "common" })}
        </Button>
      </Space>
      <Typography.Text type="secondary">{t("credentials.metadataNotice")}</Typography.Text>
      {panelError !== null && <p className="settings-panel-error" role="alert">{panelError}</p>}
      {notice !== null && <p className="settings-panel-success" role="status">{notice}</p>}

      {formOpen && (
        <div className="settings-inline-form" data-testid="credential-form">
          <Input
            data-testid="credential-name"
            aria-label={t("credentials.name")}
            placeholder={t("labels.name", { ns: "common" })}
            value={form.name}
            onChange={(event) => setForm((current) => ({ ...current, name: event.target.value }))}
          />
          <Select
            data-testid="credential-type"
            aria-label={t("credentials.type")}
            style={{ minWidth: 200 }}
            value={form.type}
            disabled={form.editingId !== null}
            options={Object.keys(CREDENTIAL_TYPE_FIELDS).map((type) => ({
              label: `${credentialTypeLabel(type)}（${credentialFields(type).join(" + ")}）`,
              value: type,
            }))}
            onChange={(value) => handleTypeChange(value)}
          />
          {formFieldKeys.map((key) => (
            <Input.Password
              key={key}
              data-testid={`credential-field-${key}`}
              aria-label={t("credentials.fieldAria", { field: key })}
              placeholder={credentialFieldLabel(key)}
              value={form.fields[key] ?? ""}
              autoComplete="new-password"
              onChange={(event) =>
                setForm((current) => ({
                  ...current,
                  fields: { ...current.fields, [key]: event.target.value },
                }))
              }
            />
          ))}
          <Space>
            <Button
              type="primary"
              data-testid="submit-credential"
              loading={submitting}
              onClick={() => void handleSubmit()}
            >
              {form.editingId === null ? t("credentials.submitCreate") : t("credentials.submitUpdate")}
            </Button>
            <Button onClick={() => setFormOpen(false)}>{t("credentials.cancel")}</Button>
          </Space>
          {form.editingId !== null && (
            <Typography.Text type="secondary">
              {t("credentials.updateHint")}
            </Typography.Text>
          )}
        </div>
      )}

      {loading ? (
        <Spin />
      ) : credentials.length === 0 ? (
        <Empty description={t("empty.noCredentials", { ns: "common" })} />
      ) : (
        <Table<Credential>
          rowKey="id"
          size="small"
          pagination={false}
          dataSource={credentials}
          columns={[
            { title: t("credentials.tableName"), dataIndex: "name", render: (name: string) => <span data-testid="credential-row">{name}</span> },
            {
              title: t("credentials.tableType"),
              dataIndex: "type",
              width: 120,
               render: (type: string) => credentialTypeLabel(type),
            },
            {
              title: t("credentials.tableActions"),
              width: 160,
              render: (_, credential) => (
                <Space>
                  <Button
                    size="small"
                    data-testid="update-credential"
                    onClick={() => openUpdate(credential)}
                  >
                    {t("credentials.submitUpdate")}
                  </Button>
                  <Button
                    size="small"
                    danger
                    data-testid="delete-credential"
                    onClick={() => void handleDelete(credential)}
                  >
                    {t("actions.delete", { ns: "common" })}
                  </Button>
                </Space>
              ),
            },
          ]}
        />
      )}
    </div>
  );
}

// --- Python 包源 -------------------------------------------------------------

type PackageSourceKind = "pypi" | "npm" | "maven";

function kindLabel(kind: PackageSourceKind): string {
  return packageSourceKindLabel(kind);
}

interface PackageSourceFormState {
  name: string;
  kind: "pypi" | "npm" | "maven";
  index_url: string;
  is_default: boolean;
  credential_id: number | null;
}

type PackageSourceTestStatus = "reachable" | "unreachable" | "timeout" | "auth-failed";

interface PackageSourceTestResult {
  status: PackageSourceTestStatus;
  detail: string | null;
}

const EMPTY_SOURCE_FORM: PackageSourceFormState = {
  name: "",
  kind: "pypi",
  index_url: "",
  is_default: false,
  credential_id: null,
};

function PackageSourcesPanel(props: { onError: (message: string) => void }) {
  const { t } = useTranslation(["settings", "common"]);
  const { onError } = props;
  const [sources, setSources] = useState<PackageSource[]>([]);
  const [credentials, setCredentials] = useState<Credential[]>([]);
  const [defaults, setDefaults] = useState<PackageSourceDefaults | null>(null);
  const [loading, setLoading] = useState(true);
  const [formOpen, setFormOpen] = useState(false);
  const [form, setForm] = useState<PackageSourceFormState>(EMPTY_SOURCE_FORM);
  const [submitting, setSubmitting] = useState(false);
  const [testing, setTesting] = useState<number | null>(null);
  const [restoring, setRestoring] = useState<"pypi" | "npm" | "maven" | null>(null);
  const [testResults, setTestResults] = useState<Map<number, PackageSourceTestResult>>(new Map());
  const [panelError, setPanelError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const fail = useCallback(
    (message: string) => {
      setPanelError(message);
      setNotice(null);
      onError(message);
    },
    [onError],
  );

  const load = useCallback(async (): Promise<boolean> => {
    setLoading(true);
    try {
      const [sourceList, credentialList, defaultsResult] = await Promise.all([
        api.listPackageSources(),
        api.listCredentials(),
        api.getPackageSourceDefaults(),
      ]);
      setSources(sourceList);
      setCredentials(credentialList);
      setDefaults(defaultsResult);
      setPanelError(null);
      return true;
    } catch (error) {
      fail(errorMessage(error));
      return false;
    } finally {
      setLoading(false);
    }
  }, [fail]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- 挂载时拉取包源与凭据列表的初始加载是有意的异步同步
    void load();
  }, [load]);

  // 凭据增删改后仅刷新凭据选择器（UX-003）；已打开的包源表单不会被清空。
  useEffect(
    () =>
      subscribeCredentialCatalog(() => {
        void api
          .listCredentials()
          .then((credentialList) => setCredentials(credentialList))
          .catch((error) => fail(errorMessage(error)));
      }),
    [fail],
  );

  async function handleSubmit() {
    if (submitting) {
      return;
    }
    const name = form.name.trim();
    const indexUrl = form.index_url.trim();
    if (name === "" || indexUrl === "") {
      fail(t("packageSources.nameAndUrlRequired"));
      return;
    }
    setNotice(null);
    setSubmitting(true);
    try {
      setPanelError(null);
      await api.createPackageSource({
        name,
        kind: form.kind,
        index_url: indexUrl,
        is_default: form.is_default,
        credential_id: form.credential_id,
      });
      setFormOpen(false);
      setForm(EMPTY_SOURCE_FORM);
      if (await load()) {
        setNotice(t("packageSources.created"));
      } else {
        setPanelError(t("packageSources.createRefreshFailed"));
      }
    } catch (error) {
      fail(errorMessage(error));
    } finally {
      setSubmitting(false);
    }
  }

  async function handleSetDefault(source: PackageSource) {
    try {
      setPanelError(null);
      setNotice(null);
      await api.updatePackageSource(source.id, { is_default: true });
      if (await load()) {
        setNotice(t("packageSources.setDefault", { name: source.name }));
      } else {
        setPanelError(t("packageSources.setDefaultRefreshFailed", { name: source.name }));
      }
    } catch (error) {
      fail(errorMessage(error));
    }
  }

  async function handleDelete(source: PackageSource) {
    if (!window.confirm(t("confirm.deletePackageSource", { name: source.name, ns: "common" }))) {
      return;
    }
    try {
      setPanelError(null);
      setNotice(null);
      await api.deletePackageSource(source.id);
      if (await load()) {
        setNotice(t("packageSources.deleted"));
      } else {
        setPanelError(t("packageSources.deleteRefreshFailed"));
      }
    } catch (error) {
      fail(errorMessage(error));
    }
  }

  async function handleTest(source: PackageSource) {
    if (testing !== null) {
      return;
    }
    setTesting(source.id);
    setTestResults((current) => {
      const next = new Map(current);
      next.delete(source.id);
      return next;
    });
    try {
      const result = await api.testPackageSource(source.id);
      const errorDetail = result.error?.trim() || null;
      const authFailure = !result.ok &&
        (result.status_code === 401 || result.status_code === 403 ||
          /auth|unauthori|forbidden|认证/i.test(errorDetail ?? ""));
      const timeout = !result.ok &&
        (result.status_code === 408 || result.status_code === 504 ||
          /timeout|timed out|time out|超时/i.test(errorDetail ?? ""));
      const status: PackageSourceTestStatus = result.ok
        ? "reachable"
        : authFailure
          ? "auth-failed"
          : timeout
            ? "timeout"
            : "unreachable";
      const detail = result.ok
        ? result.status_code === null
          ? null
          : `HTTP ${result.status_code}`
        : errorDetail ?? t("packageSources.testDetailUnknown");
      setTestResults((current) =>
        new Map(current).set(source.id, { status, detail }),
      );
    } catch (error) {
      fail(errorMessage(error));
    } finally {
      setTesting(null);
    }
  }

  async function handleRestoreDefault(kind: "pypi" | "npm" | "maven") {
    if (restoring !== null) {
      return;
    }
    const canonical = defaults?.[kind];
    if (canonical === undefined) {
      fail(t("packageSources.emptyDefaults"));
      return;
    }
    if (
      !window.confirm(
        t("packageSources.restoreConfirm", {
          name: canonical.name,
          url: canonical.index_url,
          kind: kindLabel(kind),
        }),
      )
    ) {
      return;
    }
    setRestoring(kind);
    setPanelError(null);
    setNotice(null);
    try {
      await api.restorePackageSourceDefault(kind);
      if (await load()) {
        setNotice(t("packageSources.restoreDefault", { kind: kindLabel(kind) }));
      } else {
        setPanelError(t("packageSources.restoreDefaultRefreshFailed", { kind: kindLabel(kind) }));
      }
    } catch (error) {
      fail(errorMessage(error));
    } finally {
      setRestoring(null);
    }
  }

  const kinds: ("pypi" | "npm" | "maven")[] = ["pypi", "npm", "maven"];

  const columns: ColumnsType<PackageSource> = [
    {
      title: t("labels.type", { ns: "common" }),
      dataIndex: "kind",
      width: 90,
      render: (kind: PackageSource["kind"]) => packageSourceKindLabel(kind),
    },
    {
      title: t("labels.name", { ns: "common" }),
      dataIndex: "name",
      width: 190,
      render: (_name: string, source) => (
        <Tooltip title={packageSourcePresetLabel(source)}>
          <span className="package-source-cell" data-testid="package-source-row">
            {packageSourcePresetLabel(source)}
            {source.is_default && (
              <Tag color="green" data-testid="default-source-badge">
                {t("labels.default", { ns: "common" })}
              </Tag>
            )}
          </span>
        </Tooltip>
      ),
    },
    {
      title: t("labels.repositoryUrl", { ns: "common" }),
      dataIndex: "index_url",
      width: 300,
      render: (url: string) => (
        <Tooltip title={url}>
          <span className="package-source-cell">{url}</span>
        </Tooltip>
      ),
    },
    {
      title: t("labels.accessCredential", { ns: "common" }),
      dataIndex: "credential_name",
      width: 110,
      render: (name: string | null) => name ?? "—",
    },
    {
      title: t("labels.reachability", { ns: "common" }),
      width: 190,
      render: (_, source) => {
        const result = testResults.get(source.id);
        const testingThisSource = testing === source.id;
        return (
          <div className="package-source-test-cell">
            <Button
              size="small"
              data-testid="test-package-source"
              loading={testing === source.id}
              disabled={testing !== null}
              onClick={() => void handleTest(source)}
            >
              {t("packageSources.test")}
            </Button>
            <Tooltip title={result?.detail ?? undefined}>
              <Typography.Text
                className={`package-source-test-status${testingThisSource ? " is-loading" : ""}`}
                type={
                  result === undefined || testingThisSource
                    ? undefined
                    : result.status === "reachable"
                      ? "success"
                      : "danger"
                }
                role={
                  result === undefined || testingThisSource || result.status === "reachable"
                    ? "status"
                    : "alert"
                }
                data-testid="package-source-test-result"
              >
                {testingThisSource
                  ? t("packageSources.testing")
                  : result === undefined
                    ? t("packageSources.untested")
                    : t(`packageSources.${result.status === "auth-failed" ? "authFailed" : result.status}`)}
              </Typography.Text>
            </Tooltip>
          </div>
        );
      },
    },
    {
      title: t("labels.operation", { ns: "common" }),
      width: 170,
      render: (_, source) => (
        <Space>
          <Button
            size="small"
            data-testid="set-default-source"
            disabled={source.is_default}
            onClick={() => void handleSetDefault(source)}
          >
             {t("packageSources.setDefaultAction")}
          </Button>
          <Button
            size="small"
            danger
            data-testid="delete-package-source"
            onClick={() => void handleDelete(source)}
          >
             {t("actions.delete", { ns: "common" })}
          </Button>
        </Space>
      ),
    },
  ];

  return (
    <div className="settings-panel" data-testid="package-sources-panel">
      <Space className="settings-panel-toolbar">
        <Button
          type="primary"
          data-testid="new-package-source"
          onClick={() => {
            setNotice(null);
            setFormOpen(true);
          }}
        >
          {t("packageSources.new")}
        </Button>
        <Button
          data-testid="refresh-package-sources"
          loading={loading}
          onClick={() => {
            setNotice(null);
            void load().then((ok) => ok && setNotice(t("packageSources.refreshList")));
          }}
        >
          {t("actions.refresh", { ns: "common" })}
        </Button>
      </Space>
      <Typography.Text type="secondary">
         {t("packageSources.description")}
      </Typography.Text>

      <div className="settings-package-source-defaults" data-testid="package-source-defaults">
        {defaults !== null && (
          <Space wrap>
            {kinds.map((kind) => {
              const canonical = defaults[kind];
              return (
                <Button
                  key={kind}
                  size="small"
                  data-testid={`restore-default-${kind}`}
                  loading={restoring === kind}
                  disabled={restoring !== null}
                  onClick={() => void handleRestoreDefault(kind)}
                   title={t("packageSources.restoreTitle", { url: canonical.index_url })}
                >
                   {t("packageSources.restoreButton", { kind: kindLabel(kind) })}
                </Button>
              );
            })}
          </Space>
        )}
        {kinds.map((kind) => {
          const hasDefault = sources.some((source) => source.kind === kind && source.is_default);
          if (hasDefault) {
            return null;
          }
          return (
            <Typography.Text
              key={kind}
              type="warning"
              data-testid={`no-default-source-${kind}`}
              className="settings-package-source-fallback"
            >
              {t("packageSources.fallbackNotice", { kind: kindLabel(kind) })}
            </Typography.Text>
          );
        })}
      </div>
      {panelError !== null && <p className="settings-panel-error" role="alert">{panelError}</p>}
      {notice !== null && <p className="settings-panel-success" role="status">{notice}</p>}

      {formOpen && (
        <div className="settings-inline-form" data-testid="package-source-form">
          <Input
            data-testid="package-source-name"
            aria-label={t("packageSources.name")}
            placeholder={t("labels.name", { ns: "common" })}
            value={form.name}
            onChange={(event) => setForm((current) => ({ ...current, name: event.target.value }))}
          />
          <Select
            data-testid="package-source-kind"
            aria-label={t("packageSources.kind")}
            value={form.kind}
            style={{ minWidth: 180 }}
            options={[
              { label: "PyPI", value: "pypi" },
              { label: "npm", value: "npm" },
              { label: "Maven", value: "maven" },
            ]}
            onChange={(kind: PackageSource["kind"]) =>
              setForm((current) => ({ ...current, kind, credential_id: null }))
            }
          />
          <Input
            data-testid="package-source-url"
            aria-label={t("packageSources.repositoryUrl")}
            placeholder={t("packageSources.urlPlaceholder")}
            value={form.index_url}
            onChange={(event) => setForm((current) => ({ ...current, index_url: event.target.value }))}
          />
          <Checkbox
            data-testid="package-source-default"
            checked={form.is_default}
            onChange={(event) => setForm((current) => ({ ...current, is_default: event.target.checked }))}
            >
             {t("packageSources.defaultCheckbox")}
          </Checkbox>
          <Select
            data-testid="package-source-credential"
            aria-label={t("packageSources.credential")}
            placeholder={t("packageSources.credentialPlaceholder")}
            allowClear
            style={{ minWidth: 220 }}
            value={form.credential_id ?? undefined}
            options={credentials
              .filter((credential) =>
                form.kind === "npm"
                  ? credential.type === "password" || credential.type === "token"
                  : credential.type === "password",
              )
              .map((credential) => ({
                label: credential.name,
                value: credential.id,
              }))}
            onChange={(value) => setForm((current) => ({ ...current, credential_id: value ?? null }))}
          />
          <Space>
            <Button
              type="primary"
              data-testid="submit-package-source"
              loading={submitting}
              onClick={() => void handleSubmit()}
            >
              {t("actions.create", { ns: "common" })}
            </Button>
            <Button onClick={() => setFormOpen(false)}>{t("actions.cancel", { ns: "common" })}</Button>
          </Space>
        </div>
      )}

      {loading ? (
        <Spin />
      ) : sources.length === 0 ? (
        <Empty description={t("empty.noPackageSources", { ns: "common" })} />
      ) : (
        <Table<PackageSource>
          rowKey="id"
          size="small"
          pagination={false}
          dataSource={sources}
          className="package-source-table"
          tableLayout="fixed"
          scroll={{ x: 1060 }}
          columns={columns}
        />
      )}
    </div>
  );
}

// --- 抽屉外壳 -----------------------------------------------------------------

interface SystemSettingsDrawerProps {
  open: boolean;
  onClose: () => void;
}

// Settings panels render their own persistent alert next to the failed action.
// Keep those errors local so the same message is not duplicated in the global
// Console banner while the Drawer is open.
function keepErrorInline(): void {}

function SystemLocaleControl() {
  const { i18n, t } = useTranslation("settings");
  const currentLocale = resolveSystemLocale(i18n.resolvedLanguage ?? i18n.language);
  const [updating, setUpdating] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleChange(nextLocale: SystemLocale) {
    if (updating || nextLocale === currentLocale) {
      return;
    }
    setUpdating(true);
    setError(null);
    try {
      const response = await api.updateSystemLocale(nextLocale);
      if (!isSystemLocale(response.locale)) {
        throw new Error("Invalid locale response");
      }
      await applySystemLocale(response.locale);
    } catch (err) {
      setError(userErrorMessage(err, t("localeUpdateFailed"), currentLocale));
    } finally {
      setUpdating(false);
    }
  }

  return (
    <div className="settings-locale-control" data-testid="system-locale-control">
      <label className="settings-field">
        <span className="settings-field-label">{t("language")}</span>
        <Select
          aria-label={t("language")}
          data-testid="system-locale-select"
          value={currentLocale}
          loading={updating}
          disabled={updating}
          options={[
            { value: "zh-CN", label: t("localeOptions.zh-CN") },
            { value: "en", label: t("localeOptions.en") },
          ]}
          onChange={(value: string) => {
            if (isSystemLocale(value)) {
              void handleChange(value);
            }
          }}
        />
      </label>
      {error !== null && <p className="settings-panel-error" role="alert">{error}</p>}
    </div>
  );
}

export default function SystemSettingsDrawer(props: SystemSettingsDrawerProps) {
  const { t } = useTranslation("settings");
  return (
    <Drawer
      title={t("title")}
      width={720}
      open={props.open}
      destroyOnHidden
      onClose={props.onClose}
    >
      <SystemLocaleControl />
      <Tabs
        items={[
          {
            key: "credentials",
            label: t("tabs.credentials"),
            children: <CredentialsPanel onError={keepErrorInline} />,
          },
          {
            key: "package-sources",
            label: t("tabs.packageSources"),
            children: <PackageSourcesPanel onError={keepErrorInline} />,
          },
          {
            key: "ai-model",
            label: t("tabs.aiModel"),
            children: <AiModelSettingsPanel onError={keepErrorInline} />,
          },
        ]}
      />
    </Drawer>
  );
}
