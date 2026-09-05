## Context

See `proposal.md` for motivation. The authorized implementation baseline is `main` / `origin/main` at `d28daabfe9e70a5d5db23fb62613c5c39222764b`; Issue #130 was merged by PR #133 before this change was created. The current Control service persists user Adapters and immutable Revisions, while the Web uses React 19, Ant Design 5.29.3 and a small History API wrapper instead of a router package.

Four existing constraints shape the design:

1. `create_adapter` adds a demo Credential binding when present, and `clone_adapter` copies Worker, Credential, Schedule/Input and Webhook state. Neither semantic is safe for a template Recipe.
2. Ordinary first-Revision save currently requires a selected Worker. A template copy must already contain Revision 1 while deliberately having no Worker, so it needs one narrow system-transaction exception rather than weakening normal Save.
3. The Python, JavaScript and Java Runtime Contexts do not expose a stable logical Execution id across Attempts. Whole-Execution retry therefore cannot safely use an Attempt-local random scan id.
4. Template code and metadata are non-Python package data. A source checkout can pass while a wheel or image silently omits those files unless packaging is tested explicitly.

The two user-approved concept images are the visual source of truth for the top-level navigation, full-width Gallery, card density, Logo tiles and the sister Adapter page. They are design references only and will not be committed as product screenshots.

## Goals / Non-Goals

**Goals:**

- Keep Theme, Scenario and Variant as immutable, reviewable Recipe assets while persisting only user-instantiated Adapters.
- Guarantee exact 5/17/51 inventory, lazy language code loading, version-consistent copying and truthful per-language maturity.
- Create a stopped Adapter and Revision 1 atomically without any inherited Secret, Worker, schedule, file, history or runtime binding.
- Make `preview` bounded and make `sync` idempotent under the current #130 at-least-once / whole-Execution retry behavior.
- Match the approved Gallery and Adapter layouts, preserve existing workbench drafts during browsing, and enter the new Adapter editor immediately after copying.
- Ship attractive offline Logo tiles without importing vendor trademark artwork or a new icon dependency.

**Non-Goals:**

- No editable template tables, template administration CRUD, hidden template Adapters or background template updater.
- No general Pipeline/Sink platform, Runtime network sandbox, execution exactly-once claim or automatic Dependency/Worker provisioning.
- No extension of the three Runtime Context contracts in this change; stable sync identity comes from immutable execution input.
- No vendor Logo redistribution. An officially licensed asset can replace a Logo tile later behind the same `logo_key` map.
- No claim that all 51 Variants are live-verified; release labels follow actual evidence only.

## Decisions

### 1. Store the catalog as validated package resources, not database rows

Add a Python package under `backend/src/dlr/control/template_catalog/` with this logical layout:

```text
template_catalog/
  __init__.py
  catalog.json
  provenance.json
  schemas/
    asset-snapshot-v1.schema.json
    cmdb-upsert-v1.schema.json
  scenarios/<scenario-slug>/metadata.json
  variants/<scenario-slug>/python.py
  variants/<scenario-slug>/javascript.mjs
  variants/<scenario-slug>/java.java
```

JSON avoids a new YAML parser. `importlib.resources` makes source, wheel and container loading use the same path. The catalog holds only summary/filter fields and explicit relative resource identifiers; a prevalidated `(slug, language)` map resolves files. Request values are never concatenated into filesystem paths.

At loader construction, immutable Pydantic models validate exact counts, slug and enum uniqueness, Theme references, three languages per Scenario, shared Scenario version, Logo allowlist, provenance links, content SHA-256 and required files. Invalid assets fail startup/build checks as a unit. Variant code files are not read while serving the Scenario list; they are read only for the selected Variant and cached by `(slug, version, language)`.

Hatchling normally includes resources inside a selected Python package, but this is not assumed. A wheel test builds the wheel, inspects all expected paths, installs it in an isolated environment and reads all 51 Variants through `importlib.resources`. The Control image test repeats a representative lookup.

Alternatives considered:

