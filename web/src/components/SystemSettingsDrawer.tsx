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
  Typography,
} from "antd";
import type { ColumnsType } from "antd/es/table";

import { ApiError, api } from "../api";
import { CREDENTIAL_TYPE_FIELDS, CREDENTIAL_TYPE_LABELS, credentialFields } from "../credential-fields";
import type { Credential, CredentialType, PackageSource } from "../types";
import AiModelSettingsPanel from "./AiModelSettingsPanel";

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    return `${error.message} (${error.code})`;
  }
  return "请求失败";
}

// --- 凭据管理 ---------------------------------------------------------------

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
      fail("凭据名称不能为空");
      return;
    }
    const required = credentialFields(form.type);
    const fields: Record<string, string> = {};
    for (const key of required) {
      const value = (form.fields[key] ?? "").trim();
      if (value === "") {
        fail(`字段 ${key} 不能为空`);
        return;
      }
      fields[key] = value;
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
      const operation = form.editingId === null ? "凭据已创建" : "凭据已更新";
      if (await load()) {
        setNotice(operation);
      } else {
        setPanelError(`${operation}，但刷新列表失败；请手动刷新确认，避免重复提交。`);
      }
    } catch (error) {
      fail(errorMessage(error));
    } finally {
      setSubmitting(false);
    }
  }

  async function handleDelete(credential: Credential) {
    if (!window.confirm(`确定删除凭据 “${credential.name}” 吗？引用它的绑定与包源将失效。`)) {
      return;
    }
    try {
      setPanelError(null);
      setNotice(null);
      await api.deleteCredential(credential.id);
      if (await load()) {
        setNotice("凭据已删除");
      } else {
        setPanelError("凭据已删除，但刷新列表失败；请手动刷新确认。");
      }
    } catch (error) {
      fail(errorMessage(error));
    }
  }

  const formFieldKeys = credentialFields(form.type);

  return (
    <div className="settings-panel" data-testid="credentials-panel">
      <Space className="settings-panel-toolbar">
        <Button type="primary" data-testid="new-credential" onClick={openCreate}>
          新建凭据
        </Button>
        <Button
          data-testid="refresh-credentials"
          loading={loading}
          onClick={() => {
            setNotice(null);
            void load().then((ok) => ok && setNotice("凭据列表已刷新"));
          }}
        >
          刷新
        </Button>
      </Space>
      <Typography.Text type="secondary">这里只展示凭据元数据；Secret 真值不会回显到浏览器。</Typography.Text>
      {panelError !== null && <p className="settings-panel-error" role="alert">{panelError}</p>}
      {notice !== null && <p className="settings-panel-success" role="status">{notice}</p>}

      {formOpen && (
        <div className="settings-inline-form" data-testid="credential-form">
          <Input
            data-testid="credential-name"
            aria-label="凭据名称"
            placeholder="名称"
            value={form.name}
            onChange={(event) => setForm((current) => ({ ...current, name: event.target.value }))}
          />
          <Select
            data-testid="credential-type"
            aria-label="凭据类型"
            style={{ minWidth: 200 }}
            value={form.type}
            disabled={form.editingId !== null}
            options={Object.keys(CREDENTIAL_TYPE_FIELDS).map((type) => ({
              label: `${CREDENTIAL_TYPE_LABELS[type] ?? type}（${credentialFields(type).join(" + ")}）`,
              value: type,
            }))}
            onChange={(value) => handleTypeChange(value)}
          />
          {formFieldKeys.map((key) => (
            <Input.Password
              key={key}
              data-testid={`credential-field-${key}`}
              aria-label={`凭据字段 ${key}`}
              placeholder={key}
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
              {form.editingId === null ? "创建" : "更新"}
            </Button>
            <Button onClick={() => setFormOpen(false)}>取消</Button>
          </Space>
          {form.editingId !== null && (
            <Typography.Text type="secondary">
              更新将重新加密并替换已保存的值；留空校验必填。
            </Typography.Text>
          )}
        </div>
      )}

      {loading ? (
        <Spin />
      ) : credentials.length === 0 ? (
        <Empty description="暂无凭据" />
      ) : (
        <Table<Credential>
          rowKey="id"
          size="small"
          pagination={false}
          dataSource={credentials}
          columns={[
            { title: "名称", dataIndex: "name", render: (name: string) => <span data-testid="credential-row">{name}</span> },
            {
              title: "类型",
              dataIndex: "type",
              width: 120,
              render: (type: string) => `${CREDENTIAL_TYPE_LABELS[type] ?? type}`,
            },
            {
              title: "操作",
              width: 160,
              render: (_, credential) => (
                <Space>
                  <Button
                    size="small"
                    data-testid="update-credential"
                    onClick={() => openUpdate(credential)}
                  >
                    更新
                  </Button>
                  <Button
                    size="small"
                    danger
                    data-testid="delete-credential"
                    onClick={() => void handleDelete(credential)}
                  >
                    删除
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

interface PackageSourceFormState {
  name: string;
  kind: "pypi" | "npm" | "maven";
  index_url: string;
  is_default: boolean;
  credential_id: number | null;
}

const EMPTY_SOURCE_FORM: PackageSourceFormState = {
  name: "",
  kind: "pypi",
  index_url: "",
  is_default: false,
  credential_id: null,
};

function PackageSourcesPanel(props: { onError: (message: string) => void }) {
  const { onError } = props;
  const [sources, setSources] = useState<PackageSource[]>([]);
  const [credentials, setCredentials] = useState<Credential[]>([]);
  const [loading, setLoading] = useState(true);
  const [formOpen, setFormOpen] = useState(false);
  const [form, setForm] = useState<PackageSourceFormState>(EMPTY_SOURCE_FORM);
  const [submitting, setSubmitting] = useState(false);
  const [testing, setTesting] = useState<number | null>(null);
  const [testResults, setTestResults] = useState<Map<number, { ok: boolean; text: string }>>(new Map());
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
      const [sourceList, credentialList] = await Promise.all([
        api.listPackageSources(),
        api.listCredentials(),
      ]);
      setSources(sourceList);
      setCredentials(credentialList);
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

  async function handleSubmit() {
    if (submitting) {
      return;
    }
    const name = form.name.trim();
    const indexUrl = form.index_url.trim();
    if (name === "" || indexUrl === "") {
      fail("包源名称与 index URL 均不能为空");
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
        setNotice("依赖源已创建");
      } else {
        setPanelError("依赖源已创建，但刷新列表失败；请手动刷新确认，避免重复提交。");
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
        setNotice(`${source.name} 已设为默认依赖源`);
      } else {
        setPanelError(`${source.name} 已设为默认依赖源，但刷新列表失败；请手动刷新确认。`);
      }
    } catch (error) {
      fail(errorMessage(error));
    }
  }

  async function handleDelete(source: PackageSource) {
    if (!window.confirm(`确定删除包源 “${source.name}” 吗？`)) {
      return;
    }
    try {
      setPanelError(null);
      setNotice(null);
      await api.deletePackageSource(source.id);
      if (await load()) {
        setNotice("依赖源已删除");
      } else {
        setPanelError("依赖源已删除，但刷新列表失败；请手动刷新确认。");
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
      const text = result.ok
        ? `可达${result.status_code !== null ? `（HTTP ${result.status_code}）` : ""}`
        : `不可达${result.error !== null ? `：${result.error}` : ""}`;
      setTestResults((current) => new Map(current).set(source.id, { ok: result.ok, text }));
    } catch (error) {
      fail(errorMessage(error));
    } finally {
      setTesting(null);
    }
  }

  const columns: ColumnsType<PackageSource> = [
    {
      title: "类型",
      dataIndex: "kind",
      width: 90,
      render: (kind: PackageSource["kind"]) =>
        ({ pypi: "PyPI", npm: "npm", maven: "Maven" })[kind],
    },
    {
      title: "名称",
      dataIndex: "name",
      render: (name: string, source) => (
        <span data-testid="package-source-row">
          {name}
          {source.is_default && (
            <Tag color="green" data-testid="default-source-badge">
              默认
            </Tag>
          )}
        </span>
      ),
    },
    { title: "Repository URL", dataIndex: "index_url" },
    {
      title: "凭据",
      dataIndex: "credential_name",
      width: 110,
      render: (name: string | null) => name ?? "—",
    },
    {
      title: "可达性",
      width: 160,
      render: (_, source) => {
        const result = testResults.get(source.id);
        return (
          <Space>
            <Button
              size="small"
              data-testid="test-package-source"
              loading={testing === source.id}
              disabled={testing !== null}
              onClick={() => void handleTest(source)}
            >
              测试
            </Button>
            {result !== undefined && (
              <Typography.Text
                type={result.ok ? "success" : "danger"}
                role={result.ok ? "status" : "alert"}
                data-testid="package-source-test-result"
              >
                {result.text}
              </Typography.Text>
            )}
          </Space>
        );
      },
    },
    {
      title: "操作",
      width: 170,
      render: (_, source) => (
        <Space>
          <Button
            size="small"
            data-testid="set-default-source"
            disabled={source.is_default}
            onClick={() => void handleSetDefault(source)}
          >
            设为默认
          </Button>
          <Button
            size="small"
            danger
            data-testid="delete-package-source"
            onClick={() => void handleDelete(source)}
          >
            删除
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
          新建依赖源
        </Button>
        <Button
          data-testid="refresh-package-sources"
          loading={loading}
          onClick={() => {
            setNotice(null);
            void load().then((ok) => ok && setNotice("依赖源列表已刷新"));
          }}
        >
          刷新
        </Button>
      </Space>
      <Typography.Text type="secondary">
        每种类型最多一个默认源；Worker 会先尝试本地缓存，再使用对应语言的默认依赖源。
      </Typography.Text>
      {panelError !== null && <p className="settings-panel-error" role="alert">{panelError}</p>}
      {notice !== null && <p className="settings-panel-success" role="status">{notice}</p>}

      {formOpen && (
        <div className="settings-inline-form" data-testid="package-source-form">
          <Input
            data-testid="package-source-name"
            aria-label="依赖源名称"
            placeholder="名称"
            value={form.name}
            onChange={(event) => setForm((current) => ({ ...current, name: event.target.value }))}
          />
          <Select
            data-testid="package-source-kind"
            aria-label="依赖源类型"
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
            aria-label="依赖源 Repository URL"
            placeholder="Repository URL"
            value={form.index_url}
            onChange={(event) => setForm((current) => ({ ...current, index_url: event.target.value }))}
          />
          <Checkbox
            data-testid="package-source-default"
            checked={form.is_default}
            onChange={(event) => setForm((current) => ({ ...current, is_default: event.target.checked }))}
          >
            设为该类型的默认源
          </Checkbox>
          <Select
            data-testid="package-source-credential"
            aria-label="依赖源凭据"
            placeholder="凭据（可选）"
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
              创建
            </Button>
            <Button onClick={() => setFormOpen(false)}>取消</Button>
          </Space>
        </div>
      )}

      {loading ? (
        <Spin />
      ) : sources.length === 0 ? (
        <Empty description="暂无包源" />
      ) : (
        <Table<PackageSource>
          rowKey="id"
          size="small"
          pagination={false}
          dataSource={sources}
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

export default function SystemSettingsDrawer(props: SystemSettingsDrawerProps) {
  return (
    <Drawer
      title="系统设置"
      width={720}
      open={props.open}
      destroyOnHidden
      onClose={props.onClose}
    >
      <Tabs
        items={[
          {
            key: "credentials",
            label: "凭据管理",
            children: <CredentialsPanel onError={keepErrorInline} />,
          },
          {
            key: "package-sources",
            label: "依赖源",
            children: <PackageSourcesPanel onError={keepErrorInline} />,
          },
          {
            key: "ai-model",
            label: "AI 模型",
            children: <AiModelSettingsPanel onError={keepErrorInline} />,
          },
        ]}
      />
    </Drawer>
  );
}
