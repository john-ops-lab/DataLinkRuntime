/** Adapter-level read/edit sharing management (M5.9 Wave D). */

import { useCallback, useEffect, useMemo, useState } from "react";
import { Alert, Button, Divider, Empty, Select, Space, Spin, Tag, Typography } from "antd";
import { useTranslation } from "react-i18next";

import { api } from "../api";
import type { AdapterPermission, AdapterPermissionCandidate } from "../types";
import { userErrorMessage } from "../user-message";

interface Props {
  adapterId: number;
  ownerLabel: string;
  onChanged?: () => void;
}

export default function AdapterPermissionsPanel({ adapterId, ownerLabel, onChanged }: Props) {
  const { t } = useTranslation(["adapter", "common"]);
  const [permissions, setPermissions] = useState<AdapterPermission[]>([]);
  const [candidates, setCandidates] = useState<AdapterPermissionCandidate[]>([]);
  const [selectedUserId, setSelectedUserId] = useState<number | null>(null);
  const [selectedPermission, setSelectedPermission] = useState<"read" | "edit">("read");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [grantList, candidateList] = await Promise.all([
        api.listAdapterPermissions(adapterId),
        api.listAdapterPermissionCandidates(adapterId),
      ]);
      setPermissions(grantList);
      setCandidates(candidateList);
      setError(null);
    } catch (err) {
      setError(userErrorMessage(err, t("sharing.loadFailed")));
    } finally {
      setLoading(false);
    }
  }, [adapterId, t]);

  useEffect(() => {
    // Opening the explicit sharing section intentionally loads only the
    // minimal ACL/grantee metadata endpoints; no account-management payload
    // or Credential data is requested here.
    // eslint-disable-next-line react-hooks/set-state-in-effect -- opening the sharing section starts an intentional async load
    void load();
  }, [load]);

  const grantedIds = useMemo(
    () => new Set(permissions.map((permission) => permission.user_id)),
    [permissions],
  );
  const availableCandidates = candidates.filter((candidate) => !grantedIds.has(candidate.id));

  async function saveGrant(userId: number, permission: "read" | "edit") {
    if (saving) {
      return;
    }
    setSaving(true);
    setError(null);
    try {
      await api.setAdapterPermission(adapterId, userId, permission);
      setSelectedUserId(null);
      await load();
      onChanged?.();
    } catch (err) {
      setError(userErrorMessage(err, t("sharing.saveFailed")));
    } finally {
      setSaving(false);
    }
  }

  async function revoke(userId: number) {
    if (saving) {
      return;
    }
    setSaving(true);
    setError(null);
    try {
      await api.revokeAdapterPermission(adapterId, userId);
      await load();
      onChanged?.();
    } catch (err) {
      setError(userErrorMessage(err, t("sharing.revokeFailed")));
    } finally {
      setSaving(false);
    }
  }

  if (loading) {
    return <Spin data-testid="adapter-permissions-loading" />;
  }

  return (
    <section className="adapter-permissions" data-testid="adapter-permissions">
      <Typography.Title level={5}>{t("sharing.title")}</Typography.Title>
      <Typography.Paragraph type="secondary">
        {t("sharing.owner", { owner: ownerLabel })}
      </Typography.Paragraph>
      {error !== null && <Alert type="error" showIcon role="alert" message={error} />}
      <Divider orientation="left" plain>{t("sharing.grantedTitle")}</Divider>
      {permissions.length === 0 ? (
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t("sharing.none")} />
      ) : (
        <Space direction="vertical" className="adapter-permission-list" style={{ width: "100%" }}>
          {permissions.map((grant) => (
            <div className="adapter-permission-row" key={grant.user_id} data-testid="adapter-permission-row">
              <span>
                <strong>{grant.username}</strong>
                {!grant.enabled && <Tag color="warning">{t("sharing.disabled")}</Tag>}
              </span>
              <Select<"read" | "edit">
                aria-label={t("sharing.permissionFor", { username: grant.username })}
                value={grant.permission}
                disabled={saving}
                options={[
                  { value: "read", label: t("sharing.read") },
                  { value: "edit", label: t("sharing.edit") },
                ]}
                onChange={(value) => void saveGrant(grant.user_id, value)}
              />
              <Button
                danger
                size="small"
                data-testid="revoke-adapter-permission"
                disabled={saving}
                onClick={() => void revoke(grant.user_id)}
              >
                {t("sharing.revoke")}
              </Button>
            </div>
          ))}
        </Space>
      )}
      <Divider orientation="left" plain>{t("sharing.addTitle")}</Divider>
      <Space wrap>
        <Select<number>
          aria-label={t("sharing.account")}
          data-testid="adapter-permission-account"
          value={selectedUserId ?? undefined}
          placeholder={t("sharing.accountPlaceholder")}
          disabled={saving || availableCandidates.length === 0}
          options={availableCandidates.map((candidate) => ({
            value: candidate.id,
            label: `${candidate.username}${candidate.role === "admin" ? ` · ${t("sharing.admin")}` : ""}${candidate.enabled ? "" : ` · ${t("sharing.disabled")}`}`,
          }))}
          onChange={(value) => setSelectedUserId(value)}
        />
        <Select<"read" | "edit">
          aria-label={t("sharing.permission")}
          value={selectedPermission}
          disabled={saving}
          options={[
            { value: "read", label: t("sharing.read") },
            { value: "edit", label: t("sharing.edit") },
          ]}
          onChange={(value) => setSelectedPermission(value)}
        />
        <Button
          type="primary"
          data-testid="grant-adapter-permission"
          disabled={saving || selectedUserId === null}
          onClick={() => {
            if (selectedUserId !== null) {
              void saveGrant(selectedUserId, selectedPermission);
            }
          }}
        >
          {t("sharing.grant")}
        </Button>
        <Button data-testid="refresh-adapter-permissions" loading={loading} onClick={() => void load()}>
          {t("actions.refresh", { ns: "common" })}
        </Button>
      </Space>
      {availableCandidates.length === 0 && (
        <Typography.Paragraph type="secondary">{t("sharing.noCandidates")}</Typography.Paragraph>
      )}
    </section>
  );
}
