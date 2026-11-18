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
from dlr.control.services import control_proof_rotation_v4 as rotation
from dlr.control.services import worker_legacy_window
from dlr.control.services.control_declaration_v4 import DeploymentDeclaration, read_private_json
from dlr.control.services.worker_lease_proof_v4 import public_key_bytes
from dlr.control.services.worker_remote_deployment_v4 import RemoteDeploymentUnavailable
from test_issue219_upgrade import migration_config, upgrade_database  # noqa: F401


@pytest.fixture
def offline(upgrade_database, tmp_path, monkeypatch):  # noqa: F811
    config = migration_config(upgrade_database)
    command.upgrade(config, transition.LEGACY_SCHEMA)
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
    monkeypatch.setattr(identity, "SCHEMA_REVISION", transition.J_SCHEMA)
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
                "schema": transition.LEGACY_SCHEMA,
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
        "source_schema": transition.LEGACY_SCHEMA,
        "target_schema": transition.J_SCHEMA,
        "source_sha": transition.I_SOURCE_SHA,
        "target_sha": transition.J_SOURCE_SHA,
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


def _register_source_control(session, declaration, **kwargs):
    # Simulate the real I source build; J must not start against schema 0088.
    with pytest.MonkeyPatch.context() as source_build:
        source_build.setattr(identity, "SCHEMA_REVISION", transition.LEGACY_SCHEMA)
        return identity.register_control(session, declaration, **kwargs)


def _migrate(f):
    assert f.intent.source_schema == transition.LEGACY_SCHEMA


@pytest.mark.parametrize("source_sha", sorted(transition._I_RELEASE_SHAS))
def test_only_the_three_verified_i_release_sources_can_start_j(offline, source_sha):
    f = offline
    f.path.write_text(json.dumps(dict(f.body, source_sha=source_sha)))
    candidate = transition.read_intent(str(f.path), f.source, f.target)
    assert transition._kind(candidate) == 4
    assert maintain(f.factory, "apply", candidate)["status"] == "committed_current"


@pytest.mark.parametrize("with_history", [False, True])
def test_j_cutover_preserves_prior_receipts_and_moves_the_authority(
    offline, tmp_path, with_history
):
    f = offline
    previous = None
    if with_history:
        _, previous = _predecessor(f, tmp_path, longest=True)
        assert maintain(f.factory, "apply", previous)["status"] == "committed_current"
    else:
        command.upgrade(f.config, transition.J_SCHEMA)
        with f.engine.begin() as conn:
            conn.execute(
                text("""
                UPDATE control_cluster_authority SET schema_revision=:schema,image_digest=:image
                """),
                {"schema": transition.J_SCHEMA, "image": f.target.authority.image_digest},
            )
    target = replace(
        f.target, authority=replace(f.target.authority, image_digest="sha256:" + "f" * 64)
    )
    path = tmp_path / "cutover.json"
    path.write_text(
        json.dumps(
            dict(
                f.body,
                operation_id=str(uuid4()),
                previous_id=str(previous.operation_id) if previous else None,
                source_schema=transition.J_SCHEMA,
                target_schema=SCHEMA_REVISION,
                source_sha=transition.J_SOURCE_SHA,
                target_sha="7" * 40,
            )
        )
    )
    path.chmod(0o600)
    intent = transition.read_intent(str(path), f.target, target, predecessor=previous)
    assert transition._kind(intent) == 5
    with f.factory() as session:
        assert transition.check(session, intent, settings) == "source_checked"
    assert maintain(f.factory, "apply", intent)["status"] == "committed_current"
    assert maintain(f.factory, "apply", intent)["status"] == "committed_current"
    with f.factory() as session:
        assert session.scalar(text("SELECT version_num FROM alembic_version")) == SCHEMA_REVISION
        assert session.scalar(text("SELECT count(*) FROM control_image_transitions")) == (
            6 if previous else 1
        )
        storage_role_rights = session.execute(
            text("""
                SELECT has_table_privilege(:role, 'worker_adapter_storage_owners_v4', 'SELECT'),
                       has_table_privilege(:role, 'worker_adapter_storage_owners_v4', 'INSERT'),
                       has_table_privilege(:role, 'worker_adapter_storage_owners_v4', 'UPDATE'),
                       has_table_privilege(:role, 'worker_adapter_storage_owners_v4', 'DELETE')
            """),
            {"role": f.role},
        ).one()
        assert tuple(storage_role_rights) == (True, True, False, False)
    if previous is not None:
        with f.factory() as session:
            assert transition.observe(session, previous)["status"] == "committed_superseded"


