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
