/** 左侧 Adapter Catalog：高密度行式导航 + 新建表单（M3.1 §7，业务合同仍沿用 M1）。 */

import { useEffect, useState } from "react";
import { Button, Dropdown, Empty, Input, Popover, Radio, Select, Tooltip } from "antd";
import { QuestionCircleOutlined, ReloadOutlined } from "@ant-design/icons";
import {
  DrawerForm,
  ProForm,
} from "@ant-design/pro-components";
import { useTranslation } from "react-i18next";

import { adapterAccessLevel } from "../adapter-access";
import { LANGUAGE_LABELS } from "../languages";
import type {
  AccountPrincipal,
  Adapter,
  AdapterLanguage,
  AdapterType,
  Worker,
} from "../types";

type AdapterTypeFilter = "task-manual" | "task-schedule" | "webhook";
type AdapterStatusFilter = "all" | "running" | "stopped";

interface CreateAdapterValues {
  name: string;
  description: string;
  language: AdapterLanguage;
  adapterType: AdapterType;
}

interface CatalogFilterValues {
  search: string;
  type: AdapterTypeFilter | "all";
  status: AdapterStatusFilter;
}

const DEFAULT_CREATE_ADAPTER_VALUES: CreateAdapterValues = {
  name: "",
  description: "",
  language: "python",
  adapterType: "task",
};

function matchesTypeFilter(adapter: Adapter, filter: AdapterTypeFilter): boolean {
  if (filter === "webhook") {
    return adapter.adapter_type === "webhook";
  }
  return adapter.adapter_type === "task" && adapter.run_mode === filter.replace("task-", "");
}

interface AdapterCatalogProps {
  adapters: Adapter[];
  selectedId: number | null;
  // Interaction lock: while a mutation is in flight, switching adapters and
  // starting a new create are disabled so state cannot be mixed across adapters.
  busy: boolean;
  onSelect: (adapter: Adapter) => void;
  // Returns true only when the adapter was actually created; the form is cleared
  // and closed only on real success, so failures keep the user's input editable.
  onCreate: (
    name: string,
    description: string,
    language: AdapterLanguage,
    adapterType: AdapterType,
  ) => Promise<boolean>;
  // Version lists are loaded only for selected Adapters. Known id -> seq
  // mappings are cached by App; unknown ids stay explicit as #id instead of
  // causing one list request per Catalog row.
  versionSeqById: Map<number, number>;
  // Loaded once by App and shared with Worker status/settings/Catalog. This
  // keeps Worker names visible without a per-Adapter request.
  workers: Worker[];
  // M5.5.9：列表项三点菜单——“设置”直接进入该 Adapter 设置；“复制”进入 Clone 流程。
  onOpenSettings: (adapter: Adapter) => void;
  onClone: (adapter: Adapter) => void;
  onRefresh: () => Promise<void>;
  accountPrincipal?: AccountPrincipal;
}

function versionLabel(
  versionId: number,
  serverSeq: number | null | undefined,
  versionSeqById: Map<number, number>,
): string {
  const seq = serverSeq ?? versionSeqById.get(versionId);
  return seq === undefined ? `#${versionId}` : `v${seq}`;
}

function catalogRuntimeStatus(adapter: Adapter): {
  dot: "running" | "stopped";
  label: string;
  fact: string;
} {
  // M5.5.10：主界面不展示内部 Execution #N，只保留用户可见状态。
  if (adapter.adapter_type === "task") {
    if (adapter.running_execution_id != null) {
      return { dot: "running", label: "running", fact: "running" };
    }
    const label = adapter.runtime_locked ? "scheduledRunning" : "idle";
    return { dot: adapter.runtime_locked ? "running" : "stopped", label, fact: label };
  }
  if (adapter.running_execution_id != null) {
    return { dot: "running", label: "calling", fact: "calling" };
  }
  const label = adapter.runtime_locked ? "receiving" : "stopped";
  return { dot: adapter.runtime_locked ? "running" : "stopped", label, fact: label };
}

