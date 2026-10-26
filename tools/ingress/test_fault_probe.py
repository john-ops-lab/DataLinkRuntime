"""Subprocess-only injection mechanics; no database/message service is contacted."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / "tools" / "ingress"

ACTOR = r"""
import base64,json,os,sys,types,uuid
from pathlib import Path
from types import SimpleNamespace
import fault_probe
scenario=sys.argv[1];root=Path(sys.argv[2]);probe=fault_probe.install()
envelope={"subscription":{"id":"11"},"source":{"type":"http"},"data":{"payload":{"run_id":"fault-unit","message_id":"000001","private_body":"never-log-this-body"}}}
if "rabbit" in scenario:
 envelope["source"]["type"]="rabbitmq"
 envelope["data"]={"encoding":"base64","value":base64.b64encode(json.dumps(envelope["data"]["payload"]).encode()).decode()}
execution_input=envelope
if "batch" in scenario:
 members=[]
 for index in range(101 if "101" in scenario else 20):
  value=json.loads(json.dumps(envelope))
  if value["source"]["type"]=="http":value["data"]["payload"]["message_id"]="batch-message-"+str(index+1)
  else:
   item=json.loads(base64.b64decode(value["data"]["value"]))
   item["message_id"]="batch-message-"+str(index+1)
   value["data"]["value"]=base64.b64encode(json.dumps(item).encode()).decode()
  members.append({"receipt_id":index+1,"delivery_id":index+101,"delivery_ordinal":1,"member_index":index,"input":value})
 execution_input={"schema":"dlr.event.batch.v1","batch_id":"b"*32,"manifest_hash":"a"*64,"subscription":{"id":"11","config_version":1,"source_epoch":"epoch"},"members":members}
 if "mixed-run" in scenario:members[1]["input"]["data"]["payload"]["run_id"]="different-run"
 if "oversized" in scenario:members[0]["input"]["data"]["payload"]["padding"]="x"*262144
if scenario=="worker-opaque-input":execution_input={"opaque":True}
if scenario.startswith("t1"):
 from sqlalchemy import create_engine,text,event
 from sqlalchemy.orm import Session
 engine=create_engine("sqlite:///"+str(root/"database.sqlite"))
 @event.listens_for(engine,"connect")
 def configure(connection,_):connection.execute("PRAGMA foreign_keys=ON")
 with engine.begin() as connection:
  connection.exec_driver_sql("CREATE TABLE IF NOT EXISTS outcome(id INTEGER PRIMARY KEY,note TEXT)")
  connection.exec_driver_sql("CREATE TABLE IF NOT EXISTS parent(id INTEGER PRIMARY KEY)")
  connection.exec_driver_sql("CREATE TABLE IF NOT EXISTS child(id INTEGER REFERENCES parent(id) DEFERRABLE INITIALLY DEFERRED)")
 module=types.ModuleType("dlr.control.services.subscription_delivery")
 def receive_event(session,subscription_id,owner,generation,value,shard,position):
  result=session.execute(text("INSERT INTO outcome(note) VALUES ('retained')"))
  if scenario=="t1-commit-fails":session.execute(text("INSERT INTO child(id) VALUES (999)"))
  return SimpleNamespace(id=result.lastrowid),False
 module.receive_event=receive_event
 if probe:probe.patch(module)
 unit_module=types.ModuleType("dlr.control.services.subscription_t1_unit")
 class T1Unit:
  def __init__(self,session):
   self.session=session;self.subscription=SimpleNamespace(id=11);self.source_type=envelope["source"]["type"]
  def receive(self,inputs):
   outcomes=[]
   if "dup-only" in scenario:self.session.execute(text("INSERT INTO outcome(note) VALUES ('old')"))
   for item in inputs:
    if "error-only" in scenario:
     outcomes.append(SimpleNamespace(index=item.index,error=RuntimeError("private_body"),duplicate=False,status_code=422,receipt_id=None));continue
    if "prefix" in scenario and item.index==1:
     outcomes.append(SimpleNamespace(index=item.index,error=RuntimeError("private_body"),duplicate=False,status_code=422,receipt_id=None));break
    if "independent" in scenario and item.index==1:
     outcomes.append(SimpleNamespace(index=item.index,error=RuntimeError("private_body"),duplicate=False,status_code=401,receipt_id=None));continue
    duplicate="dup-only" in scenario
    rowid=1 if duplicate else self.session.execute(text("INSERT INTO outcome(note) VALUES ('retained')")).lastrowid
    outcomes.append(SimpleNamespace(index=item.index,error=None,duplicate=duplicate,status_code=200 if duplicate else 202,receipt_id=rowid))
   if "commit-fails" in scenario:self.session.execute(text("INSERT INTO child(id) VALUES (999)"))
   return SimpleNamespace(outcomes=tuple(reversed(outcomes)) if "reversed" in scenario else tuple(outcomes))
 unit_module.T1Unit=T1Unit
 if probe:probe.patch(unit_module)
 with Session(engine) as session:
  if scenario.startswith("t1-unit"):
   items=[]
   for index in range(4):
    value=json.loads(json.dumps(envelope))
    if value["source"]["type"]=="http":value["data"]["payload"]["message_id"]="unit-"+str(index+1)
    else:
     item=json.loads(base64.b64decode(value["data"]["value"]))
     item["message_id"]="unit-"+str(index+1)
     value["data"]["value"]=base64.b64encode(json.dumps(item).encode()).decode()
    items.append(SimpleNamespace(index=index,envelope=value))
   unit_module.T1Unit(session).receive(items)
  else:module.receive_event(session,11,"owner",1,envelope,"requests",{})
  if "rollback" in scenario:session.rollback()
  try:session.commit()
  except Exception:
   if "commit-fails" not in scenario:raise
   session.rollback()
 (root/"source-ack-or-http-response").write_text("returned")
