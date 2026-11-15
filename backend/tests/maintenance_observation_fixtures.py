"""W1-D assertions within the existing restricted-role maintenance API fixture."""

from uuid import uuid4

from sqlalchemy import event, func, select

from dlr.control.models import Worker, WorkerMaintenanceOperation
from dlr.control.services import accounts


def assert_maintenance_observations(client, factory, worker_id, activation, runtime_secret):
    base = f"/api/workers/{worker_id}/v4"
    before = client.get(base + "/status")
    assert before.status_code == 200, before.text
    assert before.headers["cache-control"] == "no-store"
    status = before.json()
    assert status["observation_schema"] == 1
    assert status["maintenance_version"] == 4
    assert status["scheduling_state"] == "active"
    assert status["active_attempts"] == status["unsettled_attempts"] == 0
    assert status["has_unreconciled_attempts"] is False
    assert status["current_drain_operation"] is None
    assert status["last_recovery_disposition"] is None
    assert status["enrolled"] is True
    assert status["capabilities"] == ["python"]
    assert status["online"] is True
    for forbidden in ("recovery_binding", "host_evidence", "body_mac", "token", "credential"):
        assert forbidden not in status

    endpoint = base + "/maintenance-operations/" + activation["request_id"]
    receipt = client.get(endpoint)
    assert receipt.status_code == 200
    assert receipt.headers["cache-control"] == "no-store"
    observed = receipt.json()
    assert observed["operation"] == {
        "request_id": activation["request_id"],
        "worker_id": worker_id,
        "action": "activate",
        "expected_version": 1,
        "result_version": 2,
        "same_actor": True,
        "created_at": observed["operation"]["created_at"],
    }
    assert observed["current"]["maintenance_version"] == 4
    assert observed["current"]["scheduling_state"] == "active"
    assert observed["applies_to_current_version"] is False
    assert (
        client.get(
            f"/api/workers/{worker_id + 9999}/v4/maintenance-operations/" + activation["request_id"]
        ).status_code
        == 404
    )
    assert (
        client.get(endpoint, headers={"Authorization": "Bearer " + runtime_secret}).status_code
        == 401
    )

    # Real account sessions exercise the original authorization dependency.
    cookies = {}
    for role in ("user", "admin"):
        with factory() as session:
            user = accounts.create_user(
                session, "observe-" + role, "observation-test-password", role
            )
            user.must_change_password = False
            session.commit()
            cookies[role] = accounts.create_session(session, user)
    for url in (base + "/status", endpoint):
        assert (
            client.get(
                "/__dlr_account" + url,
                headers={"Cookie": "dlr_account_session=" + cookies["user"]},
            ).status_code
            == 403
        )
    other = client.get(
        "/__dlr_account" + endpoint,
        headers={"Cookie": "dlr_account_session=" + cookies["admin"]},
    )
    assert other.status_code == 200
    assert other.json()["operation"]["same_actor"] is False

    # An in-flight write changes Worker and its operation together. Each GET
    # must see one committed snapshot without waiting on the writer's row lock.
    pending_id = uuid4()
    with factory() as writer:
        worker = writer.get(Worker, worker_id, with_for_update=True)
        worker.maintenance_version = 5
        worker.scheduling_state = "draining"
        writer.add(
            WorkerMaintenanceOperation(
                request_id=pending_id,
                worker_id=worker_id,
                expected_version=4,
                result_version=5,
                action="drain",
                actor_kind="superadmin",
                actor_id=None,
            )
        )
        writer.flush()
        statements = []

        def record(_conn, _cursor, statement, _parameters, _context, _executemany):
            if "workers" in statement and statement.lstrip().upper().startswith(("SELECT", "WITH")):
                statements.append(statement)

        event.listen(factory.kw["bind"], "before_cursor_execute", record)
        try:
            snapshot = client.get(base + "/status")
        finally:
            event.remove(factory.kw["bind"], "before_cursor_execute", record)
        assert snapshot.status_code == 200, snapshot.text
        assert len(statements) == 1 and "FOR UPDATE" not in statements[0]
        assert snapshot.json()["maintenance_version"] == 4
        assert snapshot.json()["scheduling_state"] == "active"
        assert snapshot.json()["current_drain_operation"] is None
        assert client.get(base + "/maintenance-operations/" + str(pending_id)).status_code == 404
        writer.rollback()

    with factory() as session:
        count_before = session.scalar(select(func.count()).select_from(WorkerMaintenanceOperation))
    assert client.get(base + "/status").status_code == 200
    assert client.get(endpoint).status_code == 200
    with factory() as session:
        assert (
            session.scalar(select(func.count()).select_from(WorkerMaintenanceOperation))
            == count_before
        )
        assert session.get(Worker, worker_id).maintenance_version == 4

    # A genuine newly committed action becomes visible in a separate GET.
    drain = {"request_id": str(uuid4()), "expected_version": 4}
    assert client.post(base + "/drain", json=drain).status_code == 200
    receipt = client.get(base + "/maintenance-operations/" + drain["request_id"])
    assert receipt.status_code == 200
    assert receipt.json()["applies_to_current_version"] is True
    assert receipt.json()["current"]["scheduling_state"] == "disabled"
    snapshot = client.get(base + "/status").json()
    assert snapshot["current_drain_operation"] == {
        **drain,
        "action": "drain",
        "result_version": 5,
        "same_actor": True,
    }
    next_activation = {"request_id": str(uuid4()), "expected_version": 5}
    assert client.post(base + "/activate", json=next_activation).status_code == 200
    replaced = client.get(base + "/maintenance-operations/" + drain["request_id"]).json()
    assert replaced["applies_to_current_version"] is False
    assert replaced["current"]["maintenance_version"] == 6
    return 6
