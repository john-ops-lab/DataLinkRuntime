"""Offline image maintenance uses real PostgreSQL, with no product lifespan."""

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from dlr.common.config import settings
from dlr.common.schema_version import SCHEMA_REVISION
from dlr.control.control_identity_init_v4 import maintain
from dlr.control.models import (
    Adapter,
    AdapterVersion,
    Execution,
    ExecutionAttempt,
    Worker,
    WorkerIdentityReservation,
    WorkerInstanceHistory,
    WorkerLeaseCandidateV4,
)
from dlr.control.services import control_cluster_identity as identity
from dlr.control.services import control_image_transition_v4 as transition
from dlr.control.services.control_declaration_v4 import DeploymentDeclaration, read_private_json
from dlr.control.services.worker_lease_proof_v4 import public_key_bytes
from dlr.control.services.worker_remote_deployment_v4 import RemoteDeploymentUnavailable
from test_issue219_upgrade import migration_config, upgrade_database  # noqa: F401


@pytest.fixture
def offline(upgrade_database, tmp_path, monkeypatch):  # noqa: F811
    config = migration_config(upgrade_database)
    command.upgrade(config, SCHEMA_REVISION)
    role = "w1f_runtime_" + uuid4().hex[:12]
    with upgrade_database.begin() as conn:
        conn.execute(text(f'CREATE ROLE "{role}" NOLOGIN'))
        database = upgrade_database.url.database
        assert database.startswith("dlr_219_upgrade_") and database.replace("_", "").isalnum()
        conn.execute(text(f'REVOKE CONNECT,TEMPORARY ON DATABASE "{database}" FROM PUBLIC'))
        conn.execute(text(f'GRANT CONNECT ON DATABASE "{database}" TO "{role}"'))
    monkeypatch.setattr(settings, "worker_remote_v4_enabled", True)
    monkeypatch.setattr(settings, "worker_v4_database_role", role)
    monkeypatch.setattr(settings, "worker_v4_database_admin_roles", [])
    monkeypatch.setattr(settings, "worker_v4_control_slots", 2)
    key = b"s" * 32
    source = DeploymentDeclaration(
        identity.ControlDeclaration(
            cluster_id=uuid4(),
            image_digest="sha256:" + "1" * 64,
            managed_fingerprint="2" * 64,
            builtin_fingerprint="3" * 64,
            attachment_fingerprint="4" * 64,
            capacity_fingerprint="5" * 64,
            master_key="m" * 64,
            admin_token="a" * 64,
            worker_token="w" * 64,
            worker_proof_public_key=public_key_bytes(key),
        ),
        uuid4(),
        key,
    )
    target = replace(source, authority=replace(source.authority, image_digest="sha256:" + "6" * 64))
    proof = identity._validated(source.authority)
    with upgrade_database.begin() as conn:
        conn.execute(
            text("""
            INSERT INTO control_cluster_authority
              (singleton,cluster_id,authority_epoch,schema_revision,image_digest,
               master_check,admin_check,worker_check,worker_proof_public_key,
               managed_fingerprint,builtin_fingerprint,attachment_fingerprint,capacity_fingerprint)
            VALUES (1,:cluster,:epoch,:schema,:image,:master,:admin,:worker,:key,:m,:b,:a,:c)
        """),
            {
                "cluster": source.authority.cluster_id,
                "epoch": source.authority_epoch,
                "schema": SCHEMA_REVISION,
                "image": source.authority.image_digest,
                "master": proof.master_check,
                "admin": proof.admin_check,
                "worker": proof.worker_check,
                "key": source.authority.worker_proof_public_key,
                "m": "2" * 64,
                "b": "3" * 64,
                "a": "4" * 64,
                "c": "5" * 64,
            },
        )
    path = tmp_path / "intent.json"
    body = {
        "operation_id": str(uuid4()),
        "previous_id": None,
        "source_schema": SCHEMA_REVISION,
        "target_schema": SCHEMA_REVISION,
        "source_sha": transition.G_SOURCE_SHA,
        "target_sha": "b" * 40,
        "evidence_sha256": "e" * 64,
        "control_slots": 2,
    }
    path.write_text(json.dumps(body))
    path.chmod(0o600)
    intent = transition.read_intent(str(path), source, target)
    upgrade_database.dispose()  # A pooled idle connection is still a live client.
    engine = create_engine(upgrade_database.url, poolclass=NullPool)
    value = SimpleNamespace(
        engine=engine,
        factory=sessionmaker(engine),
        config=config,
        role=role,
        source=source,
        target=target,
        intent=intent,
        path=path,
        body=body,
    )
    try:
        yield value
    finally:
        engine.dispose()
        with upgrade_database.begin() as conn:
            conn.execute(text(f'DROP OWNED BY "{role}"'))
            conn.execute(text(f'DROP ROLE "{role}"'))
        upgrade_database.dispose()


