"""Closed failure meanings shared by queue intake and legacy recovery."""

MESSAGE_FAILURES = frozenset(
    {
        "event_content_invalid",
        "event_identity_conflict",
        "event_payload_too_large",
        "source_properties_invalid",
        "source_message_id_required",
        "source_message_type_unsupported",
        "source_message_digest_invalid",
        "source_message_encoding_invalid",
    }
)

# These codes were incorrectly persisted by the old HTTP-exception catch-all.
# Do not infer recovery eligibility from a status code or an arbitrary error.
LEGACY_LIFECYCLE_FAILURES = frozenset(
    {"source_not_available", "subscription_activation_missing", "subscription_config_conflict"}
)
RETRYABLE_FAILURES = frozenset(
    {
        "source_not_available",
        "source_disabled",
        "subscription_owner_stale",
        "subscription_owner_conflict",
    }
)
CONFIGURATION_FAILURES = frozenset(
    {
        "subscription_activation_missing",
        "subscription_config_conflict",
        "subscription_config_invalid",
        "source_activation_unavailable",
        "source_configuration_unavailable",
        "source_credential_unavailable",
        "source_shard_blocked",
        "source_authentication_failed",
        "source_authorization_failed",
    }
)