elif scenario.startswith("outbox"):
 import pika
 import pika.adapters.blocking_connection as blocking
 channel=object.__new__(blocking.BlockingChannel)
 channel._delivery_confirmation=scenario!="outbox-unconfirmed"
 channel._message_confirmation_result=blocking._CallbackResult(blocking.BlockingChannel._OnMessageConfirmationReportArgs)
 channel._puback_return=None
 def simulate_broker(**kwargs):
  if channel._delivery_confirmation:
   method=pika.spec.Basic.Nack(delivery_tag=1) if scenario=="outbox-nack" else pika.spec.Basic.Ack(delivery_tag=1)
   channel._message_confirmation_result.set_value_once(pika.frame.Method(1,method))
 channel._impl=SimpleNamespace(basic_publish=simulate_broker,channel_number=1)
 channel._flush_output=lambda *args:None
 module=types.ModuleType("dlr.control.services.outbox")
 row=SimpleNamespace(id=uuid.uuid4(),execution_id=7)
 def create_dispatch_outbox(session,execution):return row
 def _publish_row(connection,row):
  channel.basic_publish("dispatch","worker",b"private-message-body",properties=pika.BasicProperties(delivery_mode=2),mandatory=True)
  (root/"outbox-published-persisted").write_text("too-late")
 module.create_dispatch_outbox=create_dispatch_outbox;module._publish_row=_publish_row
 if probe:probe.patch(module)
 module.create_dispatch_outbox(None,SimpleNamespace(id=7,input=execution_input))
 try:module._publish_row(None,row)
 except pika.exceptions.NackError:pass