def _migrate(f):
    command.upgrade(f.config, SCHEMA_REVISION)


def _predecessor(f, tmp_path, *, longest=False, root_f_sha=transition.F_SOURCE_SHA):
    """Synthetic committed original prefix; physical provenance belongs to H4."""
    old = replace(
        f.source, authority=replace(f.source.authority, image_digest="sha256:" + "0" * 64)
    )
    root = None
    if longest:
        earliest = replace(old, authority=replace(old.authority, image_digest="sha256:" + "a" * 64))
        root_path = tmp_path / "root-intent.json"
        root_path.write_text(
            json.dumps(
                dict(
                    f.body,
                    source_schema=transition.SOURCE_SCHEMA,
                    source_sha=transition.SOURCE_SHA,
                    target_sha=root_f_sha,
                    operation_id=str(uuid4()),
                    previous_id=None,
                )
            )
        )
        root_path.chmod(0o600)
        root = transition.read_intent(str(root_path), earliest, old)
    body = dict(
        f.body,
        source_schema=SCHEMA_REVISION,
        source_sha=transition.F_SOURCE_SHA,
        target_sha=transition.G_SOURCE_SHA,
        operation_id=str(uuid4()),
        previous_id=str(root.operation_id) if root else None,
    )
    path = tmp_path / "previous-intent.json"
    path.write_text(json.dumps(body))
    path.chmod(0o600)
    previous = transition.read_intent(str(path), old, f.source, predecessor=root)
    with f.engine.begin() as conn:
        for item in [root, previous] if root else [previous]:
            conn.execute(
                text("""
                INSERT INTO control_image_transitions
                  (operation_id,cluster_id,authority_epoch,previous_id,source_digest,target_digest,
                   source_schema,target_schema,intent_sha256,writer_xid,committed_at)
                VALUES (:id,:cluster,:epoch,:previous,:source,:target,:ss,:ts,:intent,
                        txid_current(),clock_timestamp())
            """),
                {
                    "id": item.operation_id,
                    "cluster": item.source.authority.cluster_id,
                    "epoch": item.source.authority_epoch,
                    "previous": item.previous_id,
                    "source": item.source.authority.image_digest,
                    "target": item.target.authority.image_digest,
                    "ss": item.source_schema,
                    "ts": item.target_schema,
                    "intent": item.intent_sha256,
                },
            )
    f.path.write_text(json.dumps(dict(f.body, previous_id=str(previous.operation_id))))
    intent = transition.read_intent(str(f.path), f.source, f.target, predecessor=previous)
    return previous, intent


@pytest.mark.parametrize("entry", ["parse", "check", "apply"])
def test_predecessor_source_digest_cannot_become_g_target(offline, tmp_path, entry):
    f = offline
    previous, intent = _predecessor(f, tmp_path)
    target = replace(
        f.target,
        authority=replace(f.target.authority, image_digest=previous.source.authority.image_digest),
    )
    instance, token = uuid4(), identity.new_instance_token()
    with f.factory.begin() as session:
        identity.register_control(
            session, f.source.authority, instance_id=instance, instance_token=token
        )
    with f.engine.begin() as conn:
        conn.execute(
            text("UPDATE control_process_instances SET lease_until=clock_timestamp()-interval '1s'")
        )

    def state():
        with f.engine.connect() as conn:
            return {
                name: conn.execute(text(f"SELECT row_to_json(t) FROM {name} t")).scalars().all()
                for name in (
                    "control_cluster_authority",
                    "control_image_transitions",
                    "control_process_instances",
                )
            }

    before = state()
    with pytest.raises(transition.TransitionUnavailable):
        if entry == "parse":
            transition.read_intent(str(f.path), f.source, target, predecessor=previous)
        else:
            # The writer also enforces the invariant for an already parsed
            # object; rejection must precede CAS, receipt append and revocation.
            bad = replace(intent, target=target)
            with f.factory() as session:
                getattr(transition, entry)(session, bad, settings)
                session.commit()
    assert state() == before


@pytest.mark.parametrize("unknown_commit", [False, True])
def test_exact_proved_predecessor_appends_one_child_and_reads_old_f_history(
    offline, tmp_path, unknown_commit
):
    f = offline
    previous, intent = _predecessor(f, tmp_path)
    with f.factory() as session:
        assert transition.check(session, intent, settings) == "ready_to_apply"
    with f.factory() as session:
        assert transition.observe(session, intent)["status"] == "not_found"

    class LostReply(Session):
        def commit(self):
            super().commit()
            raise OSError("fixture COMMIT response lost")

    factory = sessionmaker(f.engine, class_=LostReply) if unknown_commit else f.factory
    assert maintain(factory, "apply", intent)["status"] == "committed_current"
    with f.factory() as session:
        assert transition.observe(session, previous)["status"] == "committed_superseded"
    assert maintain(f.factory, "apply", previous) == {
        "status": "refused",
        "observed_status": "committed_superseded",
    }
    with f.factory() as session:
        assert transition.check(session, previous, settings) == "committed_superseded"
    assert maintain(f.factory, "apply", intent)["status"] == "committed_current"
    with f.factory() as session:
        assert session.scalar(text("SELECT count(*) FROM control_image_transitions")) == 2
        rows = session.execute(
            text("SELECT operation_id,previous_id,intent_sha256 FROM control_image_transitions")
        ).all()
        assert (previous.operation_id, None, previous.intent_sha256) in rows
        assert (intent.operation_id, previous.operation_id, intent.intent_sha256) in rows


