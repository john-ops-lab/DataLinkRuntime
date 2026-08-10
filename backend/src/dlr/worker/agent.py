"""DLR Worker Agent (M0 stub).

Long-term positioning: an agent that actively connects to the Control Node
to fetch and execute tasks (outbound-only, no inbound HTTP service).

M0 scope: start up, mark readiness via a file, and stay alive until a
termination signal arrives. Heartbeat and task polling land in M2.
"""

import logging
import os
import signal
import threading
from pathlib import Path

logger = logging.getLogger("dlr.worker")

READY_FILE_ENV = "DLR_WORKER_READY_FILE"
DEFAULT_READY_FILE = "/tmp/dlr-worker.ready"

_stop = threading.Event()


def _handle_signal(signum: int, _frame: object) -> None:
    logger.info("received signal %s, shutting down", signum)
    _stop.set()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    ready_file = Path(os.environ.get(READY_FILE_ENV, DEFAULT_READY_FILE))
    ready_file.write_text(str(os.getpid()), encoding="utf-8")
    logger.info("worker agent started (pid=%s), ready file: %s", os.getpid(), ready_file)

    while not _stop.is_set():
        _stop.wait(timeout=5.0)

    ready_file.unlink(missing_ok=True)
    logger.info("worker agent stopped")


if __name__ == "__main__":
    main()