- Database Theme/Scenario/Variant tables: rejected because the first release is platform-maintained and immutable; tables create migration, admin, cache and partial-update semantics with no user value.
- One giant JSON containing all code: rejected because every list request or loader parse would eagerly read 51 source files and make reviews harder.
- Remote registry: rejected because self-hosted deployments must be deterministic and offline.

### 2. Use one authenticated `/api/templates` namespace with summary/detail separation

The API is:

```text
GET  /api/templates/themes
GET  /api/templates/scenarios
GET  /api/templates/scenarios/{scenario_slug}
GET  /api/templates/scenarios/{scenario_slug}/variants/{language}
POST /api/templates/scenarios/{scenario_slug}/variants/{language}/instantiate
```

The existing `require_business_principal` dependency protects the router, and existing account-session CSRF middleware protects POST. List query values use typed enums and `page_size <= 48`. Service code applies all filters before stable sort and slice. Bilingual fields are structured as `zh-CN` and `en` values so changing the UI locale does not require duplicating the catalog in Web bundles.

The list schema deliberately has no code, requirements, install notes or runtime config. Scenario detail has three Variant summaries but still no code. Only the Variant endpoint returns the selected source and full language-specific metadata.

The instantiate request body is `name`, optional `description`, and `expected_template_version`; language and all Recipe content come from the URL and server catalog. `expected_template_version` prevents a rolling deployment from copying new code after the user reviewed an old response. Success is 201 with `AdapterResponse` and `Location: /api/adapters/{id}`.

Alternative considered: accept language and code in one generic POST. Rejected because it lets a stale or malicious client replace server-reviewed Recipe facts and weakens provenance.

### 3. Implement a dedicated atomic template-instantiation service

The service does not call `create_adapter`, `clone_adapter`, or the public Save service. In one SQLAlchemy transaction it:

1. validates the current Variant and expected version and applies the existing Adapter name normalization;
2. inserts Adapter with the server-side language/type, `run_mode=manual`, platform default timeout, `runtime_worker_id=NULL`, and source fields;
3. inserts Slot 0;
4. inserts the minimal type configuration: an empty/non-sensitive Task InputConfig, or a new disabled Webhook with a fresh public id and no credential;
5. inserts Revision 1 with the exact code, requirements and reviewed non-secret runtime config from the Variant;
6. updates `latest_version_id`, commits once, refreshes, and returns the Adapter.

No helper in this path may add demo bindings. The transaction never inserts Credential/Binding, installed Dependency, Managed File/Artifact/Lease, Worker assignment, Schedule, additional ACL, Execution, Admission, Outbox, Attempt or history.

The database unique index remains the concurrency authority. Only an IntegrityError proven to be `uq_adapters_active_name` maps to `adapter_name_conflict`; other constraints are not masked. A barrier-based concurrent test asserts one complete success and one 409 without sleeps.

For account sessions, any principal with a real `user_id` (user or account admin) owns its copied Adapter, matching the explicit “current user” requirement. A deployment superadmin without an account row continues to create system-owned objects. This choice is local to template instantiation and does not change ordinary Create/Clone ownership behavior.

Alternative considered: create an empty Adapter and call Save. Rejected because it could leave a partial object, triggers the normal Worker rule, and cannot meet one-transaction semantics.

### 4. Add nullable, paired provenance columns only

Migration `0032_issue132_template_provenance` adds:

```text
adapters.template_scenario_slug VARCHAR(128) NULL
adapters.template_version       VARCHAR(64)  NULL
```

A check constraint requires both columns to be null or both non-null. There is no foreign key to a static catalog and no backfill. The fields appear read-only in `AdapterResponse`; ordinary Create, Update, Clone and Version schemas do not accept them. A catalog update or removal therefore cannot break an existing Adapter.

Alternative considered: store full provenance JSON on every Adapter. Rejected because Revision 1 already freezes the executable content and two stable facts are sufficient for audit/navigation without duplicating the catalog.

### 5. Require a stable input scan id instead of changing Runtime Context

For the 7 bulk cloud/CMDB Scenarios, `mode=sync` requires two non-secret values in immutable execution input:

- `source_scope`: stable logical source, derived/configured from provider plus account/tenant and selected scopes;
- `scan_id`: caller-generated unique identifier for one logical scan (UUID recommended).