@pytest.mark.parametrize(
    "bad_kind",
    [
        "missing_evidence",
        "digest",
        "previous_id",
        "slot_count",
        "unknown_source",
        "unknown_release",
        "already_has_successor",
        "aba",
    ],
)
def test_predecessor_evidence_and_current_chain_refuse_without_new_writes(
    offline, tmp_path, bad_kind
):
    f = offline
    previous, intent = _predecessor(f, tmp_path)
    if bad_kind == "missing_evidence":
        with pytest.raises(transition.TransitionUnavailable):
            transition.read_intent(str(f.path), f.source, f.target)
        return
    if bad_kind in {"digest", "previous_id", "slot_count", "unknown_source", "unknown_release"}:
        changes = {
            "digest": {"intent_sha256": "f" * 64},
            "previous_id": {"operation_id": uuid4()},
            "slot_count": {"control_slots": 1},
            "unknown_source": {"source_sha": "c" * 40},
            "unknown_release": {"target_sha": "c" * 40},
        }[bad_kind]
        bad_previous = replace(previous, **changes)
        # Parser refuses invalid provenance before opening a database, or the
        # stored receipt's exact digest rejects a changed historical intent.
        try:
            bad = transition.read_intent(str(f.path), f.source, f.target, predecessor=bad_previous)
        except transition.TransitionUnavailable:
            return
    else:
        assert maintain(f.factory, "apply", intent)["status"] == "committed_current"
        if bad_kind == "aba":
            with f.engine.begin() as conn:
                conn.execute(
                    text("UPDATE control_cluster_authority SET image_digest=:source"),
                    {"source": f.source.authority.image_digest},
                )
        bad = replace(intent, operation_id=uuid4())
    before = _snapshot(f)
    with f.factory() as session, pytest.raises(transition.TransitionUnavailable):
        transition.apply(session, bad, settings)
    assert _snapshot(f) == before


@pytest.mark.parametrize(
    "schema,source_sha,target_sha",
    [
        (transition.SOURCE_SCHEMA, transition.SOURCE_SHA, transition.F_SOURCE_SHA),
        (SCHEMA_REVISION, transition.F_SOURCE_SHA, transition.G_SOURCE_SHA),
    ],
)
def test_h_binary_cannot_create_old_release_pairs(
    offline, tmp_path, schema, source_sha, target_sha
):
    f = offline
    body = dict(f.body, source_schema=schema, source_sha=source_sha, target_sha=target_sha)
    f.path.write_text(json.dumps(body))
    historical = transition.read_intent(str(f.path), f.source, f.target)
    with f.factory() as session, pytest.raises(transition.TransitionUnavailable):
        transition.apply(session, historical, settings)


def _snapshot(f):
    with f.engine.connect() as conn:
        return conn.execute(text("SELECT * FROM control_cluster_authority")).one()


def _history(f):
    """Synthetic settled v4 history, distinct from the real three-host sample."""
    with f.factory.begin() as session:
        now = session.scalar(text("SELECT clock_timestamp()"))
        past = now - timedelta(minutes=5)
        worker = Worker(
            name="kept",
            status="offline",
            capabilities=["python"],
            protocol_version=4,
            identity_lifecycle="reserved",
            identity_enrolled_at=past,
            scheduling_state="disabled",
            capacity_state="ready",
        )
        session.add(worker)
        session.flush()
        session.add(
            WorkerIdentityReservation(
                request_id=uuid4(), worker_id=worker.id, name=worker.name, actor_kind="superadmin"
            )
        )
        instance = uuid4()
        worker.instance_id, worker.instance_generation, worker.instance_lease_until = (
            instance,
            1,
            past,
        )
        session.add(WorkerInstanceHistory(worker_id=worker.id, instance_id=instance, generation=1))
        session.flush()
        session.add(
            WorkerLeaseCandidateV4(
                worker_id=worker.id,
                worker_identity_id=worker.identity_id,
                instance_id=instance,
                instance_generation=1,
                revision=1,
                candidate_id=uuid4(),
                nominal_until=past,
                guard_until=past,
                proof_before=past,
                writer_xid=session.scalar(text("SELECT txid_current()")),
            )
        )
        adapter = Adapter(
            name="kept",
            language="python",
            adapter_type="task",
            run_mode="manual",
            runtime_worker_id=worker.id,
        )
        session.add(adapter)
        session.flush()
        version = AdapterVersion(adapter_id=adapter.id, seq=1, code="pass", requirements="")
        session.add(version)
        session.flush()
        execution = Execution(
            adapter_id=adapter.id,
            version_id=version.id,
            target_worker_id=worker.id,
            worker_id=worker.id,
            trigger="manual",
            status="succeeded",
            input={},
            admission_released_at=past,
        )
        session.add(execution)
        session.flush()
        attempt = ExecutionAttempt(
            execution_id=execution.id,
            adapter_id=adapter.id,
            attempt_no=1,
            worker_id=worker.id,
            worker_identity_id=worker.identity_id,
            worker_instance_id=instance,
            worker_instance_generation=1,
            fencing_token=1,
            lease_expires_at=past,
            # Normal production v4 Claim leaves this optional field unset.
            recovery_not_before=None,
            status="succeeded",
            claimed_at=past,
            ended_at=past,
            cleanup_settled_at=past,
            output_summary={"kept": True},
        )
        session.add(attempt)
        session.flush()
        return worker.id, execution.id, attempt.id, adapter.id


