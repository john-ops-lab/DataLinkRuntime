"""Bounded native owners, independent leases and diagnostic connections for Control."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import logging
import random
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import and_, exists, or_, select
from sqlalchemy.orm import Session, sessionmaker

from dlr.common.config import settings
from dlr.control.models import Credential
from dlr.control.models.subscription import (
    EventSubscription,
    SourceCheckpoint,
    SubscriptionActivation,
    SubscriptionConfigVersion,
    SubscriptionLease,
)
from dlr.control.services import subscription_delivery as delivery
from dlr.control.services import subscription_management as management
from dlr.control.services.secrets import decrypt_fields
from dlr.control.services.subscription_queue_failure import LEGACY_LIFECYCLE_FAILURES
from dlr.control.services.subscription_source import (
    ApplicationBufferBudget,
    SourceFailure,
    SourceRunContext,
    SourceRunner,
)

logger = logging.getLogger("dlr.control.subscription_runtime")
LEASE_SECONDS = 30
RENEW_SECONDS = 10


def implemented_sources() -> dict[str, SourceRunner]:
    # Implemented permits bounded connection diagnostics. Public availability is
    # independently gated by acceptance in management.AVAILABLE_SOURCES.
    from dlr.control.services.subscription_registry import SOURCES

    return {name: item.runner() for name, item in SOURCES.items() if item.implemented}


def credential_values(session: Session, credential_id: int | None) -> dict[str, str]:
    if credential_id is None:
        return {}
    credential = session.get(Credential, credential_id)
    if credential is None:
        raise SourceFailure("source_credential_unavailable")
    try:
        return decrypt_fields(credential.ciphertext)
    except Exception:
        raise SourceFailure("source_credential_unavailable") from None


@dataclass
class _Owner:
    context: SourceRunContext
    runner: SourceRunner
    thread: threading.Thread | None = None
    close_unconfirmed: bool = False
    native_closed: bool = False
    failure: SourceFailure | None = None


class SubscriptionRuntimeManager:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        registry: Mapping[str, SourceRunner] | None = None,
        *,
        max_listeners: int = 16,
        buffer_limit_bytes: int = 64 * 1024**2,
        clock: Callable[[], float] = time.monotonic,
        jitter: Callable[[float, float], float] = random.uniform,
    ) -> None:
        if not 1 <= max_listeners <= 16:
            raise ValueError("Invalid source owner limit")
        self.session_factory = session_factory
        self.registry = dict(implemented_sources() if registry is None else registry)
        self.buffer = ApplicationBufferBudget(buffer_limit_bytes)
        self._permits = threading.BoundedSemaphore(max_listeners)
        self._lock = threading.RLock()
        self._owners: dict[int, _Owner] = {}
        self._probes: set[int] = set()
        self._starting: set[int] = set()
        self._retry: dict[int, tuple[int, float]] = {}
        self._clock = clock
        self._jitter = jitter
        self._shutdown = threading.Event()
        self._supervisor: threading.Thread | None = None
        self._renewer: threading.Thread | None = None
        self._cursor = 0
        self._upper = 0

    @property
    def active_count(self) -> int:
        with self._lock:
            return len(self._owners) + len(self._probes) + len(self._starting)

    def _claim(self, subscription_id: int, *, probe: bool = False) -> bool:
        with self._lock:
            if (
                self._shutdown.is_set()
                or subscription_id in self._owners
                or subscription_id in self._probes
                or subscription_id in self._starting
                or not self._permits.acquire(blocking=False)
            ):
                return False
            (self._probes if probe else self._starting).add(subscription_id)
            return True

    def _release_start(self, subscription_id: int) -> None:
        with self._lock:
            if subscription_id in self._starting:
                self._starting.remove(subscription_id)
                self._permits.release()

    def start_once(self, subscription_id: int) -> bool:
        """No thread is created unless both a local permit and a DB lease are acquired."""
        if not settings.event_subscriptions_enabled or not self._claim(subscription_id):
            return False
        owner: _Owner | None = None
        try:
            with self.session_factory.begin() as session:
                _, sub = delivery.lock_subscription(session, subscription_id)
                runner = self.registry.get(sub.source_type)
                if (
                    runner is None
                    or sub.desired_state != "enabled"
                    or (
                        sub.connection_state == "blocked"
                        and (
                            sub.source_type not in {"rabbitmq", "rocketmq"}
                            or sub.last_error_code
                            not in LEGACY_LIFECYCLE_FAILURES | {"source_shard_blocked"}
                        )
                    )
                    or sub.deleted_at is not None
                    or sub.activation_id is None
                    or not settings.event_subscriptions_enabled
                ):
                    self._release_start(subscription_id)
                    return False
                activation = session.get(SubscriptionActivation, sub.activation_id)
                if activation is None or activation.subscription_id != sub.id:
                    raise SourceFailure("source_activation_unavailable")
                config = session.get(SubscriptionConfigVersion, activation.config_version_id)
                if config is None or config.subscription_id != sub.id:
                    raise SourceFailure("source_configuration_unavailable")
                if not delivery.recover_queue_lifecycle_fault(session, sub, config):
                    self._release_start(subscription_id)
                    return False
                if sub.connection_state == "blocked":
                    self._release_start(subscription_id)
                    return False
                from dlr.control.services.subscription_sync import native_start_decision

                decision = native_start_decision(
                    session, sub.id, activation.id, config.source_epoch
                )
                if decision.action != "allow":
                    if decision.action == "blocked":
                        sub.connection_state = "blocked"
                        sub.last_error_code = decision.code
                    self._release_start(subscription_id)
                    return False
                values = credential_values(session, config.source_credential_id)
                lease = delivery.acquire_lease(session, sub.id, uuid.uuid4().hex)
                context = SourceRunContext(
                    subscription_id=sub.id,
                    owner_id=lease.owner_id,
                    generation=lease.generation,
                    activation_id=activation.id,
                    config_version=config.version,
                    source_epoch=config.source_epoch,
                    source_config=copy.deepcopy(config.config),
                    credential_fields=values,
                    stop_event=threading.Event(),
                    session_factory=self.session_factory,
                    buffer=self.buffer.scope(),
                    connected=lambda: self._connected(subscription_id),
                    identity_snapshot=copy.deepcopy(config.source_identity),
                )
                sub.connection_state = "starting"
            owner = _Owner(context, runner)
            owner.thread = threading.Thread(
                target=self._run_owner,
                args=(owner,),
                name=f"dlr-source-{subscription_id}",
                daemon=True,
            )
            with self._lock:
                self._starting.remove(subscription_id)
                self._owners[subscription_id] = owner
                abort = self._shutdown.is_set() or not settings.event_subscriptions_enabled
            if abort:
                # A shutdown may have fenced its owner snapshot while this
                # transaction was still acquiring a lease. Do not create a native
                # client after that snapshot; acknowledge this unused lease now.
                self._finish_owner(owner, None)
                return False
            owner.thread.start()
            return True
        except SourceFailure as error:
            self._record_unowned_failure(subscription_id, error)
        except Exception:
            # DB failures/lease contention never disclose raw exception text.
            pass
        if owner is not None:
            # Thread.start itself can fail before running any SDK code.
            self._finish_owner(owner, SourceFailure("source_thread_unavailable", "backoff"))
        else:
            self._release_start(subscription_id)
        return False

    def _connected(self, subscription_id: int) -> None:
        with self._lock:
            owner = self._owners.get(subscription_id)
        if owner is None or owner.context.stop_event.is_set():
            raise SourceFailure("subscription_owner_stale", "backoff")
        context = owner.context
        with self.session_factory.begin() as session:
            _, sub = delivery.lock_subscription(session, subscription_id)
            delivery._lease(session, sub, context.owner_id, context.generation)
            if not settings.event_subscriptions_enabled:
                raise SourceFailure("source_disabled", "backoff")
            sub.connection_state = "listening"
            sub.last_error_code = None
        with self._lock:
            self._retry.pop(subscription_id, None)

    def _record_unowned_failure(self, subscription_id: int, error: SourceFailure) -> None:
        try:
            with self.session_factory.begin() as session:
                _, sub = delivery.lock_subscription(session, subscription_id)
                if sub.desired_state == "enabled":
                    sub.connection_state = "blocked"
                    sub.last_error_code = error.code
        except Exception:
            logger.warning("source initialization unavailable subscription_id=%s", subscription_id)

    def _run_owner(self, owner: _Owner) -> None:
        failure: SourceFailure | None = None
        try:
            if not owner.context.stop_event.is_set() and not self._shutdown.is_set():
                owner.runner.run(owner.context)
        except SourceFailure as error:
            failure = error
        except Exception:
            # A broken connector cannot assert resource reclamation implicitly.
            failure = SourceFailure("source_runtime_error", closed=False)
        self._finish_owner(owner, failure)

    def _finish_owner(self, owner: _Owner, failure: SourceFailure | None) -> None:
        context = owner.context
        owner.close_unconfirmed = failure is not None and not failure.closed
        owner.native_closed = not owner.close_unconfirmed
        owner.failure = failure
        context.stop_event.set()
        try:
            with self.session_factory.begin() as session:
                _, sub = delivery.lock_subscription(session, context.subscription_id)
                lease = session.get(SubscriptionLease, sub.id, with_for_update=True)
                if lease is not None and lease.owner_id == context.owner_id:
                    same_owner = True
                else:
                    same_owner = False
                if lease is not None and same_owner and lease.generation == context.generation:
                    management._fence(session, sub)
                if (
                    lease is not None
                    and same_owner
                    and lease.closing_generation == context.generation
                ):
                    if owner.close_unconfirmed:
                        sub.connection_state = "blocked"
                        sub.last_error_code = "source_close_unconfirmed"
                    else:
                        management.listener_closed(
                            session, sub.id, context.owner_id, context.generation
                        )
                        if sub.desired_state == "enabled":
                            if not settings.event_subscriptions_enabled:
                                sub.connection_state = "stopped"
                            elif failure is not None:
                                sub.connection_state = (
                                    "blocked"
                                    if failure.disposition == "blocked"
                                    else "reconnecting"
                                )
                            else:
                                sub.connection_state = "starting"
                            sub.last_error_code = failure.code if failure else None
        except Exception:
            logger.warning(
                "source close acknowledgement unavailable subscription_id=%s",
                context.subscription_id,
            )
            return  # Retry the DB acknowledgement without recreating or renewing the client.
        if owner.close_unconfirmed:
            return
        context.buffer.release_all()
        context.credential_fields.clear()
        with self._lock:
            if self._owners.get(context.subscription_id) is owner:
                self._owners.pop(context.subscription_id)
                self._permits.release()
                if failure and failure.disposition != "blocked":
                    attempt = self._retry.get(context.subscription_id, (0, 0))[0] + 1
                    ceiling = 30 if failure.disposition == "capacity" else 60
                    base = min(ceiling, 2 ** min(attempt - 1, 6))
                    delay = min(ceiling, max(1.0, base * self._jitter(0.8, 1.2)))
                    self._retry[context.subscription_id] = (attempt, self._clock() + delay)

    def stop_single(self, subscription_id: int) -> None:
        with self._lock:
            owner = self._owners.get(subscription_id)
        if owner is None:
            return
        context = owner.context
        try:
            with self.session_factory.begin() as session:
                _, sub = delivery.lock_subscription(session, subscription_id)
                lease = session.get(SubscriptionLease, sub.id, with_for_update=True)
                if lease is not None and lease.owner_id == context.owner_id:
                    if lease.generation == context.generation:
                        management._fence(session, sub)
                    if sub.connection_state != "blocked":
                        sub.connection_state = (
                            "stopping" if sub.desired_state == "disabled" else "starting"
                        )
        finally:
            # DB outage cannot authorize more reception: stop now; T1 still fences.
            context.stop_event.set()

    def stop_all(self) -> None:
        with self._lock:
            ids = list(self._owners)
        for subscription_id in ids:
            try:
                self.stop_single(subscription_id)
            except Exception:
                logger.warning("source stop fence unavailable subscription_id=%s", subscription_id)

    def reconcile_once(self) -> None:
        with self._lock:
            owners = list(self._owners.items())
            retries = list(self._retry)
        for _, owner in owners:
            if owner.native_closed:
                self._finish_owner(owner, owner.failure)
            elif owner.close_unconfirmed:
                self._record_unowned_failure(
                    owner.context.subscription_id, SourceFailure("source_close_unconfirmed")
                )
        with self.session_factory() as session:
            if retries:
                still_enabled = set(
                    session.scalars(
                        select(EventSubscription.id).where(
                            EventSubscription.id.in_(retries),
                            EventSubscription.desired_state == "enabled",
                            EventSubscription.deleted_at.is_(None),
                        )
                    )
                )
                with self._lock:
                    for subscription_id in retries:
                        if subscription_id not in still_enabled:
                            self._retry.pop(subscription_id, None)
            for subscription_id, owner in owners:
                if owner.native_closed or owner.close_unconfirmed:
                    continue
                sub = session.get(EventSubscription, subscription_id)
                lease = session.get(SubscriptionLease, subscription_id)
                if (
                    not settings.event_subscriptions_enabled
                    or sub is None
                    or sub.deleted_at is not None
                    or sub.desired_state != "enabled"
                    or sub.activation_id != owner.context.activation_id
                    or lease is None
                    or lease.owner_id != owner.context.owner_id
                    or lease.generation != owner.context.generation
                ):
                    self.stop_single(subscription_id)
            ids, self._cursor, self._upper = management.bounded_page(
                session,
                select(EventSubscription.id).where(
                    EventSubscription.deleted_at.is_(None),
                    EventSubscription.desired_state == "enabled",
                    or_(
                        EventSubscription.connection_state != "blocked",
                        and_(
                            EventSubscription.source_type.in_(("rabbitmq", "rocketmq")),
                            EventSubscription.last_error_code.in_(
                                LEGACY_LIFECYCLE_FAILURES | {"source_shard_blocked"}
                            ),
                            exists().where(
                                SourceCheckpoint.subscription_id == EventSubscription.id,
                                SourceCheckpoint.source_epoch == EventSubscription.source_epoch,
                                SourceCheckpoint.shard == "messages",
                                SourceCheckpoint.blocked_unaccepted["code"]
                                .as_string()
                                .in_(LEGACY_LIFECYCLE_FAILURES),
                            ),
                        ),
                    ),
                ),
                EventSubscription.id,
                self._cursor,
                self._upper,
            )
        for subscription_id in ids:
            with self._lock:
                retry_at = self._retry.get(subscription_id, (0, 0))[1]
            if self._clock() >= retry_at:
                self.start_once(subscription_id)

    def renew_once(self) -> None:
        with self._lock:
            owners = list(self._owners.values())
        for owner in owners:
            context = owner.context
            if context.stop_event.is_set():
                continue
            try:
                if not settings.event_subscriptions_enabled:
                    self.stop_single(context.subscription_id)
                    continue
                with self.session_factory.begin() as session:
                    delivery.renew_lease(
                        session, context.subscription_id, context.owner_id, context.generation
                    )
            except Exception:
                context.stop_event.set()

    def start(self) -> None:
        if self._supervisor is not None:
            return
        self._supervisor = threading.Thread(
            target=self._supervise, name="dlr-source-manager", daemon=True
        )
        self._renewer = threading.Thread(
            target=self._renew_loop, name="dlr-source-leases", daemon=True
        )
        self._supervisor.start()
        self._renewer.start()

    def _supervise(self) -> None:
        try:
            while not self._shutdown.is_set():
                try:
                    self.reconcile_once()
                except Exception:
                    logger.warning("source reconciliation unavailable")
                    self.stop_all()
                self._shutdown.wait(settings.event_subscription_tick_seconds)
        finally:
            self.stop_all()

    def _renew_loop(self) -> None:
        while not self._shutdown.wait(RENEW_SECONDS):
            try:
                self.renew_once()
            except Exception:
                logger.warning("source lease maintenance unavailable")

    async def aclose(self, timeout: float = 15) -> None:
        self._shutdown.set()
        if self._supervisor is None:
            # Directly started owners share the same shutdown contract. DB work
            # still runs outside the API loop/default executor.
            self._supervisor = threading.Thread(
                target=self.stop_all, name="dlr-source-manager-stop", daemon=True
            )
            self._supervisor.start()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                active = bool(self._owners or self._probes or self._starting)
            if not active and not (self._supervisor and self._supervisor.is_alive()):
                return
            await asyncio.sleep(0.02)
        # Native operations keep their permits until returning, even during shutdown.
        logger.warning("source shutdown awaiting native close")

    async def test_connection(
        self,
        subscription_id: int,
        source_config: dict[str, Any],
        credential_fields: dict[str, str],
        *,
        timeout: float = 15,
    ) -> dict[str, Any]:
        source_type = source_config["source_type"]
        runner = self.registry.get(source_type)
        if runner is None:
            raise SourceFailure("source_not_available")
        if not self._claim(subscription_id, probe=True):
            raise SourceFailure("subscription_capacity_exceeded", "capacity")
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        config = copy.deepcopy(source_config)
        secrets = dict(credential_fields)

        def deliver(result: dict[str, Any] | SourceFailure) -> None:
            if not future.done():
                if isinstance(result, SourceFailure):
                    future.set_exception(result)
                else:
                    future.set_result(result)

        def run() -> None:
            result: dict[str, Any] | SourceFailure
            try:
                checked = runner.test_connection(config, secrets)
                result = {**checked, "source_type": source_type}
            except SourceFailure as error:
                result = error
            except Exception:
                result = SourceFailure("source_connection_failed", "backoff", closed=False)
            closed = not isinstance(result, SourceFailure) or result.closed
            if closed:
                secrets.clear()
                with self._lock:
                    self._probes.discard(subscription_id)
                    self._permits.release()
            # The HTTP loop may already have shut down; native cleanup still ran.
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(deliver, result)

        thread = threading.Thread(
            target=run, name=f"dlr-source-test-{subscription_id}", daemon=True
        )
        try:
            thread.start()
        except Exception:
            secrets.clear()
            with self._lock:
                self._probes.discard(subscription_id)
                self._permits.release()
            raise SourceFailure("source_thread_unavailable", "backoff") from None
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except TimeoutError:
            raise SourceFailure("source_connection_timeout", "backoff", closed=False) from None


_runtime: SubscriptionRuntimeManager | None = None


def get_runtime() -> SubscriptionRuntimeManager:
    global _runtime
    if _runtime is None:
        from dlr.control import db

        _runtime = SubscriptionRuntimeManager(db.SessionLocal)
    return _runtime