All Attempts of one Execution already reuse its immutable input, so a #130 retry reuses both values. The Recipe must reject missing/invalid values before the first write and must never generate a random scan id inside an Attempt. A new manual scan requires a new `scan_id`; replay creates a new business scan only when the caller supplies a new id.

This is less convenient than `context.execution_id`, but it avoids broadening three language Contexts and Worker protocols in a template feature. UI documentation provides a copyable UUID example and explains retry reuse. A later Runtime change may offer a stable id as a default without changing the target contract.

Alternative considered: expose logical Execution id in all three Runtime Contexts. Rejected for this PR because it changes Worker contracts, payloads, Java Runtime API and #130 evidence outside the minimum Recipe scope.

### 6. Freeze a narrow external CMDB Upsert v1 contract

The cloud/CMDB `sync` code targets a documented logical HTTP contract named `dlr-cmdb-upsert/v1`; it is not a new DLR Control endpoint. The target base URL and bearer credential are user-supplied through non-secret config and Credential Binding respectively. Default paths are configurable only as a reviewed base plus fixed suffixes:

```text
POST /api/v1/import-scans:begin
POST /api/v1/import-scans/{scan_id}/assets:upsert
POST /api/v1/import-scans/{scan_id}/relationships:upsert
POST /api/v1/import-scans/{scan_id}:finish
```

Every request carries `schema_version`, `source_scope`, `scan_id`, a deterministic `Idempotency-Key`, and for upserts a deterministic `batch_id`. Batch identity is derived from phase, provider/product, region/scope and zero-based page number; the target stores a payload digest and returns conflict if a repeated id has different content. Assets key on `(source_scope, external_key)` and relationships on the stable triple `(from, type, to)` within source scope. Repeated begin, equal batches and finish return the prior success.

Only after every source page and every acknowledged asset/relationship batch succeeds may the Recipe call finish. Finish is the only operation permitted to mark previously seen objects absent from the complete scan as stale. Any changed-payload conflict, region/product failure or unacknowledged batch produces `partial=true`, skips finish and returns a bounded summary.

The JSON Schema assets freeze envelope fields, external-key escaping/case/missing-region rules, allowed relation types, deterministic sort/deduplication and summary bounds. Fixtures run a local target implementing idempotency, duplicate and conflict behavior, then execute the catalog source itself twice with the same scan id.

Alternative considered: label the sequence illustrative and let each Recipe invent endpoints. Rejected because `fixture-verified sync` would otherwise be untestable and behavior would diverge across languages.

### 7. Keep Variant files standalone and test the shipped source itself

Each copied Variant must run as an ordinary DLR Adapter without importing a private template package. Shared behavior is expressed by generated metadata and review rules, but each source file contains or imports through declared requirements everything it needs. Tests load and execute/compile the catalog file; they do not test a second “equivalent” helper implementation.

Requirements use the platform's existing formats:

- Python: pinned requirements.txt lines;
- JavaScript: pinned `package@version` lines accepted by the Worker parser;
- Java: pinned `groupId:artifactId:version` coordinates accepted by the Worker parser.

The first maturity pass is evidence-driven: heavy cloud SDK Variants remain `reference-generated` unless their exact dependencies are installed/resolved and syntax/compile checks run. Local REST/ServiceNow/file/database/storage fixtures may become `fixture-verified` only after the source itself runs through the corresponding language path. JavaScript Excel must prove both `.xlsx` and `.xls`; choosing a library requires maintenance, security and license review before pinning.

### 8. Pin provenance and separate code license from behavior research

`provenance.json` and the human-readable coverage matrix record scenario/resource, repository, exact SHA/tag, exact path/API, license evidence, use mode, pagination/scope, supplemental calls, external key, relationship evidence, official SDK package/version, fixture and `checked_at`.

The research baseline frozen on 2026-09-05 is:

