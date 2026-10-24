import type { EventSubscription, SubscriptionEvent } from "./subscription-types";

/** Current operational problems, rather than failures retained for audit. */
export function eventNeedsAttention(sub: EventSubscription): boolean {
  return ["blocked", "repair"].includes(sub.delivery_state)
    || sub.connection_state === "blocked"
    || (sub.desired_state === "enabled" && sub.last_error_code !== null);
}

export function eventStatusKey(sub: EventSubscription): string {
  if (sub.delivery_state === "repair") return "repairing";
  if (sub.delivery_state === "blocked") return "needsAttention";
  if (sub.connection_state === "blocked") return "connectionProblem";
  if (sub.desired_state === "disabled") {
    if (sub.connection_state !== "stopped") return "stopping";
    return sub.delivery_state !== "idle" || (sub.quota.pending_count ?? 0) > 0
      ? "stoppedWithWork" : sub.activation_id === null ? "notStarted" : "stopped";
  }
  return sub.connection_state === "listening" ? "receiving"
    : sub.connection_state === "reconnecting" ? "reconnecting" : "starting";
}

export function eventSourceLabel(sub: EventSubscription): string {
  const config = sub.config;
  switch (config.source_type) {
    case "kafka": return `Kafka · ${config.topics.join(", ")}`;
    case "kubernetes": return `Kubernetes · ${config.resource}${config.namespace ? ` · ${config.namespace}` : ""}`;
    case "rabbitmq": return `RabbitMQ · ${config.queue}`;
    case "rocketmq": return `RocketMQ · ${config.topic}`;
    default: return "—";
  }
}

export function eventBytes(value: number | undefined): string {
  if (value === undefined || !Number.isFinite(value) || value < 0) return "—";
  if (value < 1024) return `${value} B`;
  const index = Math.min(3, Math.floor(Math.log(value) / Math.log(1024)));
  return `${(value / 1024 ** index).toLocaleString(undefined, { maximumFractionDigits: 1 })} ${["B", "KB", "MB", "GB"][index]}`;
}

/** Summarize bounded source metadata; never guess the business payload schema. */
export function eventSummary(event: SubscriptionEvent, translate: (key: string, values?: Record<string, unknown>) => string): string {
  const record = (value: unknown): Record<string, unknown> => value !== null && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
  const source = record(event.envelope?.source);
  const position = record(source.position);
  const attributes = record(source.attributes);
  if (event.source_phase?.startsWith("sync.")) return translate(`ux.phase.${event.source_phase.slice(5)}`);
  if (source.type === "kubernetes") {
    const name = [attributes.namespace, attributes.name].filter(value => typeof value === "string" && value !== "").join("/");
    return `${translate(`ux.change.${String(attributes.watch_type ?? "present").toLowerCase()}`, { defaultValue: translate("ux.objectChange") })} · ${name || translate("ux.objectChange")}`.slice(0, 220);
  }
  if (source.type === "rabbitmq" || source.type === "rocketmq") {
    const scope = attributes.queue ?? attributes.topic;
    return translate("queues.messageSummary", { scope: String(scope ?? "").slice(0, 128),
      id: String(position.message_id ?? "—").slice(0, 128) });
  }
  if (source.type === "kafka" && typeof position.topic === "string") return translate("ux.kafkaSummary", { topic: position.topic.slice(0, 128), partition: position.partition ?? "—", offset: position.offset ?? "—" });
  return translate("ux.recordNumber", { id: event.id });
}
