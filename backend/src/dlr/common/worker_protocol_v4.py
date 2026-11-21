"""Fixed protocol-v4 instance lease contract shared by Control and Worker."""

import re
from urllib.parse import urlsplit

# The Control renews a live instance to DB current time plus this duration.
# A first registration with a new UUID establishes the same duration; an
# idempotent retry of an existing UUID does not renew it.
INSTANCE_LEASE_SECONDS = 30
INSTANCE_HEARTBEAT_SECONDS = 5

# Subtract this margin when deriving a local deadline and use the same
# bound for signed elapsed-time observations. The deployment must bound
# relative clock movement across the lease, including PostgreSQL failover.
V4_LEASE_CLOCK_MARGIN_NS = 5_000_000_000
WORKER_CREDENTIAL_PREFIX = "dlrw4_"
WORKER_FORCE_STOP_CODE = "worker_force_stop"
ADAPTER_CLEANUP_CAPABILITY = "adapter_ephemeral_cleanup_v1"


def is_worker_credential(value: object) -> bool:
    """Recognize the reserved v4 credential namespace without exposing it."""
    return type(value) is str and re.fullmatch(r"dlrw4_[0-9a-f]{64}", value) is not None


def validate_control_url(
    url: str, *, _test_only_plaintext_loopback: bool = False, allow_query: bool = False
) -> None:
    """Reject unauthenticated v4 endpoints before credentials or work are sent."""
    try:
        if type(url) is not str or type(_test_only_plaintext_loopback) is not bool:
            raise ValueError
        parts = urlsplit(url)
        if (
            not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or "#" in url
            or ("?" in url and not allow_query)
            or any(char.isspace() or ord(char) < 32 for char in url)
            or (
                parts.scheme != "https"
                and not (
                    _test_only_plaintext_loopback
                    and parts.scheme == "http"
                    and parts.hostname in {"127.0.0.1", "::1"}
                )
            )
        ):
            raise ValueError
        _ = parts.port
    except (TypeError, ValueError):
        raise ValueError("invalid Control base URL") from None
