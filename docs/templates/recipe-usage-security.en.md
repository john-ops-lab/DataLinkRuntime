# Template Recipe usage and security boundaries

## Catalog and copy isolation

The first catalog release is shipped as repository-owned static assets with the DLR Control wheel and container: 5 themes, 17 scenarios, and exactly 3 variants per scenario (Python, JavaScript, and Java), for 51 variants. List and detail requests read metadata only; source is loaded only for the selected language. The catalog does not download or silently update recipes after startup.

Merely browsing a Template creates no Adapter, Slot, Execution, Outbox, Attempt, or Worker state. After the user selects a language, names the copy, and confirms, one transaction creates only the new Adapter, its required Slot 0, a safe empty Task input configuration or a new disabled Webhook configuration, Revision 1, and read-only provenance fields. The Web application then refreshes the Adapter list, loads Revision 1, and enters that Adapter's editor.

The copy is independent and stopped by default. It inherits no credential, credential binding, installed dependency, worker assignment, schedule, enabled webhook, managed file, artifact, lease, ACL share, execution, or history. Future catalog changes never rewrite copied Adapters.

Disabling Managed Input Store does not block browsing, viewing source, or copying any Template, including CSV and Excel. Excel and any other Variant that requires a file cannot run until the deployment supplies that execution-input file through a supported runtime path; CSV can still run from direct `content`. Copying never fabricates a file binding.

## Before running a copy

- Review the exact language source, pinned requirements, install notes, maturity label, and provenance.
- Select a compatible, appropriately isolated Worker and install dependencies through the platform dependency flow.
- Supply every declared secret only through Credential Binding.
- Start with a non-production, tightly scoped preview or pure transformation and inspect limits, permissions, side effects, and retry behavior.
- For sync recipes, confirm the target implements `dlr-cmdb-upsert/v1`, choose stable `scan_id` and `source_scope` values, and reuse them for retries of the same logical scan.

Preview performs no CMDB target writes. Sync uses deterministic batch identities and idempotency keys. Any source or target batch failure returns a partial result and must not call finish, so stale cleanup is never triggered by an incomplete scan.

## Credentials, redirects, and output limits

Real secrets come only from `context.secrets.get(key)`. Recipe source, input skeletons, runtime configuration, examples, ordinary logs, and outputs must not contain passwords, tokens, private keys, credential-bearing URLs, or machine-local paths. REST API keys support either the reserved `DLR-Auth` header binding or `query_auth.parameter` plus `query_auth.secret_binding`; the query secret is injected only at request time and never returned.

Direct URL parameters, plain `query`, pagination parameter names, and plain headers reject credential-like names fail closed. After lowercasing and removing non-alphanumeric characters, a name is credential-like when it contains `accesskey`, `apikey`, `authorization`, `authentication`, `clientsecret`, `cookie`, `credential`, `password`, `privatekey`, `secret`, `signature`, or `token`, or ends in `auth` or `sig`. Such credentials must use `query_auth` or `DLR-Auth`. Ordinary business names such as `author`, `design`, `page`, `filter`, and `X-Trace-ID` remain valid.

Credential-bearing HTTP requests do not follow redirects implicitly. Same-origin redirect behavior is explicit where supported, and cross-origin pagination strips every credential-derived header even when its name would otherwise be allowed. Limits apply across the whole logical operation rather than resetting for every page or retry.

REST single-request output is scrubbed before its normalized response is checked against `max_response_bytes`. A secret shorter than 10 UTF-8 bytes uses an equal-or-shorter asterisk marker; longer secrets use `<redacted>`, so scrubbing cannot amplify a short secret merely because of the marker.

