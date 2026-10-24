"""Real PostgreSQL oracles for the isolated example target, not DLR custody mocks."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import re
import threading
import uuid
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.request import Request, urlopen

import psycopg
import pytest
from psycopg import sql
from sqlalchemy.engine import make_url

_SPEC = importlib.util.spec_from_file_location(
    "kubernetes_target_fixture",
    Path(__file__).resolve().parents[2] / "tools/subscriptions/kubernetes_target_probe.py",
)
assert _SPEC is not None and _SPEC.loader is not None
target = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(target)
_SAMPLE_SPEC = importlib.util.spec_from_file_location(
    "kubernetes_ordinary_sample",
    Path(__file__).resolve().parents[2] / "samples/event-subscriptions/kubernetes_to_target.py",
)
assert _SAMPLE_SPEC is not None and _SAMPLE_SPEC.loader is not None
sample = importlib.util.module_from_spec(_SAMPLE_SPEC)
_SAMPLE_SPEC.loader.exec_module(sample)


@pytest.fixture(scope="module")
def target_dsn() -> Iterator[str]:
    # This tool has its own schema and deliberately does not migrate application tables.
    configured = os.environ.get("DATABASE_URL")
    if not configured:
        pytest.skip("DATABASE_URL is required for the dedicated PostgreSQL target fixture")
    name = os.environ.get("DLR_TARGET_TEST_DATABASE", "dlr_target_test_issue219")
    assert re.fullmatch(r"dlr_target_test_[a-z0-9_]{1,40}", name)
    base = make_url(configured).set(drivername="postgresql")
    maintenance = base.set(database="postgres").render_as_string(hide_password=False)
    with psycopg.connect(maintenance, autocommit=True) as connection:
        connection.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
        )
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    dsn = base.set(database=name).render_as_string(hide_password=False)
    with psycopg.connect(dsn) as connection:
        target.initialize(connection)
    yield dsn
    with psycopg.connect(maintenance, autocommit=True) as connection:
        connection.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


@pytest.fixture
def connection(target_dsn: str) -> Iterator[psycopg.Connection[Any]]:
    with psycopg.connect(target_dsn, autocommit=True) as connection:
        yield connection


@pytest.fixture
def scope() -> str:
    return hashlib.sha256(uuid.uuid4().bytes).hexdigest()


def event(
    scope: str,
    kind: str,
    *,
    batch: str | None = "batch-a",
    uid: str = "uid-a",
    name: str = "same-name",
    count: int = 0,
    watch: str = "LIST",
    rv: str = "opaque-rv",
) -> dict[str, Any]:
    control = kind.startswith("sync.")
    sync = (
        None
        if batch is None
        else {
            "batch_id": batch,
            "scope_fingerprint": scope,
            "phase": kind.removeprefix("sync.")
            if control
            else "list"
            if watch == "LIST"
            else "watch",
            "read_complete": kind == "sync.completed",
            "item_count": count,
            "failure_code": "source_page_rejected" if kind == "sync.failed" else None,
        }
    )
    meta = {"uid": uid, "name": name, "namespace": "test-scope", "resourceVersion": rv}
    ident = hashlib.sha256(
        target.canonical(
            [
                scope,
                kind,
                batch if control or watch == "LIST" else None,
                None if control else uid,
                None if control else rv,
            ]
        )
    ).hexdigest()
    return {
        "schema": "dlr.event.v1",
        "subscription": {"id": "1", "config_version": 1, "source_epoch": "test-epoch"},
        "event": {"id": ident, "kind": kind, "object_key": None if control else uid},
        "source": {
            "type": "kubernetes",
            "identity": "kubernetes-scope-v1:" + scope,
            "position": {"resource_version": None if control else rv},
            "attributes": {
                "cluster_id": "logical-test-cluster",
                "group": "",
                "version": "v1",
                "resource": "pods",
                "namespace": "test-scope",
                "name": None if control else name,
                "uid": None if control else uid,
                "watch_type": None if control else watch,
            },
        },
        "received_at": "2026-01-01T00:00:00Z",
        "data": {
            "encoding": "json",
            "content_type": "application/json",
            "value": None if control else {"metadata": meta},
        },
        "sync": sync,
    }


def apply(connection: psycopg.Connection[Any], envelope: dict[str, Any]) -> dict[str, Any]:
    with connection.transaction():
        return target.apply_event(connection, envelope)


def objects(connection: psycopg.Connection[Any], scope: str) -> dict[str, int]:
    return dict(
        connection.execute(
            "SELECT uid,writes FROM dlr_k8s_target_objects "
            "WHERE subscription_id='1' AND scope_fingerprint=%s ORDER BY uid",
            (scope,),
        ).fetchall()
    )


def test_cleanup_requires_complete_members_and_stays_in_owned_scope(connection, scope):
    outside = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
    for owner in [scope, outside]:
        apply(connection, event(owner, "sync.started"))
        apply(connection, event(owner, "object.present", uid="old-uid"))
        apply(connection, event(owner, "sync.completed", count=1))
    apply(connection, event(scope, "sync.started", batch="batch-b"))
    member = event(scope, "object.present", batch="batch-b", uid="new-uid")
    apply(connection, member)
    completed = event(scope, "sync.completed", batch="batch-b", count=2)
    with pytest.raises(target.TargetRejected) as failure:
        apply(connection, completed)
    assert failure.value.code == "target_batch_members_incomplete"
    assert objects(connection, scope) == {"new-uid": 1, "old-uid": 1}
    completed["sync"]["item_count"] = 1
    assert apply(connection, completed)["cleaned"] == 1
    assert objects(connection, scope) == {"new-uid": 1}
    assert objects(connection, outside) == {"old-uid": 1}
    # Repeating an already applied control never repeats its cleanup.
    assert apply(connection, completed)["applied"] is False


def test_failed_batch_allows_members_to_drain_but_never_cleanup(connection, scope):
    apply(connection, event(scope, "sync.started"))
    apply(connection, event(scope, "sync.failed"))
    apply(connection, event(scope, "object.present"))
    with pytest.raises(target.TargetRejected) as failure:
        apply(connection, event(scope, "sync.completed", count=1))
    assert failure.value.code == "target_batch_cleanup_ineligible"
    assert objects(connection, scope) == {"uid-a": 1}


@pytest.mark.parametrize("old_completed_committed", [False, True])
def test_new_generation_blocks_late_cleanup_and_cached_control_reexecution(
    connection, scope, old_completed_committed
):
    apply(connection, event(scope, "sync.started"))
    apply(connection, event(scope, "object.present", uid="old-uid"))
    old = event(scope, "sync.completed", count=1)
    if old_completed_committed:
        apply(connection, old)
    # This models a new explicitly enabled batch after platform custody settled.
    apply(connection, event(scope, "sync.started", batch="batch-b"))
    apply(connection, event(scope, "object.present", batch="batch-b", uid="new-uid"))
    before = objects(connection, scope)
    if old_completed_committed:
        assert apply(connection, old)["applied"] is False
    else:
        with pytest.raises(target.TargetRejected) as failure:
            apply(connection, old)
        assert failure.value.code == "target_batch_cleanup_ineligible"
    assert objects(connection, scope) == before
    assert "new-uid" in before


def test_same_name_replacement_survives_old_uid_delete_and_no_batch_watch(connection, scope):
    apply(connection, event(scope, "sync.started"))
    apply(connection, event(scope, "object.present", uid="old-uid"))
    apply(connection, event(scope, "sync.completed", count=1))
    apply(connection, event(scope, "object.present", batch=None, uid="new-uid", watch="ADDED"))
    removed = event(scope, "object.removed", batch=None, uid="old-uid", watch="DELETED")
    assert apply(connection, removed)["removed"] == 1
    assert objects(connection, scope) == {"new-uid": 1}
    assert apply(connection, removed)["applied"] is False


@pytest.mark.parametrize("watch", ["MODIFIED", "DELETED"])
def test_old_batch_watch_cannot_overwrite_or_delete_new_generation(connection, scope, watch):
    apply(connection, event(scope, "sync.started"))
    apply(connection, event(scope, "object.present", rv="old-opaque"))
    apply(connection, event(scope, "sync.completed", count=1))
    apply(connection, event(scope, "sync.started", batch="batch-b"))
    apply(connection, event(scope, "object.present", batch="batch-b", rv="new-opaque"))
    late = event(
        scope,
        "object.removed" if watch == "DELETED" else "object.present",
        watch=watch,
        rv="late-old-opaque",
    )
    with pytest.raises(target.TargetRejected) as failure:
        apply(connection, late)
    assert failure.value.code == "target_watch_generation_stale"
    assert objects(connection, scope) == {"uid-a": 2}
    assert connection.execute(
        "SELECT payload->'metadata'->>'resourceVersion' FROM dlr_k8s_target_objects "
        "WHERE subscription_id='1' AND scope_fingerprint=%s AND uid='uid-a'",
        (scope,),
    ).fetchone() == ("new-opaque",)


def test_completed_rechecks_present_member_and_event_content_conflict(connection, scope):
    apply(connection, event(scope, "sync.started"))
    member = event(scope, "object.present")
    apply(connection, member)
    changed = copy.deepcopy(member)
    changed["data"]["value"]["unexpected"] = "changed-content"
    with pytest.raises(target.TargetRejected) as failure:
        apply(connection, changed)
    assert failure.value.code == "target_event_identity_conflict"
    assert objects(connection, scope) == {"uid-a": 1}
    # Even a durable membership receipt is insufficient if its business row disappeared.
    apply(connection, event(scope, "object.removed", batch=None, watch="DELETED"))
    with pytest.raises(target.TargetRejected) as failure:
        apply(connection, event(scope, "sync.completed", count=1))
    assert failure.value.code == "target_batch_members_incomplete"


@pytest.mark.parametrize(
    "kind", ["sync.started", "object.present", "sync.completed", "sync.failed"]
)
def test_http_postcommit_response_loss_has_one_persistent_effect(
    connection, scope, target_dsn, kind
):
    if kind != "sync.started":
        apply(connection, event(scope, "sync.started"))
    if kind == "sync.completed":
        apply(connection, event(scope, "object.present"))
    envelope = event(scope, kind, count=1 if kind == "sync.completed" else 0)
    token = "synthetic-target-fixture-token"
    server = target.create_server(
        {
            "database_url": target_dsn,
            "token": token,
            "host": "127.0.0.1",
            "port": 0,
        }
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = f"http://127.0.0.1:{server.server_port}"
    context = SimpleNamespace(
        config={"target_url": endpoint + "/events", "timeout_seconds": 5},
        secrets={"EVENT_TARGET_TOKEN": token},
    )

    def post(path, value, ident="fault-setup"):
        request = Request(
            endpoint + path,
            data=target.canonical(value),
            method="POST",
            headers={
                "Authorization": "Bearer " + token,
                "Content-Type": "application/json",
                "Idempotency-Key": ident,
            },
        )
        with urlopen(request, timeout=5) as response:
            return json.load(response)

    try:
        post("/fault", {"mode": "lost_response_once", "source_event_id": envelope["event"]["id"]})
        with pytest.raises(RuntimeError, match="event_target_result_unknown"):
            sample.handle(context, envelope)
        # The original HTTP result was unknown, but independent SQL sees the committed result.
        assert connection.execute(
            "SELECT count(*) FROM dlr_k8s_target_events "
            "WHERE subscription_id='1' AND source_event_id=%s",
            (envelope["event"]["id"],),
        ).fetchone() == (1,)
        assert sample.handle(context, envelope)["applied"] is False
        if kind == "object.present":
            assert objects(connection, scope) == {"uid-a": 1}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()
