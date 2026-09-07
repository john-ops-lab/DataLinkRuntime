"""Issue #144 API/DB assertions, run only inside a disposable smoke Control.

The outer harness supplies the phase and performs Docker lifecycle actions.
This process uses production HTTP endpoints; SQL reads verify persisted facts.
State contains synthetic IDs only, never credentials or Attempt tokens.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from dlr.control.db import SessionLocal
from dlr.control.models import AdapterExecutionSlot, ExecutionAttempt
from dlr.control.schemas.worker import REQUIRED_ISOLATION_CAPABILITIES
from sqlalchemy import select

BASE = "http://web/api"
TOKEN = os.environ["DLR_ADMIN_TOKEN"]
STATE = (
    Path(os.environ.get("DLR_ARTIFACT_STORE_ROOT", "/var/lib/dlr/artifacts"))
    / ".dlr-issue144-state.json"
)


def request(method, path, body=None, expected=200):
    raw = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path,
        data=raw,
        method=method,
        headers={
            "Authorization": "Bearer " + TOKEN,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as error:
        status, raw = error.code, error.read()
    result = json.loads(raw) if raw else None
    accepted = (expected,) if isinstance(expected, int) else expected
    detail = result.get("detail") if isinstance(result, dict) else None
    code = detail.get("code") if isinstance(detail, dict) else None
    assert status in accepted, (method, path, status, code)
    return result


def wait_ingress_ready(*, timeout=60):
    """Worker health precedes asynchronous verification of its broker topology."""
    deadline = time.monotonic() + timeout
    health = {}
    while time.monotonic() < deadline:
        health = request("GET", "/health", expected=(200, 503))
        if health["status"] == "ok" and health["rabbitmq"]["ingress"]["ready"] is True:
            return
        time.sleep(0.5)
    raise AssertionError("RabbitMQ ingress did not become ready within the deadline")


def worker(name=None):
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        matches = [
            w for w in request("GET", "/workers") if name is None or w["name"] == name
        ]
        for row in matches:
            capabilities = row["isolation_capabilities"]
            if (
                row["status"] == "online"
                and row["isolation_preflight_status"] == "passed"
                and all(
                    capabilities.get(key) is True
                    for key in REQUIRED_ISOLATION_CAPABILITIES
                )
            ):
                wait_ingress_ready()
                return row
        time.sleep(0.5)
    raise AssertionError(f"Worker {name} did not pass the production preflight")


def wait(execution_id, *, running=False, timeout=90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = request("GET", f"/executions/{execution_id}")
        assert row["dispatch_backend"] == "rabbitmq"
        if running and row["status"] == "running":
            return row
        if row["status"] not in {"queued", "running", "retry_wait"}:
            assert not running, (execution_id, row["status"], row.get("error_code"))
            return row
        time.sleep(0.2)
    raise AssertionError(f"Execution {execution_id} did not reach the expected state")


def receipt(execution_id, expected="succeeded"):
    with SessionLocal() as session:
        attempt = session.scalar(
            select(ExecutionAttempt)
            .where(
                ExecutionAttempt.execution_id == execution_id,
            )
            .order_by(ExecutionAttempt.attempt_no.desc())
            .limit(1)
        )
        assert attempt is not None and attempt.status == expected, execution_id
        isolated = attempt.cleanup_summary["sandbox"]
        assert isolated["status"] == "completed" and isolated["residue"] is False, (
            execution_id
        )
        slot = session.get(AdapterExecutionSlot, (attempt.adapter_id, 0))
        assert slot is not None and slot.active_attempt_id is None, execution_id


def save_state(state):
    STATE.write_text(json.dumps(state))
    STATE.chmod(0o600)


def create(prefix, name, worker_id, *, seconds=1):
    adapter = request(
        "POST",
        "/adapters",
        {
            "name": f"{prefix}-{name}",
            "language": "python",
            "adapter_type": "task",
        },
        201,
    )
    request("PATCH", f"/adapters/{adapter['id']}", {"runtime_worker_id": worker_id})
    request(
        "POST",
        f"/adapters/{adapter['id']}/versions",
        {
            "code": "import time\ndef handle(context, input):\n"
            "    print('ISSUE144_RUNNING',flush=True)\n"
            f"    time.sleep({seconds})\n    return {{'owner': '{name}'}}\n",
            "requirements": "",
            "runtime_config": {},
        },
        201,
    )
    return adapter["id"]


def run(adapter_id):
    return request("POST", f"/adapters/{adapter_id}/executions", {"input": {}}, 202)[
        "id"
    ]


def basic():
    primary = worker()
    prefix = "issue144-" + uuid.uuid4().hex[:10]
    state = {
        "prefix": prefix,
        "worker_a": primary["name"],
        "worker_a_id": primary["id"],
    }
    catalog = request("GET", "/templates/scenarios?page_size=48")
    assert (
        catalog["total"] == 17
        and sum(len(s["variants"]) for s in catalog["items"]) == 85
    )
    for language in ("python", "javascript", "java", "typescript", "go"):
        path = f"/templates/scenarios/json-mapping-cleaning/variants/{language}"
        variant = request("GET", path)
        adapter = request(
            "POST",
            path + "/instantiate",
            {
                "name": prefix + "-template-" + language,
                "expected_template_version": variant["template_version"],
            },
            201,
        )
        adapter_id = adapter["id"]
        assert adapter["latest_version_id"] is None
        assert (
            adapter["template_scenario_slug"] is None
            and adapter["template_version"] is None
        )
        assert request("GET", f"/adapters/{adapter_id}/versions") == []
        request(
            "PATCH", f"/adapters/{adapter_id}", {"runtime_worker_id": primary["id"]}
        )
        version = request(
            "POST",
            f"/adapters/{adapter_id}/versions",
            {key: variant[key] for key in ("code", "requirements", "runtime_config")},
            201,
        )
        assert version["seq"] == 1
        execution = request(
            "POST",
            f"/adapters/{adapter_id}/executions",
            {
                "input": {"records": [{"id": "1", "profile": {"name": "example"}}]},
            },
            202,
        )
        finished = wait(execution["id"], timeout=150)
        assert finished["status"] == "succeeded", (language, finished.get("error_code"))
        assert finished["output"] == {
            "records": [{"name": "example", "id": "1"}],
            "count": 1,
            "partial": False,
            "checkpoint": None,
        }, language
        receipt(execution["id"])
    slow = create(prefix, "sse-cancel", primary["id"], seconds=30)
    execution_id = run(slow)
    req = urllib.request.Request(
        BASE + f"/executions/{execution_id}/events",
        headers={
            "Authorization": "Bearer " + TOKEN,
        },
    )
    observed = False
    with urllib.request.urlopen(req, timeout=40) as response:
        event = ""
        for raw in response:
            line = raw.decode().strip()
            if line.startswith("event: "):
                event = line[7:]
            if event in {"log", "log_snapshot"} and "ISSUE144_RUNNING" in line:
                assert (
                    request("GET", f"/executions/{execution_id}")["status"] == "running"
                )
                observed = True
                break
    assert observed, "SSE must deliver an incremental log while the payload is running"
    request("POST", f"/executions/{execution_id}/cancel")
    assert wait(execution_id)["status"] == "cancelled"
    receipt(execution_id, "cancelled")
    state["quick_adapter"] = create(prefix, "after-restart", primary["id"])
    save_state(state)
    print(
        "issue144-api=PASS templates=17/85 five-languages=passed sse-running=passed cancel=passed"
    )


def after_restart():
    state = json.loads(STATE.read_text())
    primary = worker(state["worker_a"])
    assert primary["id"] == state["worker_a_id"]
    execution_id = run(state["quick_adapter"])
    assert wait(execution_id)["status"] == "succeeded"
    receipt(execution_id)
    print("issue144-restart=PASS registration-reused=true new-attempt=passed")


def start_pair():
    state = json.loads(STATE.read_text())
    first = worker(state["worker_a"])
    second = worker(state["worker_a"] + "-peer")
    assert first["id"] != second["id"]
    state["worker_b_id"] = second["id"]
    for suffix, target in (("a", first), ("b", second)):
        adapter_id = create(
            state["prefix"], "parallel-" + suffix, target["id"], seconds=20
        )
        state["execution_" + suffix] = run(adapter_id)
    wait(state["execution_a"], running=True)
    wait(state["execution_b"], running=True)
    assert request("GET", f"/executions/{state['execution_a']}")["status"] == "running"
    save_state(state)
    print("issue144-dual=RUNNING two-distinct-workers=true simultaneous-attempts=true")


def peer_survives():
    state = json.loads(STATE.read_text())
    second = worker(state["worker_a"] + "-peer")
    assert second["id"] == state["worker_b_id"]
    result = wait(state["execution_b"])
    assert result["status"] == "succeeded" and result["output"] == {
        "owner": "parallel-b"
    }
    receipt(state["execution_b"])
    # Cancel the stopped worker's logical execution; lease/fencing owns its
    # final state. This does not claim automatic cross-node failover.
    request("POST", f"/executions/{state['execution_a']}/cancel")
    print("issue144-dual=PASS peer-survived-stop-and-removal=true ha-not-tested")


def start_crash():
    state = json.loads(STATE.read_text())
    primary = worker(state["worker_a"])
    adapter_id = create(state["prefix"], "crash", primary["id"], seconds=60)
    state["crash_execution"] = run(adapter_id)
    wait(state["crash_execution"], running=True)
    # Wait for the actual payload, not just Control's start admission.
    deadline = time.monotonic() + 30
    while (
        "ISSUE144_RUNNING"
        not in request("GET", f"/executions/{state['crash_execution']}")["stdout"]
    ):
        assert time.monotonic() < deadline
        time.sleep(0.2)
    save_state(state)
    print("issue144-crash=RUNNING payload-log-observed=true")


def after_crash():
    state = json.loads(STATE.read_text())
    worker(state["worker_a"])
    request("POST", f"/executions/{state['crash_execution']}/cancel")
    assert wait(state["crash_execution"], timeout=90)["status"] == "cancelled"
    deadline = time.monotonic() + 120
    while (
        request("GET", f"/executions/{state['crash_execution']}")[
            "workspace_cleanup_status"
        ]
        != "completed"
    ):
        assert time.monotonic() < deadline, (
            "startup cleanup receipt did not retry after lease expiry"
        )
        time.sleep(0.5)
    after_restart()
    print("issue144-crash=PASS production-recovery-and-new-execution=true")


if __name__ == "__main__":
    {
        "basic": basic,
        "after-restart": after_restart,
        "start-pair": start_pair,
        "peer-survives": peer_survives,
        "start-crash": start_crash,
        "after-crash": after_crash,
    }[os.environ["ISSUE144_PHASE"]]()
