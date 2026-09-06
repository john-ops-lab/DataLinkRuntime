import com.aliyun.teaopenapi.Client;
import com.aliyun.teaopenapi.models.Config;
import com.aliyun.teaopenapi.models.OpenApiRequest;
import com.aliyun.teaopenapi.models.Params;
import com.aliyun.teautil.models.RuntimeOptions;
import java.io.InputStream;
import java.net.URI;
import java.net.URLEncoder;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.time.Duration;
import java.time.Instant;
import java.time.ZoneOffset;
import java.time.format.DateTimeFormatter;
import java.util.ArrayList;
import java.util.Base64;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;

/** Bounded alicloud inventory Recipe with deterministic preview/sync. */
public class Adapter {
    // 阿里云数据库与基础服务：可修改的配置集中在这里。
    // 默认无需填写运行输入；先修改下面的地址、查询条件等配置，再保存运行。
    // 调试时可传入 JSON 对象覆盖同名配置；嵌套对象需要完整填写。
    // 凭据配置：先在“凭据”中创建对应值，再到此适配器的“凭据绑定”中绑定；绑定键必须与下列名称完全一致。
    // ALICLOUD_ACCESS_KEY_ID：阿里云 AccessKey ID。
    // ALICLOUD_ACCESS_KEY_SECRET：阿里云 AccessKey Secret。
    // ALICLOUD_SECURITY_TOKEN：阿里云临时凭据 Token，仅使用临时凭据时配置。
    // CMDB_TOKEN：目标 CMDB Token，仅同步时配置。
    private static final Map<String, Object> CONFIG = defaultConfig();
    @SuppressWarnings("unchecked")
    private static Map<String, Object> defaultConfig() {
        // 参数说明与下方 JSON 使用相同顺序。
        // mode: preview 只采集并返回结果；sync 会写入目标 CMDB，请先配置下方地址和 CMDB_TOKEN 凭据。
        // account: 填写云账号标识，用于区分不同账号的资产；不是密码。
        // regions: 填写需要采集的区域 ID，可配置多个区域。
        // max_pages: 单次运行最多读取的页数。
        // max_records: 单次运行最多返回的记录数。
        // max_bytes: 单次运行处理或返回的数据大小上限，单位字节。
        // page_size: 每次请求的条数，不能超过目标接口限制。
        // timeout_seconds: 单次请求超时时间，单位秒。
        // batch_size: 每批处理的记录数。
        // source_scope: 同步范围标识：同一账号、区域和资源范围保持不变。
        // scan_id: 仅 sync 使用：每次新的扫描填写新标识，同一次运行重试保持不变。
        // cmdb_base_url: 仅 sync 使用：填写目标 CMDB 地址；目标需实现下方代码调用的扫描和批量写入接口。
        return (Map<String, Object>) Json.parse("""
            {
              "mode": "preview",
              "account": "EXAMPLE_ACCOUNT",
              "regions": [
                "cn-hangzhou"
              ],
              "max_pages": 50,
              "max_records": 5000,
              "max_bytes": 8388608,
              "page_size": 100,
              "timeout_seconds": 30,
              "batch_size": 200,
              "source_scope": "alicloud:EXAMPLE_ACCOUNT:cn-hangzhou",
              "scan_id": "",
              "cmdb_base_url": "https://cmdb.example"
            }
            """);
    }

