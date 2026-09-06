"""M3.2 Secret Store: credential encryption, CRUD, bindings and resolution.

Security contract:

- Plaintext field values are encrypted with Fernet (authenticated symmetric
  encryption) before touching the database; only ciphertext is persisted.
- The Fernet key is derived from the deployment-level Master Key
  (``DLR_MASTER_KEY``) via HKDF-SHA256, so any strong passphrase works and
  the Master Key itself is never stored.
- Without a configured Master Key every credential API answers 503 instead
  of falling back to plaintext storage.
- ``resolve_adapter_secrets`` decrypts exactly the fields one Adapter bound
  (env_key -> value), so an Execution only ever receives the secrets it
  needs. Called at claim time; the values travel inside the TaskPayload.
"""

import base64
import json
import re
import secrets as stdlib_secrets

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlalchemy import delete, exists, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from dlr.common.config import settings
from dlr.control.models import (
    Adapter,
    AdapterWebhook,
    Execution,
    ExecutionCredentialBindingSnapshot,
)
from dlr.control.models.platform import (
    CREDENTIAL_FIELDS,
    LEGACY_ACCESS_KEY_FIELDS,
    AdapterCredentialBinding,
    Credential,
)
from dlr.control.schemas.credential import BindingResponse, CredentialCreate, CredentialUpdate
from dlr.control.services.adapter import domain_error
from dlr.control.services.adapter_runtime import require_runtime_unlocked

# Stable key-derivation parameters; changing either rotates every key.
_HKDF_SALT = b"dlr-secret-store-v1"
_HKDF_INFO = b"dlr-fernet-key"

# Environment-variable style binding keys (injected as DLR_SECRET_<env_key>).
ENV_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _require_secret_store() -> None:
    """503 when the deployment has no Master Key: never store plaintext."""
    if not settings.master_key:
        raise domain_error(
            503,
            "secret_store_unavailable",
            "DLR_MASTER_KEY is not configured; credential storage is disabled",
        )


def _fernet() -> Fernet:
    if not settings.master_key:
        raise domain_error(
            503,
            "secret_store_unavailable",
            "DLR_MASTER_KEY is not configured; credential storage is disabled",
        )
    derived = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=_HKDF_SALT,
        info=_HKDF_INFO,
    ).derive(settings.master_key.encode())
    return Fernet(base64.urlsafe_b64encode(derived))


def encrypt_fields(fields: dict[str, str]) -> str:
    """Encrypt one credential's field map into the persisted ciphertext."""
    return _fernet().encrypt(json.dumps(fields, ensure_ascii=False).encode()).decode()


def decrypt_fields(ciphertext: str) -> dict[str, str]:
    """Decrypt one credential's field map; corrupt data is a server error.

    Access-key credentials encrypted before the M5.5.7 field rename
    (``access_key`` / ``secret_key``) are transparently mapped to the
    standardized ``access_key_id`` / ``access_key_secret`` names on read, so
    legacy bindings never break. Plaintext is only ever held in memory.
    """
    try:
        value = json.loads(_fernet().decrypt(ciphertext.encode()))
    except (InvalidToken, ValueError) as error:
        raise domain_error(
            500,
            "secret_store_decrypt_failed",
            "Credential ciphertext could not be decrypted (wrong Master Key?)",
        ) from error
    if not isinstance(value, dict):
        raise domain_error(500, "secret_store_decrypt_failed", "Credential ciphertext is malformed")
    fields = {str(key): str(item) for key, item in value.items()}
    if "access_key" in fields or "secret_key" in fields:
        for legacy, current in LEGACY_ACCESS_KEY_FIELDS.items():
            if legacy in fields and current not in fields:
                fields[current] = fields.pop(legacy)
    return fields


def _validate_fields(credential_type: str, fields: dict[str, str]) -> None:
    """Fields must match the type's schema exactly, with non-empty values."""
    expected = set(CREDENTIAL_FIELDS[credential_type])
    if set(fields) != expected:
        raise domain_error(
            422,
            "credential_fields_invalid",
            f"Credential type '{credential_type}' requires fields {sorted(expected)}",
            {"credential_type": credential_type, "fields": sorted(expected)},
        )
    for key, value in fields.items():
        if not value:
            raise domain_error(
                422,
                "credential_fields_invalid",
                f"Credential field '{key}' must not be empty",
                {"credential_type": credential_type, "field": key},
            )


# --- credential CRUD ---------------------------------------------------------------


def create_credential(session: Session, data: CredentialCreate) -> Credential:
    _require_secret_store()
    _validate_fields(data.type, data.fields)
    existing = session.scalar(select(Credential).where(Credential.name == data.name))
    if existing is not None:
        raise domain_error(409, "credential_name_conflict", "Credential name already exists")
    credential = Credential(name=data.name, type=data.type, ciphertext=encrypt_fields(data.fields))
    session.add(credential)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise domain_error(
            409, "credential_name_conflict", "Credential name already exists"
        ) from None
    session.refresh(credential)
    return credential


