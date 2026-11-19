import { useState } from "react";
import { Alert, Button, Drawer, Form, Input, List, Popconfirm, Select, Space, Switch, Tag, Typography } from "antd";
import { useTranslation } from "react-i18next";
import { api } from "../api";
import type { AdapterLanguage, Credential, Worker, WorkerMaintenanceSnapshot } from "../types";
import { userErrorMessage } from "../user-message";
import type { PoolRule, WorkerEnvironment, WorkerPool, WorkerPoolDetail } from "../worker-pool-types";

type PoolFields = Omit<PoolRule, "selector"> & { name: string; enabled: boolean; selector_text: string };
type EnvironmentFields = Omit<WorkerEnvironment, "toolchains"> & { toolchain_text: string };
type MemberFields = {
  enabled: boolean;
  labels_text: string;
  placement_scopes: string[];
  credential_ids: number[];
  material_scopes: string[];
};

const languages: AdapterLanguage[] = ["python", "javascript", "typescript", "java", "go"];
const labelExample = '{"zone":"trusted-a"}';

function stringMap(value: string | undefined, error: string): Record<string, string> {
  let parsed: unknown;
  try {
    parsed = JSON.parse(value?.trim() || "{}");
  } catch {
    throw new Error(error);
  }
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed) ||
    Object.values(parsed).some((item) => typeof item !== "string")) {
    throw new Error(error);
  }
  return parsed as Record<string, string>;
}