def _business_snapshot(f):
    with f.engine.connect() as conn:
        return {
            name: conn.execute(text(f"SELECT row_to_json(t) FROM {name} t ORDER BY 1::text"))
            .scalars()
            .all()
            for name in (
                "workers",
                "executions",
                "execution_attempts",
                "worker_lease_candidates_v4",
                "worker_identity_reservations",
                "worker_instance_history",
            )
        }


def test_same_schema_f_source_check_is_readonly_and_target_waits_for_cas(offline):
    f = offline
    before = _snapshot(f)
    with f.factory() as session:
        assert transition.check(session, f.intent, settings) == "ready_to_apply"
        assert session.scalar(text("SHOW transaction_read_only")) == "on"
    assert _snapshot(f) == before
    # Same schema does not authorize the G declaration before the transition.
    with f.factory() as session:
        identity._authority(session, f.source.authority)
    with f.factory() as session, pytest.raises(identity.ControlIdentityUnavailable):
        identity._authority(session, f.target.authority)
    with f.factory() as session:
        assert transition.observe(session, f.intent)["status"] == "not_found"
    assert maintain(f.factory, "apply", f.intent)["status"] == "committed_current"
    with f.factory() as session, pytest.raises(identity.ControlIdentityUnavailable):
        identity._authority(session, f.source.authority)


@pytest.mark.parametrize("committed", [False, True])
def test_commit_unknown_readback_and_idempotence_do_not_revoke_new_instance(offline, committed):
    f = offline
    _migrate(f)

    class LostReply(Session):
        def commit(self):
            if committed:
                super().commit()
            raise OSError("fixture COMMIT response lost")

    result = maintain(sessionmaker(f.engine, class_=LostReply), "apply", f.intent)
    assert result["status"] == ("committed_current" if committed else "not_found")
    if not committed:
        assert maintain(f.factory, "apply", f.intent)["status"] == "committed_current"
    instance, token = uuid4(), identity.new_instance_token()
    with f.factory.begin() as session:
        identity.register_control(
            session, f.target.authority, instance_id=instance, instance_token=token
        )
    # Runtime may be back up. A committed replay is strictly read-only on data.
    with f.factory() as session:
        transition.apply(session, f.intent, settings)
        session.commit()
    with f.factory() as session:
        assert (
            session.scalar(
                text("SELECT revoked FROM control_process_instances WHERE instance_id=:id"),
                {"id": instance},
            )
            is False
        )
        assert session.scalar(text("SELECT count(*) FROM control_image_transitions")) == 1
    with f.factory() as session, pytest.raises(identity.ControlIdentityUnavailable):
        identity.register_control(
            session,
            f.source.authority,
            instance_id=uuid4(),
            instance_token=identity.new_instance_token(),
        )


def test_uncommitted_receipt_is_not_visible_and_rollback_can_reenter(offline, monkeypatch):
    f = offline
    _migrate(f)
    with f.factory() as writer:
        transition.apply(writer, f.intent, settings)
        # Observe cooperates with the exclusive schema gate and fails closed.
        with f.factory() as reader, pytest.raises(Exception, match="schema gate unavailable"):
            transition.observe(reader, f.intent)
        writer.rollback()
    with f.factory() as reader:
        assert transition.observe(reader, f.intent)["status"] == "not_found"
    assert maintain(f.factory, "apply", f.intent)["status"] == "committed_current"