def test_control_proof_rotation_preserves_initial_epoch_and_fences_stale_declarations(
    offline, tmp_path, monkeypatch
):
    f = offline
    command.upgrade(f.config, SCHEMA_REVISION)
    with f.engine.begin() as conn:
        conn.execute(
            text("UPDATE control_cluster_authority SET schema_revision=:schema"),
            {"schema": SCHEMA_REVISION},
        )
    monkeypatch.setattr(identity, "SCHEMA_REVISION", SCHEMA_REVISION)
    key = b"x" * 32
    target = replace(
        f.source,
        authority=replace(f.source.authority, worker_proof_public_key=public_key_bytes(key)),
        authority_epoch=uuid4(),
        worker_proof_signing_key=key,
    )
    path = tmp_path / "rotation.json"
    path.write_text(
        json.dumps(
            {"operation_id": str(uuid4()), "image_transition_id": None, "evidence_sha256": "e" * 64}
        )
    )
    path.chmod(0o600)
    intent = rotation.read_intent(str(path), f.source, target)
    with f.factory() as owner:
        rotation.apply(owner, intent, settings)
        owner.commit()
    with f.factory() as reader:
        assert rotation.observe(reader, intent)["target_epoch"] == str(target.authority_epoch)
    with f.factory() as reader:
        assert identity._authority(reader, target.authority)["authority_epoch"] == (
            target.authority_epoch
        )
        assert reader.scalar(text("SELECT authority_epoch FROM control_cluster_authority")) == (
            f.source.authority_epoch
        )
    with f.factory() as reader, pytest.raises(identity.ControlIdentityUnavailable):
        identity._authority(reader, f.source.authority)
    with f.factory() as owner:
        rotation.apply(owner, intent, settings)
        owner.commit()
        assert owner.scalar(text("SELECT count(*) FROM control_proof_rotations")) == 1


def test_legacy_window_uses_one_deadline_and_preserves_workers(offline, monkeypatch):
    f = offline
    command.upgrade(f.config, SCHEMA_REVISION)
    monkeypatch.setattr(settings, "worker_v4_control_slots", 1)
    with f.factory.begin() as session:
        worker = Worker(
            name="legacy-fixed",
            status="offline",
            protocol_version=3,
            capabilities=["python"],
        )
        session.add(worker)
        session.flush()
        worker_id, identity_id = worker.id, worker.identity_id
    with f.factory() as fresh:
        now = worker_legacy_window.current_time(fresh)
        with pytest.raises(worker_legacy_window.LegacyWindowUnavailable):
            worker_legacy_window.open_once(fresh, now + timedelta(days=1))
    monkeypatch.setattr(settings, "worker_v4_deployment_mode", "A")
    with f.factory() as fresh:
        with pytest.raises(worker_legacy_window.LegacyWindowUnavailable):
            worker_legacy_window.gate(fresh)
        with pytest.raises(worker_legacy_window.LegacyWindowUnavailable):
            worker_legacy_window.open_once(fresh, now + timedelta(days=8))
    deadline = now + timedelta(days=1)
    with f.factory.begin() as owner:
        worker_legacy_window.open_once(owner, deadline)
        owner.execute(text(f'GRANT SELECT, UPDATE (id) ON worker_legacy_window TO "{f.role}"'))
    with f.factory() as runtime:
        runtime.execute(text(f'SET LOCAL ROLE "{f.role}"'))
        assert worker_legacy_window.gate(runtime) == deadline
        with pytest.raises(DBAPIError):
            runtime.execute(text("UPDATE worker_legacy_window SET id=1 WHERE id=1"))
    with f.factory() as reopened:
        assert worker_legacy_window.gate(reopened) == deadline
        row = reopened.get(Worker, worker_id)
        assert worker_legacy_window.target_allowed(reopened, row)
        row.name = "write-crossed-deadline"
        with monkeypatch.context() as crossing:
            crossing.setattr(worker_legacy_window, "current_time", lambda _: deadline)
            with pytest.raises(worker_legacy_window.LegacyWindowUnavailable):
                reopened.commit()
        reopened.rollback()
    for incompatible_mode in ("B", "C"):
        with monkeypatch.context() as incompatible:
            incompatible.setattr(settings, "worker_v4_deployment_mode", incompatible_mode)
            with f.factory() as reader, pytest.raises(worker_legacy_window.LegacyWindowUnavailable):
                worker_legacy_window.gate(reader)
    with f.factory.begin() as owner:
        worker_legacy_window.close(owner)
    with f.factory() as closed:
        assert worker_legacy_window.has_history(closed)
        assert closed.get(Worker, worker_id).identity_id == identity_id
        assert closed.get(Worker, worker_id).name == "legacy-fixed"
        with pytest.raises(worker_legacy_window.LegacyWindowUnavailable):
            worker_legacy_window.open_once(closed, deadline)
        with pytest.raises(worker_legacy_window.LegacyWindowUnavailable):
            worker_legacy_window.gate(closed)
        with pytest.raises(DBAPIError):
            closed.execute(text("UPDATE worker_legacy_window SET closed_at=NULL WHERE id=1"))


