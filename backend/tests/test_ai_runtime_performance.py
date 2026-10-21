"""Fixed local latency; compare calls/resources and optionally record lab p95."""

import json
import math
import os
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from ai_runtime_support import response, server
from dlr.common.config import settings
from dlr.control.ai.runtime.pydantic_ai.work import NETWORK_WORK, TOOL_WORK
from test_ai import assist_body, configure, create_adapter


def test_same_protocol_fixed_latency_runtime_comparison(
    api_client: Any, session_factory: Any, monkeypatch: Any
) -> None:
    rows: list[dict[str, Any]] = []
    handler_lock = threading.Lock()

    def respond(request: Any) -> Any:
        time.sleep(0.005)
        messages = request.json()["messages"]
        if any(item["role"] == "tool" for item in messages):
            return response("openai")
        return response("openai", calls=[("dlr_docs_list", "fixed-latency-call", "{}")])

    with server(responder=respond) as script:
        adapter = create_adapter(api_client, "fixed-latency-benchmark")
        configure(api_client, base_url=script.url)
        for concurrency in (1, 8):
            for runtime in ("legacy", "pydantic_ai"):
                monkeypatch.setattr(settings, "ai_assist_runtime", runtime)

                def invoke(index: int) -> float:
                    body = {
                        **assist_body(),
                        "task_intent": "consultation",
                        "message": f"Fixed local scenario {index:02}",
                    }
                    start = time.perf_counter()
                    result = api_client.post(f"/api/adapters/{adapter['id']}/ai/assist", json=body)
                    assert result.status_code == 200, result.text
                    assert result.json()["task_status"] == "answered"
                    assert len(result.json()["tool_calls"]) == 1
                    assert result.json()["tool_calls"][0]["status"] == "success"
                    return (time.perf_counter() - start) * 1000

                invoke(99)  # The cold dependency/import cost is measured separately at deployment.
                before = len(script.requests)
                with ThreadPoolExecutor(max_workers=concurrency) as pool:
                    elapsed = sorted(pool.map(invoke, range(16)))
                assert len(script.requests) - before == 32
                deadline = time.monotonic() + 2
                while (NETWORK_WORK.active or TOOL_WORK.active) and time.monotonic() < deadline:
                    time.sleep(0.005)
                assert not NETWORK_WORK.active and not TOOL_WORK.active
                assert session_factory.kw["bind"].pool.checkedout() == 0
                with handler_lock:
                    rows.append(
                        {
                            "runtime": runtime,
                            "concurrency": concurrency,
                            "samples": len(elapsed),
                            "p50_ms": round(statistics.median(elapsed), 3),
                            "p95_ms": round(elapsed[math.ceil(len(elapsed) * 0.95) - 1], 3),
                            "provider_http_requests": 32,
                            "remaining_network_work": len(NETWORK_WORK.active),
                            "remaining_tool_work": len(TOOL_WORK.active),
                            "checked_out_sessions": 0,
                        }
                    )
    for concurrency in (1, 8):
        baseline = next(
            row for row in rows if row["concurrency"] == concurrency and row["runtime"] == "legacy"
        )
        candidate = next(
            row
            for row in rows
            if row["concurrency"] == concurrency and row["runtime"] == "pydantic_ai"
        )
        candidate["p95_limit_ms"] = round(baseline["p95_ms"] * 1.2 + 100, 3)
        candidate["p95_pass"] = candidate["p95_ms"] <= candidate["p95_limit_ms"]
        if os.environ.get("DLR_AI_BENCHMARK_ENFORCE") == "1":
            assert candidate["p95_pass"], rows
    destination = os.environ.get("DLR_AI_BENCHMARK_PATH")
    if destination:
        Path(destination).write_text(
            json.dumps({"fixture": "loopback-5ms-one-docs-call-v1", "rows": rows}, indent=2) + "\n"
        )
