"""Bounded S3-compatible list and optional range reads."""

from __future__ import annotations

import base64
import ipaddress
from contextlib import suppress
from urllib.parse import urlsplit

# S3 对象读取：可修改的配置集中在这里。
# 默认无需填写运行输入；先修改下面的地址、查询条件等配置，再保存运行。
# 调试时可传入 JSON 对象覆盖同名配置；嵌套对象需要完整填写。
# 凭据配置：先在“凭据”中创建对应值，再到此适配器的“凭据绑定”中绑定；绑定键必须与下列名称完全一致。
# S3_ACCESS_KEY_ID：S3 Access Key ID。
# S3_SECRET_ACCESS_KEY：S3 Secret Access Key。
# S3_SESSION_TOKEN：S3 临时凭据 Token，仅使用临时凭据时配置。
CONFIG = {
    # S3 兼容服务地址。
    "endpoint": "https://storage.example",
    # 存储桶所在区域 ID。
    "region": "example-region-1",
    # 填写存储桶名称。
    "bucket": "example-bucket",
    # 只列出此前缀下的对象；空字符串表示整个存储桶。
    "prefix": "exports/",
    # 继续读取时填写上次 checkpoint 返回的分页标记；首次留空。
    "continuation_token": None,
    # 同一页内继续读取的位置，首次为 0。
    "object_offset": 0,
    # 填写要读取的对象键；空列表只获取对象清单。
    "read_keys": [],
    # 兼容私有 S3 服务时通常使用 true。
    "force_path_style": True,
    # 单次运行最多读取的页数。
    "max_pages": 20,
    # 最多列出的对象数。
    "max_objects": 1000,
    # 单个对象读取大小上限，单位字节。
    "max_object_bytes": 1048576,
    # 读取内容总大小上限，单位字节。
    "max_total_bytes": 4194304,
}


_STABLE_ERRORS = frozenset(
    {
        "input_must_be_object",
        "bucket_required",
        "invalid_endpoint",
        "missing_credential",
        "invalid_read_keys",
        "invalid_continuation_token",
        "invalid_object_offset",
        "invalid_checkpoint",
        "max_total_bytes_too_small",
    }
)


def _positive(value: object, default: int, maximum: int) -> int:
    return (
        min(value, maximum)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0
        else default
    )


def _validated_endpoint(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("invalid_endpoint")
    parsed = urlsplit(value)
    if (
        not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("invalid_endpoint")
    if parsed.scheme == "https":
        return value
    if parsed.scheme == "http":
        try:
            loopback = ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            loopback = parsed.hostname.lower() == "localhost"
        if loopback:
            return value
    raise ValueError("invalid_endpoint")


def _handle(context, input):
    if not isinstance(input, dict):
        raise ValueError("input_must_be_object")
    bucket = input.get("bucket")
    if not isinstance(bucket, str) or not bucket:
        raise ValueError("bucket_required")
    endpoint = _validated_endpoint(input.get("endpoint"))
    requested = _read_keys(input.get("read_keys", []))
    max_total_bytes = _positive(input.get("max_total_bytes"), 4_194_304, 16_777_216)
    continuation = input.get("continuation_token")
    if continuation is not None and (not isinstance(continuation, str) or not continuation):
        raise ValueError("invalid_continuation_token")
    object_offset = input.get("object_offset", 0)
    if (
        not isinstance(object_offset, int)
        or isinstance(object_offset, bool)
        or not 0 <= object_offset <= 1_000
    ):
        raise ValueError("invalid_object_offset")
    if (
        max_total_bytes < 256
        or _result_size(
            0,
            0,
            0,
            0,
            0,
            0,
            True,
            _checkpoint(continuation, object_offset, "checkpoint_limit"),
        )
        > max_total_bytes
    ):
        raise ValueError("max_total_bytes_too_small")
    # 此处读取凭据：请在本适配器的“凭据绑定”中配置与 get(...) 参数一致的绑定键。
    access_key = context.secrets.get("S3_ACCESS_KEY_ID")
    # 此处读取凭据：请在本适配器的“凭据绑定”中配置与 get(...) 参数一致的绑定键。
    secret_key = context.secrets.get("S3_SECRET_ACCESS_KEY")
    if not access_key or not secret_key:
        raise ValueError("missing_credential")
    import boto3
    from botocore.config import Config

    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name=str(input.get("region", "us-east-1")),
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        # 此处读取凭据：请在本适配器的“凭据绑定”中配置与 get(...) 参数一致的绑定键。
        aws_session_token=context.secrets.get("S3_SESSION_TOKEN"),
        config=Config(
            connect_timeout=10,
            read_timeout=30,
            retries={"max_attempts": 2, "mode": "standard"},
            s3={"addressing_style": "path" if input.get("force_path_style") is True else "auto"},
        ),
    )
    try:
        return _list_and_read(client, bucket, input, requested)
    finally:
        with suppress(Exception):
            client.close()


def _read_keys(raw: object) -> set[str]:
    if (
        not isinstance(raw, list)
        or len(raw) > 1_000
        or not all(isinstance(key, str) for key in raw)
    ):
        raise ValueError("invalid_read_keys")
    return set(raw)


def _encoded_size(value: object) -> int:
    import json

    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())


