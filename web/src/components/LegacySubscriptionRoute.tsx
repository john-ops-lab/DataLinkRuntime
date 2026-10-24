import { useEffect, useRef, useState } from "react";
import { Alert, Button, Spin } from "antd";
import { useTranslation } from "react-i18next";
import { api } from "../api";
import type { Adapter } from "../types";
import { subscriptionErrorMessage } from "../subscription-message";

/** Compatibility for bookmarks, without retaining a second management UI. */
export default function LegacySubscriptionRoute({ id, onOpen, onBack }: {
  id: number | null; onOpen: (adapter: Adapter) => Promise<void>; onBack: () => void;
}) {
  const { t } = useTranslation("subscription");
  const handlers = useRef({ onOpen, onBack });
  useEffect(() => { handlers.current = { onOpen, onBack }; });
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let active = true;
    const timer = window.setTimeout(() => {
      if (id === null) { handlers.current.onBack(); return; }
      void api.getSubscription(id).then(sub => api.getAdapter(sub.adapter_id)).then(async adapter => {
        if (active) await handlers.current.onOpen(adapter);
      }).catch((reason: unknown) => { if (active) setError(subscriptionErrorMessage(reason)); });
    }, 0);
    return () => { active = false; window.clearTimeout(timer); };
  }, [id]);
  return error ? <Alert type="error" message={error} action={<Button onClick={onBack}>{t("back")}</Button>} /> : <Spin />;
}
