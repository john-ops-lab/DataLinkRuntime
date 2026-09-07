"""Run in Control container: exercise real RabbitMQ + isolated Worker execution."""

import json
import os
import time
import urllib.request
import uuid

opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def api(path, data=None, method=None):
    request = urllib.request.Request(
        "http://127.0.0.1:8000/api" + path,
        data=json.dumps(data).encode() if data is not None else None,
        headers={
            "Authorization": "Bearer " + os.environ["DLR_ADMIN_TOKEN"],
            "Content-Type": "application/json",
        },
        method=method,
    )
    with opener.open(request, timeout=15) as response:
        return json.load(response) if response.status != 204 else None


assert api("/health")["database"] is True
workers = api("/workers")
workers = workers if isinstance(workers, list) else workers["items"]
worker = next(w for w in workers if w.get("status") == "online")
nonce = uuid.uuid4().hex
adapter = api(
    "/adapters",
    {
        "name": "__preview_probe_" + nonce,
        "description": "Local preview deployment verification",
        "language": "python",
        "adapter_type": "task",
        "timeout_seconds": 60,
    },
)
adapter_id = adapter["id"]
api(
    f"/adapters/{adapter_id}/versions",
    {
        "code": 'def handle(context, input):\n    return {"preview_probe": input["nonce"]}\n',
        "requirements": "",
        "runtime_config": {},
    },
)
api(f"/adapters/{adapter_id}", {"runtime_worker_id": worker["id"]}, "PATCH")
execution = api(f"/adapters/{adapter_id}/executions", {"input": {"nonce": nonce}})
for _ in range(90):
    execution = api("/executions/" + str(execution["id"]))
    if execution["status"] in {"dead_letter", "expired", "cancelled"}:
        raise RuntimeError("Preview probe failed: " + str(execution["id"]))
    if (
        execution["status"] == "succeeded"
        and execution["workspace_cleanup_status"] == "completed"
    ):
        break
    time.sleep(2)
else:
    raise RuntimeError("Preview probe timed out: " + str(execution["id"]))
assert execution["output"] == {"preview_probe": nonce}, execution["id"]
api(f"/adapters/{adapter_id}", method="DELETE")
print(
    json.dumps(
        {
            "execution_id": execution["id"],
            "status": execution["status"],
            "workspace_cleanup_status": execution["workspace_cleanup_status"],
        }
    )
)