    private static final String PROVIDER = "alicloud";
    private static final String REGION_PATTERN = "[a-z0-9]+(?:-[a-z0-9]+)*";
    private static final String OPERATIONS_JSON = """
[["rds","rds","rds.aliyuncs.com","DescribeDBInstances","2014-08-15","Items.DBInstance",["DBInstanceId"],["DBInstanceDescription"],["ZoneId"],["DBInstanceStatus"],[["VpcId","vpc","located_in"],["VSwitchId","vswitch","located_in"]],["next-token","MaxResults",1,100,"NextToken","NextToken","",""]],["redis","r-kvstore","r-kvstore.aliyuncs.com","DescribeInstances","2015-01-01","Instances.KVStoreInstance",["InstanceId"],["InstanceName"],["ZoneId"],["InstanceStatus"],[["VpcId","vpc","located_in"],["VSwitchId","vswitch","located_in"]],["numbered","PageSize",1,50,"PageNumber","","",""]],["mongodb","dds","mongodb.aliyuncs.com","DescribeDBInstances","2015-12-01","DBInstances.DBInstance",["DBInstanceId"],["DBInstanceDescription"],["ZoneId"],["DBInstanceStatus"],[["VpcId","vpc","located_in"],["VSwitchId","vswitch","located_in"]],["numbered","PageSize",30,30,"PageNumber","","",""]],["nas","nas","nas.aliyuncs.com","DescribeFileSystems","2017-06-26","FileSystems.FileSystem",["FileSystemId"],["Description"],["ZoneId"],["Status"],[["VpcId","vpc","located_in"]],["numbered","PageSize",1,100,"PageNumber","","",""]],["ram_user","ram","ram.aliyuncs.com","ListUsers","2015-05-01","Users.User",["UserId"],["UserName"],[""],[""],[],["marker","MaxItems",1,100,"Marker","Marker","",""]],["kms_key","kms","kms.aliyuncs.com","ListKeys","2016-01-20","Keys.Key",["KeyId"],["KeyId"],[""],[""],[],["numbered","PageSize",1,100,"PageNumber","","",""]],["actiontrail","actiontrail","actiontrail.aliyuncs.com","DescribeTrails","2020-07-06","TrailList",["TrailRegion","Name"],["Name"],["TrailRegion"],["Status"],[],["none","",0,0,"","","",""]],["security_center_asset","sas","sas.aliyuncs.com","DescribeCloudCenterInstances","2018-12-03","Instances",["InstanceId"],["InstanceName"],["Region"],["Status"],[["VpcInstanceId","vpc","located_in"]],["next-token","PageSize",1,20,"NextToken","PageInfo.NextToken","UseNextToken","true"]]]
""";
    private static final Set<String> RELATIONS = Set.of(
        "located_in", "attached_to", "protected_by", "member_of", "serves", "routes_to"
    );