| Source | Frozen revision | License treatment |
|---|---|---|
| `open-c3/open-c3` | `039b9a42fdc80f31520ec0918000b8c7a05162e5` | GPL-2.0; behavior research only |
| `1Panel-dev/CloudExplorer` | `aede557444bcf9d8daa49f5bb13e19cfaa43ce5f` | GPL-3.0; behavior research only |
| `turbot/steampipe-plugin-alicloud` | `d619a9d57505ae99aa5329aad3f4802ee94fde56` | Apache-2.0; clean adaptation allowed with attribution/change notice |
| `TencentCloud/steampipe-plugin-tencentcloud` | `d3a0a66fc6f67dd6ce805417efe3bc83a80bb587` | LICENSE text Apache-2.0 while API SPDX is NOASSERTION; record discrepancy and retain notice |
| `dlt-hub/dlt` | `3efddd61b9f85592bc71879ad0ede8a82d2de3d6` | Apache-2.0; clean adaptation allowed with attribution |
| `airbytehq/airbyte` | `6f59bc9217670d69e4904adb4910b870e3eaf67c` | ELv2 root license; behavior research only unless a specific connector proves otherwise |
| `yvain13/ServiceNow-CMDB-MCP` | `9f0fe8fd4792c6ee0e78fa3c74f40ddbe72feb61` | no repository LICENSE found; behavior research only |

