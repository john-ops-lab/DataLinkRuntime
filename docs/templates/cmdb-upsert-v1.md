# dlr-cmdb-upsert/v1 合同

`dlr-cmdb-upsert/v1` 是云与 CMDB Recipe 面向外部目标系统的窄逻辑 HTTP 合同，不是 DLR Control 新增的 API。目标地址来自 Revision 的非敏感 `runtime_config.cmdb_base_url`，例如 `https://cmdb.example`；Bearer 值只能从 `context.secrets.get("CMDB_TOKEN")` 获得。

机器可读请求 Schema 位于 `backend/src/dlr/control/template_catalog/schemas/cmdb-upsert-v1.schema.json`。预览对象 Schema 位于相邻的 `asset-snapshot-v1.schema.json`。

## 固定调用顺序

```text
POST /api/v1/import-scans:begin
POST /api/v1/import-scans/{scan_id}/assets:upsert
POST /api/v1/import-scans/{scan_id}/relationships:upsert
POST /api/v1/import-scans/{scan_id}:finish
```

Recipe 必须先完整验证 `mode`、`source_scope`、`scan_id`、目标配置与 Credential Binding，再发起任何目标写入。

1. `begin_scan` 可安全重放。
2. 每个来源页产生零个或多个确定性资产、关系批次；空批次不发送。
3. 所有来源范围和全部批次均获确认后，才允许 `finish_scan`。
4. 任一来源、分页、序列化、网络、目标冲突或批次确认失败，都返回 `partial=true` 并跳过 finish。
5. 只有 finish 可使目标根据一次完整扫描把旧对象标为失效。

## 稳定扫描身份

`scan_id` 与 `source_scope` 由调用者放入不可变 Execution Input。同一逻辑 Execution 的所有 Attempt 必须复用二者；Recipe 不得在 Attempt 内随机生成扫描标识。

推荐：

- `scan_id`：调用者生成的 UUID 或同等稳定、非敏感标识；
- `source_scope`：`provider:account-or-tenant:selected-scope`；
- 新的业务扫描使用新的 `scan_id`；
- 重放同一业务扫描继续使用原 `scan_id`。

## Headers

每个请求发送：

```text
Content-Type: application/json
Authorization: Bearer value-from-CMDB_TOKEN
Idempotency-Key: deterministic-key
```

JSON 中的 `idempotency_key` 必须与 Header 相同，便于不保留 Header 的审计实现验证。响应、Adapter Output 和普通日志不得包含 Authorization 值、目标 URL 的认证 Query 或原始第三方错误体。

## 请求示例

### begin_scan

```json
{
  "schema_version": "dlr-cmdb-upsert/v1",
  "operation": "begin_scan",
  "source_scope": "alicloud:EXAMPLE_ACCOUNT:example-region-1",
  "scan_id": "EXAMPLE_SCAN_ID",
  "idempotency_key": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "provider": "alicloud",
  "catalog_version": "1.0.0"
}
```

### upsert_assets

```json
{
  "schema_version": "dlr-cmdb-upsert/v1",
  "operation": "upsert_assets",
  "source_scope": "alicloud:EXAMPLE_ACCOUNT:example-region-1",
  "scan_id": "EXAMPLE_SCAN_ID",
  "idempotency_key": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "batch_id": "assets:alicloud:alicloud:EXAMPLE_ACCOUNT:example-region-1:000000",
  "batch_index": 0,
  "assets": [
    {
      "external_key": "alicloud:EXAMPLE_ACCOUNT:example-region-1:ecs_instance:i-example",
      "class": "ecs_instance",
      "provider_type": "DescribeInstances",
      "name": "example-instance",
      "account": "EXAMPLE_ACCOUNT",
      "region": "example-region-1",
      "zone": null,
      "status": "running",
      "tags": {},
      "attributes": {}
    }
  ]
}
```

### upsert_relationships