    public Object handle(Context context, Object rawInput) throws Exception {
        if (rawInput == null) rawInput = Map.of();
        if (!(rawInput instanceof Map<?, ?>)) throw new IllegalArgumentException("输入必须是 JSON 对象");
        Map<String, Object> configuredInput = new java.util.LinkedHashMap<>(CONFIG);
        for (Map.Entry<?, ?> entry : ((Map<?, ?>) rawInput).entrySet()) {
            configuredInput.put(String.valueOf(entry.getKey()), entry.getValue());
        }
        rawInput = configuredInput;
        Map<String, Object> input = object(rawInput, "input_must_be_object");
        String mode = String.valueOf(input.getOrDefault("mode", "preview"));
        if (!List.of("preview", "sync").contains(mode)) throw new IllegalArgumentException("invalid_mode");
        if ("sync".equals(mode)) validateIdentity(input);
        String account = required(input, "account");
        List<?> regions = list(input.get("regions"), "regions_required");
        if (account.length() > 256 || regions.isEmpty() || regions.size() > 32
            || new LinkedHashSet<>(regions).size() != regions.size()
            || regions.stream().anyMatch(value -> !(value instanceof String text)
                || text.length() > 128 || !text.matches(REGION_PATTERN))) {
            throw new IllegalArgumentException("account_and_regions_required");
        }
        int maxPages = positive(input.get("max_pages"), 50, 100);
        int maxRecords = positive(input.get("max_records"), 5_000, 50_000);
        int maxBytes = positive(input.get("max_bytes"), 8_388_608, 16_777_216);
        if (maxBytes < 1_024) throw new IllegalArgumentException("max_bytes_too_small");
        int pageSize = positive(input.get("page_size"), 100, 1_000);
        int timeout = positive(input.get("timeout_seconds"), 30, 120);
        long deadline = System.nanoTime() + Duration.ofSeconds(timeout).toNanos();
        Map<String, Object> fixture = input.get("fixture_pages") == null
            ? null : object(input.get("fixture_pages"), "invalid_fixture_pages");
        Map<String, Map<String, Object>> assets = new LinkedHashMap<>();
        Map<String, Map<String, Object>> relationships = new LinkedHashMap<>();
        List<Map<String, Object>> failures = new ArrayList<>();
        int pages = 0;
        boolean partial = false;
        boolean limitReached = false;
        List<String> sortedRegions = regions.stream().map(String.class::cast).distinct().sorted().toList();
        scan:
        for (String region : sortedRegions) {
            for (Object rawOperation : operations()) {
                List<?> operation = list(rawOperation, "invalid_operation");
                String resource = String.valueOf(operation.get(0));
                try {
                    String token = null;
                    Set<String> seenTokens = new LinkedHashSet<>();
                    for (int page = 1; ; page++) {
                        long remainingNanos = deadline - System.nanoTime();
                        if (pages >= maxPages || remainingNanos <= 0) {
                            partial = true; limitReached = true; break scan;
                        }
                        int remainingTimeout = Math.max(
                            1, (int) Math.min(120, (remainingNanos + 999_999_999L) / 1_000_000_000L)
                        );
                        PageRequest pageRequest = alicloudPageRequest(
                            operation, region, page, token, pageSize
                        );
                        Object payload;
                        if (fixture != null) {
                            Object rawBatches = fixture.get(resource);
                            List<?> batches = rawBatches instanceof List<?> values ? values : List.of();
                            payload = page <= batches.size() ? batches.get(page - 1) : Map.of();
                        } else {
                            payload = "alicloud".equals(PROVIDER)
                                ? alicloud(operation, region, pageRequest.query(), context, remainingTimeout)
                                : tencentcloud(operation, region, page, pageSize, context, remainingTimeout);
                        }
                        Object rawBatch = path(payload, String.valueOf(operation.get(5)));
                        List<?> batch = rawBatch instanceof List<?> values ? values : List.of();
                        pages++;
                        PageStep pageStep = alicloudContinuation(
                            operation, payload, batch.size(), pageRequest.size(), seenTokens
                        );
                        for (Object rawRecord : batch) {
                            if (!(rawRecord instanceof Map<?, ?>)) {
                                addFailureWithinBudget(
                                    failures, region, resource, assets, relationships, maxPages,
                                    maxBytes, "invalid_source_record"
                                );
                                partial = true; break scan;
                            }
                            Mapping mapped = normalize(operation, object(rawRecord, "invalid_record"), account, region);
                            if (mapped.asset == null) {
                                addFailureWithinBudget(
                                    failures, region, resource, assets, relationships, maxPages,
                                    maxBytes, "invalid_source_record"
                                );
                                partial = true; break scan;
                            }
                            String assetKey = String.valueOf(mapped.asset.get("external_key"));
                            if (!assets.containsKey(assetKey) && assets.size() >= maxRecords) {
                                partial = true; limitReached = true; break scan;
                            }
                            Map<String, Map<String, Object>> candidateAssets = new LinkedHashMap<>(assets);
                            Map<String, Map<String, Object>> candidateRelationships = new LinkedHashMap<>(relationships);
                            candidateAssets.put(assetKey, mapped.asset);
                            for (Map<String, Object> relation : mapped.relationships) {
                                candidateRelationships.put(
                                    relation.get("from") + "\u0000" + relation.get("type") + "\u0000" + relation.get("to"),
                                    relation
                                );
                            }
                            if (previewBytes(candidateAssets, candidateRelationships, maxPages, failures) > maxBytes) {
                                partial = true; limitReached = true; break scan;
                            }
                            assets.clear();
                            assets.putAll(candidateAssets);
                            relationships.clear();
                            relationships.putAll(candidateRelationships);
                        }
                        if (!pageStep.more()) break;
                        token = pageStep.token();
                    }
                } catch (Exception error) {
                    addFailureWithinBudget(
                        failures, region, resource, assets, relationships, maxPages, maxBytes
                    );
                    partial = true;
                }
            }
        }
        List<Map<String, Object>> assetList = new ArrayList<>(assets.values());
        assetList.sort(Comparator.comparing(value -> String.valueOf(value.get("external_key"))));
        List<Map<String, Object>> relationList = new ArrayList<>(relationships.values());
        relationList.sort(Comparator.comparing(value ->
            value.get("from") + "\u0000" + value.get("type") + "\u0000" + value.get("to")
        ));
        Map<String, Object> summary = new LinkedHashMap<>();
        summary.put("assets", assetList.size()); summary.put("relationships", relationList.size());
        summary.put("pages", pages);
        summary.put("failures", failures.subList(0, Math.min(failures.size(), 50)));
        Object checkpoint = partial ? Map.of(
            "failed", failures.subList(0, Math.min(failures.size(), 50)),
            "limit_reached", limitReached
        ) : null;
        if ("preview".equals(mode)) {
            Map<String, Object> result = new LinkedHashMap<>();
            result.put("schema_version", "dlr-asset-snapshot/v1");
            result.put("assets", assetList); result.put("relationships", relationList);
            result.put("summary", summary); result.put("partial", partial); result.put("checkpoint", checkpoint);
            return result;
        }
        if (partial) {
            Map<String, Object> result = new LinkedHashMap<>();
            result.put("mode", "sync"); result.put("scan_id", input.get("scan_id"));
            result.put("source_scope", input.get("source_scope")); result.put("partial", true);
            result.put("summary", summary); result.put("failed", failures); result.put("checkpoint", checkpoint);
            return result;
        }
        return sync(context, input, assetList, relationList, summary, deadline);
    }

