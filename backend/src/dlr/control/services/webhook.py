"""Domain service for the final Webhook Adapter model (M5.4.3).

Owns the singleton Webhook configuration API and the external ingress:

```text
POST /api/hooks/{public_id}
-> Authorization: Bearer <token Credential value>
-> JSON Body
-> Control validates Webhook / Token / Adapter runtime
-> creates Execution(trigger=webhook) with input = the whole JSON body
-> HTTP 202 + execution_id; the Worker executes asynchronously
```

Control never waits for the Execution to run. Rejections are immediate and
never queued: a busy Adapter answers 409 and the caller decides whether to
retry. Rejected requests are never persisted; accepted ones live in
Execution history.

Security contract:

- ``public_id`` is routing only, never an authentication secret.
- The Bearer token is compared constant-time against the decrypted value
  of the referenced token Credential (plaintext lives only in memory for
  the duration of the request).
- Tokens never appear in responses or logs.

Lock order: the receipt path locks the Adapter row first and the Webhook
row second, matching Start / Stop / PUT Webhook, so concurrent operations
can never deadlock.
"""

import json
import logging
import math
import re
import secrets as stdlib_secrets
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from dlr.common.config import settings
from dlr.control.models import Adapter, Credential, Execution, Worker
from dlr.control.models.webhook import AdapterWebhook
from dlr.control.schemas.webhook import (
    WEBHOOK_PUBLIC_ID_PATTERN,
    WebhookResponse,
    WebhookUpsert,
)
from dlr.control.security import _bearer_token
from dlr.control.services import adapter_runtime, worker_availability
from dlr.control.services.adapter import (
    _require_not_archived,
    domain_error,
)
from dlr.control.services.execution import compact_json_bytes
from dlr.control.services.locale import get_system_locale
from dlr.control.services.secrets import decrypt_fields

logger = logging.getLogger("dlr.control.webhook")

# The single Credential field a Webhook authenticates against.
WEBHOOK_CREDENTIAL_TYPE = "token"
WEBHOOK_PUBLIC_ID_RE = re.compile(WEBHOOK_PUBLIC_ID_PATTERN)


def _reject_non_standard_constant(name: str) -> float:
    """Reject NaN / Infinity / -Infinity: standard JSON only.

    ``json.loads`` accepts these constants by default, but the Execution
    input contract is standard JSON; persisting them would surface as a
    JSONB error instead of a stable 400.
    """
    raise ValueError(f"non-standard JSON constant: {name}")


def _parse_finite_float(text: str) -> float:
    """Reject numeric overflow (e.g. ``1e309`` parses to inf by default)."""
    value = float(text)
    if not math.isfinite(value):
        raise ValueError(f"non-finite number: {text}")
    return value


# U+0000 is forbidden in PostgreSQL text, and code points in the UTF-16
# surrogate range can only appear as unpaired surrogates inside a Python
# str (astral characters are single code points): both parse as JSON and
# pass the size caps, then fail at JSONB write time.
_JSONB_UNPERSISTABLE = re.compile("[\x00\ud800-\udfff]")


def _require_jsonb_persistable(payload: Any) -> None:
    """Reject every string value / object key JSONB cannot persist.

    Covers U+0000 and unpaired surrogates (accepted by ``json.loads``
    from ``\\ud800``-style escapes); such input must answer a stable 400
    with zero Executions instead of a write-time failure. The scan is
    iterative: a capped body can still nest deeper than Python's
    recursion limit.
    """
    stack: list[Any] = [payload]
    while stack:
        current = stack.pop()
        if isinstance(current, str):
            if _JSONB_UNPERSISTABLE.search(current):
                raise ValueError("string cannot be persisted as JSONB")
        elif isinstance(current, dict):
            for key, value in current.items():
                if _JSONB_UNPERSISTABLE.search(key):
                    raise ValueError("object key cannot be persisted as JSONB")
                stack.append(value)
        elif isinstance(current, list):
            stack.extend(current)


