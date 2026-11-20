"""Namespace identity and physical lock boundaries for the shared file backend."""

import base64
import hashlib
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import local
from uuid import uuid4

import pytest
from sqlalchemy import text

from dlr.common.config import settings
from dlr.control import storage_migration
from dlr.control.ai import shared_attachments
from dlr.control.ai.attachment_documents import DocumentConversionError, DocumentResult
from dlr.control.schemas.ai import AiAttachment, AiAttachmentReference
from dlr.control.services import (
    builtin_package,
    control_ha_capacity,
    shared_posix,
    storage_backends,
)
from dlr.control.services.artifact_store import ArtifactStoreBusyError
from dlr.control.services.shared_artifact_store import SharedFileArtifactStore
from dlr.control.services.shared_posix import SharedNamespace, SharedStorageUnavailable
from dlr.control.storage_migration import _assert_absent, _check_attachment_selection, _copy_one


@pytest.fixture
def verified_namespace(tmp_path, monkeypatch):
    return verified_mount(tmp_path, monkeypatch, "managed_files")


def verified_mount(tmp_path, monkeypatch, purpose):
    mounted = tmp_path.resolve() / "mount"
    mounted.mkdir(mode=0o700)
    mount = SharedNamespace(mounted, purpose, uuid4(), uuid4())
    mount.initialize()
    boot_a, boot_b = uuid4(), uuid4()
    thread = local()
    monkeypatch.setattr(shared_posix, "linux_boot_id", lambda: getattr(thread, "boot", boot_a))
    challenge = uuid4()

    def answer():
        thread.boot = boot_b
        shared_posix.prove(mount, "b", challenge, 4)

    with ThreadPoolExecutor(max_workers=1) as pool:
        partner = pool.submit(answer)
        shared_posix.prove(mount, "a", challenge, 4)
        partner.result(timeout=4)
    return mount


def test_marker_and_proof_copy_cannot_authorize_another_mount(verified_namespace, tmp_path):
    original = verified_namespace
    clone = tmp_path.resolve() / "separate"
    shutil.copytree(original.root, clone)
    candidate = SharedNamespace(
        clone, original.purpose, original.deployment_id, original.namespace_id
    )
    with pytest.raises(SharedStorageUnavailable), candidate.open(slot="a"):
        pytest.fail("copied proof authorized different storage")


def test_namespace_rejects_other_purpose_and_empty_mount(verified_namespace, tmp_path):
    original = verified_namespace
    unrelated = SharedNamespace(
        original.root, "ai_attachments", original.deployment_id, original.namespace_id
    )
    with pytest.raises(SharedStorageUnavailable), unrelated.open(slot="a"):
        pytest.fail("a different purpose opened the Managed Files namespace")
    empty = tmp_path.resolve() / "empty"
    empty.mkdir(mode=0o700)
    candidate = SharedNamespace(
        empty, original.purpose, original.deployment_id, original.namespace_id
    )
    with pytest.raises(SharedStorageUnavailable), candidate.open(slot="a"):
        pytest.fail("an empty root was treated as shared")
    assert list(empty.iterdir()) == []


def test_part_and_stream_hold_physical_lock_until_close(verified_namespace):
    publisher = SharedFileArtifactStore(verified_namespace, "a")
    reader = SharedFileArtifactStore(verified_namespace, "a")
    key = publisher.new_storage_key()
    with publisher.put_part(key) as handle:
        handle.write(b"frozen bytes")
        with pytest.raises(ArtifactStoreBusyError):
            reader.delete_part(key)
        assert reader.stat(key) is None
    publisher.commit(key)
    with reader.open(key) as stream:
        with pytest.raises(ArtifactStoreBusyError):
            publisher.delete(key)
        assert stream.read() == b"frozen bytes"
    assert publisher.delete(key)
    assert reader.stat(key) is None


def test_descendant_directory_symlink_cannot_change_publish_target(verified_namespace, tmp_path):
    outside: Path = tmp_path.resolve() / "outside"
    outside.mkdir(mode=0o700)
    (verified_namespace.root / "parts").symlink_to(outside, target_is_directory=True)
    store = SharedFileArtifactStore(verified_namespace, "a")
    with pytest.raises(OSError), store.put_part(store.new_storage_key()):
        pytest.fail("the producer followed a symlink")
    assert list(outside.iterdir()) == []


