import { useCallback, useState } from "react";
import { Radio } from "antd";
import { useTranslation } from "react-i18next";
import type {
  EventSubscription,
  SubscriptionDraftState,
  SubscriptionSourceConfig,
  SubscriptionSourceType,
} from "../subscription-types";
import KafkaSubscriptionConfig from "./KafkaSubscriptionConfig";
import KubernetesSubscriptionConfig from "./KubernetesSubscriptionConfig";
import QueueSubscriptionConfig from "./QueueSubscriptionConfig";
import HttpSubscriptionConfig from "./HttpSubscriptionConfig";

export default function SubscriptionConfigDialog(props: {
  subscription?: EventSubscription;
  availableSources: SubscriptionSourceType[];
  adapterId: number;
  canManage: boolean;
  readOnly?: boolean;
  canTest?: boolean;
  onDraftStateChange?: (state: SubscriptionDraftState) => void;
  onClose: () => void;
  onSave: (value: {
    name: string;
    description: string;
    config: SubscriptionSourceConfig;
    new_epoch: boolean;
  }, test?: boolean) => Promise<void | EventSubscription>;
}) {
  const { t } = useTranslation("subscription");
  const [sourceChoice, setSource] = useState<SubscriptionSourceType>();
  const source = props.subscription?.config.source_type ?? sourceChoice ?? props.availableSources[0];
  const [draft, setDraft] = useState<SubscriptionDraftState>({ dirty: false, busy: false });
  const reportDraft = props.onDraftStateChange;
  const onDraftStateChange = useCallback((value: SubscriptionDraftState) => {
    setDraft(current => current.dirty === value.dirty && current.busy === value.busy ? current : value);
    reportDraft?.(value);
  }, [reportDraft]);
  if (!source) return null;
  const sourceSelector = !props.subscription && props.availableSources.length > 1 ? <div className="event-source-choice">
    <span>{t("ux.source")}</span><Radio.Group value={source} aria-label={t("ux.source")}
      disabled={props.readOnly || draft.busy} options={props.availableSources.map(value => ({ value, label: t(`ux.sourceType.${value}`) }))}
      onChange={event => {
        if (draft.busy || (draft.dirty && !window.confirm(t("confirm.discardChanges", { ns: "common" })))) return;
        setSource(event.target.value as SubscriptionSourceType);
      }} />
  </div> : undefined;
  const shared = { ...props, sourceSelector, onDraftStateChange };
  return source === "kafka" ? (
    <KafkaSubscriptionConfig {...shared} />
  ) : source === "kubernetes" ? (
    <KubernetesSubscriptionConfig {...shared} />
  ) : source === "rabbitmq" || source === "rocketmq" ? (
    <QueueSubscriptionConfig key={source} sourceType={source} {...shared} />
  ) : source === "http" ? (
    <HttpSubscriptionConfig {...shared} />
  ) : null;
}