else:
 from dlr.worker.client import ControlClient
 from dlr.worker import workspace
 client=ControlClient("http://unreachable.invalid","never-log-this-token")
 payload={"protocol_version":3,"execution_id":7,"attempt_id":9,"attempt_no":1,"fencing_token":3,"input":execution_input}
 def request(method,path,body=None,**kwargs):
  if path.endswith("/v3/claim"):
   decision="ACK_NOOP" if scenario=="worker-no-claim" else "EXECUTE"
   return json.dumps({"decision":decision,"payload":payload}).encode()
  (root/"control-result-rpc-started").write_text("network-call-would-start")
  return b'{}'
 client._expect=request
 client.claim_v3(1,{"execution_id":7})
 journal=root/"journal"
 workspace.write_attempt_journal(journal,execution_id=7,attempt_id=9,attempt_no=1,fencing_token=3,lease_expires_at="2030-01-01T00:00:00+00:00",workspace=root/"workspace",claim_token="never-log-this-claim",cleanup_token="never-log-this-cleanup")
 (root/"execution-started").write_text("not-before-journal-cut")
 output={"run_id":"fault-unit","message_id":"000001","payload_sha256":"a"*64,"target_outcome":"inserted","timing":{"target_commit_returned_monotonic_ns":123456789}}
 if "batch" in scenario:
  output={"schema":"dlr.event.batch.result.v1","batch_id":execution_input["batch_id"],"manifest_hash":execution_input["manifest_hash"],"members":[{**{key:member[key] for key in ("receipt_id","delivery_id","delivery_ordinal","member_index")},"status":"succeeded","error_code":None} for member in execution_input["members"]]}
  if "invalid-result" in scenario:output["members"][1]["delivery_id"]=999999
  if "unknown-result" in scenario:
   for index,item in enumerate(output["members"]):item.update(status="unknown" if index==0 else "not_run",error_code="target_commit_unknown" if index==0 else "predecessor_failed")
 if "opaque" in scenario:output={"opaque":True}
 client.result_attempt(1,9,{"attempt_id":9,"claim_token":"never-log-this-claim","status":"succeeded","output":output,"output_truncated":"truncated" in scenario})
