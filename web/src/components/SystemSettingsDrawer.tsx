/** 系统设置抽屉：凭据管理 + 三语言依赖源（M3.3，全局平台配置）。 */

import { useCallback, useEffect, useState, type ChangeEvent } from "react";
import {
  Button,
  Checkbox,
  Dropdown,
  Empty,
  Form,
  Input,
  Menu,
  Popover,
  Select,
  Space,
  Spin,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import {
  ArrowLeftOutlined,
  DeleteOutlined,
  DownOutlined,
  EditOutlined,
  PlusOutlined,
  QuestionCircleOutlined,
  ReloadOutlined,
  SearchOutlined,
  StarOutlined,
} from "@ant-design/icons";
import {
  ModalForm,
  ProForm,
  ProTable,
} from "@ant-design/pro-components";
import type { ProColumns } from "@ant-design/pro-components";
import { useTranslation } from "react-i18next";

import { api } from "../api";
import {
  CREDENTIAL_TYPE_FIELDS,
  credentialFieldLabel,
  credentialFields,
  credentialTypeLabel,
} from "../credential-fields";
import { notifyCredentialCatalogChanged, subscribeCredentialCatalog } from "../credential-catalog";
import {
  applySystemLocale,
  isSystemLocale,
  readCachedSystemLocale,
  resolveSystemLocale,
} from "../i18n";
import { packageSourceKindLabel, packageSourcePresetLabel } from "../package-source-catalog";
import type {
  Credential,
  CredentialType,
  KnowledgeBase,
  KnowledgeSource,
  PackageSource,
  PackageSourceDefaults,
  SystemLocale,
} from "../types";
import { userErrorMessage } from "../user-message";
import AiModelSettingsPanel from "./AiModelSettingsPanel";
import type { SettingsCategory } from "../settings-route";

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

interface CredentialFormValues {
  name: string;
  type: CredentialType;
  fields: Record<string, string>;
}

interface CredentialFilters {
  keyword: string;
  type: CredentialType | "all";
}

function emptyForm(): CredentialFormState {
  return { editingId: null, name: "", type: "password", fields: {} };
}

function CredentialsPanel(props: {
  onError: (message: string) => void;
  onSaved?: () => void;
}) {
  const { t } = useTranslation(["settings", "common"]);
  const { onError, onSaved } = props;
  const [credentials, setCredentials] = useState<Credential[]>([]);
  const [loading, setLoading] = useState(true);
  const [formOpen, setFormOpen] = useState(false);
  const [form, setForm] = useState<CredentialFormState>(emptyForm);
  const [submitting, setSubmitting] = useState(false);
  const [panelError, setPanelError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [filters, setFilters] = useState<CredentialFilters>({ keyword: "", type: "all" });

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

  async function handleSubmit(values: CredentialFormValues): Promise<boolean> {
    if (submitting) {
      return false;
    }
    const name = values.name.trim();
    if (name === "") {
      fail(t("credentials.nameRequired"));
      return false;
    }
    const type = values.type;
    const required = credentialFields(type);
    const fields: Record<string, string> = {};
    for (const key of required) {
      const value = (values.fields?.[key] ?? "").trim();
      if (value === "") {
        fail(t("credentials.fieldRequired", { field: credentialFieldLabel(key) }));
        return false;
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
        return false;
      }
    }
    setNotice(null);
    setSubmitting(true);
    try {
      setPanelError(null);
      if (form.editingId === null) {
        await api.createCredential({ name, type, fields });
      } else {
        await api.updateCredential(form.editingId, { name, fields });
      }
      onSaved?.();
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
      return true;
    } catch (error) {
      fail(errorMessage(error));
      return false;
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
  const visibleCredentials = credentials.filter((credential) => {
    const keyword = filters.keyword.trim().toLowerCase();
    return (filters.type === "all" || credential.type === filters.type) &&
      (keyword === "" || credential.name.toLowerCase().includes(keyword));
  });

  return (
    <div className="settings-panel credentials-settings-panel" data-testid="credentials-panel">
      <div
        className="settings-page-toolbar credentials-toolbar"
        data-testid="credentials-toolbar"
        role="toolbar"
        aria-label={t("credentials.toolbar")}
      >
        <div className="settings-toolbar-filters" data-testid="credentials-filters">
          <Input
            allowClear
            prefix={<SearchOutlined aria-hidden="true" />}
            placeholder={t("credentials.filterKeyword")}
            aria-label={t("credentials.filterKeyword")}
            value={filters.keyword}
            onChange={(event) => setFilters((current) => ({ ...current, keyword: event.target.value }))}
            data-settings-filter="true"
          />
          <Select<CredentialType | "all">
            aria-label={t("credentials.filterType")}
            value={filters.type}
            onChange={(type) => setFilters((current) => ({ ...current, type }))}
            options={[
              { value: "all", label: t("credentials.filterAll") },
              ...Object.keys(CREDENTIAL_TYPE_FIELDS).map((type) => ({
                value: type as CredentialType,
                label: credentialTypeLabel(type),
              })),
            ]}
            data-settings-filter="true"
          />
        </div>
        <div className="settings-toolbar-actions">
          <Button
            type="primary"
            icon={<PlusOutlined aria-hidden="true" />}
            data-testid="new-credential"
            onClick={openCreate}
          >
            {t("credentials.new")}
          </Button>
          <Button
            icon={<ReloadOutlined aria-hidden="true" />}
            data-testid="refresh-credentials"
            loading={loading}
            onClick={() => {
              setNotice(null);
              void load().then((ok) => ok && setNotice(t("credentials.refreshList")));
            }}
          >
            {t("actions.refresh", { ns: "common" })}
          </Button>
          <Popover
            title={t("credentialGuide.title")}
            content={(
              <div className="credential-type-guide" data-testid="credential-type-guide">
                <Typography.Paragraph type="secondary">
                  {t("credentialGuide.description")}
                </Typography.Paragraph>
                <ul className="credential-type-guide-list">
                  {CREDENTIAL_TYPE_GUIDE.map((type) => (
                    <li key={type} data-testid={`credential-type-guide-${type}`}>
                      <strong>{credentialTypeLabel(type)}</strong>
                      {t("credentialGuide.lineSuffix", {
                        fieldsLabel: t("credentialGuide.fields"),
                        fields: t(`credentialGuide.items.${type}.fields`),
                        scenariosLabel: t("credentialGuide.scenarios"),
                        scenarios: t(`credentialGuide.items.${type}.scenarios`),
                        hint: t(`credentialGuide.${type === "password" ? "createHint" : type === "token" ? "tokenHint" : type === "access_key" ? "accessKeyHint" : "secretHint"}`),
                      })}
                    </li>
                  ))}
                </ul>
              </div>
            )}
            trigger="click"
            placement="bottomRight"
            destroyOnHidden={false}
          >
            <Tooltip title={t("credentials.help")}>
              <Button
                type="text"
                icon={<QuestionCircleOutlined aria-hidden="true" />}
                data-testid="credential-help"
                aria-label={t("credentials.help")}
                aria-haspopup="dialog"
              />
            </Tooltip>
          </Popover>
        </div>
      </div>
      <div className="settings-panel-notice" role="note">
        <Typography.Text type="secondary">{t("credentials.metadataNotice")}</Typography.Text>
      </div>
      {panelError !== null && <p className="settings-panel-error" role="alert">{panelError}</p>}
      {notice !== null && <p className="settings-panel-success" role="status">{notice}</p>}

      <ModalForm<CredentialFormValues>
        key={`credential-form-${formOpen}-${form.editingId ?? "new"}-${form.type}`}
        title={form.editingId === null ? t("credentials.submitCreate") : t("credentials.submitUpdate")}
        open={formOpen}
        initialValues={{ name: form.name, type: form.type, fields: form.fields }}
        modalProps={{ destroyOnHidden: true, onCancel: () => setFormOpen(false) }}
        submitter={{
          render: () => [
            <Button
              key="submit"
              type="primary"
              data-testid="submit-credential"
              loading={submitting}
              onClick={() =>
                void handleSubmit({
                  name: form.name,
                  type: form.type,
                  fields: form.fields,
                })
              }
            >
              {form.editingId === null ? t("credentials.submitCreate") : t("credentials.submitUpdate")}
            </Button>,
            <Button key="cancel" onClick={() => setFormOpen(false)} disabled={submitting}>
              {t("credentials.cancel")}
            </Button>,
          ],
        }}
        onValuesChange={(changed, values) => {
          const nextType = values.type ?? form.type;
          setForm((current) => ({
            ...current,
            name: values.name ?? current.name,
            type: nextType,
            fields: changed.type === undefined ? (values.fields ?? current.fields) : {},
          }));
          if (changed.type !== undefined) {
            handleTypeChange(nextType);
          }
        }}
        onFinish={handleSubmit}
      >
        <div className="settings-inline-form" data-testid="credential-form">
          <Form.Item name="name" noStyle>
            <Input
              data-testid="credential-name"
              aria-label={t("credentials.name")}
              placeholder={t("labels.name", { ns: "common" })}
            />
          </Form.Item>
          <Form.Item name="type" noStyle>
            <Select<CredentialType>
              data-testid="credential-type"
              aria-label={t("credentials.type")}
              style={{ minWidth: 200 }}
              disabled={form.editingId !== null}
              options={Object.keys(CREDENTIAL_TYPE_FIELDS).map((type) => ({
                label: t("credentials.typeOption", {
                  type: credentialTypeLabel(type),
                  fields: credentialFields(type).join(" + "),
                }),
                value: type as CredentialType,
              }))}
            />
          </Form.Item>
          {formFieldKeys.map((key) => (
            <Form.Item key={key} name={["fields", key]} noStyle>
              <Input.Password
                data-testid={`credential-field-${key}`}
                aria-label={t("credentials.fieldAria", { field: key })}
                placeholder={credentialFieldLabel(key)}
                autoComplete="new-password"
              />
            </Form.Item>
          ))}
          {form.editingId !== null && (
            <Typography.Text type="secondary">
              {t("credentials.updateHint")}
            </Typography.Text>
          )}
        </div>
      </ModalForm>

      {loading ? (
        <Spin />
      ) : credentials.length === 0 ? (
        <Empty description={t("empty.noCredentials", { ns: "common" })} />
      ) : (
        <ProTable<Credential>
          rowKey="id"
          size="small"
          search={false}
          options={false}
          pagination={{ pageSize: 8, showSizeChanger: true }}
          dataSource={visibleCredentials}
          className="credentials-table"
          tableLayout="fixed"
          locale={{ emptyText: t("empty.noCredentials", { ns: "common" }) }}
          columns={[
            {
              title: t("credentials.tableName"),
              dataIndex: "name",
              ellipsis: true,
              render: (_, credential) => (
                <span
                  className="credential-name-cell"
                  data-testid="credential-row"
                  title={credential.name}
                >
                  {credential.name}
                </span>
              ),
            },
            {
              title: t("credentials.tableType"),
              dataIndex: "type",
              width: 160,
              render: (type) => credentialTypeLabel(String(type)),
            },
            {
              title: t("credentials.tableActions"),
              width: 112,
              align: "right",
              render: (_, credential) => (
                <Space size={0} className="credential-row-actions">
                  <Tooltip title={t("credentials.editAria", { name: credential.name })}>
                    <Button
                      type="text"
                      size="small"
                      icon={<EditOutlined aria-hidden="true" />}
                      data-testid="update-credential"
                      aria-label={t("credentials.editAria", { name: credential.name })}
                      onClick={() => openUpdate(credential)}
                    />
                  </Tooltip>
                  <Tooltip title={t("credentials.deleteAria", { name: credential.name })}>
                    <Button
                      type="text"
                      size="small"
                      danger
                      icon={<DeleteOutlined aria-hidden="true" />}
                      data-testid="delete-credential"
                      aria-label={t("credentials.deleteAria", { name: credential.name })}
                      onClick={() => void handleDelete(credential)}
                    />
                  </Tooltip>
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

interface PackageSourceFilters {
  keyword: string;
  kind: PackageSourceKind | "all";
  defaultOnly: "all" | "default" | "custom";
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

function PackageSourcesPanel(props: {
  onError: (message: string) => void;
  onSaved?: () => void;
}) {
  const { t } = useTranslation(["settings", "common"]);
  const { onError, onSaved } = props;
  const [sources, setSources] = useState<PackageSource[]>([]);
  const [credentials, setCredentials] = useState<Credential[]>([]);
  const [defaults, setDefaults] = useState<PackageSourceDefaults | null>(null);
  const [loading, setLoading] = useState(true);
  const [formOpen, setFormOpen] = useState(false);
  const [form, setForm] = useState<PackageSourceFormState>(EMPTY_SOURCE_FORM);
  const [sourceForm] = ProForm.useForm<PackageSourceFormState>();
  const [submitting, setSubmitting] = useState(false);
  const [testing, setTesting] = useState<number | null>(null);
  const [restoring, setRestoring] = useState<"pypi" | "npm" | "maven" | null>(null);
  const [testResults, setTestResults] = useState<Map<number, PackageSourceTestResult>>(new Map());
  const [panelError, setPanelError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [filters, setFilters] = useState<PackageSourceFilters>({
    keyword: "",
    kind: "all",
    defaultOnly: "all",
  });

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

  async function handleSubmit(): Promise<boolean> {
    if (submitting) {
      return false;
    }
    const name = form.name.trim();
    const indexUrl = form.index_url.trim();
    if (name === "" || indexUrl === "") {
      fail(t("packageSources.nameAndUrlRequired"));
      return false;
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
      onSaved?.();
      setFormOpen(false);
      setForm(EMPTY_SOURCE_FORM);
      if (await load()) {
        setNotice(t("packageSources.created"));
      } else {
        setPanelError(t("packageSources.createRefreshFailed"));
      }
      return true;
    } catch (error) {
      fail(errorMessage(error));
      return false;
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

  const visibleSources = sources.filter((source) => {
    const keyword = filters.keyword.trim().toLowerCase();
    const matchesKeyword = keyword === "" ||
      [source.name, source.index_url, source.credential_name ?? ""].some((value) =>
        value.toLowerCase().includes(keyword),
      );
    const matchesKind = filters.kind === "all" || source.kind === filters.kind;
    const matchesDefault = filters.defaultOnly === "all" ||
      (filters.defaultOnly === "default" ? source.is_default : !source.is_default);
    return matchesKeyword && matchesKind && matchesDefault;
  });

  const columns: ProColumns<PackageSource>[] = [
    {
      title: t("labels.type", { ns: "common" }),
      dataIndex: "kind",
      width: 90,
      render: (_, source) => packageSourceKindLabel(source.kind),
    },
    {
      title: t("labels.name", { ns: "common" }),
      dataIndex: "name",
      width: 190,
      render: (_, source) => (
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
      render: (_, source) => (
        <Tooltip title={source.index_url}>
          <span className="package-source-cell" title={source.index_url}>{source.index_url}</span>
        </Tooltip>
      ),
    },
    {
      title: t("labels.accessCredential", { ns: "common" }),
      dataIndex: "credential_name",
      width: 140,
      render: (_, source) => source.credential_name ?? "—",
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
      width: 104,
      render: (_, source) => (
        <Space size={0} className="package-source-row-actions">
          <Tooltip title={t("packageSources.setDefaultAria", { name: source.name })}>
            <Button
              type="text"
              size="small"
              icon={<StarOutlined aria-hidden="true" />}
              data-testid="set-default-source"
              aria-label={t("packageSources.setDefaultAria", { name: source.name })}
              disabled={source.is_default}
              onClick={() => void handleSetDefault(source)}
            />
          </Tooltip>
          <Tooltip title={t("packageSources.deleteAria", { name: source.name })}>
            <Button
              type="text"
              size="small"
              danger
              icon={<DeleteOutlined aria-hidden="true" />}
              data-testid="delete-package-source"
              aria-label={t("packageSources.deleteAria", { name: source.name })}
              onClick={() => void handleDelete(source)}
            />
          </Tooltip>
        </Space>
      ),
    },
  ];

  const restoreMenuItems = kinds.map((kind) => {
    const canonical = defaults?.[kind];
    return {
      key: kind,
      disabled: restoring !== null || canonical === undefined,
      label: (
        <span
          data-testid={`restore-default-${kind}`}
          title={canonical === undefined ? t("packageSources.emptyDefaults") : undefined}
        >
          {t("packageSources.restoreButton", { kind: kindLabel(kind) })}
        </span>
      ),
    };
  });

  return (
    <div className="settings-panel" data-testid="package-sources-panel">
      <div className="settings-page-toolbar" data-testid="package-sources-toolbar" role="toolbar">
        <div className="settings-toolbar-filters" data-testid="package-sources-filters">
          <Input
            allowClear
            prefix={<SearchOutlined aria-hidden="true" />}
            placeholder={t("packageSources.filterKeyword")}
            aria-label={t("packageSources.filterKeyword")}
            value={filters.keyword}
            onChange={(event) => setFilters((current) => ({ ...current, keyword: event.target.value }))}
            data-settings-filter="true"
          />
          <Select<PackageSourceKind | "all">
            aria-label={t("packageSources.filterKind")}
            value={filters.kind}
            onChange={(kind) => setFilters((current) => ({ ...current, kind }))}
            options={[
              { value: "all", label: t("packageSources.filterAll") },
              ...kinds.map((kind) => ({ value: kind, label: kindLabel(kind) })),
            ]}
            data-settings-filter="true"
          />
          <Select<PackageSourceFilters["defaultOnly"]>
            aria-label={t("packageSources.filterDefault")}
            value={filters.defaultOnly}
            onChange={(defaultOnly) => setFilters((current) => ({ ...current, defaultOnly }))}
            options={[
              { value: "all", label: t("packageSources.filterAll") },
              { value: "default", label: t("labels.default", { ns: "common" }) },
              { value: "custom", label: t("packageSources.filterCustom") },
            ]}
            data-settings-filter="true"
          />
        </div>
        <div className="settings-toolbar-actions">
          <Button
            type="primary"
            icon={<PlusOutlined aria-hidden="true" />}
            data-testid="new-package-source"
            onClick={() => {
              setNotice(null);
              setForm(EMPTY_SOURCE_FORM);
              sourceForm.resetFields();
              sourceForm.setFieldsValue(EMPTY_SOURCE_FORM);
              setFormOpen(true);
            }}
          >
            {t("packageSources.new")}
          </Button>
          <Button
            icon={<ReloadOutlined aria-hidden="true" />}
            data-testid="refresh-package-sources"
            loading={loading}
            onClick={() => {
              setNotice(null);
              void load().then((ok) => ok && setNotice(t("packageSources.refreshList")));
            }}
          >
            {t("actions.refresh", { ns: "common" })}
          </Button>
          <Dropdown
            menu={{
              items: restoreMenuItems,
              onClick: ({ key }) => {
                if (kinds.includes(key as PackageSourceKind)) {
                  void handleRestoreDefault(key as PackageSourceKind);
                }
              },
            }}
            placement="bottomRight"
            trigger={["click"]}
          >
            <Button
              icon={<DownOutlined aria-hidden="true" />}
              data-testid="restore-default-menu"
              aria-label={t("packageSources.restoreMenu")}
            >
              {t("packageSources.restoreMenu")}
            </Button>
          </Dropdown>
        </div>
      </div>

      <div className="settings-package-source-defaults" data-testid="package-source-defaults">
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

      <ModalForm<PackageSourceFormState>
        key={`package-source-form-${formOpen}`}
        form={sourceForm}
        title={t("packageSources.new")}
        open={formOpen}
        initialValues={form}
        modalProps={{ destroyOnHidden: true, onCancel: () => setFormOpen(false) }}
        submitter={{
          render: (submitterProps) => [
            <Button
              key="submit"
              type="primary"
              data-testid="submit-package-source"
              loading={submitting}
              onClick={submitterProps.submit}
            >
              {t("actions.create", { ns: "common" })}
            </Button>,
            <Button key="cancel" onClick={() => setFormOpen(false)} disabled={submitting}>
              {t("actions.cancel", { ns: "common" })}
            </Button>,
          ],
        }}
        onValuesChange={(changed, values) => {
          const kindChanged = changed.kind !== undefined && values.kind !== form.kind;
          if (kindChanged) {
            sourceForm.setFieldValue("credential_id", null);
          }
          setForm((current) => {
            const nextKind = values.kind ?? current.kind;
            return {
              ...current,
              name: values.name ?? current.name,
              kind: nextKind,
              index_url: values.index_url ?? current.index_url,
              is_default: values.is_default ?? current.is_default,
              credential_id: kindChanged || nextKind !== current.kind
                ? null
                : values.credential_id ?? null,
            };
          });
        }}
        onFinish={handleSubmit}
      >
        <div className="settings-inline-form" data-testid="package-source-form">
          <Form.Item name="name" noStyle>
            <Input
              data-testid="package-source-name"
              aria-label={t("packageSources.name")}
              placeholder={t("labels.name", { ns: "common" })}
            />
          </Form.Item>
          <Form.Item name="kind" noStyle>
            <Select<PackageSourceKind>
              data-testid="package-source-kind"
              aria-label={t("packageSources.kind")}
              style={{ minWidth: 180 }}
              options={kinds.map((kind) => ({ label: kindLabel(kind), value: kind }))}
            />
          </Form.Item>
          <Form.Item name="index_url" noStyle>
            <Input
              data-testid="package-source-url"
              aria-label={t("packageSources.repositoryUrl")}
              placeholder={t("packageSources.urlPlaceholder")}
            />
          </Form.Item>
          <Form.Item name="is_default" valuePropName="checked" noStyle>
            <Checkbox data-testid="package-source-default">
              {t("packageSources.defaultCheckbox")}
            </Checkbox>
          </Form.Item>
          <Form.Item name="credential_id" noStyle>
            <Select<number>
              data-testid="package-source-credential"
              aria-label={t("packageSources.credential")}
              placeholder={t("packageSources.credentialPlaceholder")}
              allowClear
              style={{ minWidth: 220 }}
              options={credentials
                .filter((credential) =>
                  form.kind === "npm"
                    ? credential.type === "password" || credential.type === "token"
                    : credential.type === "password",
                )
                .map((credential) => ({ label: credential.name, value: credential.id }))}
            />
          </Form.Item>
        </div>
      </ModalForm>

      {loading ? (
        <Spin />
      ) : sources.length === 0 ? (
        <Empty description={t("empty.noPackageSources", { ns: "common" })} />
      ) : (
        <ProTable<PackageSource>
          rowKey="id"
          size="small"
          search={false}
          options={false}
          pagination={{ pageSize: 8, showSizeChanger: true }}
          dataSource={visibleSources}
          className="package-source-table"
          tableLayout="fixed"
          columns={columns}
        />
      )}
    </div>
  );
}

// --- 知识库 -------------------------------------------------------------------

interface KnowledgeSourceFormState {
  enabled: boolean;
  credential_id: number | null;
}

const EMPTY_KNOWLEDGE_SOURCE_FORM: KnowledgeSourceFormState = {
  enabled: true,
  credential_id: null,
};

type KnowledgeSourceDisplayStatus =
  | KnowledgeSource["status"]
  | "connected"
  | "error";

function KnowledgeSourcesPanel(props: {
  active: boolean;
  onError: (message: string) => void;
  onSaved?: () => void;
}) {
  const { t } = useTranslation(["settings", "common"]);
  const { active, onError, onSaved } = props;
  const [source, setSource] = useState<KnowledgeSource | null>(null);
  const [credentials, setCredentials] = useState<Credential[]>([]);
  const [form, setForm] = useState<KnowledgeSourceFormState>(EMPTY_KNOWLEDGE_SOURCE_FORM);
  const [knowledgeBases, setKnowledgeBases] = useState<KnowledgeBase[]>([]);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [panelError, setPanelError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [testStatus, setTestStatus] = useState<KnowledgeSourceDisplayStatus | null>(null);
  const [testErrorCode, setTestErrorCode] = useState<string | null>(null);

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
      const [sourceResult, credentialList] = await Promise.all([
        api.getKnowledgeSource("ima"),
        api.listCredentials(),
      ]);
      setSource(sourceResult);
      setCredentials(credentialList);
      setForm({ enabled: sourceResult.enabled, credential_id: sourceResult.credential_id });
      setTestStatus(null);
      setTestErrorCode(null);
      setKnowledgeBases([]);
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
    if (!active) {
      return;
    }
    // 仅在用户打开知识库 Tab 时加载，避免隐藏面板在每个设置测试中触发网络请求。
    // eslint-disable-next-line react-hooks/set-state-in-effect -- 打开 Tab 时有意异步同步服务端配置
    void load();
  }, [active, load]);

  useEffect(() => {
    if (!active) {
      return undefined;
    }
    return subscribeCredentialCatalog(() => {
      void api
        .listCredentials()
        .then((credentialList) => setCredentials(credentialList))
        .catch((error) => fail(errorMessage(error)));
    });
  }, [active, fail]);

  async function handleSave(): Promise<boolean> {
    if (saving || source === null) {
      return false;
    }
    setSaving(true);
    setPanelError(null);
    setNotice(null);
    try {
      const updated = await api.updateKnowledgeSource("ima", {
        enabled: form.enabled,
        credential_id: form.credential_id,
      });
      setSource(updated);
      setForm({ enabled: updated.enabled, credential_id: updated.credential_id });
      setTestStatus(null);
      setTestErrorCode(null);
      setKnowledgeBases([]);
      setNotice(t("knowledgeSources.saved"));
      onSaved?.();
      return true;
    } catch (error) {
      fail(errorMessage(error));
      return false;
    } finally {
      setSaving(false);
    }
  }

  async function handleTest() {
    if (testing || source === null || !form.enabled) {
      return;
    }
    setTesting(true);
    setPanelError(null);
    setNotice(null);
    try {
      const result = await api.testKnowledgeSource("ima");
      setTestStatus(result.status);
      setTestErrorCode(result.error_code);
      setKnowledgeBases(result.knowledge_bases);
      if (result.ok) {
        setNotice(t("knowledgeSources.testSucceeded"));
      }
    } catch (error) {
      setTestStatus("error");
      setTestErrorCode(error instanceof Error ? "network_error" : "unknown_error");
      fail(errorMessage(error));
    } finally {
      setTesting(false);
    }
  }

  const displayedStatus: KnowledgeSourceDisplayStatus = testStatus ?? source?.status ?? "unconfigured";
  const statusColor = displayedStatus === "connected"
    ? "green"
    : displayedStatus === "error"
      ? "red"
      : displayedStatus === "unconfigured"
        ? "gold"
        : displayedStatus === "disabled"
          ? "default"
          : "blue";
  const displayedError = testErrorCode === null
    ? null
    : t(`knowledgeSources.errorCodes.${testErrorCode}`, {
      defaultValue: t("knowledgeSources.testFailed"),
    });

  return (
    <div className="settings-panel knowledge-sources-panel" data-testid="knowledge-sources-panel">
      {loading ? (
        <Spin />
      ) : source === null ? null : (
        <>
          <div className="knowledge-source-summary" data-testid="knowledge-source-summary">
            <div>
              <Typography.Text strong>{source.name}</Typography.Text>
              <Tag color={statusColor} data-testid="knowledge-source-status">
                {t(`knowledgeSources.status.${displayedStatus}`)}
              </Tag>
            </div>
            <Typography.Text type="secondary">
              {t("knowledgeSources.endpointLabel")}: <code data-testid="knowledge-source-endpoint">{source.endpoint}</code>
            </Typography.Text>
            <Typography.Text type="secondary">{t("knowledgeSources.endpointNotice")}</Typography.Text>
          </div>

          <ProForm<KnowledgeSourceFormState>
            className="settings-inline-form knowledge-source-form wave-c-form"
            data-testid="knowledge-source-form"
            layout="vertical"
            submitter={false}
            onFinish={handleSave}
          >
            <ProForm.Item label={t("knowledgeSources.enabled")}>
              <Checkbox
                data-testid="knowledge-source-enabled"
                checked={form.enabled}
                onChange={(event) => {
                  setForm((current) => ({ ...current, enabled: event.target.checked }));
                  setTestStatus(null);
                  setTestErrorCode(null);
                }}
              >
                {t("knowledgeSources.enabled")}
              </Checkbox>
            </ProForm.Item>
            <ProForm.Item label={t("knowledgeSources.credential")}>
              <Select<number>
                data-testid="knowledge-source-credential"
                aria-label={t("knowledgeSources.credential")}
                placeholder={t("knowledgeSources.credentialPlaceholder")}
                allowClear
                style={{ minWidth: 260 }}
                value={form.credential_id ?? undefined}
                options={credentials
                  .filter((credential) => credential.type === "access_key")
                  .map((credential) => ({ label: credential.name, value: credential.id }))}
                onChange={(value) => {
                  setForm((current) => ({ ...current, credential_id: value ?? null }));
                  setTestStatus(null);
                  setTestErrorCode(null);
                }}
              />
            </ProForm.Item>
          </ProForm>

          <div
            className="knowledge-source-actions"
            data-testid="knowledge-source-actions"
            role="toolbar"
            aria-label={t("knowledgeSources.actions")}
          >
            <Button
              data-testid="test-knowledge-source"
              loading={testing}
              disabled={!form.enabled || testing}
              onClick={() => void handleTest()}
            >
              {t("knowledgeSources.test")}
            </Button>
            <Button
              type="primary"
              data-testid="save-knowledge-source"
              loading={saving}
              onClick={() => void handleSave()}
            >
              {t("knowledgeSources.save")}
            </Button>
          </div>

          <Typography.Text type="secondary">{t("knowledgeSources.credentialNotice")}</Typography.Text>
          {panelError !== null && <p className="settings-panel-error" role="alert">{panelError}</p>}
          {displayedError !== null && (
            <p className="settings-panel-error" role="alert" data-testid="knowledge-source-test-error">
              {displayedError} ({testErrorCode})
            </p>
          )}
          {notice !== null && <p className="settings-panel-success" role="status">{notice}</p>}

          <div className="knowledge-source-bases" data-testid="knowledge-source-bases">
            <Typography.Title level={5}>{t("knowledgeSources.knowledgeBasesTitle")}</Typography.Title>
            {knowledgeBases.length === 0 ? (
              <Typography.Text type="secondary">{t("knowledgeSources.noKnowledgeBases")}</Typography.Text>
            ) : (
              <ProTable<KnowledgeBase>
                rowKey="id"
                size="small"
                search={false}
                options={false}
                pagination={{ pageSize: 8, showSizeChanger: true }}
                dataSource={knowledgeBases}
                columns={[
                  { title: t("knowledgeSources.knowledgeBaseName"), dataIndex: "name" },
                  {
                    title: t("knowledgeSources.knowledgeBaseStatus"),
                    dataIndex: "status",
                    render: () => <Tag color="green">{t("knowledgeSources.status.accessible")}</Tag>,
                  },
                ]}
              />
            )}
          </div>
        </>
      )}
    </div>
  );
}

// --- 抽屉外壳 -----------------------------------------------------------------

interface SystemSettingsDrawerProps {
  open: boolean;
  onClose: () => void;
  category?: SettingsCategory;
  onCategoryChange?: (category: SettingsCategory) => void;
}

// Settings panels render their own persistent alert next to the failed action.
// Keep those errors local so the same message is not duplicated in the global
// Console banner while the Drawer is open.
function keepErrorInline(): void {}

function SystemLocaleControl() {
  const { i18n, t } = useTranslation("settings");
  const currentUiLocale = resolveSystemLocale(i18n.resolvedLanguage ?? i18n.language);
  const deploymentLocale = readCachedSystemLocale();
  const [updating, setUpdating] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleChange(nextLocale: SystemLocale) {
    if (updating || nextLocale === deploymentLocale) {
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
      setError(userErrorMessage(err, t("localeUpdateFailed"), currentUiLocale));
    } finally {
      setUpdating(false);
    }
  }

  return (
    <div className="settings-locale-control" data-testid="system-locale-control">
      <ProForm className="settings-locale-form" layout="vertical" submitter={false}>
        <ProForm.Item label={t("language")}>
          <Select
            aria-label={t("language")}
            data-testid="system-locale-select"
            value={deploymentLocale}
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
        </ProForm.Item>
      </ProForm>
      {error !== null && <p className="settings-panel-error" role="alert">{error}</p>}
    </div>
  );
}

export default function SystemSettingsDrawer(props: SystemSettingsDrawerProps) {
  const { t } = useTranslation("settings");
  const standalone = props.category === undefined;
  const [localActiveCategory, setLocalActiveCategory] = useState<SettingsCategory>(props.category ?? "credentials");
  const [dirty, setDirty] = useState(false);
  const activeCategory = props.category ?? localActiveCategory;

  if (!props.open) {
    return null;
  }

  function confirmLeave(): boolean {
    if (!dirty) {
      return true;
    }
    return window.confirm(t("confirm.unsavedChanges"));
  }

  function selectCategory(nextCategory: SettingsCategory): void {
    if (nextCategory === activeCategory) {
      return;
    }
    if (!confirmLeave()) {
      return;
    }
    setDirty(false);
    setLocalActiveCategory(nextCategory);
    props.onCategoryChange?.(nextCategory);
  }

  function close(): void {
    if (confirmLeave()) {
      props.onClose();
    }
  }

  const categoryItems = [
    { key: "general", label: t("categories.general") },
    { key: "credentials", label: t("categories.credentials") },
    { key: "package-sources", label: t("categories.packageSources") },
    { key: "ai-model", label: t("categories.aiModel") },
    { key: "knowledge-sources", label: t("categories.knowledgeSources") },
  ];
  const categoryCopy: Record<SettingsCategory, { title: string; description: string }> = {
    general: { title: t("categories.general"), description: t("descriptions.general") },
    credentials: { title: t("categories.credentials"), description: t("descriptions.credentials") },
    "package-sources": { title: t("categories.packageSources"), description: t("descriptions.packageSources") },
    "ai-model": { title: t("categories.aiModel"), description: t("descriptions.aiModel") },
    "knowledge-sources": { title: t("categories.knowledgeSources"), description: t("descriptions.knowledgeSources") },
  };
  const copy = categoryCopy[activeCategory];

  function markDirty(event: ChangeEvent<HTMLElement>): void {
    const target = event.target as HTMLElement;
    if (
      target.closest("[data-testid=system-locale-select]") ||
      target.closest("[data-settings-filter=true]")
    ) {
      return;
    }
    setDirty(true);
  }

  return (
    <section className="settings-center" data-testid="system-settings-center" aria-labelledby="system-settings-title">
      <div className="settings-center-header">
        <Button
          type="link"
          icon={<ArrowLeftOutlined aria-hidden="true" />}
          data-testid="settings-back"
          aria-label={t("backToAdapters")}
          onClick={close}
        >
          {t("backToAdapters")}
        </Button>
        <Typography.Title id="system-settings-title" level={2} className="settings-center-title">
          {t("title")}
        </Typography.Title>
      </div>
      <div className="settings-center-layout">
        <nav className="settings-center-nav" aria-label={t("categoryNavigation")}>
          <Menu
            mode="inline"
            selectedKeys={[activeCategory]}
            items={categoryItems}
            onClick={({ key }) => {
              if (typeof key === "string" && categoryItems.some((item) => item.key === key)) {
                selectCategory(key as SettingsCategory);
              }
            }}
          />
        </nav>
        <section
          className="settings-center-content"
          onChangeCapture={markDirty}
          tabIndex={-1}
          aria-labelledby="settings-category-title"
        >
          <div
            className={`settings-center-content-inner settings-content-${activeCategory}`}
            data-testid="settings-category-main"
          >
            <header className="settings-category-header">
              <Typography.Title id="settings-category-title" level={3}>{copy.title}</Typography.Title>
              <Typography.Paragraph type="secondary">{copy.description}</Typography.Paragraph>
            </header>
            {standalone && activeCategory !== "general" && <SystemLocaleControl />}
            {activeCategory === "general" && <SystemLocaleControl />}
            {activeCategory === "credentials" && (
              <CredentialsPanel onError={keepErrorInline} onSaved={() => setDirty(false)} />
            )}
            {activeCategory === "package-sources" && (
              <PackageSourcesPanel onError={keepErrorInline} onSaved={() => setDirty(false)} />
            )}
            {activeCategory === "ai-model" && (
              <AiModelSettingsPanel onError={keepErrorInline} onSaved={() => setDirty(false)} />
            )}
            {activeCategory === "knowledge-sources" && (
              <KnowledgeSourcesPanel active onError={keepErrorInline} onSaved={() => setDirty(false)} />
            )}
          </div>
        </section>
      </div>
    </section>
  );
}