`THIRD_PARTY_NOTICES.md` (or the repository's equivalent) records any compatible direct adaptation. GPL/ELv2/unlicensed sources contribute only functional facts; names, structure, comments, fixtures and code are independently written. The coverage matrix, not a product-name guess, controls which resource types and relationships are claimed.

### 9. Preserve the current shell and add lightweight Gallery routes

No routing dependency is added. The existing History API utilities are extracted to a small route module that parses:

```text
/ and /adapters              -> adapter surface
/templates                   -> gallery list
/templates/{scenario_slug}   -> scenario detail
/settings/{category}         -> existing settings flow
```

`ApplicationShell` receives `activeSection` and a primary-navigation callback and renders semantic page links after the product name. The Adapter surface stays mounted but receives `hidden` while Gallery is active, preserving Monaco draft, TaskRunSettings state and STAGED input. Returning triggers editor layout. Portal surfaces such as Drawer/Modal close during page transition, and busy management operations disable conflicting navigation.

Browsing Gallery alone does not discard Adapter draft state. Before instantiate, the app calls the existing comprehensive `confirmWorkspaceLeave`; cancel means no POST. Success refreshes Adapter Catalog, loads the returned id and Revision 1 through the existing content path, changes URL to `/adapters`, activates Edit and focuses the editor. If post-commit Revision load fails, the new Adapter remains selected and the real load error is shown.

The top-level beforeunload guard is extended to include code/requirements dirty state in addition to the existing STAGED-file guard.

Alternative considered: unmount Workbench on `/templates`. Rejected because child-owned run-setting and STAGED-file state would be silently lost.

### 10. Build original Logo tiles from existing icons and controlled palettes

The catalog exposes one of 17 allowlisted keys:

```text
alicloud-compute, alicloud-network, alicloud-data,
tencentcloud-compute, tencentcloud-network, tencentcloud-data,
servicenow-cmdb, rest-request, rest-pagination, webhook-normalize,
file-csv, file-excel, data-json, database-postgresql, database-mysql,
storage-s3, transfer-sftp
```

`TemplateScenarioLogo` maps each key to an existing `@ant-design/icons` category glyph, a controlled color/gradient, shape and small type marker. This gives the approved concept's strong visual identity without copying Alibaba Cloud, Tencent, ServiceNow, PostgreSQL, MySQL, AWS or other trademark artwork. There is no remote request and no new dependency. Unknown keys defensively fall back to DLR blue Code, while startup validation prevents shipped unknowns. Logo graphics are decorative; card title, vendor and type remain text.

Alternative considered: Simple Icons or vendor media kits. Rejected because a software/CC0 repository license does not grant trademark redistribution rights, and ServiceNow/Tencent usage terms require separate permission or limit use.

### 11. Make Gallery request state deterministic and accessible

The Gallery owns Theme, filters, debounced query and `pageByTheme`. Filter changes reset only the active Theme to page 1. `AbortController` plus a monotonically increasing request generation prevents stale responses from replacing current results. Variant responses are session-cached by `(slug, version, language)`.

Ant Design Tabs, Select, Input, Pagination, Modal, Tag, Skeleton and Empty use the repository-pinned 5.29.3 APIs. Card navigation uses semantic links; current primary navigation uses `aria-current`. Maturity has text, not color alone. Modal title, initial focus, validation association, focus return and keyboard controls are explicit. Layout breakpoints follow the specs, and all dynamic fields plus the new `template` i18n namespace are bilingual without `dangerouslySetInnerHTML`.

### 12. Layer verification by risk and keep maturity machine-checkable

Verification is split into:

1. catalog/schema/package tests: exact 5/17/51, hashes, source files, Logo keys, no secret/path/endpoint findings, wheel/image inclusion;
2. API/service/database tests: auth/CSRF, filters/sort/pagination/no-code responses, version conflict, transaction rollback, exact Revision, no bindings, owner behavior and barrier-based name race;
3. Variant gates: requirements parser, Python compile/import, `node --check`, Java compile against current Runtime API, then source-executing fixtures for any claimed level;
4. source/license audit: pinned coverage matrix, NOTICE, relationship evidence and maturity receipts;
5. Web Vitest: routes, response races, lazy cache, per-language maturity, local Logo map, dirty preservation, duplicate-submit/409 and automatic editor handoff;
6. browser verification: direct routes, filters/pagination, language switching, copy/conflict, Managed Input disabled, keyboard/focus, console/network errors, 1680/1280/900/560 layouts and comparison to both approved concepts;
7. regressions: ordinary Create/Clone/Save, migrations from 0031 and fresh head, full relevant Backend/Web checks and proportional Compose smoke.

Timeout/backoff tests use injected clocks and event barriers, never sleeps. Maturity validation fails when the receipt required by a label is missing; it never automatically upgrades sibling languages.

## Risks / Trade-offs

- [51 standalone Variants create a large review surface] → Generate no behavior at runtime; use shared schemas, deterministic inventories, per-language gates and coverage/maturity matrices so omissions are machine-detectable.
- [Caller-supplied scan id is less convenient] → Provide clear input schema/example and reject before writes; retain current Runtime boundary and reliable retry identity. A later Runtime default can be backward compatible.
- [A target may not implement `dlr-cmdb-upsert/v1`] → Mark sync prerequisites explicitly, keep preview independently usable and do not label sync fixture-verified until the local target contract passes.
- [Provider SDK/API coverage can drift] → Pin SDKs and source revisions, show `checked_at`, keep unsupported gaps explicit and require a versioned catalog update for changes.
- [Static assets work in checkout but disappear from distribution] → Build/install wheel and inspect Control image as release gates.
- [A template version changes during rolling deploy] → Require `expected_template_version` and return 409 for refresh instead of silently copying different code.
- [Original color/glyph tiles may be mistaken for official marks] → Use neutral category geometry plus textual vendor labels, document them as DLR artwork and avoid vendor logos.
- [Keeping Workbench mounted consumes memory] → Only one existing Workbench remains mounted; Gallery caches bounded metadata/selected Variants and clears portal surfaces.
- [Automated syntax checks may be mistaken for usability] → Render exact per-language maturity and require source-executing fixtures or live receipts for higher labels.

## Migration Plan

1. Build and validate catalog, schemas, source matrix and wheel contents before enabling routes.
2. Apply additive migration 0032. Existing rows receive null/null provenance and remain readable by old/new code.
3. Deploy Control with template endpoints and static assets, then Web with the new navigation. New Web requires new endpoints, so Control is deployed first in rolling environments.
4. Run authenticated API smoke and copy one Task plus one Webhook Variant; verify Revision 1 and absence of all prohibited bindings.
5. Rollback prefers reverting Web/Control while leaving the additive columns. Already-instantiated Adapters remain ordinary usable Adapters.
6. Only an explicit database downgrade removes the two optional audit columns; it loses provenance labels but never Revision code. No catalog rollback deletes user Adapters.

There are no unresolved questions that can safely change the specs or task breakdown; the API, sync identity, target contract, ownership and Logo licensing strategy are frozen here.