"""


class FaultProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.root.chmod(0o700)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def arm(
        self, point: str, *, mode: str = "reference-single-v1", **match: object
    ) -> None:
        role = "control" if point.startswith(("t1_", "outbox_")) else "worker"
        value = {
            "schema": "dlr.ingress-fault-arm/v1",
            "arm_id": "test-cut",
            "point": point,
            "role": role,
            "match": {"subscription_id": 11, "run_id": "fault-unit", **match},
            "result_proof_mode": mode,
        }
        (self.root / "arm.json").write_text(json.dumps(value))
        (self.root / "arm.json").chmod(0o600)

    def child(
        self, scenario: str, *, enabled: bool = True, observe: bool = False
    ) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ)
        for key in list(env):
            if key.startswith(("DLR_INGRESS_FAULT_", "DLR_B0_")):
                env.pop(key)
        point = json.loads((self.root / "arm.json").read_text())["point"]
        role = "control" if point.startswith(("t1_", "outbox_")) else "worker"
        env["PYTHONPATH"] = os.pathsep.join(
            [
                str(TOOLS / "fault_sitecustomize"),
                str(TOOLS),
                str(ROOT / "tools/ingress_b0"),
                str(ROOT / "backend/src"),
            ]
        )
        if enabled:
            env.update(
                DLR_INGRESS_FAULT_ENABLED="1",
                DLR_INGRESS_FAULT_SCOPE="issue222-isolated",
                DLR_INGRESS_FAULT_ROLE=role,
                DLR_INGRESS_FAULT_ARM=str(self.root / "arm.json"),
            )
        if observe:
            env.update(
                DLR_B0_ROLE=role, DLR_B0_DIAGNOSTICS_DIR=str(self.root / "observation")
            )
        return subprocess.run(
            [sys.executable, "-c", ACTOR, scenario, str(self.root)],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

    def proof(self) -> dict[str, object]:
        raw = (self.root / "fault-test-cut.json").read_text()
        self.assertNotIn("never-log", raw)
        self.assertNotIn("private_body", raw)
        self.assertNotIn("private-message-body", raw)
        self.assertNotIn(str(self.root), raw)
        return json.loads(raw)

    def rows(self) -> int:
        with sqlite3.connect(self.root / "database.sqlite") as connection:
            return connection.execute("SELECT count(*) FROM outcome").fetchone()[0]

    def test_before_commit_kills_child_and_real_transaction_is_not_durable(
        self,
    ) -> None:
        self.arm("t1_before_commit")
        child = self.child("t1")
        self.assertEqual(child.returncode, 137, child.stderr)
        self.assertEqual(self.rows(), 0)
        self.assertIs(self.proof()["proof"]["transaction_committed"], False)
        self.assertFalse((self.root / "source-ack-or-http-response").exists())

    def test_after_commit_kills_child_after_real_commit_and_before_response(
        self,
    ) -> None:
        self.arm("t1_after_commit")
        child = self.child("t1")
        self.assertEqual(child.returncode, 137, child.stderr)
        self.assertEqual(self.rows(), 1)
        self.assertIs(self.proof()["proof"]["transaction_committed"], True)
        self.assertFalse((self.root / "source-ack-or-http-response").exists())

    def test_same_arm_is_exclusive_across_restart(self) -> None:
        self.arm("t1_after_commit")
        self.assertEqual(self.child("t1").returncode, 137)
        first = (self.root / "fault-test-cut.json").read_bytes()
        child = self.child("t1")
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertEqual(self.rows(), 2)
        self.assertEqual(first, (self.root / "fault-test-cut.json").read_bytes())

    def test_default_disabled_does_not_change_commit_or_create_marker(self) -> None:
        self.arm("t1_after_commit")
        child = self.child("t1", enabled=False)
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertEqual(self.rows(), 1)
        self.assertFalse((self.root / "fault-test-cut.json").exists())

    def test_wrong_run_does_not_kill_unrelated_work(self) -> None:
        self.arm("t1_after_commit", run_id="another-run")
        child = self.child("t1")
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertFalse((self.root / "fault-test-cut.json").exists())

    def test_failed_real_commit_is_not_misreported_as_after_commit(self) -> None:
        self.arm("t1_after_commit")
        child = self.child("t1-commit-fails")
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertEqual(self.rows(), 0)
        self.assertFalse((self.root / "fault-test-cut.json").exists())

    def test_rollback_removes_pending_commit_cut(self) -> None:
        self.arm("t1_after_commit")
        child = self.child("t1-rollback")
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertEqual(self.rows(), 0)
        self.assertFalse((self.root / "fault-test-cut.json").exists())

    def test_real_pika_confirm_path_requires_ack_callback_before_cut(self) -> None:
        self.arm("outbox_after_confirm")
        child = self.child("outbox-ack")
        self.assertEqual(child.returncode, 137, child.stderr)
        proof = self.proof()["proof"]
        self.assertIs(proof["broker_basic_ack_observed"], True)
        self.assertIs(proof["outbox_mark_published_called"], False)
        self.assertFalse((self.root / "outbox-published-persisted").exists())

    def test_unconfirmed_return_is_not_a_confirm_and_nack_never_cuts(self) -> None:
        self.arm("outbox_after_confirm")
        for scenario in ("outbox-unconfirmed", "outbox-nack"):
            with self.subTest(scenario=scenario):
                child = self.child(scenario)
                self.assertEqual(child.returncode, 0, child.stderr)
                self.assertFalse((self.root / "fault-test-cut.json").exists())

    def test_claim_cut_follows_actual_journal_file_and_directory_sync(self) -> None:
        self.arm("worker_after_claim_journal")
        child = self.child("worker-claim")
        self.assertEqual(child.returncode, 137, child.stderr)
        self.assertTrue((self.root / "journal/attempt-9.attempt.json").is_file())
        self.assertFalse((self.root / "execution-started").exists())
        proof = self.proof()["proof"]
        self.assertIs(proof["journal_file_fsynced"], True)
        self.assertIs(proof["journal_directory_fsynced"], True)

    def test_journal_without_execute_claim_does_not_qualify(self) -> None:
        self.arm("worker_after_claim_journal")
        child = self.child("worker-no-claim")
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertFalse((self.root / "fault-test-cut.json").exists())

    def test_target_cut_is_pre_rpc_evidence_only_and_requires_independent_target_oracle(
        self,
    ) -> None:
        self.arm("target_after_commit_before_result")
        child = self.child("worker-result")
        self.assertEqual(child.returncode, 137, child.stderr)
        self.assertFalse((self.root / "control-result-rpc-started").exists())
        proof = self.proof()
        self.assertIs(proof["diagnostic_receipt_fsynced"], True)
        self.assertIs(proof["proof"]["product_result_journal"], False)
        self.assertIs(proof["proof"]["target_commit_verified"], False)
        evidence = (self.root / "fault-test-cut.result-evidence.json").read_text()
        self.assertNotIn("never-log", evidence)
        self.assertNotIn("private_body", evidence)
        self.assertEqual(
            json.loads(evidence)["authority"], "evidence_only_not_product_recovery"
        )

    def test_opaque_output_requires_explicit_execution_and_independent_oracle_mode(
        self,
    ) -> None:
        self.arm(
            "target_after_commit_before_result", mode="external-oracle", execution_id=7
        )
        child = self.child("worker-opaque")
        self.assertEqual(child.returncode, 137, child.stderr)
        self.assertEqual(self.proof()["proof"]["result_proof_mode"], "external-oracle")

    def test_batch_can_be_armed_before_dispatch_by_authoritative_subscription_and_run(self) -> None:
        self.arm("target_after_commit_before_result", mode="external-oracle")
        child = self.child("worker-batch-opaque")
        self.assertEqual(child.returncode, 137, child.stderr)
        proof = self.proof()
        self.assertEqual(proof["ids"]["execution_id"], 7)
        self.assertEqual(proof["ids"]["batch_member_count"], 20)
        self.assertEqual(proof["proof"]["identity_binding"], "claim_input_batch")
        self.assertIs(proof["proof"]["independent_target_oracle_required"], True)
        self.assertIs(proof["proof"]["target_commit_verified"], False)

    def test_strict_batch_result_uses_input_manifest_not_output_run_self_description(self) -> None:
        self.arm("target_after_commit_before_result", mode="reference-batch-v1", message_id="batch-message-20")
        child = self.child("worker-batch-rabbit")
        self.assertEqual(child.returncode, 137, child.stderr)
        proof = self.proof()
        self.assertEqual(proof["ids"]["message_id"], "batch-message-20")
        self.assertEqual(proof["ids"]["source_type"], "rabbitmq")
        self.assertIs(proof["proof"]["batch_result_validated"], True)
        self.assertIs(proof["proof"]["adapter_commit_marker_present"], False)
        self.assertIs(proof["proof"]["product_result_journal"], False)

    def test_batch_claim_journal_and_outbox_cuts_keep_current_real_hooks(self) -> None:
        for point, scenario in [("worker_after_claim_journal", "worker-batch"), ("outbox_after_confirm", "outbox-batch-ack")]:
            with self.subTest(point=point), tempfile.TemporaryDirectory() as directory:
                original = self.root
                self.root = Path(directory)
                self.root.chmod(0o700)
                try:
                    self.arm(point)
                    child = self.child(scenario)
                    self.assertEqual(child.returncode, 137, child.stderr)
                    self.assertEqual(self.proof()["ids"]["batch_member_count"], 20)
                finally:
                    self.root = original

    def test_invalid_or_incomplete_batch_never_claims_post_commit_cut(self) -> None:
        for scenario in ("worker-batch-invalid-result", "worker-batch-unknown-result", "worker-batch-mixed-run", "worker-batch-oversized", "worker-batch-101", "worker-batch-truncated"):
            with self.subTest(scenario=scenario):
                self.arm("target_after_commit_before_result", mode="reference-batch-v1")
                child = self.child(scenario)
                self.assertEqual(child.returncode, 0, child.stderr)
                self.assertFalse((self.root / "fault-test-cut.json").exists())

    def test_batch_wrong_run_and_nonbatch_input_do_not_qualify_as_strict_batch(self) -> None:
        for scenario, match in [("worker-batch", {"run_id": "different-run"}), ("worker-result", {})]:
            with self.subTest(scenario=scenario):
                self.arm("target_after_commit_before_result", mode="reference-batch-v1", **match)
                child = self.child(scenario)
                self.assertEqual(child.returncode, 0, child.stderr)
                self.assertFalse((self.root / "fault-test-cut.json").exists())

    def test_t1_rabbit_envelope_cuts_before_any_ack_for_both_commit_sides(self) -> None:
        for point in ("t1_before_commit", "t1_after_commit"):
            with self.subTest(point=point), tempfile.TemporaryDirectory() as directory:
                original = self.root
                self.root = Path(directory)
                self.root.chmod(0o700)
                try:
                    self.arm(point)
                    child = self.child("t1-rabbit")
                    self.assertEqual(child.returncode, 137, child.stderr)
                    self.assertFalse((self.root / "source-ack-or-http-response").exists())
                finally:
                    self.root = original

    def test_t1_unit_four_members_bind_selected_new_outcome_to_same_commit(self) -> None:
        for point, expected in [("t1_before_commit", 0), ("t1_after_commit", 4)]:
            with self.subTest(point=point), tempfile.TemporaryDirectory() as directory:
                original = self.root
                self.root = Path(directory)
                self.root.chmod(0o700)
                try:
                    self.arm(point, message_id="unit-4")
                    child = self.child("t1-unit-reversed")
                    self.assertEqual(child.returncode, 137, child.stderr)
                    self.assertEqual(self.rows(), expected)
                    proof = self.proof()
                    self.assertEqual(proof["ids"]["message_id"], "unit-4")
                    self.assertEqual(proof["ids"]["receipt_id"], 4)
                    self.assertEqual(proof["proof"]["reception_path"], "T1Unit.receive")
                    self.assertEqual(proof["proof"]["t1_unit_new_items"], 4)
                    self.assertFalse((self.root / "source-ack-or-http-response").exists())
                finally:
                    self.root = original

    def test_t1_unit_prefix_and_http_error_only_mark_successful_new_items(self) -> None:
        for scenario, expected in [("t1-unit-rabbit-prefix", 1), ("t1-unit-independent", 3)]:
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as directory:
                original = self.root
                self.root = Path(directory)
                self.root.chmod(0o700)
                try:
                    self.arm("t1_after_commit")
                    child = self.child(scenario)
                    self.assertEqual(child.returncode, 137, child.stderr)
                    self.assertEqual(self.rows(), expected)
                    self.assertEqual(self.proof()["proof"]["t1_unit_new_items"], expected)
                finally:
                    self.root = original

    def test_t1_unit_duplicates_and_errors_never_arm_a_new_responsibility_fault(self) -> None:
        for scenario, rows in [("t1-unit-dup-only", 1), ("t1-unit-error-only", 0)]:
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as directory:
                original = self.root
                self.root = Path(directory)
                self.root.chmod(0o700)
                try:
                    self.arm("t1_after_commit")
                    child = self.child(scenario)
                    self.assertEqual(child.returncode, 0, child.stderr)
                    self.assertEqual(self.rows(), rows)
                    self.assertFalse((self.root / "fault-test-cut.json").exists())
                finally:
                    self.root = original

    def test_t1_unit_commit_failure_and_rollback_never_mark_after_commit(self) -> None:
        for scenario in ("t1-unit-commit-fails", "t1-unit-rollback"):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as directory:
                original = self.root
                self.root = Path(directory)
                self.root.chmod(0o700)
                try:
                    self.arm("t1_after_commit")
                    child = self.child(scenario)
                    self.assertEqual(child.returncode, 0, child.stderr)
                    self.assertEqual(self.rows(), 0)
                    self.assertFalse((self.root / "fault-test-cut.json").exists())
                finally:
                    self.root = original

    def test_import_hooks_compose_with_frozen_b0_observer(self) -> None:
        self.arm("t1_after_commit")
        child = self.child("t1", observe=True)
        self.assertEqual(child.returncode, 137, child.stderr)
        self.assertTrue((self.root / "observation/control.jsonl").exists())
        self.assertIs(self.proof()["proof"]["transaction_committed"], True)

    def test_fifo_arm_is_rejected_without_blocking_the_process(self) -> None:
        fifo = self.root / "arm-fifo"
        os.mkfifo(fifo, 0o600)
        env = {
            **os.environ,
            "PYTHONPATH": str(TOOLS),
            "DLR_INGRESS_FAULT_ENABLED": "1",
            "DLR_INGRESS_FAULT_SCOPE": "issue222-isolated",
            "DLR_INGRESS_FAULT_ROLE": "control",
            "DLR_INGRESS_FAULT_ARM": str(fifo),
        }
        child = subprocess.run(
            [
                sys.executable,
                "-c",
                "import fault_probe; assert fault_probe.install().arm() is None",
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        self.assertEqual(child.returncode, 0, child.stderr)

    def test_duplicate_arm_field_is_rejected(self) -> None:
        self.arm("t1_after_commit")
        path = self.root / "arm.json"
        raw = path.read_text()
        path.write_text(raw[:-1] + ',"role":"control"}')
        child = self.child("t1")
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertFalse((self.root / "fault-test-cut.json").exists())

    def test_nonprivate_arm_does_not_kill(self) -> None:
        self.arm("t1_after_commit")
        (self.root / "arm.json").chmod(0o644)
        child = self.child("t1")
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertFalse((self.root / "fault-test-cut.json").exists())


if __name__ == "__main__":
    unittest.main()
