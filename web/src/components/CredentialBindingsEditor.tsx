/** 凭据绑定编辑器：代码中的凭据名 → 凭据字段（M3.2，全量替换保存语义）。 */

import { useCallback, useEffect, useState } from "react";
import { Alert, Button, Empty, Input, Select, Space, Spin, Typography } from "antd";

import { api } from "../api";
import { credentialFields } from "../credential-fields";
import { subscribeCredentialCatalog } from "../credential-catalog";
import type { Credential } from "../types";
import { userErrorMessage } from "../user-message";

interface BindingRow {
  env_key: string;
  credential_id: number | null;
  field: string;
}

interface CredentialBindingsEditorProps {
  adapterId: number;
  disabled: boolean;
  onError: (message: string | null) => void;
  /** 保存成功后通知父组件（如刷新 Diff 基线）。 */
  onSaved?: () => void;
  /** 打开「系统设置 → 凭据管理」的入口（M5.5.7：不在编辑页重复实现新建表单）。 */
  onOpenSettings?: () => void;
}

function errorMessage(error: unknown): string {
  return userErrorMessage(error);
}

function toRows(bindings: { env_key: string; credential_id: number; field: string }[]): BindingRow[] {
  return bindings.map((binding) => ({
    env_key: binding.env_key,
    credential_id: binding.credential_id,
    field: binding.field,
  }));
}