@pytest.mark.parametrize(
    "field",
    [
        "master_key",
        "admin_token",
        "worker_token",
        "managed_fingerprint",
        "builtin_fingerprint",
        "attachment_fingerprint",
        "capacity_fingerprint",
        "worker_proof_public_key",
        "cluster_id",
        "authority_epoch",
    ],
)
def test_changed_invariants_or_credentials_refuse_without_writes(offline, field):
    f = offline
    _migrate(f)
    changed = (
        uuid4()
        if field in {"cluster_id", "authority_epoch"}
        else (b"x" * 32 if field == "worker_proof_public_key" else "x" * 64)
    )
    bad_source = (
        replace(f.source, **{field: changed})
        if field == "authority_epoch"
        else replace(f.source, authority=replace(f.source.authority, **{field: changed}))
    )
    bad = replace(f.intent, source=bad_source)
    before = _snapshot(f)
    with f.factory() as session, pytest.raises(transition.TransitionUnavailable):
        transition.apply(session, bad, settings)
    assert _snapshot(f) == before


@pytest.mark.parametrize(
    "block",
    [
        "runtime_login",
        "same_owner_session",
        "other_session",
        "prepared",
        "legacy",
        "schedule",
        "pending_cleanup",
    ],
)
def test_offline_fences_and_unsettled_responsibilities_refuse(offline, block):
    f = offline
    _migrate(f)
    if block == "prepared":
        with f.engine.connect() as conn:
            if int(conn.scalar(text("SHOW max_prepared_transactions"))) == 0:
                pytest.skip("server requires max_prepared_transactions > 0 for this scenario")
    other = None
    gid = "w1f_" + uuid4().hex
    try:
        with f.engine.begin() as conn:
            if block == "runtime_login":
                conn.execute(text(f'ALTER ROLE "{f.role}" LOGIN'))
            elif block == "prepared":
                conn.execute(text("SELECT 1"))
                conn.execute(text(f"PREPARE TRANSACTION '{gid}'"))
            elif block in {"legacy", "schedule", "pending_cleanup"}:
                if block == "legacy":
                    conn.execute(
                        text(
                            "INSERT INTO workers(name,status,capabilities) "
                            "VALUES ('old','offline','[]')"
                        )
                    )
                elif block == "schedule":
                    adapter = conn.scalar(
                        text(
                            "INSERT INTO adapters(name,language,adapter_type) "
                            "VALUES ('scheduled','python','task') RETURNING id"
                        )
                    )
                    conn.execute(
                        text(
                            "INSERT INTO adapter_schedules(adapter_id,cron,timezone,enabled) "
                            "VALUES (:id,'* * * * *','UTC',true)"
                        ),
                        {"id": adapter},
                    )
                else:
                    worker = conn.scalar(
                        text(
                            "INSERT INTO workers(name,status,capabilities) "
                            "VALUES ('old','offline','[]') RETURNING id"
                        )
                    )
                    conn.execute(
                        text(
                            "INSERT INTO worker_cleanup_requests(worker_id,adapter_id) "
                            "VALUES (:id,7)"
                        ),
                        {"id": worker},
                    )
        if block in {"same_owner_session", "other_session"}:
            other = f.engine.connect()
            other.execute(text("SELECT 1"))
            if block == "other_session":
                other.execute(text(f'SET ROLE "{f.role}"'))
        before = _snapshot(f)
        with (
            f.factory() as session,
            pytest.raises((transition.TransitionUnavailable, RemoteDeploymentUnavailable)),
        ):
            transition.apply(session, f.intent, settings)
        assert _snapshot(f) == before
    finally:
        if other is not None:
            other.close()
        if block == "prepared":
            with f.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
                conn.execute(text(f"ROLLBACK PREPARED '{gid}'"))


def test_runtime_and_worker_no_receipt_access_and_owner_cannot_rewrite(offline):
    f = offline
    with f.engine.begin() as conn:
        conn.execute(
            text(
                "ALTER DEFAULT PRIVILEGES GRANT SELECT,INSERT,UPDATE,DELETE ON TABLES "
                f'TO "{f.role}"'
            )
        )
    _migrate(f)
    with f.engine.begin() as conn:
        for permission in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE"):
            assert (
                conn.scalar(
                    text(
                        "SELECT has_table_privilege(:role,'control_image_transitions',:permission)"
                    ),
                    {"role": f.role, "permission": permission},
                )
                is False
            )
        # Even an accidental inherited/default INSERT grant cannot bypass owner semantics.
        conn.execute(text(f'GRANT INSERT ON control_image_transitions TO "{f.role}"'))
    with f.factory() as session:
        session.execute(text(f'SET ROLE "{f.role}"'))
        with pytest.raises(transition.TransitionUnavailable):
            transition.apply(session, f.intent, settings)  # Nested/SET ROLE is rejected.
    assert maintain(f.factory, "apply", f.intent)["status"] == "committed_current"
    for statement in (
        "UPDATE control_image_transitions SET writer_xid=writer_xid+1",
        "DELETE FROM control_image_transitions",
    ):
        with (
            f.engine.begin() as conn,
            pytest.raises(DBAPIError, match="Image transition unavailable"),
        ):
            conn.execute(text(statement))
    with pytest.raises(DBAPIError, match="Image transition prevents downgrade"):
        command.downgrade(f.config, transition.SOURCE_SCHEMA)


