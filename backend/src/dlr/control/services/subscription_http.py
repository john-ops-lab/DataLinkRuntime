"""Passive HTTP owners use the same fenced Runtime lease as pull connectors."""

from typing import Any

from dlr.common.config import settings
from dlr.control.services.subscription_source import SourceFailure, SourceRunContext


class HttpSourceRunner:
    def run(self, context: SourceRunContext) -> None:
        context.connected()
        while not context.stop_event.wait(0.25):
            if not settings.http_ingress_enabled:
                raise SourceFailure("source_disabled", "backoff")

    def test_connection(
        self, source_config: dict[str, Any], credential_fields: dict[str, str]
    ) -> dict[str, Any]:
        if not credential_fields.get("token"):
            raise SourceFailure("source_credential_unavailable")
        return {"cluster_id": "http", "identity_verified": True}
