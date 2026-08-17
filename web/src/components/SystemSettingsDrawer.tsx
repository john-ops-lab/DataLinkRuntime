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

import { api } from "../api";
import { CREDENTIAL_TYPE_FIELDS, CREDENTIAL_TYPE_LABELS, credentialFields } from "../credential-fields";
import { notifyCredentialCatalogChanged, subscribeCredentialCatalog } from "../credential-catalog";
import type {
  Credential,
  CredentialType,
  PackageSource,
  PackageSourceDefaults,
} from "../types";
import { userErrorMessage } from "../user-message";
import AiModelSettingsPanel from "./AiModelSettingsPanel";

function errorMessage(error: unknown): string {
  return userErrorMessage(error);
}

// --- 凭据管理 ---------------------------------------------------------------

// M5.5.7：四类凭据的常驻用户说明，帮助用户在新建前选择正确的类型。
// 与 credential-fields.ts / 后端 CREDENTIAL_FIELDS 保持一致。
const CREDENTIAL_TYPE_GUIDE: Array<{
  type: CredentialType;
  fields: string;
  scenarios: string;
  hint: string;
}> = [
  {
    type: "password",
    fields: "username + password",
    scenarios: "数据库、SSH、FTP/SFTP、HTTP Basic、设备账号登录",
    hint: "目标系统需要「用户名 + 密码」时选择",
  },
  {
    type: "token",
    fields: "token",
    scenarios: "API Token、Bearer Token、PAT、Webhook Token",
    hint: "目标系统直接提供一串 Token 时选择",
  },
  {
    type: "access_key",
    fields: "access_key_id + access_key_secret",
    scenarios: "阿里云、AWS、腾讯云、对象存储、云平台 API 请求签名",
    hint: "目标系统提供 AK/SK、AccessKey ID/Secret 等成对密钥时选择",
  },
  {
    type: "secret",
    fields: "value（可存放 api_key、client_secret、signing_secret、private_key 等）",
    scenarios: "第三方 API Key、OAuth Client Secret、签名密钥、加密密钥",
    hint: "前三种不匹配时的兜底类型，避免与「访问密钥」混淆",
  },
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
    // M5.5.7：创建前一次性明文提醒。保存后 Secret 真值无法再通过浏览器
    // 查看，因此必须在最终提交前明确告知用户先妥善保存或复制。
    if (form.editingId === null) {
      const confirmed = window.confirm(
        "保存后密码、Token、密钥等敏感内容无法再次通过浏览器查看，请先妥善保存或复制。\n\n确定创建该凭据吗？",
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
      const operation = form.editingId === null ? "凭据已创建" : "凭据已更新";
      if (await load()) {
        setNotice(operation);
      } else {
        setPanelError(`${operation}，但刷新列表失败；请手动刷新确认，避免重复提交。`);
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
        <Typography.Title level={5}>凭据类型说明</Typography.Title>
        <Typography.Paragraph type="secondary">
          不同凭据类型是常见敏感信息结构的模板，帮助你快速选择正确字段。无法确定时，优先选择最接近的类型；仍不匹配时可使用「通用密钥」。
        </Typography.Paragraph>
        <ul className="credential-type-guide-list">
          {CREDENTIAL_TYPE_GUIDE.map((item) => (
            <li key={item.type} data-testid={`credential-type-guide-${item.type}`}>
              <strong>{CREDENTIAL_TYPE_LABELS[item.type] ?? item.type}</strong>（字段：
              {item.fields}）：常见场景为 {item.scenarios}。{item.hint}。
            </li>
          ))}
        </ul>
      </div>
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
      <Typography.Text type="secondary">这里只展示凭据元数据；密钥真值不会回显到浏览器。</Typography.Text>
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

type PackageSourceKind = "pypi" | "npm" | "maven";

function kindLabel(kind: PackageSourceKind): string {
  return { pypi: "PyPI", npm: "npm", maven: "Maven" }[kind];
}

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
  const [defaults, setDefaults] = useState<PackageSourceDefaults | null>(null);
  const [loading, setLoading] = useState(true);
  const [formOpen, setFormOpen] = useState(false);
  const [form, setForm] = useState<PackageSourceFormState>(EMPTY_SOURCE_FORM);
  const [submitting, setSubmitting] = useState(false);
  const [testing, setTesting] = useState<number | null>(null);
  const [restoring, setRestoring] = useState<"pypi" | "npm" | "maven" | null>(null);
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
      fail("包源名称与索引 URL 均不能为空");
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
      const errorDetail = result.error?.trim();
      const text = result.ok
        ? `可达${result.status_code !== null ? `（HTTP ${result.status_code}）` : ""}`
        : errorDetail
          ? `不可达：${errorDetail}`
          : "不可达（请检查 URL、凭据和网络）";
      setTestResults((current) => new Map(current).set(source.id, { ok: result.ok, text }));
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
      fail("默认依赖源信息未加载，请刷新后重试");
      return;
    }
    if (
      !window.confirm(
        `确定把 ${canonical.name}（${canonical.index_url}）恢复为 ${kindLabel(kind)} 的默认依赖源吗？` +
          "已有源会被更新或设为默认；绑定的凭据保持不变。",
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
        setNotice(`${kindLabel(kind)} 已恢复默认依赖源`);
      } else {
        setPanelError(`${kindLabel(kind)} 已恢复默认依赖源，但刷新列表失败；请手动刷新确认。`);
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
    { title: "仓库 URL", dataIndex: "index_url" },
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
        每种类型最多一个默认源；运行节点会先尝试本地缓存，再使用对应语言的默认依赖源。
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
                  title={`恢复为平台默认：${canonical.index_url}`}
                >
                  恢复默认 {kindLabel(kind)}
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
              {kindLabel(kind)} 未配置默认依赖源：Worker 将只使用本地缓存，缓存不足时安装会明确失败，
              不会静默使用未配置的地址；可点击上方「恢复默认 {kindLabel(kind)}」使用平台默认镜像，
              或新建并设为默认。
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
            aria-label="依赖源仓库 URL"
            placeholder="仓库 URL"
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
