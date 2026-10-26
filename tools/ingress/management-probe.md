# Bounded management probe

This auxiliary probe replaces the expensive B0 diagnostic calls for the formal
capacity round. B0 fixtures and the frozen `workload.py` are unchanged. The probe
only sends authenticated `GET` requests and never changes adapters, subscriptions,
queues, business tables, or platform configuration.

Run the probe and producer inside **the same external Linux generator container**,
under their shared **512 MiB / 0.5 CPU** limits. Do not run a second generator or
resource sampler in another time namespace and subtract uncalibrated UTC times.
The root controller verifies actual container resource limits independently.

Private configuration (outside the checkout; the values below are placeholders):

```json
{
  "schema": "dlr.ingress.management-probe.v1",
  "run_id": "capacity-http-round1",
  "api_base": "https://PRIVATE_ENDPOINT",
  "admin_token": "PRIVATE_ADMIN_TOKEN",
  "subscription_id": 7,
  "count": 10000,
  "stage": "http",
  "urls": {
    "adapters": "/api/adapters",
    "events": "/api/event-subscriptions/7/events?limit=20",
    "backlog": "/api/event-subscriptions/7"
  },
  "output_path": "/private/evidence",
  "producer_log_path": "/private/evidence/capacity-http-round1-http.private.jsonl",
  "producer_result_path": "/private/evidence/capacity-http-round1-http.private.json",
  "baseline_samples": 20,
  "post_drain_seconds": 600,
  "deadline_seconds": 1800,
  "request_timeout_seconds": 5,
  "response_max_bytes": 2097152,
  "log_max_bytes": 16777216
}
```

For RabbitMQ use `stage: "publish"` and the matching `-publish.private.jsonl` and
`-publish.private.json` paths. Every round uses a fresh subscription with zero
accepted records and a new `run_id`; never reuse a warmup or previous round.
The source's queue and credential preparation belongs to the controller.

The normal HTTP producer configuration must explicitly set
`"http_concurrency": 125`. Its existing maximum of 128 is unchanged; this wrapper
rejects settings above 125 so the three management requests keep the shared
outstanding HTTP request count at or below 128.

From the generator container, start the probe as a foreground process under the
controller's process supervision:

```sh
python /source/tools/ingress/management_probe.py start --config /private/probe.json
```

In another process in that same container, wait for at least twenty successful
idle samples per endpoint and the saved zero-count poll, then start the frozen
producer through its clock witness wrapper:

```sh
python /source/tools/ingress/management_probe.py wait-ready --config /private/probe.json --timeout 60
python /source/tools/ingress/management_probe.py run-producer --config /private/probe.json --workload-config /private/producer.json --stage http
```

The wrapper runs the frozen producer once, with no replay on command failure.
It verifies the frozen file SHA and records the Linux boot ID, active/child time
namespace, monotonic start/end, return code, and producer log/result SHA. These
receipts establish a common `CLOCK_MONOTONIC` domain without changing the frozen
producer result schema. They contain no endpoint, token, producer body or config
copy. The existing first HTTP `started_ns`, or RabbitMQ `publish_started` timestamp,
is the sender's first-request boundary. It must fall after probe readiness and
inside the wrapper interval. Missing, changed or foreign-domain evidence fails.

The probe sends each of the three fixed endpoints once per second, with at most
one request per endpoint and three in total. Each request has a five-second total
deadline including streaming, no transport retries or redirects, and a maximum
2 MiB uncompressed body. Encoded bodies are rejected to keep decompression memory
bounded. A still-running request causes an explicit missed-slot record instead
of another queued request. A timeout, non-200 response, malformed response,
missed slot, exceeded evidence budget or stopped probe prevents a passing result.

JSONL samples retain scheduled/start/end monotonic timestamps, HTTP status,
elapsed time, timeout and stable error codes. Response bodies are discarded.
Only allowlisted receipt counts, quota/limit numbers, list sizes and an optional
oldest-pending Unix timestamp survive. The latter is diagnostic only; it is never
used to compare platform and generator clocks. `accepted` must equal the sum of
the seven real receipt states. `replayed` and `coalesced` are aliases and are not
added to the unique accepted count.

The first observed fully accepted poll gives an acceptance upper endpoint. Its
preceding incomplete poll's request start gives a conservative lower endpoint.
The first poll with all 10,000 succeeded and all other states zero gives the
completion upper endpoint. The report calculates:

```text
actual acceptance - actual first arrival <= accepted_poll.response_end - sender_begin
actual completion - actual acceptance <= done_poll.response_end - prior_incomplete_accept_poll.request_begin
```

These are conservative causal upper bounds, not exact platform commit timings.
The acceptance upper bound must be at most 60 seconds; the completion-after-
acceptance upper bound at most 600 seconds. Missing a prior incomplete sample or
clock provenance yields an unknown interval and cannot pass. The first sender
boundary is never reset. After completion, all three endpoints continue at 1 Hz
for at least 600 seconds and receipt states must stay completely succeeded.

Latency uses nearest-rank p95, separately for idle baseline, load/drain, and the
post-drain window. Each endpoint's load/drain and post-drain p95 must both be at
most `max(500 ms, idle p95 * 2)`. A long quiet post-drain window therefore cannot
hide an overloaded load-phase p95. Missing phases are unknown rather than zero.

The probe exits after complete post-drain observation, an external stop, or its
overall deadline (never above 1800 seconds). It writes private PID, ready,
producer clock and analysis receipts with exclusive creation; previous evidence
is never overwritten. The controller may create the run-specific
`<run_id>-management.stop` file or signal the recorded PID. An early stop is
incomplete evidence. The bounded JSONL and stable exit code retain the failure.

After the probe ends, or to recompute from unchanged evidence:

```sh
python /source/tools/ingress/management_probe.py analyze --config /private/probe.json
```

`management_and_timing_passed` is only this probe's gate. Independent business
ID/hash/value/write-count reconciliation, original raw identity checks, and actual
Control/Worker/generator resource evidence remain mandatory. The probe never
declares overall capacity acceptance. No live workload is started by its tests.