    private static int previewBytes(
        Map<String, Map<String, Object>> assets,
        Map<String, Map<String, Object>> relationships,
        int pages,
        List<Map<String, Object>> failures
    ) {
        List<Map<String, Object>> assetList = new ArrayList<>(assets.values());
        assetList.sort(Comparator.comparing(value -> String.valueOf(value.get("external_key"))));
        List<Map<String, Object>> relationList = new ArrayList<>(relationships.values());
        relationList.sort(Comparator.comparing(value ->
            value.get("from") + "\u0000" + value.get("type") + "\u0000" + value.get("to")
        ));
        Map<String, Object> summary = new LinkedHashMap<>();
        summary.put("assets", assetList.size());
        summary.put("relationships", relationList.size());
        summary.put("pages", pages);
        summary.put("failures", failures);
        Map<String, Object> preview = new LinkedHashMap<>();
        preview.put("schema_version", "dlr-asset-snapshot/v1");
        preview.put("assets", assetList);
        preview.put("relationships", relationList);
        preview.put("summary", summary);
        preview.put("partial", true);
        preview.put("checkpoint", Map.of("failed", failures, "limit_reached", true));
        return Json.stringify(preview).getBytes(StandardCharsets.UTF_8).length;
    }

    private static void addFailureWithinBudget(
        List<Map<String, Object>> failures,
        String region,
        String resource,
        Map<String, Map<String, Object>> assets,
        Map<String, Map<String, Object>> relationships,
        int maxPages,
        int maxBytes
    ) {
        addFailureWithinBudget(
            failures, region, resource, assets, relationships, maxPages, maxBytes,
            "source_read_failed"
        );
    }

    private static void addFailureWithinBudget(
        List<Map<String, Object>> failures,
        String region,
        String resource,
        Map<String, Map<String, Object>> assets,
        Map<String, Map<String, Object>> relationships,
        int maxPages,
        int maxBytes,
        String error
    ) {
        if (failures.size() >= 50) return;
        List<Map<String, Object>> candidate = new ArrayList<>(failures);
        candidate.add(Map.of("region", region, "resource", resource, "error", error));
        if (previewBytes(assets, relationships, maxPages, candidate) <= maxBytes) {
            failures.add(candidate.get(candidate.size() - 1));
        }
    }

    private static Mapping normalize(List<?> operation, Map<String, Object> record, String account, String region) {
        Object rawIdentifier = first(record, list(operation.get(6), "invalid_id_fields"));
        if (!(rawIdentifier instanceof String identifier) || identifier.isBlank()) {
            return new Mapping(null, List.of());
        }
        String key = external(account, region, String.valueOf(operation.get(0)), identifier);
        Object name = first(record, list(operation.get(7), "invalid_name_fields"));
        Object zone = first(record, list(operation.get(8), "invalid_zone_fields"));
        Object status = first(record, list(operation.get(9), "invalid_status_fields"));
        Map<String, Object> asset = new LinkedHashMap<>();
        asset.put("external_key", key); asset.put("class", operation.get(0));
        asset.put("provider_type", operation.get(3)); asset.put("name", String.valueOf(name == null ? identifier : name));
        asset.put("account", account); asset.put("region", region);
        asset.put("zone", zone == null ? null : String.valueOf(zone));
        asset.put("status", status == null ? null : String.valueOf(status));
        asset.put("tags", Map.of()); asset.put("attributes", Map.of("source_action", operation.get(3)));
        List<Map<String, Object>> relationships = new ArrayList<>();
        for (Object rawRelation : list(operation.get(10), "invalid_relationships")) {
            List<?> relation = list(rawRelation, "invalid_relationship");
            String type = String.valueOf(relation.get(2));
            if (!RELATIONS.contains(type)) continue;
            for (Object target : values(path(record, String.valueOf(relation.get(0))))) {
                if (!(target instanceof String targetId) || targetId.isBlank()) continue;
                relationships.add(Map.of(
                    "from", key, "type", type,
                    "to", external(account, region, String.valueOf(relation.get(1)), targetId)
                ));
            }
        }
        return new Mapping(asset, relationships);
    }