export default function CredentialBindingsEditor(props: CredentialBindingsEditorProps) {
  const { adapterId, onError } = props;
  const [credentials, setCredentials] = useState<Credential[]>([]);
  const [rows, setRows] = useState<BindingRow[]>([]);
  const [baseline, setBaseline] = useState<BindingRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setNotice(null);
    try {
      const [credentialList, bindingList] = await Promise.all([
        api.listCredentials(),
        api.listAdapterBindings(adapterId),
      ]);
      setCredentials(credentialList);
      const loaded = toRows(bindingList);
      setRows(loaded);
      setBaseline(loaded);
    } catch (error) {
      onError(errorMessage(error));
    } finally {
      setLoading(false);
    }
  }, [adapterId, onError]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- 挂载时拉取凭据与绑定的初始加载是有意的异步同步
    void load();
  }, [load]);

  // 凭据增删改后仅刷新凭据选项（UX-003）；未保存的绑定行保持原样。
  useEffect(
    () =>
      subscribeCredentialCatalog(() => {
        void api
          .listCredentials()
          .then((credentialList) => setCredentials(credentialList))
          .catch((error) => onError(errorMessage(error)));
      }),
    [onError],
  );

  function updateRow(index: number, patch: Partial<BindingRow>) {
    setNotice(null);
    setRows((current) => current.map((row, i) => (i === index ? { ...row, ...patch } : row)));
  }

  function updateRows(updater: (current: BindingRow[]) => BindingRow[]) {
    setNotice(null);
    setRows(updater);
  }

  function handleCredentialChange(index: number, credentialId: number) {
    // 切换凭据后字段集合可能变化：重置为该类型的第一个字段。
    const credential = credentials.find((candidate) => candidate.id === credentialId);
    const fields = credential !== undefined ? credentialFields(credential.type) : [];
    updateRow(index, { credential_id: credentialId, field: fields[0] ?? "" });
  }

  async function handleSave() {
    if (saving || props.disabled) {
      return;
    }
    const envKeys = rows.map((row) => row.env_key.trim());
    if (envKeys.some((envKey) => envKey === "")) {
      props.onError("每行绑定都必须填写代码中的凭据名");
      return;
    }
    if (new Set(envKeys).size !== envKeys.length) {
      props.onError("代码中的凭据名不能重复");
      return;
    }
    if (rows.some((row) => row.credential_id === null || row.field === "")) {
      props.onError("每行绑定都必须选择凭据与字段");
      return;
    }
    props.onError(null);
    setSaving(true);
    try {
      const saved = await api.setAdapterBindings(
        props.adapterId,
        rows.map((row) => ({
          env_key: row.env_key.trim(),
          credential_id: row.credential_id as number,
          field: row.field,
        })),
      );
      const refreshed = toRows(saved);
      setRows(refreshed);
      setBaseline(refreshed);
      setNotice("凭据绑定已保存");
      props.onSaved?.();
    } catch (error) {
      setNotice(null);
      props.onError(errorMessage(error));
    } finally {
      setSaving(false);
    }
  }

  const dirty = JSON.stringify(rows) !== JSON.stringify(baseline);
  // M5.5.7：只要用户改过“代码中的凭据名”，就提示代码引用需要同步修改，
  // 不允许 UI 改名后代码静默失效。
  const envKeyRenamed = rows.some((row, index) => {
    const baselineRow = baseline[index];
    return baselineRow !== undefined && row.env_key.trim() !== baselineRow.env_key.trim();
  });

  if (loading) {
    return <Spin />;
  }

  return (
    <div className="binding-editor" data-testid="credential-bindings">
      <Typography.Paragraph type="secondary" className="binding-editor-help">
        代码中的凭据名用于脚本读取绑定后的敏感信息，例如{" "}
        <code>context.secrets.get(&quot;PASSWORD&quot;)</code>；绑定将凭据字段以{" "}
        <code>DLR_SECRET_{"{env_key}"}</code> 注入该 Adapter 的执行环境，保存为全量替换。
      </Typography.Paragraph>
      <Typography.Paragraph type="secondary" className="binding-editor-help">
        如需新增凭据，请前往「系统设置 → 凭据管理」新建。
        {props.onOpenSettings !== undefined && (
          <Button
            size="small"
            type="link"
            data-testid="open-settings-for-credentials"
            onClick={props.onOpenSettings}
          >
            打开系统设置
          </Button>
        )}
      </Typography.Paragraph>
      {envKeyRenamed && (
        <Alert
          type="warning"
          showIcon
          role="alert"
          data-testid="binding-rename-hint"
          message="修改代码中的凭据名后，代码中的引用也需要保持一致。"
        />
      )}
      {notice !== null && <p className="settings-panel-success" role="status">{notice}</p>}
      {rows.length === 0 ? (
        <Empty description="暂无凭据绑定" />
      ) : (
        <div className="binding-rows">
          {rows.map((row, index) => {
            const credential = credentials.find((candidate) => candidate.id === row.credential_id);
            const fieldOptions =
              credential !== undefined
                ? credentialFields(credential.type).map((field) => ({ label: field, value: field }))
                : [];
            return (
              <Space
                key={index}
                className="binding-row"
                data-testid="binding-row"
                role="group"
                aria-label={`绑定 ${index + 1}`}
              >
                <Input
                  data-testid="binding-env-key"
                  aria-label={`绑定 ${index + 1} 代码中的凭据名`}
                  placeholder="代码中的凭据名（如 DB_PASSWORD）"
                  value={row.env_key}
                  disabled={props.disabled || saving}
                  onChange={(event) => updateRow(index, { env_key: event.target.value })}
                />
                <Select
                  data-testid="binding-credential"
                  aria-label={`绑定 ${index + 1} 凭据`}
                  placeholder="选择凭据"
                  style={{ minWidth: 160 }}
                  value={row.credential_id ?? undefined}
                  disabled={props.disabled || saving}
                  options={credentials.map((candidate) => ({
                    label: candidate.name,
                    value: candidate.id,
                  }))}
                  onChange={(value) => handleCredentialChange(index, value)}
                />
                <Select
                  data-testid="binding-field"
                  aria-label={`绑定 ${index + 1} 字段`}
                  placeholder="字段"
                  style={{ minWidth: 120 }}
                  value={row.field !== "" ? row.field : undefined}
                  disabled={props.disabled || saving || fieldOptions.length === 0}
                  options={fieldOptions}
                  onChange={(value) => updateRow(index, { field: value })}
                />
                <Button
                  danger
                  data-testid="remove-binding"
                  aria-label={`删除绑定 ${index + 1}`}
                  disabled={props.disabled || saving}
                  onClick={() => updateRows((current) => current.filter((_, i) => i !== index))}
                >
                  删除
                </Button>
              </Space>
            );
          })}
        </div>
      )}
      <Space className="binding-actions">
        <Button
          data-testid="add-binding"
          disabled={props.disabled || saving}
          onClick={() =>
            updateRows((current) => [
              ...current,
              { env_key: "", credential_id: null, field: "" },
            ])
          }
        >
          添加绑定
        </Button>
        <Button
          type="primary"
          data-testid="save-bindings"
          loading={saving}
          disabled={props.disabled || !dirty}
          onClick={() => void handleSave()}
        >
          保存绑定
        </Button>
      </Space>
    </div>
  );
}
