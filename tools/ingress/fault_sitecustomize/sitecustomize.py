"""Explicit isolated-only entry; optionally composes the frozen B0 observer."""

import os

if os.environ.get("DLR_B0_ROLE") in {"control", "worker"} and os.environ.get(
    "DLR_B0_DIAGNOSTICS_DIR"
):
    try:
        import observe

        observe.install()
    except Exception:  # noqa: BLE001, S110 - a missing diagnostic is reported as missing evidence
        pass

if (
    os.environ.get("DLR_INGRESS_FAULT_ENABLED") == "1"
    and os.environ.get("DLR_INGRESS_FAULT_SCOPE") == "issue222-isolated"
):
    try:
        import fault_probe

        fault_probe.install()
    except Exception:  # noqa: BLE001, S110 - absent fault proof is NO_COUNT, not a product failure
        pass