    private static PageRequest alicloudPageRequest(
        List<?> operation, String region, int page, String token, int requestedSize
    ) {
        List<?> pagination = list(operation.get(11), "invalid_pagination");
        String kind = String.valueOf(pagination.get(0));
        Map<String, String> query = new LinkedHashMap<>();
        query.put("RegionId", region);
        int effectiveSize = 0;
        if (!"none".equals(kind)) {
            int minimum = ((Number) pagination.get(2)).intValue();
            int maximum = ((Number) pagination.get(3)).intValue();
            effectiveSize = Math.max(minimum, Math.min(requestedSize, maximum));
            query.put(String.valueOf(pagination.get(1)), String.valueOf(effectiveSize));
            if (List.of("numbered", "current-page").contains(kind)) {
                query.put(String.valueOf(pagination.get(4)), String.valueOf(page));
            } else if (token != null) {
                query.put(String.valueOf(pagination.get(4)), token);
            }
            if (!String.valueOf(pagination.get(6)).isEmpty()) {
                query.put(String.valueOf(pagination.get(6)), String.valueOf(pagination.get(7)));
            }
        }
        return new PageRequest(query, effectiveSize);
    }

    private static PageStep alicloudContinuation(
        List<?> operation, Object payload, int batchSize, int effectiveSize, Set<String> seenTokens
    ) {
        List<?> pagination = list(operation.get(11), "invalid_pagination");
        String kind = String.valueOf(pagination.get(0));
        if ("none".equals(kind)) return new PageStep(false, null);
        if (List.of("numbered", "current-page").contains(kind)) {
            return new PageStep(batchSize >= effectiveSize, null);
        }
        Object rawToken = path(payload, String.valueOf(pagination.get(5)));
        if (rawToken == null || String.valueOf(rawToken).isEmpty()) {
            return new PageStep(false, null);
        }
        if (!(rawToken instanceof String nextToken) || nextToken.length() > 4_096
            || !seenTokens.add(nextToken)) {
            throw new IllegalArgumentException("source_pagination_no_progress");
        }
        return new PageStep(true, nextToken);
    }

    private static Object alicloud(List<?> operation, String region, Map<String, String> query, Context context, int timeout) throws Exception {
        // 此处读取凭据：请在本适配器的“凭据绑定”中配置与 get(...) 参数一致的绑定键。
        String access = context.secrets.get("ALICLOUD_ACCESS_KEY_ID");
        // 此处读取凭据：请在本适配器的“凭据绑定”中配置与 get(...) 参数一致的绑定键。
        String secret = context.secrets.get("ALICLOUD_ACCESS_KEY_SECRET");
        if (access == null || secret == null) throw new IllegalArgumentException("missing_credential");
        Config config = new Config()
            .setAccessKeyId(access).setAccessKeySecret(secret)
            // 此处读取凭据：请在本适配器的“凭据绑定”中配置与 get(...) 参数一致的绑定键。
            .setSecurityToken(context.secrets.get("ALICLOUD_SECURITY_TOKEN"))
            .setEndpoint(alicloudEndpoint(String.valueOf(operation.get(2)), region))
            .setRegionId(region).setProtocol("HTTPS")
            .setReadTimeout(timeout * 1_000).setConnectTimeout(Math.min(timeout, 20) * 1_000);
        Client client = new Client(config);
        Params params = new Params()
            .setAction(String.valueOf(operation.get(3))).setVersion(String.valueOf(operation.get(4)))
            .setProtocol("HTTPS").setPathname("/").setMethod("POST").setAuthType("AK")
            .setBodyType("json").setReqBodyType("json").setStyle("RPC");
        OpenApiRequest apiRequest = new OpenApiRequest().setQuery(query);
        Map<String, ?> result = client.callApi(
            params, apiRequest,
            new RuntimeOptions().setReadTimeout(timeout * 1_000)
                .setConnectTimeout(Math.min(timeout, 20) * 1_000).setAutoretry(false)
        );
        Object body = result.get("body");
        return body == null ? result : body;
    }