@pytest.mark.parametrize("change", ["slots", "default_v3"])
def test_changed_slot_count_and_default_v3_refuse_without_writes(offline, monkeypatch, change):
    f = offline
    _migrate(f)
    monkeypatch.setattr(
        settings,
        "worker_v4_control_slots" if change == "slots" else "worker_remote_v4_enabled",
        1 if change == "slots" else False,
    )
    before = _snapshot(f)
    for operation in (transition.check, transition.apply):
        with f.factory() as session, pytest.raises(transition.TransitionUnavailable):
            operation(session, f.intent, settings)
    assert _snapshot(f) == before


def test_same_id_conflict_new_id_stale_predecessor_and_superseded_receipt(offline):
    f = offline
    _migrate(f)
    assert maintain(f.factory, "apply", f.intent)["status"] == "committed_current"
    for bad in (replace(f.intent, intent_sha256="f" * 64), replace(f.intent, operation_id=uuid4())):
        before = _snapshot(f)
        with f.factory() as session, pytest.raises(transition.TransitionUnavailable):
            transition.apply(session, bad, settings)
        assert _snapshot(f) == before
    # A future transition fixture takes B back to A at the same schema. The old
    # receipt is historical only, and an original request cannot exploit ABA.
    next_id = uuid4()
    with f.engine.begin() as conn:
        conn.execute(
            text("""
            INSERT INTO control_image_transitions
            SELECT :id,cluster_id,authority_epoch,operation_id,target_digest,source_digest,
                   target_schema,target_schema,:intent,txid_current(),clock_timestamp()
            FROM control_image_transitions WHERE operation_id=:previous
        """),
            {"id": next_id, "previous": f.intent.operation_id, "intent": "9" * 64},
        )
        conn.execute(
            text("UPDATE control_cluster_authority SET image_digest=:digest"),
            {"digest": f.source.authority.image_digest},
        )
    with f.factory() as session, pytest.raises(transition.TransitionUnavailable):
        transition.observe(session, f.intent)
    with f.factory() as session, pytest.raises(transition.TransitionUnavailable):
        transition.apply(session, replace(f.intent, operation_id=uuid4()), settings)


@pytest.mark.parametrize(
    "change",
    [
        "unknown_field",
        "duplicate",
        "permissions",
        "symlink",
        "too_large",
        "unsupported_pair",
        "target_change",
    ],
)
def test_private_closed_intent_rejects_invalid_inputs(offline, tmp_path, change):
    f = offline
    body = dict(f.body)
    if change == "unknown_field":
        body["extra"] = True
    elif change == "unsupported_pair":
        body["source_sha"] = "c" * 40
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(body))
    path.chmod(0o600)
    if change == "duplicate":
        path.write_text('{"operation_id":"duplicate","operation_id":"duplicate"}')
    elif change == "permissions":
        path.chmod(0o644)
    elif change == "symlink":
        link = tmp_path / "link.json"
        link.symlink_to(path)
        path = link
    elif change == "too_large":
        path.write_text(" " * 16385)
    target = (
        replace(f.target, authority=replace(f.target.authority, capacity_fingerprint="7" * 64))
        if change == "target_change"
        else f.target
    )
    with pytest.raises(transition.TransitionUnavailable):
        transition.read_intent(str(path), f.source, target)
    if change in {"duplicate", "permissions", "symlink", "too_large"}:
        with pytest.raises(Exception, match="control declaration unavailable"):
            read_private_json(str(path))


@pytest.mark.parametrize("slots", [1, 2])
def test_settled_history_and_identity_preserved_for_original_slot_configuration(
    offline, monkeypatch, slots
):
    f = offline
    monkeypatch.setattr(settings, "worker_v4_control_slots", slots)
    f.path.write_text(json.dumps(f.body | {"control_slots": slots}))
    f.intent = transition.read_intent(str(f.path), f.source, f.target)
    _history(f)
    before = _business_snapshot(f)
    _migrate(f)
    assert _business_snapshot(f) == before
    assert maintain(f.factory, "apply", f.intent)["status"] == "committed_current"
    assert _business_snapshot(f) == before