def webhook_path(public_id: str) -> str:
    """The external entry path of one Webhook (built from the routing id)."""
    return f"/api/hooks/{public_id}"


def _webhook_response(session: Session, webhook: AdapterWebhook) -> WebhookResponse:
    credential = (
        session.get(Credential, webhook.credential_id)
        if webhook.credential_id is not None
        else None
    )
    if webhook.credential_id is not None and credential is None:
        # RESTRICT normally makes this impossible.
        raise RuntimeError("webhook references a missing credential")
    return WebhookResponse(
        adapter_id=webhook.adapter_id,
        enabled=webhook.enabled,
        public_id=webhook.public_id,
        hook_path=webhook_path(webhook.public_id),
        credential_id=webhook.credential_id,
        credential_name=credential.name if credential is not None else None,
        created_at=webhook.created_at,
        updated_at=webhook.updated_at,
    )


# --- Webhook configuration API ----------------------------------------------------


def get_webhook(session: Session, adapter_id: int) -> WebhookResponse:
    """Return the Adapter's Webhook or 404 ``webhook_not_configured``."""
    adapter = session.get(Adapter, adapter_id)
    if adapter is None:
        raise domain_error(404, "adapter_not_found", "Adapter not found")
    webhook = session.scalar(select(AdapterWebhook).where(AdapterWebhook.adapter_id == adapter_id))
    if webhook is None:
        raise domain_error(404, "webhook_not_configured", "Webhook is not configured")
    return _webhook_response(session, webhook)


def upsert_webhook(session: Session, adapter_id: int, data: WebhookUpsert) -> WebhookResponse:
    """Replace the stopped Webhook config or Start/Stop receiving.

    The Adapter row is locked first. Different Adapters may Start
    concurrently, so the partial unique index remains the final path
    ownership defense after the explicit user-readable conflict check.
    """
    adapter = session.get(Adapter, adapter_id, with_for_update=True)
    if adapter is None:
        raise domain_error(404, "adapter_not_found", "Adapter not found")
    if adapter.adapter_type != "webhook":
        raise domain_error(409, "adapter_type_mismatch", "Only webhook Adapters support Webhook")
    _require_not_archived(adapter)
    webhook = session.scalar(
        select(AdapterWebhook).where(AdapterWebhook.adapter_id == adapter_id).with_for_update()
    )
    if webhook is None:
        webhook = AdapterWebhook(
            adapter_id=adapter_id,
            public_id=stdlib_secrets.token_hex(8),
            enabled=False,
            credential_id=None,
        )
        session.add(webhook)
        session.flush()

    changed = (
        data.public_id != webhook.public_id
        or data.credential_id != webhook.credential_id
        or data.enabled != webhook.enabled
    )
    if (
        data.public_id != webhook.public_id
        and WEBHOOK_PUBLIC_ID_RE.fullmatch(data.public_id) is None
    ):
        raise domain_error(
            422,
            "webhook_path_invalid",
            "Webhook path must use 3-64 lowercase letters, digits or hyphens "
            "and start with a letter or digit",
            {"path": data.public_id},
        )
    if adapter_runtime.adapter_runtime_locked(session, adapter):
        disable_only = (
            webhook.enabled
            and not data.enabled
            and data.public_id == webhook.public_id
            and data.credential_id == webhook.credential_id
        )
        if changed and not disable_only:
            adapter_runtime.require_runtime_unlocked(session, adapter)

    credential = None
    if data.credential_id is not None:
        credential = session.scalar(
            select(Credential).where(Credential.id == data.credential_id).with_for_update()
        )
        if credential is None:
            raise domain_error(404, "credential_not_found", "Credential not found")
        if credential.type != WEBHOOK_CREDENTIAL_TYPE:
            raise domain_error(
                422,
                "webhook_credential_type_invalid",
                f"Webhook requires a '{WEBHOOK_CREDENTIAL_TYPE}' Credential, "
                f"got '{credential.type}'",
                {"expected_type": WEBHOOK_CREDENTIAL_TYPE, "actual_type": credential.type},
            )

    if data.enabled and not webhook.enabled:
        if adapter.latest_version_id is None:
            raise domain_error(
                409, "adapter_has_no_version", "Save the Webhook Adapter before receiving"
            )
        if credential is None:
            raise domain_error(
                409, "webhook_token_required", "Choose a Token Credential before receiving"
            )
        if adapter.runtime_worker_id is None:
            raise domain_error(
                409, "runtime_worker_required", "Choose a runtime Worker before receiving"
            )
        worker = session.get(Worker, adapter.runtime_worker_id)
        if worker is None or not worker_availability.is_effectively_online(
            worker, now=worker_availability.current_time(session)
        ):
            raise domain_error(409, "worker_offline", "The runtime Worker is offline")
        if adapter.language not in worker.capabilities:
            raise domain_error(
                409,
                "worker_capability_missing",
                f"The runtime Worker does not support {adapter.language}",
                {"language": adapter.language},
            )
        if adapter_runtime.active_execution(session, adapter.id) is not None:
            raise domain_error(409, "adapter_busy", "The Adapter already has an active Execution")
        owner = session.scalar(
            select(AdapterWebhook.adapter_id).where(
                AdapterWebhook.public_id == data.public_id,
                AdapterWebhook.enabled.is_(True),
                AdapterWebhook.adapter_id != adapter.id,
            )
        )
        if owner is not None:
            raise domain_error(
                409,
                "webhook_path_in_use",
                f"Webhook path '{data.public_id}' is used by another running Adapter",
                {"path": data.public_id},
            )

    webhook.enabled = data.enabled
    webhook.public_id = data.public_id
    webhook.credential_id = credential.id if credential is not None else None
    try:
        session.commit()
    except IntegrityError as error:
        session.rollback()
        constraint = getattr(getattr(error.orig, "diag", None), "constraint_name", None)
        if constraint == "uq_adapter_webhooks_enabled_public_id":
            raise domain_error(
                409,
                "webhook_path_in_use",
                f"Webhook path '{data.public_id}' is used by another running Adapter",
                {"path": data.public_id},
            ) from None
        raise
    session.refresh(webhook)
    return _webhook_response(session, webhook)