    private static Object tencentcloud(List<?> operation, String region, int page, int size, Context context, int timeout) throws Exception {
        // 此处读取凭据：请在本适配器的“凭据绑定”中配置与 get(...) 参数一致的绑定键。
        String access = context.secrets.get("TENCENTCLOUD_SECRET_ID");
        // 此处读取凭据：请在本适配器的“凭据绑定”中配置与 get(...) 参数一致的绑定键。
        String secret = context.secrets.get("TENCENTCLOUD_SECRET_KEY");
        if (access == null || secret == null) throw new IllegalArgumentException("missing_credential");
        String service = String.valueOf(operation.get(1));
        String host = String.valueOf(operation.get(2));
        String action = String.valueOf(operation.get(3));
        String version = String.valueOf(operation.get(4));
        String body = Json.stringify(Map.of("Offset", (page - 1) * size, "Limit", size));
        long timestamp = Instant.now().getEpochSecond();
        String date = DateTimeFormatter.ISO_LOCAL_DATE.withZone(ZoneOffset.UTC).format(Instant.ofEpochSecond(timestamp));
        String canonical = String.join("\n",
            "POST", "/", "",
            "content-type:application/json; charset=utf-8\nhost:" + host.toLowerCase()
                + "\nx-tc-action:" + action.toLowerCase() + "\n",
            "content-type;host;x-tc-action", hex(sha256(body.getBytes(StandardCharsets.UTF_8)))
        );
        String scope = date + "/" + service + "/tc3_request";
        String toSign = String.join("\n", "TC3-HMAC-SHA256", String.valueOf(timestamp), scope,
            hex(sha256(canonical.getBytes(StandardCharsets.UTF_8))));
        byte[] key = hmac(hmac(hmac(("TC3" + secret).getBytes(StandardCharsets.UTF_8), date), service), "tc3_request");
        String signature = hex(hmac(key, toSign));
        String authorization = "TC3-HMAC-SHA256 Credential=" + access + "/" + scope
            + ", SignedHeaders=content-type;host;x-tc-action, Signature=" + signature;
        HttpRequest.Builder builder = HttpRequest.newBuilder(URI.create("https://" + host))
            .timeout(Duration.ofSeconds(timeout))
            .header("Content-Type", "application/json; charset=utf-8")
            .header("X-TC-Action", action).header("X-TC-Version", version)
            .header("X-TC-Timestamp", String.valueOf(timestamp)).header("X-TC-Region", region)
            .header("Authorization", authorization)
            .POST(HttpRequest.BodyPublishers.ofString(body));
        // 此处读取凭据：请在本适配器的“凭据绑定”中配置与 get(...) 参数一致的绑定键。
        String token = context.secrets.get("TENCENTCLOUD_TOKEN");
        if (token != null) builder.header("X-TC-Token", token);
        HttpResponse<InputStream> response = HttpClient.newBuilder()
            .followRedirects(HttpClient.Redirect.NEVER).build().send(
            builder.build(), HttpResponse.BodyHandlers.ofInputStream()
        );
        if (response.statusCode() < 200 || response.statusCode() >= 300) {
            response.body().close(); throw new IllegalArgumentException("provider_api_error");
        }
        byte[] bytes;
        try (InputStream stream = response.body()) { bytes = stream.readNBytes(4_194_305); }
        if (bytes.length > 4_194_304) throw new IllegalArgumentException("provider_response_too_large");
        Map<String, Object> payload = object(Json.parse(new String(bytes, StandardCharsets.UTF_8)), "provider_response_invalid");
        Object responseValue = payload.getOrDefault("Response", payload);
        if (responseValue instanceof Map<?, ?> map && map.get("Error") != null) {
            throw new IllegalArgumentException("provider_api_error");
        }
        return responseValue;
    }

