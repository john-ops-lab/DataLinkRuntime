"""Focused protocol/privacy/budget tests; no product imports or services."""

import asyncio
import contextvars
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import observe


class ObservationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.writer = observe.Writer(self.temp.name, "control")
        self.observer = observe.Observer(self.writer)

    def rows(self):
        self.observer.flush()
        path = Path(self.temp.name) / "control.jsonl"
        return (
            [json.loads(line) for line in path.read_text().splitlines()]
            if path.exists()
            else []
        )

    def test_disabled_startup_has_no_product_imports_or_files(self):
        path = Path(__file__).parent.resolve()
        env = {
            key: value
            for key, value in os.environ.items()
            if key not in {"DLR_B0_ROLE", "DLR_B0_DIAGNOSTICS_DIR"}
        }
        env["PYTHONPATH"] = str(path)
        env["DLR_B0_DIAGNOSTICS_DIR"] = str(Path(self.temp.name) / "not-created")
        output = subprocess.check_output(
            [
                sys.executable,
                "-c",
                "import sys,json; print(json.dumps({'observe': 'observe' in sys.modules, 'dlr': any(x.startswith('dlr') for x in sys.modules)}))",
            ],
            env=env,
            text=True,
        )
        self.assertEqual(json.loads(output), {"observe": False, "dlr": False})
        self.assertFalse((Path(self.temp.name) / "not-created").exists())

    def test_disabled_install_does_not_change_import_hooks(self):
        hooks = list(sys.meta_path)
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(observe.install())
        self.assertEqual(hooks, sys.meta_path)
        self.assertEqual(list(Path(self.temp.name).iterdir()), [])

    def test_return_and_exception_identity_are_unchanged(self):
        result = object()
        failure = RuntimeError("do-not-record-this-secret")

        def returned(execution_id):
            return result

        def failed(execution_id):
            raise failure

        wrapped = self.observer.wrap(returned, "returned")
        self.assertIs(wrapped(12), result)
        self.assertEqual(wrapped.__name__, returned.__name__)
        self.assertIs(wrapped.__wrapped__, returned)
        self.assertIs(self.observer.wrap(returned, "another-alias"), wrapped)
        with self.assertRaises(RuntimeError) as caught:
            self.observer.wrap(failed, "failed")(12)
        self.assertIs(caught.exception, failure)
        ends = [row for row in self.rows() if row.get("phase") == "end"]
        self.assertEqual(ends[0]["ids"], {"execution_id": 12})
        self.assertEqual(ends[-1]["error_type"], "RuntimeError")
        self.assertNotIn(
            "do-not-record-this-secret",
            (Path(self.temp.name) / "control.jsonl").read_text(),
        )

    def test_async_return_and_cancellation_are_preserved(self):
        value = object()

        async def original(attempt_id):
            await asyncio.sleep(0)
            return value

        self.assertIs(asyncio.run(self.observer.wrap(original, "async")(7)), value)

        async def cancelled():
            raise asyncio.CancelledError()

        with self.assertRaises(asyncio.CancelledError):
            asyncio.run(self.observer.wrap(cancelled, "cancel")())
        self.assertTrue(
            any(row.get("error_type") == "CancelledError" for row in self.rows())
        )

    def test_generator_send_throw_return_close_and_context(self):
        events = []

        def original(execution_id):
            try:
                first = yield "first"
                try:
                    yield first
                except ValueError:
                    yield "caught"
                return "finished"
            finally:
                events.append("closed")

        wrapped = self.observer.wrap(original, "generator")
        generator = wrapped(2)
        self.assertEqual(next(generator), "first")
        self.assertIsNone(observe._IDS.get())
        self.assertEqual(contextvars.Context().run(generator.send, "second"), "second")
        self.assertEqual(generator.throw(ValueError("not-logged")), "caught")
        with self.assertRaises(StopIteration) as caught:
            next(generator)
        self.assertEqual(caught.exception.value, "finished")
        self.assertEqual(events, ["closed"])
        another = wrapped(3)
        next(another)
        contextvars.Context().run(another.close)
        self.assertEqual(events, ["closed", "closed"])
        self.assertIsNone(observe._IDS.get())

    def test_orm_properties_never_load_and_arbitrary_fields_are_not_recorded(self):
        class EventReceipt:
            @property
            def id(self):
                raise AssertionError("ORM load attempted")

            @property
            def envelope(self):
                raise AssertionError("payload access attempted")

        row = EventReceipt()
        row.__dict__.update(
            {"id": 14, "subscription_id": 8, "payload": "secret", "token": "secret"}
        )
        self.assertEqual(
            observe.identifiers(row), {"receipt_id": 14, "subscription_id": 8}
        )
        row.__dict__.pop("id")
        self.assertEqual(observe.identifiers(row), {"subscription_id": 8})

    def test_sql_classification_never_retains_statement_or_parameters(self):
        statement = "SELECT secret_value FROM event_receipts WHERE token='VERY_SECRET' FOR UPDATE"
        result = observe._sql_classification(statement)
        self.assertEqual(
            result,
            {"operation": "select", "tables": ["event_receipts"], "for_update": True},
        )
        self.assertNotIn("SECRET", json.dumps(result))
        self.assertEqual(
            observe._sql_classification(object()),
            {"operation": "other", "tables": [], "for_update": False},
        )

    def test_repetition_is_sampled_but_exact_aggregate_is_kept(self):
        def original(delivery_id):
            return None

        wrapped = self.observer.wrap(original, "repeat")
        for _ in range(12):
            wrapped(9)
        rows = self.rows()
        aggregate = [row for row in rows if row["kind"] == "aggregate"]
        self.assertEqual(sum(row["count"] for row in aggregate), 12)
        self.assertLess(len([row for row in rows if row["kind"] == "span"]), 24)
        self.assertTrue(
            all(row["elapsed_ns"] >= 0 for row in rows if row.get("phase") == "end")
        )

    def test_secondary_inherited_ids_do_not_multiply_responsibility_details(self):
        def original(delivery_id, **secondary):
            return None

        wrapped = self.observer.wrap(original, "stable-responsibility")
        for n in range(12):
            wrapped(9, execution_id=100 + n, attempt_id=200 + n, outbox_id=300 + n)
        rows = self.rows()
        spans = [row for row in rows if row["kind"] == "span"]
        self.assertEqual(len([row for row in spans if row["phase"] == "begin"]), 2)
        self.assertEqual(len([row for row in spans if row["phase"] == "end"]), 2)
        self.assertTrue(all(row["ids"]["delivery_id"] == 9 for row in spans))
        aggregate = [row for row in rows if row["kind"] == "aggregate"]
        self.assertEqual(sum(row["count"] for row in aggregate), 12)
        self.assertEqual(sum(sum(row["bucket_counts"]) for row in aggregate), 12)

    def test_sql_details_are_global_per_class_with_complete_stage_aggregates(self):
        callbacks = {}
        module = types.ModuleType("sqlalchemy")
        module.event = types.SimpleNamespace(
            listen=lambda engine, name, callback: callbacks.update({name: callback})
        )
        module.engine = types.SimpleNamespace(Engine=object())
        with patch.dict(sys.modules, {"sqlalchemy": module}):
            observe._sql_events(self.observer)
        statement = "SELECT id FROM event_receipts FOR UPDATE"
        for n in range(20):
            identity = observe._IDS.set({"delivery_id": n + 1, "execution_id": n + 200})
            stage = observe._STAGE.set("phase-" + str(n % 2))
            try:
                context = types.SimpleNamespace()
                callbacks["before_cursor_execute"](
                    None, None, statement, None, context, None
                )
                callbacks["after_cursor_execute"](
                    None, None, statement, None, context, None
                )
            finally:
                observe._IDS.reset(identity)
                observe._STAGE.reset(stage)
        rows = self.rows()
        self.assertEqual(len([row for row in rows if row["kind"] == "sql"]), 2)
        aggregates = [row for row in rows if row["kind"] == "aggregate"]
        self.assertEqual(len(aggregates), 2)
        self.assertEqual(sum(row["count"] for row in aggregates), 20)
        self.assertEqual(sum(sum(row["bucket_counts"]) for row in aggregates), 20)

    def test_real_t2_and_terminal_commits_survive_prior_unchanged_scans(self):
        class Execution:
            def __init__(self):
                self.id = 44

        class EventDelivery:
            def __init__(self):
                self.id, self.receipt_id, self.execution_id = 9, 8, 44
                self.state = "processing"

            def __getattr__(self, name):
                raise AssertionError("ORM load attempted: " + name)

        class Session:
            def __init__(self):
                self.commits = 0

            def commit(self):
                self.commits += 1

        session, delivery = Session(), EventDelivery()
        calls = {"dispatch": 0, "observe": 0}

        def dispatch(session, delivery_id):
            calls["dispatch"] += 1
            return Execution() if calls["dispatch"] == 8 else None

        def context(session, delivery_id):
            return (delivery,)

        context = self.observer.wrap(
            context, "dlr.control.services.subscription_delivery._delivery_context"
        )

        def observed(session, delivery_id):
            item = context(session, delivery_id)[0]
            calls["observe"] += 1
            if calls["observe"] == 8:
                item.state = "succeeded"

        dispatched = self.observer.wrap(
            dispatch, "dlr.control.services.subscription_delivery.dispatch_delivery"
        )
        observed = self.observer.wrap(
            observed, "dlr.control.services.subscription_delivery.observe_terminal"
        )
        committed = self.observer.wrap(
            Session.commit, "sqlalchemy.orm.session.Session.commit"
        )
        identity = observe._IDS.set({"delivery_id": 9, "receipt_id": 8})
        pending = observe._BOUNDARY_COMMIT.set(None)
        try:
            for _ in range(8):
                result = dispatched(session, 9)
                if calls["dispatch"] < 8:
                    self.assertIsNone(result)
                else:
                    self.assertIsInstance(result, Execution)
                committed(session)
            for _ in range(8):
                self.assertIsNone(observed(session, 9))
                committed(session)
        finally:
            observe._IDS.reset(identity)
            observe._BOUNDARY_COMMIT.reset(pending)
        self.assertEqual(session.commits, 16)
        boundaries = [
            row
            for row in self.rows()
            if row.get("phase") == "end" and row.get("boundary_kind")
        ]
        self.assertEqual(
            [(row["boundary_kind"], row["boundary_commit"]) for row in boundaries],
            [
                ("materialized", False),
                ("materialized", True),
                ("terminal", False),
                ("terminal", True),
            ],
        )
        self.assertTrue(all(row["ids"]["delivery_id"] == 9 for row in boundaries))
        self.assertTrue(all(row["ids"]["receipt_id"] == 8 for row in boundaries))
        self.assertTrue(all(row["ids"]["execution_id"] == 44 for row in boundaries))
        self.assertTrue(
            all(row["commit_succeeded"] for row in boundaries if row["boundary_commit"])
        )
        self.assertEqual(boundaries[-1]["responsibility_state"], "succeeded")

    def test_failed_boundary_commit_preserves_exception_and_never_claims_success(self):
        class Session:
            def commit(self):
                raise failure

        session = Session()
        failure = RuntimeError("SECRET_COMMIT_FAILURE")
        import weakref

        pending = observe._BOUNDARY_COMMIT.set(
            (
                weakref.ref(session),
                {"delivery_id": 9, "receipt_id": 8, "execution_id": 44},
                "materialized",
                None,
            )
        )
        try:
            committed = self.observer.wrap(
                Session.commit, "sqlalchemy.orm.session.Session.commit"
            )
            with self.assertRaises(RuntimeError) as caught:
                committed(session)
            self.assertIs(caught.exception, failure)
            self.assertIsNone(observe._BOUNDARY_COMMIT.get())
        finally:
            observe._BOUNDARY_COMMIT.reset(pending)
        commits = [row for row in self.rows() if row.get("boundary_commit")]
        self.assertEqual(len(commits), 1)
        self.assertEqual(commits[0]["outcome"], "error")
        self.assertFalse(commits[0]["commit_succeeded"])

    @unittest.skipUnless(
        importlib.util.find_spec("sqlalchemy"), "requires existing SQLAlchemy library"
    )
    def test_terminal_state_survives_autoflush_without_external_model_reference(self):
        import weakref

        from sqlalchemy import Integer, String, create_engine
        from sqlalchemy.orm import DeclarativeBase, Session, mapped_column

        class Base(DeclarativeBase):
            pass

        class EventDelivery(Base):
            __tablename__ = "b0_delivery"
            id = mapped_column(Integer, primary_key=True)
            receipt_id = mapped_column(Integer)
            execution_id = mapped_column(Integer)
            state = mapped_column(String)

        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        references = []
        calls = 0

        def context(session, delivery_id):
            item = session.get(EventDelivery, delivery_id)
            references.append(weakref.ref(item))
            return (item,)

        context = self.observer.wrap(
            context, "dlr.control.services.subscription_delivery._delivery_context"
        )

        def observed(session, delivery_id):
            nonlocal calls
            item = context(session, delivery_id)[0]
            calls += 1
            if calls == 8:
                item.state = "succeeded"
            session.flush()

        observed = self.observer.wrap(
            observed, "dlr.control.services.subscription_delivery.observe_terminal"
        )
        committed = self.observer.wrap(
            Session.commit, "sqlalchemy.orm.session.Session.commit"
        )
        pending = observe._BOUNDARY_COMMIT.set(None)
        try:
            with Session(engine, expire_on_commit=False) as session:
                session.add(
                    EventDelivery(
                        id=9, receipt_id=8, execution_id=44, state="processing"
                    )
                )
                session.commit()
                for _ in range(8):
                    self.assertIsNone(observed(session, 9))
                    # Nothing, including the diagnostic span, retains this row
                    # once the original function and its wrapper have returned.
                    self.assertIsNone(references[-1]())
                    committed(session)
        finally:
            observe._BOUNDARY_COMMIT.reset(pending)
            engine.dispose()
        boundaries = [
            row
            for row in self.rows()
            if row.get("phase") == "end" and row.get("boundary_kind") == "terminal"
        ]
        self.assertEqual(len(boundaries), 2)
        self.assertEqual([row["boundary_commit"] for row in boundaries], [False, True])
        self.assertTrue(boundaries[-1]["commit_succeeded"])
        self.assertTrue(
            all(row["responsibility_state"] == "succeeded" for row in boundaries)
        )
        self.assertFalse(
            any(
                row.get("outcome") == "failed"
                for row in self.rows()
                if row["kind"] == "hook"
            )
        )

    def test_duration_buckets_are_exact_including_boundaries_and_flush(self):
        durations = [0, 100_000, 100_001, 250_000, 5_000_000_000, 5_000_000_001]
        for duration in durations:
            self.observer.aggregate("timing", duration, False)
        self.observer.flush()
        self.observer.aggregate("timing", 1_000_000, True)
        aggregates = [row for row in self.rows() if row["kind"] == "aggregate"]
        first, last = aggregates
        self.assertEqual(first["count"], len(durations))
        self.assertEqual(first["sum_ns"], sum(durations))
        self.assertEqual(first["bucket_counts"][0], 2)
        self.assertEqual(first["bucket_counts"][1], 2)
        self.assertEqual(first["bucket_counts"][-2:], [1, 1])
        self.assertEqual(sum(first["bucket_counts"]), len(durations))
        self.assertEqual(last["count"], 1)
        self.assertEqual(last["errors"], 1)
        self.assertEqual(sum(last["bucket_counts"]), 1)

    def test_role_budget_is_shared_by_writers_and_has_explicit_marker(self):
        left = observe.Writer(self.temp.name, "control", max_bytes=1536)
        right = observe.Writer(self.temp.name, "control", max_bytes=1536)
        for n in range(100):
            (left if n % 2 else right).write(
                {"kind": "test", "n": n, "padding": "x" * 100}
            )
        path = Path(self.temp.name) / "control.jsonl"
        self.assertLessEqual(path.stat().st_size, 1536)
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertTrue(any(row["kind"] == "budget_exhausted" for row in rows))
        self.assertGreater(left.dropped + right.dropped, 0)

    def test_no_arguments_exception_text_sql_or_secrets_reach_disk(self):
        secret = "SENTINEL_CREDENTIAL_BODY_SQL_SECRET"

        def original(payload, *, token):
            raise ValueError(secret)

        with self.assertRaises(ValueError):
            self.observer.wrap(original, "privacy")(
                {"execution_id": 4, "body": secret, "sql": secret, "secrets": secret},
                token=secret,
            )
        content = (Path(self.temp.name) / "control.jsonl").read_text()
        self.assertNotIn(secret, content)
        self.assertIn('"execution_id":4', content)

    def test_aliases_receive_same_wrapper_without_product_import(self):
        name = "dlr.control.services.subscription_queue"
        alias_name = "dlr.example_importer"
        module = types.ModuleType(name)
        importer = types.ModuleType(alias_name)

        def take_custody(context, message):
            return message

        module.take_custody = take_custody
        module.envelope = lambda *args: None
        module.capacity_ready = lambda *args: True
        importer.alias = take_custody
        with patch.dict(sys.modules, {name: module, alias_name: importer}):
            observe._patch(self.observer, module)
            self.assertIs(importer.alias, module.take_custody)
            token = object()
            self.assertIs(importer.alias(None, token), token)

    def test_diagnostic_write_failure_does_not_mask_business_result(self):
        path = Path(self.temp.name) / "not-a-directory"
        path.write_text("file")
        observer = observe.Observer(observe.Writer(path, "worker"))
        marker = object()
        self.assertIs(observer.wrap(lambda: marker, "write-failed")(), marker)
        self.assertGreater(observer.writer.errors, 0)

    @unittest.skipUnless(
        importlib.util.find_spec("sqlalchemy") and importlib.util.find_spec("pika"),
        "requires existing SQLAlchemy/Pika libraries",
    )
    def test_lazy_import_real_sqlalchemy_pool_commit_and_pika_hooks(self):
        path = Path(__file__).parent.resolve()
        target = Path(self.temp.name) / "libraries"
        env = dict(os.environ)
        env.update(
            {
                "PYTHONPATH": str(path),
                "DLR_B0_ROLE": "control",
                "DLR_B0_DIAGNOSTICS_DIR": str(target),
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        script = """
import sys
import sqlalchemy,pika
from sqlalchemy.orm import Session
from sqlalchemy.pool import QueuePool
engine=sqlalchemy.create_engine('sqlite://',poolclass=QueuePool)
with Session(engine) as session:
    assert session.scalar(sqlalchemy.text('SELECT :secret'),{'secret':'SENTINEL_LIBRARY_SECRET'})=='SENTINEL_LIBRARY_SECRET'
    session.commit()
assert not any(x.startswith('dlr') for x in sys.modules)
"""
        subprocess.check_call([sys.executable, "-c", script], env=env)
        content = (target / "control.jsonl").read_text()
        self.assertNotIn("SENTINEL_LIBRARY_SECRET", content)
        rows = [json.loads(line) for line in content.splitlines()]
        self.assertTrue(any(row.get("kind") == "sql" for row in rows))
        hooks = {row["target"]: row["outcome"] for row in rows if row["kind"] == "hook"}
        for name in (
            "sqlalchemy.pool.impl.QueuePool._do_get",
            "sqlalchemy.orm.session.Session.commit",
            "pika.adapters.blocking_connection.BlockingChannel.basic_ack",
            "sqlalchemy.engine.Engine.events",
        ):
            self.assertEqual(hooks.get(name), "installed", name)
        self.assertFalse(
            any(
                row.get("outcome") in {"missing", "failed", "unsupported"}
                for row in rows
                if row["kind"] == "hook"
            )
        )


if __name__ == "__main__":
    unittest.main()