def register_namespace(api_client, session_factory, tmp_path, monkeypatch, purpose):
    namespace = verified_mount(tmp_path, monkeypatch, purpose)
    monkeypatch.setattr(settings, "control_ha_slot", "a")
    monkeypatch.setattr(storage_backends, "configured", lambda *_: namespace)
    monkeypatch.setattr(storage_backends, "runtime_mount_id", lambda _: 11)
    monkeypatch.setattr(control_ha_capacity, "assert_local_control", lambda _: uuid4())
    with session_factory.begin() as session:
        session.execute(
            text("""
            INSERT INTO control_storage_bindings (purpose,deployment_id,namespace_id,
                generation,state,manifest_sha256,policy_sha256)
            VALUES (:purpose,:deployment,:namespace,1,'active',:digest,:policy)
        """),
            {
                "purpose": purpose,
                "deployment": namespace.deployment_id,
                "namespace": namespace.namespace_id,
                "digest": "0" * 64,
                "policy": storage_backends.policy_sha256(purpose),
            },
        )
    with namespace.open(slot="a") as root:
        assert storage_backends._observe_mount(namespace, "a", shared_posix.preflight_id(root), 11)
    return namespace


def test_shared_builtin_cancel_holds_charge_while_writer_owns_part(
    api_client, session_factory, tmp_path, monkeypatch
):
    mount = register_namespace(
        api_client, session_factory, tmp_path, monkeypatch, "builtin_packages"
    )
    with session_factory() as writer:
        upload = builtin_package.reserve_upload(
            writer, kind="pypi", filename="one.whl", size_bytes=83
        )
        _, lock, stream = builtin_package.begin_upload(writer, upload.id)
        with lock, stream:
            stream.write(b"some bytes")
            stream.flush()
            with session_factory() as peer:
                builtin_package.cancel_upload(peer, upload.id)
                assert builtin_package.usage(peer)["reserved_bytes"] == 83
                assert (mount.root / (upload.id + ".part")).is_file()
                with pytest.raises(Exception, match="cancelled"):
                    builtin_package.check_shared_upload(upload.id)
    with session_factory() as peer:
        builtin_package.cancel_upload(peer, upload.id)
        assert builtin_package.usage(peer)["reserved_bytes"] == 0
        assert not (mount.root / (upload.id + ".part")).exists()


def test_shared_attachment_checks_scope_and_preserves_charge_until_pin_closes(
    api_client, session_factory, tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "ai_document_converter_url", "http://converter.invalid")
    mount = register_namespace(api_client, session_factory, tmp_path, monkeypatch, "ai_attachments")
    converter_id = uuid4()

    class Converter:
        def __init__(self, url, *, instance_id=None):
            self.url = url
            self.instance_id = instance_id

        def identity(self):
            return converter_id

        def prepare(self, filename, content_type, data, **_):
            assert self.instance_id == converter_id
            return DocumentResult("success", hashlib.sha256(data).hexdigest(), "store-v1", 1)

        def cancel(self, job_id):
            assert self.instance_id == converter_id

    monkeypatch.setattr(shared_attachments, "DocumentConverterClient", Converter)
    owner, page = ("user", 21), uuid4()
    a, b = shared_attachments.SharedAttachmentStore(), shared_attachments.SharedAttachmentStore()
    attachment = AiAttachment(
        filename="private-example.txt",
        content_type="text/plain",
        data_base64=base64.b64encode(b"retained content example").decode(),
    )
    result = a.prepare(owner, 34, page, attachment, deadline=time.monotonic() + 8)
    read = b.get(owner, 34, page, result.attachment_ref)
    assert (read.sha256, read.expires_at) == (result.sha256, result.expires_at)
    with pytest.raises(DocumentConversionError, match="expired"):
        b.get(("user", 22), 34, page, result.attachment_ref)
    frozen = AiAttachmentReference(
        attachment_ref=result.attachment_ref,
        sha256=result.sha256,
        representation_revision=result.representation_revision,
    )
    with b.reader(owner, 34, page, [frozen]) as reader:
        assert reader.overview()["attachments"][0]["size_bytes"] == result.size_bytes
        a.release(owner, 34, page)
        with pytest.raises(DocumentConversionError, match="expired"):
            reader.overview()
        with session_factory() as tx:
            metadata = str(
                tx.scalar(text("SELECT row_to_json(o) FROM control_attachment_objects o"))
            )
        assert "private-example" not in metadata and "retained content" not in metadata
        b.sweep()
        b.sweep()
        assert any(mount.root.glob("o-*"))
    for _ in range(3):
        b.sweep()
    assert not list(mount.root.glob("o-*"))
    with pytest.raises(DocumentConversionError, match="cancelled"):
        b.prepare(owner, 34, page, attachment, deadline=time.monotonic() + 8)