    private static Object sync(Context context, Map<String, Object> input, List<Map<String, Object>> assets,
                               List<Map<String, Object>> relationships, Map<String, Object> summary, long deadline) throws Exception {
        String scan = required(input, "scan_id");
        String scope = required(input, "source_scope");
        String base = input.get("cmdb_base_url") instanceof String value ? value : null;
        // 此处读取凭据：请在本适配器的“凭据绑定”中配置与 get(...) 参数一致的绑定键。
        String token = context.secrets.get("CMDB_TOKEN");
        URI target;
        try { target = base == null ? null : URI.create(base); }
        catch (Exception error) { target = null; }
        String host = target == null ? null : target.getHost();
        boolean loopback = List.of("localhost", "127.0.0.1", "::1").contains(host);
        if (target == null || token == null || host == null || target.getUserInfo() != null
            || target.getRawQuery() != null || target.getFragment() != null
            || !("https".equals(target.getScheme()) || ("http".equals(target.getScheme()) && loopback))) {
            throw new IllegalArgumentException("cmdb_target_not_configured");
        }
        Map<String, Object> common = new LinkedHashMap<>();
        common.put("schema_version", "dlr-cmdb-upsert/v1"); common.put("source_scope", scope); common.put("scan_id", scan);
        try {
            String beginIdempotency = digest(List.of("begin", scope, scan));
            Map<String, Object> begin = new LinkedHashMap<>(common);
            begin.put("operation", "begin_scan"); begin.put("idempotency_key", beginIdempotency);
            begin.put("provider", PROVIDER); begin.put("catalog_version", "1.0.0");
            post(base, "/api/v1/import-scans:begin", begin, token, beginIdempotency, deadline);
            int batchSize = positive(input.get("batch_size"), 200, 1_000);
            List<List<Map<String, Object>>> phases = List.of(assets, relationships);
            List<String> names = List.of("assets", "relationships");
            for (int phaseAt = 0; phaseAt < phases.size(); phaseAt++) {
                List<Map<String, Object>> values = phases.get(phaseAt);
                String phase = names.get(phaseAt);
                for (int at = 0; at < values.size(); at += batchSize) {
                    List<Map<String, Object>> batch = values.subList(at, Math.min(values.size(), at + batchSize));
                    int batchIndex = at / batchSize;
                    String batchId = phase + ":" + PROVIDER + ":" + scope + ":" + String.format("%06d", batchIndex);
                    String idempotency = digest(List.of(phase, scope, scan, batchId));
                    Map<String, Object> body = new LinkedHashMap<>(common);
                    body.put("operation", "upsert_" + phase); body.put("idempotency_key", idempotency);
                    body.put("batch_id", batchId); body.put("batch_index", batchIndex); body.put(phase, batch);
                    post(base, "/api/v1/import-scans/" + encode(scan) + "/" + phase + ":upsert",
                        body, token, idempotency, deadline);
                }
            }
            String finishIdempotency = digest(List.of("finish", scope, scan));
            Map<String, Object> finish = new LinkedHashMap<>(common);
            finish.put("operation", "finish_scan"); finish.put("idempotency_key", finishIdempotency);
            finish.put("complete", true); finish.put("summary", summary);
            post(base, "/api/v1/import-scans/" + encode(scan) + ":finish",
                finish, token, finishIdempotency, deadline);
        } catch (Exception error) {
            Map<String, Object> failed = new LinkedHashMap<>();
            failed.put("mode", "sync"); failed.put("scan_id", scan); failed.put("source_scope", scope);
            failed.put("partial", true); failed.put("summary", summary);
            failed.put("failed", List.of("target_batch")); failed.put("checkpoint", Map.of("scan_id", scan));
            return failed;
        }
        Map<String, Object> done = new LinkedHashMap<>();
        done.put("mode", "sync"); done.put("scan_id", scan); done.put("source_scope", scope);
        done.put("partial", false); done.put("summary", summary); done.put("failed", List.of()); done.put("checkpoint", null);
        return done;
    }

