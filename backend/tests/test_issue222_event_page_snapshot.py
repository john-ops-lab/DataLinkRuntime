"""Real PostgreSQL pagination remains self-consistent during concurrent custody."""

from sqlalchemy import event

from dlr.control.services import subscription_delivery as delivery
from test_issue219_delivery import receive
from test_issue219_delivery import world as world
from test_issue219_product import empty_subscription


def test_count_and_items_use_one_snapshot_when_writer_commits_after_receipt_query(
    api_client, world, session_factory, test_engine
):
    injected = False
    selected = []

    def insert_after_first_receipt_read(conn, cursor, statement, parameters, context, many):
        nonlocal injected
        lowered = statement.lower()
        if (
            injected
            or "event_receipts" not in lowered
            or not lowered.lstrip().startswith(("select", "with"))
        ):
            return
        # The original implementation first reads count=0, then this writer
        # commits before its separate page read. The one-statement version's
        # PostgreSQL snapshot has already fixed both count and page at zero.
        injected = True
        selected.append(statement)
        with session_factory.begin() as writer:
            receive(writer, world)

    event.listen(test_engine, "after_cursor_execute", insert_after_first_receipt_read)
    try:
        first = api_client.get(f"/api/event-subscriptions/{world['sub']}/events?limit=2")
    finally:
        event.remove(test_engine, "after_cursor_execute", insert_after_first_receipt_read)
    assert injected
    assert first.status_code == 200, first.text
    assert first.json() == {"items": [], "total": 0, "next_before_id": None}
    assert "source_raw_body" not in selected[0]
    second = api_client.get(f"/api/event-subscriptions/{world['sub']}/events?limit=2")
    assert second.status_code == 200, second.text
    assert second.json()["total"] == len(second.json()["items"]) == 1
    assert second.json()["next_before_id"] is None
    with session_factory() as session:
        assert delivery.audit_quota(session, world["sub"])["differences"] == {}


def test_cursor_keeps_full_scope_total_and_empty_pages_do_not_lose_it(
    api_client, world, session_factory
):
    with session_factory.begin() as session:
        identities = [receive(session, world, offset=index).id for index in range(3)]
        other = empty_subscription(session, "events-scope")
    path = f"/api/event-subscriptions/{world['sub']}/events"
    first = api_client.get(path, params={"limit": 2})
    assert first.status_code == 200, first.text
    page = first.json()
    assert page["total"] == 3
    assert [item["id"] for item in page["items"]] == identities[::-1][:2]
    assert page["next_before_id"] == identities[1]
    second = api_client.get(path, params={"limit": 2, "before_id": page["next_before_id"]})
    assert second.status_code == 200, second.text
    assert second.json()["total"] == 3
    assert [item["id"] for item in second.json()["items"]] == identities[:1]
    assert second.json()["next_before_id"] is None
    empty = api_client.get(path, params={"limit": 2, "before_id": identities[0]})
    assert empty.status_code == 200
    assert empty.json() == {"items": [], "total": 3, "next_before_id": None}
    unrelated = api_client.get(f"/api/event-subscriptions/{other}/events?limit=2")
    assert unrelated.status_code == 200
    assert unrelated.json() == {"items": [], "total": 0, "next_before_id": None}