def list_credentials(session: Session) -> list[Credential]:
    _require_secret_store()
    return list(session.scalars(select(Credential).order_by(Credential.id.asc())).all())


def get_credential(session: Session, credential_id: int) -> Credential:
    _require_secret_store()
    credential = session.get(Credential, credential_id)
    if credential is None:
        raise domain_error(404, "credential_not_found", "Credential not found")
    return credential


def update_credential(session: Session, credential_id: int, data: CredentialUpdate) -> Credential:
    _require_secret_store()
    credential = session.get(
        Credential,
        credential_id,
        with_for_update=data.fields is not None,
    )
    if credential is None:
        raise domain_error(404, "credential_not_found", "Credential not found")

    # Token values are runtime configuration. Serialize value replacement
    # against Webhook Start on the Credential row, then reject while any
    # referencing Webhook is enabled or still has an active call. Name-only
    # edits remain metadata-only and do not need the runtime lock.
    if data.fields is not None:
        locked_webhook = session.scalar(
            select(AdapterWebhook.adapter_id)
            .where(
                AdapterWebhook.credential_id == credential_id,
                or_(
                    AdapterWebhook.enabled.is_(True),
                    exists(
                        select(Execution.id).where(
                            Execution.adapter_id == AdapterWebhook.adapter_id,
                            Execution.status.in_(("pending", "running")),
                        )
                    ),
                ),
            )
            .limit(1)
        )
        if locked_webhook is not None:
            raise domain_error(
                409,
                "credential_webhook_runtime_locked",
                "Stop every Webhook using this Credential and wait for active calls "
                "to finish before changing its value",
            )

        _validate_fields(credential.type, data.fields)

    if data.name is not None and data.name != credential.name:
        conflict = session.scalar(
            select(Credential).where(Credential.name == data.name, Credential.id != credential_id)
        )
        if conflict is not None:
            raise domain_error(409, "credential_name_conflict", "Credential name already exists")
        credential.name = data.name
    if data.fields is not None:
        credential.ciphertext = encrypt_fields(data.fields)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise domain_error(
            409, "credential_name_conflict", "Credential name already exists"
        ) from None
    session.refresh(credential)
    return credential


def delete_credential(session: Session, credential_id: int) -> None:
    credential = get_credential(session, credential_id)
    try:
        session.delete(credential)
        session.commit()
    except IntegrityError:
        session.rollback()
        raise domain_error(
            409,
            "credential_in_use",
            "Credential is still referenced (Adapter binding, Webhook or platform setting) "
            "and cannot be deleted",
        ) from None


# --- demo credential bootstrap -------------------------------------------------

# M5.5.7 demo Credentials referenced by the default Adapter bindings and by
# the three-language Starter Code. Values are generated fresh per deployment;
# nothing fixed or predictable ever lives in the repository, image or Compose.
DEMO_PASSWORD_CREDENTIAL_NAME = "demo-passwd"
DEMO_TOKEN_CREDENTIAL_NAME = "demo-token"


def bootstrap_demo_credentials(session: Session) -> None:
    """Create the demo Credentials with fresh random values (idempotent).

    Called at Control startup. Values are random per deployment, encrypted
    at rest and never returned by any API, so nothing readable or
    predictable is exposed. Without a configured Master Key the bootstrap is
    skipped; new Adapters then simply start without demo bindings.
    """
    if not settings.master_key:
        return
    specs = (
        (
            DEMO_PASSWORD_CREDENTIAL_NAME,
            "password",
            {"username": "demo", "password": stdlib_secrets.token_hex(16)},
        ),
        (
            DEMO_TOKEN_CREDENTIAL_NAME,
            "token",
            {"token": stdlib_secrets.token_hex(16)},
        ),
    )
    for name, credential_type, fields in specs:
        existing = session.scalar(select(Credential.id).where(Credential.name == name))
        if existing is not None:
            continue
        session.add(Credential(name=name, type=credential_type, ciphertext=encrypt_fields(fields)))
    session.commit()


def demo_credential_id(session: Session, name: str) -> int | None:
    """Resolve one demo Credential id by name, or None when absent."""
    return session.scalar(select(Credential.id).where(Credential.name == name))


# --- adapter bindings ----------------------------------------------------------------


def list_adapter_bindings(session: Session, adapter_id: int) -> list[BindingResponse]:
    adapter = session.get(Adapter, adapter_id)
    if adapter is None:
        raise domain_error(404, "adapter_not_found", "Adapter not found")
    rows = session.execute(
        select(AdapterCredentialBinding, Credential)
        .join(Credential, Credential.id == AdapterCredentialBinding.credential_id)
        .where(AdapterCredentialBinding.adapter_id == adapter_id)
        .order_by(AdapterCredentialBinding.id.asc())
    ).all()
    return [
        BindingResponse(
            env_key=binding.env_key,
            credential_id=binding.credential_id,
            field=binding.field,
            credential_name=credential.name,
            credential_type=credential.type,
        )
        for binding, credential in rows
    ]


