import { useState } from "react";
import { Modal, Select } from "antd";
import { useTranslation } from "react-i18next";
import type {
  EventSubscription,
  SubscriptionDraftState,
  SubscriptionSourceConfig,
  SubscriptionSourceType,
} from "../subscription-types";
import KafkaSubscriptionConfig from "./KafkaSubscriptionConfig";
import KubernetesSubscriptionConfig from "./KubernetesSubscriptionConfig";

export default function SubscriptionConfigDialog(props: {
  subscription?: EventSubscription;
  availableSources: SubscriptionSourceType[];
  adapterId: number;
  canManage: boolean;
  onDraftStateChange?: (state: SubscriptionDraftState) => void;
  onClose: () => void;
  onSave: (value: {
    name: string;
    description: string;
    config: SubscriptionSourceConfig;
    new_epoch: boolean;
  }) => Promise<void>;
}) {
  const { t } = useTranslation("subscription");
  const [source, setSource] = useState<SubscriptionSourceType | undefined>(
    props.subscription?.config.source_type ??
      (props.availableSources.length === 1
        ? props.availableSources[0]
        : undefined),
  );
  const [choice, setChoice] = useState<SubscriptionSourceType>();
  if (!source)
    return (
      <Modal
        open
        title={t("create")}
        onCancel={props.onClose}
        onOk={() => setSource(choice)}
        okButtonProps={{ disabled: !choice }}
        okText={t("continue")}
        cancelText={t("cancel")}
      >
        <Select
          aria-label={t("source")}
          style={{ width: "100%" }}
          value={choice}
          onChange={setChoice}
          options={props.availableSources.map((value) => ({
            value,
            label: value === "kafka" ? "Kafka" : "Kubernetes",
          }))}
        />
      </Modal>
    );
  return source === "kafka" ? (
    <KafkaSubscriptionConfig {...props} />
  ) : (
    <KubernetesSubscriptionConfig {...props} />
  );
}