# --- External ingress ----------------------------------------------------------------


def _require_bearer(session: Session, webhook: AdapterWebhook, authorization: str | None) -> None:
    """401 unless the Bearer token matches the Credential's token value.

    The comparison is constant-time; plaintext appears only in memory for
    the duration of this request. The response never distinguishes "wrong
    token" from "missing token".
    """
    provided = _bearer_token(authorization)
    if webhook.credential_id is None:
        raise RuntimeError("enabled webhook has no credential")
    credential = session.get(Credential, webhook.credential_id)
    if credential is None:
        # RESTRICT normally makes this impossible.
        raise RuntimeError("webhook references a missing credential")
    expected = decrypt_fields(credential.ciphertext)[WEBHOOK_CREDENTIAL_TYPE]
    if provided is None or not stdlib_secrets.compare_digest(provided, expected):
        raise domain_error(401, "unauthorized", "Invalid or missing bearer token")


def receive_webhook(
    session: Session, public_id: str, authorization: str | None, body: bytes
) -> Execution:
    """Validate one external Webhook request and create its Execution.

    Order is fixed: body stream cap (route) -> unknown/disabled public_id ->
    404; token -> 401; unified runtime gate -> 409 family;
    body contract -> 400 (invalid or non-standard JSON, or strings JSONB
    cannot persist) and 413 (compact JSON over the Execution input cap).
    The Adapter row is locked before the Webhook row (platform lock
    order), so a concurrent Stop / Start / PUT is fully serialized with
    receipt.
    """
    # Re-check enabled under the row lock. ``populate_existing`` prevents the
    # unlocked routing lookup from masking a concurrent Stop in the identity map.
    webhook = session.scalar(
        select(AdapterWebhook).where(
            AdapterWebhook.public_id == public_id,
            AdapterWebhook.enabled.is_(True),
        )
    )
    if webhook is None:
        # Never leak Credential metadata for unknown ids.
        raise domain_error(404, "webhook_not_found", "Webhook not found")
    adapter = session.get(Adapter, webhook.adapter_id, with_for_update=True)
    if adapter is None:
        # Adapter deletion cascades the webhook; a racing delete lost nothing.
        raise domain_error(404, "webhook_not_found", "Webhook not found")
    webhook = session.scalar(
        select(AdapterWebhook)
        .where(
            AdapterWebhook.id == webhook.id,
            AdapterWebhook.enabled.is_(True),
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if webhook is None or webhook.adapter_id != adapter.id:
        raise domain_error(404, "webhook_not_found", "Webhook not found")
    _require_bearer(session, webhook, authorization)

    # --- unified runtime gate ------------------------------------------------
    if adapter.archived_at is not None:
        raise domain_error(409, "adapter_deleted", "Adapter is deleted")
    if adapter.adapter_type != "webhook":
        raise domain_error(409, "adapter_type_mismatch", "Only webhook Adapters support Webhook")
    if adapter.latest_version_id is None or adapter.runtime_worker_id is None:
        raise domain_error(409, "adapter_not_ready", "Save the Adapter and choose a runtime Worker")
    worker = session.get(Worker, adapter.runtime_worker_id)
    if worker is None or not worker_availability.is_effectively_online(
        worker, now=worker_availability.current_time(session)
    ):
        raise domain_error(409, "worker_offline", "The runtime Worker is offline")
    if adapter.language not in worker.capabilities:
        raise domain_error(
            409,
            "worker_capability_missing",
            f"The runtime Worker does not support {adapter.language}",
        )
    if adapter_runtime.active_execution(session, adapter.id) is not None:
        raise domain_error(409, "adapter_busy", "The Adapter already has an active Execution")

    # --- body contract: raw size, then JSON, then normalized size ------------
    # The raw byte cap is the ingress memory protection; the compact JSON
    # cap is the Execution input contract (the same big-field unit as
    # Manual / Schedule input). A raw-small body can still normalize above
    # the cap, so both checks run and both persist zero Executions.
    if len(body) > settings.execution_input_max_bytes:
        raise domain_error(
            413,
            "execution_input_too_large",
            f"Input exceeds the {settings.execution_input_max_bytes} byte limit",
            {"max_bytes": settings.execution_input_max_bytes},
        )
    try:
        payload: Any = json.loads(
            body,
            parse_constant=_reject_non_standard_constant,
            parse_float=_parse_finite_float,
        )
        _require_jsonb_persistable(payload)
    except (UnicodeDecodeError, ValueError) as error:
        raise domain_error(400, "webhook_body_invalid_json", "Body must be valid JSON") from error
    if len(compact_json_bytes(payload)) > settings.execution_input_max_bytes:
        raise domain_error(
            413,
            "execution_input_too_large",
            f"Input exceeds the {settings.execution_input_max_bytes} byte limit",
            {"max_bytes": settings.execution_input_max_bytes},
        )

    execution = Execution(
        adapter_id=adapter.id,
        version_id=adapter.latest_version_id,
        trigger="webhook",
        status="pending",
        target_worker_id=adapter.runtime_worker_id,
        input=payload,
        input_source_type="json",
        input_config_revision=1,
        input_snapshot={"source_type": "json", "revision": 1},
        locale=get_system_locale(session),
    )
    # Retention is a unified periodic service, not an inline side effect of
    # accepting a request.  This keeps receipt latency bounded and lets a
    # failed cleanup retry in small batches without touching active work.
    session.add(execution)
    try:
        session.flush()
    except IntegrityError:
        # Lost the race against a concurrently created active
        # Execution: the partial unique index is the final defense.
        session.rollback()
        raise domain_error(
            409, "adapter_busy", "The Adapter already has an active Execution"
        ) from None
    session.commit()
    session.refresh(execution)
    logger.info("webhook accepted: adapter=%s execution=%s", adapter.id, execution.id)
    return execution
