"""Ordinary Python Task: send bounded Kubernetes events to a transactional target.

The target, rather than an HTTP header alone, must persist batch generation,
UID membership, cleanup eligibility and the result of every control operation.
"""

import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

CONFIG = {"target_url": "https://target.example.test/events", "timeout_seconds": 5}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def handle(context, input):
    if (
        not isinstance(input, dict)
        or input.get("schema") != "dlr.event.v1"
        or input.get("source", {}).get("type") != "kubernetes"
        or input.get("event", {}).get("kind")
        not in {"object.present", "object.removed", "sync.started", "sync.completed", "sync.failed"}
    ):
        raise ValueError("kubernetes_event_required")
    config = {**CONFIG, **(context.config if isinstance(context.config, dict) else {})}
    parsed = urlsplit(config["target_url"])
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ValueError("target_url_invalid")
    timeout = config["timeout_seconds"]
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 30:
        raise ValueError("target_timeout_invalid")
    token = context.secrets.get("EVENT_TARGET_TOKEN")
    if not token:
        raise ValueError("event_target_credential_required")
    ident = input["event"]["id"]
    body = json.dumps(input, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
    if len(body) > 1024 * 1024:
        raise ValueError("target_request_too_large")
    request = Request(
        config["target_url"],
        data=body,
        method="POST",
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            "Idempotency-Key": ident,
        },
    )
    try:
        with build_opener(NoRedirect()).open(request, timeout=timeout) as response:
            raw = response.read(8193)
            if len(raw) > 8192:
                raise ValueError("target_response_too_large")
        outcome = json.loads(raw)
    except HTTPError as error:
        error.close()
        raise RuntimeError("event_target_rejected") from None
    except (URLError, OSError):
        # The remote transaction may already have committed; replay the same event.
        raise RuntimeError("event_target_result_unknown") from None
    if not isinstance(outcome, dict) or outcome.get("source_event_id") != ident:
        raise ValueError("target_response_invalid")
    return outcome