function catalogSubtitle(
  adapter: Adapter,
  runtimeStatus: ReturnType<typeof catalogRuntimeStatus>,
  versionSeqById: Map<number, number>,
  workersById: Map<number, Worker>,
  translate: (key: string, options?: Record<string, unknown>) => string,
): { primary: string; attention: string[]; full: string } {
  // M5.5.9：目录行直接展示 Adapter 类型，便于快速扫描。
  const typeLabel = adapter.adapter_type === "task"
    ? `[${translate("types.task")}]`
    : `[${translate("types.webhook")}]`;
  if (adapter.adapter_type === "task") {
    const attention: string[] = [];
    const mode = translate(`catalog.${adapter.run_mode === "schedule" ? "schedule" : "manual"}`);
    const status = translate(`catalog.${runtimeStatus.label}`);
    const workerId = adapter.runtime_worker_id;
    if (workerId != null) {
      const worker = workersById.get(workerId);
      if (worker !== undefined && worker.status !== "online") {
        attention.push(translate("catalog.workerOffline"));
      }
    }
    const primary = translate("catalog.taskSubtitle", {
      type: typeLabel,
      language: LANGUAGE_LABELS[adapter.language],
      mode,
      status,
    });
    const fullParts = [primary, ...attention];
    if (workerId == null) {
      fullParts.push(translate("catalog.workerNotConfigured"));
    } else {
      const worker = workersById.get(workerId);
      fullParts.push(worker === undefined
        ? translate("catalog.workerUnknown", { id: workerId })
        : translate("catalog.workerNamed", { name: worker.name }));
    }
    if (adapter.description.trim() !== "") {
      fullParts.push(adapter.description.trim());
    }
    return { primary, attention, full: fullParts.join(" · ") };
  }
  const attention: string[] = [];
  const workerId = adapter.runtime_worker_id;
  if (workerId !== null && workerId !== undefined) {
    const worker = workersById.get(workerId);
    if (worker !== undefined && worker.status !== "online") {
      attention.push(translate("catalog.workerOffline"));
    }
  }
  const revision =
    adapter.latest_version_id == null
      ? translate("catalog.notSaved")
      : translate("catalog.saved", { version: versionLabel(adapter.latest_version_id, null, versionSeqById) });
  const primary = translate("catalog.webhookSubtitle", {
    type: typeLabel,
    language: LANGUAGE_LABELS[adapter.language],
    status: translate(`catalog.${runtimeStatus.label}`),
    revision,
  });
  const fullParts = [primary, ...attention];
  if (workerId === null || workerId === undefined) {
    fullParts.push(translate("catalog.workerNotConfigured"));
  } else {
    const worker = workersById.get(workerId);
    fullParts.push(worker === undefined
      ? translate("catalog.workerUnknown", { id: workerId })
      : translate("catalog.workerNamed", { name: worker.name }));
  }
  if (adapter.description.trim() !== "") {
    fullParts.push(adapter.description.trim());
  }
  return { primary, attention, full: fullParts.join(" · ") };
}

function relationshipLabel(
  adapter: Adapter,
  principal: AccountPrincipal | undefined,
  translate: (key: string, options?: Record<string, unknown>) => string,
): string {
  const level = adapterAccessLevel(adapter, principal);
  if (level === "owner") {
    return translate("access.mine");
  }
  if (level === "edit") {
    return translate("access.sharedEdit");
  }
  if (level === "read") {
    return translate("access.sharedRead");
  }
  if (adapter.owner_user_id == null) {
    return translate("access.systemAdmin");
  }
  return translate("access.adminAll");
}

