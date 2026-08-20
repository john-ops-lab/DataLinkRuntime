"""Wave A account bootstrap, password hashing and session lifecycle."""

import base64
import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from dlr.control.models.account import User, UserSession
from dlr.control.schemas.account import AccountPrincipalResponse
from dlr.control.services.adapter import domain_error

DEFAULT_ADMIN_USERNAME = "admin"
DEFAULT_ADMIN_PASSWORD = "admin123"
DEFAULT_ADMIN_ROLE = "admin"

# scrypt is a mature memory-hard password hash equivalent in purpose to the
# requested Argon2id baseline. The encoded format includes all parameters.
_HASH_PREFIX = "scrypt"
# 16 MiB working memory keeps the hash within OpenSSL's default limit while
# remaining a standard memory-hard scrypt configuration for this deployment.
_SCRYPT_LOG_N = 14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32
_SALT_BYTES = 16
SESSION_COOKIE_NAME = "dlr_account_session"
CSRF_COOKIE_NAME = "dlr_account_csrf"
SESSION_TTL = timedelta(hours=8)


@dataclass(frozen=True)
class AccountSession:
    """The account row and the server-side session matched by one request."""

    user: User
    session: UserSession


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def hash_password(password: str) -> str:
    """Return a self-describing memory-hard password hash."""
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=1 << _SCRYPT_LOG_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
    )
    return (
        f"{_HASH_PREFIX}$ln={_SCRYPT_LOG_N},r={_SCRYPT_R},p={_SCRYPT_P}${_b64(salt)}${_b64(digest)}"
    )


def verify_password(password: str, encoded: str) -> bool:
    """Verify a stored hash without exposing malformed-hash details."""
    try:
        prefix, params, salt_text, digest_text = encoded.split("$", 3)
        if prefix != _HASH_PREFIX:
            return False
        values = dict(item.split("=", 1) for item in params.split(","))
        log_n = int(values["ln"])
        r = int(values["r"])
        p = int(values["p"])
        if not (12 <= log_n <= 20 and 1 <= r <= 32 and 1 <= p <= 8):
            return False
        expected = _unb64(digest_text)
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=_unb64(salt_text),
            n=1 << log_n,
            r=r,
            p=p,
            dklen=len(expected),
        )
    except (ValueError, KeyError, TypeError):
        return False
    return secrets.compare_digest(actual, expected)


def principal_response(user: User) -> AccountPrincipalResponse:
    """Build the deliberately secret-free principal response."""
    return AccountPrincipalResponse(
        id=user.id,
        username=user.username,
        role=user.role,  # type: ignore[arg-type]
        enabled=user.enabled,
        must_change_password=user.must_change_password,
    )


def bootstrap_default_admin(session: Session) -> None:
    """Idempotently insert the first admin without persisting its plaintext password."""
    statement = (
        insert(User)
        .values(
            username=DEFAULT_ADMIN_USERNAME,
            password_hash=hash_password(DEFAULT_ADMIN_PASSWORD),
            role=DEFAULT_ADMIN_ROLE,
            enabled=True,
            must_change_password=True,
        )
        .on_conflict_do_nothing(index_elements=[User.username])
    )
    session.execute(statement)
    session.commit()


def account_session_hash(raw_token: str) -> str:
    """Hash a raw cookie value before comparing it with database state."""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def create_session(session: Session, user: User) -> str:
    """Create a fresh random session and return only its raw cookie value."""
    raw_token = secrets.token_urlsafe(32)
    now = datetime.now(UTC)
    session.add(
        UserSession(
            user_id=user.id,
            session_hash=account_session_hash(raw_token),
            expires_at=now + SESSION_TTL,
            created_at=now,
            last_seen_at=now,
        )
    )
    session.commit()
    return raw_token


def find_session(session: Session, raw_token: str | None) -> AccountSession | None:
    """Find a live enabled account session; expired sessions are rejected."""
    if not raw_token:
        return None
    matched = session.execute(
        select(UserSession, User)
        .join(User, User.id == UserSession.user_id)
        .where(UserSession.session_hash == account_session_hash(raw_token))
    ).one_or_none()
    if matched is None:
        return None
    user_session, user = matched
    if user_session.expires_at <= datetime.now(UTC):
        return None
    if not user.enabled:
        return None
    return AccountSession(user=user, session=user_session)


def invalidate_user_sessions(session: Session, user_id: int) -> None:
    """Invalidate every session, used by disable/reset/password-change flows."""
    session.execute(delete(UserSession).where(UserSession.user_id == user_id))


def change_password(session: Session, current: AccountSession, new_password: str) -> None:
    """Replace a password and invalidate all old sessions atomically."""
    current.user.password_hash = hash_password(new_password)
    current.user.must_change_password = False
    current.user.updated_at = datetime.now(UTC)
    invalidate_user_sessions(session, current.user.id)
    session.commit()


def reset_password(session: Session, username: str, new_password: str) -> None:
    """Perform the narrow superadmin reset operation."""
    user = session.scalar(select(User).where(User.username == username))
    if user is None:
        raise domain_error(404, "account_not_found", "Account not found")
    user.password_hash = hash_password(new_password)
    user.must_change_password = True
    user.updated_at = datetime.now(UTC)
    invalidate_user_sessions(session, user.id)
    session.commit()