export default function WorkerPoolsPanel({ workers, readWorker }: {
  workers: Worker[];
  readWorker?: (id: number) => Promise<WorkerMaintenanceSnapshot>;
}) {
  const { t } = useTranslation(["runtime", "common"]);
  const [open, setOpen] = useState(false);
  const [pools, setPools] = useState<WorkerPool[]>([]);
  const [environments, setEnvironments] = useState<WorkerEnvironment[]>([]);
  const [credentials, setCredentials] = useState<Credential[]>([]);
  const [pool, setPool] = useState<WorkerPoolDetail | null>(null);
  const [memberId, setMemberId] = useState<number>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [creatingEnvironment, setCreatingEnvironment] = useState(false);
  const [previewLanguage, setPreviewLanguage] = useState<AdapterLanguage>("python");
  const [preview, setPreview] = useState<Awaited<ReturnType<typeof api.previewWorkerPoolMembers>>>();
  const member = pool?.members.find((item) => item.worker_id === memberId);
  const initialMember: MemberFields = {
    enabled: member?.enabled ?? true,
    labels_text: member ? JSON.stringify(member.labels) : "{}",
    placement_scopes: member?.placement_scopes ?? [],
    credential_ids: member?.credential_ids ?? [],
    material_scopes: member?.material_scopes ?? [],
  };

  async function run(operation: () => Promise<void>) {
    setBusy(true);
    setError(null);
    try {
      await operation();
    } catch (err) {
      setError(err instanceof Error && err.message === t("pool.objectInvalid")
        ? err.message : userErrorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  function accept(result: WorkerPoolDetail) {
    setPool(result);
    setPools((items) => [...items.filter((row) => row.id !== result.id), result]
      .sort((a, b) => a.name.localeCompare(b.name)));
    setPreview(undefined);
  }

  function choosePool(poolId: number | null) {
    if (poolId === null) {
      setPool(null);
      setMemberId(undefined);
      setPreview(undefined);
      setError(null);
      return;
    }
    void run(async () => {
      accept(await api.getWorkerPool(poolId));
      setMemberId(undefined);
    });
  }

  function show() {
    setOpen(true);
    void run(async () => {
      const [poolRows, rules, references] = await Promise.all([
        api.listWorkerPools(), api.listWorkerEnvironments(), api.listCredentials(),
      ]);
      setPools(poolRows);
      setEnvironments(rules);
      setCredentials(references);
      setMemberId(undefined);
      setPreview(undefined);
      setPool(poolRows.length ? await api.getWorkerPool(poolRows[0].id) : null);
    });
  }

  async function savePool(fields: PoolFields) {
    const rule = {
      selector: stringMap(fields.selector_text, t("pool.objectInvalid")),
      environment_class: fields.environment_class,
      placement_scope: fields.placement_scope,
    };
    accept(await (pool
      ? api.updateWorkerPool(pool.id, { expected_version: pool.version, name: fields.name, enabled: fields.enabled, rule })
      : api.createWorkerPool(fields.name, rule)));
  }

  async function saveMember(fields: MemberFields) {
    if (!pool || !memberId) return;
    const identity = member?.worker_identity_id ?? (await readWorker?.(memberId))?.worker_identity_id;
    if (!identity) {
      setError(t("pool.workerIdentityMissing"));
      return;
    }
    accept(await api.updateWorkerPoolMember(pool.id, memberId, {
      expected_version: pool.version,
      worker_identity_id: identity,
      enabled: fields.enabled,
      labels: stringMap(fields.labels_text, t("pool.objectInvalid")),
      placement_scopes: fields.placement_scopes,
      credential_ids: fields.credential_ids,
      material_scopes: fields.material_scopes,
    }));
  }

  return <>
    <Button size="small" onClick={show}>{t("pool.manage")}</Button>
    <Drawer title={t("pool.manage")} open={open} width="min(680px, 100%)"
      maskClosable={!busy} keyboard={!busy} onClose={() => { if (!busy) setOpen(false); }}>
      <Space direction="vertical" size="large" style={{ width: "100%" }}>
        <Alert type="info" showIcon message={t("pool.membershipPolicy")} />
        {error && <Alert type="error" showIcon message={error} />}
        <Space wrap>
          <Select aria-label={t("pool.title")} value={pool?.id} style={{ minWidth: 240 }} disabled={busy}
            placeholder={t("pool.choose")} onChange={choosePool}
            options={pools.map((item) => ({
              value: item.id,
              label: t("pool.choice", { name: item.name, revision: item.revision.revision,
                state: t(item.enabled ? "pool.enabled" : "pool.disabled") }),
            }))} />
          <Button onClick={() => choosePool(null)} disabled={busy}>{t("pool.new")}</Button>
          <Button onClick={() => setCreatingEnvironment((value) => !value)} disabled={busy}>{t("pool.newEnvironment")}</Button>
        </Space>
        {creatingEnvironment && <Form<EnvironmentFields> layout="vertical" disabled={busy}
          initialValues={{ architecture: "amd64", builtin_materials: [] }}
          onFinish={(fields) => void run(async () => {
            const { toolchain_text, ...rule } = fields;
            const created = await api.createWorkerEnvironment({
              ...rule, toolchains: stringMap(toolchain_text, t("pool.objectInvalid")),
            });
            setEnvironments((items) => [...items, created]);
            setCreatingEnvironment(false);
          })}>
          <Form.Item label={t("pool.environment")} name="name" rules={[{ required: true }]}>
            <Input maxLength={64} />
          </Form.Item>
          <Form.Item label={t("pool.imageDigest")} name="image_digest" rules={[{ required: true }]}>
            <Input maxLength={71} placeholder="sha256:…" />
          </Form.Item>
          <Form.Item label={t("pool.architecture")} name="architecture" rules={[{ required: true }]}>
            <Select options={[{ value: "amd64" }, { value: "arm64" }]} />
          </Form.Item>
          <Form.Item label={t("pool.toolchains")} name="toolchain_text" rules={[{ required: true }]}>
            <Input.TextArea rows={2} placeholder='{"python":"…"}' />
          </Form.Item>
          <Form.Item label={t("pool.materialCapabilities")} name="builtin_materials">
            <Select mode="tags" />
          </Form.Item>
          <Button htmlType="submit" type="primary" loading={busy}>{t("pool.saveEnvironment")}</Button>
        </Form>}
        <Form<PoolFields> key={pool ? `${pool.id}-${pool.version}` : "new"} layout="vertical" disabled={busy}
          initialValues={{
            name: pool?.name, enabled: pool?.enabled ?? true, environment_class: pool?.revision.environment_class,
            placement_scope: pool?.revision.placement_scope, selector_text: JSON.stringify(pool?.revision.selector ?? {}),
          }}
          onFinish={(fields) => void run(() => savePool(fields))}>
          <Form.Item label={t("pool.name")} name="name" rules={[{ required: true }]}>
            <Input maxLength={128} />
          </Form.Item>
          {pool && <Form.Item label={t("pool.enabled")} name="enabled" valuePropName="checked"><Switch /></Form.Item>}
          <Form.Item label={t("pool.environment")} name="environment_class" rules={[{ required: true }]}>
            <Select options={environments.map(({ name }) => ({ value: name, label: name }))} />
          </Form.Item>
          <Form.Item label={t("pool.scope")} name="placement_scope" rules={[{ required: true }]}>
            <Input maxLength={64} />
          </Form.Item>
          <Form.Item label={t("pool.selector")} name="selector_text">
            <Input.TextArea rows={2} placeholder={labelExample} />
          </Form.Item>
          <Button htmlType="submit" type="primary" loading={busy}>{t("pool.save")}</Button>
        </Form>
        {pool && <>
          <div>
            <Typography.Title level={5}>{t("pool.members")}</Typography.Title>
            <Select aria-label={t("pool.memberWorker")} value={memberId} onChange={setMemberId}
              style={{ width: "100%" }} disabled={busy} placeholder={t("pool.memberWorker")}
              options={Array.from(new Set([...workers.filter((item) => item.protocol_version === 4).map((item) => item.id),
                ...pool.members.map((item) => item.worker_id)])).map((id) => ({
                value: id, label: workers.find((item) => item.id === id)?.name ?? `#${id}`,
              }))} />
            {memberId !== undefined && <Form<MemberFields> key={`${pool.id}-${pool.version}-${memberId}`}
              layout="vertical" disabled={busy} initialValues={initialMember}
              onFinish={(fields) => void run(() => saveMember(fields))}>
              <Form.Item label={t("pool.memberEnabled")} name="enabled" valuePropName="checked"><Switch /></Form.Item>
              <Form.Item label={t("pool.memberLabels")} name="labels_text"><Input.TextArea rows={2} placeholder={labelExample} /></Form.Item>
              <Form.Item label={t("pool.memberScopes")} name="placement_scopes"><Select mode="tags" /></Form.Item>
              <Form.Item label={t("pool.credentialGrants")} name="credential_ids">
                <Select mode="multiple" options={credentials.map((item) => ({ value: item.id, label: item.name }))} />
              </Form.Item>
              <Form.Item label={t("pool.materialGrants")} name="material_scopes"><Select mode="tags" /></Form.Item>
              <Button htmlType="submit" loading={busy}>{t("pool.saveMember")}</Button>
            </Form>}
          </div>
          <div>
            <Typography.Title level={5}>{t("pool.preview")}</Typography.Title>
            <Space wrap>
              <Select aria-label={t("pool.language")} value={previewLanguage} disabled={busy}
                onChange={(language) => { setPreviewLanguage(language); setPreview(undefined); }}
                options={languages.map((language) => ({ value: language }))} />
              <Button disabled={busy} onClick={() => void run(async () => {
                setPreview(await api.previewWorkerPoolMembers(pool.id, pool.revision.revision, previewLanguage));
              })}>{t("pool.checkMembers")}</Button>
            </Space>
            {preview && <List size="small" dataSource={preview} renderItem={(item) =>
              <List.Item>{workers.find((worker) => worker.id === item.worker_id)?.name ?? `#${item.worker_id}`}
                <Tag>{t(`pool.qualification.${item.reason ?? "eligible"}`)}</Tag>
              </List.Item>} />}
          </div>
          <div>
            <Typography.Title level={5}>{t("pool.revisions")}</Typography.Title>
            <List size="small" dataSource={pool.revisions} renderItem={(revision) => <List.Item
              actions={revision.revision === pool.revision.revision ? [] : [
                <Popconfirm key="delete" title={t("pool.deleteRevision")} disabled={busy}
                  onConfirm={() => run(async () => accept(await api.deleteWorkerPoolRevision(pool.id, revision.revision, pool.version)))}>
                  <Button size="small" danger disabled={busy}>{t("actions.delete", { ns: "common" })}</Button>
                </Popconfirm>,
              ]}>
              <Typography.Text>#{revision.revision} · {revision.environment_class} · {revision.placement_scope}</Typography.Text>
            </List.Item>} />
          </div>
          <details>
            <summary>{t("pool.audit")}</summary>
            <List size="small" dataSource={pool.audit} renderItem={(item) =>
              <List.Item>
                {t(`pool.auditAction.${item.action}`)} · #{item.version} · {new Date(item.created_at).toLocaleString()}
              </List.Item>} />
          </details>
        </>}
      </Space>
    </Drawer>
  </>;
}