@pytest.mark.parametrize(
    "block",
    [
        "queued",
        "running",
        "retry_wait",
        "cleanup_unknown",
        "worker_lease",
        "worker_guard",
        "nominal_lease",
        "counter",
        "cleanup_request",
        "recovery_guard",
    ],
)
def test_pending_business_and_signed_boundaries_refuse_without_changing_history(offline, block):
    f = offline
    worker, execution, attempt, adapter = _history(f)
    _migrate(f)
    with f.engine.begin() as conn:
        if block in {"queued", "running", "retry_wait"}:
            conn.execute(
                text("UPDATE executions SET status=:status WHERE id=:id"),
                {"status": block, "id": execution},
            )
        elif block == "cleanup_unknown":
            conn.execute(
                text("UPDATE execution_attempts SET cleanup_settled_at=NULL WHERE id=:id"),
                {"id": attempt},
            )
        elif block == "recovery_guard":
            conn.execute(
                text(
                    "UPDATE execution_attempts SET recovery_not_before="
                    "clock_timestamp()+interval '1 minute' WHERE id=:id"
                ),
                {"id": attempt},
            )
        elif block == "worker_lease":
            conn.execute(
                text(
                    "UPDATE workers SET instance_lease_until=clock_timestamp()+interval '1 minute' "
                    "WHERE id=:id"
                ),
                {"id": worker},
            )
        elif block in {"worker_guard", "nominal_lease"}:
            column = "guard_until" if block == "worker_guard" else "nominal_until"
            conn.execute(
                text(
                    f"UPDATE worker_lease_candidates_v4 SET writer_xid=txid_current(),{column}="
                    "clock_timestamp()+interval '1 minute' WHERE worker_id=:id"
                ),
                {"id": worker},
            )
        elif block == "counter":
            conn.execute(
                text(
                    "INSERT INTO adapter_execution_admission"
                    "(adapter_id,outstanding_count,outstanding_bytes) VALUES (:id,1,1) "
                    "ON CONFLICT (adapter_id) DO UPDATE SET outstanding_count=1,outstanding_bytes=1"
                ),
                {"id": adapter},
            )
        else:
            conn.execute(
                text("INSERT INTO worker_cleanup_requests(worker_id,adapter_id) VALUES (:id,7)"),
                {"id": worker},
            )
    before = _business_snapshot(f)
    with f.factory() as session, pytest.raises(transition.TransitionUnavailable, match="unsettled"):
        transition.apply(session, f.intent, settings)
    assert _business_snapshot(f) == before


def test_live_control_lease_blocks_then_expired_instance_is_permanently_revoked(
    offline, monkeypatch
):
    f = offline
    instance, token = uuid4(), identity.new_instance_token()
    monkeypatch.setattr(identity, "SCHEMA_REVISION", f.intent.source_schema)
    with f.factory.begin() as session:
        identity.register_control(
            session, f.source.authority, instance_id=instance, instance_token=token
        )
    _migrate(f)
    with f.factory() as session, pytest.raises(transition.TransitionUnavailable, match="unsettled"):
        transition.apply(session, f.intent, settings)
    # Test fixture time boundary; the deployment sample waits for actual expiry.
    with f.engine.begin() as conn:
        conn.execute(
            text("UPDATE control_process_instances SET lease_until=clock_timestamp()-interval '1s'")
        )
    assert maintain(f.factory, "apply", f.intent)["status"] == "committed_current"
    monkeypatch.setattr(identity, "SCHEMA_REVISION", SCHEMA_REVISION)
    with f.factory.begin() as session, pytest.raises(identity.ControlIdentityUnavailable):
        identity.renew_control(
            session, f.target.authority, instance_id=instance, instance_token=token
        )
    with f.engine.connect() as conn:
        assert conn.scalar(text("SELECT revoked FROM control_process_instances")) is True


def test_runtime_login_identity_cannot_apply_even_with_application_read_grants(offline):
    f = offline
    _migrate(f)
    with f.engine.begin() as conn:
        conn.execute(
            text(f'GRANT SELECT ON alembic_version,control_cluster_authority TO "{f.role}"')
        )
    runtime = create_engine(f.engine.url, poolclass=NullPool)

    @event.listens_for(runtime, "connect")
    def runtime_session(dbapi, _):
        old = dbapi.autocommit
        dbapi.autocommit = True
        with dbapi.cursor() as cursor:
            cursor.execute(f'SET SESSION AUTHORIZATION "{f.role}"')
        dbapi.autocommit = old

    before = _snapshot(f)
    try:
        with (
            Session(runtime) as session,
            pytest.raises(transition.TransitionUnavailable, match="owner"),
        ):
            transition.apply(session, f.intent, settings)
    finally:
        runtime.dispose()
    assert _snapshot(f) == before