def test_cli_reads_four_original_private_predecessor_sets(offline, tmp_path, monkeypatch, capsys):
    from dlr.control import control_identity_init_v4 as cli

    f = offline
    previous, _ = _predecessor(f, tmp_path, longest=True)
    declarations = {
        "source": f.source,
        "target": f.target,
        "previous": previous.source,
        "root": previous.predecessor.source,
        "origin": previous.predecessor.predecessor.source,
        "ancestor": previous.predecessor.predecessor.predecessor.source,
    }
    monkeypatch.setattr(
        cli, "read_declaration", lambda config: declarations[config.control_identity_file]
    )
    monkeypatch.setattr(cli, "create_engine", lambda *_a, **_k: f.engine)
    args = ["check", "--source", "source", "--target", "target", "--intent", str(f.path)]
    for name in ("previous", "root", "origin", "ancestor"):
        args.extend(
            [f"--{name}-source", name, f"--{name}-intent", str(tmp_path / f"{name}-intent.json")]
        )
    assert cli._maintenance_main(args) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "source_checked"


def _predecessor(f, tmp_path, *, longest=False, root_f_sha=transition.F_SOURCE_SHA):
    """Synthetic original EF/FG/GH receipts; physical provenance belongs to I4."""
    h = replace(f.source, authority=replace(f.source.authority, image_digest="sha256:" + "7" * 64))
    g = replace(f.source, authority=replace(f.source.authority, image_digest="sha256:" + "0" * 64))
    prefix = []

    def original(label, source, target, schema, source_sha, target_sha):
        path = tmp_path / f"{label}-intent.json"
        path.write_text(
            json.dumps(
                dict(
                    f.body,
                    source_schema=schema,
                    target_schema=transition.LEGACY_SCHEMA,
                    source_sha=source_sha,
                    target_sha=target_sha,
                    operation_id=str(uuid4()),
                    previous_id=str(prefix[-1].operation_id) if prefix else None,
                )
            )
        )
        path.chmod(0o600)
        item = transition.read_intent(
            str(path), source, target, predecessor=prefix[-1] if prefix else None
        )
        prefix.append(item)
        return item

    if longest:
        e = replace(g, authority=replace(g.authority, image_digest="sha256:" + "9" * 64))
        old_f = replace(g, authority=replace(g.authority, image_digest="sha256:" + "a" * 64))
        original("ancestor", e, old_f, transition.SOURCE_SCHEMA, transition.SOURCE_SHA, root_f_sha)
        original(
            "origin",
            old_f,
            g,
            transition.LEGACY_SCHEMA,
            transition.F_SOURCE_SHA,
            transition.G_SOURCE_SHA,
        )
        original(
            "root", g, h, transition.LEGACY_SCHEMA, transition.G_SOURCE_SHA, transition.H_SOURCE_SHA
        )
    previous = original(
        "previous",
        h,
        f.source,
        transition.LEGACY_SCHEMA,
        transition.H_SOURCE_SHA,
        transition.I_SOURCE_SHA,
    )
    with f.engine.begin() as conn:
        for item in prefix:
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
        _register_source_control(
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
        assert transition.check(session, intent, settings) == "source_checked"
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
        (transition.LEGACY_SCHEMA, transition.F_SOURCE_SHA, transition.G_SOURCE_SHA),
        (transition.LEGACY_SCHEMA, transition.G_SOURCE_SHA, transition.H_SOURCE_SHA),
    ],
)
def test_i_binary_cannot_create_old_release_pairs(
    offline, tmp_path, schema, source_sha, target_sha
):
    f = offline
    body = dict(
        f.body,
        source_schema=schema,
        target_schema=transition.LEGACY_SCHEMA,
        source_sha=source_sha,
        target_sha=target_sha,
    )
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