def test_old_preflight_cannot_register_replaced_mount_or_control(
    api_client, session_factory, tmp_path, monkeypatch
):
    mount = register_namespace(api_client, session_factory, tmp_path, monkeypatch, "managed_files")
    original, replacement, proof = uuid4(), uuid4(), uuid4()
    monkeypatch.setattr(control_ha_capacity, "assert_local_control", lambda _: original)
    assert storage_backends._observe_mount(mount, "a", proof, 11)
    assert not storage_backends._observe_mount(mount, "a", proof, 12)
    monkeypatch.setattr(control_ha_capacity, "assert_local_control", lambda _: replacement)
    assert not storage_backends._observe_mount(mount, "a", proof, 11)
    assert storage_backends._observe_mount(mount, "a", uuid4(), 12)


def test_copy_resume_can_reuse_only_verified_destination(tmp_path):
    import os

    source = tmp_path / "source"
    source.write_bytes(b"verified measured bytes")
    destination = tmp_path / "dest"
    destination.mkdir(mode=0o700)
    name = str(uuid4()) + ".blob"
    residual = destination / f".copy-{name}.part"
    residual.write_bytes(b"interrupted temporary data")
    residual.chmod(0o600)
    fd = os.open(destination, os.O_RDONLY | os.O_DIRECTORY)
    try:
        data = source.read_bytes()
        expected = len(data), hashlib.sha256(data).hexdigest()
        for _ in range(2):
            with source.open("rb") as saved:
                assert _copy_one(saved, fd, name, *expected) == expected
        assert not residual.exists()
        with pytest.raises(SharedStorageUnavailable, match="destination remains"):
            _assert_absent(fd, False, name.removesuffix(".blob"), False)
    finally:
        os.close(fd)


def test_attachment_binding_requires_retirement_and_cannot_reselect_retired_identity(
    api_client, session_factory, tmp_path, monkeypatch
):
    mount = register_namespace(api_client, session_factory, tmp_path, monkeypatch, "ai_attachments")
    replacement = SharedNamespace(mount.root, mount.purpose, mount.deployment_id, uuid4())
    with session_factory.begin() as session:
        with pytest.raises(SharedStorageUnavailable, match="outstanding"):
            _check_attachment_selection(session, replacement, mount.namespace_id)
        session.execute(
            text("""
            INSERT INTO control_attachment_retirements (namespace_id,deployment_id)
            VALUES (:id,:deployment)
        """),
            {"id": mount.namespace_id, "deployment": mount.deployment_id},
        )
        _check_attachment_selection(session, replacement, mount.namespace_id)
        with pytest.raises(SharedStorageUnavailable, match="outstanding"):
            _check_attachment_selection(session, mount, None)


def test_retirement_requires_stop_for_job_created_after_backup(
    api_client, session_factory, tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "ai_document_converter_url", "http://converter.invalid")
    mount = register_namespace(api_client, session_factory, tmp_path, monkeypatch, "ai_attachments")
    job_id, converter_id = uuid4(), uuid4()
    monkeypatch.setattr(storage_migration, "_owner", lambda _: mount.deployment_id)
    monkeypatch.setattr(storage_backends, "runtime_mount_id", lambda _: 22)
    with (
        pytest.raises(SharedStorageUnavailable, match="current observation"),
        storage_backends.verified_root(mount, "a"),
    ):
        pytest.fail("accepted a replaced mount before the next monitor probe")
    assert mount.purpose == "ai_attachments"
    (mount.root / ".job-locks").mkdir(mode=0o700)
    stand_in = mount.root / ("o-" + uuid4().hex)
    stand_in.mkdir(mode=0o700)
    with mount.open(slot="a") as root:
        shared_attachments._converter_intent(
            root, shared_attachments._Job(job_id, converter_id, 1, "a")
        )
    mount.revoke_temporary()

    class Converter:
        stopped = False

        def __init__(self, url, *, instance_id=None):
            assert url == settings.ai_document_converter_url and instance_id == converter_id

        def cancel(self, provided):
            assert provided == job_id.hex
            if not self.stopped:
                raise DocumentConversionError("ai_attachment_cancel_failed")

    monkeypatch.setattr(storage_migration, "DocumentConverterClient", Converter)
    with pytest.raises(DocumentConversionError, match="cancel_failed"):
        storage_migration.retire_temporary(session_factory, mount)
    assert stand_in.is_dir()
    assert (mount.root / ".job-locks" / ("j-" + job_id.hex)).exists()
    Converter.stopped = True
    storage_migration.retire_temporary(session_factory, mount)
    assert not stand_in.exists()
    assert not (mount.root / ".job-locks" / ("j-" + job_id.hex)).exists()