    private static void post(String base, String path, Object body, String token, String idempotency, long deadline) throws Exception {
        long remaining = deadline - System.nanoTime();
        if (remaining <= 0) throw new IllegalArgumentException("cmdb_target_error");
        HttpRequest value = HttpRequest.newBuilder(URI.create(base.endsWith("/") ? base.substring(0, base.length() - 1) + path : base + path))
            .timeout(Duration.ofNanos(remaining)).header("Content-Type", "application/json")
            .header("Authorization", "Bearer " + token).header("Idempotency-Key", idempotency)
            .POST(HttpRequest.BodyPublishers.ofString(Json.stringify(body))).build();
        HttpResponse<InputStream> response = HttpClient.newBuilder()
            .followRedirects(HttpClient.Redirect.NEVER).build()
            .send(value, HttpResponse.BodyHandlers.ofInputStream());
        response.body().close();
        if (response.statusCode() < 200 || response.statusCode() >= 300) throw new IllegalArgumentException("cmdb_target_error");
    }

    @SuppressWarnings("unchecked")
    private static List<?> operations() { return (List<?>) Json.parse(OPERATIONS_JSON); }
    private static Object path(Object value, String dotted) { Object current = value; if (dotted.isEmpty()) return current; for (String part : dotted.split("\\.")) { if (!(current instanceof Map<?, ?> map) || !map.containsKey(part)) return null; current = map.get(part); } return current; }
    private static Object first(Map<String, Object> record, List<?> fields) { for (Object field : fields) { Object value = path(record, String.valueOf(field)); if (value != null && !String.valueOf(value).isEmpty() && (!(value instanceof List<?> list) || !list.isEmpty())) return value; } return null; }
    private static List<?> values(Object value) { if (value == null || String.valueOf(value).isEmpty()) return List.of(); if (value instanceof List<?> list) return list; if (value instanceof Map<?, ?> map) for (Object nested : map.values()) if (nested instanceof List<?> list) return list; return List.of(value); }
    private static String external(String account, String region, String type, Object id) { return PROVIDER + ":" + encode(account) + ":" + encode(region == null || region.isEmpty() ? "global" : region) + ":" + type + ":" + encode(String.valueOf(id)); }
    private static String alicloudEndpoint(String template, String region) { String marker = ".cn-hangzhou.aliyuncs.com"; return template.endsWith(marker) ? template.substring(0, template.length() - marker.length()) + "." + region + ".aliyuncs.com" : template; }
    private static String encode(String value) { return URLEncoder.encode(value, StandardCharsets.UTF_8).replace("+", "%20"); }
    private static String digest(Object value) { return hex(sha256(Json.stringify(value).getBytes(StandardCharsets.UTF_8))); }
    private static byte[] sha256(byte[] value) { try { return MessageDigest.getInstance("SHA-256").digest(value); } catch (Exception error) { throw new IllegalStateException(error); } }
    private static byte[] hmac(byte[] key, String value) { try { Mac mac = Mac.getInstance("HmacSHA256"); mac.init(new SecretKeySpec(key, "HmacSHA256")); return mac.doFinal(value.getBytes(StandardCharsets.UTF_8)); } catch (Exception error) { throw new IllegalStateException(error); } }
    private static String hex(byte[] value) { return java.util.HexFormat.of().formatHex(value); }
    private static void validateIdentity(Map<String, Object> input) { for (String key : List.of("scan_id", "source_scope")) { String value = required(input, key); if (!value.matches("[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}")) throw new IllegalArgumentException("invalid_" + key); } }
    private static int positive(Object raw, int fallback, int maximum) { return raw instanceof Number n && n.intValue() > 0 ? Math.min(n.intValue(), maximum) : fallback; }
    private static String required(Map<String, Object> value, String key) { Object raw = value.get(key); if (!(raw instanceof String text) || text.isBlank()) throw new IllegalArgumentException("invalid_" + key); return text; }
    private static List<?> list(Object raw, String code) { if (!(raw instanceof List<?> value)) throw new IllegalArgumentException(code); return value; }
    @SuppressWarnings("unchecked") private static Map<String, Object> object(Object raw, String code) { if (!(raw instanceof Map<?, ?> value)) throw new IllegalArgumentException(code); return (Map<String, Object>) value; }
    private record Mapping(Map<String, Object> asset, List<Map<String, Object>> relationships) {}
    private record PageRequest(Map<String, String> query, int size) {}
    private record PageStep(boolean more, String token) {}
}