def test_same_schema_h_source_check_is_readonly_and_target_waits_for_cas(offline):
    f = offline
    before = _snapshot(f)
    with f.factory() as session:
        assert transition.check(session, f.intent, settings) == "source_checked"
        assert session.scalar(text("SHOW transaction_read_only")) == "on"
    assert _snapshot(f) == before
    # Same schema does not authorize the I declaration before the transition.
    with f.factory() as session, pytest.MonkeyPatch.context() as source_build:
        source_build.setattr(identity, "SCHEMA_REVISION", transition.LEGACY_SCHEMA)
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
    with f.engine.connect() as catalogue:
        expected_schema = transition.J_SCHEMA if committed else transition.LEGACY_SCHEMA
        assert catalogue.scalar(text("SELECT version_num FROM alembic_version")) == expected_schema
        assert (
            catalogue.scalar(text("SELECT schema_revision FROM control_cluster_authority"))
            == expected_schema
        )
        assert catalogue.scalar(
            text(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_name='worker_credential_audit' AND column_name='request_id'"
            )
        ) == int(committed)
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
    with pytest.raises(RuntimeError, match="HTTP credential receipts require forward repair"):
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
        _register_source_control(
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
    monkeypatch.setattr(identity, "SCHEMA_REVISION", transition.J_SCHEMA)
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
def test_longest_chain_appends_fifth_preserves_original_hashes_and_readonly_history(
    offline, tmp_path, unknown_commit
):
    f = offline
    previous, intent = _predecessor(f, tmp_path, longest=True)
    root = previous.predecessor.predecessor.predecessor
    history = (root, previous.predecessor.predecessor, previous.predecessor, previous)
    before = [(item.operation_id, item.intent_sha256) for item in history]

    class LostReply(Session):
        def commit(self):
            super().commit()
            raise OSError("fixture lost COMMIT response")

    with f.factory() as session:
        assert transition.check(session, intent, settings) == "source_checked"
    factory = sessionmaker(f.engine, class_=LostReply) if unknown_commit else f.factory
    assert maintain(factory, "apply", intent)["status"] == "committed_current"
    for item in history:
        with f.factory() as session:
            assert transition.observe(session, item)["status"] == "committed_superseded"
        assert maintain(f.factory, "apply", item)["status"] == "refused"
    with f.factory() as session:
        rows = session.execute(
            text("SELECT operation_id,intent_sha256 FROM control_image_transitions")
        ).all()
        assert len(rows) == 5 and all(row in rows for row in before)


@pytest.mark.parametrize("ancestor", ["e", "f", "g", "h", "i"])
@pytest.mark.parametrize("entry", ["parse", "check", "apply"])
def test_longest_chain_refuses_every_ancestor_digest_before_write(
    offline, tmp_path, ancestor, entry
):
    f = offline
    previous, intent = _predecessor(f, tmp_path, longest=True)
    digest = {
        "e": previous.predecessor.predecessor.predecessor.source.authority.image_digest,
        "f": previous.predecessor.predecessor.source.authority.image_digest,
        "g": previous.predecessor.source.authority.image_digest,
        "h": previous.source.authority.image_digest,
        "i": previous.target.authority.image_digest,
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
        assert session.scalar(text("SELECT count(*) FROM control_image_transitions")) == 4


def test_sixth_receipt_is_never_observable_as_supported_history(offline, tmp_path):
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
    for item in (
        previous.predecessor.predecessor.predecessor,
        previous.predecessor.predecessor,
        previous.predecessor,
        previous,
        intent,
    ):
        with f.factory() as session, pytest.raises(transition.TransitionUnavailable):
            transition.observe(session, item)


def test_original_verified_f_aliases_need_no_historical_intent_rewrite(offline, tmp_path):
    f = offline
    previous, intent = _predecessor(
        f, tmp_path, longest=True, root_f_sha="49269698f63cd85128f16f60c0ad2ba684d55902"
    )
    assert (
        previous.predecessor.predecessor.predecessor.target_sha
        != previous.predecessor.predecessor.source_sha
    )
    with f.factory() as session:
        assert transition.check(session, intent, settings) == "source_checked"
    assert maintain(f.factory, "apply", intent)["status"] == "committed_current"
    with f.factory() as session:
        assert (
            transition.observe(session, previous.predecessor.predecessor)["status"]
            == "committed_superseded"
        )


def test_j_ddl_failure_rolls_back_catalogue_authority_and_receipt(offline, monkeypatch):
    f = offline
    migrate = transition._migrate

    def interrupted(session, revision):
        migrate(session, revision)
        raise OSError("fixture failure after transactional DDL")

    monkeypatch.setattr(transition, "_migrate", interrupted)
    before = _snapshot(f)
    assert maintain(f.factory, "apply", f.intent)["status"] == "not_found"
    assert _snapshot(f) == before
    with f.factory() as session:
        assert (
            session.scalar(text("SELECT version_num FROM alembic_version"))
            == transition.LEGACY_SCHEMA
        )
        assert (
            session.scalar(
                text(
                    "SELECT count(*) FROM information_schema.columns "
                    "WHERE table_name='worker_credential_audit' AND column_name='request_id'"
                )
            )
            == 0
        )
        assert session.scalar(text("SELECT count(*) FROM control_image_transitions")) == 0
