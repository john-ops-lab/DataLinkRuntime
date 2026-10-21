"""Migration-only adapter around the existing Assist implementation."""

from dlr.control.ai.runtime.contracts import PreparedAssist, RuntimeOutcome, RuntimeResources


class LegacyRuntime:
    def execute(self, prepared: PreparedAssist, resources: RuntimeResources) -> RuntimeOutcome:
        return RuntimeOutcome.from_response(resources.legacy_execute(prepared))
