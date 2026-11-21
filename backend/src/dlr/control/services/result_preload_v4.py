"""Single-use result child. Idle startup has no deployment configuration."""

from __future__ import annotations

import os
import socket
import sys

from dlr.control.services.claim_child_v4 import _clock, _read_frame, _restrict

IDLE_SECONDS = 5.0


def _load_dependencies() -> None:
    import fastapi  # noqa: F401
    import psycopg  # noqa: F401
    import pydantic_settings  # noqa: F401
    from cryptography.hazmat.primitives.asymmetric import ed25519  # noqa: F401
    from sqlalchemy.orm import configure_mappers

    from dlr.control import models  # noqa: F401
    from dlr.control.schemas import reliable_runtime  # noqa: F401

    configure_mappers()
    if "dlr.common.config" in sys.modules or "dlr.control.db" in sys.modules:
        raise RuntimeError("result preload imported deployment configuration")


def main() -> int:
    try:
        fd, parent_pid, address_space = map(int, sys.argv[1:])
        _restrict(parent_pid, address_space)
        with socket.socket(fileno=fd) as channel:
            _load_dependencies()
            channel.settimeout(IDLE_SECONDS)
            channel.sendall(b"R")
            request = _read_frame(channel)
            snapshot = request["settings"]
            if (
                request.get("operation") not in {"stage", "finalize"}
                or type(snapshot) is not dict
                or snapshot.get("control_ha_slot") not in {"a", "b"}
                or snapshot.get("worker_v4_control_slots") != 2
                or snapshot.get("worker_v4_claim_child_address_space_bytes") != address_space
                or snapshot.get("postgres_capacity_token") is not None
            ):
                raise ValueError("invalid result handoff")
            # These are needed by the import-time Settings validator and the
            # cgroup envelope. The original result protocol validates the full
            # settings snapshot before importing any service or opening a DB.
            for name in ("rabbitmq_url", "rabbitmq_management_url"):
                value = snapshot[name]
                if type(value) is not str or not value:
                    raise ValueError("invalid result configuration")
                os.environ["DLR_" + name.upper()] = value
            os.environ["DLR_WORKER_V4_CONTROL_SLOTS"] = "2"
            from dlr.control.services.result_child_v4 import _run

            channel.settimeout(min(5.0, request["control_deadline"] - _clock()))
            _run(channel, address_space, _first_request=request)
        return 0
    except BaseException:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