def set_adapter_bindings(
    session: Session, adapter_id: int, items: list[tuple[str, int, str]]
) -> list[BindingResponse]:
    """Replace the Adapter's binding set; validates every row up front.

    ``items`` holds already-schema-validated ``(env_key, credential_id,
    field)`` triples. The whole replacement happens in one transaction.
    """
    adapter = session.get(Adapter, adapter_id, with_for_update=True)
    if adapter is None:
        raise domain_error(404, "adapter_not_found", "Adapter not found")
    if adapter.archived_at is not None:
        raise domain_error(409, "adapter_deleted", "Adapter is deleted")
    require_runtime_unlocked(session, adapter)

    seen_env_keys: set[str] = set()
    for env_key, credential_id, field in items:
        if not ENV_KEY_PATTERN.match(env_key):
            raise domain_error(
                422,
                "binding_env_key_invalid",
                "env_key must be an environment variable name (letters, digits, underscore)",
                {"env_key": env_key},
            )
        if env_key in seen_env_keys:
            raise domain_error(
                422,
                "binding_env_key_invalid",
                f"env_key '{env_key}' is bound twice",
                {"env_key": env_key},
            )
        seen_env_keys.add(env_key)
        credential = session.get(Credential, credential_id)
        if credential is None:
            raise domain_error(404, "credential_not_found", "Credential not found")
        if field not in CREDENTIAL_FIELDS[credential.type]:
            raise domain_error(
                422,
                "binding_field_invalid",
                f"Credential type '{credential.type}' has no field '{field}'",
                {"credential_type": credential.type, "field": field},
            )

    session.execute(
        delete(AdapterCredentialBinding).where(AdapterCredentialBinding.adapter_id == adapter_id)
    )
    for env_key, credential_id, field in items:
        session.add(
            AdapterCredentialBinding(
                adapter_id=adapter_id,
                env_key=env_key,
                credential_id=credential_id,
                field=field,
            )
        )
    session.commit()
    return list_adapter_bindings(session, adapter_id)


def resolve_adapter_secrets(session: Session, adapter_id: int) -> dict[str, str]:
    """env_key -> decrypted value for every binding of one Adapter.

    Called at claim time; an Execution only ever receives the secrets its
    own Adapter bound. Missing Master Key with existing bindings is a hard
    503: the Execution must fail loudly instead of running without secrets.
    """
    rows = session.scalars(
        select(AdapterCredentialBinding)
        .where(AdapterCredentialBinding.adapter_id == adapter_id)
        .order_by(AdapterCredentialBinding.id.asc())
    ).all()
    if not rows:
        return {}
    secrets: dict[str, str] = {}
    for binding in rows:
        credential = session.get(Credential, binding.credential_id)
        if credential is None:
            # RESTRICT normally makes this impossible.
            raise RuntimeError("binding references a missing credential")
        secrets[binding.env_key] = decrypt_fields(credential.ciphertext)[binding.field]
    return secrets


def resolve_execution_secrets(session: Session, execution: Execution) -> dict[str, str]:
    """Decrypt the immutable credential binding snapshot of one Execution.

    RabbitMQ rows must not resolve the Adapter's current bindings at Claim
    time: an administrator may have replaced those bindings after acceptance.
    """
    snapshots = execution.credential_bindings_snapshot
    if not isinstance(snapshots, list):
        raise RuntimeError("execution credential binding snapshot is malformed")
    if not snapshots:
        return {}
    rows = session.scalars(
        select(ExecutionCredentialBindingSnapshot)
        .where(ExecutionCredentialBindingSnapshot.execution_id == execution.id)
        .order_by(ExecutionCredentialBindingSnapshot.id.asc())
    ).all()
    if len(rows) != len(snapshots):
        raise RuntimeError("execution credential binding snapshot is incomplete")
    result: dict[str, str] = {}
    for snapshot, row in zip(snapshots, rows, strict=True):
        if not isinstance(snapshot, dict):
            raise RuntimeError("execution credential binding snapshot is malformed")
        if (
            snapshot.get("binding_id") != row.binding_id
            or snapshot.get("credential_id") != row.credential_id
            or snapshot.get("env_key") != row.env_key
            or snapshot.get("field") != row.field
        ):
            raise RuntimeError("execution credential binding snapshot does not match its rows")
        credential = session.get(Credential, row.credential_id)
        if credential is None:
            raise RuntimeError("execution binding references a missing credential")
        fields = decrypt_fields(credential.ciphertext)
        try:
            result[row.env_key] = fields[row.field]
        except KeyError as error:
            raise domain_error(
                500,
                "secret_store_decrypt_failed",
                "Credential does not contain its snapshotted field",
            ) from error
    return result