@pytest.mark.parametrize("has_predecessor", [False, True])
def test_competing_operation_waits_for_exclusive_gate_then_cannot_fork(
    offline, tmp_path, has_predecessor
):
    f = offline
    if has_predecessor:
        _previous, f.intent = _predecessor(f, tmp_path)
    _migrate(f)
    waiting = Event()
    competitor = replace(f.intent, operation_id=uuid4())
    other_engine = create_engine(f.engine.url, poolclass=NullPool)

    @event.listens_for(other_engine, "before_cursor_execute")
    def wait_observed(_conn, _cursor, statement, _params, _context, _many):
        if "pg_try_advisory_xact_lock(" in statement:
            waiting.set()

    def compete():
        with Session(other_engine) as session, pytest.raises(transition.TransitionUnavailable):
            transition.apply(session, competitor, settings)

    try:
        with f.factory() as first, ThreadPoolExecutor(max_workers=1) as executor:
            transition.apply(first, f.intent, settings)
            future = executor.submit(compete)
            assert waiting.wait(2)
            first.commit()
            future.result(timeout=7)
        with f.factory() as session:
            assert transition.observe(session, f.intent)["status"] == "committed_current"
        with f.factory() as session:
            with pytest.raises(transition.TransitionUnavailable):
                transition.observe(session, competitor)
            assert session.scalar(text("SELECT count(*) FROM control_image_transitions")) == (
                2 if has_predecessor else 1
            )
    finally:
        other_engine.dispose()


@pytest.mark.parametrize("unknown_commit", [False, True])
def test_longest_chain_appends_third_preserves_both_original_hashes_and_readonly_history(
    offline, tmp_path, unknown_commit
):
    f = offline
    previous, intent = _predecessor(f, tmp_path, longest=True)
    root = previous.predecessor
    before = [(item.operation_id, item.intent_sha256) for item in (root, previous)]

    class LostReply(Session):
        def commit(self):
            super().commit()
            raise OSError("fixture lost COMMIT response")

    with f.factory() as session:
        assert transition.check(session, intent, settings) == "ready_to_apply"
    factory = sessionmaker(f.engine, class_=LostReply) if unknown_commit else f.factory
    assert maintain(factory, "apply", intent)["status"] == "committed_current"
    for item in (root, previous):
        with f.factory() as session:
            assert transition.observe(session, item)["status"] == "committed_superseded"
        assert maintain(f.factory, "apply", item)["status"] == "refused"
    with f.factory() as session:
        rows = session.execute(
            text("SELECT operation_id,intent_sha256 FROM control_image_transitions")
        ).all()
        assert len(rows) == 3 and all(row in rows for row in before)


@pytest.mark.parametrize("ancestor", ["e", "f", "g"])
@pytest.mark.parametrize("entry", ["parse", "check", "apply"])
def test_longest_chain_refuses_every_ancestor_digest_before_write(
    offline, tmp_path, ancestor, entry
):
    f = offline
    previous, intent = _predecessor(f, tmp_path, longest=True)
    digest = {
        "e": previous.predecessor.source.authority.image_digest,
        "f": previous.source.authority.image_digest,
        "g": previous.target.authority.image_digest,
    }[ancestor]
    target = replace(f.target, authority=replace(f.target.authority, image_digest=digest))
    before = _snapshot(f)
    with pytest.raises(transition.TransitionUnavailable):
        if entry == "parse":
            transition.read_intent(str(f.path), f.source, target, predecessor=previous)
        else:
            with f.factory() as session:
                getattr(transition, entry)(session, replace(intent, target=target), settings)
                session.commit()
    assert _snapshot(f) == before
    with f.factory() as session:
        assert session.scalar(text("SELECT count(*) FROM control_image_transitions")) == 2


def test_fourth_receipt_is_never_observable_as_supported_history(offline, tmp_path):
    f = offline
    previous, intent = _predecessor(f, tmp_path, longest=True)
    assert maintain(f.factory, "apply", intent)["status"] == "committed_current"
    with f.engine.begin() as conn:
        conn.execute(
            text("""INSERT INTO control_image_transitions
            SELECT :id,cluster_id,authority_epoch,operation_id,target_digest,:target,
                target_schema,target_schema,:hash,txid_current(),clock_timestamp()
            FROM control_image_transitions WHERE operation_id=:previous"""),
            {
                "id": uuid4(),
                "target": "sha256:" + "d" * 64,
                "hash": "f" * 64,
                "previous": intent.operation_id,
            },
        )
    for item in (previous.predecessor, previous, intent):
        with f.factory() as session, pytest.raises(transition.TransitionUnavailable):
            transition.observe(session, item)


def test_original_verified_f_aliases_need_no_historical_intent_rewrite(offline, tmp_path):
    f = offline
    previous, intent = _predecessor(
        f, tmp_path, longest=True, root_f_sha="49269698f63cd85128f16f60c0ad2ba684d55902"
    )
    assert previous.predecessor.target_sha != previous.source_sha
    with f.factory() as session:
        assert transition.check(session, intent, settings) == "ready_to_apply"
    assert maintain(f.factory, "apply", intent)["status"] == "committed_current"
    with f.factory() as session:
        assert transition.observe(session, previous.predecessor)["status"] == "committed_superseded"