export default function AdapterCatalog({
  adapters,
  selectedId,
  busy,
  onSelect,
  onCreate,
  versionSeqById,
  workers,
  onOpenSettings,
  onClone,
  onRefresh,
  accountPrincipal,
}: AdapterCatalogProps) {
  const { t } = useTranslation(["adapter", "common"]);
  const [creating, setCreating] = useState(false);
  const [createForm] = ProForm.useForm<CreateAdapterValues>();
  const [filters, setFilters] = useState<CatalogFilterValues>({
    search: "",
    type: "all",
    status: "all",
  });
  const [submitting, setSubmitting] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const workersById = new Map(workers.map((worker) => [worker.id, worker]));

  useEffect(() => {
    if (creating) {
      createForm.setFieldsValue(DEFAULT_CREATE_ADAPTER_VALUES);
    }
  }, [createForm, creating]);

  async function handleCreate(values: CreateAdapterValues): Promise<boolean> {
    const trimmed = values.name.trim();
    if (!trimmed || busy || submitting) {
      return false;
    }
    setSubmitting(true);
    try {
      const created = await onCreate(
        trimmed,
        values.description,
        values.language,
        values.adapterType,
      );
      if (created) {
        createForm.resetFields();
        setCreating(false);
      }
      return created;
    } finally {
      setSubmitting(false);
    }
  }

  async function handleRefresh(): Promise<void> {
    if (busy || refreshing) {
      return;
    }
    setRefreshing(true);
    try {
      await onRefresh();
    } finally {
      setRefreshing(false);
    }
  }

  const keyword = filters.search.trim().toLowerCase();
  const inView = adapters.filter((adapter) => !adapter.archived_at);
  // M5.8-008：类型 / 状态 / 关键词三个筛选条件叠加生效；状态筛选只过滤列表，
  // 不改变 Adapter 的真实运行状态。
  const selectedTypeFilter = filters.type;
  const typeFiltered =
    selectedTypeFilter === "all"
      ? inView
      : inView.filter((adapter) => matchesTypeFilter(adapter, selectedTypeFilter));
  const statusFiltered =
    filters.status === "all"
      ? typeFiltered
      : typeFiltered.filter((adapter) => catalogRuntimeStatus(adapter).dot === filters.status);
  const visible = keyword === ""
    ? statusFiltered
    : statusFiltered.filter((adapter) =>
        [adapter.name, adapter.description].some((value) => value.toLowerCase().includes(keyword)),
      );
  const typeFilterOptions: Array<{ value: AdapterTypeFilter | "all"; label: string }> = [
    { value: "all", label: t("catalog.filterTypeAll") },
    { value: "task-manual", label: t("catalog.taskManual") },
    { value: "task-schedule", label: t("catalog.taskSchedule") },
    { value: "webhook", label: t("catalog.webhook") },
  ];
  const statusFilterOptions: Array<{ value: AdapterStatusFilter; label: string }> = [
    { value: "all", label: t("catalog.filterStatusAll") },
    { value: "running", label: t("catalog.running") },
    { value: "stopped", label: t("catalog.stopped") },
  ];

  return (
    <aside className="catalog" data-testid="adapter-catalog">
      <div className="catalog-header" data-testid="adapter-catalog-header">
        <div className="catalog-heading">
          <h2 className="catalog-title">{t("catalog.title")}</h2>
        </div>
        <div className="catalog-header-actions" data-testid="adapter-catalog-actions">
          <Button
            size="small"
            type="primary"
            data-testid="show-create-form"
            disabled={busy}
            onClick={() => {
              setCreating(true);
            }}
          >
            {t("catalog.new")}
          </Button>
          <Tooltip title={t("catalog.refreshAria")}>
            <Button
              size="small"
              type="text"
              icon={<ReloadOutlined />}
              data-testid="refresh-adapters"
              aria-label={t("catalog.refreshAria")}
              loading={refreshing}
              disabled={busy}
              onClick={() => void handleRefresh()}
            />
          </Tooltip>
          <Popover
            title={t("catalog.helpTitle")}
            content={<span className="catalog-help-content">{t("catalog.helpDescription")}</span>}
            trigger="click"
          >
            <Button
              size="small"
              type="text"
              icon={<QuestionCircleOutlined />}
              data-testid="adapter-catalog-help"
              aria-label={t("catalog.helpAria")}
            />
          </Popover>
        </div>
      </div>

      <div
        className="catalog-toolbar"
        data-testid="adapter-catalog-toolbar"
        role="toolbar"
        aria-label={t("catalog.toolbarAria")}
      >
        <div className="catalog-search-control" aria-label={t("catalog.filterGroupAria")}>
          <Input
            className="catalog-search-input"
            data-testid="adapter-search"
            aria-label={t("catalog.search")}
            placeholder={t("catalog.search")}
            allowClear
            size="small"
            value={filters.search}
            onChange={(event) => setFilters((current) => ({ ...current, search: event.target.value }))}
          />
          <Select<AdapterTypeFilter | "all">
            size="small"
            className="catalog-filter-type"
            data-testid="adapter-type-filter"
            aria-label={t("catalog.filterTypeAria")}
            value={filters.type}
            options={typeFilterOptions}
            onChange={(value) => setFilters((current) => ({ ...current, type: value }))}
          />
          <Select<AdapterStatusFilter>
            size="small"
            className="catalog-filter-status"
            data-testid="adapter-status-filter"
            aria-label={t("catalog.filterStatusAria")}
            value={filters.status}
            options={statusFilterOptions}
            onChange={(value) => setFilters((current) => ({ ...current, status: value }))}
          />
        </div>
        <span className="catalog-summary" data-testid="adapter-catalog-summary">
          {t("catalog.summary", { count: visible.length })}
        </span>
      </div>

      <div className="catalog-list" data-testid="adapter-catalog-list">
        {visible.length === 0 ? (
          <Empty
            className="catalog-empty"
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description={inView.length === 0 ? t("catalog.empty") : t("catalog.noMatch")}
          />
        ) : (
          visible.map((adapter) => {
            const runtimeStatusKey = catalogRuntimeStatus(adapter);
            const accessLevel = adapterAccessLevel(adapter, accountPrincipal);
            const runtimeStatus = {
              ...runtimeStatusKey,
              label: t(`catalog.${runtimeStatusKey.label}`),
              fact: t(`catalog.${runtimeStatusKey.fact}`),
            };
            const statusDescription = t("catalog.statusDescription", {
              type: t(`types.${adapter.adapter_type}`),
              status: runtimeStatus.label,
            });
            const subtitle = catalogSubtitle(
              adapter,
              runtimeStatusKey,
              versionSeqById,
              workersById,
              (key, options) => t(key, options),
            );
            const relationship = relationshipLabel(adapter, accountPrincipal, (key, options) =>
              t(key, options),
            );
            const ownerLabel = adapter.owner_user_id == null
              ? t("access.systemOwner")
              : adapter.owner_username ?? t("access.ownerUnknown");
            const accessibleRuntimeFact = statusDescription;
            const ariaLabel = adapter.access_level !== undefined || accountPrincipal !== undefined
              ? `${adapter.name}，${relationship}，${ownerLabel}，${subtitle.full.replace(runtimeStatus.fact, accessibleRuntimeFact)}`
              : `${adapter.name}，${subtitle.full.replace(runtimeStatus.fact, accessibleRuntimeFact)}`;
            return (
              <div key={adapter.id} className="catalog-row">
                <button
                  type="button"
                  data-testid="adapter-item"
                  className={adapter.id === selectedId ? "catalog-item selected" : "catalog-item"}
                  disabled={busy}
                  title={`${adapter.name}${adapter.description ? ` — ${adapter.description}` : ""}\n${subtitle.full}`}
                  aria-label={ariaLabel}
                  onClick={() => onSelect(adapter)}
                >
                  <span className="catalog-item-name">
                    <span
                      className={`catalog-status-dot catalog-status-${runtimeStatus.dot}`}
                      title={statusDescription}
                    />
                    {adapter.name}
                  </span>
                  <span className="catalog-item-access" data-testid="adapter-access">
                    {relationship}
                    {adapter.owner_user_id == null && relationship !== t("access.systemAdmin") && (
                      <> · {ownerLabel}</>
                    )}
                  </span>
                  <span className="catalog-item-sub" title={subtitle.full}>
                    <span>{subtitle.primary}</span>
                    {subtitle.attention.map((item) => (
                      <span className="catalog-item-attention" key={item}> · {item}</span>
                    ))}
                  </span>
                </button>
                {/* M5.5.9：三点菜单只提供“设置/复制”；点击菜单按钮不触发行选择，
                    再次点击或点击空白处由 Dropdown 关闭，键盘可达（原生 Button）。 */}
                <Dropdown
                  trigger={["click"]}
                  placement="bottomRight"
                  menu={{
                    items: [
                      { key: "settings", label: t("catalog.settings") },
                      ...(accessLevel === "read"
                        ? []
                        : [{ key: "clone", label: t("catalog.clone") }]),
                    ],
                    onClick: ({ key }) => {
                      if (key === "settings") {
                        onOpenSettings(adapter);
                      } else if (key === "clone") {
                        onClone(adapter);
                      }
                    },
                  }}
                >
                  <Button
                    size="small"
                    type="text"
                    className={adapter.id === selectedId
                      ? "catalog-item-menu catalog-item-menu-selected"
                      : "catalog-item-menu"}
                    disabled={busy}
                    aria-label={t("catalog.moreActions", { name: adapter.name })}
                    data-testid="adapter-item-menu"
                  >
                    ···
                  </Button>
                </Dropdown>
              </div>
            );
          })
        )}
      </div>

      <DrawerForm<CreateAdapterValues>
        form={createForm}
        title={t("catalog.createTitle")}
        width={360}
        open={creating}
        formKey={creating ? "adapter-create" : "adapter-create-closed"}
        initialValues={{
          ...DEFAULT_CREATE_ADAPTER_VALUES,
        }}
        drawerProps={{ destroyOnHidden: true }}
        onOpenChange={(open) => {
          if (!open) {
            createForm.resetFields();
          }
          createForm.setFieldsValue(DEFAULT_CREATE_ADAPTER_VALUES);
          setCreating(open);
        }}
        onFinish={(values) => handleCreate(values ?? createForm.getFieldsValue(true))}
        submitter={{
          render: (submitterProps) => [
            <Button
              key="submit"
              type="primary"
              data-testid="create-adapter"
              loading={submitting}
              disabled={busy}
              onClick={submitterProps.submit}
            >
              {t("catalog.create")}
            </Button>,
            <Button key="cancel" onClick={() => setCreating(false)}>
              {t("actions.cancel", { ns: "common" })}
            </Button>,
          ],
        }}
      >
        <ProForm.Item
          name="name"
          label={t("catalog.name")}
          rules={[{ required: true, whitespace: true, message: t("catalog.namePlaceholder") }]}
        >
          <Input
            data-testid="new-adapter-name"
            aria-label={t("catalog.name")}
            placeholder={t("catalog.namePlaceholder")}
            disabled={busy}
          />
        </ProForm.Item>
        <div role="radiogroup" aria-label={t("catalog.type")}>
          <ProForm.Item name="adapterType" label={t("catalog.type")}>
            <Radio.Group
              data-testid="new-adapter-type"
              disabled={busy}
              options={[
                { value: "task", label: t("types.taskAdapter") },
                { value: "webhook", label: t("types.webhookAdapter") },
              ]}
            />
          </ProForm.Item>
        </div>
        <div role="radiogroup" aria-label={t("catalog.language")}>
          <ProForm.Item name="language" label={t("catalog.languageField")}>
            <Radio.Group
              data-testid="new-adapter-language"
              disabled={busy}
              options={[
                { value: "python", label: "Python" },
                { value: "javascript", label: "JavaScript" },
                { value: "java", label: "Java" },
              ]}
            />
          </ProForm.Item>
        </div>
        <ProForm.Item name="description" label={t("catalog.description")}>
          <Input.TextArea
            data-testid="new-adapter-description"
            aria-label={t("catalog.description")}
            placeholder={t("catalog.descriptionPlaceholder")}
            disabled={busy}
            rows={3}
          />
        </ProForm.Item>
      </DrawerForm>
    </aside>
  );
}