def _array_size(count: int, item_bytes: int) -> int:
    return 2 if count == 0 else item_bytes + count + 1


def _checkpoint(token: str | None, object_offset: int, reason: str | None = None):
    value = {"continuation_token": token, "object_offset": object_offset}
    if reason is not None:
        value["reason"] = reason
    return value


def _result(
    objects,
    contents,
    total_bytes: int,
    pages: int,
    partial: bool,
    checkpoint,
    *,
    object_count: int | None = None,
):
    return {
        "objects": objects,
        "contents": contents,
        "summary": {
            "objects": len(objects) if object_count is None else object_count,
            "bytes_read": total_bytes,
            "pages": pages,
        },
        "partial": partial,
        "checkpoint": checkpoint,
    }


def _result_size(
    object_count: int,
    object_item_bytes: int,
    content_count: int,
    content_item_bytes: int,
    total_bytes: int,
    pages: int,
    partial: bool,
    checkpoint,
) -> int:
    shell = _result(
        [],
        [],
        total_bytes,
        pages,
        partial,
        checkpoint,
        object_count=object_count,
    )
    return (
        _encoded_size(shell)
        - 4
        + _array_size(object_count, object_item_bytes)
        + _array_size(content_count, content_item_bytes)
    )


def _read_content_size(key: str, raw_bytes: int) -> int:
    shell = {
        "key": key,
        "status": "read",
        "bytes": raw_bytes,
        "content_base64": "",
    }
    return _encoded_size(shell) + 4 * ((raw_bytes + 2) // 3)


def _raw_capacity(
    key: str,
    maximum: int,
    object_count: int,
    object_item_bytes: int,
    content_count: int,
    content_item_bytes: int,
    total_bytes: int,
    pages: int,
    checkpoint,
    max_total_bytes: int,
) -> int:
    low, high = 0, maximum
    while low < high:
        candidate = (low + high + 1) // 2
        candidate_content_bytes = content_item_bytes + _read_content_size(key, candidate)
        if (
            _result_size(
                object_count,
                object_item_bytes,
                content_count + 1,
                candidate_content_bytes,
                total_bytes + candidate,
                pages,
                True,
                checkpoint,
            )
            <= max_total_bytes
        ):
            low = candidate
        else:
            high = candidate - 1
    return low


def _list_and_read(client, bucket: str, input: dict[str, object], requested: set[str]):
    max_objects = _positive(input.get("max_objects"), 1_000, 10_000)
    max_pages = _positive(input.get("max_pages"), 20, 200)
    max_object_bytes = _positive(input.get("max_object_bytes"), 1_048_576, 8_388_608)
    max_total_bytes = _positive(input.get("max_total_bytes"), 4_194_304, 16_777_216)
    continuation = input.get("continuation_token")
    object_offset = input.get("object_offset", 0)
    objects: list[dict[str, object]] = []
    contents: list[dict[str, object]] = []
    object_item_bytes = 0
    content_item_bytes = 0
    total_bytes = 0
    partial = False
    pages = 0
    output_checkpoint = None
    stopped = False
    while pages < max_pages and not stopped:
        page_token = continuation
        page_offset = object_offset
        params = {
            "Bucket": bucket,
            "Prefix": str(input.get("prefix", "")),
            "MaxKeys": min(1_000, max_objects),
        }
        if page_token:
            params["ContinuationToken"] = page_token
        response = client.list_objects_v2(**params)
        pages += 1
        page_items = response.get("Contents", [])
        if page_offset > len(page_items):
            raise ValueError("invalid_checkpoint")
        for item_at in range(page_offset, len(page_items)):
            item = page_items[item_at]
            current_checkpoint = _checkpoint(page_token, item_at, "output_limit")
            after_checkpoint = _checkpoint(page_token, item_at + 1, "checkpoint_limit")
            if len(objects) >= max_objects:
                partial = True
                output_checkpoint = _checkpoint(page_token, item_at, "max_objects")
                stopped = True
                break
            key = str(item["Key"])
            size = int(item.get("Size", 0))
            metadata = {
                "key": key,
                "size": size,
                "etag": str(item.get("ETag", "")).strip('"'),
                "lastModified": item.get("LastModified").isoformat()
                if item.get("LastModified")
                else None,
            }
            metadata_bytes = _encoded_size(metadata)
            candidate_object_bytes = object_item_bytes + metadata_bytes
            candidate_content = None
            if key in requested and (
                size > max_object_bytes or total_bytes + size > max_total_bytes
            ):
                candidate_content = {
                    "key": key,
                    "status": "limit_exceeded",
                    "size": size,
                }
            if candidate_content is not None:
                candidate_content_bytes = content_item_bytes + _encoded_size(candidate_content)
                if (
                    _result_size(
                        len(objects) + 1,
                        candidate_object_bytes,
                        len(contents) + 1,
                        candidate_content_bytes,
                        total_bytes,
                        pages,
                        True,
                        after_checkpoint,
                    )
                    > max_total_bytes
                ):
                    partial = True
                    output_checkpoint = current_checkpoint
                    stopped = True
                    break
                objects.append(metadata)
                contents.append(candidate_content)
                object_item_bytes = candidate_object_bytes
                content_item_bytes = candidate_content_bytes
                partial = True
                continue
            if key not in requested:
                if (
                    _result_size(
                        len(objects) + 1,
                        candidate_object_bytes,
                        len(contents),
                        content_item_bytes,
                        total_bytes,
                        pages,
                        True,
                        after_checkpoint,
                    )
                    > max_total_bytes
                ):
                    partial = True
                    output_checkpoint = current_checkpoint
                    stopped = True
                    break
                objects.append(metadata)
                object_item_bytes = candidate_object_bytes
                continue
            read_limit = _raw_capacity(
                key,
                max_object_bytes,
                len(objects) + 1,
                candidate_object_bytes,
                len(contents),
                content_item_bytes,
                total_bytes,
                pages,
                after_checkpoint,
                max_total_bytes,
            )
            if size > read_limit:
                partial = True
                output_checkpoint = current_checkpoint
                stopped = True
                break
            if read_limit == 0:
                body = b""
            else:
                result = client.get_object(
                    Bucket=bucket,
                    Key=key,
                    Range=f"bytes=0-{read_limit - 1}",
                )
                body_stream = result["Body"]
                try:
                    body = body_stream.read(read_limit + 1)
                finally:
                    with suppress(Exception):
                        body_stream.close()
            if len(body) > read_limit:
                partial = True
                output_checkpoint = current_checkpoint
                stopped = True
                break
            encoded = base64.b64encode(body).decode()
            candidate_content = {
                "key": key,
                "status": "read",
                "bytes": len(body),
                "content_base64": encoded,
            }
            candidate_content_bytes = content_item_bytes + _encoded_size(candidate_content)
            if (
                _result_size(
                    len(objects) + 1,
                    candidate_object_bytes,
                    len(contents) + 1,
                    candidate_content_bytes,
                    total_bytes + len(body),
                    pages,
                    True,
                    after_checkpoint,
                )
                > max_total_bytes
            ):
                partial = True
                output_checkpoint = current_checkpoint
                stopped = True
                break
            objects.append(metadata)
            contents.append(candidate_content)
            object_item_bytes = candidate_object_bytes
            content_item_bytes = candidate_content_bytes
            total_bytes += len(body)
        if stopped:
            break
        truncated = bool(response.get("IsTruncated"))
        next_continuation = response.get("NextContinuationToken")
        if not truncated:
            continuation = None
            object_offset = 0
            break
        if not isinstance(next_continuation, str) or not next_continuation:
            partial = True
            output_checkpoint = _checkpoint(page_token, len(page_items), "missing_token")
            break
        next_checkpoint = _checkpoint(next_continuation, 0)
        if (
            _result_size(
                len(objects),
                object_item_bytes,
                len(contents),
                content_item_bytes,
                total_bytes,
                pages,
                True,
                next_checkpoint,
            )
            > max_total_bytes
        ):
            partial = True
            output_checkpoint = _checkpoint(page_token, len(page_items), "output_limit")
            break
        continuation = next_continuation
        object_offset = 0
    if continuation is not None and output_checkpoint is None:
        partial = True
        output_checkpoint = _checkpoint(continuation, object_offset)
    result = _result(objects, contents, total_bytes, pages, partial, output_checkpoint)
    if _encoded_size(result) > max_total_bytes:
        raise ValueError("max_total_bytes_too_small")
    return result


def handle(context, input):
    if input is None:
        input = {}
    if not isinstance(input, dict):
        raise ValueError("输入必须是 JSON 对象")
    input = {**CONFIG, **input}
    try:
        return _handle(context, input)
    except ValueError as error:
        code = str(error)
        if code in _STABLE_ERRORS:
            raise ValueError(code) from None
        raise ValueError("s3_operation_failed") from None
    except Exception:
        raise ValueError("s3_operation_failed") from None
