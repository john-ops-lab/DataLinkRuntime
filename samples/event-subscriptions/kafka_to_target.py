"""Ordinary Python Task Adapter; the target must atomically deduplicate business_id."""

import base64
import json
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

CONFIG = {"target_url": "https://target.example.test/events", "timeout_seconds": 5}
BUSINESS_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}\Z")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def handle(context, input):
    if (
        not isinstance(input, dict)
        or input.get("schema") != "dlr.event.v1"
        or input.get("source", {}).get("type") != "kafka"
        or input.get("event", {}).get("kind") != "message"
    ):
        raise ValueError("kafka_event_required")
    value = input["data"]["value"]
    if value is None:
        # The application must explicitly decide what a tombstone means.
        raise ValueError("tombstone_business_policy_required")
    try:
        payload = json.loads(base64.b64decode(value, validate=True).decode("utf-8"))
    except (ValueError, UnicodeError):
        raise ValueError("utf8_json_business_payload_required") from None
    business_id = payload.get("business_id") if isinstance(payload, dict) else None
    if not isinstance(business_id, str) or BUSINESS_ID.fullmatch(business_id) is None:
        raise ValueError("stable_business_id_required")
    config = {**CONFIG, **(context.config if isinstance(context.config, dict) else {})}
    target = urlsplit(config["target_url"])
    if (
        target.scheme not in {"http", "https"}
        or not target.hostname
        or target.username is not None
        or target.password is not None
        or target.fragment
    ):
        raise ValueError("target_url_invalid")
    timeout = config["timeout_seconds"]
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 30:
        raise ValueError("target_timeout_invalid")
    token = context.secrets.get("EVENT_TARGET_TOKEN")
    if not token:
        raise ValueError("event_target_credential_required")
    body = json.dumps(
        {"business_id": business_id, "payload": payload, "source_event_id": input["event"]["id"]},
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    request = Request(
        config["target_url"],
        data=body,
        method="POST",
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            "Idempotency-Key": business_id,
        },
    )
    try:
        with build_opener(NoRedirect()).open(request, timeout=timeout) as response:
            result = response.read(65537)
            if len(result) > 65536:
                raise ValueError("target_response_too_large")
        outcome = json.loads(result)
    except HTTPError as error:
        error.close()
        raise RuntimeError("event_target_rejected") from None
    except (URLError, OSError):
        # The target may already have committed: replay the same business ID.
        raise RuntimeError("event_target_result_unknown") from None
    if not isinstance(outcome, dict) or outcome.get("business_id") != business_id:
        raise ValueError("target_response_invalid")
    return {
        "business_id": business_id,
        "source_event_id": input["event"]["id"],
        "applied": outcome.get("applied"),
    }
