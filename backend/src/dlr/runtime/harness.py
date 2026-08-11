"""Python Runtime harness (M2 Runtime Contract).

Executed by the version-scoped venv's own Python as a plain script:

    <venv>/bin/python harness.py <workspace>

Intentionally stdlib-only so it runs inside any adapter venv without extra
installs. The workspace is a per-execution directory prepared by the Worker:

    adapter.py           immutable AdapterVersion snapshot
    input.json           execution input (any JSON value, including null)
    runtime_config.json  version runtime_config (JSON object)
    output.json          written by the harness with the return value

Any exception (including a non-JSON-serializable return value) prints a
traceback to stderr and exits non-zero, which the Worker reports as failed.
"""

import importlib.util
import json
import logging
import os
import sys
import traceback
from pathlib import Path
from types import ModuleType


class Secrets:
    """Read-through access to Worker-provided ``DLR_SECRET_<KEY>`` values.

    Real secret values never pass through the Control Node or the database.
    """

    def get(self, key: str) -> str | None:
        return os.environ.get(f"DLR_SECRET_{key}")


class Context:
    """The ``context`` object passed to ``handle(context, input)``."""

    def __init__(self, config: object, secrets: Secrets, logger: logging.Logger) -> None:
        self.config = config
        self.secrets = secrets
        self.logger = logger


def _build_logger() -> logging.Logger:
    logger = logging.getLogger("dlr.adapter")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    logger.propagate = False
    return logger


def _load_adapter(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location("dlr_adapter", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load adapter module from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    workspace = Path(sys.argv[1])
    input_value = json.loads((workspace / "input.json").read_text(encoding="utf-8"))
    config = json.loads((workspace / "runtime_config.json").read_text(encoding="utf-8"))

    module = _load_adapter(workspace / "adapter.py")
    context = Context(config=config, secrets=Secrets(), logger=_build_logger())
    result = module.handle(context, input_value)

    # A non-serializable return value raises here and becomes a failure.
    (workspace / "output.json").write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(1)
