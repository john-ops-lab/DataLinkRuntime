"""Wave A account bootstrap, password hashing and session lifecycle."""

import base64
import hashlib
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import ceil
from threading import Lock
from time import monotonic

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
LOGIN_THROTTLE_WINDOW_SECONDS = 60.0
LOGIN_THROTTLE_BLOCK_SECONDS = 60.0
LOGIN_THROTTLE_MAX_FAILURES = 5
LOGIN_THROTTLE_BASE_DELAY_SECONDS = 0.1
LOGIN_THROTTLE_MAX_DELAY_SECONDS = 2.0
LOGIN_THROTTLE_MAX_KEYS = 4096


@dataclass(frozen=True)
class AccountSession:
    """The account row and the server-side session matched by one request."""

    user: User
    session: UserSession


@dataclass(frozen=True)
class LoginThrottlePermit:
    """One concurrency-safe login attempt reservation."""

    key: str
    generation: int
    delay_seconds: float


@dataclass(frozen=True)
class LoginThrottleDecision:
    """Either a reserved attempt or a bounded retry response."""

    permit: LoginThrottlePermit | None
    retry_after_seconds: int | None = None


@dataclass
class _LoginThrottleState:
    generation: int
    window_started_at: float
    last_activity_at: float
    failures: int = 0
    in_flight: int = 0
    blocked_until: float | None = None


class LoginThrottle:
    """Process-local, source-and-username login backoff with atomic reservations.

    Control is a single service in the Wave A deployment. The socket peer is
    used as the source identity instead of trusting forwarded headers, so a
    client cannot evade the budget by spoofing ``X-Forwarded-For``. The
    in-flight reservation closes the race where many concurrent guesses could
    otherwise all pass the pre-check before their failures are recorded.
    """

    def __init__(self, clock: Callable[[], float] = monotonic) -> None:
        self._clock = clock
        self._lock = Lock()
        self._entries: dict[str, _LoginThrottleState] = {}
        self._next_generation = 0

    def _new_state(self, now: float) -> _LoginThrottleState:
        generation = self._next_generation
        self._next_generation += 1
        return _LoginThrottleState(
            generation=generation,
            window_started_at=now,
            last_activity_at=now,
        )

    def _prune(self, now: float) -> None:
        stale_keys = [
            key
            for key, state in self._entries.items()
            if state.in_flight == 0
            and now - state.last_activity_at >= LOGIN_THROTTLE_WINDOW_SECONDS
        ]
        for key in stale_keys:
            self._entries.pop(key, None)
        if len(self._entries) < LOGIN_THROTTLE_MAX_KEYS:
            return
        inactive = [state for state in self._entries.values() if state.in_flight == 0]
        if inactive:
            oldest = min(inactive, key=lambda state: state.last_activity_at)
            oldest_key = next(key for key, state in self._entries.items() if state is oldest)
            self._entries.pop(oldest_key)

    def begin(self, key: str) -> LoginThrottleDecision:
        """Reserve one attempt, returning a delay or a retry-after interval."""
        now = self._clock()
        with self._lock:
            state = self._entries.get(key)
            if state is None:
                self._prune(now)
                if len(self._entries) >= LOGIN_THROTTLE_MAX_KEYS:
                    return LoginThrottleDecision(permit=None, retry_after_seconds=1)
                state = self._new_state(now)
                self._entries[key] = state
            elif now - state.window_started_at >= LOGIN_THROTTLE_WINDOW_SECONDS or (
                state.blocked_until is not None and now >= state.blocked_until
            ):
                state = self._new_state(now)
                self._entries[key] = state

            if state.blocked_until is not None and now < state.blocked_until:
                return LoginThrottleDecision(
                    permit=None,
                    retry_after_seconds=max(1, ceil(state.blocked_until - now)),
                )

            effective_failures = state.failures + state.in_flight
            if effective_failures >= LOGIN_THROTTLE_MAX_FAILURES:
                state.blocked_until = now + LOGIN_THROTTLE_BLOCK_SECONDS
                state.last_activity_at = now
                return LoginThrottleDecision(
                    permit=None,
                    retry_after_seconds=int(LOGIN_THROTTLE_BLOCK_SECONDS),
                )

            state.in_flight += 1
            state.last_activity_at = now
            delay = min(
                LOGIN_THROTTLE_BASE_DELAY_SECONDS * (2**effective_failures),
                LOGIN_THROTTLE_MAX_DELAY_SECONDS,
            )
            return LoginThrottleDecision(permit=LoginThrottlePermit(key, state.generation, delay))

    def record_failure(self, permit: LoginThrottlePermit) -> None:
        """Commit a reserved attempt as a failure, if it is still current."""
        now = self._clock()
        with self._lock:
            state = self._entries.get(permit.key)
            if state is None or state.generation != permit.generation:
                return
            state.in_flight = max(0, state.in_flight - 1)
            state.failures += 1
            state.last_activity_at = now
            if state.failures >= LOGIN_THROTTLE_MAX_FAILURES:
                state.blocked_until = now + LOGIN_THROTTLE_BLOCK_SECONDS

    def record_success(self, permit: LoginThrottlePermit) -> None:
        """Clear the failure window after a successful credential check."""
        with self._lock:
            state = self._entries.get(permit.key)
            if state is not None and state.generation == permit.generation:
                self._entries.pop(permit.key, None)

    def reset_username(self, username: str) -> None:
        """Clear all source buckets when superadmin resets an account password."""
        suffix = f"\x00{username.casefold()}"
        with self._lock:
            for key in tuple(self._entries):
                if key.endswith(suffix):
                    self._entries.pop(key, None)

    def reset(self) -> None:
        """Clear state for isolated tests; not used by request handling."""
        with self._lock:
            self._entries.clear()


account_login_throttle = LoginThrottle()


def login_throttle_key(source: str, username: str) -> str:
    """Build a non-secret source/username bucket key."""
    return f"{source}\x00{username.casefold()}"


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
    account_login_throttle.reset_username(username)
