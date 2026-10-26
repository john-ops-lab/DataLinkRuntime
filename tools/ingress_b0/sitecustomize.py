"""Enabled only for a B0 process explicitly given both diagnostic settings."""

import os

if os.environ.get("DLR_B0_ROLE") in {"control", "worker"} and os.environ.get(
    "DLR_B0_DIAGNOSTICS_DIR"
):
    try:
        import observe

        observe.install()
    except Exception:  # noqa: BLE001, S110 - opt-in diagnostics must fail open
        # Optional diagnosis never changes the application's import outcome.
        pass