REST pagination commits records only as complete source pages. `pages` counts successful HTTP responses only; a byte- or deadline-preflight stop before the next request does not inflate it. If a page exceeds the remaining `max_records` budget, or if adding the complete scrubbed page would exceed the `max_bytes` records-output budget, none of that page enters the output. A page or offset checkpoint identifies that current uncommitted boundary with `start_page` or `start_offset` and can be overlaid on the original input for direct resume. If one source page itself exceeds the cap, reduce `page_size` or raise the cap before resuming. Cursor and next-URL continuations are conservatively treated as opaque and potentially credential-bearing: partial output uses `checkpoint: null`, emits neither the original value nor a non-resumable redacted placeholder, and makes no direct-resume claim.

`max_bytes` bounds the normalized result or Adapter output described by each contract. It does not automatically prove that every third-party SDK also bounds its raw transport response. In particular, the Alibaba Cloud `callApi` transport used by the three Alibaba scenarios has no auditable response-byte cap in the published Recipe; those variants remain `reference-generated` until a bounded transport and matching fixtures satisfy the maturity gate. Tencent Cloud's built-in JavaScript transport and ServiceNow JavaScript reads use streaming max-plus-one cancellation canaries, but those narrow checks alone do not upgrade maturity.

All three ServiceNow variants require `max_bytes >= 1024`. That value independently bounds both cumulative raw Table API response bytes across the scan and the fully serialized preview or sync envelope, including failures and checkpoints. `instance_id`, `scan_id`, and `source_scope` are each limited to 128 characters, so identifiers and normalized assets cannot bypass the output budget. If an invalid record, `max_records`, or the normalized-output budget stops processing within a page, `checkpoint.offset` identifies the first unprocessed source row instead of advancing over the rest of that page. These partial paths perform no target begin, upsert, or finish operation, and sync never finishes an incomplete scan.

## Storage and transfer

The S3-compatible variants require `max_total_bytes >= 256`. The limit bounds the complete compact JSON UTF-8 result, not just downloaded raw bytes: listing metadata, status entries, summary, checkpoint and base64 expansion all count. An object's metadata and optional content are committed atomically. A mid-page checkpoint retains the request page's `continuation_token` plus the zero-based `object_offset` of the first unprocessed object, so resume does not skip the rest of that page.

The SFTP variants use the same minimum and complete-envelope budget for paths, listing metadata, status, summary, checkpoint and base64 content. Each file is committed atomically. `checkpoint.start_at` is an opaque relative path for the first unprocessed item in server enumeration order. Resume must find that exact path before emitting more entries and fails closed if it disappeared; it never assumes that a locally bounded listing slice represents global lexical order. Host-key pinning, server `realpath`, and remote-root confinement remain mandatory.

## Excel XLSX and XLS

Excel accepts `.xlsx` and legacy `.xls` through different safety boundaries:

- XLSX packages are inspected before workbook parsing. Encryption, VBA or macro-enabled parts, embedded or ActiveX content, external relationships, unsafe archive expansion, and file/output limit violations fail closed. Formula cells are never evaluated and are emitted as null when detected.
- Legacy OLE XLS uses offline, data-only parsing paths. No variant creates a formula evaluator, macro runtime, or external-link fetch. Complete OLE active-content preflight is not available, so the contract promises non-execution rather than claiming every active part was detected and rejected. XLS remains experimental.

A narrow real XLSX parity fixture has run through all three pinned libraries. A real legacy XLS fixture and the complete XLSX/XLS maturity gate remain open, so these checks and malicious-package canaries are development evidence only, not a maturity Receipt.

## Maturity and receipts

Maturity belongs to one exact `scenario_slug + version + language + source_sha256`. All initial 51 variants are `reference-generated`, meaning there is no evidence/Receipt matching the current source hash that satisfies every gate for the next maturity level. Narrow smoke tests or security canaries may have run, but they do not by themselves establish `syntax-verified`, `fixture-verified`, or `live-verified` status.

See the [source and resource coverage matrix (Simplified Chinese)](source-coverage-matrix.md), [`dlr-cmdb-upsert/v1` contract (Simplified Chinese)](cmdb-upsert-v1.md), and [maturity and Receipt rules (Simplified Chinese)](maturity-receipts.md) for the detailed audit records.