```json
{
  "schema_version": "dlr-cmdb-upsert/v1",
  "operation": "upsert_relationships",
  "source_scope": "alicloud:EXAMPLE_ACCOUNT:example-region-1",
  "scan_id": "EXAMPLE_SCAN_ID",
  "idempotency_key": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
  "batch_id": "relationships:alicloud:alicloud:EXAMPLE_ACCOUNT:example-region-1:000000",
  "batch_index": 0,
  "relationships": [
    {
      "from": "alicloud:EXAMPLE_ACCOUNT:example-region-1:ecs_instance:i-example",
      "type": "located_in",
      "to": "alicloud:EXAMPLE_ACCOUNT:example-region-1:zone:example-zone"
    }
  ]
}
```

### finish_scan

```json
{
  "schema_version": "dlr-cmdb-upsert/v1",
  "operation": "finish_scan",
  "source_scope": "alicloud:EXAMPLE_ACCOUNT:example-region-1",
  "scan_id": "EXAMPLE_SCAN_ID",
  "idempotency_key": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
  "complete": true,
  "summary": {
    "assets": 1,
    "relationships": 1,
    "pages": 1,
    "failures": []
  }
}
```

## 外部键与关系

资产键固定为：

```text
provider:account:region-or-global:type:source-id
```

规则：

- provider 与 type 使用目录声明的小写稳定标识；
- account、region 和 source-id 使用来源返回的稳定标识，不把显示名称当作 id；
- 缺失 region 使用稳定字面量 `global`；
- 对组件中的 `%` 先编码为 `%25`，再把 `:` 编码为 `%3A`；其余大小写不擅自改变；
- 资产按 `external_key` 升序排序并去重；
- 相同键与相同 canonical JSON 可折叠；相同键但 payload 不同必须在发送前报冲突；
- 关系只允许 `located_in`、`attached_to`、`protected_by`、`member_of`、`serves`、`routes_to`；
- 关系稳定键是同一 source_scope 内的 `from + type + to`，按该三元组排序并去重；
- 不根据名称、标签或产品常识猜测未观测关系。

## 批次和幂等

`batch_id` 由 `phase、provider、source_scope、zero-based-batch-index` 的规范化值确定，不包含 Attempt id、时间戳或随机数。固定 canonical 形式：

```text
phase:provider:source-scope:batch-index
```

目标必须保存每个 `source_scope + scan_id + idempotency_key` 对应的 canonical payload digest：

- 首次请求：原子应用并保存结果与 digest；
- 相同 key、相同 digest：返回之前的成功结果，不重复写业务对象；
- 相同 key、不同 digest：返回 409 冲突，不覆盖旧 payload；
- 重复 begin 或 finish 使用相同规则；
- 资产键为 `source_scope + external_key`；
- 关系键为 `source_scope + from + type + to`。

网络不确定时 Recipe 仅重试协议允许安全重放的相同 payload 和相同 Idempotency-Key，并保持有限次数与总时限。它不得用新 key 掩盖不确定结果。

## 有界结果和失败

每个 Recipe 必须限制 `max_pages`、`max_records`、`max_bytes`、`batch_size`、`timeout_seconds` 和失败清单长度。超限或失败时返回语法完整的摘要：

```json
{
  "mode": "sync",
  "scan_id": "EXAMPLE_SCAN_ID",
  "source_scope": "alicloud:EXAMPLE_ACCOUNT:example-region-1",
  "partial": true,
  "summary": {
    "assets": 200,
    "relationships": 80,
    "pages": 3,
    "failures": [
      {"region": "example-region-2", "resource": "ecs_instance", "error": "source_read_failed"}
    ]
  },
  "failed": [
    {"region": "example-region-2", "resource": "ecs_instance", "error": "source_read_failed"}
  ],
  "checkpoint": {
    "failed": [
      {"region": "example-region-2", "resource": "ecs_instance", "error": "source_read_failed"}
    ],
    "limit_reached": false
  }
}
```

sync Output 不返回完整 assets、relationships、Secret 或原始响应。目标未实现该合同、未配置 `CMDB_TOKEN` 或不接受幂等冲突规则时，用户仍可使用 preview，但不得把 sync 描述为已验证。
