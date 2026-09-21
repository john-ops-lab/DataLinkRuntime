#!/usr/bin/env python3
"""Read-only evidence verifier for an explicit local-preview carry-forward.

The controller passes only private files to this program.  Successful output is
limited to canonical hashes, counts and stable codes; raw database values and
journal tokens never reach stdout.
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import datetime as dt
import decimal
import gc
import hashlib
import hmac
import ipaddress
import json
import math
import os
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import quote, unquote, urlsplit

FORMAT_VERSION = 2
AUDITED_FORMAT_VERSION = 3
AUDITED_MODE = "audited-web-same-schema-v1"
GROUP2_FORMAT_VERSION = 4
GROUP2_MODE = "audited-group2-same-schema-v1"
GROUP2_REVIEW_SCHEMA = "group2-reviewed-scope-v1"
GROUP2_FROM_SHA = "3d3ad7bff1cedde321628f217daafd130945e1d0"
GROUP2_PRODUCT_SHA = "1c698e04bf9c388b8d6812c3bc45c7fb836230c7"
GROUP2_FROM_TREE = "40fa62ef17a31f56c1eb319459d01c7086f0529e"
GROUP2_PRODUCT_TREE = "b7aabc5dacdcc67988cacfd033a2d98808bcec97"
GROUP2_PRODUCT_RAW_DIGEST = (
    "08a602c5183922f98a67af2e0f28c3f88a92f91ea3be4ad0d336ad6f8970f3f0"
)
GROUP2_REQUEST_DIGEST = (
    "ec0dfbeee43216ffc0e97498d209c3afc0c82742c3d041fa54a6d52464240b9e"
)
MANIFEST_ID = re.compile(r"[0-9a-f]{32}")
SHA = re.compile(r"[0-9a-f]{40}")
DIGEST = re.compile(r"[0-9a-f]{64}")
TERMINAL_EXECUTIONS = {"succeeded", "dead_letter", "cancelled", "expired"}
ACTIVE_ATTEMPTS = {"claimed", "running"}
TERMINAL_ATTEMPTS = {
    "succeeded",
    "failed",
    "timed_out",
    "cancelled",
    "worker_lost",
    "resource_exceeded",
}
RESPONSIBILITY_TABLES = (
    "executions",
    "execution_attempts",
    "adapter_execution_slots",
    "execution_infrastructure_incidents",
    "execution_outbox",
    "adapter_execution_admission",
    "global_execution_admission",
    "execution_input_artifact_leases",
    "execution_artifact_holds",
    "execution_credential_binding_snapshots",
    "execution_idempotency_records",
    "schedule_dispatch_outcomes",
    "worker_cleanup_requests",
)
AUDIT_TABLE = "execution_incident_dispositions"
AUDIT_COLUMNS = (
    "id",
    "incident_id",
    "execution_id",
    "idempotency_key",
    "request_hash",
    "actor_kind",
    "user_id",
    "action",
    "reason_code",
    "outcome",
    "code",
    "from_generation",
    "to_generation",
    "from_outbox_id",
    "to_outbox_id",
    "execution_status",
    "created_at",
)
AUDITED_TABLES = (*RESPONSIBILITY_TABLES, AUDIT_TABLE)
ASSET_TABLES = (
    "adapters",
    "adapter_versions",
    "adapter_permissions",
    "adapter_credential_bindings",
    "adapter_input_configs",
    "adapter_input_artifact_bindings",
    "adapter_schedules",
    "adapter_webhooks",
    "credentials",
    "user_templates",
    "users",
    "package_sources",
    "builtin_package_settings",
    "builtin_package_uploads",
    "builtin_packages",
    "managed_input_artifacts",
    "system_settings",
    "knowledge_source_settings",
    "ai_custom_providers",
    "ai_model_settings",
    "user_sessions",
)
GROUP2_CONTROLLER_PATHS = {
    "tools/local-preview/preview.py": "100755",
    "tools/local-preview/deploy.sh": "100755",
    "tools/local-preview/carry_forward.py": "100644",
    "tools/local-preview/tests/test_preview.py": "100644",
    "tools/local-preview/tests/test_carry_forward.py": "100644",
    "docs/zh-CN/local-preview.md": "100644",
    "openspec/changes/issue161-product-correctness/design.md": "100644",
    "openspec/changes/issue161-product-correctness/proposal.md": "100644",
    "openspec/changes/issue161-product-correctness/tasks.md": "100644",
    "openspec/changes/issue161-product-correctness/specs/incident-preserving-upgrade/spec.md": "100644",
}
GROUP2_CONTROLLER_FILES = {
    "preview.py",
    "migrations.py",
    "deploy.sh",
    "verify.py",
    "assets.py",
    "carry_forward.py",
}
GROUP2_RECONCILE_EVIDENCE_FILES = {
    "failed-manifest.json",
    "first-startup.json",
    "prior-success.json",
    "authority.json",
    "platform.json",
    "source-review.json",
    "ci.json",
}
GROUP2_RECONCILE_ACTIONS = [
    "incident-tool-stage",
    "restore-prior-software",
    "reconcile-control-plane",
]
GROUP2_PARTIAL_INCIDENT_ID = "04deee8840a94841bc7d9bfc91b5e95c"
GROUP2_PARTIAL_ORIGINAL_TOOL_SHA = "04f7a2476cdbd4cdddf7a7b810bd577490f44ce9"
GROUP2_PARTIAL_ORIGINAL_REQUEST_DIGEST = (
    "63f6ac4d2323ecea2d5345153b19862d4ab52c38df79058db00c396e8afe3635"
)
GROUP2_PARTIAL_FAILED_SHA = "42aab1cadec97394458b21b0e4521bb0f523e8c0"
GROUP2_PARTIAL_NONCE = "a3a4b6c1c3ed4e8d8d80a4402396b678"
GROUP2_PARTIAL_WORKER_ID = (
    "3ddf3dc64baa04bf66ecbabe963288d669a294510f15616156c94bc910e4e417"
)
GROUP2_PARTIAL_FINALIZE_FILES = {
    "original-incident.json", "partial-attempt.json", "diagnostic.json",
    "scope-acceptance.json", "source-review.json", "ci.json",
}
GROUP2_PARTIAL_FINALIZE_ACTIONS = [
    "partial-finalize-tool-stage",
    "read-current-preservation",
    "finalize-control-plane",
]
GROUP2_POST_FINALIZE_REBOOT_FILES = {
    "parent-finalize.json",
    "stopped-platform.json",
    "prior-success.json",
    "source-review.json",
    "scope-approval.json",
}
GROUP2_POST_FINALIZE_REBOOT_ACTIONS = [
    "stage-isolated-reboot-tool",
    "prepare-bound-sandbox-keeper",
    "start-bound-postgres-rabbitmq",
    "start-bound-control-worker-web",
    "persist-post-finalize-reboot-source",
]
GROUP2_POST_FINALIZE_REBOOT_PHASES = (
    "stopped",
    "keeper_ready",
    "database_ready",
    "applications_started",
    "verified",
    "host_verified",
)
GROUP2_POST_FINALIZE_REBOOT_LOOPS = {
    "demo_bootstrap",
    "scheduler",
    "retention",
    "artifact_gc",
    "orphan_audit",
    "admission",
    "attempt",
    "infrastructure_dlq",
    "topology",
    "outbox",
    "worker_startup",
}
GROUP2_POST_FINALIZE_REBOOT_SOURCES = {
    "demo_bootstrap": {"backend/src/dlr/control/app.py": "4151e3b52931223cd3a85be51ee19eb4293e6cc2956c9057058807f4f1d49233"},
    "scheduler": {"backend/src/dlr/control/app.py": "4151e3b52931223cd3a85be51ee19eb4293e6cc2956c9057058807f4f1d49233"},
    "retention": {
        "backend/src/dlr/control/services/retention.py": "d05f239b6bb61dece95fecbbda895621847fd65198ff9bae0f6e071120a1a1a7",
        "backend/src/dlr/control/services/idempotency.py": "4182eca3239a8f8b1d22f7edd1c42a420de73422e488cf007488f7f3f730bf3b",
    },
    "artifact_gc": {
        "backend/src/dlr/control/services/managed_input_gc.py": "886990b07dcfce78636cd24a64e3cbf7481a3f246f6323ad27e859cace99eabb",
        "backend/src/dlr/control/services/managed_input_upload.py": "ddbda206b69283754ed35ecf080530e00227d70b341b9ed6b32077969bc1f52b",
    },
    "orphan_audit": {
        "backend/src/dlr/control/services/managed_input_gc.py": "886990b07dcfce78636cd24a64e3cbf7481a3f246f6323ad27e859cace99eabb",
        "backend/src/dlr/control/services/artifact_store.py": "06e65580af705e0516a5427be8f91dcd76abbfa0dd9b0a58c08b62b41a793562",
    },
    "admission": {"backend/src/dlr/control/services/admission.py": "4af0a4caeece03cc1503d96836bb10b47ab528a72771f1641ea5acfc3eaf541b"},
    "attempt": {"backend/src/dlr/control/services/attempt.py": "15fe7fa972b606c496a63360dc70240aa5399394bfe0e1e96340f959247c6269"},
    "infrastructure_dlq": {"backend/src/dlr/control/services/rabbitmq.py": "91e2182448d6908bd4eb21b9f7acf48a55ebb55670a3f6dbba107b1b088b0b3e"},
    "topology": {"backend/src/dlr/control/services/rabbitmq.py": "91e2182448d6908bd4eb21b9f7acf48a55ebb55670a3f6dbba107b1b088b0b3e"},
    "outbox": {"backend/src/dlr/control/services/outbox.py": "ec1ad2df985c556d9c438d9662f856609fd55b71919d4ac12d6f3f79fac9018e"},
    "worker_startup": {
        "backend/src/dlr/worker/agent.py": "bd47921f231c8057f7ec06222c0d0e83e2878352ba35a9faea8cae50fad4283d",
        "backend/src/dlr/worker/workspace.py": "954dc3fb1207f78698a891217b05a3ff268c43f8d6ce7590eca176c032bb0289",
    },
    "configuration": {
        "backend/src/dlr/common/config.py": "b88ea4297e634b98846949da2014292f5c7e0399761bdffe0309b4fad1090d4e",
        "backend/src/dlr/worker/consumer.py": "1afba22fdc2704389acf77b7330e0ac3f10a1751c22f7bd12abe218118a50315",
        "backend/src/dlr/control/services/dispatch.py": "cd7c827258a7c6f87bd4851df843d01ff16a2f3428cb0ebb04f0525128e79ba7",
        "backend/src/dlr/control/services/infrastructure_dlq.py": "cb39ef8c7df3d316601c0aa4e968cc9a7bed3ab48455437e5be912f1eca256e5",
    },
}
GROUP2_POST_FINALIZE_REBOOT_PREPARE_SHA256 = (
    "bc0ae4442c9ecfa37e7aef2d655da223f648d5658a8fed96fb7ff599d7aabe7c"
)
GROUP2_POST_FINALIZE_REBOOT_HOST_CONTROL_FILES = {
    "state.json", "config.json", "attention.json", "preview.env",
    "installation.json", "prepare-sandbox-host.sh",
}
GROUP2_POST_FINALIZE_REBOOT_VM_CONTROL_FILES = {
    "current-sha", "transaction.json", "deployment.json", "preview.env",
    "build.env", "compose.local.json", "prepare-sandbox-host.sh",
}
GROUP2_POST_FINALIZE_REBOOT_HOST_FINALIZE_FILES = {
    "USER-APPROVAL.txt", "abandonment.json", "approval.json", "chain.json",
    "host-authority.json", "phase.json", "preservation-snapshot.json",
    "receipt.json", "request.json", "result.json",
}
GROUP2_POST_FINALIZE_REBOOT_VM_FINALIZE_FILES = {
    "USER-APPROVAL.txt", "approval.json", "current.json", "host-authority.json",
    "host-validated.json", "phase.json", "receipt.json", "request.json",
    "result.json",
}
GROUP2_POST_FINALIZE_REBOOT_VM_INSTALLED = {
    "deploy.sh", "carry_forward.py", "verify.py", "assets.py",
}
GROUP2_REBOOT_ADMISSION_TABLES = {
    "users", "credentials", "adapters", "adapter_input_configs",
    "adapter_schedules", "workers", "executions", "execution_attempts",
    "adapter_execution_slots", "adapter_execution_admission",
    "global_execution_admission", "execution_outbox",
    "execution_idempotency_records", "execution_artifact_holds",
    "execution_input_artifact_leases", "managed_input_artifacts",
    "adapter_input_artifact_bindings", "managed_input_upload_reservations",
    "artifact_deletion_jobs", "managed_input_capacity", "managed_input_settings",
    "worker_cleanup_requests", "runtime_reconciliation_cursors",
}
GROUP2_REBOOT_ADMISSION_COLUMNS = {
    "users": {"id", "username"}, "credentials": {"id", "name"},
    "adapters": {"id", "adapter_type", "archived_at"},
    "adapter_input_configs": {"adapter_id", "source_type", "revision"},
    "adapter_schedules": {"id", "adapter_id", "enabled", "next_run_at"},
    "workers": {"id", "name"},
    "executions": {"id", "adapter_id", "trigger", "status", "dispatch_backend",
        "logical_input_bytes", "admission_released_at", "created_at",
        "dispatch_generation", "next_attempt_at", "target_worker_id_snapshot"},
    "execution_attempts": {"id", "execution_id", "adapter_id", "worker_id", "status", "lease_expires_at"},
    "adapter_execution_slots": {"adapter_id", "slot_no", "active_attempt_id", "lease_expires_at"},
    "adapter_execution_admission": {"adapter_id", "outstanding_count", "outstanding_bytes"},
    "global_execution_admission": {"singleton_key", "outstanding_count", "outstanding_bytes"},
    "execution_outbox": {"id", "execution_id", "dispatch_generation", "message_id", "routing_key", "status", "available_at", "lease_expires_at", "published_at"},
    "execution_idempotency_records": {"id", "execution_id", "expires_at"},
    "execution_artifact_holds": {"id", "execution_id", "artifact_id", "expires_at", "purged_at"},
    "execution_input_artifact_leases": {"execution_id", "artifact_id"},
    "managed_input_artifacts": {"id", "adapter_id", "upload_session_id", "upload_reservation_id", "storage_key", "status", "size_bytes", "sha256", "expires_at", "delete_attempts", "delete_lease_until"},
    "adapter_input_artifact_bindings": {"adapter_id", "artifact_id", "ordinal", "input_config_revision"},
    "managed_input_upload_reservations": {"id", "adapter_id", "upload_session_id", "reserved_bytes", "status", "expires_at"},
    "artifact_deletion_jobs": {"id", "storage_key", "status", "delete_attempts", "delete_lease_until"},
    "managed_input_capacity": {"id", "actual_bytes", "reserved_bytes"},
    "worker_cleanup_requests": {"id", "worker_id", "adapter_id", "status", "attempts", "created_at"},
    "runtime_reconciliation_cursors": {"name", "after_id", "upper_id"},
}
GROUP2_REBOOT_ADMISSION_PRIMARY_KEYS = {
    **{name: ("id",) for name in GROUP2_REBOOT_ADMISSION_COLUMNS
       if name not in {"adapter_input_configs", "adapter_execution_slots",
                       "adapter_execution_admission", "global_execution_admission",
                       "execution_input_artifact_leases", "adapter_input_artifact_bindings",
                       "runtime_reconciliation_cursors"}},
    "adapter_input_configs": ("adapter_id",),
    "adapter_execution_slots": ("adapter_id", "slot_no"),
    "adapter_execution_admission": ("adapter_id",),
    "global_execution_admission": ("singleton_key",),
    "execution_input_artifact_leases": ("execution_id", "artifact_id"),
    "adapter_input_artifact_bindings": ("adapter_id", "artifact_id"),
    "runtime_reconciliation_cursors": ("name",),
    "managed_input_settings": ("id",),
}
GROUP2_REBOOT_MUTATION_GUARD_TABLES = (
    "managed_input_upload_reservations", "artifact_deletion_jobs",
    "managed_input_capacity", "managed_input_settings",
)
GROUP2_POST_FINALIZE_REBOOT_PARENT_FINALIZE_ID = "371ecfb7f47e4309a6eab7dd0e4e6bef"
BOOT_ID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)
GROUP2_PARTIAL_FIXED_HASHES = {
    "official_attempt": "b19732783fb607a2f49143eb109ab5ef552327f97a7113a6fddea5428b372d5c",
    "phase": "00ca97f2dbdb192c6d2b5567d77785c57a017c806f460679ce9f3f18d583b17c",
    "deploy_tail": "9c81b1df570bdd3ac02f54b2d9177262e649773905d06ef83f4258584150d18b",
    "diagnostic_result": "0a90154b1d2cf82722dca4de17875ea60bd6e6a722b3ab02f9f298d2eaabfe34",
    "diagnostic_vm_state": "16695dfe07ef001bfbef1665d27bdda0960615dc94d9f98f15ac6f84b6b98895",
    "diagnostic_host_state": "5d96ca8172bc3c4d3a55e59d511c32b1c22bb984b65fc3f4818d32632b56cf32",
    "decision_report": "7e8d1314e639525d21b51a6a6c2b49b9e3cd411cbfa184eb2f32dc8c6d3e5f47",
}
GROUP2_INHERITED_REVIEW_HASHES = {
    "group2-product-integration-recheck.md": "e5c457872b19af0994ce76e3a894e8ffbb2cbd7a11868236d5c4bb8bd2fe152f",
    "group2-ci-web-code-review.md": "aa4a861dc2784244921a79af25183e4600b6328b8ddff573dbb3f1425c24c43f",
    "group2-python-harness-code-review.md": "31bc67be2eefadceba6c1d612731d24f349983adf61567386ebc56ea39062c2b",
}
# The 57-path approved product anchor.  The three planning artifacts are
# intentionally content-bound by the final independent review rather than by
# the earlier product commit; every other blob remains pinned to that review.
GROUP2_PRODUCT_RULES = {
    ".env.example": (
        "M",
        "100644",
        "100644",
        "fa62b572ce28cb43c6666cae0b63f5d8ac9861ff",
        "b30ff96f579e58df107635a15161ab5195962638",
    ),
    "backend/src/dlr/common/config.py": (
        "M",
        "100644",
        "100644",
        "a8995f83ea3135918c943a14967a32566defe11a",
        "9b0ebdef9650da99a0d1c596d99a19577702c306",
    ),
    "backend/src/dlr/control/ai/dlr_docs.py": (
        "M",
        "100644",
        "100644",
        "5aa1a2b61e3443089792383201572ca0bb158104",
        "187f850288dad1856a0bd8bd82083bf1fbf62835",
    ),
    "backend/src/dlr/control/app.py": (
        "M",
        "100644",
        "100644",
        "76126751eab20a45a8a34158093e7a221a40c074",
        "d90bc58f38c7b567cfb04119b45b8c0bf7d9ae7c",
    ),
    "backend/src/dlr/control/services/input_config.py": (
        "M",
        "100644",
        "100644",
        "86a525003d513e696d170c4f4069559630e4b3ba",
        "f2a12986d2de6d8738a4aaa5255235dc82be0c96",
    ),
    "backend/src/dlr/control/services/package_source.py": (
        "M",
        "100644",
        "100644",
        "b8aee5005899ff0ddfbe1a4d9540cc86d730b3df",
        "916b886dac14f7e4ce849ed06087b5acc3766b98",
    ),
    "backend/src/dlr/control/services/schedule.py": (
        "M",
        "100644",
        "100644",
        "d9c6facbadd6d802cb618b325c99cf5e541bf58e",
        "0591245faa4539c79e529f257f5ebf3d91f6b958",
    ),
    "backend/src/dlr/runtime/harness.py": (
        "M",
        "100644",
        "100644",
        "3182c6ae4b0c62f63c3ad62cc8fe95300395766b",
        "7ad74e5b6f5327a68aff241ac450de9f0fb2f367",
    ),
    "backend/tests/test_account_wave_c.py": (
        "M",
        "100644",
        "100644",
        "a77a27399b11d60b3ccb8fb208adafb8f78a56f7",
        "f79fb09cc7b0c0af38cc4b2089246fb824b310bb",
    ),
    "backend/tests/test_issue127_c4.py": (
        "M",
        "100644",
        "100644",
        "fcde54a8f039a672c6f6f56d4756e0ce949c5460",
        "b28fdd6eef99945f1e97c33936a99855880ab18b",
    ),
    "backend/tests/test_issue150_upload_proxy_contract.py": (
        "A",
        "000000",
        "100644",
        "0" * 40,
        "4d75f4a83d5daff8b6f47b6f6972aa23f1d5d785",
    ),
    "backend/tests/test_issue153_json_type_persistence.py": (
        "A",
        "000000",
        "100644",
        "0" * 40,
        "865ff443e16b6c3c981a0b3eed90ebbcd9e04521",
    ),
    "backend/tests/test_issue157_package_source_validation.py": (
        "A",
        "000000",
        "100644",
        "0" * 40,
        "1500fe5b3e0cd30ddccbce44731ecf60b8eaf24b",
    ),
    "backend/tests/test_issue159_java_docs_contract.py": (
        "A",
        "000000",
        "100644",
        "0" * 40,
        "af8fb28b3e30635363aea16122681b734b4a87b3",
    ),
    "backend/tests/test_package_sources.py": (
        "M",
        "100644",
        "100644",
        "9e5bd3f6975eaab5bd6706571ae404d333cf4761",
        "65ef0ae31f7007e2b6fc566c4247091675e8b4ab",
    ),
    "backend/tests/test_runtime.py": (
        "M",
        "100644",
        "100644",
        "637e092a4abe9c251fed7755601f1dd9d5feb347",
        "9de645f1985175b32cf55a6182f3bcbb3866a80f",
    ),
    "docker-compose.yml": (
        "M",
        "100644",
        "100644",
        "60f7466be4537a7afd7a98db35861b04043f9364",
        "bc05b6614edb9d2354e517880fa5343caf4a7afa",
    ),
    "docker/nginx-account.conf": (
        "M",
        "100644",
        "100644",
        "36195c9e64cd0f1be21c9387ddcf96ef8a2f15e2",
        "af9b780a03f9c592ee4903f1fead8f1225844e9a",
    ),
    "docker/nginx.conf": (
        "M",
        "100644",
        "100644",
        "2b0671a61832fd7f3706c123d7d3cbf704c63c78",
        "416d93a64b9050f2471b4caf70823898e5129eeb",
    ),
    "docs/en/issue127-managed-input-operations.md": (
        "M",
        "100644",
        "100644",
        "f046e21110ff2a6980fe23deaa4e4f8c58c9e72b",
        "2874894289df8dba0ce65901cb035c5246e64a56",
    ),
    "docs/zh-CN/issue127-managed-input-operations.md": (
        "M",
        "100644",
        "100644",
        "1ee2fc5079c44405029cf89b4bc74a9c1e09fd2a",
        "4a34f5d002f2b9287f0600ac67d3d84bab4384ec",
    ),
    "openspec/changes/issue161-product-correctness/.openspec.yaml": (
        "A",
        "000000",
        "100644",
        "0" * 40,
        "cbd245e433b3fb3e7a2e4c189af09bc739ba8e32",
    ),
    "openspec/changes/issue161-product-correctness/design.md": (
        "A",
        "000000",
        "100644",
        None,
        None,
    ),
    "openspec/changes/issue161-product-correctness/proposal.md": (
        "A",
        "000000",
        "100644",
        None,
        None,
    ),
    "openspec/changes/issue161-product-correctness/specs/adapter-input-config/spec.md": (
        "A",
        "000000",
        "100644",
        "0" * 40,
        "e4c4d188b0e0acf6adf9ce9e76e0f55ce8732286",
    ),
    "openspec/changes/issue161-product-correctness/specs/execution-output-presence/spec.md": (
        "A",
        "000000",
        "100644",
        "0" * 40,
        "e157dc2a3bbd86a276a28632769cb0544bd794c1",
    ),
    "openspec/changes/issue161-product-correctness/specs/input-compatibility-rollout/spec.md": (
        "A",
        "000000",
        "100644",
        "0" * 40,
        "be553898805bb7957e51cc9fd72c6625a3be7648",
    ),
    "openspec/changes/issue161-product-correctness/specs/java-context-documentation-contract/spec.md": (
        "A",
        "000000",
        "100644",
        "0" * 40,
        "66da214473b25fa13852f2f61411d92262111bea",
    ),
    "openspec/changes/issue161-product-correctness/specs/managed-input-proxy-boundary/spec.md": (
        "A",
        "000000",
        "100644",
        "0" * 40,
        "1809176b599238ee531f1112020c2053c4ba9cb0",
    ),
    "openspec/changes/issue161-product-correctness/specs/package-source-address-validation/spec.md": (
        "A",
        "000000",
        "100644",
        "0" * 40,
        "2ad42fa71c2b538d61a206c9783b6bae8b5bb8d1",
    ),
    "openspec/changes/issue161-product-correctness/specs/workbench-runtime-consistency/spec.md": (
        "A",
        "000000",
        "100644",
        "0" * 40,
        "c099d56ad22d7a3b059f99ee02532506050efe52",
    ),
    "openspec/changes/issue161-product-correctness/tasks.md": (
        "A",
        "000000",
        "100644",
        None,
        None,
    ),
    "web/src/App.test.tsx": (
        "M",
        "100644",
        "100644",
        "c9094f1cc283273baab97666e9c069a6a59392e7",
        "1adfefd534855295c682a87d0432d0576ae39e89",
    ),
    "web/src/App.tsx": (
        "M",
        "100644",
        "100644",
        "45219c44d797791f2cf786f39fe5de1d4cdb560c",
        "91349157a2f4a67c6e862a7638158af641bf37dd",
    ),
    "web/src/components/CredentialBindingsEditor.test.tsx": (
        "M",
        "100644",
        "100644",
        "046abae7d813bea4f890600afff56c59b29d21f0",
        "cd809d5bc0ceab60a55245b36900da2f85709464",
    ),
    "web/src/components/CredentialBindingsEditor.tsx": (
        "M",
        "100644",
        "100644",
        "c9649bf2934f111f1fd1393665ef9aeca72473e7",
        "168f448ff1151aaa3c4bc61c16e63f5d240e1b9e",
    ),
    "web/src/components/ExecutionHistoryPanel.d2.test.tsx": (
        "M",
        "100644",
        "100644",
        "bdef9b20000353662e34ca135c8da4767b41442b",
        "4d0e342cdb9f8a2890de79fb65e3d7139af26730",
    ),
    "web/src/components/ExecutionHistoryPanel.tsx": (
        "M",
        "100644",
        "100644",
        "90731a9956af7ae51ad2f49dd5e7841661d2ebb6",
        "e69cc0e0232a08d8db3e72689cff79644f11cf1d",
    ),
    "web/src/components/LiveLogWorkspace.test.tsx": (
        "M",
        "100644",
        "100644",
        "239f701dcbf33f6ac698f8ab261ebc9d4e7c6046",
        "7d4935a6a5f92ccd39dfba0245e22c5ac97f8c43",
    ),
    "web/src/components/OutputView.test.tsx": (
        "A",
        "000000",
        "100644",
        "0" * 40,
        "302ffddc4a7fbdae9902f37cbd57f80857e5f6f0",
    ),
    "web/src/components/OutputView.tsx": (
        "M",
        "100644",
        "100644",
        "2a4ce004111013ff42b52f58d9fec3275dc2ac09",
        "27edf7af844cb4db9609e22f39dbb5714401c749",
    ),
    "web/src/components/SystemSettingsDrawer.test.tsx": (
        "M",
        "100644",
        "100644",
        "d79ddfe4383f9158a9b794673ca0d03a869f2473",
        "c40317c4ad996d9551fc449e1aabdb04bca4e33c",
    ),
    "web/src/components/SystemSettingsDrawer.tsx": (
        "M",
        "100644",
        "100644",
        "147b6cabd9b6e65e47c0d31fd1d85bbcc0f3ae6d",
        "2d468388171e50b4e627882839683b02ad58d9ba",
    ),
    "web/src/components/TaskRunSettingsPanel.tsx": (
        "M",
        "100644",
        "100644",
        "ee3b6de1cf6040112f969bf2c073826dca025aa9",
        "42410bbc4f4b2948df1593c5f507e62e3c031acf",
    ),
    "web/src/components/TaskWorkbenchHeader.tsx": (
        "M",
        "100644",
        "100644",
        "57c1ce7ba5518942a26867aac5ff1b5f5ff0256b",
        "31f4894e648e049c5ee04544901f032dc49d0d36",
    ),
    "web/src/components/WebhookTriggerPanel.tsx": (
        "M",
        "100644",
        "100644",
        "0b616df10ddf358c5874ec644b22f0290fd21f4a",
        "771e551a307cc62b3c37687b7e112f75ef46460b",
    ),
    "web/src/components/WebhookWorkbenchHeader.tsx": (
        "M",
        "100644",
        "100644",
        "74d11fa626c3c569173653b107b76a69522187bf",
        "7bae9e3177ad770afd0845d2098fdb6d33a31a96",
    ),
    "web/src/execution-output.ts": (
        "A",
        "000000",
        "100644",
        "0" * 40,
        "03c36f35d31b4deb38115bfd2788cba433e30610",
    ),
    "web/src/hooks/useExecutionWatcher.test.tsx": (
        "M",
        "100644",
        "100644",
        "4329ea004ecde042b73a008ea974b5138f1a582a",
        "a6c9024a502121b3c2bc8c93d201ef52a1b91571",
    ),
    "web/src/hooks/useExecutionWatcher.ts": (
        "M",
        "100644",
        "100644",
        "eec5af6936beddb6441ede2241dff958a5d733aa",
        "4d5b2fdda247ddad864850e7e9e3abb480e0970e",
    ),
    "web/src/hooks/useRuntimeAuthority.test.tsx": (
        "A",
        "000000",
        "100644",
        "0" * 40,
        "e6fac49ed3a1e9450dbd3ad3090a6482b5cc96ac",
    ),
    "web/src/hooks/useRuntimeAuthority.ts": (
        "A",
        "000000",
        "100644",
        "0" * 40,
        "d4c7ac4ff8014d154fbff6f8d6ed1dfb172970e2",
    ),
    "web/src/i18n/locales/en/runtime.json": (
        "M",
        "100644",
        "100644",
        "9305a07842c6fd2e346d93e6c14b1109459a8546",
        "0e14c1d2e3174046b7a0ab6e00e7f09baf146065",
    ),
    "web/src/i18n/locales/en/settings.json": (
        "M",
        "100644",
        "100644",
        "f058f7299a36967acba0f8bc7445db26bc5b31e5",
        "494812afed9cb10ebd6b22fd6a5cf8a37f63b040",
    ),
    "web/src/i18n/locales/zh-CN/runtime.json": (
        "M",
        "100644",
        "100644",
        "36ed94e8cf86f64fe096e3d60c73bb69de1e3e51",
        "439fd618605ae4e0ee071bc0fa0f5750a1488b40",
    ),
    "web/src/i18n/locales/zh-CN/settings.json": (
        "M",
        "100644",
        "100644",
        "badfe2bafaa880f76ea618a6113431985d03d2fd",
        "0a949ad673e5532a739dbf19032e02a5a7cb36ee",
    ),
    "web/src/runtime-refresh-policy.ts": (
        "M",
        "100644",
        "100644",
        "6f2627334f86109839e57506b45dd55bc03a1c3b",
        "b423eb221e3ed565539262d6796fd43dd4301011",
    ),
}
AUDITED_SOURCE_MODES = {
    "web/src/index.css": "100644",
    "web/tests/e2e/issue152-popconfirm.spec.ts": "100644",
    "tools/local-preview/carry_forward.py": "100644",
    "tools/local-preview/preview.py": "100755",
    "tools/local-preview/deploy.sh": "100755",
    "tools/local-preview/tests/test_carry_forward.py": "100644",
    "tools/local-preview/tests/test_preview.py": "100644",
    "tools/local-preview/tests/test_preview_locks.py": "100644",
    "docs/en/local-preview.md": "100644",
    "docs/zh-CN/local-preview.md": "100644",
    "openspec/changes/issue161-runtime-reliability/proposal.md": "100644",
    "openspec/changes/issue161-runtime-reliability/design.md": "100644",
    "openspec/changes/issue161-runtime-reliability/tasks.md": "100644",
    "openspec/changes/issue161-runtime-reliability/specs/incident-preserving-upgrade/spec.md": "100644",
}
MAX_CAPTURE_ENTRIES = 100_000
MAX_CAPTURE_BYTES = 16 * 1024 * 1024 * 1024
MAX_JSON_BYTES = 1024 * 1024
EMPTY_TABLES_BY_REVISION = {
    "0040_issue152_dispositions": ("execution_incident_dispositions",),
}
SCHEMA_ADDITIONS = {
    ("0038_issue138_languages", "0039_issue134_reconcile"): {
        "runtime_reconciliation_cursors"
    },
    ("0039_issue134_reconcile", "0040_issue152_dispositions"): {
        "execution_incident_dispositions"
    },
    ("0038_issue138_languages", "0040_issue152_dispositions"): {
        "runtime_reconciliation_cursors",
        "execution_incident_dispositions",
    },
    ("0040_issue152_dispositions", "0040_issue152_dispositions"): set(),
}


class CarryForwardError(RuntimeError):
    """A stable fail-closed verifier result."""

    def __init__(self, code: str):
        if not re.fullmatch(r"[a-z0-9_]+", code):
            raise ValueError("invalid verifier error code")
        super().__init__(code)
        self.code = code


def _positive(value: Any, code: str = "selection_invalid") -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise CarryForwardError(code)
    return value


def canonical(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CarryForwardError("noncanonical_value")
        return value
    if isinstance(value, decimal.Decimal):
        return {"$decimal": format(value, "f")}
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        normalized = value
        if isinstance(value, dt.datetime) and value.tzinfo is not None:
            normalized = value.astimezone(dt.timezone.utc)
        return {"$datetime": normalized.isoformat()}
    if isinstance(value, uuid.UUID):
        return {"$uuid": str(value)}
    if isinstance(value, bytes):
        return {"$bytes": base64.b64encode(value).decode("ascii")}
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise CarryForwardError("noncanonical_value")
        return {key: canonical(value[key]) for key in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [canonical(item) for item in value]
    raise CarryForwardError("noncanonical_value")


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        canonical(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def streaming_digest(value: Any) -> str:
    """Hash canonical JSON incrementally for evidence objects with large raw inputs."""
    output = hashlib.sha256()
    for chunk in canonical_json_chunks(value):
        output.update(chunk.encode("utf-8"))
    return output.hexdigest()


def canonical_json_chunks(value: Any):
    """Yield canonical JSON without copying large safe ASCII strings."""
    if value is None:
        yield "null"
    elif value is True:
        yield "true"
    elif value is False:
        yield "false"
    elif isinstance(value, int) and not isinstance(value, bool):
        yield str(value)
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise CarryForwardError("noncanonical_value")
        yield json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    elif isinstance(value, str):
        if (
            len(value) > 1024 * 1024
            and value.isascii()
            and '"' not in value
            and "\\" not in value
            and all(ord(character) >= 0x20 for character in value)
        ):
            yield '"'
            for offset in range(0, len(value), 1024 * 1024):
                yield value[offset:offset + 1024 * 1024]
            yield '"'
        else:
            yield json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    elif isinstance(value, list):
        yield "["
        for index, item in enumerate(value):
            if index:
                yield ","
            yield from canonical_json_chunks(item)
        yield "]"
    elif isinstance(value, dict) and all(
        isinstance(key, str) for key in value
    ):
        yield "{"
        for index, key in enumerate(sorted(value)):
            if index:
                yield ","
            yield json.dumps(key, ensure_ascii=False, separators=(",", ":"))
            yield ":"
            yield from canonical_json_chunks(value[key])
        yield "}"
    else:
        raise CarryForwardError("noncanonical_value")


def _secure_file(path: Path, *, output: bool = False) -> None:
    if not path.is_absolute() or ".." in path.parts:
        raise CarryForwardError("private_path_invalid")
    parent = path.parent
    try:
        parent_info = parent.stat()
    except OSError as error:
        raise CarryForwardError("private_parent_invalid") from error
    if (
        not stat.S_ISDIR(parent_info.st_mode)
        or parent_info.st_uid != os.geteuid()
        or parent_info.st_mode & 0o077
    ):
        raise CarryForwardError("private_parent_invalid")
    if output and not path.exists():
        return
    try:
        info = path.lstat()
    except OSError as error:
        raise CarryForwardError("private_file_invalid") from error
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_mode & 0o077
        or info.st_nlink != 1
    ):
        raise CarryForwardError("private_file_invalid")


def read_private(path: Path) -> Any:
    _secure_file(path)
    try:
        return json.loads(path.read_text())
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise CarryForwardError("private_json_invalid") from error


def write_private(path: Path, value: Any) -> None:
    _secure_file(path, output=True)
    with tempfile.NamedTemporaryFile(
        mode="wb", dir=path.parent, prefix=".carry-forward-", delete=False
    ) as output:
        temporary = Path(output.name)
        os.chmod(temporary, 0o600)
        output.write(canonical_bytes(value) + b"\n")
        output.flush()
        os.fsync(output.fileno())
    try:
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def _stable_code(value: Any) -> bool:
    return (
        isinstance(value, str)
        and re.fullmatch(r"[a-z][a-z0-9_]{0,127}", value) is not None
    )


def _uuid_text(value: Any, code: str = "selection_invalid") -> str:
    if not isinstance(value, str):
        raise CarryForwardError(code)
    try:
        parsed = uuid.UUID(value)
    except ValueError as error:
        raise CarryForwardError(code) from error
    if str(parsed) != value:
        raise CarryForwardError(code)
    return value


def is_audited_mode(mode: Any) -> bool:
    return mode in {AUDITED_MODE, GROUP2_MODE}


def normalize_selection(value: Any, *, mode: str | None = None) -> dict[str, Any]:
    required = {"queued", "cleanup_execution_ids"}
    if is_audited_mode(mode):
        required.add("terminal_executions")
    elif mode is not None:
        raise CarryForwardError("selection_invalid")
    if not isinstance(value, dict) or set(value) != required:
        raise CarryForwardError("selection_invalid")
    queued = value["queued"]
    cleanup = value["cleanup_execution_ids"]
    if not isinstance(queued, list) or not isinstance(cleanup, list):
        raise CarryForwardError("selection_invalid")
    normalized_queued: list[dict[str, Any]] = []
    seen_executions: set[int] = set()
    seen_incidents: set[int] = set()
    for item in queued:
        if not isinstance(item, dict) or set(item) != {"execution_id", "incident_ids"}:
            raise CarryForwardError("selection_invalid")
        execution_id = _positive(item["execution_id"])
        incidents = item["incident_ids"]
        if not isinstance(incidents, list) or not incidents:
            raise CarryForwardError("selection_invalid")
        incident_ids = [_positive(entry) for entry in incidents]
        if len(set(incident_ids)) != len(incident_ids):
            raise CarryForwardError("selection_duplicate")
        if execution_id in seen_executions or seen_incidents.intersection(incident_ids):
            raise CarryForwardError("selection_duplicate")
        seen_executions.add(execution_id)
        seen_incidents.update(incident_ids)
        normalized_queued.append(
            {"execution_id": execution_id, "incident_ids": sorted(incident_ids)}
        )
    cleanup_ids = [_positive(entry) for entry in cleanup]
    if len(set(cleanup_ids)) != len(cleanup_ids) or seen_executions.intersection(
        cleanup_ids
    ):
        raise CarryForwardError("selection_duplicate")
    terminals: list[dict[str, Any]] = []
    terminal_execution_ids: set[int] = set()
    terminal_incident_ids: set[int] = set()
    terminal_disposition_ids: set[str] = set()
    if is_audited_mode(mode):
        raw_terminals = value["terminal_executions"]
        terminal_keys = {
            "execution_id",
            "incident_id",
            "disposition_id",
            "expected_status",
            "expected_generation",
            "expected_output_digest",
            "expected_error_code",
            "expected_last_error_code",
            "expected_attempt_count",
        }
        if not isinstance(raw_terminals, list) or not raw_terminals:
            raise CarryForwardError("selection_invalid")
        for item in raw_terminals:
            if not isinstance(item, dict) or set(item) != terminal_keys:
                raise CarryForwardError("selection_invalid")
            execution_id = _positive(item["execution_id"])
            incident_id = _positive(item["incident_id"])
            disposition_id = _uuid_text(item["disposition_id"])
            status = item["expected_status"]
            generation = _positive(item["expected_generation"])
            attempt_count = item["expected_attempt_count"]
            output_digest = item["expected_output_digest"]
            error_code = item["expected_error_code"]
            last_error_code = item["expected_last_error_code"]
            if (
                status not in {"succeeded", "dead_letter", "cancelled"}
                or not isinstance(output_digest, str)
                or not DIGEST.fullmatch(output_digest)
                or not isinstance(attempt_count, int)
                or isinstance(attempt_count, bool)
                or attempt_count < 0
                or (error_code is not None and not _stable_code(error_code))
                or (last_error_code is not None and not _stable_code(last_error_code))
            ):
                raise CarryForwardError("selection_invalid")
            if status == "succeeded" and (
                error_code is not None or last_error_code is not None
            ):
                raise CarryForwardError("selection_invalid")
            if status == "dead_letter" and (
                error_code is None or error_code != last_error_code
            ):
                raise CarryForwardError("selection_invalid")
            if status in {"succeeded", "dead_letter"} and attempt_count < 1:
                raise CarryForwardError("selection_invalid")
            if status == "cancelled" and (
                attempt_count != 0
                or error_code != "execution_cancelled"
                or last_error_code != "execution_cancelled"
                or output_digest != digest(None)
            ):
                raise CarryForwardError("selection_invalid")
            if (
                execution_id in terminal_execution_ids
                or execution_id in seen_executions
                or incident_id in terminal_incident_ids
                or incident_id in seen_incidents
                or disposition_id in terminal_disposition_ids
            ):
                raise CarryForwardError("selection_duplicate")
            terminal_execution_ids.add(execution_id)
            terminal_incident_ids.add(incident_id)
            terminal_disposition_ids.add(disposition_id)
            terminals.append(
                {
                    **item,
                    "execution_id": execution_id,
                    "incident_id": incident_id,
                    "disposition_id": disposition_id,
                    "expected_generation": generation,
                    "expected_attempt_count": attempt_count,
                }
            )
    if not normalized_queued and not cleanup_ids and not terminals:
        raise CarryForwardError("selection_empty")
    result = {
        "queued": sorted(normalized_queued, key=lambda item: item["execution_id"]),
        "cleanup_execution_ids": sorted(cleanup_ids),
    }
    if is_audited_mode(mode):
        result["terminal_executions"] = sorted(
            terminals, key=lambda item: item["execution_id"]
        )
    if mode == GROUP2_MODE and (
        len(result["queued"]) != 9
        or len(result["cleanup_execution_ids"]) != 3
        or len(result["terminal_executions"]) != 5
    ):
        raise CarryForwardError("group2_selection_count_invalid")
    return result


def selected_execution_ids(selection: dict[str, Any]) -> set[int]:
    return {
        *(item["execution_id"] for item in selection["queued"]),
        *selection["cleanup_execution_ids"],
    }


def manifest_mode(value: dict[str, Any] | None) -> str | None:
    if value is None:
        return None
    mode = value.get("mode")
    if mode is None:
        return None
    if not is_audited_mode(mode):
        raise CarryForwardError("manifest_mode_invalid")
    return mode


def datetime_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        if value.tzinfo is not None:
            value = value.astimezone(dt.timezone.utc)
        return value.isoformat()
    if isinstance(value, str):
        try:
            parsed = dt.datetime.fromisoformat(value)
        except ValueError as error:
            raise CarryForwardError("attempt_lease_invalid") from error
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(dt.timezone.utc)
        return parsed.isoformat()
    raise CarryForwardError("attempt_lease_invalid")


def row_digest(row: dict[str, Any]) -> str:
    return digest(row)


def project_rows(
    tables: dict[str, dict[str, Any]],
    *,
    required: tuple[str, ...] = RESPONSIBILITY_TABLES,
) -> dict[str, Any]:
    projection: dict[str, Any] = {}
    for name in required:
        table = tables.get(name)
        if not isinstance(table, dict):
            raise CarryForwardError("schema_table_missing")
        columns, primary_key, rows = (
            table.get("columns"),
            table.get("primary_key"),
            table.get("rows"),
        )
        if (
            not isinstance(columns, list)
            or not columns
            or not all(isinstance(column, str) for column in columns)
            or len(set(columns)) != len(columns)
            or not isinstance(primary_key, list)
            or not primary_key
            or not set(primary_key).issubset(columns)
            or not isinstance(rows, list)
            or not all(
                isinstance(row, dict) and set(row) == set(columns) for row in rows
            )
        ):
            raise CarryForwardError("schema_projection_invalid")
        rows = sorted(
            rows, key=lambda row: canonical_bytes([row[key] for key in primary_key])
        )
        projection[name] = {
            "columns": columns,
            "primary_key": primary_key,
            "rows": [row_digest(row) for row in rows],
            "count": len(rows),
        }
    return projection


def _by_execution(
    rows: list[dict[str, Any]], execution_id: int
) -> list[dict[str, Any]]:
    return [row for row in rows if row.get("execution_id") == execution_id]


def derive_responsibilities(
    tables: dict[str, dict[str, Any]],
    selection: dict[str, Any],
    *,
    mode: str | None = None,
) -> dict[str, Any]:
    selection = normalize_selection(selection, mode=mode)
    selected = selected_execution_ids(selection)
    rows = {name: value["rows"] for name, value in tables.items()}
    executions = {row.get("id"): row for row in rows["executions"]}
    attempts = rows["execution_attempts"]
    incidents = {
        row.get("id"): row for row in rows["execution_infrastructure_incidents"]
    }
    active = [row for row in attempts if row.get("status") in ACTIVE_ATTEMPTS]
    if active:
        raise CarryForwardError("active_attempt_present")
    if any(
        row.get("active_attempt_id") is not None
        for row in rows["adapter_execution_slots"]
    ):
        raise CarryForwardError("active_slot_present")
    if any(
        row.get("status") in {"pending", "running"}
        for row in rows["worker_cleanup_requests"]
    ):
        raise CarryForwardError("worker_cleanup_active")
    for row in rows["executions"]:
        if (
            row.get("status") in {"queued", "running", "retry_wait"}
            and row.get("id") not in selected
        ):
            raise CarryForwardError("unselected_execution_busy")
        if (
            row.get("workspace_cleanup_status") in {"pending", "deferred"}
            and row.get("id") not in selected
        ):
            raise CarryForwardError("unselected_cleanup_responsibility")
    for row in rows["execution_attempts"]:
        summary = row.get("cleanup_summary")
        if (
            isinstance(summary, dict)
            and summary.get("workspace_cleanup_status") == "deferred"
            and row.get("execution_id") not in selected
        ):
            raise CarryForwardError("unselected_cleanup_responsibility")

    result: list[dict[str, Any]] = []
    for queued in selection["queued"]:
        execution_id = queued["execution_id"]
        execution = executions.get(execution_id)
        if not execution or execution.get("status") != "queued":
            raise CarryForwardError("queued_execution_invalid")
        if execution.get("dispatch_backend") != "rabbitmq":
            raise CarryForwardError("queued_backend_invalid")
        if execution.get("admission_released_at") is not None:
            raise CarryForwardError("queued_admission_released")
        adapter_id = execution.get("adapter_id")
        adapter_admission = next(
            (
                row
                for row in rows["adapter_execution_admission"]
                if row.get("adapter_id") == adapter_id
            ),
            None,
        )
        global_admission = next(
            (
                row
                for row in rows["global_execution_admission"]
                if row.get("singleton_key") == "global"
            ),
            None,
        )
        if (
            not adapter_admission
            or not isinstance(adapter_admission.get("outstanding_count"), int)
            or adapter_admission["outstanding_count"] <= 0
            or not global_admission
            or not isinstance(global_admission.get("outstanding_count"), int)
            or global_admission["outstanding_count"] <= 0
        ):
            raise CarryForwardError("queued_admission_invalid")
        outbox = [
            row
            for row in rows["execution_outbox"]
            if row.get("execution_id") == execution_id
            and row.get("dispatch_generation") == execution.get("dispatch_generation")
        ]
        if len(outbox) != 1:
            raise CarryForwardError("queued_outbox_missing")
        execution_incidents = []
        for incident_id in queued["incident_ids"]:
            incident = incidents.get(incident_id)
            if (
                not incident
                or incident.get("execution_id") != execution_id
                or incident.get("status") != "open"
                or incident.get("dispatch_generation")
                != execution.get("dispatch_generation")
            ):
                raise CarryForwardError("queued_incident_invalid")
            execution_incidents.append(incident_id)
        all_open_incidents = {
            row.get("id")
            for row in rows["execution_infrastructure_incidents"]
            if row.get("execution_id") == execution_id and row.get("status") == "open"
        }
        if all_open_incidents != set(execution_incidents):
            raise CarryForwardError("queued_incident_unselected")
        result.append(
            _responsibility(
                execution, _by_execution(attempts, execution_id), execution_incidents
            )
        )
    for execution_id in selection["cleanup_execution_ids"]:
        execution = executions.get(execution_id)
        if not execution or execution.get("status") not in TERMINAL_EXECUTIONS:
            raise CarryForwardError("cleanup_execution_invalid")
        result.append(
            _responsibility(execution, _by_execution(attempts, execution_id), [])
        )
    return {"executions": sorted(result, key=lambda item: item["execution_id"])}


def _row_by_id(rows: list[dict[str, Any]], value: Any) -> dict[str, Any] | None:
    matches = [row for row in rows if str(row.get("id")) == str(value)]
    if len(matches) > 1:
        raise CarryForwardError("terminal_identity_duplicate")
    return matches[0] if matches else None


def derive_terminal_evidence(
    tables: dict[str, dict[str, Any]], selection: dict[str, Any]
) -> dict[str, Any]:
    selection = normalize_selection(selection, mode=AUDITED_MODE)
    rows = {name: value["rows"] for name, value in tables.items()}
    audits = rows[AUDIT_TABLE]
    selected_audits = {
        item["disposition_id"] for item in selection["terminal_executions"]
    }
    if {str(row.get("id")) for row in audits} != selected_audits:
        raise CarryForwardError("audit_set_mismatch")
    executions = {row.get("id"): row for row in rows["executions"]}
    incidents = {
        row.get("id"): row for row in rows["execution_infrastructure_incidents"]
    }
    attempts = rows["execution_attempts"]
    outbox = rows["execution_outbox"]
    cleanup_ids = set(selection["cleanup_execution_ids"])
    evidence: list[dict[str, Any]] = []
    for expected in selection["terminal_executions"]:
        execution_id = expected["execution_id"]
        incident_id = expected["incident_id"]
        execution = executions.get(execution_id)
        incident = incidents.get(incident_id)
        audit = _row_by_id(audits, expected["disposition_id"])
        if execution is None or incident is None or audit is None:
            raise CarryForwardError("terminal_identity_missing")
        if (
            execution.get("status") != expected["expected_status"]
            or execution.get("dispatch_backend") != "rabbitmq"
            or execution.get("dispatch_generation") != expected["expected_generation"]
            or execution.get("attempt_count") != expected["expected_attempt_count"]
            or digest(execution.get("output")) != expected["expected_output_digest"]
            or type(execution.get("error_code"))
            is not type(expected["expected_error_code"])
            or execution.get("error_code") != expected["expected_error_code"]
            or type(execution.get("last_error_code"))
            is not type(expected["expected_last_error_code"])
            or execution.get("last_error_code") != expected["expected_last_error_code"]
            or execution.get("ended_at") is None
        ):
            raise CarryForwardError("terminal_execution_mismatch")
        execution_attempts = sorted(
            _by_execution(attempts, execution_id),
            key=lambda row: row.get("attempt_no", 0),
        )
        cleanup = _responsibility(execution, execution_attempts, [])["cleanup"]
        if cleanup in {"not_applicable", "deferred_preserved"} and (
            execution_id not in cleanup_ids
        ):
            raise CarryForwardError("terminal_cleanup_unselected")
        if (
            len(execution_attempts) != expected["expected_attempt_count"]
            or [row.get("attempt_no") for row in execution_attempts]
            != list(range(1, len(execution_attempts) + 1))
            or any(
                row.get("status") not in TERMINAL_ATTEMPTS
                or row.get("ended_at") is None
                for row in execution_attempts
            )
        ):
            raise CarryForwardError("terminal_attempt_mismatch")
        if (
            expected["expected_status"] == "succeeded"
            and execution_attempts[-1].get("status") != "succeeded"
        ):
            raise CarryForwardError("terminal_attempt_mismatch")
        if expected["expected_status"] == "dead_letter" and (
            execution_attempts[-1].get("status") != "failed"
            or execution_attempts[-1].get("error_code")
            != expected["expected_error_code"]
        ):
            raise CarryForwardError("terminal_attempt_mismatch")
        if expected["expected_status"] == "cancelled" and execution_attempts:
            raise CarryForwardError("terminal_attempt_mismatch")
        if (
            incident.get("execution_id") != execution_id
            or incident.get("status") != "resolved"
            or incident.get("resolved_at") is None
            or audit.get("incident_id") != incident_id
            or audit.get("execution_id") != execution_id
            or incident.get("dispatch_generation") != audit.get("from_generation")
        ):
            raise CarryForwardError("terminal_incident_mismatch")
        actor_kind, user_id = audit.get("actor_kind"), audit.get("user_id")
        if not (
            (actor_kind == "superadmin" and user_id is None)
            or (
                actor_kind == "account"
                and isinstance(user_id, int)
                and not isinstance(user_id, bool)
                and user_id > 0
            )
        ):
            raise CarryForwardError("audit_actor_invalid")
        try:
            _uuid_text(str(audit.get("id")), "audit_identity_invalid")
            _uuid_text(str(audit.get("idempotency_key")), "audit_identity_invalid")
            dt.datetime.fromisoformat(str(audit.get("created_at")))
        except (TypeError, ValueError) as error:
            raise CarryForwardError("audit_identity_invalid") from error
        action = audit.get("action")
        reason_code = audit.get("reason_code")
        request = {
            "action": action,
            "expected_generation": audit.get("from_generation"),
            "reason_code": reason_code,
        }
        if (
            audit.get("request_hash")
            != hashlib.sha256(canonical_bytes(request)).hexdigest()
        ):
            raise CarryForwardError("audit_request_hash_invalid")
        from_row = _row_by_id(outbox, audit.get("from_outbox_id"))
        to_row = _row_by_id(outbox, audit.get("to_outbox_id"))
        if (
            audit.get("from_outbox_id") is None
            or audit.get("to_outbox_id") is None
            or from_row is None
            or to_row is None
            or from_row.get("execution_id") != execution_id
            or to_row.get("execution_id") != execution_id
            or from_row.get("dispatch_generation") != audit.get("from_generation")
            or to_row.get("dispatch_generation") != audit.get("to_generation")
            or from_row.get("status") != "published"
            or to_row.get("status") != "published"
            or incident.get("message_id") != from_row.get("message_id")
        ):
            raise CarryForwardError("audit_outbox_invalid")
        if expected["expected_status"] in {"succeeded", "dead_letter"}:
            valid_disposition = (
                action == "recover"
                and reason_code in {"capacity_repaired", "routing_repaired"}
                and audit.get("outcome") == "recovery_dispatched"
                and audit.get("code") == "recovery_dispatched"
                and audit.get("execution_status") == "queued"
                and audit.get("to_generation") == audit.get("from_generation") + 1
                and audit.get("to_generation") == execution.get("dispatch_generation")
                and str(audit.get("from_outbox_id")) != str(audit.get("to_outbox_id"))
            )
        else:
            valid_disposition = (
                action == "terminate"
                and reason_code in {"operator_cancel", "verified_terminal"}
                and audit.get("outcome") == "execution_terminal"
                and audit.get("code") == "execution_cancelled"
                and audit.get("execution_status") == "cancelled"
                and audit.get("from_generation")
                == audit.get("to_generation")
                == execution.get("dispatch_generation")
                and str(audit.get("from_outbox_id")) == str(audit.get("to_outbox_id"))
            )
        if not valid_disposition:
            raise CarryForwardError("audit_disposition_invalid")
        if execution.get("admission_released_at") is None:
            raise CarryForwardError("terminal_admission_not_released")
        if any(
            row.get("execution_id") == execution_id
            for name in ("execution_input_artifact_leases", "execution_artifact_holds")
            for row in rows[name]
        ):
            raise CarryForwardError("terminal_resource_present")
        if execution.get("replay_of_execution_id") is not None or any(
            row.get("replay_of_execution_id") == execution_id
            for row in rows["executions"]
        ):
            raise CarryForwardError("terminal_replay_present")
        evidence.append(
            {
                "execution_id": execution_id,
                "incident_id": incident_id,
                "disposition_id": expected["disposition_id"],
                "status": execution["status"],
                "generation": execution["dispatch_generation"],
                "attempt_count": execution["attempt_count"],
            }
        )
    _validate_admission_totals(rows)
    return {"executions": evidence}


def _validate_admission_totals(rows: dict[str, list[dict[str, Any]]]) -> None:
    expected_by_adapter: dict[int, tuple[int, int]] = {}
    unreleased = [
        row
        for row in rows["executions"]
        if row.get("dispatch_backend") == "rabbitmq"
        and row.get("admission_released_at") is None
    ]
    for execution in unreleased:
        adapter_id = _positive(
            execution.get("adapter_id"), "admission_identity_invalid"
        )
        logical_bytes = execution.get("logical_input_bytes")
        if (
            not isinstance(logical_bytes, int)
            or isinstance(logical_bytes, bool)
            or logical_bytes < 0
        ):
            raise CarryForwardError("admission_value_invalid")
        count, size = expected_by_adapter.get(adapter_id, (0, 0))
        expected_by_adapter[adapter_id] = (count + 1, size + logical_bytes)
    admissions = rows["adapter_execution_admission"]
    actual_by_adapter = {row.get("adapter_id"): row for row in admissions}
    if len(actual_by_adapter) != len(admissions) or not set(
        expected_by_adapter
    ).issubset(actual_by_adapter):
        raise CarryForwardError("adapter_admission_mismatch")
    for adapter_id, row in actual_by_adapter.items():
        if (
            row.get("outstanding_count"),
            row.get("outstanding_bytes"),
        ) != expected_by_adapter.get(adapter_id, (0, 0)):
            raise CarryForwardError("adapter_admission_mismatch")
    global_rows = rows["global_execution_admission"]
    expected_global = (
        len(unreleased),
        sum(row.get("logical_input_bytes") for row in unreleased),
    )
    if (
        len(global_rows) != 1
        or global_rows[0].get("singleton_key") != "global"
        or (
            global_rows[0].get("outstanding_count"),
            global_rows[0].get("outstanding_bytes"),
        )
        != expected_global
    ):
        raise CarryForwardError("global_admission_mismatch")


def _responsibility(
    execution: dict[str, Any], attempts: list[dict[str, Any]], incident_ids: list[int]
) -> dict[str, Any]:
    execution_id = _positive(execution.get("id"), "execution_identity_invalid")
    attempt_ids = sorted(
        _positive(row.get("id"), "attempt_identity_invalid") for row in attempts
    )
    attempt_count = execution.get("attempt_count")
    if (
        not isinstance(attempt_count, int)
        or isinstance(attempt_count, bool)
        or attempt_count < 0
    ):
        raise CarryForwardError("execution_attempt_count_invalid")
    if attempt_count != len(attempts):
        raise CarryForwardError("execution_attempt_count_invalid")
    if any(row.get("status") not in TERMINAL_ATTEMPTS for row in attempts):
        raise CarryForwardError("attempt_state_unknown")
    attempt_cleanup: dict[int, str] = {}
    for row in attempts:
        summary = row.get("cleanup_summary")
        status = (
            summary.get("workspace_cleanup_status")
            if isinstance(summary, dict)
            else None
        )
        if status not in {"completed", "deferred"}:
            raise CarryForwardError("attempt_cleanup_state_unknown")
        attempt_cleanup[row["id"]] = status
    deferred_attempt_ids = sorted(
        attempt_id
        for attempt_id, status in attempt_cleanup.items()
        if status == "deferred"
    )
    cleanup_status = execution.get("workspace_cleanup_status")
    if (
        attempt_count == 0
        and not attempts
        and execution.get("worker_id") is None
        and execution.get("started_at") is None
        and cleanup_status == "pending"
    ):
        cleanup = "not_applicable"
    elif attempts and cleanup_status == "completed" and not deferred_attempt_ids:
        cleanup = "completed"
    elif (
        attempts
        and cleanup_status in {"completed", "deferred"}
        and deferred_attempt_ids
    ):
        cleanup = "deferred_preserved"
    else:
        raise CarryForwardError("cleanup_state_unknown")
    return {
        "execution_id": execution_id,
        "status": execution.get("status"),
        "generation": execution.get("dispatch_generation"),
        "attempt_count": attempt_count,
        "attempt_ids": attempt_ids,
        "deferred_attempt_ids": deferred_attempt_ids,
        "attempts": [
            {
                "attempt_id": row["id"],
                "attempt_no": row.get("attempt_no"),
                "fencing_token": row.get("fencing_token"),
                "status": row.get("status"),
                "lease_expires_at": datetime_text(row.get("lease_expires_at")),
                "cleanup_status": attempt_cleanup[row["id"]],
            }
            for row in sorted(attempts, key=lambda row: row["id"])
        ],
        "incident_ids": incident_ids,
        "cleanup": cleanup,
    }


def compare_projection(before: dict[str, Any], after: dict[str, Any]) -> None:
    if set(before) != set(after):
        raise CarryForwardError("projection_table_changed")
    for table, left in before.items():
        right = after[table]
        if left.get("columns") != right.get("columns"):
            raise CarryForwardError("projection_columns_changed")
        if left.get("primary_key") != right.get("primary_key"):
            raise CarryForwardError("projection_primary_key_changed")
        if left.get("rows") != right.get("rows") or left.get("count") != right.get(
            "count"
        ):
            raise CarryForwardError("projection_rows_changed")


def validate_projection_evidence(
    value: Any, *, required: tuple[str, ...] = RESPONSIBILITY_TABLES
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != set(required):
        raise CarryForwardError("projection_table_changed")
    for table in required:
        item = value[table]
        if not isinstance(item, dict) or set(item) != {
            "columns",
            "primary_key",
            "rows",
            "count",
        }:
            raise CarryForwardError("schema_projection_invalid")
        columns, primary_key, rows = item["columns"], item["primary_key"], item["rows"]
        if (
            not isinstance(columns, list)
            or not columns
            or not all(
                isinstance(column, str) and re.fullmatch(r"[a-z][a-z0-9_]*", column)
                for column in columns
            )
            or len(set(columns)) != len(columns)
            or not isinstance(primary_key, list)
            or not primary_key
            or not set(primary_key).issubset(columns)
            or not isinstance(rows, list)
            or not all(isinstance(row, str) and DIGEST.fullmatch(row) for row in rows)
            or item["count"] != len(rows)
        ):
            raise CarryForwardError("schema_projection_invalid")
        if table == AUDIT_TABLE and (
            columns != list(AUDIT_COLUMNS) or primary_key != ["id"]
        ):
            raise CarryForwardError("audit_schema_invalid")
    return value


def _safe_tree(
    root: Path,
    *,
    allowed_top: set[str] | None = None,
    ignored_top: set[str] | None = None,
) -> list[dict[str, Any]]:
    try:
        root_info = root.lstat()
    except OSError as error:
        raise CarryForwardError("storage_root_unavailable") from error
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        raise CarryForwardError("storage_root_invalid")
    root_device = root_info.st_dev
    result: list[dict[str, Any]] = []
    total_bytes = 0
    pending = [root]
    while pending:
        parent = pending.pop()
        try:
            children = sorted(parent.iterdir(), key=lambda item: item.name)
        except OSError as error:
            raise CarryForwardError("storage_read_failed") from error
        for child in children:
            try:
                info = child.lstat()
            except OSError as error:
                raise CarryForwardError("storage_read_failed") from error
            if info.st_dev != root_device:
                raise CarryForwardError("storage_device_changed")
            if (
                parent == root
                and allowed_top is not None
                and child.name not in allowed_top
            ):
                if child.name in (ignored_top or set()):
                    continue
                raise CarryForwardError("storage_entry_unknown")
            relative = child.relative_to(root).as_posix()
            metadata = {
                "mode": stat.S_IMODE(info.st_mode),
                "uid": info.st_uid,
                "gid": info.st_gid,
                "device": info.st_dev,
                "inode": info.st_ino,
                "mtime_ns": info.st_mtime_ns,
            }
            if stat.S_ISDIR(info.st_mode):
                result.append({"path": relative, "type": "directory", **metadata})
                pending.append(child)
            elif stat.S_ISREG(info.st_mode):
                total_bytes += info.st_size
                if total_bytes > MAX_CAPTURE_BYTES:
                    raise CarryForwardError("storage_capture_limit")
                hasher = hashlib.sha256()
                descriptor = -1
                try:
                    descriptor = os.open(
                        child, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                    )
                    opened = os.fstat(descriptor)
                    stable = (
                        opened.st_dev,
                        opened.st_ino,
                        opened.st_mode,
                        opened.st_uid,
                        opened.st_gid,
                        opened.st_size,
                        opened.st_mtime_ns,
                    )
                    expected = (
                        info.st_dev,
                        info.st_ino,
                        info.st_mode,
                        info.st_uid,
                        info.st_gid,
                        info.st_size,
                        info.st_mtime_ns,
                    )
                    if stable != expected or not stat.S_ISREG(opened.st_mode):
                        raise CarryForwardError("storage_changed_during_read")
                    with os.fdopen(descriptor, "rb") as source:
                        descriptor = -1
                        for block in iter(lambda: source.read(1024 * 1024), b""):
                            hasher.update(block)
                except OSError as error:
                    raise CarryForwardError("storage_read_failed") from error
                finally:
                    if descriptor >= 0:
                        os.close(descriptor)
                result.append(
                    {
                        "path": relative,
                        "type": "file",
                        **metadata,
                        "size": info.st_size,
                        "sha256": hasher.hexdigest(),
                    }
                )
            elif stat.S_ISLNK(info.st_mode):
                try:
                    target = os.readlink(child)
                except OSError as error:
                    raise CarryForwardError("storage_read_failed") from error
                result.append(
                    {
                        "path": relative,
                        "type": "symlink",
                        **metadata,
                        "target_sha256": hashlib.sha256(target.encode()).hexdigest(),
                    }
                )
            else:
                raise CarryForwardError("storage_entry_unknown")
            if len(result) > MAX_CAPTURE_ENTRIES:
                raise CarryForwardError("storage_capture_limit")
    return sorted(result, key=lambda item: item["path"])


def _root_identity(root: Path) -> dict[str, int]:
    try:
        info = root.lstat()
    except OSError as error:
        raise CarryForwardError("storage_root_unavailable") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise CarryForwardError("storage_root_invalid")
    return {
        "mode": stat.S_IMODE(info.st_mode),
        "uid": info.st_uid,
        "gid": info.st_gid,
        "device": info.st_dev,
        "inode": info.st_ino,
        "mtime_ns": info.st_mtime_ns,
    }


def _validate_directory(
    path: Path, *, modes: set[int], expected_uid: int | None, code: str
) -> None:
    try:
        info = path.lstat()
    except OSError as error:
        raise CarryForwardError(code) from error
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISDIR(info.st_mode)
        or stat.S_IMODE(info.st_mode) not in modes
        or (expected_uid is not None and info.st_uid != expected_uid)
    ):
        raise CarryForwardError(code)


def _validate_lock(path: Path, expected_uid: int | None) -> None:
    try:
        info = path.lstat()
    except OSError as error:
        raise CarryForwardError("storage_lock_invalid") from error
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or stat.S_IMODE(info.st_mode) != 0o600
        or (expected_uid is not None and info.st_uid != expected_uid)
    ):
        raise CarryForwardError("storage_lock_invalid")


def _load_closed_json(
    path: Path, fields: set[str], code: str, *, expected_uid: int | None = None
) -> dict[str, Any]:
    descriptor = -1
    try:
        info = path.lstat()
        if info.st_size > MAX_JSON_BYTES:
            raise CarryForwardError(code)
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) != (
            info.st_dev,
            info.st_ino,
            info.st_size,
            info.st_mtime_ns,
        ):
            raise CarryForwardError(code)
        with os.fdopen(descriptor, "r", encoding="utf-8") as source:
            descriptor = -1
            value = json.load(source)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise CarryForwardError(code) from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or stat.S_IMODE(info.st_mode) != 0o600
        or (expected_uid is not None and info.st_uid != expected_uid)
        or not isinstance(value, dict)
        or set(value) != fields
    ):
        raise CarryForwardError(code)
    return value


def _workspace_identity(
    runtime_root: Path, execution_id: int, attempt_id: int, expected_uid: int | None
) -> bool:
    attempt_root = runtime_root / "workspaces" / f"attempt-{attempt_id}"
    workspace = attempt_root / f"dlr-exec-{execution_id}"
    try:
        workspace.lstat()
    except FileNotFoundError:
        return False
    except OSError as error:
        raise CarryForwardError("workspace_identity_invalid") from error
    for path in (runtime_root, runtime_root / "workspaces", attempt_root, workspace):
        info = path.lstat()
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISDIR(info.st_mode)
            or (expected_uid is not None and info.st_uid != expected_uid)
        ):
            raise CarryForwardError("workspace_identity_invalid")
    if stat.S_IMODE(workspace.lstat().st_mode) != 0o700:
        raise CarryForwardError("workspace_identity_invalid")
    marker = _load_closed_json(
        workspace / ".dlr-execution-workspace",
        {"execution_id", "format"},
        "workspace_identity_invalid",
        expected_uid=expected_uid,
    )
    manifest = _load_closed_json(
        workspace / "input_manifest.json",
        {"execution_id", "files"},
        "workspace_identity_invalid",
        expected_uid=expected_uid,
    )
    if (
        marker != {"execution_id": execution_id, "format": 1}
        or manifest.get("execution_id") != execution_id
        or not isinstance(manifest.get("files"), list)
        or len(manifest["files"]) > 8
    ):
        raise CarryForwardError("workspace_identity_invalid")
    descriptor_fields = {
        "artifact_id",
        "ordinal",
        "mount_name",
        "original_filename",
        "content_type",
        "size_bytes",
        "sha256",
    }
    for ordinal, descriptor in enumerate(manifest["files"]):
        if (
            not isinstance(descriptor, dict)
            or set(descriptor) != descriptor_fields
            or not isinstance(descriptor.get("artifact_id"), int)
            or isinstance(descriptor.get("artifact_id"), bool)
            or descriptor["artifact_id"] <= 0
            or descriptor.get("ordinal") != ordinal
            or not isinstance(descriptor.get("mount_name"), str)
            or not re.fullmatch(
                r"input-[0-9]{2}(?:\.[A-Za-z0-9]+)?", descriptor["mount_name"]
            )
            or not isinstance(descriptor.get("original_filename"), str)
            or not isinstance(descriptor.get("content_type"), str)
            or not isinstance(descriptor.get("size_bytes"), int)
            or isinstance(descriptor.get("size_bytes"), bool)
            or descriptor["size_bytes"] < 0
            or not isinstance(descriptor.get("sha256"), str)
            or not DIGEST.fullmatch(descriptor["sha256"])
        ):
            raise CarryForwardError("workspace_identity_invalid")
    return True


def _journal_facts(
    runtime_root: Path,
    journal_root: Path,
    expected_uid: int | None = None,
    credential_hashes: dict[int, dict[str, str | None]] | None = None,
) -> dict[str, Any]:
    cleanup_fields = {
        "cleanup_token",
        "execution_id",
        "protocol_version",
        "workspace_path",
        "attempt_id",
    }
    attempt_fields = {
        "attempt_id",
        "attempt_no",
        "claim_token",
        "cleanup_token",
        "execution_id",
        "fencing_token",
        "lease_expires_at",
        "protocol_version",
        "workspace_path",
    }
    cleanup: list[dict[str, Any]] = []
    for path in sorted(journal_root.iterdir(), key=lambda item: item.name):
        match = re.fullmatch(
            r"execution-([1-9][0-9]*)-attempt-([1-9][0-9]*)\.cleanup\.json",
            path.name,
        )
        if not match:
            continue
        value = _load_closed_json(
            path, cleanup_fields, "cleanup_journal_invalid", expected_uid=expected_uid
        )
        execution_id, attempt_id = map(int, match.groups())
        if (
            value["execution_id"] != execution_id
            or value["attempt_id"] != attempt_id
            or value["protocol_version"] != 3
            or not isinstance(value["cleanup_token"], str)
            or not value["cleanup_token"]
            or not isinstance(value["workspace_path"], str)
            or value["workspace_path"]
            != f"/var/lib/dlr/runtime/workspaces/attempt-{attempt_id}/dlr-exec-{execution_id}"
        ):
            raise CarryForwardError("cleanup_journal_invalid")
        cleanup.append(
            {
                "execution_id": execution_id,
                "attempt_id": attempt_id,
                "cleanup_token_matches": (
                    hmac.compare_digest(
                        hashlib.sha256(value["cleanup_token"].encode()).hexdigest(),
                        expected,
                    )
                    if credential_hashes is not None
                    and isinstance(
                        expected := credential_hashes.get(attempt_id, {}).get(
                            "cleanup_token_hash"
                        ),
                        str,
                    )
                    else None
                ),
                "workspace_suffix": (
                    f"workspaces/attempt-{attempt_id}/dlr-exec-{execution_id}"
                ),
                "workspace_present": _workspace_identity(
                    runtime_root, execution_id, attempt_id, expected_uid
                ),
            }
        )
    attempt: list[dict[str, Any]] = []
    attempt_root = runtime_root / "attempt-journal"
    if attempt_root.exists():
        for path in sorted(attempt_root.iterdir(), key=lambda item: item.name):
            match = re.fullmatch(r"attempt-([1-9][0-9]*)\.attempt\.json", path.name)
            if not match:
                continue
            value = _load_closed_json(
                path,
                attempt_fields,
                "attempt_journal_invalid",
                expected_uid=expected_uid,
            )
            attempt_id = int(match.group(1))
            positive = ("execution_id", "attempt_id", "attempt_no", "fencing_token")
            if (
                any(
                    not isinstance(value[key], int)
                    or isinstance(value[key], bool)
                    or value[key] <= 0
                    for key in positive
                )
                or value["attempt_id"] != attempt_id
                or value["protocol_version"] != 3
                or not isinstance(value["claim_token"], str)
                or not value["claim_token"]
                or not isinstance(value["cleanup_token"], str)
                or not value["cleanup_token"]
                or not isinstance(value["workspace_path"], str)
                or value["workspace_path"]
                != (
                    f"/var/lib/dlr/runtime/workspaces/attempt-{attempt_id}/"
                    f"dlr-exec-{value['execution_id']}"
                )
            ):
                raise CarryForwardError("attempt_journal_invalid")
            attempt.append(
                {
                    "execution_id": value["execution_id"],
                    "attempt_id": attempt_id,
                    "attempt_no": value["attempt_no"],
                    "fencing_token": value["fencing_token"],
                    "claim_token_matches": (
                        hmac.compare_digest(
                            hashlib.sha256(value["claim_token"].encode()).hexdigest(),
                            expected_claim,
                        )
                        if credential_hashes is not None
                        and isinstance(
                            expected_claim := credential_hashes.get(attempt_id, {}).get(
                                "claim_token_hash"
                            ),
                            str,
                        )
                        else None
                    ),
                    "cleanup_token_matches": (
                        hmac.compare_digest(
                            hashlib.sha256(value["cleanup_token"].encode()).hexdigest(),
                            expected_cleanup,
                        )
                        if credential_hashes is not None
                        and isinstance(
                            expected_cleanup := credential_hashes.get(
                                attempt_id, {}
                            ).get("cleanup_token_hash"),
                            str,
                        )
                        else None
                    ),
                    "lease_expires_at": datetime_text(value["lease_expires_at"]),
                }
            )
    recovery: list[dict[str, Any]] = []
    recovery_root = journal_root / "sandbox-recovery"
    _validate_directory(
        recovery_root,
        modes={0o700},
        expected_uid=expected_uid,
        code="sandbox_recovery_invalid",
    )
    marker_fields = {
        "cgroup_name",
        "execution_id",
        "mount_name",
        "mount_path",
        "namespace_identity",
        "cgroup_device",
        "cgroup_inode",
    }
    namespace_fields = {
        "boot_id",
        "parent_device",
        "parent_inode",
        "root_device",
        "root_inode",
    }
    for path in sorted(recovery_root.iterdir(), key=lambda item: item.name):
        value = _load_closed_json(
            path, marker_fields, "sandbox_recovery_invalid", expected_uid=expected_uid
        )
        name = value.get("cgroup_name")
        attempt_match = (
            re.fullmatch(r"attempt-([1-9][0-9]*)-([1-9][0-9]*)", name)
            if isinstance(name, str)
            else None
        )
        preflight_match = (
            re.fullmatch(r"dlr-preflight-([0-9a-f]{16,64})", name)
            if isinstance(name, str)
            else None
        )
        if (
            (attempt_match is None and preflight_match is None)
            or path.name != f"sandbox-{name}.json"
            or value.get("mount_name") != ".dlr-sandbox-mount"
            or type(value.get("execution_id")) is not int
            or value["execution_id"] <= 0
            or type(value.get("cgroup_device")) is not int
            or value["cgroup_device"] < 0
            or type(value.get("cgroup_inode")) is not int
            or value["cgroup_inode"] < 0
        ):
            raise CarryForwardError("sandbox_recovery_invalid")
        identity = value.get("namespace_identity")
        if (
            not isinstance(identity, dict)
            or set(identity) != namespace_fields
            or not isinstance(identity.get("boot_id"), str)
            or not re.fullmatch(
                r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}",
                identity["boot_id"],
            )
            or any(
                type(identity[key]) is not int or identity[key] < 0
                for key in namespace_fields - {"boot_id"}
            )
        ):
            raise CarryForwardError("sandbox_recovery_invalid")
        if attempt_match is not None:
            execution_id, attempt_id = map(int, attempt_match.groups())
            expected_parent = runtime_root / "workspaces" / f"attempt-{attempt_id}"
            expected_mount = expected_parent / ".dlr-sandbox-mount"
            kind = "attempt"
            if value["execution_id"] != execution_id:
                raise CarryForwardError("sandbox_recovery_invalid")
        else:
            execution_id, attempt_id = 1, None
            expected_parent = runtime_root / name
            expected_mount = expected_parent / ".dlr-sandbox-mount"
            kind = "preflight"
            if value["execution_id"] != 1:
                raise CarryForwardError("sandbox_recovery_invalid")
        _validate_directory(
            expected_parent,
            modes={0o700},
            expected_uid=expected_uid,
            code="sandbox_recovery_invalid",
        )
        if value.get("mount_path") != str(expected_mount):
            raise CarryForwardError("sandbox_recovery_invalid")
        if expected_mount.exists():
            _validate_directory(
                expected_mount,
                modes={0o700},
                expected_uid=expected_uid,
                code="sandbox_recovery_invalid",
            )
        recovery.append(
            {
                "kind": kind,
                "cgroup_name": name,
                "execution_id": execution_id,
                "attempt_id": attempt_id,
                "namespace_identity": identity,
                "cgroup_device": value["cgroup_device"],
                "cgroup_inode": value["cgroup_inode"],
                "marker_fingerprint": digest(value),
            }
        )
    return {"cleanup": cleanup, "attempt": attempt, "sandbox_recovery": recovery}


def capture_files(
    runtime_root: Path,
    journal_root: Path,
    material_roots: dict[str, Path] | None = None,
    expected_uid: int | None = None,
    credential_hashes: dict[int, dict[str, str | None]] | None = None,
    attempt_statuses: dict[int, str] | None = None,
) -> dict[str, Any]:
    _validate_directory(
        runtime_root,
        modes={0o700, 0o711},
        expected_uid=expected_uid,
        code="runtime_root_invalid",
    )
    _validate_directory(
        journal_root,
        modes={0o700},
        expected_uid=expected_uid,
        code="journal_root_invalid",
    )
    _validate_lock(runtime_root / ".dlr-instance.lock", expected_uid)
    _validate_lock(journal_root / ".dlr-instance.lock", expected_uid)
    attempt_root = runtime_root / "attempt-journal"
    _validate_directory(
        attempt_root,
        modes={0o700},
        expected_uid=expected_uid,
        code="attempt_journal_root_invalid",
    )
    _validate_lock(attempt_root / ".dlr-instance.lock", expected_uid)
    workspaces = runtime_root / "workspaces"
    _validate_directory(
        workspaces,
        modes={0o700},
        expected_uid=expected_uid,
        code="workspace_root_invalid",
    )
    preflight_roots = {
        child.name
        for child in runtime_root.iterdir()
        if re.fullmatch(r"dlr-preflight-[0-9a-f]{16,64}", child.name)
    }
    runtime = _safe_tree(
        runtime_root,
        allowed_top={
            ".dlr-instance.lock",
            "attempt-journal",
            "version-cache",
            "workspaces",
            *preflight_roots,
        },
    )
    journal = _safe_tree(journal_root)
    materials = {
        name: {
            "root": _root_identity(path),
            "entries": (entries := _safe_tree(path)),
            "digest": digest(entries),
        }
        for name, path in sorted((material_roots or {}).items())
    }
    journal_facts = _journal_facts(
        runtime_root, journal_root, expected_uid, credential_hashes
    )
    associated_attempts = {
        item["attempt_id"]
        for name in ("cleanup", "attempt", "sandbox_recovery")
        for item in journal_facts[name]
        if type(item.get("attempt_id")) is int
    }
    workspace_device = workspaces.lstat().st_dev
    empty_attempt_shells = []
    for child in sorted(workspaces.iterdir(), key=lambda item: item.name):
        match = re.fullmatch(r"attempt-([1-9][0-9]*)", child.name)
        if match is None:
            continue
        before = child.lstat()
        children = list(child.iterdir())
        after = child.lstat()
        stable = (
            before.st_dev,
            before.st_ino,
            before.st_mode,
            before.st_uid,
            before.st_gid,
            before.st_mtime_ns,
        ) == (
            after.st_dev,
            after.st_ino,
            after.st_mode,
            after.st_uid,
            after.st_gid,
            after.st_mtime_ns,
        )
        if children:
            continue
        attempt_id = int(match.group(1))
        if (
            not stable
            or stat.S_ISLNK(before.st_mode)
            or not stat.S_ISDIR(before.st_mode)
            or stat.S_IMODE(before.st_mode) != 0o700
            or before.st_dev != workspace_device
            or (expected_uid is not None and before.st_uid != expected_uid)
        ):
            raise CarryForwardError("workspace_identity_invalid")
        status = (attempt_statuses or {}).get(attempt_id)
        if status is not None and status not in TERMINAL_ATTEMPTS:
            raise CarryForwardError("attempt_state_unknown")
        empty_attempt_shells.append(
            {
                "attempt_id": attempt_id,
                "classification": (
                    "deferred_responsibility_empty_shell"
                    if attempt_id in associated_attempts
                    else "terminal_attempt_empty_shell"
                    if status is not None
                    else "owned_empty_shell_without_db_row"
                ),
            }
        )
    empty_attempt_shells.sort(key=lambda item: item["attempt_id"])
    return {
        "runtime": {
            "root": _root_identity(runtime_root),
            "entries": runtime,
            "digest": digest(runtime),
        },
        "journal": {
            "root": _root_identity(journal_root),
            "entries": journal,
            "digest": digest(journal),
        },
        "materials": materials,
        "journal_facts": journal_facts,
        "empty_attempt_shells": empty_attempt_shells,
    }


def _validate_capture_digests(value: Any) -> None:
    if not isinstance(value, dict) or set(value) != {
        "runtime",
        "journal",
        "materials",
        "journal_facts",
        "empty_attempt_shells",
    }:
        raise CarryForwardError("file_evidence_invalid")
    for key in ("runtime", "journal"):
        tree = value[key]
        if (
            not isinstance(tree, dict)
            or set(tree) != {"root", "entries", "digest"}
            or not isinstance(tree["entries"], list)
            or tree["entries"] != sorted(tree["entries"], key=lambda item: item["path"])
            or len({item["path"] for item in tree["entries"]}) != len(tree["entries"])
            or tree["digest"] != digest(tree["entries"])
        ):
            raise CarryForwardError("file_evidence_invalid")
    for item in value["materials"].values():
        if item.get("digest") != digest(item.get("entries")):
            raise CarryForwardError("file_evidence_invalid")


def _window_ns(value: Any, start: int, end: int, code: str) -> None:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < start
        or value > end
    ):
        raise CarryForwardError(code)


def _entry_map(value: dict[str, Any], root: str) -> dict[str, dict[str, Any]]:
    return {item["path"]: item for item in value[root]["entries"]}


def _metadata_delta(
    before: dict[str, Any], after: dict[str, Any], allowed: set[str], code: str
) -> dict[str, Any]:
    if set(before) != set(after) or any(
        before[key] != after[key] for key in before if key not in allowed
    ):
        raise CarryForwardError(code)
    return {
        key: {"before": before[key], "after": after[key]}
        for key in sorted(allowed)
        if before[key] != after[key]
    }


def _validate_startup_proof(value: Any) -> dict[str, Any]:
    keys = {
        "container_id",
        "image_id",
        "started_at",
        "restart_count",
        "nonce",
        "window_start_ns",
        "window_end_ns",
        "preflight_receipt",
        "log_evidence_digest",
    }
    if not isinstance(value, dict) or set(value) != keys:
        raise CarryForwardError("startup_proof_invalid")
    if (
        not isinstance(value["container_id"], str)
        or not value["container_id"]
        or not isinstance(value["image_id"], str)
        or not value["image_id"]
        or not isinstance(value["started_at"], str)
        or not isinstance(value["restart_count"], int)
        or isinstance(value["restart_count"], bool)
        or value["restart_count"] != 0
        or not isinstance(value["nonce"], str)
        or re.fullmatch(r"[0-9a-f]{16,64}", value["nonce"]) is None
        or not isinstance(value["window_start_ns"], int)
        or isinstance(value["window_start_ns"], bool)
        or not isinstance(value["window_end_ns"], int)
        or value["window_end_ns"] < value["window_start_ns"]
    ):
        raise CarryForwardError("startup_proof_invalid")
    _digest_text(value["log_evidence_digest"], "startup_proof_invalid")
    receipt = value["preflight_receipt"]
    cleanup = receipt.get("cleanup") if isinstance(receipt, dict) else None
    capabilities = receipt.get("capabilities") if isinstance(receipt, dict) else None
    required_capabilities = {
        "adapter_control_plane_hidden",
        "adapter_mount_blocked",
        "bounded_output",
        "cgroup_kill",
        "cgroup_namespace_private",
        "cgroup_v2",
        "cpu_hard_limit",
        "memory_hard_limit",
        "mount_namespace",
        "no_new_privileges",
        "nofile_hard_limit",
        "pid_namespace",
        "pids_hard_limit",
        "preflight_passed",
        "sandbox_cleanup",
        "swap_hard_limit",
        "tmpfs_hard_limit",
    }
    hidden = (
        receipt.get("adapter_hidden_cgroup_paths", {})
        if isinstance(receipt, dict)
        else {}
    )
    namespace = (
        receipt.get("namespace_identity", {}) if isinstance(receipt, dict) else {}
    )
    if (
        not isinstance(receipt, dict)
        or receipt.get("cgroup_name") != f"dlr-preflight-{value['nonce']}"
        or receipt.get("status") != "passed"
        or receipt.get("workspace_residue") is not False
        or not isinstance(cleanup, dict)
        or cleanup.get("status") != "completed"
        or cleanup.get("residue") is not False
        or cleanup.get("error_code") is not None
        or cleanup.get("cgroup_name") != receipt.get("cgroup_name")
        or not isinstance(capabilities, dict)
        or set(capabilities) != required_capabilities
        or any(capabilities[key] is not True for key in required_capabilities)
        or receipt.get("adapter_control_pipe_fds") != []
        or set(hidden) != {"/run/dlr-cgroup", "/sys/fs/cgroup"}
        or any(
            item != {"read_blocked": True, "write_blocked": True}
            for item in hidden.values()
        )
        or receipt.get("agent_outside_attempt") is not True
        or receipt.get("helper_outside_attempt") is not True
        or receipt.get("probe_in_attempt") is not True
        or receipt.get("child_empty_after_kill") is not True
        or receipt.get("process_exited_after_kill") is not True
        or receipt.get("worker_cgroup_management")
        != {"child_limit_write_read": True, "parent_controllers_read": True}
        or not isinstance(namespace, dict)
        or set(namespace)
        != {"boot_id", "parent_device", "parent_inode", "root_device", "root_inode"}
        or namespace.get("parent_device") != namespace.get("root_device")
        or namespace.get("parent_inode") == namespace.get("root_inode")
        or any(
            re.search(r"(?i)(token|password|secret|cookie|authorization)", key)
            for key in receipt
        )
    ):
        raise CarryForwardError("startup_proof_invalid")
    return value


def _rfc3339_ns(value: Any) -> int:
    if not isinstance(value, str):
        raise CarryForwardError("startup_proof_invalid")
    match = re.fullmatch(
        r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}:\d{2})",
        value,
    )
    if match is None:
        raise CarryForwardError("startup_proof_invalid")
    zone = "+00:00" if match.group(3) == "Z" else match.group(3)
    try:
        seconds = int(dt.datetime.fromisoformat(match.group(1) + zone).timestamp())
    except ValueError as error:
        raise CarryForwardError("startup_proof_invalid") from error
    fraction = int((match.group(2) or "").ljust(9, "0"))
    return seconds * 1_000_000_000 + fraction


def compare_group2_startup_files(
    before: Any, after: Any, startup_proof: Any
) -> dict[str, Any]:
    _validate_capture_digests(before)
    _validate_capture_digests(after)
    proof = _validate_startup_proof(startup_proof)
    if (
        before["materials"] != after["materials"]
        or before["journal_facts"] != after["journal_facts"]
        or before["empty_attempt_shells"] != after["empty_attempt_shells"]
    ):
        raise CarryForwardError("group2_startup_files_changed")
    deltas: list[dict[str, Any]] = []
    runtime_root_delta = _metadata_delta(
        before["runtime"]["root"],
        after["runtime"]["root"],
        {"mtime_ns"},
        "group2_startup_files_changed",
    )
    if runtime_root_delta:
        mtime = after["runtime"]["root"]["mtime_ns"]
        if mtime < before["runtime"]["root"]["mtime_ns"]:
            raise CarryForwardError("group2_startup_files_changed")
        _window_ns(
            mtime,
            proof["window_start_ns"],
            proof["window_end_ns"],
            "group2_startup_files_changed",
        )
        deltas.append({"root": "runtime", "path": ".", "fields": runtime_root_delta})
    if before["journal"]["root"] != after["journal"]["root"]:
        raise CarryForwardError("group2_startup_files_changed")
    for root in ("runtime", "journal"):
        left, right = _entry_map(before, root), _entry_map(after, root)
        if set(left) != set(right):
            raise CarryForwardError("group2_startup_files_changed")
        for path in left:
            allowed = (
                {"mtime_ns"}
                if root == "journal" and path == "sandbox-recovery"
                else set()
            )
            changed = _metadata_delta(
                left[path], right[path], allowed, "group2_startup_files_changed"
            )
            if changed:
                mtime = right[path]["mtime_ns"]
                if mtime < left[path]["mtime_ns"]:
                    raise CarryForwardError("group2_startup_files_changed")
                _window_ns(
                    mtime,
                    proof["window_start_ns"],
                    proof["window_end_ns"],
                    "group2_startup_files_changed",
                )
                deltas.append({"root": root, "path": path, "fields": changed})
    expected = {("runtime", "."), ("journal", "sandbox-recovery")}
    if {(item["root"], item["path"]) for item in deltas} != expected:
        raise CarryForwardError("group2_startup_files_changed")
    return {"code": "group2_startup_files_ok", "allowed_deltas": deltas}


def _validate_probe_proof(
    value: Any, *, require_cleanup: bool = True
) -> dict[str, Any]:
    keys = {
        "adapter_id",
        "execution_id",
        "worker_id",
        "attempt_id",
        "window_start_ns",
        "window_end_ns",
        "probe_result",
        "cleanup",
        "log_evidence_digest",
        "event_refs",
    }
    if not isinstance(value, dict) or set(value) != keys:
        raise CarryForwardError("probe_proof_invalid")
    for key in ("adapter_id", "execution_id", "worker_id", "attempt_id"):
        _positive(value[key], "probe_proof_invalid")
    if (
        not isinstance(value["window_start_ns"], int)
        or isinstance(value["window_start_ns"], bool)
        or not isinstance(value["window_end_ns"], int)
        or value["window_end_ns"] < value["window_start_ns"]
    ):
        raise CarryForwardError("probe_proof_invalid")
    _digest_text(value["log_evidence_digest"], "probe_proof_invalid")
    if not isinstance(value["event_refs"], list) or not value["event_refs"]:
        raise CarryForwardError("probe_proof_invalid")
    for item in value["event_refs"]:
        if (
            not isinstance(item, dict)
            or set(item) != {"offset", "line_sha256"}
            or not isinstance(item["offset"], int)
            or isinstance(item["offset"], bool)
            or item["offset"] < 0
            or DIGEST.fullmatch(item["line_sha256"]) is None
        ):
            raise CarryForwardError("probe_proof_invalid")
    cleanup = value["cleanup"]
    if require_cleanup and (
        not isinstance(cleanup, dict)
        or set(cleanup) != {"row", "row_sha256"}
        or cleanup["row_sha256"] != digest(cleanup["row"])
    ):
        raise CarryForwardError("probe_cleanup_invalid")
    if not require_cleanup and cleanup is not None:
        raise CarryForwardError("probe_cleanup_invalid")
    return value


def compare_group2_probe_files(
    before: Any, after: Any, probe_proof: Any
) -> dict[str, Any]:
    _validate_capture_digests(before)
    _validate_capture_digests(after)
    proof = _validate_probe_proof(probe_proof)
    if (
        before["materials"] != after["materials"]
        or before["journal_facts"] != after["journal_facts"]
        or before["runtime"]["root"] != after["runtime"]["root"]
    ):
        raise CarryForwardError("group2_probe_files_changed")
    start, end = proof["window_start_ns"], proof["window_end_ns"]
    attempt_id = proof["attempt_id"]
    deltas: list[dict[str, Any]] = []
    for root in ("runtime", "journal"):
        left, right = _entry_map(before, root), _entry_map(after, root)
        new_paths = set(right) - set(left)
        removed = set(left) - set(right)
        expected_new = (
            {f"workspaces/attempt-{attempt_id}"} if root == "runtime" else set()
        )
        if removed or new_paths != expected_new:
            raise CarryForwardError("group2_probe_files_changed")
        for path in set(left):
            allowed: set[str] = set()
            if root == "runtime" and path in {
                "attempt-journal",
                "workspaces",
                "version-cache",
                "version-cache/entries",
            }:
                allowed = {"mtime_ns"}
            elif (
                root == "runtime"
                and path == "version-cache/.dlr-cache-reservations.json"
            ):
                allowed = {"inode", "mtime_ns"}
            elif root == "journal" and path == "sandbox-recovery":
                allowed = {"mtime_ns"}
            changed = _metadata_delta(
                left[path], right[path], allowed, "group2_probe_files_changed"
            )
            if changed:
                if "mtime_ns" not in changed:
                    raise CarryForwardError("group2_probe_files_changed")
                mtime = right[path]["mtime_ns"]
                if mtime < left[path]["mtime_ns"]:
                    raise CarryForwardError("group2_probe_files_changed")
                _window_ns(mtime, start, end, "group2_probe_files_changed")
                deltas.append({"root": root, "path": path, "fields": changed})
        for path in new_paths:
            item = right[path]
            if item.get("type") != "directory" or item.get("mode") != 0o700:
                raise CarryForwardError("group2_probe_files_changed")
            workspaces = right.get("workspaces")
            if not isinstance(workspaces, dict) or any(
                item.get(key) != workspaces.get(key) for key in ("uid", "gid", "device")
            ):
                raise CarryForwardError("group2_probe_files_changed")
            _window_ns(item.get("mtime_ns"), start, end, "group2_probe_files_changed")
            if any(other.startswith(path + "/") for other in right):
                raise CarryForwardError("group2_probe_files_changed")
            deltas.append({"root": root, "path": path, "fields": {"created": item}})
    journal_root_delta = _metadata_delta(
        before["journal"]["root"],
        after["journal"]["root"],
        {"mtime_ns"},
        "group2_probe_files_changed",
    )
    if journal_root_delta:
        mtime = after["journal"]["root"]["mtime_ns"]
        if mtime < before["journal"]["root"]["mtime_ns"]:
            raise CarryForwardError("group2_probe_files_changed")
        _window_ns(mtime, start, end, "group2_probe_files_changed")
        deltas.append({"root": "journal", "path": ".", "fields": journal_root_delta})
    expected_shells = [
        *before["empty_attempt_shells"],
        {
            "attempt_id": attempt_id,
            "classification": "owned_empty_shell_without_db_row",
        },
    ]
    if after["empty_attempt_shells"] != sorted(
        expected_shells, key=lambda item: item["attempt_id"]
    ):
        raise CarryForwardError("group2_probe_files_changed")
    return {"code": "group2_probe_files_ok", "allowed_deltas": deltas}


def compare_group2_post_probe(
    manifest: Any,
    before_db: Any,
    after_db: Any,
    before_files: Any,
    after_files: Any,
    probe_proof: Any,
) -> dict[str, Any]:
    validated = validate_manifest(manifest)
    if manifest_mode(validated) != GROUP2_MODE:
        raise CarryForwardError("manifest_mode_invalid")
    proof = _validate_probe_proof(probe_proof)
    for value in (before_db, after_db):
        if not isinstance(value, dict):
            raise CarryForwardError("group2_post_probe_invalid")
        validate_projection_evidence(value.get("projection"), required=AUDITED_TABLES)
        validate_projection_evidence(
            value.get("asset_projection"), required=ASSET_TABLES
        )
        validate_group2_protected_rows(value.get("protected_rows"))
        validate_group2_schema_shape(value.get("schema_shape"))
    if (
        before_db["projection"] != validated["old_runtime_projection"]
        or before_db["responsibilities"] != validated["responsibilities"]
        or before_db["protected_rows"] != validated["protected_rows"]
        or before_db["asset_projection"] != validated["asset_projection"]
        or before_db["schema_shape"] != validated["schema_shape"]
        or before_db["schema_inventory"] != validated["schema_inventory"]
        or after_db["asset_projection"] != before_db["asset_projection"]
        or after_db["schema_shape"] != before_db["schema_shape"]
        or after_db["schema_inventory"] != before_db["schema_inventory"]
        or after_db["responsibilities"] != before_db["responsibilities"]
    ):
        raise CarryForwardError("group2_post_probe_changed")
    before_ids = before_db["protected_rows"]
    if (
        proof["adapter_id"] in before_ids["adapter_ids"]
        or proof["execution_id"] in before_ids["execution_ids"]
        or proof["attempt_id"] in before_ids["attempt_ids"]
    ):
        raise CarryForwardError("probe_identity_not_new")
    cleanup_row = proof["cleanup"]["row"]
    if (
        not isinstance(cleanup_row, dict)
        or cleanup_row.get("adapter_id") != proof["adapter_id"]
        or cleanup_row.get("worker_id") != proof["worker_id"]
        or cleanup_row.get("status") != "completed"
        or cleanup_row.get("error_code") is not None
        or not isinstance(cleanup_row.get("attempts"), int)
        or isinstance(cleanup_row.get("attempts"), bool)
        or not 1 <= cleanup_row["attempts"] <= 3
    ):
        raise CarryForwardError("probe_cleanup_invalid")

    def timestamp_ns(value: Any) -> int:
        if not isinstance(value, dict) or set(value) != {"$datetime"}:
            raise CarryForwardError("probe_timestamp_invalid")
        try:
            parsed = dt.datetime.fromisoformat(value["$datetime"])
        except (TypeError, ValueError) as error:
            raise CarryForwardError("probe_timestamp_invalid") from error
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return int(parsed.timestamp() * 1_000_000_000)

    cleanup_times = [
        timestamp_ns(cleanup_row[key])
        for key in ("created_at", "updated_at", "completed_at")
    ]
    if cleanup_times != sorted(cleanup_times) or any(
        value < proof["window_start_ns"] or value > proof["window_end_ns"]
        for value in cleanup_times
    ):
        raise CarryForwardError("probe_timestamp_invalid")
    for name in AUDITED_TABLES:
        left = before_ids["tables"][name]["rows"]
        right = after_db["protected_rows"]["tables"][name]["rows"]
        left_by_pk = {digest(row["pk"]): row for row in left}
        right_by_pk = {digest(row["pk"]): row for row in right}
        if not set(left_by_pk).issubset(right_by_pk):
            raise CarryForwardError("group2_post_probe_changed")
        for key, old in left_by_pk.items():
            new = right_by_pk[key]
            if name == "global_execution_admission":
                if old["stable_hash"] != new["stable_hash"]:
                    raise CarryForwardError("group2_post_probe_changed")
                old_updated = timestamp_ns(old["updated_at"])
                new_updated = timestamp_ns(new["updated_at"])
                if (
                    new_updated <= old_updated
                    or new_updated < proof["window_start_ns"]
                    or new_updated > proof["window_end_ns"]
                ):
                    raise CarryForwardError("group2_post_probe_changed")
            elif old != new:
                raise CarryForwardError("group2_post_probe_changed")
        added = [right_by_pk[key] for key in set(right_by_pk) - set(left_by_pk)]
        if name == "worker_cleanup_requests":
            if len(added) != 1 or added[0]["hash"] != proof["cleanup"]["row_sha256"]:
                raise CarryForwardError("probe_cleanup_invalid")
        elif added:
            raise CarryForwardError("group2_post_probe_changed")
    file_result = compare_group2_probe_files(before_files, after_files, proof)
    return {
        "code": "group2_post_probe_ok",
        "cleanup_row_sha256": proof["cleanup"]["row_sha256"],
        "post_db_digest": digest(
            {
                "protected_rows": after_db["protected_rows"],
                "asset_projection": after_db["asset_projection"],
                "schema_shape": after_db["schema_shape"],
            }
        ),
        "file_delta_digest": digest(file_result["allowed_deltas"]),
    }


def validate_file_responsibilities(
    evidence: dict[str, Any], responsibilities: dict[str, Any]
) -> None:
    runtime_entries = {entry["path"]: entry for entry in evidence["runtime"]["entries"]}
    journal_entries = {entry["path"]: entry for entry in evidence["journal"]["entries"]}
    runtime_paths = set(runtime_entries)
    journal_paths = {entry["path"] for entry in evidence["journal"]["entries"]}
    if any(
        entry.get("type") == "symlink" for entry in evidence["journal"]["entries"]
    ) or any(
        entry.get("type") == "symlink"
        and not entry.get("path", "").startswith("version-cache/entries/")
        for entry in evidence["runtime"]["entries"]
    ):
        raise CarryForwardError("responsibility_symlink_rejected")
    cleanup_facts = evidence.get("journal_facts", {}).get("cleanup", [])
    attempt_facts = evidence.get("journal_facts", {}).get("attempt", [])
    selected_ids = {item["execution_id"] for item in responsibilities["executions"]}
    selected_attempts = {
        attempt["attempt_id"]: item["execution_id"]
        for item in responsibilities["executions"]
        for attempt in item.get("attempts", [])
    }
    deferred_attempts = {
        attempt_id: item["execution_id"]
        for item in responsibilities["executions"]
        for attempt_id in item.get("deferred_attempt_ids", [])
    }
    recovery_facts = evidence.get("journal_facts", {}).get("sandbox_recovery", [])
    empty_shells = evidence.get("empty_attempt_shells")
    if not isinstance(empty_shells, list) or any(
        not isinstance(item, dict)
        or set(item) != {"attempt_id", "classification"}
        or type(item["attempt_id"]) is not int
        or item["attempt_id"] <= 0
        or item["classification"]
        not in {
            "deferred_responsibility_empty_shell",
            "terminal_attempt_empty_shell",
            "owned_empty_shell_without_db_row",
        }
        for item in empty_shells
    ):
        raise CarryForwardError("workspace_identity_invalid")
    empty_shell_ids = [item["attempt_id"] for item in empty_shells]
    if empty_shell_ids != sorted(set(empty_shell_ids)):
        raise CarryForwardError("workspace_identity_invalid")
    captured_empty_ids = {
        int(match.group(1))
        for path, entry in runtime_entries.items()
        if (match := re.fullmatch(r"workspaces/attempt-([1-9][0-9]*)", path))
        and entry.get("type") == "directory"
        and not any(other.startswith(f"{path}/") for other in runtime_paths)
    }
    if captured_empty_ids != set(empty_shell_ids):
        raise CarryForwardError("workspace_identity_invalid")
    associated_shell_ids = {
        item["attempt_id"]
        for item in cleanup_facts + attempt_facts + recovery_facts
        if type(item.get("attempt_id")) is int
    }
    shell_classifications = {
        item["attempt_id"]: item["classification"] for item in empty_shells
    }
    for attempt_id, classification in shell_classifications.items():
        deferred_execution = deferred_attempts.get(attempt_id)
        if classification == "deferred_responsibility_empty_shell":
            if (
                attempt_id not in associated_shell_ids
                or deferred_execution is None
                or selected_attempts.get(attempt_id) != deferred_execution
                or any(
                    item.get("execution_id") != deferred_execution
                    for item in cleanup_facts + attempt_facts + recovery_facts
                    if item.get("attempt_id") == attempt_id
                )
            ):
                raise CarryForwardError("workspace_identity_invalid")
        elif attempt_id in associated_shell_ids:
            raise CarryForwardError("workspace_identity_invalid")
    retired_preflight_names = {
        marker["cgroup_name"]
        for marker in recovery_facts
        if marker.get("kind") == "preflight"
    }
    if any(
        fact["execution_id"] not in selected_ids
        for fact in cleanup_facts + attempt_facts
    ):
        raise CarryForwardError("unselected_storage_responsibility")
    cache_allowed = {
        "version-cache",
        "version-cache/entries",
        "version-cache/.dlr-cache-reservations.json",
        "version-cache/.dlr-cache-reservations.lock",
    }
    for path, entry in runtime_entries.items():
        match = re.search(r"(^|/)dlr-exec-([1-9][0-9]*)($|/)", path)
        if match and int(match.group(2)) not in selected_ids:
            raise CarryForwardError("unselected_storage_responsibility")
        if path == ".dlr-instance.lock" and entry.get("type") != "file":
            raise CarryForwardError("storage_lock_invalid")
        if (
            path.startswith("attempt-journal/")
            and path != "attempt-journal/.dlr-instance.lock"
            and (
                not re.fullmatch(
                    r"attempt-journal/attempt-[1-9][0-9]*\.attempt\.json", path
                )
                or entry.get("type") != "file"
            )
        ):
            raise CarryForwardError("attempt_journal_unknown")
        if path.startswith("version-cache"):
            if path not in cache_allowed and not path.startswith(
                "version-cache/entries/"
            ):
                raise CarryForwardError("version_cache_unknown")
            if path in {"version-cache", "version-cache/entries"} and (
                entry.get("type") != "directory" or entry.get("mode") != 0o711
            ):
                raise CarryForwardError("version_cache_invalid")
            if path == "version-cache/.dlr-cache-reservations.json" and (
                entry.get("type") != "file" or entry.get("mode") != 0o600
            ):
                raise CarryForwardError("version_cache_invalid")
            if path == "version-cache/.dlr-cache-reservations.lock" and (
                entry.get("type") != "file" or entry.get("mode") != 0o644
            ):
                raise CarryForwardError("version_cache_invalid")
        if path == "workspaces":
            if entry.get("type") != "directory" or entry.get("mode") != 0o700:
                raise CarryForwardError("workspace_root_invalid")
            continue
        if path.startswith("workspaces/"):
            parts = path.split("/")
            attempt_match = re.fullmatch(r"attempt-([1-9][0-9]*)", parts[1])
            if not attempt_match:
                raise CarryForwardError("workspace_entry_unknown")
            attempt_id = int(attempt_match.group(1))
            if len(parts) == 2:
                if entry.get("type") != "directory" or entry.get("mode") != 0o700:
                    raise CarryForwardError("workspace_identity_invalid")
                continue
            execution_id = deferred_attempts.get(attempt_id)
            if (
                execution_id is None
                or selected_attempts.get(attempt_id) != execution_id
            ):
                raise CarryForwardError("workspace_entry_unknown")
            expected = f"dlr-exec-{execution_id}"
            if parts[2] not in {expected, ".dlr-sandbox-mount"}:
                raise CarryForwardError("workspace_identity_invalid")
        preflight = path.split("/", 1)[0]
        if (
            preflight.startswith("dlr-preflight-")
            and preflight not in retired_preflight_names
        ):
            raise CarryForwardError("sandbox_recovery_unselected")
    for path in journal_paths:
        if path == ".dlr-instance.lock":
            if journal_entries[path].get("type") != "file":
                raise CarryForwardError("storage_lock_invalid")
            continue
        if path == "sandbox-recovery":
            if journal_entries[path].get("type") != "directory":
                raise CarryForwardError("sandbox_recovery_unknown")
            continue
        if path.startswith("sandbox-recovery/"):
            if (
                not re.fullmatch(
                    r"sandbox-recovery/sandbox-(?:attempt-[1-9][0-9]*-[1-9][0-9]*|dlr-preflight-[0-9a-f]{16,64})\.json",
                    path,
                )
                or journal_entries[path].get("type") != "file"
            ):
                raise CarryForwardError("sandbox_recovery_unknown")
            continue
        if not re.fullmatch(
            r"execution-[1-9][0-9]*-attempt-[1-9][0-9]*\.cleanup\.json", path
        ):
            raise CarryForwardError("cleanup_journal_unknown")
    for item in responsibilities["executions"]:
        execution_id = item["execution_id"]
        attempt_ids = item["attempt_ids"]
        related_runtime = {
            path
            for path in runtime_paths
            if re.search(rf"(^|/)dlr-exec-{execution_id}($|/)", path)
        }
        related_cleanup = {
            path
            for path in journal_paths
            if re.fullmatch(
                rf"execution-{execution_id}-attempt-[1-9][0-9]*\.cleanup\.json", path
            )
        }
        related_attempt = {
            path
            for path in runtime_paths
            if re.fullmatch(r"attempt-journal/attempt-[1-9][0-9]*\.attempt\.json", path)
            and int(path.rsplit("-", 1)[1].split(".", 1)[0]) in attempt_ids
        }
        if item["cleanup"] == "not_applicable" and (
            related_runtime
            or related_cleanup
            or related_attempt
            or any(fact["execution_id"] == execution_id for fact in cleanup_facts)
            or any(fact["execution_id"] == execution_id for fact in attempt_facts)
        ):
            raise CarryForwardError("never_claimed_storage_present")
        if item["cleanup"] == "completed" and (
            related_runtime or related_cleanup or related_attempt
        ):
            raise CarryForwardError("completed_storage_present")
        if item["cleanup"] == "deferred_preserved":
            expected = {
                f"execution-{execution_id}-attempt-{attempt_id}.cleanup.json"
                for attempt_id in item["deferred_attempt_ids"]
            }
            if related_cleanup != expected:
                raise CarryForwardError("deferred_journal_missing")
            facts = [
                fact for fact in cleanup_facts if fact["execution_id"] == execution_id
            ]
            attempts = {attempt["attempt_id"]: attempt for attempt in item["attempts"]}
            if (
                not facts
                or any(
                    fact["attempt_id"] not in item["deferred_attempt_ids"]
                    for fact in facts
                )
                or any(fact.get("cleanup_token_matches") is not True for fact in facts)
            ):
                raise CarryForwardError("deferred_journal_identity_invalid")
        attempts = {
            attempt["attempt_id"]: attempt for attempt in item.get("attempts", [])
        }
        for fact in (
            fact for fact in attempt_facts if fact["execution_id"] == execution_id
        ):
            attempt = attempts.get(fact["attempt_id"])
            if not attempt or any(
                fact.get(key) is not True
                if key in {"claim_token_matches", "cleanup_token_matches"}
                else fact[key] != attempt.get(key)
                for key in (
                    "attempt_no",
                    "fencing_token",
                    "claim_token_matches",
                    "cleanup_token_matches",
                    "lease_expires_at",
                )
            ):
                raise CarryForwardError("attempt_journal_identity_invalid")
    for marker in evidence.get("journal_facts", {}).get("sandbox_recovery", []):
        if marker["kind"] == "attempt" and (
            deferred_attempts.get(marker["attempt_id"]) != marker["execution_id"]
        ):
            raise CarryForwardError("sandbox_recovery_unselected")


def _read_text(path: Path) -> str:
    try:
        return path.read_text().strip()
    except OSError as error:
        raise CarryForwardError("kernel_read_failed") from error


def _mountinfo(
    path: Path,
    *,
    budget: dict[str, int] | None = None,
    deadline: float | None = None,
) -> list[dict[str, str]]:
    def decode(value: str) -> str:
        return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), value)

    if deadline is not None and time.monotonic() > deadline:
        raise CarryForwardError("kernel_inventory_limit")
    try:
        raw_bytes = path.read_bytes()
        raw = raw_bytes.decode()
    except OSError as error:
        raise CarryForwardError("kernel_identity_unknown") from error
    except UnicodeError as error:
        raise CarryForwardError("kernel_identity_unknown") from error
    if len(raw_bytes) > 8 * 1024 * 1024:
        raise CarryForwardError("kernel_inventory_limit")
    if budget is not None:
        budget["mountinfo_bytes"] = budget.get("mountinfo_bytes", 0) + len(raw_bytes)
        if budget["mountinfo_bytes"] > 128 * 1024 * 1024:
            raise CarryForwardError("kernel_inventory_limit")
    result = []
    for line in raw.splitlines():
        fields = line.split()
        try:
            separator = fields.index("-")
            result.append(
                {
                    "major_minor": fields[2],
                    "root": decode(fields[3]),
                    "mountpoint": decode(fields[4]),
                    "filesystem": fields[separator + 1],
                    "source": decode(fields[separator + 2]),
                }
            )
        except (ValueError, IndexError) as error:
            raise CarryForwardError("kernel_identity_unknown") from error
    return result


def _docker_text(arguments: list[str]) -> str:
    try:
        return subprocess.run(
            arguments,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError) as error:
        raise CarryForwardError("kernel_identity_unknown") from error


def _worker_runtime_config(config: Any) -> dict[str, Any]:
    if not isinstance(config, dict):
        raise CarryForwardError("kernel_identity_unknown")
    environment = config.get("Env")
    if not isinstance(environment, list) or not all(
        isinstance(item, str) and "=" in item for item in environment
    ):
        raise CarryForwardError("kernel_identity_unknown")
    values = dict(item.split("=", 1) for item in environment)
    runtime = values.get("DLR_RUNTIME_ROOT", "/var/lib/dlr/runtime")
    result = {
        "user": config.get("User") or "0",
        "runtime_root": runtime,
        "journal_root": values.get(
            "DLR_WORKSPACE_CLEANUP_JOURNAL_ROOT", "/var/lib/dlr/journal"
        ),
        "attempt_journal_root": values.get(
            "DLR_ATTEMPT_JOURNAL_ROOT", f"{runtime}/attempt-journal"
        ),
        "cgroup_path": values.get("DLR_SANDBOX_CGROUP_PATH"),
    }
    if result != {
        "user": result["user"],
        "runtime_root": "/var/lib/dlr/runtime",
        "journal_root": "/var/lib/dlr/journal",
        "attempt_journal_root": "/var/lib/dlr/runtime/attempt-journal",
        "cgroup_path": "/run/dlr-cgroup",
    } or not re.fullmatch(r"[0-9]+(?::[0-9]+)?", result["user"]):
        raise CarryForwardError("kernel_identity_unknown")
    return result


def _delegated_worker_root(cgroup: str, control_group: str) -> Path:
    cgroup_path = Path(cgroup.removeprefix("0::"))
    parent_path = Path(control_group)
    try:
        delegated = cgroup_path.relative_to(parent_path)
    except ValueError as error:
        raise CarryForwardError("kernel_identity_unknown") from error
    if len(delegated.parts) != 2 or delegated.parts[-1] != "agent":
        raise CarryForwardError("kernel_identity_unknown")
    return Path("/sys/fs/cgroup").joinpath(*parent_path.parts[1:], delegated.parts[0])


def _worker_authority(
    container: str,
    runtime_volume: str,
    journal_volume: str,
    control_group: str,
) -> dict[str, Any]:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]*", container):
        raise CarryForwardError("kernel_identity_unknown")
    container_id = _docker_text(["docker", "inspect", container, "--format", "{{.Id}}"])
    image_id = _docker_text(["docker", "inspect", container, "--format", "{{.Image}}"])
    started_at = _docker_text(
        ["docker", "inspect", container, "--format", "{{.State.StartedAt}}"]
    )
    pid_text = _docker_text(
        ["docker", "inspect", container, "--format", "{{.State.Pid}}"]
    )
    labels = {
        "com.docker.compose.project": _docker_text(
            [
                "docker",
                "inspect",
                container,
                "--format",
                '{{index .Config.Labels "com.docker.compose.project"}}',
            ]
        ),
        "com.docker.compose.service": _docker_text(
            [
                "docker",
                "inspect",
                container,
                "--format",
                '{{index .Config.Labels "com.docker.compose.service"}}',
            ]
        ),
    }
    try:
        runtime_config = _worker_runtime_config(
            json.loads(
                _docker_text(
                    ["docker", "inspect", container, "--format", "{{json .Config}}"]
                )
            )
        )
    except json.JSONDecodeError as error:
        raise CarryForwardError("kernel_identity_unknown") from error
    try:
        pid = int(pid_text)
    except ValueError as error:
        raise CarryForwardError("kernel_identity_unknown") from error
    if pid <= 0 or labels["com.docker.compose.service"] != "worker":
        raise CarryForwardError("kernel_identity_unknown")
    stat_fields = _read_text(Path(f"/proc/{pid}/stat")).split()
    cgroup = _read_text(Path(f"/proc/{pid}/cgroup"))
    if (
        len(stat_fields) < 22
        or not cgroup.startswith(f"0::{control_group}/")
        or not cgroup.endswith("/agent")
    ):
        raise CarryForwardError("kernel_identity_unknown")
    worker_root = _delegated_worker_root(cgroup, control_group)
    worker_root_info = worker_root.lstat()
    parent = Path("/sys/fs/cgroup") / control_group.removeprefix("/")
    parent_info = parent.lstat()
    process_cgroup_info = Path(f"/proc/{pid}/root/sys/fs/cgroup").lstat()
    if (worker_root_info.st_dev, worker_root_info.st_ino) != (
        process_cgroup_info.st_dev,
        process_cgroup_info.st_ino,
    ):
        raise CarryForwardError("kernel_identity_unknown")
    volume_identities: dict[str, Any] = {}
    mounts = _mountinfo(Path(f"/proc/{pid}/mountinfo"))
    for name, volume, target in (
        ("runtime", runtime_volume, "/var/lib/dlr/runtime"),
        ("journal", journal_volume, "/var/lib/dlr/journal"),
    ):
        mountpoint = _docker_text(
            ["docker", "volume", "inspect", volume, "--format", "{{.Mountpoint}}"]
        )
        source_info = Path(mountpoint).lstat()
        process_info = Path(f"/proc/{pid}/root{target}").lstat()
        matches = [item for item in mounts if item["mountpoint"] == target]
        if len(matches) != 1 or (source_info.st_dev, source_info.st_ino) != (
            process_info.st_dev,
            process_info.st_ino,
        ):
            raise CarryForwardError("kernel_identity_unknown")
        volume_identities[name] = {
            "name": volume,
            "device": source_info.st_dev,
            "inode": source_info.st_ino,
            "mount": matches[0],
        }
    authority = {
        "container_id": container_id,
        "image_id": image_id,
        "started_at": started_at,
        "pid": pid,
        "pid_starttime": stat_fields[21],
        "mount_namespace": os.readlink(f"/proc/{pid}/ns/mnt"),
        "cgroup_namespace": os.readlink(f"/proc/{pid}/ns/cgroup"),
        "parent_device": parent_info.st_dev,
        "parent_inode": parent_info.st_ino,
        "root_device": worker_root_info.st_dev,
        "root_inode": worker_root_info.st_ino,
        "labels": labels,
        "runtime_config": runtime_config,
        "volumes": volume_identities,
    }
    if _read_text(Path(f"/proc/{pid}/stat")).split()[21] != stat_fields[21]:
        raise CarryForwardError("kernel_identity_unknown")
    return authority


def _mount_root_contains(root: str, candidate: str) -> bool:
    try:
        Path(candidate).relative_to(Path(root))
    except ValueError:
        return False
    return True


def _selected_mount_matches(
    mount: dict[str, str], targets: set[tuple[str, str, str]]
) -> bool:
    return any(
        mount["filesystem"] == filesystem
        and mount["major_minor"] == major_minor
        and _mount_root_contains(root, mount["root"])
        for filesystem, major_minor, root in targets
    )


def _related_mount_namespaces(authority: dict[str, Any]) -> dict[str, Any]:
    targets = {
        (
            item["mount"]["filesystem"],
            item["mount"]["major_minor"],
            item["mount"]["root"],
        )
        for item in authority["volumes"].values()
    }
    old_namespaces = {
        authority.get("mount_namespace"),
        authority.get("cgroup_namespace"),
    }
    if None in old_namespaces or not all(
        isinstance(value, str)
        and re.fullmatch(r"(?:mnt|cgroup):\[[1-9][0-9]*\]", value)
        for value in old_namespaces
    ):
        raise CarryForwardError("kernel_identity_unknown")
    scanned = _scan_related_mount_namespaces(targets, old_namespaces)
    return {
        key: value for key, value in scanned.items()
        if key not in {"related", "pins"}
    }


def _scan_related_mount_namespaces(
    targets: set[tuple[str, str, str]], tracked_namespaces: set[str], *,
    include_records: bool = False,
) -> dict[str, Any]:
    if (
        not isinstance(targets, set) or not targets
        or any(
            not isinstance(item, tuple) or len(item) != 3
            or any(not isinstance(value, str) or not value for value in item)
            for item in targets
        )
        or not isinstance(tracked_namespaces, set)
        or any(
            not isinstance(value, str)
            or re.fullmatch(r"(?:mnt|cgroup):\[[1-9][0-9]*\]", value) is None
            for value in tracked_namespaces
        )
    ):
        raise CarryForwardError("kernel_identity_unknown")
    old_namespaces = tracked_namespaces
    proc = Path("/proc")
    deadline = time.monotonic() + 20
    budget = {"mountinfo_bytes": 0, "fd_entries": 0}
    try:
        pids = sorted(int(path.name) for path in proc.iterdir() if path.name.isdigit())
    except OSError as error:
        raise CarryForwardError("kernel_identity_unknown") from error
    tasks: list[tuple[int, int, Path]] = []
    for pid in pids:
        if time.monotonic() > deadline:
            raise CarryForwardError("kernel_inventory_limit")
        process = proc / str(pid)
        tasks.append((pid, pid, process))
        task_root = proc / str(pid) / "task"
        try:
            tids = sorted(
                int(path.name) for path in task_root.iterdir() if path.name.isdigit()
            )
        except FileNotFoundError:
            tids = []
        except OSError as error:
            if process.exists():
                raise CarryForwardError("kernel_identity_unknown") from error
            continue
        tasks.extend((pid, tid, task_root / str(tid)) for tid in tids if tid != pid)
        if len(tasks) > 8192:
            raise CarryForwardError("kernel_inventory_limit")

    representatives: dict[str, tuple[int, int, Path]] = {}
    task_namespaces: dict[tuple[int, int], tuple[str, str]] = {}
    for pid, tid, task in tasks:
        if time.monotonic() > deadline:
            raise CarryForwardError("kernel_inventory_limit")
        try:
            mount_namespace = os.readlink(task / "ns/mnt")
            cgroup_namespace = os.readlink(task / "ns/cgroup")
        except FileNotFoundError:
            continue
        except OSError as error:
            if task.exists():
                raise CarryForwardError("kernel_identity_unknown") from error
            continue
        if not re.fullmatch(
            r"mnt:\[[1-9][0-9]*\]", mount_namespace
        ) or not re.fullmatch(r"cgroup:\[[1-9][0-9]*\]", cgroup_namespace):
            raise CarryForwardError("kernel_identity_unknown")
        representatives.setdefault(mount_namespace, (pid, tid, task))
        task_namespaces[(pid, tid)] = (mount_namespace, cgroup_namespace)
        if len(representatives) > 2048:
            raise CarryForwardError("kernel_inventory_limit")

    member_namespaces = {
        namespace for namespaces in task_namespaces.values() for namespace in namespaces
    }

    related: list[dict[str, Any]] = []
    namespace_pins: list[dict[str, Any]] = []
    unknown_pins: list[dict[str, Any]] = []
    for namespace, (pid, tid, task) in sorted(representatives.items()):
        try:
            mounts = _mountinfo(task / "mountinfo", budget=budget, deadline=deadline)
        except CarryForwardError:
            if not task.exists():
                continue
            raise
        matches = [item for item in mounts if _selected_mount_matches(item, targets)]
        if matches:
            record = {"namespace": namespace, "mounts_digest": digest(matches)}
            if include_records:
                members = []
                for (member_pid, member_tid), namespaces in sorted(task_namespaces.items()):
                    if namespaces[0] != namespace:
                        continue
                    task = (proc / str(member_pid) / "task" / str(member_tid))
                    try:
                        stat_fields = _read_text(task / "stat").split()
                        cgroup = _read_text(task / "cgroup")
                    except CarryForwardError:
                        if task.exists():
                            raise
                        continue
                    if len(stat_fields) < 22:
                        raise CarryForwardError("kernel_identity_unknown")
                    members.append({
                        "pid": member_pid, "tid": member_tid,
                        "pid_starttime": stat_fields[21], "cgroup": cgroup,
                    })
                record.update(mounts=matches, members=members)
            related.append(record)
        for item in mounts:
            if item.get("filesystem") != "nsfs":
                continue
            pinned = {
                value
                for value in (item.get("root"), item.get("source"))
                if isinstance(value, str)
                and re.fullmatch(r"(?:mnt|cgroup):\[[1-9][0-9]*\]", value)
            }
            for value in sorted(pinned):
                record = {
                    "kind": "nsfs",
                    "owner_mount_namespace": namespace,
                    "pinned": value,
                    "mountpoint_digest": digest(item.get("mountpoint")),
                }
                if value in old_namespaces:
                    namespace_pins.append(record)
                elif value not in member_namespaces:
                    unknown_pins.append(record)

    fd_pins: list[dict[str, Any]] = []
    for pid, tid, task in tasks:
        if (pid, tid) not in task_namespaces:
            continue
        if time.monotonic() > deadline:
            raise CarryForwardError("kernel_inventory_limit")
        fd_root = task / "fd"
        try:
            descriptors = sorted(fd_root.iterdir(), key=lambda path: path.name)
        except FileNotFoundError:
            continue
        except OSError as error:
            if task.exists():
                raise CarryForwardError("kernel_identity_unknown") from error
            continue
        budget["fd_entries"] += len(descriptors)
        if budget["fd_entries"] > 65536:
            raise CarryForwardError("kernel_inventory_limit")
        for descriptor in descriptors:
            if time.monotonic() > deadline:
                raise CarryForwardError("kernel_inventory_limit")
            try:
                target = os.readlink(descriptor)
            except FileNotFoundError:
                continue
            except OSError as error:
                if task.exists():
                    raise CarryForwardError("kernel_identity_unknown") from error
                continue
            if not re.fullmatch(r"(?:mnt|cgroup):\[[1-9][0-9]*\]", target):
                continue
            if target in old_namespaces or target not in member_namespaces:
                mount_namespace, cgroup_namespace = task_namespaces[(pid, tid)]
                record = {
                    "kind": "fd",
                    "owner_mount_namespace": mount_namespace,
                    "owner_cgroup_namespace": cgroup_namespace,
                    "pinned": target,
                }
                if target in old_namespaces:
                    fd_pins.append(record)
                else:
                    unknown_pins.append(record)

    if unknown_pins:
        raise CarryForwardError("kernel_identity_unknown")

    related = sorted(related, key=lambda item: item["namespace"])
    pins = sorted([*namespace_pins, *fd_pins], key=lambda item: canonical_bytes(item))
    target = {"related": related, "pins": pins}
    return {
        "task_count": len(task_namespaces),
        "namespace_count": len(representatives),
        "mountinfo_bytes": budget["mountinfo_bytes"],
        "fd_entries": budget["fd_entries"],
        "related_count": len(related),
        "pin_count": len(pins),
        "target_digest": digest(target),
        "related": related,
        "pins": pins,
    }


def validate_retired_markers(
    markers: list[dict[str, Any]],
    *,
    boot_id: str,
    parent_device: int,
    parent_inode: int,
    children: dict[str, dict[str, Any]],
) -> list[dict[str, str]]:
    child_tuples = {
        (item["device"], item["inode"])
        for item in children.values()
        if isinstance(item, dict)
        and type(item.get("device")) is int
        and type(item.get("inode")) is int
    }
    result = []
    for marker in markers:
        identity = marker.get("namespace_identity")
        if not isinstance(identity, dict):
            raise CarryForwardError("sandbox_recovery_invalid")
        retired = identity.get("boot_id") != boot_id or (
            (identity.get("parent_device"), identity.get("parent_inode"))
            == (parent_device, parent_inode)
            and (identity.get("root_device"), identity.get("root_inode"))
            not in child_tuples
        )
        if not retired or marker.get("cgroup_name") in children:
            raise CarryForwardError("sandbox_namespace_not_retired")
        result.append(
            {
                "cgroup_name": marker["cgroup_name"],
                "marker_fingerprint": marker["marker_fingerprint"],
            }
        )
    return sorted(result, key=lambda item: item["cgroup_name"])


def capture_kernel(
    unit: str,
    *,
    require_idle: bool,
    expected_description: str | None = None,
    worker_container: str | None = None,
    runtime_volume: str | None = None,
    journal_volume: str | None = None,
    baseline_authority: dict[str, Any] | None = None,
    recovery_markers: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if not re.fullmatch(r"dlr-[A-Za-z0-9][A-Za-z0-9_.-]*\.service", unit):
        raise CarryForwardError("kernel_unit_invalid")
    try:
        values = [
            subprocess.run(
                ["systemctl", "show", unit, f"--property={name}", "--value"],
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            ).stdout.strip()
            for name in (
                "ActiveState",
                "Delegate",
                "ControlGroup",
                "MainPID",
                "Description",
            )
        ]
    except (OSError, subprocess.SubprocessError) as error:
        raise CarryForwardError("kernel_status_unavailable") from error
    active, delegate, control_group, main_pid_text, description = values
    expected = f"/system.slice/{unit}"
    if (
        active != "active"
        or delegate != "yes"
        or control_group != expected
        or (expected_description is not None and description != expected_description)
        or not description.startswith(f"DataLinkRuntime Sandbox {unit} ")
    ):
        raise CarryForwardError("kernel_keeper_invalid")
    try:
        main_pid = int(main_pid_text)
    except ValueError as error:
        raise CarryForwardError("kernel_keeper_invalid") from error
    if main_pid <= 0 or _read_text(Path(f"/proc/{main_pid}/comm")) != "sleep":
        raise CarryForwardError("kernel_keeper_invalid")
    if _read_text(Path(f"/proc/{main_pid}/cgroup")) != f"0::{expected}/agent":
        raise CarryForwardError("kernel_keeper_invalid")
    stat_fields = _read_text(Path(f"/proc/{main_pid}/stat")).split()
    if len(stat_fields) < 22:
        raise CarryForwardError("kernel_keeper_invalid")
    parent = Path("/sys/fs/cgroup") / control_group.removeprefix("/")
    try:
        parent_info = parent.lstat()
    except OSError as error:
        raise CarryForwardError("kernel_read_failed") from error
    agent_pids = _read_text(parent / "agent/cgroup.procs").splitlines()
    if agent_pids != [str(main_pid)] or _read_text(parent / "cgroup.procs"):
        raise CarryForwardError("kernel_keeper_invalid")
    tree: dict[str, dict[str, Any]] = {}
    try:
        pending = sorted(
            (path for path in parent.iterdir() if path.is_dir()), reverse=True
        )
    except OSError as error:
        raise CarryForwardError("kernel_read_failed") from error
    while pending:
        child = pending.pop()
        try:
            child_info = child.lstat()
            descendants = sorted(
                (path for path in child.iterdir() if path.is_dir()), reverse=True
            )
        except OSError as error:
            raise CarryForwardError("kernel_read_failed") from error
        pending.extend(descendants)
        events = dict(
            line.split(None, 1)
            for line in _read_text(child / "cgroup.events").splitlines()
            if line.strip()
        )
        try:
            populated = int(events["populated"])
            pids = [
                int(value) for value in _read_text(child / "cgroup.procs").splitlines()
            ]
        except (KeyError, ValueError) as error:
            raise CarryForwardError("kernel_events_invalid") from error
        if populated not in {0, 1} or any(pid <= 0 for pid in pids):
            raise CarryForwardError("kernel_events_invalid")
        relative = child.relative_to(parent).as_posix()
        tree[relative] = {
            "populated": populated,
            "process_count": len(pids),
            "process_digest": digest(sorted(pids)),
            "device": child_info.st_dev,
            "inode": child_info.st_ino,
        }
    keeper = tree.get("agent")
    if (
        keeper is None
        or keeper["populated"] != 1
        or keeper["process_count"] != 1
        or keeper["process_digest"] != digest([main_pid])
    ):
        raise CarryForwardError("kernel_keeper_invalid")
    if require_idle and set(tree) != {"agent"}:
        raise CarryForwardError("kernel_not_idle")
    try:
        repeated = [
            subprocess.run(
                ["systemctl", "show", unit, f"--property={name}", "--value"],
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            ).stdout.strip()
            for name in (
                "ActiveState",
                "Delegate",
                "ControlGroup",
                "MainPID",
                "Description",
            )
        ]
    except (OSError, subprocess.SubprocessError) as error:
        raise CarryForwardError("kernel_status_unavailable") from error
    if (
        repeated != values
        or _read_text(Path(f"/proc/{main_pid}/stat")).split()[21] != stat_fields[21]
    ):
        raise CarryForwardError("kernel_keeper_changed")
    supplied = (worker_container, runtime_volume, journal_volume)
    if any(supplied) and not all(supplied):
        raise CarryForwardError("kernel_identity_unknown")
    authority = baseline_authority
    if all(supplied):
        authority = _worker_authority(
            worker_container or "",
            runtime_volume or "",
            journal_volume or "",
            control_group,
        )
    namespace_evidence = None
    retired_markers: list[dict[str, Any]] = []
    if require_idle:
        if not isinstance(authority, dict):
            raise CarryForwardError("kernel_identity_unknown")
        first = _related_mount_namespaces(authority)
        second = _related_mount_namespaces(authority)
        stable_keys = ("related_count", "pin_count", "target_digest")
        if any(first.get(key) != second.get(key) for key in stable_keys):
            raise CarryForwardError("kernel_identity_unknown")
        if first["related_count"]:
            raise CarryForwardError("kernel_namespace_active")
        if first["pin_count"]:
            raise CarryForwardError("kernel_namespace_active")
        namespace_evidence = second
        boot_id = _read_text(Path("/proc/sys/kernel/random/boot_id"))
        retired_markers = validate_retired_markers(
            recovery_markers or [],
            boot_id=boot_id,
            parent_device=parent_info.st_dev,
            parent_inode=parent_info.st_ino,
            children=tree,
        )
    return {
        "boot_id": _read_text(Path("/proc/sys/kernel/random/boot_id")),
        "unit": unit,
        "control_group": control_group,
        "keeper_pid": main_pid,
        "keeper_starttime": stat_fields[21],
        "description": description,
        "parent_device": parent_info.st_dev,
        "parent_inode": parent_info.st_ino,
        "children": tree,
        "old_worker_authority": authority,
        "namespace_evidence": namespace_evidence,
        "retired_markers": sorted(
            retired_markers, key=lambda item: item["cgroup_name"]
        ),
    }


def compare_kernel(before: dict[str, Any], after: dict[str, Any]) -> None:
    for key in (
        "boot_id",
        "unit",
        "control_group",
        "keeper_pid",
        "keeper_starttime",
        "description",
        "parent_device",
        "parent_inode",
    ):
        if before.get(key) != after.get(key):
            raise CarryForwardError("kernel_keeper_changed")
    children = after.get("children", {})
    agent = children.get("agent") if isinstance(children, dict) else None
    if (
        set(children) != {"agent"}
        or not isinstance(agent, dict)
        or agent.get("populated") != 1
        or agent.get("process_count") != 1
        or agent.get("process_digest") != digest([after["keeper_pid"]])
    ):
        raise CarryForwardError("kernel_not_idle")
    if before.get("old_worker_authority") != after.get("old_worker_authority"):
        raise CarryForwardError("kernel_identity_unknown")
    namespace = after.get("namespace_evidence")
    if (
        not isinstance(namespace, dict)
        or namespace.get("related_count") != 0
        or namespace.get("pin_count") != 0
        or not isinstance(namespace.get("target_digest"), str)
    ):
        raise CarryForwardError("kernel_namespace_active")


def validate_candidate_tables(
    revision: str, existing: set[str], counts: dict[str, int]
) -> None:
    for name in EMPTY_TABLES_BY_REVISION.get(revision, ()):
        if name not in existing:
            raise CarryForwardError("candidate_table_missing")
        if counts.get(name) != 0:
            raise CarryForwardError("candidate_table_not_empty")


def validate_schema_transition(from_revision: str, to_revision: str) -> set[str]:
    allowed = SCHEMA_ADDITIONS.get((from_revision, to_revision))
    if allowed is None:
        raise CarryForwardError("candidate_schema_path_unknown")
    return allowed


def validate_schema_inventory(
    from_revision: str,
    to_revision: str,
    baseline_tables: set[str],
    existing: set[str],
    counts: dict[str, int],
    cursor_rows: list[tuple[Any, ...]],
) -> None:
    allowed = validate_schema_transition(from_revision, to_revision)
    if existing != baseline_tables | allowed:
        raise CarryForwardError("candidate_schema_inventory_changed")
    if (
        "execution_incident_dispositions" in allowed
        and counts.get("execution_incident_dispositions") != 0
    ):
        raise CarryForwardError("candidate_table_not_empty")
    if "runtime_reconciliation_cursors" in allowed and cursor_rows != [
        ("expired_attempts", 0, 0)
    ]:
        raise CarryForwardError("candidate_seed_invalid")


def projection_columns(
    name: str,
    actual_columns: list[str],
    baseline_projection: dict[str, Any] | None,
    *,
    mode: str | None,
) -> list[str]:
    if baseline_projection is None:
        return actual_columns
    baseline_columns = list(baseline_projection[name]["columns"])
    if is_audited_mode(mode):
        if actual_columns != baseline_columns:
            raise CarryForwardError("projection_columns_changed")
        return actual_columns
    if not set(baseline_columns).issubset(actual_columns):
        raise CarryForwardError("projection_columns_missing")
    return baseline_columns


def validate_storage_identity(storage: Any) -> list[dict[str, Any]]:
    if not isinstance(storage, list):
        raise CarryForwardError("storage_identity_invalid")
    identities: list[dict[str, Any]] = []
    for item in storage:
        if (
            not isinstance(item, dict)
            or set(item) != {"service", "type", "source", "destination", "read_only"}
            or item["type"] not in {"volume", "bind", "tmpfs"}
            or not isinstance(item["service"], str)
            or not isinstance(item["source"], str)
            or not isinstance(item["destination"], str)
            or not item["destination"].startswith("/")
            or type(item["read_only"]) is not bool
            or (item["type"] != "tmpfs" and not item["source"])
        ):
            raise CarryForwardError("storage_identity_invalid")
        identities.append(item)
    keys = [(item["service"], item["destination"]) for item in identities]
    if len(keys) != len(set(keys)):
        raise CarryForwardError("storage_identity_invalid")
    for protected in (item for item in identities if item["type"] == "volume"):
        root = Path(protected["destination"])
        for item in identities:
            if item["service"] != protected["service"] or item is protected:
                continue
            candidate = Path(item["destination"])
            try:
                candidate.relative_to(root)
            except ValueError:
                try:
                    root.relative_to(candidate)
                except ValueError:
                    continue
            raise CarryForwardError("storage_identity_shadowed")
    return sorted(identities, key=lambda item: (item["service"], item["destination"]))


def inspect_database(
    selection: dict[str, Any],
    baseline_projection: dict[str, Any] | None = None,
    *,
    baseline_schema_inventory: dict[str, Any] | None = None,
    from_revision: str | None = None,
    to_revision: str | None = None,
    schema_phase: str | None = None,
    mode: str | None = None,
    group2_reboot_admission: bool = False,
) -> dict[str, Any]:
    if mode not in {None, AUDITED_MODE, GROUP2_MODE}:
        raise CarryForwardError("manifest_mode_invalid")
    selection = normalize_selection(selection, mode=mode)
    required_tables = AUDITED_TABLES if is_audited_mode(mode) else RESPONSIBILITY_TABLES
    try:
        from sqlalchemy import create_engine, inspect, text
    except ImportError as error:
        raise CarryForwardError("sqlalchemy_unavailable") from error
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise CarryForwardError("database_url_missing")
    engine = create_engine(url)
    tables: dict[str, dict[str, Any]] = {}
    reboot_admission_raw: dict[str, list[dict[str, Any]]] | None = None
    reboot_database_rows: dict[str, dict[str, Any]] | None = None
    reboot_mutation_guard_rows: dict[str, dict[str, Any]] | None = None
    reboot_db_clock: Any = None
    with engine.connect() as connection:
        connection.execute(
            text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        )
        inspector = inspect(connection)
        existing = set(inspector.get_table_names())
        revision = connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one()
        candidate_counts = {
            name: connection.execute(
                text(f'SELECT count(*) FROM "{name}"')
            ).scalar_one()
            for name in EMPTY_TABLES_BY_REVISION.get(revision, ())
            if name in existing
        }
        if mode is None:
            validate_candidate_tables(revision, existing, candidate_counts)
        elif revision != "0040_issue152_dispositions":
            raise CarryForwardError("candidate_schema_path_unknown")
        if baseline_schema_inventory is not None:
            if not isinstance(baseline_schema_inventory, dict) or set(
                baseline_schema_inventory
            ) != {"tables"}:
                raise CarryForwardError("schema_inventory_invalid")
            baseline_tables = baseline_schema_inventory["tables"]
            if (
                not isinstance(baseline_tables, list)
                or not all(isinstance(name, str) for name in baseline_tables)
                or baseline_tables != sorted(set(baseline_tables))
                or from_revision is None
                or to_revision is None
                or schema_phase not in {"before", "after"}
            ):
                raise CarryForwardError("schema_inventory_invalid")
            if schema_phase == "before":
                if revision != from_revision or existing != set(baseline_tables):
                    raise CarryForwardError("schema_inventory_invalid")
            else:
                if revision != to_revision:
                    raise CarryForwardError("schema_inventory_invalid")
                cursor_rows = (
                    [
                        tuple(row)
                        for row in connection.execute(
                            text(
                                "SELECT name, after_id, upper_id "
                                "FROM runtime_reconciliation_cursors ORDER BY name"
                            )
                        )
                    ]
                    if "runtime_reconciliation_cursors" in existing
                    and "runtime_reconciliation_cursors" not in set(baseline_tables)
                    else []
                )
                validate_schema_inventory(
                    from_revision,
                    to_revision,
                    set(baseline_tables),
                    existing,
                    candidate_counts,
                    cursor_rows,
                )
        capture_tables = (
            tuple(dict.fromkeys((*required_tables, *ASSET_TABLES)))
            if mode == GROUP2_MODE
            else required_tables
        )
        schema_shape: dict[str, Any] = {}
        if mode == GROUP2_MODE:
            for name in sorted(existing):
                columns = inspector.get_columns(name)
                primary_key = list(
                    (inspector.get_pk_constraint(name) or {}).get("constrained_columns")
                    or []
                )
                if not primary_key:
                    raise CarryForwardError("schema_primary_key_missing")
                schema_shape[name] = {
                    "columns": [
                        {
                            "name": column["name"],
                            "type": str(column["type"]),
                            "nullable": bool(column["nullable"]),
                            "default": None
                            if column.get("default") is None
                            else str(column["default"]),
                        }
                        for column in columns
                    ],
                    "primary_key": primary_key,
                }
        for name in capture_tables:
            if name not in existing:
                raise CarryForwardError("schema_table_missing")
            actual_columns = [column["name"] for column in inspector.get_columns(name)]
            actual_primary_key = list(
                (inspector.get_pk_constraint(name) or {}).get("constrained_columns")
                or []
            )
            if not actual_primary_key:
                raise CarryForwardError("schema_primary_key_missing")
            if name == AUDIT_TABLE and (
                actual_columns != list(AUDIT_COLUMNS) or actual_primary_key != ["id"]
            ):
                raise CarryForwardError("audit_schema_invalid")
            if (
                baseline_projection is not None
                and name in baseline_projection
                and actual_primary_key != list(baseline_projection[name]["primary_key"])
            ):
                raise CarryForwardError("projection_primary_key_changed")
            columns = projection_columns(
                name,
                actual_columns,
                baseline_projection if name in required_tables else None,
                mode=mode,
            )
            primary_key = actual_primary_key
            quoted_columns = ",".join(f'"{column}"' for column in columns)
            order = ",".join(f'"{column}"' for column in primary_key)
            values = connection.execute(
                text(f'SELECT {quoted_columns} FROM "{name}" ORDER BY {order}')
            ).mappings()
            tables[name] = {
                "columns": columns,
                "primary_key": primary_key,
                "rows": [dict(row) for row in values],
            }
        if group2_reboot_admission:
            if not GROUP2_REBOOT_ADMISSION_TABLES.issubset(existing):
                raise CarryForwardError("group2_reboot_admission_schema_invalid")
            reboot_admission_raw = {}
            for name in sorted(GROUP2_REBOOT_ADMISSION_TABLES):
                actual_columns = [column["name"] for column in inspector.get_columns(name)]
                selected = (
                    actual_columns if name == "managed_input_settings"
                    else sorted(GROUP2_REBOOT_ADMISSION_COLUMNS[name])
                )
                if not set(selected).issubset(actual_columns):
                    raise CarryForwardError("group2_reboot_admission_schema_invalid")
                primary_key = GROUP2_REBOOT_ADMISSION_PRIMARY_KEYS[name]
                quoted = ",".join(f'"{column}"' for column in selected)
                order = ",".join(f'"{column}"' for column in primary_key)
                rows = connection.execute(
                    text(f'SELECT {quoted} FROM "{name}" ORDER BY {order}')
                ).mappings()
                reboot_admission_raw[name] = [dict(row) for row in rows]
            reboot_database_rows = {
                name: tables[name] for name in tables
            }
            reboot_mutation_guard_rows = {}
            for name in GROUP2_REBOOT_MUTATION_GUARD_TABLES:
                actual_columns = [column["name"] for column in inspector.get_columns(name)]
                primary_key = list(
                    (inspector.get_pk_constraint(name) or {}).get("constrained_columns") or []
                )
                if not primary_key:
                    raise CarryForwardError("group2_reboot_admission_schema_invalid")
                quoted = ",".join(f'"{column}"' for column in actual_columns)
                order = ",".join(f'"{column}"' for column in primary_key)
                rows = connection.execute(
                    text(f'SELECT {quoted} FROM "{name}" ORDER BY {order}')
                ).mappings()
                reboot_mutation_guard_rows[name] = {
                    "columns": actual_columns, "primary_key": primary_key,
                    "rows": [dict(row) for row in rows],
                }
            reboot_db_clock = connection.execute(
                text("SELECT clock_timestamp()")
            ).scalar_one()
    projection = project_rows(tables, required=required_tables)
    responsibilities = derive_responsibilities(tables, selection, mode=mode)
    terminal_evidence = (
        derive_terminal_evidence(tables, selection) if is_audited_mode(mode) else None
    )
    if terminal_evidence is not None:
        responsibilities = {
            **responsibilities,
            "terminal_executions": terminal_evidence["executions"],
        }
    credential_hashes = {
        row["id"]: {
            "claim_token_hash": row.get("claim_token_hash"),
            "cleanup_token_hash": row.get("cleanup_token_hash"),
        }
        for row in tables["execution_attempts"]["rows"]
    }
    attempt_statuses = {
        row["id"]: row["status"] for row in tables["execution_attempts"]["rows"]
    }
    result = {
        "projection": projection,
        "responsibilities": responsibilities,
        "schema_inventory": {"tables": sorted(existing)},
        "_credential_hashes": credential_hashes,
        "_attempt_statuses": attempt_statuses,
    }
    if mode == GROUP2_MODE:
        protected_tables: dict[str, Any] = {}
        for name in AUDITED_TABLES:
            source = tables[name]
            protected_rows = []
            for row in source["rows"]:
                protected = {
                    "pk": {key: canonical(row[key]) for key in source["primary_key"]},
                    "hash": row_digest(row),
                }
                if name == "global_execution_admission":
                    stable = {
                        key: item for key, item in row.items() if key != "updated_at"
                    }
                    protected.update(
                        stable_hash=row_digest(stable),
                        updated_at=canonical(row.get("updated_at")),
                    )
                if name == "worker_cleanup_requests":
                    protected.update(
                        {
                            key: canonical(row.get(key))
                            for key in (
                                "id",
                                "adapter_id",
                                "worker_id",
                                "status",
                                "attempts",
                                "error_code",
                                "created_at",
                                "updated_at",
                                "completed_at",
                            )
                        }
                    )
                protected_rows.append(protected)
            protected_tables[name] = {
                "columns": source["columns"],
                "primary_key": source["primary_key"],
                "rows": protected_rows,
            }

        def ids(name: str) -> list[int]:
            return sorted(
                _positive(row["id"], "protected_rows_invalid")
                for row in tables[name]["rows"]
            )

        result.update(
            protected_rows={
                "tables": protected_tables,
                "adapter_ids": ids("adapters"),
                "version_ids": ids("adapter_versions"),
                "execution_ids": ids("executions"),
                "attempt_ids": ids("execution_attempts"),
            },
            asset_projection=project_rows(tables, required=ASSET_TABLES),
            schema_shape=schema_shape,
        )
    if group2_reboot_admission:
        result["_reboot_admission_raw"] = reboot_admission_raw
        result["_reboot_database_rows"] = reboot_database_rows
        result["_reboot_mutation_guard_rows"] = reboot_mutation_guard_rows
        result["_reboot_db_clock"] = reboot_db_clock
    return result


def manifest_payload(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if key != "manifest_digest"}


def seal_manifest(value: dict[str, Any]) -> dict[str, Any]:
    sealed = canonical(dict(value))
    sealed["manifest_digest"] = digest(manifest_payload(sealed))
    validate_manifest(sealed)
    return sealed


def validate_source_diff(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"tree_digest", "entries"}:
        raise CarryForwardError("source_diff_invalid")
    if not isinstance(value["tree_digest"], str) or not DIGEST.fullmatch(
        value["tree_digest"]
    ):
        raise CarryForwardError("source_diff_invalid")
    entries = value["entries"]
    keys = {"status", "old_mode", "new_mode", "old_oid", "new_oid", "path"}
    if (
        not isinstance(entries, list)
        or not entries
        or any(not isinstance(item, dict) or set(item) != keys for item in entries)
        or entries != sorted(entries, key=lambda item: item["path"])
        or len({item["path"] for item in entries}) != len(entries)
    ):
        raise CarryForwardError("source_diff_invalid")
    for item in entries:
        expected_mode = AUDITED_SOURCE_MODES.get(item.get("path"))
        if (
            item["status"] != "M"
            or expected_mode is None
            or item["old_mode"] != expected_mode
            or item["new_mode"] != expected_mode
            or not isinstance(item["old_oid"], str)
            or not SHA.fullmatch(item["old_oid"])
            or not isinstance(item["new_oid"], str)
            or not SHA.fullmatch(item["new_oid"])
            or item["old_oid"] == item["new_oid"]
            or not isinstance(item["path"], str)
            or not item["path"]
        ):
            raise CarryForwardError("source_diff_invalid")
    return value


def _digest_text(value: Any, code: str) -> str:
    if not isinstance(value, str) or DIGEST.fullmatch(value) is None:
        raise CarryForwardError(code)
    return value


def _sha_text(value: Any, code: str) -> str:
    if not isinstance(value, str) or SHA.fullmatch(value) is None:
        raise CarryForwardError(code)
    return value


def _group2_expected_rules() -> dict[str, tuple[str, str, str, str | None, str | None]]:
    rules = dict(GROUP2_PRODUCT_RULES)
    for path, mode in GROUP2_CONTROLLER_PATHS.items():
        if path in rules:
            status, old_mode, new_mode, _, _ = rules[path]
            rules[path] = (status, old_mode, new_mode, None, None)
        else:
            rules[path] = (
                "A" if path.endswith("incident-preserving-upgrade/spec.md") else "M",
                "000000"
                if path.endswith("incident-preserving-upgrade/spec.md")
                else mode,
                mode,
                None,
                None,
            )
    if len(rules) != 64:
        raise CarryForwardError("group2_source_rules_invalid")
    return rules


def validate_group2_source_diff(value: Any, scope: Any) -> dict[str, Any]:
    keys = {
        "from_sha",
        "to_sha",
        "from_tree",
        "to_tree",
        "raw_diff_sha256",
        "tree_digest",
        "entries",
    }
    if not isinstance(value, dict) or set(value) != keys:
        raise CarryForwardError("group2_source_diff_invalid")
    _sha_text(value["from_tree"], "group2_source_diff_invalid")
    _sha_text(value["to_tree"], "group2_source_diff_invalid")
    _sha_text(value["from_sha"], "group2_source_diff_invalid")
    _sha_text(value["to_sha"], "group2_source_diff_invalid")
    for key in ("raw_diff_sha256", "tree_digest"):
        _digest_text(value[key], "group2_source_diff_invalid")
    entries = value["entries"]
    entry_keys = {"status", "old_mode", "new_mode", "old_oid", "new_oid", "path"}
    rules = _group2_expected_rules()
    if (
        not isinstance(entries, list)
        or len(entries) != 64
        or any(
            not isinstance(item, dict) or set(item) != entry_keys for item in entries
        )
        or entries != sorted(entries, key=lambda item: item["path"])
        or {item["path"] for item in entries} != set(rules)
        or digest(
            {
                "from_tree": value["from_tree"],
                "to_tree": value["to_tree"],
                "entries": entries,
            }
        )
        != value["tree_digest"]
    ):
        raise CarryForwardError("group2_source_diff_invalid")
    for item in entries:
        status, old_mode, new_mode, old_oid, new_oid = rules[item["path"]]
        if (
            item["status"] != status
            or item["old_mode"] != old_mode
            or item["new_mode"] != new_mode
            or not isinstance(item["old_oid"], str)
            or not isinstance(item["new_oid"], str)
            or SHA.fullmatch(item["old_oid"]) is None
            or SHA.fullmatch(item["new_oid"]) is None
            or (old_oid is not None and item["old_oid"] != old_oid)
            or (new_oid is not None and item["new_oid"] != new_oid)
        ):
            raise CarryForwardError("group2_source_diff_invalid")
        if status == "M" and (
            item["old_oid"] == "0" * 40 or item["old_oid"] == item["new_oid"]
        ):
            raise CarryForwardError("group2_source_diff_invalid")
        if status == "A" and item["old_oid"] != "0" * 40:
            raise CarryForwardError("group2_source_diff_invalid")
    if (
        value["from_sha"] != GROUP2_FROM_SHA
        or SHA.fullmatch(value["to_sha"]) is None
        or not isinstance(scope, dict)
        or value != scope.get("final_source")
    ):
        raise CarryForwardError("group2_source_scope_mismatch")
    return value


def validate_group2_review_scope(value: Any) -> dict[str, Any]:
    required = {
        "schema",
        "mode",
        "repo",
        "pr",
        "approval",
        "product_anchor",
        "final_source",
        "reviews",
        "controller_files",
        "image_binding",
        "migration_graph",
        "ci",
        "preservation_reference",
        "scope_digest",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise CarryForwardError("group2_review_scope_invalid")
    if value["schema"] != GROUP2_REVIEW_SCHEMA or value["mode"] != GROUP2_MODE:
        raise CarryForwardError("group2_review_scope_invalid")
    if (
        not isinstance(value["repo"], str)
        or re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value["repo"]) is None
    ):
        raise CarryForwardError("group2_review_scope_invalid")
    _positive(value["pr"], "group2_review_scope_invalid")
    approval = value["approval"]
    approval_keys = {
        "request_sha256",
        "user_approval_sha256",
        "product_scope_sha256",
        "review_bindings_sha256",
    }
    if not isinstance(approval, dict) or set(approval) != approval_keys:
        raise CarryForwardError("group2_approval_invalid")
    for item in approval.values():
        _digest_text(item, "group2_approval_invalid")
    if approval["request_sha256"] != GROUP2_REQUEST_DIGEST:
        raise CarryForwardError("group2_approval_invalid")
    anchor = value["product_anchor"]
    anchor_keys = {
        "base_sha",
        "head_sha",
        "base_tree",
        "head_tree",
        "raw_diff_sha256",
        "files",
    }
    if not isinstance(anchor, dict) or set(anchor) != anchor_keys:
        raise CarryForwardError("group2_product_anchor_invalid")
    expected_anchor = (
        GROUP2_FROM_SHA,
        GROUP2_PRODUCT_SHA,
        GROUP2_FROM_TREE,
        GROUP2_PRODUCT_TREE,
        GROUP2_PRODUCT_RAW_DIGEST,
    )
    if (
        tuple(
            anchor[key]
            for key in (
                "base_sha",
                "head_sha",
                "base_tree",
                "head_tree",
                "raw_diff_sha256",
            )
        )
        != expected_anchor
    ):
        raise CarryForwardError("group2_product_anchor_invalid")
    anchor_entries = anchor["files"]
    if (
        not isinstance(anchor_entries, list)
        or len(anchor_entries) != 57
        or any(not isinstance(item, dict) for item in anchor_entries)
        or anchor_entries
        != sorted(anchor_entries, key=lambda item: item.get("path", ""))
        or {item.get("path") for item in anchor_entries if isinstance(item, dict)}
        != set(GROUP2_PRODUCT_RULES)
    ):
        raise CarryForwardError("group2_product_anchor_invalid")
    for item in anchor_entries:
        rule = (
            GROUP2_PRODUCT_RULES.get(item.get("path"))
            if isinstance(item, dict)
            else None
        )
        if (
            rule is None
            or set(item)
            != {"status", "old_mode", "new_mode", "old_oid", "new_oid", "path"}
            or tuple(item[key] for key in ("status", "old_mode", "new_mode"))
            != rule[:3]
            or (rule[3] is not None and item["old_oid"] != rule[3])
            or (rule[4] is not None and item["new_oid"] != rule[4])
        ):
            raise CarryForwardError("group2_product_anchor_invalid")
    final_source = value["final_source"]
    validate_group2_source_diff(final_source, value)
    reviews = value["reviews"]
    review_keys = {"name", "report_sha256", "reviewed_commit", "coverage", "status"}
    final_blobs = {item["path"]: item["new_oid"] for item in final_source["entries"]}
    if (
        not isinstance(reviews, list)
        or len(reviews) < 4
        or any(
            not isinstance(item, dict) or set(item) != review_keys for item in reviews
        )
        or any(item["status"] != "APPROVED" for item in reviews)
        or any(
            _digest_text(item["report_sha256"], "group2_reviews_invalid")
            != item["report_sha256"]
            for item in reviews
        )
        or any(
            _sha_text(item["reviewed_commit"], "group2_reviews_invalid")
            != item["reviewed_commit"]
            for item in reviews
        )
        or not all(
            isinstance(item["coverage"], list)
            and all(
                isinstance(entry, dict)
                and set(entry) == {"path", "blob_oid"}
                and isinstance(entry["path"], str)
                and SHA.fullmatch(entry["blob_oid"]) is not None
                and final_blobs.get(entry["path"]) == entry["blob_oid"]
                for entry in item["coverage"]
            )
            for item in reviews
        )
        or {entry["path"] for item in reviews for entry in item["coverage"]}
        != set(_group2_expected_rules())
    ):
        raise CarryForwardError("group2_reviews_invalid")
    controller = value["controller_files"]
    if not isinstance(controller, dict) or set(controller) != {"files", "digest"}:
        raise CarryForwardError("group2_controller_files_invalid")
    files = controller["files"]
    if not isinstance(files, dict) or set(files) != GROUP2_CONTROLLER_FILES:
        raise CarryForwardError("group2_controller_files_invalid")
    for item in files.values():
        _digest_text(item, "group2_controller_files_invalid")
    _digest_text(controller["digest"], "group2_controller_files_invalid")
    if controller["digest"] != digest(files):
        raise CarryForwardError("group2_controller_files_invalid")
    image_binding = value["image_binding"]
    if not isinstance(image_binding, dict) or set(image_binding) != {
        "old_image_ids",
        "candidate_image_ids",
    }:
        raise CarryForwardError("group2_image_binding_invalid")
    for key in ("old_image_ids", "candidate_image_ids"):
        if not isinstance(image_binding[key], dict) or not image_binding[key]:
            raise CarryForwardError("group2_image_binding_invalid")
    migration = value["migration_graph"]
    if not isinstance(migration, dict) or set(migration) != {
        "from_files",
        "to_files",
        "graph_digest",
        "head",
    }:
        raise CarryForwardError("group2_migration_graph_invalid")
    if (
        migration["head"] != "0040_issue152_dispositions"
        or migration["from_files"] != migration["to_files"]
    ):
        raise CarryForwardError("group2_migration_graph_invalid")
    _digest_text(migration["graph_digest"], "group2_migration_graph_invalid")
    migration_files = migration["from_files"]
    if (
        not isinstance(migration_files, list)
        or any(
            not isinstance(item, dict) or set(item) != {"path", "mode", "oid"}
            for item in migration_files
        )
        or migration_files != sorted(migration_files, key=lambda item: item["path"])
        or any(
            item["mode"] not in {"100644", "100755"}
            or not isinstance(item["oid"], str)
            or SHA.fullmatch(item["oid"]) is None
            for item in migration_files
        )
    ):
        raise CarryForwardError("group2_migration_graph_invalid")
    ci = value["ci"]
    if not isinstance(ci, dict) or set(ci) != {
        "head_sha",
        "run_id",
        "run_attempt",
        "workflow_path",
        "event",
        "jobs",
        "evidence_sha256",
    }:
        raise CarryForwardError("group2_ci_invalid")
    _positive(ci["run_id"], "group2_ci_invalid")
    _positive(ci["run_attempt"], "group2_ci_invalid")
    if (
        ci["head_sha"] != final_source["to_sha"]
        or ci["workflow_path"] != ".github/workflows/ci.yml"
        or ci["event"] != "pull_request"
    ):
        raise CarryForwardError("group2_ci_invalid")
    jobs = ci["jobs"]
    if (
        not isinstance(jobs, list)
        or any(
            not isinstance(item, dict) or set(item) != {"id", "name", "conclusion"}
            for item in jobs
        )
        or jobs != sorted(jobs, key=lambda item: (item["name"], item["id"]))
        or any(item["conclusion"].lower() != "success" for item in jobs)
        or any(
            sum(item["name"] == name for item in jobs) != 1
            for name in ("backend", "web", "local-preview", "compose-smoke")
        )
    ):
        raise CarryForwardError("group2_ci_invalid")
    _digest_text(ci["evidence_sha256"], "group2_ci_invalid")
    reference = value["preservation_reference"]
    reference_keys = {"snapshot", "snapshot_digest", "review_report_sha256"}
    if not isinstance(reference, dict) or set(reference) != reference_keys:
        raise CarryForwardError("group2_preservation_reference_invalid")
    _digest_text(reference["snapshot_digest"], "group2_preservation_reference_invalid")
    _digest_text(
        reference["review_report_sha256"], "group2_preservation_reference_invalid"
    )
    snapshot = reference["snapshot"]
    if (
        not isinstance(snapshot, dict)
        or set(snapshot) != {"selection", "db", "files", "lineage"}
        or reference["snapshot_digest"] != digest(snapshot)
        or not isinstance(snapshot["lineage"], list)
        or any(
            not isinstance(item, dict)
            or set(item) != {"name", "sha256"}
            or DIGEST.fullmatch(item["sha256"]) is None
            for item in snapshot["lineage"]
        )
    ):
        raise CarryForwardError("group2_preservation_reference_invalid")
    _digest_text(value["scope_digest"], "group2_review_scope_invalid")
    if (
        digest({key: item for key, item in value.items() if key != "scope_digest"})
        != value["scope_digest"]
    ):
        raise CarryForwardError("group2_review_scope_digest_mismatch")
    return value


def validate_group2_protected_rows(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "tables",
        "adapter_ids",
        "version_ids",
        "execution_ids",
        "attempt_ids",
    }:
        raise CarryForwardError("protected_rows_invalid")
    if set(value["tables"]) != set(AUDITED_TABLES):
        raise CarryForwardError("protected_rows_invalid")
    for key in ("adapter_ids", "version_ids", "execution_ids", "attempt_ids"):
        ids = value[key]
        if (
            not isinstance(ids, list)
            or ids != sorted(set(ids))
            or any(
                not isinstance(item, int) or isinstance(item, bool) or item <= 0
                for item in ids
            )
        ):
            raise CarryForwardError("protected_rows_invalid")
    for name, table in value["tables"].items():
        if not isinstance(table, dict) or set(table) != {
            "columns",
            "primary_key",
            "rows",
        }:
            raise CarryForwardError("protected_rows_invalid")
        columns, primary_key, rows = (
            table["columns"],
            table["primary_key"],
            table["rows"],
        )
        if (
            not isinstance(columns, list)
            or not columns
            or not isinstance(primary_key, list)
            or not primary_key
            or not set(primary_key).issubset(columns)
            or not isinstance(rows, list)
        ):
            raise CarryForwardError("protected_rows_invalid")
        expected_keys = {"pk", "hash"}
        if name == "global_execution_admission":
            expected_keys |= {"stable_hash", "updated_at"}
        if name == "worker_cleanup_requests":
            expected_keys |= {
                "id",
                "adapter_id",
                "worker_id",
                "status",
                "attempts",
                "error_code",
                "created_at",
                "updated_at",
                "completed_at",
            }
        row_keys = []
        for row in rows:
            if not isinstance(row, dict) or set(row) != expected_keys:
                raise CarryForwardError("protected_rows_invalid")
            if not isinstance(row["pk"], dict) or set(row["pk"]) != set(primary_key):
                raise CarryForwardError("protected_rows_invalid")
            row_keys.append(
                tuple(_protected_pk_order(row["pk"][key]) for key in primary_key)
            )
            _digest_text(row["hash"], "protected_rows_invalid")
            if name == "global_execution_admission":
                _digest_text(row["stable_hash"], "protected_rows_invalid")
        if row_keys != sorted(set(row_keys)):
            raise CarryForwardError("protected_rows_invalid")
    return value


def _protected_pk_order(value: Any) -> tuple[int, Any]:
    if value is None:
        return (0, "")
    if isinstance(value, bool):
        return (1, int(value))
    if isinstance(value, int):
        return (2, value)
    if isinstance(value, float):
        return (3, value)
    if isinstance(value, str):
        return (4, value)
    if isinstance(value, dict) and len(value) == 1:
        marker, item = next(iter(value.items()))
        if marker == "$decimal":
            return (2, decimal.Decimal(item))
        if marker in {"$datetime", "$uuid", "$bytes"}:
            return (4, item)
    return (9, canonical_bytes(value))


def validate_group2_schema_shape(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or not value:
        raise CarryForwardError("schema_shape_invalid")
    for name, table in value.items():
        if (
            not isinstance(name, str)
            or re.fullmatch(r"[a-z][a-z0-9_]*", name) is None
            or not isinstance(table, dict)
            or set(table) != {"columns", "primary_key"}
        ):
            raise CarryForwardError("schema_shape_invalid")
        columns = table["columns"]
        primary_key = table["primary_key"]
        if (
            not isinstance(columns, list)
            or not columns
            or any(
                not isinstance(item, dict)
                or set(item) != {"name", "type", "nullable", "default"}
                or not isinstance(item["name"], str)
                or not isinstance(item["type"], str)
                or type(item["nullable"]) is not bool
                or (
                    item["default"] is not None and not isinstance(item["default"], str)
                )
                for item in columns
            )
            or not isinstance(primary_key, list)
            or not primary_key
            or not set(primary_key).issubset({item["name"] for item in columns})
        ):
            raise CarryForwardError("schema_shape_invalid")
    return value


def validate_group2_manifest_extensions(manifest: Any) -> None:
    scope = validate_group2_review_scope(manifest.get("review_scope"))
    if (
        manifest.get("review_scope_digest") != scope["scope_digest"]
        or manifest.get("ci_binding") != scope["ci"]
        or manifest.get("preservation_reference_digest")
        != scope["preservation_reference"]["snapshot_digest"]
        or manifest.get("source_diff") != scope["final_source"]
        or manifest.get("from_sha") != scope["final_source"]["from_sha"]
        or manifest.get("to_sha") != scope["final_source"]["to_sha"]
        or manifest.get("repo") != scope["repo"]
        or manifest.get("pr") != scope["pr"]
        or manifest.get("controller_files_digest")
        != scope["controller_files"]["digest"]
        or manifest.get("migration_graph_digest")
        != scope["migration_graph"]["graph_digest"]
        or manifest.get("old_image_ids") != scope["image_binding"]["old_image_ids"]
        or manifest.get("candidate_image_ids")
        != scope["image_binding"]["candidate_image_ids"]
    ):
        raise CarryForwardError("group2_manifest_binding_invalid")
    validate_group2_source_diff(manifest["source_diff"], scope)
    validate_group2_protected_rows(manifest.get("protected_rows"))
    validate_projection_evidence(
        manifest.get("asset_projection"), required=ASSET_TABLES
    )
    shape = validate_group2_schema_shape(manifest.get("schema_shape"))
    if set(shape) != set(manifest["schema_inventory"]["tables"]):
        raise CarryForwardError("schema_shape_invalid")
    account_entry = validate_group2_account_entry(manifest.get("account_entry"))
    log_evidence = manifest.get("log_evidence")
    if (
        not isinstance(log_evidence, dict)
        or set(log_evidence)
        != {
            "profile_digest",
            "files",
            "roots",
            "clock",
            "observed_at_ns",
            "evidence_digest",
        }
        or log_evidence["profile_digest"] != manifest["account_entry"]["profile_digest"]
        or log_evidence["evidence_digest"]
        != digest(
            {
                key: item
                for key, item in log_evidence.items()
                if key != "evidence_digest"
            }
        )
    ):
        raise CarryForwardError("log_evidence_invalid")
    _validate_log_clock(log_evidence["clock"], log_evidence["observed_at_ns"])
    _validate_log_endpoint(log_evidence, account_entry)


def _compare_group2_db_strict(before: dict[str, Any], after: dict[str, Any]) -> None:
    for key in ("protected_rows", "asset_projection", "schema_shape"):
        if before.get(key) != after.get(key):
            raise CarryForwardError(f"group2_{key}_changed")


def validate_manifest(value: Any) -> dict[str, Any]:
    required = {
        "format_version",
        "manifest_id",
        "created_at",
        "repo",
        "pr",
        "from_sha",
        "to_sha",
        "from_schema",
        "to_schema",
        "controller_files_digest",
        "migration_graph_digest",
        "old_image_ids",
        "candidate_image_ids",
        "selection",
        "responsibilities",
        "old_runtime_projection",
        "schema_inventory",
        "storage_identity",
        "old_containers",
        "file_evidence",
        "kernel_evidence",
        "manifest_digest",
    }
    if not isinstance(value, dict):
        raise CarryForwardError("manifest_shape_invalid")
    version = value.get("format_version")
    mode = (
        value.get("mode")
        if version in {AUDITED_FORMAT_VERSION, GROUP2_FORMAT_VERSION}
        else None
    )
    if version == AUDITED_FORMAT_VERSION:
        required = {*required, "mode", "source_diff"}
    elif version == GROUP2_FORMAT_VERSION:
        required = {
            *required,
            "mode",
            "source_diff",
            "review_scope",
            "review_scope_digest",
            "ci_binding",
            "preservation_reference_digest",
            "protected_rows",
            "asset_projection",
            "schema_shape",
            "account_entry",
            "log_evidence",
        }
    if set(value) != required:
        raise CarryForwardError("manifest_shape_invalid")
    if type(version) is not int or version not in {
        FORMAT_VERSION,
        AUDITED_FORMAT_VERSION,
        GROUP2_FORMAT_VERSION,
    }:
        raise CarryForwardError("manifest_version_invalid")
    if version == AUDITED_FORMAT_VERSION:
        if type(version) is not int:
            raise CarryForwardError("manifest_version_invalid")
        if mode != AUDITED_MODE:
            raise CarryForwardError("manifest_mode_invalid")
        if (
            value["from_schema"] != "0040_issue152_dispositions"
            or value["to_schema"] != "0040_issue152_dispositions"
        ):
            raise CarryForwardError("candidate_schema_path_unknown")
        validate_source_diff(value["source_diff"])
    elif version == GROUP2_FORMAT_VERSION:
        if mode != GROUP2_MODE:
            raise CarryForwardError("manifest_mode_invalid")
        if (
            value["from_schema"] != "0040_issue152_dispositions"
            or value["to_schema"] != "0040_issue152_dispositions"
        ):
            raise CarryForwardError("candidate_schema_path_unknown")
    if not isinstance(value["manifest_id"], str) or not MANIFEST_ID.fullmatch(
        value["manifest_id"]
    ):
        raise CarryForwardError("manifest_id_invalid")
    for key in ("from_sha", "to_sha"):
        if not isinstance(value[key], str) or not SHA.fullmatch(value[key]):
            raise CarryForwardError("manifest_sha_invalid")
    for key in ("controller_files_digest", "migration_graph_digest", "manifest_digest"):
        if not isinstance(value[key], str) or not DIGEST.fullmatch(value[key]):
            raise CarryForwardError("manifest_digest_invalid")
    _positive(value["pr"], "manifest_pr_invalid")
    if not isinstance(value["repo"], str) or not re.fullmatch(
        r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value["repo"]
    ):
        raise CarryForwardError("manifest_repo_invalid")
    try:
        dt.datetime.fromisoformat(value["created_at"])
    except (TypeError, ValueError) as error:
        raise CarryForwardError("manifest_time_invalid") from error
    for key in ("from_schema", "to_schema"):
        if not isinstance(value[key], str) or not re.fullmatch(
            r"[a-zA-Z0-9_.-]+", value[key]
        ):
            raise CarryForwardError("manifest_schema_invalid")
    validate_schema_transition(value["from_schema"], value["to_schema"])
    normalize_selection(value["selection"], mode=mode)
    validate_storage_identity(value["storage_identity"])
    validate_projection_evidence(
        value["old_runtime_projection"],
        required=AUDITED_TABLES if is_audited_mode(mode) else RESPONSIBILITY_TABLES,
    )
    if is_audited_mode(mode):
        responsibilities = value["responsibilities"]
        if (
            not isinstance(responsibilities, dict)
            or set(responsibilities) != {"executions", "terminal_executions"}
            or not isinstance(responsibilities["executions"], list)
            or not isinstance(responsibilities["terminal_executions"], list)
            or len(responsibilities["terminal_executions"])
            != len(value["selection"]["terminal_executions"])
        ):
            raise CarryForwardError("responsibility_invalid")
    inventory = value["schema_inventory"]
    if (
        not isinstance(inventory, dict)
        or set(inventory) != {"tables"}
        or not isinstance(inventory["tables"], list)
        or inventory["tables"] != sorted(set(inventory["tables"]))
        or not all(
            isinstance(name, str) and re.fullmatch(r"[a-z][a-z0-9_]*", name)
            for name in inventory["tables"]
        )
    ):
        raise CarryForwardError("schema_inventory_invalid")
    if mode == GROUP2_MODE:
        validate_group2_manifest_extensions(value)
    if digest(manifest_payload(value)) != value["manifest_digest"]:
        raise CarryForwardError("manifest_digest_mismatch")
    return value


def _command_check_db(args: argparse.Namespace) -> dict[str, Any]:
    baseline = read_private(args.baseline) if args.baseline else None
    if baseline and "manifest_digest" in baseline:
        baseline = validate_manifest(baseline)
    mode = manifest_mode(baseline) if baseline else getattr(args, "mode", None)
    selection = normalize_selection(
        read_private(args.ids)
        if args.ids
        else baseline.get("selection")
        if baseline
        else None,
        mode=mode,
    )
    baseline_projection = None
    if baseline:
        baseline_projection = baseline.get(
            "old_runtime_projection", baseline.get("projection")
        )
    result = inspect_database(
        selection,
        baseline_projection,
        baseline_schema_inventory=baseline.get("schema_inventory")
        if baseline
        else None,
        from_revision=baseline.get("from_schema") if baseline else None,
        to_revision=baseline.get("to_schema") if baseline else None,
        schema_phase=args.schema_phase,
        mode=mode,
    )
    result.pop("_credential_hashes", None)
    result.pop("_attempt_statuses", None)
    if args.baseline:
        compare_projection(baseline_projection, result["projection"])
        if baseline["responsibilities"] != result["responsibilities"]:
            raise CarryForwardError("responsibility_changed")
        if mode == GROUP2_MODE:
            _compare_group2_db_strict(baseline, result)
    write_private(args.output, result)
    return {"code": "db_ok", "tables": len(result["projection"])}


def _command_capture(args: argparse.Namespace) -> dict[str, Any]:
    materials: dict[str, Path] = {}
    for item in args.material_root:
        if "=" not in item:
            raise CarryForwardError("material_root_invalid")
        name, path = item.split("=", 1)
        if not re.fullmatch(r"[a-z][a-z0-9_-]*", name) or name in materials:
            raise CarryForwardError("material_root_invalid")
        materials[name] = Path(path)
    evidence = capture_files(
        args.runtime_root, args.journal_root, materials, args.expected_uid
    )
    if args.db:
        db = read_private(args.db)
        validate_file_responsibilities(evidence, db["responsibilities"])
    if args.baseline:
        baseline = read_private(args.baseline)
        if "manifest_digest" in baseline:
            baseline = validate_manifest(baseline)
        baseline_evidence = baseline.get("file_evidence", baseline)
        responsibilities = baseline.get("responsibilities")
        if responsibilities:
            validate_file_responsibilities(evidence, responsibilities)
        if baseline_evidence != evidence:
            raise CarryForwardError("file_evidence_changed")
    write_private(args.output, evidence)
    return {
        "code": "files_ok",
        "runtime_entries": len(evidence["runtime"]["entries"]),
        "journal_entries": len(evidence["journal"]["entries"]),
        "material_roots": len(evidence["materials"]),
    }


def _material_roots(values: list[str]) -> dict[str, Path]:
    materials: dict[str, Path] = {}
    for item in values:
        if "=" not in item:
            raise CarryForwardError("material_root_invalid")
        name, path = item.split("=", 1)
        if not re.fullmatch(r"[a-z][a-z0-9_-]*", name) or name in materials:
            raise CarryForwardError("material_root_invalid")
        materials[name] = Path(path)
    return materials


def _command_capture_state(args: argparse.Namespace) -> dict[str, Any]:
    admission_vm_lower_ns = time.time_ns()
    baseline = read_private(args.baseline) if args.baseline else None
    if baseline and "manifest_digest" in baseline:
        baseline = validate_manifest(baseline)
    mode = manifest_mode(baseline) if baseline else getattr(args, "mode", None)
    selection = normalize_selection(
        read_private(args.ids)
        if args.ids
        else baseline.get("selection")
        if baseline
        else None,
        mode=mode,
    )
    baseline_projection = None
    if baseline:
        baseline_projection = baseline.get(
            "old_runtime_projection", baseline.get("projection")
        )
    db = inspect_database(
        selection,
        baseline_projection,
        baseline_schema_inventory=baseline.get("schema_inventory")
        if baseline
        else None,
        from_revision=baseline.get("from_schema") if baseline else None,
        to_revision=baseline.get("to_schema") if baseline else None,
        schema_phase=args.schema_phase,
        mode=mode,
        group2_reboot_admission=bool(getattr(args, "admission_output", None)),
    )
    admission_raw = db.pop("_reboot_admission_raw", None)
    admission_database_rows = db.pop("_reboot_database_rows", None)
    admission_mutation_guard_rows = db.pop("_reboot_mutation_guard_rows", None)
    admission_clock = db.pop("_reboot_db_clock", None)
    admission_vm_upper_ns = time.time_ns()
    credential_hashes = db.pop("_credential_hashes")
    attempt_statuses = db.pop("_attempt_statuses")
    files = capture_files(
        args.runtime_root,
        args.journal_root,
        _material_roots(args.material_root),
        args.expected_uid,
        credential_hashes,
        attempt_statuses,
    )
    validate_file_responsibilities(files, db["responsibilities"])
    if baseline:
        compare_projection(baseline_projection, db["projection"])
        if baseline["responsibilities"] != db["responsibilities"]:
            raise CarryForwardError("responsibility_changed")
        if mode == GROUP2_MODE:
            _compare_group2_db_strict(baseline, db)
        baseline_files = baseline.get("file_evidence", baseline)
        if baseline_files != files:
            raise CarryForwardError("file_evidence_changed")
    write_private(args.db_output, db)
    write_private(args.files_output, files)
    if getattr(args, "admission_output", None):
        write_private(
            args.admission_output,
            {"raw_tables": admission_raw, "database_rows": admission_database_rows,
             "mutation_guard_rows": admission_mutation_guard_rows,
             "db_clock": admission_clock,
             "vm_lower_ns": admission_vm_lower_ns,
             "vm_upper_ns": admission_vm_upper_ns,
             "database_digest": digest(db), "files_digest": digest(files)},
        )
    return {
        "code": "state_ok",
        "tables": len(db["projection"]),
        "runtime_entries": len(files["runtime"]["entries"]),
        "journal_entries": len(files["journal"]["entries"]),
    }


def _command_kernel(args: argparse.Namespace) -> dict[str, Any]:
    baseline = None
    baseline_kernel = None
    recovery_markers = None
    if args.baseline:
        baseline = read_private(args.baseline)
        if "manifest_digest" in baseline:
            baseline = validate_manifest(baseline)
        baseline_kernel = baseline.get("kernel_evidence", baseline)
        recovery_markers = (
            baseline.get("file_evidence", {})
            .get("journal_facts", {})
            .get("sandbox_recovery", [])
        )
    evidence = capture_kernel(
        args.unit,
        require_idle=args.require_idle,
        expected_description=args.expected_description,
        worker_container=args.worker_container,
        runtime_volume=args.runtime_volume,
        journal_volume=args.journal_volume,
        baseline_authority=(
            baseline_kernel.get("old_worker_authority")
            if isinstance(baseline_kernel, dict)
            else None
        ),
        recovery_markers=recovery_markers,
    )
    if baseline_kernel is not None:
        compare_kernel(baseline_kernel, evidence)
    write_private(args.output, evidence)
    return {"code": "kernel_ok", "children": len(evidence["children"])}


def _command_plan(args: argparse.Namespace) -> dict[str, Any]:
    context = read_private(args.context)
    mode = context.get("mode")
    if mode not in {None, AUDITED_MODE, GROUP2_MODE}:
        raise CarryForwardError("manifest_mode_invalid")
    selection = normalize_selection(read_private(args.ids), mode=mode)
    db = read_private(args.db)
    files = read_private(args.files)
    kernel = read_private(args.kernel)
    validate_file_responsibilities(files, db["responsibilities"])
    validate_storage_identity(context.get("storage_identity"))
    workers = [
        item
        for item in context.get("old_containers", [])
        if item.get("service") == "worker"
    ]
    authority = kernel.get("old_worker_authority")
    if (
        len(workers) != 1
        or not isinstance(authority, dict)
        or any(
            authority.get(key) != workers[0].get(key)
            for key in ("container_id", "image_id", "labels")
        )
        or authority.get("runtime_config") != workers[0].get("runtime_config")
    ):
        raise CarryForwardError("kernel_identity_unknown")
    expected_volumes = {
        item["destination"]: item["source"]
        for item in context.get("storage_identity", [])
        if item.get("service") == "worker" and item.get("type") == "volume"
    }
    if {
        name: authority.get("volumes", {}).get(name, {}).get("name")
        for name in ("runtime", "journal")
    } != {
        "runtime": expected_volumes.get("/var/lib/dlr/runtime"),
        "journal": expected_volumes.get("/var/lib/dlr/journal"),
    }:
        raise CarryForwardError("kernel_identity_unknown")
    value = {
        "format_version": (
            GROUP2_FORMAT_VERSION
            if mode == GROUP2_MODE
            else AUDITED_FORMAT_VERSION
            if mode == AUDITED_MODE
            else FORMAT_VERSION
        ),
        "manifest_id": context["manifest_id"],
        "created_at": context["created_at"],
        "repo": context["repo"],
        "pr": context["pr"],
        "from_sha": context["from_sha"],
        "to_sha": context["to_sha"],
        "from_schema": context["from_schema"],
        "to_schema": context["to_schema"],
        "controller_files_digest": context["controller_files_digest"],
        "migration_graph_digest": context["migration_graph_digest"],
        "old_image_ids": context["old_image_ids"],
        "candidate_image_ids": context["candidate_image_ids"],
        "selection": selection,
        "responsibilities": db["responsibilities"],
        "old_runtime_projection": db["projection"],
        "schema_inventory": db["schema_inventory"],
        "storage_identity": context["storage_identity"],
        "old_containers": context["old_containers"],
        "file_evidence": files,
        "kernel_evidence": kernel,
    }
    if mode == AUDITED_MODE:
        value["mode"] = mode
        value["source_diff"] = validate_source_diff(context.get("source_diff"))
    elif mode == GROUP2_MODE:
        scope = validate_group2_review_scope(context.get("review_scope"))
        snapshot = scope["preservation_reference"]["snapshot"]
        if (
            snapshot["selection"] != selection
            or snapshot["db"] != db
            or snapshot["files"] != files
        ):
            raise CarryForwardError("group2_preservation_reference_mismatch")
        value.update(
            mode=mode,
            source_diff=validate_group2_source_diff(context.get("source_diff"), scope),
            review_scope=scope,
            review_scope_digest=scope["scope_digest"],
            ci_binding=scope["ci"],
            preservation_reference_digest=scope["preservation_reference"][
                "snapshot_digest"
            ],
            protected_rows=db["protected_rows"],
            asset_projection=db["asset_projection"],
            schema_shape=db["schema_shape"],
            account_entry=context["account_entry"],
            log_evidence=context["log_evidence"],
        )
    manifest = seal_manifest(value)
    write_private(args.output, manifest)
    return {"code": "manifest_ready", "manifest_id": manifest["manifest_id"]}


def _command_compare(args: argparse.Namespace) -> dict[str, Any]:
    before, after = read_private(args.before), read_private(args.after)
    compare_projection(before["projection"], after["projection"])
    if before["responsibilities"] != after["responsibilities"]:
        raise CarryForwardError("responsibility_changed")
    return {"code": "projection_ok", "tables": len(before["projection"])}


def _closed_request(value: Any, operation: str, fields: set[str]) -> dict[str, Any]:
    required = {"mode", "operation", *fields}
    if (
        not isinstance(value, dict)
        or set(value) != required
        or value["mode"] != GROUP2_MODE
        or value["operation"] != operation
    ):
        raise CarryForwardError("group2_runtime_request_invalid")
    return value


def _run_json(arguments: list[str], code: str) -> Any:
    try:
        completed = subprocess.run(
            arguments, check=True, capture_output=True, text=True, timeout=60
        )
        return json.loads(completed.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as error:
        raise CarryForwardError(code) from error


def _read_explicit_env(path: Path) -> dict[str, str]:
    _secure_file(path)
    result: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)", line)
        if match is None or match.group(1) in result:
            raise CarryForwardError("account_env_invalid")
        value = match.group(2)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        result[match.group(1)] = value
    required_ports = {"DLR_WEB_HOST_PORT", "DLR_ACCOUNT_WEB_HOST_PORT"}
    if not required_ports.issubset(result):
        raise CarryForwardError("account_port_missing")
    for name in required_ports:
        inherited = os.environ.get(name)
        if inherited is not None and inherited != result[name]:
            raise CarryForwardError("account_env_override")
    return result


def _loopback_binding(value: Any) -> tuple[str, str]:
    if not isinstance(value, str) or not value:
        raise CarryForwardError("account_binding_invalid")
    if value.startswith("["):
        match = re.fullmatch(r"\[([^]]+)\]:([1-9][0-9]{0,4})", value)
    else:
        match = re.fullmatch(r"([^:]+):([1-9][0-9]{0,4})", value)
    if match is None:
        raise CarryForwardError("account_binding_invalid")
    host, port = match.groups()
    try:
        address = ipaddress.ip_address(host)
    except ValueError as error:
        raise CarryForwardError("account_binding_invalid") from error
    if not address.is_loopback or int(port) > 65535:
        raise CarryForwardError("account_binding_invalid")
    return host, port


def _compose_json(project: str, release: Path, env_file: Path) -> dict[str, Any]:
    if not release.is_absolute() or ".." in release.parts or not release.is_dir():
        raise CarryForwardError("account_release_invalid")
    compose_file = release / "docker-compose.yml"
    overlay_file = release / "compose.preview.json"
    if not compose_file.is_file() or not overlay_file.is_file():
        raise CarryForwardError("account_release_invalid")
    return _run_json(
        [
            "docker",
            "compose",
            "--project-name",
            project,
            "--env-file",
            str(env_file),
            "-f",
            str(compose_file),
            "-f",
            str(overlay_file),
            "config",
            "--format",
            "json",
        ],
        "account_compose_invalid",
    )


def _service_profile(config: dict[str, Any], service: str) -> dict[str, Any]:
    value = config.get("services", {}).get(service)
    if not isinstance(value, dict):
        raise CarryForwardError("account_compose_invalid")
    declared_volumes = config.get("volumes") or {}
    mounts = []
    for item in value.get("volumes", []):
        if not isinstance(item, dict):
            raise CarryForwardError("account_compose_invalid")
        source = item.get("source")
        if item.get("type") == "volume":
            declaration = declared_volumes.get(source)
            if not isinstance(declaration, dict):
                raise CarryForwardError("account_compose_invalid")
            source = declaration.get("name")
        mounts.append(
            {
                "type": item.get("type"),
                "source": source,
                "destination": item.get("target"),
                "rw": not bool(item.get("read_only", False)),
            }
        )
    ports = []
    for item in value.get("ports", []):
        if not isinstance(item, dict):
            raise CarryForwardError("account_binding_invalid")
        ports.append(
            {
                "host_ip": item.get("host_ip"),
                "published": str(item.get("published")),
                "target": int(item.get("target")),
                "protocol": item.get("protocol", "tcp"),
            }
        )
    declared_networks = config.get("networks") or {}
    network_names = []
    for name in value.get("networks") or {}:
        declaration = declared_networks.get(name)
        if not isinstance(declaration, dict) or not isinstance(
            declaration.get("name"), str
        ):
            raise CarryForwardError("account_compose_invalid")
        network_names.append(declaration["name"])
    return {
        "image": value.get("image"),
        "command": value.get("command"),
        "effective_command": None,
        "networks": sorted(network_names),
        "mounts": sorted(
            mounts, key=lambda item: (str(item["destination"]), str(item["source"]))
        ),
        "ports": sorted(ports, key=lambda item: (item["target"], item["published"])),
    }


def _command_part(value: Any) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return value
    raise CarryForwardError("account_image_config_invalid")


def _image_command(image_id: str) -> list[list[str] | None]:
    raw = _run_json(
        ["docker", "image", "inspect", image_id], "account_image_config_invalid"
    )
    if not isinstance(raw, list) or len(raw) != 1 or not isinstance(raw[0], dict):
        raise CarryForwardError("account_image_config_invalid")
    config = raw[0].get("Config")
    if not isinstance(config, dict) or config.get("Volumes") is not None:
        raise CarryForwardError("account_image_config_invalid")
    return [_command_part(config.get("Entrypoint")), _command_part(config.get("Cmd"))]


def _effective_command(
    config: dict[str, Any], service: str, image_command: list[list[str] | None]
) -> list[list[str] | None]:
    service_value = config.get("services", {}).get(service)
    if not isinstance(service_value, dict):
        raise CarryForwardError("account_compose_invalid")
    entrypoint = service_value.get("entrypoint")
    command = service_value.get("command")
    return [
        image_command[0] if entrypoint is None else _command_part(entrypoint),
        image_command[1] if command is None else _command_part(command),
    ]


def _profile_port_bindings(profile: dict[str, Any]) -> dict[str, list[dict[str, str]]]:
    result: dict[str, list[dict[str, str]]] = {}
    for port in profile["ports"]:
        key = f"{port['target']}/{port['protocol']}"
        result.setdefault(key, []).append(
            {"HostIp": port["host_ip"], "HostPort": port["published"]}
        )
    return result


def _container_matches_profile(
    container: Any, profile: Any, project: str, service: str
) -> bool:
    return bool(
        isinstance(container, dict)
        and isinstance(profile, dict)
        and container.get("labels", {}).get("com.docker.compose.project") == project
        and container.get("labels", {}).get("com.docker.compose.service") == service
        and isinstance(container.get("command"), list)
        and len(container["command"]) == 2
        and container["command"] == profile.get("effective_command")
        and container.get("port_bindings") == _profile_port_bindings(profile)
        and container.get("mounts") == profile.get("mounts")
        and container.get("networks") == profile.get("networks")
    )


def _inspect_container(project: str, service: str) -> dict[str, Any]:
    raw = _run_json(
        ["docker", "inspect", f"{project}-{service}-1"], "account_inspect_invalid"
    )
    if not isinstance(raw, list) or len(raw) != 1 or not isinstance(raw[0], dict):
        raise CarryForwardError("account_inspect_invalid")
    item = raw[0]
    state = item.get("State") or {}
    network = item.get("NetworkSettings") or {}
    host = item.get("HostConfig") or {}
    config = item.get("Config") or {}
    return {
        "container_id": item.get("Id"),
        "image_id": item.get("Image"),
        "status": state.get("Status"),
        "health": (state.get("Health") or {}).get("Status"),
        "started_at": state.get("StartedAt"),
        "restart_count": item.get("RestartCount"),
        "command": [
            _command_part(config.get("Entrypoint")),
            _command_part(config.get("Cmd")),
        ],
        "labels": config.get("Labels") or {},
        "port_bindings": host.get("PortBindings") or {},
        "mounts": sorted(
            [
                {
                    "type": mount.get("Type"),
                    "source": (
                        mount.get("Name")
                        if mount.get("Type") == "volume"
                        else mount.get("Source")
                    ),
                    "destination": mount.get("Destination"),
                    "rw": mount.get("RW"),
                }
                for mount in item.get("Mounts", [])
            ],
            key=lambda mount: str(mount["destination"]),
        ),
        "networks": sorted((network.get("Networks") or {}).keys()),
    }


def capture_group2_account_entry(request: Any) -> dict[str, Any]:
    value = _closed_request(
        request,
        "account-capture",
        {
            "project",
            "from_sha",
            "to_sha",
            "old_release",
            "candidate_release",
            "env_file",
            "old_image_ids",
            "candidate_image_ids",
        },
    )
    if not isinstance(value["project"], str) or not re.fullmatch(
        r"[a-z0-9][a-z0-9_-]*", value["project"]
    ):
        raise CarryForwardError("account_project_invalid")
    for key in ("from_sha", "to_sha"):
        _sha_text(value[key], "account_sha_invalid")
    env_file = Path(value["env_file"])
    env = _read_explicit_env(env_file)
    old = _compose_json(value["project"], Path(value["old_release"]), env_file)
    candidate = _compose_json(
        value["project"], Path(value["candidate_release"]), env_file
    )
    old_profiles = {
        name: _service_profile(old, name)
        for name in ("control", "worker", "web", "account-web")
    }
    candidate_profiles = {
        name: _service_profile(candidate, name)
        for name in ("control", "worker", "web", "account-web")
    }
    candidate_account = candidate_profiles["account-web"]
    expected_bindings = {
        "account-web": _loopback_binding(env["DLR_ACCOUNT_WEB_HOST_PORT"]),
        "web": _loopback_binding(env["DLR_WEB_HOST_PORT"]),
    }
    for service, (host, published) in expected_bindings.items():
        ports = candidate_profiles[service]["ports"]
        if ports != [
            {
                "host_ip": host,
                "published": published,
                "target": 80,
                "protocol": "tcp",
            }
        ]:
            raise CarryForwardError("account_binding_invalid")
    if old_profiles["control"]["ports"] or old_profiles["worker"]["ports"]:
        raise CarryForwardError("account_binding_invalid")
    port = candidate_account["ports"][0]
    old_containers = {
        name: _inspect_container(value["project"], name) for name in old_profiles
    }
    old_images = value["old_image_ids"]
    candidate_images = value["candidate_image_ids"]
    if (
        not isinstance(old_images, dict)
        or not old_images
        or not isinstance(candidate_images, dict)
        or not candidate_images
    ):
        raise CarryForwardError("account_image_invalid")

    def bound_image(images: dict[str, Any], service: str, sha: str) -> str:
        matches = [
            image for tag, image in images.items() if tag.endswith(f"-{service}:{sha}")
        ]
        if len(matches) != 1 or not isinstance(matches[0], str) or not matches[0]:
            raise CarryForwardError("account_image_invalid")
        return matches[0]

    candidate_image_ids_by_service = {}
    for service in ("control", "worker", "web"):
        old_image = bound_image(old_images, service, value["from_sha"])
        candidate_image = bound_image(candidate_images, service, value["to_sha"])
        if (
            old_containers[service]["image_id"] != old_image
            or old_images.get(old_profiles[service]["image"]) != old_image
            or candidate_images.get(candidate_profiles[service]["image"])
            != candidate_image
        ):
            raise CarryForwardError("account_image_invalid")
        candidate_image_ids_by_service[service] = candidate_image
    web_image = bound_image(candidate_images, "web", value["to_sha"])
    if (
        old_profiles["account-web"]["image"] != old_profiles["web"]["image"]
        or candidate_profiles["account-web"]["image"]
        != candidate_profiles["web"]["image"]
    ):
        raise CarryForwardError("account_image_invalid")
    if not isinstance(web_image, str):
        raise CarryForwardError("account_image_invalid")
    candidate_image_ids_by_service["account-web"] = web_image
    image_commands: dict[str, list[list[str] | None]] = {}

    def image_command(image_id: str) -> list[list[str] | None]:
        if image_id not in image_commands:
            image_commands[image_id] = _image_command(image_id)
        return image_commands[image_id]

    for service, old_service_profile in old_profiles.items():
        old_service_profile["effective_command"] = _effective_command(
            old, service, image_command(old_containers[service]["image_id"])
        )
        candidate_profiles[service]["effective_command"] = _effective_command(
            candidate,
            service,
            image_command(candidate_image_ids_by_service[service]),
        )
        old_profile = {
            key: item for key, item in old_service_profile.items() if key != "image"
        }
        candidate_profile = {
            key: item
            for key, item in candidate_profiles[service].items()
            if key != "image"
        }
        if old_profile != candidate_profile:
            raise CarryForwardError("account_profile_changed")
    for service, container in old_containers.items():
        if not _container_matches_profile(
            container, old_profiles[service], value["project"], service
        ):
            raise CarryForwardError("account_old_binding_invalid")
    log_files = []
    log_roots = []
    for service, profile in candidate_profiles.items():
        target = f"/var/lib/dlr/platform-logs/{service}"
        matches = [
            mount for mount in profile["mounts"] if mount["destination"] == target
        ]
        if len(matches) != 1 or not isinstance(matches[0]["source"], str):
            raise CarryForwardError("account_log_mount_invalid")
        names = (
            ("access.log", "error.log")
            if service in {"web", "account-web"}
            else (f"{service}.log",)
        )
        log_files.extend(str(Path(matches[0]["source"]) / name) for name in names)
        log_roots.append(
            {"path": matches[0]["source"], "allowed_new_files": sorted(names)}
        )
    profile = {
        "project": value["project"],
        "from_sha": value["from_sha"],
        "to_sha": value["to_sha"],
        "old_containers": old_containers,
        "old_profiles": old_profiles,
        "candidate_profiles": candidate_profiles,
        "candidate_web_image_id": web_image,
        "candidate_image_ids_by_service": candidate_image_ids_by_service,
        "ports": {"account": port, "token": candidate_profiles["web"]["ports"]},
        "log_files": sorted(log_files),
        "log_roots": sorted(log_roots, key=lambda item: item["path"]),
    }
    profile["profile_digest"] = digest(profile)
    return profile


def validate_group2_account_entry(value: Any) -> dict[str, Any]:
    keys = {
        "project",
        "from_sha",
        "to_sha",
        "old_containers",
        "old_profiles",
        "candidate_profiles",
        "candidate_web_image_id",
        "candidate_image_ids_by_service",
        "ports",
        "log_files",
        "log_roots",
        "profile_digest",
    }
    if not isinstance(value, dict) or set(value) != keys:
        raise CarryForwardError("account_entry_invalid")
    if value["profile_digest"] != digest(
        {key: item for key, item in value.items() if key != "profile_digest"}
    ):
        raise CarryForwardError("account_entry_invalid")
    services = {"control", "worker", "web", "account-web"}
    if (
        not isinstance(value["project"], str)
        or re.fullmatch(r"[a-z0-9][a-z0-9_-]*", value["project"]) is None
        or SHA.fullmatch(value["from_sha"]) is None
        or SHA.fullmatch(value["to_sha"]) is None
        or not isinstance(value["old_profiles"], dict)
        or set(value["old_profiles"]) != services
        or not isinstance(value["candidate_profiles"], dict)
        or set(value["candidate_profiles"]) != services
        or not isinstance(value["old_containers"], dict)
        or set(value["old_containers"]) != services
        or not isinstance(value["ports"], dict)
        or set(value["ports"]) != {"account", "token"}
    ):
        raise CarryForwardError("account_entry_invalid")
    account_ports = value["candidate_profiles"]["account-web"].get("ports")
    if (
        not isinstance(account_ports, list)
        or len(account_ports) != 1
        or value["ports"]["account"] != account_ports[0]
        or value["ports"]["token"] != value["candidate_profiles"]["web"].get("ports")
    ):
        raise CarryForwardError("account_entry_invalid")
    profile_keys = {
        "image",
        "command",
        "effective_command",
        "networks",
        "mounts",
        "ports",
    }
    container_keys = {
        "container_id",
        "image_id",
        "status",
        "health",
        "started_at",
        "restart_count",
        "command",
        "labels",
        "port_bindings",
        "mounts",
        "networks",
    }
    if any(
        not isinstance(profile, dict) or set(profile) != profile_keys
        for group in (value["old_profiles"], value["candidate_profiles"])
        for profile in group.values()
    ) or any(
        not isinstance(container, dict) or set(container) != container_keys
        for container in value["old_containers"].values()
    ):
        raise CarryForwardError("account_entry_invalid")
    if not isinstance(value["log_files"], list) or value["log_files"] != sorted(
        set(value["log_files"])
    ):
        raise CarryForwardError("account_entry_invalid")
    if (
        not isinstance(value["candidate_image_ids_by_service"], dict)
        or set(value["candidate_image_ids_by_service"])
        != {"control", "worker", "web", "account-web"}
        or value["candidate_image_ids_by_service"]["account-web"]
        != value["candidate_web_image_id"]
        or value["candidate_image_ids_by_service"].get("web")
        != value["candidate_web_image_id"]
        or any(
            not isinstance(image, str) or not image
            for image in value["candidate_image_ids_by_service"].values()
        )
        or not isinstance(value["log_roots"], list)
        or value["log_roots"]
        != sorted(value["log_roots"], key=lambda item: item.get("path", ""))
    ):
        raise CarryForwardError("account_entry_invalid")
    approved = set(value["log_files"])
    expected_roots = []
    for service, service_profile in value["candidate_profiles"].items():
        target = f"/var/lib/dlr/platform-logs/{service}"
        matches = [
            mount
            for mount in service_profile.get("mounts", [])
            if mount.get("destination") == target
        ]
        if len(matches) != 1 or not isinstance(matches[0].get("source"), str):
            raise CarryForwardError("account_entry_invalid")
        names = (
            ["access.log", "error.log"]
            if service in {"web", "account-web"}
            else [f"{service}.log"]
        )
        expected_roots.append(
            {"path": matches[0]["source"], "allowed_new_files": names}
        )
    if value["log_roots"] != sorted(expected_roots, key=lambda item: item["path"]):
        raise CarryForwardError("account_entry_invalid")
    for root in value["log_roots"]:
        if (
            not isinstance(root, dict)
            or set(root) != {"path", "allowed_new_files"}
            or not isinstance(root["path"], str)
            or not Path(root["path"]).is_absolute()
            or not isinstance(root["allowed_new_files"], list)
            or root["allowed_new_files"] != sorted(set(root["allowed_new_files"]))
            or any(
                not isinstance(name, str)
                or not name
                or "/" in name
                or name in {".", ".."}
                for name in root["allowed_new_files"]
            )
            or any(
                str(Path(root["path"]) / name) not in approved
                for name in root["allowed_new_files"]
            )
        ):
            raise CarryForwardError("account_entry_invalid")
    return value


def check_group2_entry_boundaries(request: Any) -> dict[str, Any]:
    value = _closed_request(
        request,
        "account-check",
        {"profile", "project", "to_sha", "candidate_image_ids"},
    )
    profile = validate_group2_account_entry(value["profile"])
    if value["project"] != profile["project"] or value["to_sha"] != profile["to_sha"]:
        raise CarryForwardError("account_profile_changed")
    containers = {
        name: _inspect_container(value["project"], name)
        for name in ("control", "worker", "web", "account-web")
    }
    images = value["candidate_image_ids"]
    expected = {}
    for service in ("control", "worker", "web"):
        matches = [
            image
            for tag, image in images.items()
            if tag.endswith(f"-{service}:{value['to_sha']}")
        ]
        if len(matches) != 1:
            raise CarryForwardError("account_image_invalid")
        expected[service] = matches[0]
    if any(
        containers[service]["image_id"] != expected[service] for service in expected
    ):
        raise CarryForwardError("account_candidate_invalid")
    binding = profile["ports"]["account"]
    host = binding["host_ip"]
    authority = f"[{host}]" if ":" in host else host
    account_csrf = _http_result(
        f"http://{authority}:{binding['published']}/api/auth/account/csrf"
    )
    return validate_group2_account_check(
        profile,
        {
            "containers": containers,
            "profile_digest": profile["profile_digest"],
            "account_csrf": account_csrf,
        },
    )


_HTTP_RESULT_KEYS = {
    "status",
    "code",
    "body_status",
    "csrf_cookie",
    "csrf_cookie_path",
    "csrf_cookie_samesite_lax",
    "csrf_cookie_httponly",
    "redirect",
}


def _validate_http_result(value: Any, code: str) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or set(value) != _HTTP_RESULT_KEYS
        or not isinstance(value["status"], int)
        or isinstance(value["status"], bool)
        or not 100 <= value["status"] <= 599
        or value["code"] is not None
        and not isinstance(value["code"], str)
        or value["body_status"] is not None
        and not isinstance(value["body_status"], str)
        or any(
            not isinstance(value[key], bool)
            for key in (
                "csrf_cookie",
                "csrf_cookie_path",
                "csrf_cookie_samesite_lax",
                "csrf_cookie_httponly",
                "redirect",
            )
        )
    ):
        raise CarryForwardError(code)
    return value


def validate_group2_account_check(profile: Any, result: Any) -> dict[str, Any]:
    profile = validate_group2_account_entry(profile)
    services = {"control", "worker", "web", "account-web"}
    if (
        not isinstance(result, dict)
        or set(result) != {"containers", "profile_digest", "account_csrf"}
        or result["profile_digest"] != profile["profile_digest"]
        or not isinstance(result["containers"], dict)
        or set(result["containers"]) != services
    ):
        raise CarryForwardError("account_check_invalid")
    containers = result["containers"]
    for service, container in containers.items():
        if (
            not isinstance(container, dict)
            or set(container) != set(profile["old_containers"][service])
            or container.get("image_id")
            != profile["candidate_image_ids_by_service"][service]
            or container.get("command")
            != profile["old_containers"][service].get("command")
            or container.get("status") != "running"
            or container.get("health") != "healthy"
            or not _container_matches_profile(
                container,
                profile["candidate_profiles"][service],
                profile["project"],
                service,
            )
        ):
            raise CarryForwardError("account_binding_changed")
    account_csrf = _validate_http_result(
        result["account_csrf"], "account_check_invalid"
    )
    if (
        account_csrf["status"] != 200
        or account_csrf["code"] is not None
        or account_csrf["body_status"] != "ok"
        or not account_csrf["csrf_cookie"]
        or not account_csrf["csrf_cookie_path"]
        or not account_csrf["csrf_cookie_samesite_lax"]
        or account_csrf["csrf_cookie_httponly"]
        or account_csrf["redirect"]
    ):
        raise CarryForwardError("account_entry_unhealthy")
    return result


class _Timespec(ctypes.Structure):
    _fields_ = (("tv_sec", ctypes.c_long), ("tv_nsec", ctypes.c_long))


def _log_observation() -> tuple[dict[str, Any], int]:
    if sys.platform == "darwin":
        precise = time.time_ns()
        resolution_ns = max(
            1, math.ceil(time.get_clock_info("time").resolution * 1_000_000_000)
        )
        return {
            "kind": "precise-realtime",
            "lower_bound_ns": precise,
            "resolution_ns": resolution_ns,
        }, precise
    if sys.platform != "linux":
        raise CarryForwardError("log_clock_unavailable")
    library = ctypes.CDLL(None, use_errno=True)
    clock_id = 5  # Linux CLOCK_REALTIME_COARSE ABI value.
    current = _Timespec()
    resolution = _Timespec()
    if library.clock_gettime(clock_id, ctypes.byref(current)) != 0:
        raise CarryForwardError("log_clock_unavailable")
    if library.clock_getres(clock_id, ctypes.byref(resolution)) != 0:
        raise CarryForwardError("log_clock_unavailable")
    lower_bound_ns = current.tv_sec * 1_000_000_000 + current.tv_nsec
    resolution_ns = resolution.tv_sec * 1_000_000_000 + resolution.tv_nsec
    precise = time.time_ns()
    if resolution_ns <= 0 or lower_bound_ns > precise:
        raise CarryForwardError("log_clock_invalid")
    return {
        "kind": "linux-realtime-coarse",
        "lower_bound_ns": lower_bound_ns,
        "resolution_ns": resolution_ns,
    }, precise


def capture_log_prefix(profile: Any) -> dict[str, Any]:
    profile = validate_group2_account_entry(profile)
    files = []
    roots = []
    observed_paths = set()
    for declaration in profile["log_roots"]:
        root = Path(declaration["path"])
        root_info = root.lstat()
        if not stat.S_ISDIR(root_info.st_mode) or stat.S_ISLNK(root_info.st_mode):
            raise CarryForwardError("log_root_invalid")
        entries = []
        for path in sorted(root.rglob("*")):
            info = path.lstat()
            relative = path.relative_to(root).as_posix()
            common = {
                "path": relative,
                "device": info.st_dev,
                "inode": info.st_ino,
                "mode": stat.S_IMODE(info.st_mode),
                "uid": info.st_uid,
                "gid": info.st_gid,
            }
            if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
                entries.append(
                    {**common, "type": "directory", "mtime_ns": info.st_mtime_ns}
                )
                continue
            if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                raise CarryForwardError("log_file_invalid")
            content, stable, _ = _read_utf8_log_snapshot(path, info)
            item = {
                "path": str(path),
                "exists": True,
                "device": stable.st_dev,
                "inode": stable.st_ino,
                "mode": stat.S_IMODE(stable.st_mode),
                "uid": stable.st_uid,
                "gid": stable.st_gid,
                "size": len(content),
                "prefix_sha256": hashlib.sha256(content).hexdigest(),
            }
            files.append(item)
            observed_paths.add(str(path))
            entries.append({**common, "type": "file"})
        roots.append(
            {
                "path": str(root),
                "device": root_info.st_dev,
                "inode": root_info.st_ino,
                "mode": stat.S_IMODE(root_info.st_mode),
                "uid": root_info.st_uid,
                "gid": root_info.st_gid,
                "mtime_ns": root_info.st_mtime_ns,
                "entries": entries,
                "allowed_new_files": declaration["allowed_new_files"],
            }
        )
    for text_path in profile["log_files"]:
        if text_path not in observed_paths:
            files.append({"path": text_path, "exists": False})
    files.sort(key=lambda item: item["path"])
    clock, observed_at_ns = _log_observation()
    result = {
        "profile_digest": profile["profile_digest"],
        "files": files,
        "roots": roots,
        "clock": clock,
        "observed_at_ns": observed_at_ns,
    }
    result["evidence_digest"] = digest(result)
    _validate_log_endpoint(result, profile)
    return result


def read_log_append(baseline: Any) -> dict[str, Any]:
    capture_keys = {
        "profile_digest",
        "files",
        "roots",
        "clock",
        "observed_at_ns",
        "evidence_digest",
    }
    append_keys = {
        "profile_digest",
        "files",
        "roots",
        "clock",
        "baseline_evidence_digest",
        "observed_after_ns",
        "evidence_digest",
    }
    if (
        not isinstance(baseline, dict)
        or frozenset(baseline)
        not in {
            frozenset(capture_keys),
            frozenset(append_keys),
        }
        or baseline["evidence_digest"]
        != digest(
            {key: item for key, item in baseline.items() if key != "evidence_digest"}
        )
    ):
        raise CarryForwardError("log_evidence_invalid")
    baseline_digest = baseline["evidence_digest"]
    chained = set(baseline) == append_keys
    observed_before_ns = baseline.get(
        "observed_after_ns", baseline.get("observed_at_ns")
    )
    if not isinstance(observed_before_ns, int) or isinstance(observed_before_ns, bool):
        raise CarryForwardError("log_evidence_invalid")
    _validate_log_clock(baseline["clock"], observed_before_ns)
    _validate_log_endpoint(baseline)
    baseline_files = []
    for item in baseline["files"]:
        if not chained:
            baseline_files.append(item)
            continue
        if not isinstance(item, dict) or not {
            "path",
            "exists",
            "appended_text",
            "end_size",
            "end_sha256",
        }.issubset(item):
            raise CarryForwardError("log_evidence_invalid")
        _digest_text(item["end_sha256"], "log_evidence_invalid")
        if item["exists"]:
            required = {
                "path",
                "exists",
                "device",
                "inode",
                "mode",
                "uid",
                "gid",
                "size",
                "prefix_sha256",
                "appended_text",
                "end_size",
                "end_sha256",
            }
            if set(item) != required:
                raise CarryForwardError("log_evidence_invalid")
            baseline_files.append(
                {
                    key: item[key]
                    for key in (
                        "path",
                        "exists",
                        "device",
                        "inode",
                        "mode",
                        "uid",
                        "gid",
                    )
                }
                | {
                    "size": item["end_size"],
                    "prefix_sha256": item["end_sha256"],
                }
            )
        else:
            if set(item) != {
                "path",
                "exists",
                "appended_text",
                "end_size",
                "end_sha256",
            } or (item["appended_text"], item["end_size"], item["end_sha256"]) != (
                "",
                0,
                hashlib.sha256(b"").hexdigest(),
            ):
                raise CarryForwardError("log_evidence_invalid")
            baseline_files.append({"path": item["path"], "exists": False})
    expected_paths = {item["path"] for item in baseline_files}
    current_paths = set()
    output_roots = []
    for root_item in baseline["roots"]:
        if (
            not isinstance(root_item, dict)
            or set(root_item)
            != {
                "path",
                "device",
                "inode",
                "mode",
                "uid",
                "gid",
                "mtime_ns",
                "entries",
                "allowed_new_files",
            }
            or not isinstance(root_item["mtime_ns"], int)
            or isinstance(root_item["mtime_ns"], bool)
            or not isinstance(root_item["entries"], list)
        ):
            raise CarryForwardError("log_evidence_invalid")
        root = Path(root_item["path"])
        info = root.lstat()
        identity = (
            info.st_dev,
            info.st_ino,
            stat.S_IMODE(info.st_mode),
            info.st_uid,
            info.st_gid,
        )
        expected_identity = tuple(
            root_item[key] for key in ("device", "inode", "mode", "uid", "gid")
        )
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or identity != expected_identity
        ):
            raise CarryForwardError("log_root_changed")
        entries = []
        for path in sorted(root.rglob("*")):
            child = path.lstat()
            relative = path.relative_to(root).as_posix()
            common = {
                "path": relative,
                "device": child.st_dev,
                "inode": child.st_ino,
                "mode": stat.S_IMODE(child.st_mode),
                "uid": child.st_uid,
                "gid": child.st_gid,
            }
            if stat.S_ISDIR(child.st_mode) and not stat.S_ISLNK(child.st_mode):
                entries.append(
                    {**common, "type": "directory", "mtime_ns": child.st_mtime_ns}
                )
            elif stat.S_ISREG(child.st_mode) and not stat.S_ISLNK(child.st_mode):
                entries.append({**common, "type": "file"})
                current_paths.add(str(path))
            else:
                raise CarryForwardError("log_file_invalid")
        if any(
            not isinstance(item, dict)
            or set(item)
            != (
                {
                    "path",
                    "device",
                    "inode",
                    "mode",
                    "uid",
                    "gid",
                    "type",
                    "mtime_ns",
                }
                if item.get("type") == "directory"
                else {"path", "device", "inode", "mode", "uid", "gid", "type"}
            )
            or item.get("type") not in {"directory", "file"}
            for item in root_item["entries"]
        ):
            raise CarryForwardError("log_evidence_invalid")
        old_entries = {item["path"]: item for item in root_item["entries"]}
        if len(old_entries) != len(root_item["entries"]):
            raise CarryForwardError("log_evidence_invalid")
        new_entries = {item["path"]: item for item in entries}
        for relative, old in old_entries.items():
            if relative not in new_entries or new_entries[relative] != old:
                raise CarryForwardError("log_root_changed")
        allowed_new = set(root_item["allowed_new_files"])
        additions = set(new_entries) - set(old_entries)
        if additions - allowed_new or any(
            new_entries[name]["type"] != "file" for name in additions
        ):
            raise CarryForwardError("log_unknown_path")
        root_mtime_ns = info.st_mtime_ns
        if additions:
            if root_mtime_ns < root_item["mtime_ns"]:
                raise CarryForwardError("log_root_changed")
        elif root_mtime_ns != root_item["mtime_ns"]:
            raise CarryForwardError("log_root_changed")
        output_roots.append(
            {**root_item, "mtime_ns": root_mtime_ns, "entries": entries}
        )
    if current_paths - expected_paths:
        raise CarryForwardError("log_unknown_path")
    output = []
    for item in baseline_files:
        path = Path(item["path"])
        if not item["exists"]:
            if not path.exists():
                output.append(
                    {
                        **item,
                        "appended_text": "",
                        "end_size": 0,
                        "end_sha256": hashlib.sha256(b"").hexdigest(),
                    }
                )
                continue
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                raise CarryForwardError("log_file_invalid")
            content, stable, appended_text = _read_utf8_log_snapshot(path, info, 0)
            output.append(
                {
                    "path": item["path"],
                    "exists": True,
                    "device": stable.st_dev,
                    "inode": stable.st_ino,
                    "mode": stat.S_IMODE(stable.st_mode),
                    "uid": stable.st_uid,
                    "gid": stable.st_gid,
                    "size": 0,
                    "prefix_sha256": hashlib.sha256(b"").hexdigest(),
                    "appended_text": appended_text,
                    "end_size": len(content),
                    "end_sha256": hashlib.sha256(content).hexdigest(),
                }
            )
            continue
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise CarryForwardError("log_file_invalid")
        if (
            info.st_dev,
            info.st_ino,
            stat.S_IMODE(info.st_mode),
            info.st_uid,
            info.st_gid,
        ) != (
            item["device"],
            item["inode"],
            item["mode"],
            item["uid"],
            item["gid"],
        ) or info.st_size < item["size"]:
            raise CarryForwardError("log_prefix_changed")
        content, stable, appended_text = _read_utf8_log_snapshot(
            path,
            info,
            item["size"],
            item["prefix_sha256"],
        )
        output.append(
            {
                **item,
                "appended_text": appended_text,
                "end_size": len(content),
                "end_sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    clock, observed_after_ns = _log_observation()
    for previous, current in zip(baseline["roots"], output_roots, strict=True):
        previous_entries = {item["path"] for item in previous["entries"]}
        current_entries = {item["path"] for item in current["entries"]}
        if current_entries - previous_entries:
            _window_ns(
                current["mtime_ns"],
                max(previous["mtime_ns"], baseline["clock"]["lower_bound_ns"]),
                observed_after_ns,
                "log_root_changed",
            )
        elif current["mtime_ns"] != previous["mtime_ns"]:
            raise CarryForwardError("log_root_changed")
    result = {
        "profile_digest": baseline["profile_digest"],
        "files": output,
        "roots": output_roots,
        "clock": clock,
        "baseline_evidence_digest": baseline_digest,
        "observed_after_ns": observed_after_ns,
    }
    result["evidence_digest"] = digest(result)
    _validate_log_link(baseline, result, baseline["profile_digest"])
    return result


def _log_stat_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        stat.S_IMODE(value.st_mode),
        value.st_uid,
        value.st_gid,
    )


def _validate_log_stat_transition(
    before: os.stat_result,
    after: os.stat_result,
    minimum_size: int,
) -> None:
    if (
        not stat.S_ISREG(before.st_mode)
        or not stat.S_ISREG(after.st_mode)
        or _log_stat_identity(before) != _log_stat_identity(after)
        or after.st_size < minimum_size
        or after.st_size < before.st_size
        or (
            after.st_size == before.st_size
            and after.st_mtime_ns != before.st_mtime_ns
        )
        or (
            after.st_size == before.st_size
            and after.st_ctime_ns != before.st_ctime_ns
        )
    ):
        raise CarryForwardError("log_file_unstable")


def _log_timestamp_transition_only(
    before: os.stat_result,
    after: os.stat_result,
    minimum_size: int,
) -> bool:
    return (
        stat.S_ISREG(before.st_mode)
        and stat.S_ISREG(after.st_mode)
        and _log_stat_identity(before) == _log_stat_identity(after)
        and after.st_size == before.st_size
        and after.st_size >= minimum_size
        and (
            after.st_mtime_ns != before.st_mtime_ns
            or after.st_ctime_ns != before.st_ctime_ns
        )
    )


def _read_log_prefix(descriptor: int, length: int) -> bytes:
    os.lseek(descriptor, 0, os.SEEK_SET)
    remaining = length
    chunks = []
    while remaining:
        chunk = os.read(descriptor, min(1024 * 1024, remaining))
        if not chunk:
            raise CarryForwardError("log_file_unstable")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _resolve_log_append_metadata_race(
    descriptor: int,
    path: Path,
    identity: os.stat_result,
    observed_size: int,
    transient_size: int,
    high_watermark: int,
    content: bytes,
) -> None:
    for _ in range(3):
        time.sleep(0.001)
        if _read_log_prefix(descriptor, observed_size) != content:
            raise CarryForwardError("log_file_unstable")
        descriptor_info = os.fstat(descriptor)
        try:
            path_info = path.lstat()
        except OSError as error:
            raise CarryForwardError("log_file_unstable") from error
        for current in (descriptor_info, path_info):
            if (
                not stat.S_ISREG(current.st_mode)
                or _log_stat_identity(current) != _log_stat_identity(identity)
                or current.st_size < high_watermark
            ):
                raise CarryForwardError("log_file_unstable")
        if path_info.st_size < descriptor_info.st_size:
            raise CarryForwardError("log_file_unstable")
        high_watermark = path_info.st_size
        if (
            descriptor_info.st_size > transient_size
            and path_info.st_size > transient_size
        ):
            return
    raise CarryForwardError("log_file_unstable")


def _read_stable_log(
    path: Path, expected: os.stat_result | None = None
) -> tuple[bytes, os.stat_result]:
    try:
        path_before = path.lstat()
    except OSError as error:
        raise CarryForwardError("log_read_failed") from error
    if expected is not None:
        _validate_log_stat_transition(expected, path_before, expected.st_size)
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    descriptor = None
    try:
        descriptor = os.open(path, flags)
        before = os.fstat(descriptor)
        _validate_log_stat_transition(path_before, before, path_before.st_size)
        observed_size = before.st_size
        first = _read_log_prefix(descriptor, observed_size)
        second = _read_log_prefix(descriptor, observed_size)
        if first != second:
            raise CarryForwardError("log_file_unstable")
        after = os.fstat(descriptor)
        try:
            path_after = path.lstat()
        except OSError as error:
            raise CarryForwardError("log_file_unstable") from error
        transient_size = None
        for previous, current in ((before, after), (after, path_after)):
            try:
                _validate_log_stat_transition(previous, current, observed_size)
            except CarryForwardError:
                if not _log_timestamp_transition_only(
                    previous, current, observed_size
                ):
                    raise
                transient_size = current.st_size
        if transient_size is not None:
            _resolve_log_append_metadata_race(
                descriptor,
                path,
                before,
                observed_size,
                transient_size,
                max(before.st_size, after.st_size, path_after.st_size),
                first,
            )
    except OSError as error:
        raise CarryForwardError("log_read_failed") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return first, before


def _read_utf8_log_snapshot(
    path: Path,
    expected: os.stat_result,
    offset: int = 0,
    prefix_sha256: str | None = None,
) -> tuple[bytes, os.stat_result, str]:
    for attempt in range(3):
        content, stable = _read_stable_log(path, expected)
        prefix = content[:offset]
        if len(prefix) != offset or (
            prefix_sha256 is not None
            and hashlib.sha256(prefix).hexdigest() != prefix_sha256
        ):
            raise CarryForwardError("log_prefix_changed")
        appended = content[offset:]
        try:
            return content, stable, appended.decode("utf-8")
        except UnicodeDecodeError as error:
            partial_tail = (
                error.end == len(appended)
                and error.reason == "unexpected end of data"
            )
            if not partial_tail:
                raise CarryForwardError("log_append_invalid") from error
            if attempt == 2:
                raise CarryForwardError("log_append_partial_utf8") from error
            time.sleep(0)
    raise CarryForwardError("log_append_partial_utf8")


def _http_result(
    url: str, *, method: str = "GET", headers: dict[str, str] | None = None
) -> dict[str, Any]:
    class NoRedirect(urllib_request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    request = urllib_request.Request(url, method=method, headers=headers or {})
    try:
        with urllib_request.build_opener(
            urllib_request.ProxyHandler({}), NoRedirect()
        ).open(request, timeout=10) as response:
            status, raw, response_headers = (
                response.status,
                response.read(64 * 1024),
                response.headers,
            )
    except urllib_error.HTTPError as error:
        status, raw, response_headers = error.code, error.read(64 * 1024), error.headers
    try:
        body = json.loads(raw) if raw else None
    except json.JSONDecodeError:
        body = None
    detail = body.get("detail") if isinstance(body, dict) else None
    code = detail.get("code") if isinstance(detail, dict) else None
    cookies = response_headers.get_all("Set-Cookie", [])
    csrf = next(
        (item for item in cookies if item.startswith("dlr_account_csrf=")), None
    )
    return {
        "status": status,
        "code": code,
        "body_status": body.get("status") if isinstance(body, dict) else None,
        "csrf_cookie": csrf is not None,
        "csrf_cookie_path": bool(
            csrf and re.search(r"(?:^|;)\s*Path=/\s*(?:;|$)", csrf, re.IGNORECASE)
        ),
        "csrf_cookie_samesite_lax": bool(
            csrf and re.search(r"(?:^|;)\s*SameSite=lax\s*(?:;|$)", csrf, re.IGNORECASE)
        ),
        "csrf_cookie_httponly": bool(
            csrf and re.search(r"(?:^|;)\s*HttpOnly\s*(?:;|$)", csrf, re.IGNORECASE)
        ),
        "redirect": 300 <= status < 400,
    }


def _entry_probe(request: Any) -> dict[str, Any]:
    value = _closed_request(request, "entry-probe", {"profile", "env_file"})
    profile = validate_group2_account_entry(value["profile"])
    env = _read_explicit_env(Path(value["env_file"]))
    token = env.get("DLR_ADMIN_TOKEN")
    if not token:
        raise CarryForwardError("admin_token_missing")
    account_binding = profile["ports"]["account"]
    account_port = account_binding["published"]
    token_ports = profile["ports"]["token"]
    if not isinstance(token_ports, list) or len(token_ports) != 1:
        raise CarryForwardError("account_binding_invalid")
    token_port = token_ports[0]["published"]
    account_host = account_binding["host_ip"]
    token_host = token_ports[0]["host_ip"]

    def authority(host: str, port: str) -> str:
        rendered = f"[{host}]" if ":" in host else host
        return f"http://{rendered}:{port}"

    account = authority(account_host, account_port)
    token_url = authority(token_host, token_port)
    probes = {
        "account_csrf": _http_result(account + "/api/auth/account/csrf"),
        "token_csrf": _http_result(token_url + "/api/auth/account/csrf"),
        "account_admin": _http_result(
            account + "/api/auth/admin/verify",
            headers={"Authorization": f"Bearer {token}"},
        ),
        "token_admin": _http_result(
            token_url + "/api/auth/admin/verify",
            headers={"Authorization": f"Bearer {token}"},
        ),
        "token_admin_missing": _http_result(token_url + "/api/auth/admin/verify"),
        "token_admin_invalid": _http_result(
            token_url + "/api/auth/admin/verify",
            headers={"Authorization": "Bearer group2-invalid-probe"},
        ),
        "account_me": _http_result(account + "/api/auth/account/me"),
        "account_me_bearer": _http_result(
            account + "/api/auth/account/me",
            headers={"Authorization": f"Bearer {token}"},
        ),
        "account_write_no_csrf": _http_result(
            account + "/api/auth/account/logout", method="POST"
        ),
        "account_write_bad_csrf": _http_result(
            account + "/api/auth/account/logout",
            method="POST",
            headers={"Cookie": "dlr_account_csrf=left", "X-CSRF-Token": "right"},
        ),
        "token_spoof_header": _http_result(
            token_url + "/api/auth/account/csrf",
            headers={"X-DLR-Entry-Mode": "account"},
        ),
        "token_internal_prefix": _http_result(
            token_url + "/__dlr_account/api/auth/account/csrf"
        ),
        "account_internal_prefix": _http_result(
            account + "/__dlr_account/api/auth/admin/verify",
            headers={"Authorization": f"Bearer {token}"},
        ),
    }
    return validate_group2_entry_probe(
        profile,
        {
            "profile_digest": profile["profile_digest"],
            "probes": probes,
            "passed": True,
        },
    )


def validate_group2_entry_probe(profile: Any, result: Any) -> dict[str, Any]:
    profile = validate_group2_account_entry(profile)
    expected_status_code = {
        "token_csrf": (401, "account_entry_required"),
        "account_admin": (401, "token_entry_required"),
        "token_admin_missing": (401, "unauthorized"),
        "token_admin_invalid": (401, "unauthorized"),
        "account_me": (401, "account_session_required"),
        "account_me_bearer": (401, "account_session_required"),
        "account_write_no_csrf": (403, "account_csrf_invalid"),
        "account_write_bad_csrf": (403, "account_csrf_invalid"),
        "token_spoof_header": (401, "account_entry_required"),
    }
    probe_names = {
        "account_csrf",
        "token_csrf",
        "account_admin",
        "token_admin",
        "token_admin_missing",
        "token_admin_invalid",
        "account_me",
        "account_me_bearer",
        "account_write_no_csrf",
        "account_write_bad_csrf",
        "token_spoof_header",
        "token_internal_prefix",
        "account_internal_prefix",
    }
    if (
        not isinstance(result, dict)
        or set(result) != {"profile_digest", "probes", "passed"}
        or result["profile_digest"] != profile["profile_digest"]
        or result["passed"] is not True
        or not isinstance(result["probes"], dict)
        or set(result["probes"]) != probe_names
    ):
        raise CarryForwardError("entry_boundary_invalid")
    probes = result["probes"]
    for item in probes.values():
        _validate_http_result(item, "entry_boundary_invalid")
    account_csrf = probes["account_csrf"]
    if (
        account_csrf["status"] != 200
        or account_csrf["body_status"] != "ok"
        or not account_csrf["csrf_cookie"]
        or not account_csrf["csrf_cookie_path"]
        or not account_csrf["csrf_cookie_samesite_lax"]
        or account_csrf["csrf_cookie_httponly"]
        or probes["token_admin"]["status"] != 200
        or probes["token_admin"]["body_status"] != "ok"
        or any(
            (probes[name]["status"], probes[name]["code"]) != expected
            for name, expected in expected_status_code.items()
        )
        or any(item["redirect"] for item in probes.values())
        or probes["token_internal_prefix"]["status"] == 200
        and probes["token_internal_prefix"]["body_status"] == "ok"
        or probes["account_internal_prefix"]["status"] == 200
        and probes["account_internal_prefix"]["body_status"] == "ok"
    ):
        raise CarryForwardError("entry_boundary_invalid")
    return result


def _validate_group2_worker_lifetime(started: Any, current: Any) -> None:
    fields = ("container_id", "image_id", "started_at", "restart_count")
    if (
        not isinstance(started, dict)
        or not isinstance(current, dict)
        or any(started.get(key) != current.get(key) for key in fields)
    ):
        raise CarryForwardError("group2_worker_lifetime_changed")


def _appended_log(logs: Any, suffix: str) -> dict[str, Any]:
    if not isinstance(logs, dict) or not isinstance(logs.get("files"), list):
        raise CarryForwardError("log_evidence_invalid")
    matches = [item for item in logs["files"] if item.get("path", "").endswith(suffix)]
    if len(matches) != 1 or not isinstance(matches[0].get("appended_text"), str):
        raise CarryForwardError("log_evidence_invalid")
    return matches[0]


def _appended_text(logs: Any, suffix: str) -> str:
    return _appended_log(logs, suffix)["appended_text"]


def _validate_log_clock(value: Any, precise_ns: Any) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or set(value) != {"kind", "lower_bound_ns", "resolution_ns"}
        or value["kind"] not in {"linux-realtime-coarse", "precise-realtime"}
        or not isinstance(value["lower_bound_ns"], int)
        or isinstance(value["lower_bound_ns"], bool)
        or value["lower_bound_ns"] < 0
        or not isinstance(value["resolution_ns"], int)
        or isinstance(value["resolution_ns"], bool)
        or value["resolution_ns"] <= 0
        or not isinstance(precise_ns, int)
        or isinstance(precise_ns, bool)
        or precise_ns < 0
        or value["lower_bound_ns"] > precise_ns
        or value["kind"] == "precise-realtime"
        and value["lower_bound_ns"] != precise_ns
    ):
        raise CarryForwardError("log_clock_invalid")
    return value


def _validate_log_link(
    before: Any, after: Any, profile_digest: str | None = None
) -> tuple[int, int]:
    capture_keys = {
        "profile_digest",
        "files",
        "roots",
        "clock",
        "observed_at_ns",
        "evidence_digest",
    }
    append_keys = {
        "profile_digest",
        "files",
        "roots",
        "clock",
        "baseline_evidence_digest",
        "observed_after_ns",
        "evidence_digest",
    }
    if (
        not isinstance(before, dict)
        or set(before) not in (capture_keys, append_keys)
        or not isinstance(after, dict)
        or set(after) != append_keys
        or before["evidence_digest"]
        != digest(
            {key: item for key, item in before.items() if key != "evidence_digest"}
        )
        or after["evidence_digest"]
        != digest(
            {key: item for key, item in after.items() if key != "evidence_digest"}
        )
        or after["baseline_evidence_digest"] != before["evidence_digest"]
        or after["profile_digest"] != before["profile_digest"]
        or profile_digest is not None
        and before["profile_digest"] != profile_digest
    ):
        raise CarryForwardError("log_evidence_link_invalid")
    start = before.get("observed_after_ns", before.get("observed_at_ns"))
    end = after["observed_after_ns"]
    if (
        not isinstance(start, int)
        or isinstance(start, bool)
        or not isinstance(end, int)
        or isinstance(end, bool)
        or end < start
    ):
        raise CarryForwardError("log_evidence_link_invalid")
    before_clock = _validate_log_clock(before["clock"], start)
    after_clock = _validate_log_clock(after["clock"], end)
    if (
        after_clock["kind"] != before_clock["kind"]
        or after_clock["resolution_ns"] != before_clock["resolution_ns"]
        or after_clock["lower_bound_ns"] < before_clock["lower_bound_ns"]
    ):
        raise CarryForwardError("log_evidence_link_invalid")
    _validate_log_segment_transition(before, after, start, end)
    return start, end


def _validate_log_identity(value: Any) -> None:
    if any(
        not isinstance(value.get(key), int)
        or isinstance(value.get(key), bool)
        or value[key] < 0
        for key in ("device", "inode", "mode", "uid", "gid")
    ) or value["mode"] > 0o7777:
        raise CarryForwardError("log_evidence_invalid")


def _validate_absolute_log_path(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value.startswith("/")
        or value.startswith("//")
        or "\x00" in value
        or os.path.normpath(value) != value
    ):
        raise CarryForwardError("log_evidence_invalid")
    return value


def _log_endpoint_files(value: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict) or not isinstance(value.get("files"), list):
        raise CarryForwardError("log_evidence_invalid")
    endpoint = {}
    is_append = "baseline_evidence_digest" in value
    empty_digest = hashlib.sha256(b"").hexdigest()
    for item in value["files"]:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise CarryForwardError("log_evidence_invalid")
        path = _validate_absolute_log_path(item["path"])
        if path in endpoint or not isinstance(item.get("exists"), bool):
            raise CarryForwardError("log_evidence_invalid")
        if not is_append:
            required = (
                {
                    "path",
                    "exists",
                    "device",
                    "inode",
                    "mode",
                    "uid",
                    "gid",
                    "size",
                    "prefix_sha256",
                }
                if item["exists"]
                else {"path", "exists"}
            )
            if set(item) != required:
                raise CarryForwardError("log_evidence_invalid")
            if item["exists"]:
                _digest_text(item["prefix_sha256"], "log_evidence_invalid")
                _validate_log_identity(item)
                if (
                    not isinstance(item["size"], int)
                    or isinstance(item["size"], bool)
                    or item["size"] < 0
                ):
                    raise CarryForwardError("log_evidence_invalid")
            endpoint[path] = dict(item)
            continue
        required = (
            {
                "path",
                "exists",
                "device",
                "inode",
                "mode",
                "uid",
                "gid",
                "size",
                "prefix_sha256",
                "appended_text",
                "end_size",
                "end_sha256",
            }
            if item["exists"]
            else {"path", "exists", "appended_text", "end_size", "end_sha256"}
        )
        if set(item) != required or not isinstance(item["appended_text"], str):
            raise CarryForwardError("log_evidence_invalid")
        if not item["exists"]:
            if (item["appended_text"], item["end_size"], item["end_sha256"]) != (
                "",
                0,
                empty_digest,
            ):
                raise CarryForwardError("log_evidence_invalid")
            endpoint[path] = {"path": path, "exists": False}
            continue
        _digest_text(item["prefix_sha256"], "log_evidence_invalid")
        _digest_text(item["end_sha256"], "log_evidence_invalid")
        _validate_log_identity(item)
        appended = item["appended_text"].encode("utf-8")
        if (
            not isinstance(item["size"], int)
            or isinstance(item["size"], bool)
            or item["size"] < 0
            or item["end_size"] != item["size"] + len(appended)
            or not appended
            and item["end_sha256"] != item["prefix_sha256"]
            or item["size"] == 0
            and item["prefix_sha256"] == empty_digest
            and item["end_sha256"] != hashlib.sha256(appended).hexdigest()
        ):
            raise CarryForwardError("log_evidence_invalid")
        endpoint[path] = {
            key: item[key]
            for key in ("path", "exists", "device", "inode", "mode", "uid", "gid")
        } | {"size": item["end_size"], "prefix_sha256": item["end_sha256"]}
    return endpoint


def _validate_log_root_transition(
    previous: Any, current: Any, previous_clock_ns: int, end_ns: int
) -> None:
    if not isinstance(previous, list) or not isinstance(current, list):
        raise CarryForwardError("log_evidence_invalid")
    old_roots = {item.get("path"): item for item in previous if isinstance(item, dict)}
    new_roots = {item.get("path"): item for item in current if isinstance(item, dict)}
    if (
        len(old_roots) != len(previous)
        or len(new_roots) != len(current)
        or set(old_roots) != set(new_roots)
    ):
        raise CarryForwardError("log_evidence_invalid")
    root_keys = {
        "path",
        "device",
        "inode",
        "mode",
        "uid",
        "gid",
        "mtime_ns",
        "entries",
        "allowed_new_files",
    }
    for path, old in old_roots.items():
        new = new_roots[path]
        if (
            set(old) != root_keys
            or set(new) != root_keys
            or any(
                not isinstance(item["mtime_ns"], int)
                or isinstance(item["mtime_ns"], bool)
                or item["mtime_ns"] < 0
                for item in (old, new)
            )
            or not isinstance(old["allowed_new_files"], list)
            or old["allowed_new_files"] != sorted(set(old["allowed_new_files"]))
            or any(
                not isinstance(name, str)
                or not name
                or "/" in name
                or "\x00" in name
                or name in {".", ".."}
                for name in old["allowed_new_files"]
            )
            or not isinstance(old["entries"], list)
            or not isinstance(new["entries"], list)
        ):
            raise CarryForwardError("log_evidence_invalid")
        _validate_log_identity(old)
        _validate_log_identity(new)
        if (
            any(
                old[key] != new[key]
                for key in ("path", "device", "inode", "mode", "uid", "gid")
            )
            or old["allowed_new_files"] != new["allowed_new_files"]
        ):
            raise CarryForwardError("log_evidence_link_invalid")
        old_entries = {
            item.get("path"): item for item in old["entries"] if isinstance(item, dict)
        }
        new_entries = {
            item.get("path"): item for item in new["entries"] if isinstance(item, dict)
        }
        for item in (*old["entries"], *new["entries"]):
            if not isinstance(item, dict):
                raise CarryForwardError("log_evidence_invalid")
            expected = (
                {
                    "path",
                    "device",
                    "inode",
                    "mode",
                    "uid",
                    "gid",
                    "type",
                    "mtime_ns",
                }
                if item.get("type") == "directory"
                else {"path", "device", "inode", "mode", "uid", "gid", "type"}
            )
            if (
                set(item) != expected
                or item.get("type") not in {"directory", "file"}
                or not isinstance(item.get("path"), str)
                or item["path"].startswith("/")
                or "\x00" in item["path"]
                or any(part in {"", ".", ".."} for part in item["path"].split("/"))
            ):
                raise CarryForwardError("log_evidence_invalid")
            _validate_log_identity(item)
            if item["type"] == "directory" and (
                not isinstance(item["mtime_ns"], int)
                or isinstance(item["mtime_ns"], bool)
                or item["mtime_ns"] < 0
            ):
                raise CarryForwardError("log_evidence_invalid")
        if (
            len(old_entries) != len(old["entries"])
            or len(new_entries) != len(new["entries"])
            or any(new_entries.get(name) != item for name, item in old_entries.items())
        ):
            raise CarryForwardError("log_evidence_link_invalid")
        additions = set(new_entries) - set(old_entries)
        if additions - set(old["allowed_new_files"]) or any(
            "/" in name or new_entries[name].get("type") != "file" for name in additions
        ):
            raise CarryForwardError("log_evidence_link_invalid")
        if additions:
            if new["mtime_ns"] < old["mtime_ns"]:
                raise CarryForwardError("log_evidence_link_invalid")
            _window_ns(
                new["mtime_ns"],
                max(old["mtime_ns"], previous_clock_ns),
                end_ns,
                "log_evidence_link_invalid",
            )
        elif new["mtime_ns"] != old["mtime_ns"]:
            raise CarryForwardError("log_evidence_link_invalid")


def _validate_log_endpoint_cross_view(
    value: Any, files: dict[str, dict[str, Any]]
) -> None:
    roots = value.get("roots")
    if not isinstance(roots, list):
        raise CarryForwardError("log_evidence_invalid")
    if not roots:
        raise CarryForwardError("log_evidence_invalid")
    root_files: dict[str, dict[str, Any]] = {}
    root_entries: dict[str, dict[str, Any]] = {}
    root_paths = []
    for root in roots:
        root_path = Path(_validate_absolute_log_path(root["path"]))
        root_paths.append(root_path)
        for entry in root["entries"]:
            absolute = str(root_path / entry["path"])
            if absolute in root_entries:
                raise CarryForwardError("log_evidence_invalid")
            root_entries[absolute] = entry
            if entry["type"] == "file":
                root_files[absolute] = entry

        entries = {entry["path"]: entry for entry in root["entries"]}
        for relative in entries:
            parts = relative.split("/")
            for length in range(1, len(parts)):
                parent = "/".join(parts[:length])
                if parent not in entries or entries[parent]["type"] != "directory":
                    raise CarryForwardError("log_evidence_invalid")

    for index, root_path in enumerate(root_paths):
        for other in root_paths[index + 1 :]:
            try:
                root_path.relative_to(other)
            except ValueError:
                try:
                    other.relative_to(root_path)
                except ValueError:
                    continue
            raise CarryForwardError("log_evidence_invalid")

    for path, item in files.items():
        file_path = Path(path)
        containing = []
        for root_path in root_paths:
            try:
                relative = file_path.relative_to(root_path)
            except ValueError:
                continue
            if relative == Path("."):
                raise CarryForwardError("log_evidence_invalid")
            containing.append(root_path)
        if len(containing) != 1:
            raise CarryForwardError("log_evidence_invalid")
        relative_parts = file_path.relative_to(containing[0]).parts
        for length in range(1, len(relative_parts)):
            parent = str(containing[0].joinpath(*relative_parts[:length]))
            if parent in root_entries and root_entries[parent]["type"] != "directory":
                raise CarryForwardError("log_evidence_invalid")
        if not item["exists"] and path in root_entries:
            raise CarryForwardError("log_evidence_link_invalid")

    existing = {path for path, item in files.items() if item["exists"]}
    if set(root_files) != existing:
        raise CarryForwardError("log_evidence_link_invalid")
    for path, entry in root_files.items():
        item = files[path]
        if any(
            entry[key] != item[key]
            for key in ("device", "inode", "mode", "uid", "gid")
        ):
            raise CarryForwardError("log_evidence_link_invalid")


def _validate_log_endpoint(
    value: Any, profile: dict[str, Any] | None = None
) -> dict[str, dict[str, Any]]:
    files = _log_endpoint_files(value)
    observed_ns = value.get("observed_after_ns", value.get("observed_at_ns"))
    clock = _validate_log_clock(value.get("clock"), observed_ns)
    roots = value.get("roots")
    _validate_log_root_transition(
        roots, roots, clock["lower_bound_ns"], observed_ns
    )
    _validate_log_endpoint_cross_view(value, files)
    if profile is not None:
        declarations = {
            item["path"]: item["allowed_new_files"] for item in profile["log_roots"]
        }
        observed = {
            item["path"]: item["allowed_new_files"] for item in roots
        }
        if observed != declarations or not set(profile["log_files"]).issubset(files):
            raise CarryForwardError("log_evidence_invalid")
    return files


def _validate_log_segment_transition(
    before: Any, after: Any, start_ns: int, end_ns: int
) -> None:
    previous_files = _validate_log_endpoint(before)
    current_files = _validate_log_endpoint(after)
    if set(previous_files) != set(current_files):
        raise CarryForwardError("log_evidence_link_invalid")
    current_items = {item["path"]: item for item in after["files"]}
    empty_digest = hashlib.sha256(b"").hexdigest()
    for path, previous in previous_files.items():
        current = current_items[path]
        if previous["exists"]:
            if (
                not current["exists"]
                or any(
                    current[key] != previous[key]
                    for key in ("device", "inode", "mode", "uid", "gid")
                )
                or (current["size"], current["prefix_sha256"])
                != (previous["size"], previous["prefix_sha256"])
            ):
                raise CarryForwardError("log_evidence_link_invalid")
        elif current["exists"] and (
            current["size"], current["prefix_sha256"]
        ) != (0, empty_digest):
            raise CarryForwardError("log_evidence_link_invalid")
    _validate_log_root_transition(
        before.get("roots"),
        after.get("roots"),
        before["clock"]["lower_bound_ns"],
        end_ns,
    )


def combine_group2_log_window(before: Any, *segments: Any) -> dict[str, Any]:
    if not segments:
        raise CarryForwardError("log_evidence_link_invalid")
    initial = _log_endpoint_files(before)
    previous = before
    accumulated = {path: "" for path in initial}
    for segment in segments:
        _validate_log_link(previous, segment)
        segment_items = {item["path"]: item for item in segment["files"]}
        for path in initial:
            item = segment_items[path]
            accumulated[path] += item["appended_text"]
        previous = segment
    final = _log_endpoint_files(previous)
    output = []
    empty_digest = hashlib.sha256(b"").hexdigest()
    for path in sorted(initial):
        first, last = initial[path], final[path]
        text_value = accumulated[path]
        if not first["exists"] and not last["exists"]:
            output.append(
                {
                    "path": path,
                    "exists": False,
                    "appended_text": "",
                    "end_size": 0,
                    "end_sha256": empty_digest,
                }
            )
            continue
        base = (
            first
            if first["exists"]
            else {
                key: last[key]
                for key in ("path", "exists", "device", "inode", "mode", "uid", "gid")
            }
            | {"size": 0, "prefix_sha256": empty_digest}
        )
        output.append(
            {
                **base,
                "appended_text": text_value,
                "end_size": last["size"],
                "end_sha256": last["prefix_sha256"],
            }
        )
    result = {
        "profile_digest": before["profile_digest"],
        "files": output,
        "roots": previous["roots"],
        "clock": previous["clock"],
        "baseline_evidence_digest": before["evidence_digest"],
        "observed_after_ns": previous["observed_after_ns"],
    }
    result["evidence_digest"] = digest(result)
    _validate_log_link(before, result)
    return result


def _startup_proof(request: Any) -> dict[str, Any]:
    value = _closed_request(
        request,
        "startup-proof",
        {
            "profile",
            "logs_before",
            "logs_after",
            "container_before",
            "container_after",
            "window_start_ns",
            "window_end_ns",
        },
    )
    profile = validate_group2_account_entry(value["profile"])
    return _worker_startup_proof(
        value,
        profile,
        profile["candidate_image_ids_by_service"]["worker"],
        profile["candidate_profiles"]["worker"],
    )


def _worker_startup_proof(
    value: dict[str, Any],
    profile: dict[str, Any],
    expected_image: str,
    expected_profile: dict[str, Any],
    *,
    require_same_container: bool = False,
) -> dict[str, Any]:
    """Validate the Worker-specific startup facts shared by deploy and incident restore."""
    logs_before = value["logs_before"]
    logs_after = value["logs_after"]
    try:
        log_start_ns, log_end_ns = _validate_log_link(
            logs_before, logs_after, profile["profile_digest"]
        )
    except CarryForwardError as error:
        raise CarryForwardError("startup_log_evidence_invalid") from error
    text_value = _appended_text(value["logs_after"], "/worker/worker.log")
    marker = "sandbox preflight receipt: "
    receipts = []
    for line in text_value.splitlines():
        if marker not in line:
            continue
        try:
            receipt = json.loads(line.split(marker, 1)[1])
        except json.JSONDecodeError as error:
            raise CarryForwardError("startup_proof_invalid") from error
        receipts.append(receipt)
    if (
        len(receipts) != 1
        or text_value.count("sandbox preflight passed; rabbitmq execution gate=True")
        != 1
    ):
        raise CarryForwardError("startup_proof_invalid")
    receipt = receipts[0]
    match = re.fullmatch(
        r"dlr-preflight-([0-9a-f]{16,64})", str(receipt.get("cgroup_name"))
    )
    after = value["container_after"]
    before = value["container_before"]
    window_start = value["window_start_ns"]
    window_end = value["window_end_ns"]
    if (
        match is None
        or not isinstance(after, dict)
        or not isinstance(after.get("image_id"), str)
        or after.get("restart_count") != 0
        or not isinstance(before, dict)
        or (
            before.get("container_id") != after.get("container_id")
            if require_same_container
            else before.get("container_id") == after.get("container_id")
        )
        or after.get("image_id") != expected_image
        or after.get("command") != profile["old_containers"]["worker"].get("command")
        or after.get("status") != "running"
        or after.get("health") != "healthy"
        or not _container_matches_profile(
            after, expected_profile, profile["project"], "worker"
        )
        or not isinstance(window_start, int)
        or isinstance(window_start, bool)
        or not isinstance(window_end, int)
        or isinstance(window_end, bool)
        or window_end < window_start
        or log_start_ns > window_start
        or log_end_ns < window_end
        or not window_start <= _rfc3339_ns(after.get("started_at")) <= window_end
    ):
        raise CarryForwardError("startup_proof_invalid")
    proof = {
        "container_id": after["container_id"],
        "image_id": after["image_id"],
        "started_at": after["started_at"],
        "restart_count": after["restart_count"],
        "nonce": match.group(1),
        "window_start_ns": value["window_start_ns"],
        "window_end_ns": value["window_end_ns"],
        "preflight_receipt": receipt,
        "log_evidence_digest": value["logs_after"]["evidence_digest"],
    }
    return _validate_startup_proof(proof)


def _same_container_worker_startup_proof(
    value: dict[str, Any],
    profile: dict[str, Any],
    expected_image: str,
    expected_profile: dict[str, Any],
    prior_nonces: Any,
) -> dict[str, Any]:
    """Validate this reboot's stopped-to-running transition without weakening old callers."""
    before = value.get("container_before")
    after = value.get("container_after")
    if (
        not isinstance(before, dict)
        or not isinstance(after, dict)
        or before.get("status") == "running"
        or before.get("health") not in {None, "unhealthy"}
        or before.get("started_at") == after.get("started_at")
        or not isinstance(prior_nonces, list)
        or len(prior_nonces) != len(set(prior_nonces))
        or any(
            not isinstance(item, str)
            or re.fullmatch(r"[0-9a-f]{16,64}", item) is None
            for item in prior_nonces
        )
    ):
        raise CarryForwardError("group2_reboot_startup_invalid")
    try:
        proof = _worker_startup_proof(
            value,
            profile,
            expected_image,
            expected_profile,
            require_same_container=True,
        )
    except CarryForwardError as error:
        raise CarryForwardError("group2_reboot_startup_invalid") from error
    if proof["nonce"] in prior_nonces:
        raise CarryForwardError("group2_reboot_startup_invalid")
    return proof


_ACCESS = re.compile(
    r"(?P<client>\[[0-9A-Fa-f:]+\]|[0-9A-Fa-f:.]+):[0-9]+\s+-\s+"
    r'"(?P<method>GET|POST|PATCH|DELETE) (?P<path>/api/[^ ]+) HTTP/[0-9.]+" '
    r"(?P<status>[0-9]{3})"
)


def _access_client(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    try:
        return ipaddress.ip_address(value.strip("[]"))
    except ValueError as error:
        raise CarryForwardError("probe_log_unparseable") from error


def derive_group2_probe_provenance(request: Any) -> dict[str, Any]:
    value = _closed_request(
        request,
        "probe-proof",
        {"logs_before", "logs_after", "probe_result", "before_db", "cleanup"},
    )
    window_start, window_end = _validate_log_link(
        value["logs_before"], value["logs_after"]
    )
    log_item = _appended_log(value["logs_after"], "/control/control.log")
    text_value = log_item["appended_text"]
    events = []
    byte_offset = log_item.get("size", 0)
    if (
        not isinstance(byte_offset, int)
        or isinstance(byte_offset, bool)
        or byte_offset < 0
    ):
        raise CarryForwardError("log_evidence_invalid")
    for raw_line in text_value.splitlines(keepends=True):
        line = raw_line.rstrip("\r\n")
        offset = byte_offset
        byte_offset += len(raw_line.encode("utf-8"))
        match = _ACCESS.search(line)
        if match:
            events.append(
                (
                    offset,
                    _access_client(match.group("client")),
                    match.group("method"),
                    match.group("path"),
                    int(match.group("status")),
                    line,
                )
            )
        elif " /api/" in line and "HTTP/" in line:
            raise CarryForwardError("probe_log_unparseable")
    probe_result = value["probe_result"]
    if (
        not isinstance(probe_result, dict)
        or set(probe_result) != {"execution_id", "status", "workspace_cleanup_status"}
        or probe_result["status"] != "succeeded"
        or probe_result["workspace_cleanup_status"] != "completed"
    ):
        raise CarryForwardError("probe_result_invalid")
    execution_id = _positive(probe_result["execution_id"], "probe_result_invalid")
    adapter_creates = [
        (o, p)
        for o, client, m, p, s, _ in events
        if m == "POST" and p == "/api/adapters" and s == 201
    ]
    if len(adapter_creates) != 1:
        raise CarryForwardError("probe_provenance_invalid")
    candidates = []
    for _, client, method, path, status, _ in events:
        match = re.fullmatch(r"/api/adapters/([1-9][0-9]*)/executions", path)
        if method == "POST" and match and status == 202:
            candidates.append(int(match.group(1)))
    if len(candidates) != 1:
        raise CarryForwardError("probe_provenance_invalid")
    adapter_id = candidates[0]
    starts = []
    results = []
    for _, _, method, path, status, _ in events:
        match = re.fullmatch(
            r"/api/workers/([1-9][0-9]*)/attempts/([1-9][0-9]*)/(start|result)", path
        )
        if method == "POST" and match and 200 <= status < 300:
            target = starts if match.group(3) == "start" else results
            target.append((int(match.group(1)), int(match.group(2))))
    if len(starts) != 1 or results != starts:
        raise CarryForwardError("probe_provenance_invalid")
    worker_id, attempt_id = starts[0]
    required_paths = {
        ("POST", f"/api/adapters/{adapter_id}/versions"),
        ("PATCH", f"/api/adapters/{adapter_id}"),
        ("POST", f"/api/adapters/{adapter_id}/executions"),
        ("DELETE", f"/api/adapters/{adapter_id}"),
        ("POST", f"/api/workers/{worker_id}/attempts/{attempt_id}/start"),
        ("POST", f"/api/workers/{worker_id}/attempts/{attempt_id}/result"),
        ("POST", f"/api/workers/executions/{execution_id}/workspace-cleanup"),
    }
    observed = {
        (method, path)
        for _, _, method, path, status, _ in events
        if 200 <= status < 300
    }
    if not required_paths.issubset(observed):
        raise CarryForwardError("probe_provenance_invalid")

    def offsets(method: str, path: str, status: int | None = None) -> list[int]:
        return [
            offset
            for offset, _, event_method, event_path, event_status, _ in events
            if event_method == method
            and event_path == path
            and (status is None or event_status == status)
        ]

    prefix_sequence = [
        offsets("POST", "/api/adapters", 201),
        offsets("POST", f"/api/adapters/{adapter_id}/versions"),
        offsets("PATCH", f"/api/adapters/{adapter_id}"),
        offsets("POST", f"/api/adapters/{adapter_id}/executions", 202),
        offsets("POST", f"/api/workers/{worker_id}/attempts/{attempt_id}/start"),
        offsets("POST", f"/api/workers/{worker_id}/attempts/{attempt_id}/result"),
        offsets("POST", f"/api/workers/executions/{execution_id}/workspace-cleanup"),
    ]
    deletes = offsets("DELETE", f"/api/adapters/{adapter_id}", 204)
    if any(len(item) != 1 for item in prefix_sequence) or len(deletes) != 1:
        raise CarryForwardError("probe_provenance_invalid")
    final_gets = [
        offset
        for offset in offsets("GET", f"/api/executions/{execution_id}", 200)
        if prefix_sequence[-1][0] < offset < deletes[0]
    ]
    if not final_gets:
        raise CarryForwardError("probe_provenance_invalid")
    ordered = [item[0] for item in prefix_sequence] + [final_gets[-1], deletes[0]]
    if ordered != sorted(ordered):
        raise CarryForwardError("probe_event_order_invalid")
    nonempty_claims = offsets("POST", f"/api/workers/{worker_id}/v3/claim", 200)
    empty_claims = offsets("POST", f"/api/workers/{worker_id}/v3/claim", 204)
    if len(nonempty_claims) != 1 or nonempty_claims[0] > prefix_sequence[4][0]:
        raise CarryForwardError("probe_claim_chain_invalid")
    cleanup_claims = offsets("POST", f"/api/workers/{worker_id}/cleanups/claim", 200)
    empty_cleanup_claims = offsets(
        "POST", f"/api/workers/{worker_id}/cleanups/claim", 204
    )
    cleanup_results = []
    for offset, _, method, path, status, _ in events:
        match = re.fullmatch(
            rf"/api/workers/{worker_id}/cleanups/([1-9][0-9]*)/result", path
        )
        if method == "POST" and match:
            cleanup_results.append((offset, int(match.group(1)), status))
    if len(cleanup_claims) > 1 or len(cleanup_results) > 1:
        raise CarryForwardError("probe_cleanup_log_invalid")
    if cleanup_results and (
        len(cleanup_claims) != 1
        or cleanup_results[0][2] != 204
        or not ordered[-1] < cleanup_claims[0] < cleanup_results[0][0]
    ):
        raise CarryForwardError("probe_cleanup_log_invalid")
    for _, client, method, path, event_status, _ in events:
        attempt_match = re.fullmatch(
            r"/api/workers/([1-9][0-9]*)/attempts/([1-9][0-9]*)/(start|renew|progress|result)",
            path,
        )
        if attempt_match and (
            int(attempt_match.group(1)) != worker_id
            or int(attempt_match.group(2)) != attempt_id
        ):
            raise CarryForwardError("probe_provenance_invalid")
        business_request = path == "/api/adapters" or path.startswith(
            ("/api/adapters/", "/api/executions/")
        )
        if business_request and not client.is_loopback:
            raise CarryForwardError("probe_business_not_loopback")
        if business_request:
            expected_business = {
                ("POST", "/api/adapters"): 201,
                ("POST", f"/api/adapters/{adapter_id}/versions"): 201,
                ("PATCH", f"/api/adapters/{adapter_id}"): 200,
                ("POST", f"/api/adapters/{adapter_id}/executions"): 202,
                ("DELETE", f"/api/adapters/{adapter_id}"): 204,
                ("GET", f"/api/executions/{execution_id}"): 200,
            }.get((method, path))
            if event_status != expected_business:
                raise CarryForwardError("probe_business_request_unknown")
        if method in {"POST", "PATCH", "DELETE"}:
            allowed_write = (
                (method, path) in required_paths
                or (method == "POST" and path == "/api/adapters")
                or method == "POST"
                and path
                in {
                    f"/api/workers/{worker_id}/heartbeat",
                    f"/api/workers/{worker_id}/v3/claim",
                    f"/api/workers/{worker_id}/cleanups/claim",
                }
                or attempt_match is not None
                and int(attempt_match.group(1)) == worker_id
                and int(attempt_match.group(2)) == attempt_id
                or method == "POST"
                and re.fullmatch(
                    rf"/api/workers/{worker_id}/cleanups/[1-9][0-9]*/result", path
                )
                is not None
            )
            if not allowed_write:
                raise CarryForwardError("probe_business_write_unknown")
            if path == f"/api/workers/{worker_id}/heartbeat":
                valid_status = event_status == 204
            elif path in {
                f"/api/workers/{worker_id}/v3/claim",
                f"/api/workers/{worker_id}/cleanups/claim",
            }:
                valid_status = event_status in {200, 204}
            elif attempt_match is not None:
                valid_status = event_status == 200
            elif re.fullmatch(
                rf"/api/workers/{worker_id}/cleanups/[1-9][0-9]*/result", path
            ):
                valid_status = event_status == 204
            else:
                expected_status = {
                    ("POST", "/api/adapters"): 201,
                    ("POST", f"/api/adapters/{adapter_id}/versions"): 201,
                    ("PATCH", f"/api/adapters/{adapter_id}"): 200,
                    ("POST", f"/api/adapters/{adapter_id}/executions"): 202,
                    ("DELETE", f"/api/adapters/{adapter_id}"): 204,
                    (
                        "POST",
                        f"/api/workers/executions/{execution_id}/workspace-cleanup",
                    ): 200,
                }.get((method, path))
                valid_status = event_status == expected_status
            if not valid_status:
                raise CarryForwardError("probe_critical_request_failed")
    if len(empty_claims) + len(nonempty_claims) != len(
        offsets("POST", f"/api/workers/{worker_id}/v3/claim")
    ) or len(empty_cleanup_claims) + len(cleanup_claims) != len(
        offsets("POST", f"/api/workers/{worker_id}/cleanups/claim")
    ):
        raise CarryForwardError("probe_claim_chain_invalid")
    before = value["before_db"]["protected_rows"]
    if (
        adapter_id in before["adapter_ids"]
        or execution_id in before["execution_ids"]
        or attempt_id in before["attempt_ids"]
    ):
        raise CarryForwardError("probe_identity_not_new")
    cleanup = value["cleanup"]
    if cleanup is not None:
        row = cleanup.get("row") if isinstance(cleanup, dict) else None
        cleanup_id = row.get("id") if isinstance(row, dict) else None
        if (
            not isinstance(cleanup_id, int)
            or isinstance(cleanup_id, bool)
            or cleanup_id <= 0
        ):
            raise CarryForwardError("probe_cleanup_invalid")
        if (
            len(cleanup_claims) != 1
            or len(cleanup_results) != 1
            or cleanup_results[0][1:] != (cleanup_id, 204)
        ):
            raise CarryForwardError("probe_cleanup_log_invalid")
    proof = {
        "adapter_id": adapter_id,
        "execution_id": execution_id,
        "worker_id": worker_id,
        "attempt_id": attempt_id,
        "window_start_ns": window_start,
        "window_end_ns": window_end,
        "probe_result": probe_result,
        "cleanup": cleanup,
        "log_evidence_digest": value["logs_after"]["evidence_digest"],
        "event_refs": [
            {"offset": offset, "line_sha256": hashlib.sha256(line.encode()).hexdigest()}
            for offset, _, _, _, _, line in events
        ],
    }
    return _validate_probe_proof(proof, require_cleanup=cleanup is not None)


def wait_group2_probe_cleanup(request: Any) -> dict[str, Any]:
    value = _closed_request(
        request, "probe-cleanup", {"provenance", "before_db", "timeout_seconds"}
    )
    provenance = _validate_probe_proof(value["provenance"], require_cleanup=False)
    timeout = value["timeout_seconds"]
    if (
        not isinstance(timeout, int)
        or isinstance(timeout, bool)
        or not 1 <= timeout <= 120
    ):
        raise CarryForwardError("probe_cleanup_timeout_invalid")
    try:
        from sqlalchemy import create_engine, inspect, text
    except ImportError as error:
        raise CarryForwardError("sqlalchemy_unavailable") from error
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise CarryForwardError("database_url_missing")
    old_rows = value["before_db"]["protected_rows"]["tables"][
        "worker_cleanup_requests"
    ]["rows"]
    old_pks = {digest(row["pk"]) for row in old_rows}
    deadline = time.monotonic() + timeout
    while True:
        engine = create_engine(url)
        with engine.connect() as connection:
            connection.execute(
                text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            )
            inspector = inspect(connection)
            columns = [
                column["name"]
                for column in inspector.get_columns("worker_cleanup_requests")
            ]
            primary_key = list(
                (inspector.get_pk_constraint("worker_cleanup_requests") or {}).get(
                    "constrained_columns"
                )
                or []
            )
            quoted = ",".join(f'"{column}"' for column in columns)
            order = ",".join(f'"{column}"' for column in primary_key)
            rows = [
                dict(row)
                for row in connection.execute(
                    text(
                        f'SELECT {quoted} FROM "worker_cleanup_requests" ORDER BY {order}'
                    )
                ).mappings()
            ]
        fresh = [
            row
            for row in rows
            if digest({key: canonical(row[key]) for key in primary_key}) not in old_pks
        ]
        matching = [
            row
            for row in fresh
            if row.get("adapter_id") == provenance["adapter_id"]
            and row.get("worker_id") == provenance["worker_id"]
        ]
        if len(fresh) > 1 or len(matching) > 1:
            raise CarryForwardError("probe_cleanup_ambiguous")
        if matching:
            row = matching[0]
            if row.get("status") == "failed" or row.get("error_code") is not None:
                raise CarryForwardError("probe_cleanup_failed")
            if row.get("status") == "completed":
                canonical_row = canonical(row)
                return {"row": canonical_row, "row_sha256": digest(canonical_row)}
        if time.monotonic() >= deadline:
            raise CarryForwardError("probe_cleanup_timeout")
        time.sleep(0.5)


def _validate_group2_manifest_db(manifest: dict[str, Any], value: Any) -> None:
    if not isinstance(value, dict):
        raise CarryForwardError("group2_receipt_evidence_invalid")
    compare_projection(manifest["old_runtime_projection"], value.get("projection"))
    for key in (
        "responsibilities",
        "protected_rows",
        "asset_projection",
        "schema_shape",
        "schema_inventory",
    ):
        if value.get(key) != manifest[key]:
            raise CarryForwardError("group2_receipt_evidence_changed")


def _validate_group2_stage_inputs(
    manifest: dict[str, Any], stage_inputs: Any
) -> list[dict[str, Any]]:
    names = [
        "preflight",
        "control_stopped",
        "stopped",
        "after_backup",
        "after_migration",
    ]
    if not isinstance(stage_inputs, dict) or set(stage_inputs) != set(names):
        raise CarryForwardError("group2_receipt_evidence_invalid")
    logs = []
    previous = manifest["log_evidence"]
    for name in names:
        item = stage_inputs[name]
        if not isinstance(item, dict) or set(item) != {"db", "files", "logs_after"}:
            raise CarryForwardError("group2_receipt_evidence_invalid")
        _validate_group2_manifest_db(manifest, item["db"])
        _validate_capture_digests(item["files"])
        if item["files"] != manifest["file_evidence"]:
            raise CarryForwardError("group2_receipt_evidence_changed")
        _validate_log_link(
            previous,
            item["logs_after"],
            manifest["account_entry"]["profile_digest"],
        )
        previous = item["logs_after"]
        logs.append(previous)
    return logs


def validate_group2_receipt_evidence(manifest: Any, evidence: Any) -> dict[str, Any]:
    manifest = validate_manifest(manifest)
    validate_group2_manifest_extensions(manifest)
    if not isinstance(evidence, dict) or set(evidence) != {
        "stages",
        "stage_inputs",
        "startup",
        "probe",
        "post_health",
    }:
        raise CarryForwardError("group2_receipt_evidence_invalid")
    stage_logs = _validate_group2_stage_inputs(manifest, evidence["stage_inputs"])
    stages = evidence["stages"]
    stage_names = {
        "preflight",
        "control_stopped",
        "stopped",
        "backup",
        "same_schema",
        "started",
        "probe",
        "natural_cleanup",
        "post_preservation",
        "post_health",
    }
    if not isinstance(stages, dict) or set(stages) != stage_names:
        raise CarryForwardError("group2_receipt_evidence_invalid")
    startup = evidence["startup"]
    if not isinstance(startup, dict) or set(startup) != {
        "request",
        "proof",
        "before_files",
        "after_files",
        "after_db",
        "result",
    }:
        raise CarryForwardError("group2_receipt_evidence_invalid")
    request = startup["request"]
    if (
        not isinstance(request, dict)
        or request.get("profile") != manifest["account_entry"]
        or request.get("container_before")
        != manifest["account_entry"]["old_containers"]["worker"]
        or startup["before_files"] != manifest["file_evidence"]
        or request.get("logs_before") != stage_logs[-1]
    ):
        raise CarryForwardError("group2_receipt_evidence_changed")
    proof = _startup_proof(request)
    if proof != startup["proof"]:
        raise CarryForwardError("group2_receipt_evidence_changed")
    _validate_group2_manifest_db(manifest, startup["after_db"])
    startup_result = compare_group2_startup_files(
        startup["before_files"], startup["after_files"], proof
    )
    if startup_result != startup["result"]:
        raise CarryForwardError("group2_receipt_evidence_changed")

    probe = evidence["probe"]
    if not isinstance(probe, dict) or set(probe) != {
        "proof",
        "before_db",
        "after_db",
        "before_files",
        "after_files",
        "logs_before",
        "logs_partial",
        "logs_final",
        "logs_complete",
        "probe_result",
        "cleanup",
        "result",
    }:
        raise CarryForwardError("group2_receipt_evidence_invalid")
    if (
        probe["before_db"] != startup["after_db"]
        or probe["before_files"] != startup["after_files"]
    ):
        raise CarryForwardError("group2_receipt_evidence_changed")
    _validate_log_link(
        request["logs_after"],
        probe["logs_before"],
        manifest["account_entry"]["profile_digest"],
    )
    complete = combine_group2_log_window(
        probe["logs_before"], probe["logs_partial"], probe["logs_final"]
    )
    if complete != probe["logs_complete"]:
        raise CarryForwardError("group2_receipt_evidence_changed")
    partial = derive_group2_probe_provenance(
        {
            "mode": GROUP2_MODE,
            "operation": "probe-proof",
            "logs_before": probe["logs_before"],
            "logs_after": probe["logs_partial"],
            "probe_result": probe["probe_result"],
            "before_db": probe["before_db"],
            "cleanup": None,
        }
    )
    final = derive_group2_probe_provenance(
        {
            "mode": GROUP2_MODE,
            "operation": "probe-proof",
            "logs_before": probe["logs_before"],
            "logs_after": complete,
            "probe_result": probe["probe_result"],
            "before_db": probe["before_db"],
            "cleanup": probe["cleanup"],
        }
    )
    for key in ("adapter_id", "execution_id", "worker_id", "attempt_id"):
        if partial[key] != final[key]:
            raise CarryForwardError("group2_receipt_evidence_changed")
    if final != probe["proof"]:
        raise CarryForwardError("group2_receipt_evidence_changed")
    post_result = compare_group2_post_probe(
        manifest,
        probe["before_db"],
        probe["after_db"],
        probe["before_files"],
        probe["after_files"],
        final,
    )
    if post_result != probe["result"]:
        raise CarryForwardError("group2_receipt_evidence_changed")

    health = evidence["post_health"]
    if not isinstance(health, dict) or set(health) != {
        "account_check",
        "entry_probe",
        "logs_before",
        "logs_after",
    }:
        raise CarryForwardError("group2_receipt_evidence_invalid")
    account_check = validate_group2_account_check(
        manifest["account_entry"], health["account_check"]
    )
    _validate_group2_worker_lifetime(
        request.get("container_after"), account_check.get("containers", {}).get("worker")
    )
    validate_group2_entry_probe(manifest["account_entry"], health["entry_probe"])
    if health["logs_before"] != probe["logs_final"]:
        raise CarryForwardError("group2_receipt_evidence_changed")
    _validate_log_link(
        health["logs_before"],
        health["logs_after"],
        manifest["account_entry"]["profile_digest"],
    )
    expected_stages = {
        "preflight": evidence["stage_inputs"]["preflight"]["db"],
        "control_stopped": evidence["stage_inputs"]["control_stopped"]["db"],
        "stopped": evidence["stage_inputs"]["stopped"]["db"],
        "backup": {
            "db": evidence["stage_inputs"]["after_backup"]["db"],
            "dump_sha256": stages["backup"].get("dump_sha256")
            if isinstance(stages["backup"], dict)
            else None,
            "list_sha256": stages["backup"].get("list_sha256")
            if isinstance(stages["backup"], dict)
            else None,
        },
        "same_schema": evidence["stage_inputs"]["after_migration"]["db"],
        "started": startup_result,
        "probe": probe["probe_result"],
        "natural_cleanup": probe["cleanup"],
        "post_preservation": {
            "result": post_result,
            "db": probe["after_db"],
            "files": probe["after_files"],
        },
        "post_health": {
            "account_check": health["account_check"],
            "entry_probe": health["entry_probe"],
            "logs_after": health["logs_after"],
        },
    }
    for key in ("dump_sha256", "list_sha256"):
        if (
            not isinstance(expected_stages["backup"][key], str)
            or DIGEST.fullmatch(expected_stages["backup"][key]) is None
        ):
            raise CarryForwardError("group2_receipt_evidence_invalid")
    if stages != expected_stages:
        raise CarryForwardError("group2_receipt_evidence_changed")
    return {
        "startup_result": startup_result,
        "post_result": post_result,
        "account_ready": True,
        "entry_ready": True,
        "cleanup_ready": True,
        "log_chain_digest": digest(
            {
                "pre_start": [item["evidence_digest"] for item in stage_logs],
                "startup": request["logs_after"]["evidence_digest"],
                "entry": probe["logs_before"]["evidence_digest"],
                "probe_before": probe["logs_before"]["evidence_digest"],
                "probe_partial": probe["logs_partial"]["evidence_digest"],
                "probe_final": probe["logs_final"]["evidence_digest"],
                "probe_complete": complete["evidence_digest"],
                "health": health["logs_after"]["evidence_digest"],
            }
        ),
    }


def _validate_group2_recovery_lineage(
    deployment: Any, predecessor: Any, fresh: Any
) -> None:
    if (
        not isinstance(deployment, dict)
        or set(deployment) != {"db", "files", "post_preservation", "logs_after"}
        or not isinstance(predecessor, dict)
        or set(predecessor)
        != {
            "kind",
            "recovery_id",
            "evidence_digest",
            "db",
            "files",
            "logs_after",
        }
        or predecessor["kind"] not in {"deployment", "recovery"}
        or not isinstance(fresh, dict)
        or set(fresh) != {"db", "files"}
    ):
        raise CarryForwardError("group2_recovery_lineage_invalid")
    if predecessor["kind"] == "deployment":
        if (
            predecessor["recovery_id"] is not None
            or predecessor["evidence_digest"] is not None
            or predecessor["db"] != deployment["db"]
            or predecessor["files"] != deployment["files"]
            or predecessor["logs_after"] != deployment["logs_after"]
        ):
            raise CarryForwardError("group2_recovery_lineage_changed")
    elif (
        not isinstance(predecessor["recovery_id"], str)
        or MANIFEST_ID.fullmatch(predecessor["recovery_id"]) is None
        or not isinstance(predecessor["evidence_digest"], str)
        or DIGEST.fullmatch(predecessor["evidence_digest"]) is None
    ):
        raise CarryForwardError("group2_recovery_lineage_invalid")
    if predecessor["db"] != fresh["db"] or predecessor["files"] != fresh["files"]:
        raise CarryForwardError("group2_recovery_lineage_changed")
    _validate_capture_digests(deployment["files"])
    _validate_capture_digests(fresh["files"])
    _validate_capture_digests(predecessor["files"])


def validate_group2_recovery_evidence(
    manifest: Any, baseline: Any, evidence: Any, expected_deployment: Any
) -> dict[str, Any]:
    manifest = validate_manifest(manifest)
    validate_group2_manifest_extensions(manifest)
    if (
        not isinstance(baseline, dict)
        or set(baseline) != {"deployment", "predecessor", "fresh"}
        or not isinstance(baseline.get("deployment"), dict)
        or set(baseline["deployment"])
        != {"db", "files", "post_preservation", "logs_after"}
        or baseline["deployment"] != expected_deployment
        or not isinstance(baseline.get("fresh"), dict)
        or set(baseline["fresh"]) != {"db", "files"}
        or not isinstance(evidence, dict)
        or set(evidence)
        != {
            "request",
            "proof",
            "before_db",
            "after_db",
            "before_files",
            "after_files",
            "account_check",
            "entry_probe",
            "logs_before",
            "logs_after",
            "preservation",
        }
    ):
        raise CarryForwardError("group2_recovery_evidence_invalid")
    if (
        evidence["before_db"] != baseline["fresh"]["db"]
        or evidence["before_files"] != baseline["fresh"]["files"]
        or not isinstance(evidence["request"], dict)
        or evidence["request"].get("profile") != manifest["account_entry"]
        or evidence["request"].get("logs_before") != evidence["logs_before"]
        or evidence["request"].get("logs_after") != evidence["logs_after"]
    ):
        raise CarryForwardError("group2_recovery_evidence_changed")
    deployment = baseline["deployment"]
    fresh = baseline["fresh"]
    preserved = deployment["post_preservation"]
    if (
        not isinstance(preserved, dict)
        or set(preserved)
        != {"code", "cleanup_row_sha256", "post_db_digest", "file_delta_digest"}
        or preserved["code"] != "group2_post_probe_ok"
        or any(
            not isinstance(preserved[key], str)
            or DIGEST.fullmatch(preserved[key]) is None
            for key in (
                "cleanup_row_sha256",
                "post_db_digest",
                "file_delta_digest",
            )
        )
        or preserved["post_db_digest"]
        != digest(
            {
                "protected_rows": deployment["db"].get("protected_rows"),
                "asset_projection": deployment["db"].get("asset_projection"),
                "schema_shape": deployment["db"].get("schema_shape"),
            }
        )
    ):
        raise CarryForwardError("group2_recovery_evidence_invalid")
    _validate_group2_recovery_lineage(
        deployment, baseline["predecessor"], fresh
    )
    _validate_log_link(
        baseline["predecessor"]["logs_after"],
        evidence["logs_before"],
        manifest["account_entry"]["profile_digest"],
    )
    for key in (
        "projection",
        "responsibilities",
        "protected_rows",
        "asset_projection",
        "schema_shape",
        "schema_inventory",
    ):
        if evidence["after_db"].get(key) != fresh["db"].get(key):
            raise CarryForwardError("group2_recovery_evidence_changed")
    proof = _startup_proof(evidence["request"])
    if proof != evidence["proof"]:
        raise CarryForwardError("group2_recovery_evidence_changed")
    startup_result = compare_group2_startup_files(
        evidence["before_files"], evidence["after_files"], proof
    )
    if startup_result != evidence["preservation"]:
        raise CarryForwardError("group2_recovery_evidence_changed")
    account_check = validate_group2_account_check(
        manifest["account_entry"], evidence["account_check"]
    )
    before_worker = evidence["request"].get("container_before")
    worker_profile = manifest["account_entry"]["candidate_profiles"]["worker"]
    if (
        not isinstance(before_worker, dict)
        or before_worker.get("image_id")
        != manifest["account_entry"]["candidate_image_ids_by_service"]["worker"]
        or before_worker.get("command")
        != manifest["account_entry"]["old_containers"]["worker"].get("command")
        or before_worker.get("status") not in {"running", "exited"}
        or not _container_matches_profile(
            before_worker,
            worker_profile,
            manifest["account_entry"]["project"],
            "worker",
        )
        or evidence["request"].get("container_after")
        != account_check["containers"]["worker"]
    ):
        raise CarryForwardError("group2_recovery_evidence_changed")
    validate_group2_entry_probe(manifest["account_entry"], evidence["entry_probe"])
    _validate_log_link(
        evidence["logs_before"],
        evidence["logs_after"],
        manifest["account_entry"]["profile_digest"],
    )
    return {
        "startup_result": startup_result,
        "preservation": evidence["preservation"],
        "account_ready": True,
        "entry_ready": True,
        "log_chain_digest": digest(
            {
                "before": evidence["logs_before"]["evidence_digest"],
                "after": evidence["logs_after"]["evidence_digest"],
            }
        ),
    }


def _closed_object(value: Any, fields: set[str], code: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise CarryForwardError(code)
    return value


def _reconcile_artifact_value(value: Any) -> tuple[Any, str]:
    """Return the parsed value and the digest of the exact supplied bytes."""
    if isinstance(value, bytes):
        raw = value
    elif isinstance(value, str):
        raw = value.encode()
    elif isinstance(value, dict):
        raw = canonical_bytes(value)
    else:
        raise CarryForwardError("group2_reconcile_artifact_invalid")
    try:
        parsed = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise CarryForwardError("group2_reconcile_artifact_invalid") from error
    return parsed, hashlib.sha256(raw).hexdigest()


def _validate_embedded_bytes(value: Any, code: str) -> bytes:
    value = _closed_object(value, {"sha256", "content_b64"}, code)
    _digest_text(value["sha256"], code)
    try:
        raw = base64.b64decode(value["content_b64"], validate=True)
    except (TypeError, ValueError) as error:
        raise CarryForwardError(code) from error
    if hashlib.sha256(raw).hexdigest() != value["sha256"]:
        raise CarryForwardError(code)
    return raw


def _validate_embedded_digest(value: Any, expected: str, code: str) -> None:
    """Validate a large embedded byte string without retaining decoded bytes."""
    value = _closed_object(value, {"sha256", "content_b64"}, code)
    if value["sha256"] != expected or not isinstance(value["content_b64"], str):
        raise CarryForwardError(code)
    _digest_text(expected, code)
    encoded = value["content_b64"]
    if len(encoded) % 4:
        raise CarryForwardError(code)
    output = hashlib.sha256()
    block = 4 * 1024 * 1024
    try:
        for offset in range(0, len(encoded), block):
            chunk = encoded[offset:offset + block]
            decoded = base64.b64decode(chunk, validate=True)
            output.update(decoded)
    except (TypeError, ValueError) as error:
        raise CarryForwardError(code) from error
    if output.hexdigest() != expected:
        raise CarryForwardError(code)


def _read_embedded_file(value: Any, code: str) -> bytes:
    if not isinstance(value, dict) or value.get("exists") is not True:
        raise CarryForwardError(code)
    return _validate_embedded_bytes(
        {"sha256": value.get("sha256"), "content_b64": value.get("content_b64")},
        code,
    )


def _embedded_json(value: Any, code: str) -> Any:
    try:
        return json.loads(_read_embedded_file(value, code))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise CarryForwardError(code) from error


def _reconcile_vm_installed_files(authority: dict[str, Any]) -> dict[str, str]:
    inventory = authority.get("vm_inventory")
    files = inventory.get("files") if isinstance(inventory, dict) else None
    names = ("deploy.sh", "carry_forward.py", "verify.py", "assets.py")
    if not isinstance(files, dict):
        raise CarryForwardError("group2_reconcile_authority_invalid")
    result = {}
    for name in names:
        item = files.get(name)
        if (
            not isinstance(item, dict)
            or item.get("exists") is not True
            or item.get("kind") != "file"
            or DIGEST.fullmatch(str(item.get("sha256"))) is None
        ):
            raise CarryForwardError("group2_reconcile_authority_invalid")
        result[name] = item["sha256"]
    return result


def _machine_json_records(raw: bytes) -> list[dict[str, Any]]:
    try:
        text_value = raw.decode("utf-8")
    except UnicodeError as error:
        raise CarryForwardError("group2_reconcile_review_invalid") from error
    records = []
    for body in re.findall(r"```json\s*\n(.*?)\n```", text_value, re.DOTALL):
        try:
            item = json.loads(body)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            records.append(item)
    return records


def _validate_reconcile_wrappers(
    request: dict[str, Any], parsed: dict[str, Any], raw_hashes: dict[str, str]
) -> None:
    failed = validate_manifest(parsed["failed-manifest.json"])
    validate_group2_manifest_extensions(failed)
    first = _closed_object(
        parsed["first-startup.json"],
        {"schema", "check_raw", "log_read_diagnostic", "actual_results", "db", "files", "legacy_assets"},
        "group2_reconcile_first_startup_invalid",
    )
    prior = _closed_object(
        parsed["prior-success.json"], {"schema", "host", "vm"},
        "group2_reconcile_prior_invalid",
    )
    authority = _closed_object(
        parsed["authority.json"], {"schema", "snapshot", "host_authority", "vm_inventory"},
        "group2_reconcile_authority_invalid",
    )
    platform = _closed_object(
        parsed["platform.json"], {"schema", "images", "postgres_format", "source_files"},
        "group2_reconcile_platform_invalid",
    )
    if (
        first["schema"] != "group2-first-startup-evidence-v1"
        or prior["schema"] != "group2-prior-success-evidence-v1"
        or authority["schema"] != "group2-incident-authority-v1"
        or platform["schema"] != "group2-incident-platform-v1"
    ):
        raise CarryForwardError("group2_reconcile_artifact_invalid")
    prior_id = request["prior"]["manifest_id"]
    prior_manifest_name = f"carry-forward/manifests/{prior_id}.json"
    prior_consumed_name = f"carry-forward/consumed/{prior_id}.json"
    expected_host = {"state.json", prior_manifest_name, prior_consumed_name}
    expected_vm = {
        prior_manifest_name,
        prior_consumed_name,
        f"releases/{GROUP2_FROM_SHA}/images.json",
        f"releases/{GROUP2_FROM_SHA}/probe.json",
        f"releases/{GROUP2_FROM_SHA}/receipt.json",
        f"releases/{GROUP2_FROM_SHA}/schema",
    }
    if set(prior["host"]) != expected_host or set(prior["vm"]) != expected_vm:
        raise CarryForwardError("group2_reconcile_prior_invalid")
    host_state = _embedded_json(
        prior["host"]["state.json"], "group2_reconcile_prior_invalid"
    )
    host_consumed_raw = _read_embedded_file(
        prior["host"][prior_consumed_name], "group2_reconcile_prior_invalid"
    )
    vm_manifest_raw = _read_embedded_file(
        prior["vm"][prior_manifest_name], "group2_reconcile_prior_invalid"
    )
    prior_manifest = validate_manifest(json.loads(vm_manifest_raw))
    vm_images = _embedded_json(
        prior["vm"][f"releases/{GROUP2_FROM_SHA}/images.json"],
        "group2_reconcile_prior_invalid",
    )
    vm_receipt_raw = _read_embedded_file(
        prior["vm"][f"releases/{GROUP2_FROM_SHA}/receipt.json"],
        "group2_reconcile_prior_invalid",
    )
    try:
        vm_receipt = json.loads(vm_receipt_raw)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise CarryForwardError("group2_reconcile_prior_invalid") from error
    vm_receipt = _closed_object(
        vm_receipt, {"schema", "images", "probe", "backup", "carry_forward"},
        "group2_reconcile_prior_invalid",
    )
    vm_probe = _embedded_json(
        prior["vm"][f"releases/{GROUP2_FROM_SHA}/probe.json"],
        "group2_reconcile_prior_invalid",
    )
    vm_schema = _read_embedded_file(
        prior["vm"][f"releases/{GROUP2_FROM_SHA}/schema"],
        "group2_reconcile_prior_invalid",
    ).decode().strip()
    if (
        prior["host"][prior_manifest_name] != {"exists": False}
        or prior["vm"][prior_consumed_name] != {"exists": False}
        or host_consumed_raw != vm_manifest_raw
        or prior_manifest["manifest_id"] != prior_id
        or prior_manifest["manifest_digest"] != request["prior"]["manifest_digest"]
        or host_state.get("sha") != GROUP2_FROM_SHA
        or host_state.get("schema") != request["prior"]["schema"]
        or host_state.get("images") != vm_images
        or digest(vm_images) != request["prior"]["images_digest"]
        or vm_receipt["schema"] != request["prior"]["schema"]
        or vm_receipt["images"] != vm_images
        or vm_receipt["probe"] != vm_probe
        or not isinstance(vm_receipt["backup"], str)
        or not vm_receipt["backup"].startswith("/")
        or vm_receipt["carry_forward"]
        != {
            "manifest_id": prior_id,
            "manifest_digest": request["prior"]["manifest_digest"],
            "selection_count": len(prior_manifest["responsibilities"]["executions"]),
        }
        or hashlib.sha256(vm_receipt_raw).hexdigest() != request["prior"]["receipt_sha256"]
        or hashlib.sha256(host_consumed_raw).hexdigest() != request["prior"]["consumed_sha256"]
        or vm_schema != request["prior"]["schema"]
    ):
        raise CarryForwardError("group2_reconcile_prior_invalid")
    snapshot = authority["snapshot"]
    if (
        not isinstance(snapshot, dict)
        or snapshot.get("enabled") is not False
        or snapshot.get("selected_pr") != request["pr"]
        or snapshot.get("state") != host_state
        or snapshot.get("carry_reference", {}).get("manifest_id")
        != request["failed"]["manifest_id"]
        or snapshot.get("carry_reference", {}).get("manifest_digest")
        != request["failed"]["manifest_digest"]
        or snapshot.get("attention", {}).get("phase") != "switching"
        or snapshot.get("attention", {}).get("candidate", {}).get("sha")
        != request["failed"]["sha"]
        or snapshot.get("transaction", {}).get("phase") != "starting"
        or snapshot.get("transaction", {}).get("sha") != request["failed"]["sha"]
        or digest(snapshot.get("installed_files"))
        != request["failed"]["installed_controller_files_digest"]
    ):
        raise CarryForwardError("group2_reconcile_authority_invalid")
    _reconcile_vm_installed_files(authority)
    image_ids = {
        service: item.get("Id")
        for service, item in platform["images"].get("prior", {}).items()
    }
    expected_prior_images = {
        tag: image_ids[tag.rsplit("-", 1)[-1].split(":", 1)[0]]
        for tag in vm_images
    }
    if expected_prior_images != vm_images:
        raise CarryForwardError("group2_reconcile_platform_invalid")
    source_files = platform["source_files"]
    expected_source_files = {
        "old-postgres.Dockerfile", "candidate-postgres.Dockerfile",
        "old-postgres-entrypoint.sh", "candidate-postgres-entrypoint.sh",
    }
    if not isinstance(source_files, dict) or set(source_files) != expected_source_files:
        raise CarryForwardError("group2_reconcile_platform_invalid")
    for value in source_files.values():
        _validate_embedded_bytes(value, "group2_reconcile_platform_invalid")
    postgres_format = _closed_object(
        platform["postgres_format"],
        {"data_pg_version", "current_server_version", "prior_binary_version",
         "candidate_binary_version", "claim"},
        "group2_reconcile_platform_invalid",
    )
    prior_postgres = platform["images"].get("prior", {}).get("postgres", {})
    candidate_postgres = platform["images"].get("candidate", {}).get("postgres", {})
    if (
        not all(
            isinstance(postgres_format[key], str) and postgres_format[key]
            for key in postgres_format
        )
        or prior_postgres.get("postgres_version")
        != postgres_format["prior_binary_version"]
        or candidate_postgres.get("postgres_version")
        != postgres_format["candidate_binary_version"]
        or f"PG_MAJOR={postgres_format['data_pg_version']}"
        not in prior_postgres.get("postgres_env_versions", [])
        or f"PG_MAJOR={postgres_format['data_pg_version']}"
        not in candidate_postgres.get("postgres_env_versions", [])
    ):
        raise CarryForwardError("group2_reconcile_platform_invalid")
    source = _closed_object(
        parsed["source-review.json"],
        {"schema", "status", "tool_sha", "source_scope", "controller_files",
         "inherited_reviews", "tool_review"},
        "group2_reconcile_review_invalid",
    )
    if (
        source["schema"] != "group2-reconcile-source-review-v1"
        or source["status"] != "APPROVED"
        or source["tool_sha"] != request["tool"]["sha"]
        or source["controller_files"] != request["tool"]["controller_files"]
        or digest(source["source_scope"]) != request["tool"]["source_scope_digest"]
    ):
        raise CarryForwardError("group2_reconcile_review_invalid")
    validate_group2_source_diff(source["source_scope"], {"final_source": source["source_scope"]})
    inherited = source["inherited_reviews"]
    if not isinstance(inherited, list) or len(inherited) != 3:
        raise CarryForwardError("group2_reconcile_review_invalid")
    inherited_names = set()
    for item in inherited:
        item = _closed_object(item, {"name", "sha256", "content_b64"}, "group2_reconcile_review_invalid")
        if not isinstance(item["name"], str) or item["name"] in inherited_names:
            raise CarryForwardError("group2_reconcile_review_invalid")
        inherited_names.add(item["name"])
        _validate_embedded_bytes(
            {"sha256": item["sha256"], "content_b64": item["content_b64"]},
            "group2_reconcile_review_invalid",
        )
        if GROUP2_INHERITED_REVIEW_HASHES.get(item["name"]) != item["sha256"]:
            raise CarryForwardError("group2_reconcile_review_invalid")
    if inherited_names != set(GROUP2_INHERITED_REVIEW_HASHES):
        raise CarryForwardError("group2_reconcile_review_invalid")
    tool_review = _closed_object(
        source["tool_review"], {"sha256", "content_b64", "entries"},
        "group2_reconcile_review_invalid",
    )
    review_raw = _validate_embedded_bytes(
        {"sha256": tool_review["sha256"], "content_b64": tool_review["content_b64"]},
        "group2_reconcile_review_invalid",
    )
    expected_entries = [
        item for item in source["source_scope"]["entries"]
        if item.get("path") in GROUP2_CONTROLLER_PATHS
    ]
    if tool_review["entries"] != expected_entries or len(expected_entries) != 10:
        raise CarryForwardError("group2_reconcile_review_invalid")
    matches = [
        item for item in _machine_json_records(review_raw)
        if item.get("schema") == "group2-reconcile-tool-review-v1"
    ]
    expected_record = {
        "schema": "group2-reconcile-tool-review-v1",
        "status": "APPROVED",
        "head_sha": request["tool"]["sha"],
        "source_scope_digest": request["tool"]["source_scope_digest"],
        "controller_files": request["tool"]["controller_files"],
        "entries": expected_entries,
    }
    if matches != [expected_record]:
        raise CarryForwardError("group2_reconcile_review_invalid")
    ci = _closed_object(
        parsed["ci.json"], {"schema", "binding", "raw"},
        "group2_reconcile_ci_invalid",
    )
    if ci["schema"] != "group2-reconcile-ci-v1" or not isinstance(ci["raw"], dict):
        raise CarryForwardError("group2_reconcile_ci_invalid")
    run, jobs_response = ci["raw"].get("run"), ci["raw"].get("jobs")
    if not isinstance(run, dict) or not isinstance(jobs_response, dict):
        raise CarryForwardError("group2_reconcile_ci_invalid")
    jobs = jobs_response.get("jobs")
    if not isinstance(jobs, list) or jobs_response.get("total_count") != len(jobs):
        raise CarryForwardError("group2_reconcile_ci_invalid")
    normalized_jobs = sorted(
        ({key: job.get(key) for key in ("name", "id", "conclusion")} for job in jobs),
        key=lambda item: (item["name"], item["id"]),
    )
    binding = {
        "head_sha": run.get("head_sha"), "run_id": run.get("id"),
        "run_attempt": run.get("run_attempt"), "workflow_path": run.get("path"),
        "event": run.get("event"), "jobs": normalized_jobs,
    }
    if (
        ci["binding"] != binding
        or binding["head_sha"] != request["tool"]["sha"]
        or run.get("status") != "completed"
        or run.get("conclusion") != "success"
        or any(
            job.get("status") != "completed"
            or job.get("run_id") != run.get("id")
            or job.get("run_attempt") != run.get("run_attempt")
            or job.get("head_sha") != run.get("head_sha")
            for job in jobs
        )
        or any(job["conclusion"] != "success" for job in normalized_jobs)
        or any(sum(job["name"] == name for job in normalized_jobs) != 1
               for name in ("backend", "web", "local-preview", "compose-smoke"))
    ):
        raise CarryForwardError("group2_reconcile_ci_invalid")
    if (
        failed["manifest_id"] != request["failed"]["manifest_id"]
        or failed["manifest_digest"] != request["failed"]["manifest_digest"]
        or failed["to_sha"] != request["failed"]["sha"]
        or failed["review_scope_digest"] != request["failed"]["scope_digest"]
        or failed["controller_files_digest"]
        != request["failed"]["installed_controller_files_digest"]
        or raw_hashes["failed-manifest.json"] != request["failed"]["manifest_sha256"]
        or digest({name: raw_hashes[name] for name in (
            "failed-manifest.json", "first-startup.json", "prior-success.json",
            "authority.json", "platform.json")})
        != request["failed"]["initial_evidence_digest"]
        or raw_hashes["source-review.json"] != request["tool"]["review_report_sha256"]
        or raw_hashes["ci.json"] != request["tool"]["ci_evidence_sha256"]
    ):
        raise CarryForwardError("group2_reconcile_artifact_binding_changed")
    check = first["check_raw"]
    required_check = {
        "after-migration/db.json", "after-migration/files.json",
        "group2/account-check.json", "group2/log-before-start.json",
        "group2/log-after-start-request.json", "group2/start-window.json",
    }
    if not isinstance(check, dict) or not required_check.issubset(check):
        raise CarryForwardError("group2_reconcile_first_startup_invalid")
    before_logs = check["group2/log-before-start.json"].get("log_evidence")
    after_request = check["group2/log-after-start-request.json"]
    after_logs = after_request.get("baseline")
    # The saved request binds the exact baseline used for the append read.  The
    # resulting segment is retained by the diagnostic wrapper rather than by a
    # replacement success file after the original read failed.
    if before_logs != after_logs:
        raise CarryForwardError("group2_reconcile_first_startup_invalid")
    diagnostic = first["log_read_diagnostic"]
    log_after = diagnostic.get("log_append") if isinstance(diagnostic, dict) else None
    window = check["group2/start-window.json"]
    account_check = check["group2/account-check.json"].get("account_check", {})
    startup_request = {
        "mode": GROUP2_MODE,
        "operation": "startup-proof",
        "profile": failed["account_entry"],
        "logs_before": before_logs,
        "logs_after": log_after,
        "container_before": failed["account_entry"]["old_containers"]["worker"],
        "container_after": account_check.get("containers", {}).get("worker"),
        "window_start_ns": window.get("window_start_ns"),
        "window_end_ns": window.get("window_end_ns"),
    }
    proof = _startup_proof(startup_request)
    compare_group2_startup_files(
        check["after-migration/files.json"], first["files"], proof
    )
    reconstruction = first["actual_results"].get("startup_reconstruction")
    if (
        not isinstance(reconstruction, dict)
        or set(reconstruction) != {"result", "proof"}
        or reconstruction["result"] != "DERIVED_FROM_LATER_DIAGNOSTIC_ONLY"
        or reconstruction["proof"] != proof
    ):
        raise CarryForwardError("group2_reconcile_first_startup_changed")
    _validate_group2_manifest_db(failed, first["db"])
    legacy = first["legacy_assets"]
    projection = first["db"]["asset_projection"]
    if not isinstance(legacy, dict) or set(legacy) != set(ASSET_TABLES):
        raise CarryForwardError("group2_reconcile_first_startup_changed")
    for name in ASSET_TABLES:
        item = legacy[name]
        projected = projection[name]
        if (
            not isinstance(item, dict)
            or set(item) != {"columns", "rows"}
            or item["columns"] != projected["columns"]
            or not isinstance(item["rows"], list)
            or not all(
                isinstance(row, str) and DIGEST.fullmatch(row)
                for row in item["rows"]
            )
            or len(item["rows"]) != projected["count"]
        ):
            raise CarryForwardError("group2_reconcile_first_startup_changed")


def validate_group2_reconcile_user_record(
    request: Any, approval: Any, raw: bytes
) -> dict[str, Any]:
    """Validate the separately retained transcript against request and approval."""
    if not isinstance(raw, bytes):
        raise CarryForwardError("group2_reconcile_approval_record_invalid")
    _closed_object(
        request,
        {"schema", "mode", "action", "incident_id", "repo", "pr", "failed",
         "prior", "tool", "restore", "evidence_files", "request_digest"},
        "group2_reconcile_request_invalid",
    )
    try:
        text = raw.decode("utf-8")
        decoder = json.JSONDecoder()
        record, end = decoder.raw_decode(text)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise CarryForwardError("group2_reconcile_approval_record_invalid") from error
    if text[end:] not in {"", "\n"}:
        raise CarryForwardError("group2_reconcile_approval_record_invalid")
    record = _closed_object(
        record,
        {"schema", "request_digest", "tool_sha", "actions", "presented_request",
         "user_reply"},
        "group2_reconcile_approval_record_invalid",
    )
    presented = record["presented_request"]
    if (
        approval.get("user_record_sha256") != hashlib.sha256(raw).hexdigest()
        or record["schema"] != "group2-starting-reconcile-user-record-v1"
        or any(
            record.get(key) != approval.get(key)
            for key in ("request_digest", "tool_sha", "actions", "user_reply")
        )
        or record["request_digest"] != request["request_digest"]
        or record["tool_sha"] != request["tool"]["sha"]
        or record["actions"] != GROUP2_RECONCILE_ACTIONS
        or not isinstance(record["user_reply"], str)
        or not record["user_reply"].strip()
        or not isinstance(presented, str)
        or any(
            value not in presented
            for value in (
                request["request_digest"], request["tool"]["sha"],
                *GROUP2_RECONCILE_ACTIONS,
            )
        )
    ):
        raise CarryForwardError("group2_reconcile_approval_record_invalid")
    return record


def _partial_embedded_json(value: Any, code: str) -> tuple[Any, bytes]:
    raw = _validate_embedded_bytes(value, code)
    try:
        return json.loads(raw), raw
    except (UnicodeError, json.JSONDecodeError) as error:
        raise CarryForwardError(code) from error


def _validate_group2_partial_originals(artifacts: dict[str, Any]) -> dict[str, Any]:
    """Validate the four frozen incident inputs without inventing missing history."""
    code = "group2_partial_finalize_artifact_invalid"
    original = _closed_object(
        artifacts["original-incident.json"],
        {"request", "approval", "user_record", "artifacts", "tool_source"}, code,
    )
    old_request, _ = _partial_embedded_json(original["request"], code)
    old_approval, _ = _partial_embedded_json(original["approval"], code)
    old_user_record = _validate_embedded_bytes(original["user_record"], code)
    old_artifacts = _closed_object(
        original["artifacts"], GROUP2_RECONCILE_EVIDENCE_FILES, code
    )
    old_raw = {
        name: _validate_embedded_bytes(value, code)
        for name, value in old_artifacts.items()
    }
    validated = validate_group2_reconcile_request(
        old_request, old_approval, old_raw
    )
    validate_group2_reconcile_user_record(
        old_request, old_approval, old_user_record
    )
    if (
        old_request["incident_id"] != GROUP2_PARTIAL_INCIDENT_ID
        or old_request["request_digest"] != GROUP2_PARTIAL_ORIGINAL_REQUEST_DIGEST
        or old_request["tool"]["sha"] != GROUP2_PARTIAL_ORIGINAL_TOOL_SHA
    ):
        raise CarryForwardError(code)
    tool_source = _closed_object(
        original["tool_source"], {"deploy.sh", "carry_forward.py"}, code
    )
    for name, value in tool_source.items():
        raw = _validate_embedded_bytes(value, code)
        if hashlib.sha256(raw).hexdigest() != old_request["tool"][
            "controller_files"
        ][name]:
            raise CarryForwardError(code)

    partial = _closed_object(
        artifacts["partial-attempt.json"],
        {"official_attempt", "official_log", "phase", "deploy_tail", "historical"},
        code,
    )
    for key in ("official_attempt", "phase", "deploy_tail"):
        if partial[key].get("sha256") != GROUP2_PARTIAL_FIXED_HASHES[key]:
            raise CarryForwardError(code)
    attempt, _ = _partial_embedded_json(partial["official_attempt"], code)
    official_log = _validate_embedded_bytes(partial["official_log"], code)
    phase, _ = _partial_embedded_json(partial["phase"], code)
    _validate_embedded_bytes(partial["deploy_tail"], code)
    expected_stages = {
        name: ["db.json", "files.json", "ids.json"]
        for name in (
            "preflight", "control-stopped", "apps-stopped", "restored-postgres"
        )
    }
    if (
        attempt
        != {
            **attempt,
            "source_sha": GROUP2_PARTIAL_ORIGINAL_TOOL_SHA,
            "request_digest": GROUP2_PARTIAL_ORIGINAL_REQUEST_DIGEST,
            "incident_id": GROUP2_PARTIAL_INCIDENT_ID,
            "exit_code": 1,
        }
        or attempt.get("log_sha256") != hashlib.sha256(official_log).hexdigest()
        or phase.get("phase", {}).get("incident_id") != GROUP2_PARTIAL_INCIDENT_ID
        or phase.get("phase", {}).get("phase") != "restoring-apps"
        or phase.get("transaction", {}).get("phase") != "starting"
        or phase.get("transaction", {}).get("sha") != GROUP2_PARTIAL_FAILED_SHA
        or phase.get("result") is not False
        or phase.get("stages") != expected_stages
    ):
        raise CarryForwardError(code)
    historical_names = {
        f"{stage}/{name}"
        for stage in expected_stages
        for name in ("ids.json", "db.json", "files.json")
    }
    historical = _closed_object(partial["historical"], historical_names, code)

    diagnostic = _closed_object(
        artifacts["diagnostic.json"], {"result", "vm_state", "host_state"}, code
    )
    for key, fixed in (
        ("result", "diagnostic_result"),
        ("vm_state", "diagnostic_vm_state"),
        ("host_state", "diagnostic_host_state"),
    ):
        if diagnostic[key].get("sha256") != GROUP2_PARTIAL_FIXED_HASHES[fixed]:
            raise CarryForwardError(code)
    diagnostic_result, _ = _partial_embedded_json(diagnostic["result"], code)
    diagnostic_vm_state, _ = _partial_embedded_json(diagnostic["vm_state"], code)
    diagnostic_host_state, _ = _partial_embedded_json(
        diagnostic["host_state"], code
    )
    originals = _group2_reconcile_originals(validated)
    saved = diagnostic_result.get("historical_capture_bytes")
    if not isinstance(saved, dict) or set(saved) != historical_names:
        raise CarryForwardError(code)
    for name, value in historical.items():
        raw = _validate_embedded_bytes(value, code)
        if value != saved[name]:
            raise CarryForwardError(code)
        parsed, _ = _reconcile_artifact_value(raw)
        suffix = name.rsplit("/", 1)[1]
        expected = (
            originals["manifest"]["selection"]
            if suffix == "ids.json"
            else originals["db"]
            if suffix == "db.json"
            else originals["files_after_startup"]
        )
        if parsed != expected:
            raise CarryForwardError(code)

    scope = _closed_object(
        artifacts["scope-acceptance.json"],
        {"incident_id", "original_request_digest", "decision_report",
         "presented_request", "user_reply"},
        code,
    )
    if (
        scope["incident_id"] != GROUP2_PARTIAL_INCIDENT_ID
        or scope["original_request_digest"]
        != GROUP2_PARTIAL_ORIGINAL_REQUEST_DIGEST
        or scope["decision_report"].get("sha256")
        != GROUP2_PARTIAL_FIXED_HASHES["decision_report"]
        or not isinstance(scope["presented_request"], str)
        or not scope["presented_request"].strip()
        or scope["user_reply"] != "收尾吧"
    ):
        raise CarryForwardError(code)
    _validate_embedded_bytes(scope["decision_report"], code)
    return {
        "validated_original": validated,
        "originals": originals,
        "attempt": attempt,
        "phase": phase,
        "diagnostic": diagnostic_result,
        "diagnostic_vm_state": diagnostic_vm_state,
        "diagnostic_host_state": diagnostic_host_state,
    }


def validate_group2_partial_finalize_inputs(artifacts: Any) -> dict[str, Any]:
    """Validate the four frozen inputs available before final review and CI."""
    names = {
        "original-incident.json", "partial-attempt.json",
        "diagnostic.json", "scope-acceptance.json",
    }
    if not isinstance(artifacts, dict) or set(artifacts) != names:
        raise CarryForwardError("group2_partial_finalize_artifact_invalid")
    parsed = {
        name: _reconcile_artifact_value(value)[0]
        for name, value in artifacts.items()
    }
    return _validate_group2_partial_originals(parsed)


def _validate_group2_partial_tool(
    tool: dict[str, Any], source: Any, ci: Any
) -> None:
    code = "group2_partial_finalize_tool_invalid"
    source = _closed_object(
        source,
        {"schema", "status", "tool_sha", "source_scope", "controller_files",
         "inherited_reviews", "tool_review"},
        code,
    )
    if (
        source["schema"] != "group2-reconcile-source-review-v1"
        or source["status"] != "APPROVED"
        or source["tool_sha"] != tool["sha"]
        or source["controller_files"] != tool["controller_files"]
        or digest(source["source_scope"]) != tool["source_scope_digest"]
    ):
        raise CarryForwardError(code)
    validate_group2_source_diff(
        source["source_scope"], {"final_source": source["source_scope"]}
    )
    inherited = source["inherited_reviews"]
    if not isinstance(inherited, list) or {
        item.get("name") for item in inherited if isinstance(item, dict)
    } != set(GROUP2_INHERITED_REVIEW_HASHES):
        raise CarryForwardError(code)
    for item in inherited:
        item = _closed_object(item, {"name", "sha256", "content_b64"}, code)
        _validate_embedded_bytes(
            {"sha256": item["sha256"], "content_b64": item["content_b64"]},
            code,
        )
        if GROUP2_INHERITED_REVIEW_HASHES[item["name"]] != item["sha256"]:
            raise CarryForwardError(code)
    review = _closed_object(
        source["tool_review"], {"sha256", "content_b64", "entries"}, code
    )
    review_raw = _validate_embedded_bytes(
        {"sha256": review["sha256"], "content_b64": review["content_b64"]}, code
    )
    entries = [
        item for item in source["source_scope"]["entries"]
        if item.get("path") in GROUP2_CONTROLLER_PATHS
    ]
    if review["entries"] != entries or len(entries) != 10:
        raise CarryForwardError(code)
    records = _machine_json_records(review_raw)
    expected_tool = {
        "schema": "group2-reconcile-tool-review-v1",
        "status": "APPROVED",
        "head_sha": tool["sha"],
        "source_scope_digest": tool["source_scope_digest"],
        "controller_files": tool["controller_files"],
        "entries": entries,
    }
    tool_matches = [
        item for item in records
        if item.get("schema") == "group2-reconcile-tool-review-v1"
    ]
    independent = [
        item for item in records
        if item.get("schema") == "group2-independent-review-v1"
    ]
    if tool_matches != [expected_tool] or len(independent) != 1:
        raise CarryForwardError(code)
    independent_record = _closed_object(
        independent[0],
        {"schema", "status", "reviewed_commit", "source_kind", "coverage",
         "blocking_findings"}, code,
    )
    coverage = independent_record["coverage"]
    if (
        independent_record["status"] != "APPROVED"
        or independent_record["reviewed_commit"] != tool["sha"]
        or independent_record["source_kind"] != "git_commit"
        or independent_record["blocking_findings"] != []
        or not isinstance(coverage, list)
        or len(coverage) != 10
    ):
        raise CarryForwardError(code)
    coverage_by_path = {}
    for item in coverage:
        item = _closed_object(
            item, {"path", "mode", "blob_oid", "sha256"}, code
        )
        if item["path"] in coverage_by_path:
            raise CarryForwardError(code)
        _digest_text(item["sha256"], code)
        coverage_by_path[item["path"]] = item
    if any(
        coverage_by_path.get(item["path"])
        != {
            "path": item["path"], "mode": item["new_mode"],
            "blob_oid": item["new_oid"],
            "sha256": coverage_by_path.get(item["path"], {}).get("sha256"),
        }
        for item in entries
    ):
        raise CarryForwardError(code)

    ci = _closed_object(ci, {"schema", "binding", "raw"}, code)
    run = ci.get("raw", {}).get("run") if isinstance(ci.get("raw"), dict) else None
    jobs_value = ci.get("raw", {}).get("jobs") if isinstance(ci.get("raw"), dict) else None
    jobs = jobs_value.get("jobs") if isinstance(jobs_value, dict) else None
    if (
        ci["schema"] != "group2-reconcile-ci-v1"
        or not isinstance(run, dict)
        or not isinstance(jobs, list)
        or jobs_value.get("total_count") != len(jobs)
    ):
        raise CarryForwardError(code)
    normalized = sorted(
        ({key: job.get(key) for key in ("name", "id", "conclusion")} for job in jobs),
        key=lambda item: (item["name"], item["id"]),
    )
    binding = {
        "head_sha": run.get("head_sha"), "run_id": run.get("id"),
        "run_attempt": run.get("run_attempt"), "workflow_path": run.get("path"),
        "event": run.get("event"), "jobs": normalized,
    }
    if (
        ci["binding"] != binding
        or binding["head_sha"] != tool["sha"]
        or run.get("status") != "completed"
        or run.get("conclusion") != "success"
        or any(
            job.get("status") != "completed"
            or job.get("run_id") != run.get("id")
            or job.get("run_attempt") != run.get("run_attempt")
            or job.get("head_sha") != run.get("head_sha")
            or job.get("conclusion") != "success"
            for job in jobs
        )
        or any(sum(job["name"] == name for job in normalized) != 1
               for name in ("backend", "web", "local-preview", "compose-smoke"))
    ):
        raise CarryForwardError(code)


def validate_group2_partial_finalize_request(
    request: Any, approval: Any, user_record: bytes, artifacts: Any
) -> dict[str, Any]:
    code = "group2_partial_finalize_request_invalid"
    request = _closed_object(
        request,
        {"schema", "mode", "action", "incident_id", "finalize_id", "repo", "pr",
         "original_request_digest", "tool", "evidence_files", "request_digest"},
        code,
    )
    if (
        request["schema"] != "group2-partial-finalize-request-v1"
        or request["mode"] != GROUP2_MODE
        or request["action"] != "finalize-observed-prior-software"
        or request["incident_id"] != GROUP2_PARTIAL_INCIDENT_ID
        or MANIFEST_ID.fullmatch(str(request["finalize_id"])) is None
        or not isinstance(request["repo"], str)
        or re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", request["repo"])
        is None
        or request["original_request_digest"]
        != GROUP2_PARTIAL_ORIGINAL_REQUEST_DIGEST
        or request["request_digest"]
        != digest({key: value for key, value in request.items() if key != "request_digest"})
    ):
        raise CarryForwardError(code)
    _positive(request["pr"], code)
    tool = _closed_object(
        request["tool"],
        {"sha", "controller_files", "source_scope_digest", "review_report_sha256",
         "ci_evidence_sha256"}, code,
    )
    if (
        tool["sha"] in {GROUP2_PARTIAL_ORIGINAL_TOOL_SHA, GROUP2_PARTIAL_FAILED_SHA}
        or set(tool["controller_files"]) != GROUP2_CONTROLLER_FILES
    ):
        raise CarryForwardError(code)
    _sha_text(tool["sha"], code)
    for value in (*tool["controller_files"].values(), tool["source_scope_digest"],
                  tool["review_report_sha256"], tool["ci_evidence_sha256"]):
        _digest_text(value, code)
    evidence_files = _closed_object(
        request["evidence_files"], GROUP2_PARTIAL_FINALIZE_FILES, code
    )
    if not isinstance(artifacts, dict) or set(artifacts) != GROUP2_PARTIAL_FINALIZE_FILES:
        raise CarryForwardError(code)
    parsed = {}
    for name, value in artifacts.items():
        parsed[name], actual = _reconcile_artifact_value(value)
        if evidence_files[name] != actual:
            raise CarryForwardError("group2_partial_finalize_artifact_invalid")
    context = _validate_group2_partial_originals(parsed)
    old_request = context["validated_original"]["request"]
    if request["repo"] != old_request["repo"] or request["pr"] != old_request["pr"]:
        raise CarryForwardError(code)
    if (
        evidence_files["source-review.json"] != tool["review_report_sha256"]
        or evidence_files["ci.json"] != tool["ci_evidence_sha256"]
    ):
        raise CarryForwardError(code)
    _validate_group2_partial_tool(
        tool, parsed["source-review.json"], parsed["ci.json"]
    )
    approval = _closed_object(
        approval,
        {"schema", "status", "request_digest", "tool_sha", "actions", "user_reply",
         "user_record_sha256"}, code,
    )
    if not isinstance(user_record, bytes):
        raise CarryForwardError(code)
    try:
        text = user_record.decode()
        record, end = json.JSONDecoder().raw_decode(text)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise CarryForwardError(code) from error
    record = _closed_object(
        record,
        {"schema", "request_digest", "tool_sha", "actions", "presented_request",
         "user_reply"}, code,
    )
    presented = record["presented_request"]
    if (
        text[end:] not in {"", "\n"}
        or approval["schema"] != "group2-partial-finalize-approval-v1"
        or approval["status"] != "USER_APPROVED"
        or approval["request_digest"] != request["request_digest"]
        or approval["tool_sha"] != tool["sha"]
        or approval["actions"] != GROUP2_PARTIAL_FINALIZE_ACTIONS
        or approval["user_record_sha256"] != hashlib.sha256(user_record).hexdigest()
        or record["schema"] != "group2-partial-finalize-user-record-v1"
        or any(record[key] != approval[key] for key in ("request_digest", "tool_sha", "actions", "user_reply"))
        or not isinstance(approval["user_reply"], str)
        or not approval["user_reply"].strip()
        or not isinstance(presented, str)
        or any(value not in presented for value in (
            request["incident_id"], request["finalize_id"],
            request["original_request_digest"], request["request_digest"], tool["sha"],
            *GROUP2_PARTIAL_FINALIZE_ACTIONS,
            "historical", "outer_official_attempt",
        ))
    ):
        raise CarryForwardError(code)
    return {"request": request, "approval": approval, "user_record": record,
            "artifacts": parsed, "context": context}


def group2_partial_finalize_original_reference(chain: Any) -> dict[str, Any]:
    code = "group2_partial_finalize_chain_invalid"
    if not isinstance(chain, dict):
        raise CarryForwardError(code)
    sources = _closed_object(
        chain.get("source_artifacts"), GROUP2_PARTIAL_FINALIZE_FILES, code
    )
    original_raw = _validate_embedded_bytes(sources["original-incident.json"], code)
    try:
        original = json.loads(original_raw)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise CarryForwardError(code) from error
    original = _closed_object(
        original,
        {"request", "approval", "user_record", "artifacts", "tool_source"}, code,
    )
    old_sources = _closed_object(
        original["artifacts"], GROUP2_RECONCILE_EVIDENCE_FILES, code
    )
    failed_raw = _validate_embedded_bytes(old_sources["failed-manifest.json"], code)
    try:
        failed = validate_manifest(json.loads(failed_raw))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise CarryForwardError(code) from error
    return failed["review_scope"]["preservation_reference"]


def _utc_text_ns(value: Any) -> int:
    if not isinstance(value, str):
        raise CarryForwardError("group2_partial_finalize_history_invalid")
    match = re.fullmatch(
        r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d{1,9}))?\+00:00", value
    )
    if match is None:
        raise CarryForwardError("group2_partial_finalize_history_invalid")
    timestamp = dt.datetime.fromisoformat(match.group(1) + "+00:00")
    seconds = int((timestamp - dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)).total_seconds())
    return seconds * 1_000_000_000 + int((match.group(2) or "").ljust(9, "0"))


def _group2_partial_history(validated: dict[str, Any]) -> dict[str, Any]:
    context = validated["context"]
    originals = context["originals"]
    manifest = originals["manifest"]
    diagnostic = context["diagnostic"]
    stage = _validate_reconcile_stage(diagnostic.get("stage"))
    attempt = context["attempt"]
    startup_request = {
        "mode": GROUP2_MODE,
        "operation": "startup-proof",
        "profile": manifest["account_entry"],
        "logs_before": originals["logs_after_startup"],
        "logs_after": stage["logs"],
        "container_before": originals["first_startup"]["request"]["container_after"],
        "container_after": stage["containers"]["worker"],
        "window_start_ns": _utc_text_ns(attempt["started_at"]),
        "window_end_ns": _utc_text_ns(attempt["finished_at"]),
    }
    proof = _incident_startup_proof(startup_request)
    if (
        proof.get("nonce") != GROUP2_PARTIAL_NONCE
        or proof.get("container_id")
        != GROUP2_PARTIAL_WORKER_ID
        or stage["db"] != originals["db"]
        or stage["containers"]["worker"].get("restart_count") != 0
        or stage["storage"] != manifest["storage_identity"]
    ):
        raise CarryForwardError("group2_partial_finalize_history_invalid")
    for service in ("postgres", "control", "worker", "web"):
        matches = [
            image for tag, image in manifest["old_image_ids"].items()
            if tag.endswith(f"-{service}:{GROUP2_FROM_SHA}")
        ]
        container = stage["containers"][service]
        if (
            matches != [container.get("image_id")]
            or container.get("status") != "running"
            or container.get("health") != "healthy"
            or container.get("restart_count") != 0
        ):
            raise CarryForwardError("group2_partial_finalize_history_invalid")
        if service in {"control", "worker", "web"} and not _container_matches_profile(
            container, manifest["account_entry"]["old_profiles"][service],
            manifest["account_entry"]["project"], service,
        ):
            raise CarryForwardError("group2_partial_finalize_history_invalid")
    rabbit = stage["containers"]["rabbitmq"]
    account = stage["containers"]["account-web"]
    if (
        rabbit.get("status") != "running"
        or rabbit.get("health") != "healthy"
        or rabbit.get("restart_count") != 0
        or account.get("status") == "running"
    ):
        raise CarryForwardError("group2_partial_finalize_history_invalid")
    startup_files = compare_group2_startup_files(
        originals["files_after_startup"], stage["files"], proof
    )
    _validate_log_link(originals["logs_after_startup"], stage["logs"])
    _validate_reconcile_log_activity(stage["logs"], allow_registration=True)
    original_kernel = context["validated_original"]["artifacts"]["authority.json"][
        "vm_inventory"
    ]["current_kernel"]
    _validate_restored_running_kernel(
        original_kernel, stage["kernel"], proof,
        stage["containers"]["worker"], manifest["account_entry"],
    )
    return {"stage": stage, "proof": proof, "startup_files": startup_files,
            "original_kernel": original_kernel}


def validate_group2_partial_finalize_result(
    validated: Any, evidence: Any
) -> dict[str, Any]:
    code = "group2_partial_finalize_result_invalid"
    validated = _closed_object(
        validated, {"request", "approval", "user_record", "artifacts", "context"}, code
    )
    evidence = _closed_object(
        evidence, {"current", "authority", "postgres", "token_health"}, code
    )
    history = _group2_partial_history(validated)
    previous = history["stage"]
    current = _validate_reconcile_stage(evidence["current"])
    validate_group2_reconcile_transition(
        previous, current,
        kernel_baseline=previous["kernel"], require_idle=False,
    )
    if (
        current["containers"] != previous["containers"]
        or current["db"] != validated["context"]["originals"]["db"]
        or current["files"] != previous["files"]
    ):
        raise CarryForwardError("group2_partial_finalize_current_changed")
    _validate_restored_running_kernel(
        history["original_kernel"], current["kernel"], history["proof"],
        current["containers"]["worker"],
        validated["context"]["originals"]["manifest"]["account_entry"],
    )
    for service in ("postgres", "rabbitmq", "control", "worker", "web"):
        item = current["containers"][service]
        if (
            item.get("status") != "running"
            or item.get("health") != "healthy"
            or item.get("restart_count") != 0
        ):
            raise CarryForwardError("group2_partial_finalize_current_changed")
    account = current["containers"]["account-web"]
    if account.get("status") == "running" or account != previous["containers"]["account-web"]:
        raise CarryForwardError("group2_partial_finalize_current_changed")
    platform = validated["context"]["validated_original"]["artifacts"]["platform.json"]
    postgres_format = platform["postgres_format"]
    if evidence["postgres"] != {
        "schema": "0040_issue152_dispositions",
        "binary_version": postgres_format["prior_binary_version"],
        "server_version": postgres_format["current_server_version"],
        "data_pg_version": postgres_format["data_pg_version"],
    } or evidence["token_health"] != {"status": 200, "database": True}:
        raise CarryForwardError("group2_partial_finalize_health_changed")
    authority = _closed_object(
        evidence["authority"],
        {"host_files", "host_installed", "vm_files", "vm_installed", "prior_success"},
        code,
    )
    if (
        set(authority["host_files"]) != {"config.json", "state.json", "attention.json"}
        or set(authority["host_installed"]) != GROUP2_CONTROLLER_FILES
        or set(authority["vm_files"]) != {"transaction.json", "current-sha", "phase.json"}
        or set(authority["vm_installed"]) != {"deploy.sh", "carry_forward.py", "verify.py", "assets.py"}
        or set(authority["prior_success"]) != {"host", "vm"}
    ):
        raise CarryForwardError(code)
    host_values = {
        name: _partial_embedded_json(value, code)[0]
        for name, value in authority["host_files"].items()
    }
    transaction, _ = _partial_embedded_json(authority["vm_files"]["transaction.json"], code)
    phase, _ = _partial_embedded_json(authority["vm_files"]["phase.json"], code)
    current_sha = _validate_embedded_bytes(authority["vm_files"]["current-sha"], code).decode().strip()
    if (
        transaction.get("phase") != "starting"
        or transaction.get("sha") != GROUP2_PARTIAL_FAILED_SHA
        or phase != validated["context"]["phase"]["phase"]
        or current_sha != GROUP2_FROM_SHA
    ):
        raise CarryForwardError("group2_partial_finalize_authority_changed")
    old_artifacts = validated["context"]["validated_original"]["artifacts"]
    old_request = validated["context"]["validated_original"]["request"]
    old_authority = old_artifacts["authority.json"]["snapshot"]
    diagnostic_host = validated["context"]["diagnostic_host_state"]
    diagnostic_vm = validated["context"]["diagnostic_vm_state"]
    config = host_values["config.json"]
    if (
        config.get("enabled") is not False
        or config.get("repo") != old_request["repo"]
        or config.get("pr") != old_request["pr"]
        or config.get("carry_forward") != old_authority["carry_reference"]
        or host_values["state.json"] != old_authority["state"]
        or host_values["attention.json"] != old_authority["attention"]
        or authority["host_files"]["state.json"]["sha256"]
        != diagnostic_host.get("state_sha256")
        or authority["host_files"]["attention.json"]["sha256"]
        != diagnostic_host.get("attention_sha256")
        or authority["host_installed"] != diagnostic_host.get("installed")
        or authority["vm_installed"] != diagnostic_vm.get("installed")
        or diagnostic_vm.get("current_sha") != GROUP2_FROM_SHA
    ):
        raise CarryForwardError("group2_partial_finalize_authority_changed")
    expected_vm_installed = _reconcile_vm_installed_files(old_artifacts["authority.json"])
    if authority["vm_installed"] != expected_vm_installed:
        raise CarryForwardError("group2_partial_finalize_authority_changed")
    prior = old_artifacts["prior-success.json"]
    expected_prior = {}
    for side in ("host", "vm"):
        expected_prior[side] = {
            name: (
                {"exists": False}
                if value == {"exists": False}
                else {"exists": True, "sha256": value.get("sha256")}
            )
            for name, value in prior[side].items()
        }
    if authority["prior_success"] != expected_prior:
        raise CarryForwardError("group2_partial_finalize_authority_changed")
    request = validated["request"]
    basis = {
        "history": "source_phase_and_saved_db_files",
        "startup_window": "outer_official_attempt",
        "before_worker": "saved_first_startup_container",
        "unavailable": [
            "historical_stage_logs", "historical_stage_kernel",
            "historical_stage_containers", "historical_stage_storage",
            "historical_stage_windows", "original_compose_startup_window",
        ],
    }
    receipt = {
        "schema": "group2-partial-finalize-receipt-v1",
        "result": "finalized_observed_prior_software",
        "incident_id": request["incident_id"], "finalize_id": request["finalize_id"],
        "original_request_digest": request["original_request_digest"],
        "request_digest": request["request_digest"], "tool_sha": request["tool"]["sha"],
        "failed_sha": GROUP2_PARTIAL_FAILED_SHA, "restored_sha": GROUP2_FROM_SHA,
        "basis": basis, "startup_proof": history["proof"],
        "startup_files": history["startup_files"],
        "evidence_digest": digest(evidence),
    }
    receipt["receipt_digest"] = digest(receipt)
    return receipt


def validate_group2_partial_finalize_preservation(
    chain: Any, original_reference: Any
) -> dict[str, Any]:
    return _validate_group2_partial_finalize_preservation(
        chain, original_reference, None, include_context=False
    )


def _post_finalize_reboot_parent(value: Any, code: str) -> dict[str, Any]:
    value = _closed_object(
        value, {"chain", "original_reference", "snapshot", "review"}, code
    )
    parent_validation = _validate_group2_partial_finalize_preservation(
        value["chain"], value["original_reference"], None, include_context=True
    )
    snapshot = parent_validation["snapshot"]
    review = _closed_object(
        value["review"], {"schema", "status", "snapshot_digest", "lineage"}, code
    )
    if (
        value["chain"].get("request", {}).get("incident_id")
        != GROUP2_PARTIAL_INCIDENT_ID
        or value["chain"].get("request", {}).get("finalize_id")
        != GROUP2_POST_FINALIZE_REBOOT_PARENT_FINALIZE_ID
        or value["snapshot"] != snapshot
        or review["schema"] != "group2-preservation-review-v1"
        or review["status"] != "APPROVED"
        or review["snapshot_digest"] != digest(snapshot)
        or review["lineage"] != snapshot["lineage"]
    ):
        raise CarryForwardError(code)
    return {"snapshot": snapshot, "review": review,
            "validated": parent_validation["validated"],
            "evidence": value["chain"]["result"]["evidence"],
            "receipt": value["chain"]["result"]["receipt"]}


def group2_post_finalize_reboot_expected_authority_paths(
    prior_success: Any,
) -> dict[str, list[str]]:
    """Derive the closed host/VM roster from fixed controls and the parent success."""
    code = "group2_reboot_prior_changed"
    prior_success = _closed_object(prior_success, {"schema", "host", "vm"}, code)
    if (
        prior_success["schema"] != "group2-prior-success-evidence-v1"
        or not isinstance(prior_success["host"], dict)
        or not isinstance(prior_success["vm"], dict)
    ):
        raise CarryForwardError(code)
    prefix = (
        f"incidents/{GROUP2_PARTIAL_INCIDENT_ID}/finalize/"
        f"{GROUP2_POST_FINALIZE_REBOOT_PARENT_FINALIZE_ID}/"
    )
    return {
        "host": sorted(
            GROUP2_POST_FINALIZE_REBOOT_HOST_CONTROL_FILES
            | set(prior_success["host"])
            | {prefix + name for name in GROUP2_POST_FINALIZE_REBOOT_HOST_FINALIZE_FILES}
        ),
        "vm": sorted(
            GROUP2_POST_FINALIZE_REBOOT_VM_CONTROL_FILES
            | set(prior_success["vm"])
            | {prefix + name for name in GROUP2_POST_FINALIZE_REBOOT_VM_FINALIZE_FILES}
        ),
    }


def _group2_reboot_file_descriptor(value: Any, code: str) -> dict[str, Any]:
    if value == {"exists": False}:
        return value
    value = _closed_object(
        value, {"exists", "kind", "sha256", "mode", "uid", "gid", "symlink"}, code
    )
    if (
        value["exists"] is not True
        or value["kind"] != "regular"
        or value["symlink"] is not False
        or not isinstance(value["mode"], int)
        or isinstance(value["mode"], bool)
        or value["mode"] & ~0o7777
        or any(not isinstance(value[key], int) or isinstance(value[key], bool)
               for key in ("uid", "gid"))
    ):
        raise CarryForwardError(code)
    _digest_text(value["sha256"], code)
    return value


def _group2_reboot_path_descriptor(path: Path) -> dict[str, Any]:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {"exists": False}
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise CarryForwardError("group2_reboot_authority_changed")
    output = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            output.update(chunk)
    return {
        "exists": True, "kind": "regular", "sha256": output.hexdigest(),
        "mode": stat.S_IMODE(info.st_mode), "uid": info.st_uid, "gid": info.st_gid,
        "symlink": False,
    }


def _verify_group2_reboot_vm_authority(
    root: Path, validated: dict[str, Any]
) -> dict[str, Any]:
    prior = validated["request"]["prior"]
    actual_files = {
        relative: _group2_reboot_path_descriptor(root / relative)
        for relative in validated["authority_paths"]["vm"]
    }
    actual_installed = {
        name: _group2_reboot_path_descriptor(root / name)
        for name in GROUP2_POST_FINALIZE_REBOOT_VM_INSTALLED
    }
    if (
        actual_files != prior["vm_files"]
        or actual_installed != prior["installed"]["vm"]
        or actual_files["prepare-sandbox-host.sh"]
        != validated["platform"]["prepare_script"]["vm"]
    ):
        raise CarryForwardError("group2_reboot_authority_changed")
    return {
        "host_files": prior["host_files"], "vm_files": actual_files,
        "installed": {
            "host": prior["installed"]["host"], "vm": actual_installed,
        },
    }


def validate_group2_post_finalize_reboot_request(
    request: Any, approval: Any, user_record: bytes, artifacts: Any
) -> dict[str, Any]:
    """Validate the one-boot closed request and independent execution approval."""
    code = "group2_reboot_request_invalid"
    request = _closed_object(
        request,
        {"schema", "incident_id", "finalize_id", "boot_id", "parent", "prior",
         "tool", "window", "evidence_files", "request_digest"},
        code,
    )
    if (
        request["schema"] != "group2-post-finalize-reboot-request-v1"
        or request["incident_id"] != GROUP2_PARTIAL_INCIDENT_ID
        or request["finalize_id"] != GROUP2_POST_FINALIZE_REBOOT_PARENT_FINALIZE_ID
        or BOOT_ID.fullmatch(str(request["boot_id"])) is None
        or request["request_digest"]
        != digest({key: value for key, value in request.items() if key != "request_digest"})
    ):
        raise CarryForwardError(code)
    parent = _closed_object(
        request["parent"],
        {"request_sha256", "receipt_sha256", "chain_sha256", "snapshot_sha256",
         "review_sha256"},
        code,
    )
    for value in parent.values():
        _digest_text(value, code)
    prior = _closed_object(
        request["prior"],
        {"sha", "schema", "host_files", "vm_files", "installed", "containers",
         "images", "storage", "parameters"},
        code,
    )
    if prior["sha"] != GROUP2_FROM_SHA or prior["schema"] != "0040_issue152_dispositions":
        raise CarryForwardError(code)
    for name in ("host_files", "vm_files", "installed", "containers", "images", "parameters"):
        if not isinstance(prior[name], dict):
            raise CarryForwardError(code)
    if not isinstance(prior["storage"], list):
        raise CarryForwardError(code)
    if set(prior["containers"]) != {
        "postgres", "rabbitmq", "control", "worker", "web", "account-web"
    }:
        raise CarryForwardError(code)
    for container in prior["containers"].values():
        if (
            not isinstance(container, dict)
            or not isinstance(container.get("container_id"), str)
            or not container["container_id"]
            or not isinstance(container.get("image_id"), str)
        ):
            raise CarryForwardError(code)
        _digest_text(container.get("inspect_static_digest"), code)
        _digest_text(container.get("stopped_state_digest"), code)
    tool = _closed_object(
        request["tool"],
        {"sha", "tree", "controller_files", "source_scope_digest",
         "review_report_sha256", "ci_evidence_sha256"},
        code,
    )
    _sha_text(tool["sha"], code)
    _sha_text(tool["tree"], code)
    if not isinstance(tool["controller_files"], dict) or set(
        tool["controller_files"]
    ) != GROUP2_CONTROLLER_FILES:
        raise CarryForwardError(code)
    for value in (
        *tool["controller_files"].values(), tool["source_scope_digest"],
        tool["review_report_sha256"], tool["ci_evidence_sha256"],
    ):
        _digest_text(value, code)
    window = _closed_object(
        request["window"],
        {"not_before_ns", "deadline_ns", "successor_deadline_ns"}, code,
    )
    if (
        not isinstance(window["not_before_ns"], int)
        or isinstance(window["not_before_ns"], bool)
        or not isinstance(window["deadline_ns"], int)
        or isinstance(window["deadline_ns"], bool)
        or window["deadline_ns"] <= window["not_before_ns"]
        or not isinstance(window["successor_deadline_ns"], int)
        or isinstance(window["successor_deadline_ns"], bool)
        or window["successor_deadline_ns"] <= window["deadline_ns"]
    ):
        raise CarryForwardError(code)
    evidence_files = _closed_object(
        request["evidence_files"], GROUP2_POST_FINALIZE_REBOOT_FILES, code
    )
    if not isinstance(artifacts, dict) or set(artifacts) != GROUP2_POST_FINALIZE_REBOOT_FILES:
        raise CarryForwardError(code)
    parsed, raw_hashes = {}, {}
    for name, value in artifacts.items():
        parsed[name], raw_hashes[name] = _reconcile_artifact_value(value)
        _digest_text(evidence_files[name], code)
        if evidence_files[name] != raw_hashes[name]:
            raise CarryForwardError("group2_reboot_artifact_digest_mismatch")
    parent_value = _post_finalize_reboot_parent(parsed["parent-finalize.json"], code)
    parent_chain = parsed["parent-finalize.json"]["chain"]
    if (
        parent["request_sha256"] != digest(parent_chain.get("request"))
        or parent["receipt_sha256"]
        != digest(parent_chain.get("result", {}).get("receipt"))
        or parent["chain_sha256"] != digest(parent_chain)
        or parent["snapshot_sha256"] != digest(parent_value["snapshot"])
        or parent["review_sha256"] != digest(parent_value["review"])
    ):
        raise CarryForwardError("group2_reboot_parent_changed")
    prior_success = _closed_object(
        parsed["prior-success.json"], {"schema", "host", "vm"}, code
    )
    parent_evidence = parent_value["evidence"]
    parent_current = parent_evidence.get("current")
    parent_authority = parent_evidence.get("authority")
    parent_context = parent_value["validated"].get("context")
    parent_originals = (
        parent_context.get("originals") if isinstance(parent_context, dict) else None
    )
    parent_manifest = (
        parent_originals.get("manifest") if isinstance(parent_originals, dict) else None
    )
    if (
        not isinstance(parent_current, dict)
        or not isinstance(parent_authority, dict)
        or not isinstance(parent_manifest, dict)
        or {
            service: {
                key: item for key, item in _reboot_expected_container(container).items()
                if key not in {"status", "health"}
            }
            for service, container in prior["containers"].items()
        }
        != {
            service: {
                key: item for key, item in container.items()
                if key not in {"status", "health"}
            }
            for service, container in parent_current.get("containers", {}).items()
            if isinstance(container, dict)
        }
        or parent_current.get("storage") != prior["storage"]
        or prior["images"]
        != {
            service: container.get("image_id")
            for service, container in parent_current.get("containers", {}).items()
        }
    ):
        raise CarryForwardError("group2_reboot_prior_changed")
    expected_prior_success = parent_authority.get("prior_success")
    normalized_prior_success = {
        side: {
            name: (
                {"exists": False}
                if descriptor == {"exists": False}
                else {"exists": True, "sha256": descriptor.get("sha256")}
            )
            for name, descriptor in prior_success[side].items()
        }
        for side in ("host", "vm")
    }
    if normalized_prior_success != expected_prior_success:
        raise CarryForwardError("group2_reboot_prior_changed")
    authority_paths = group2_post_finalize_reboot_expected_authority_paths(
        prior_success
    )
    platform = _closed_object(
        parsed["stopped-platform.json"],
        {"schema", "prior", "account_entry", "prior_nonces", "prepare_script"},
        code,
    )
    if (
        platform["schema"] != "group2-post-finalize-reboot-platform-v1"
        or platform["prior"] != prior
        or validate_group2_account_entry(platform["account_entry"])
        != parent_manifest.get("account_entry")
        or not isinstance(platform["prior_nonces"], list)
        or len(platform["prior_nonces"]) != len(set(platform["prior_nonces"]))
    ):
        raise CarryForwardError("group2_reboot_prior_changed")
    first_startup = parent_originals.get("first_startup")
    first_proof = (
        first_startup.get("proof") if isinstance(first_startup, dict) else None
    )
    nonce_values = {
        first_proof.get("nonce") if isinstance(first_proof, dict) else None,
        parent_value["receipt"].get("startup_proof", {}).get("nonce"),
    }
    if (
        any(not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{32}", value) is None
            for value in nonce_values)
        or platform["prior_nonces"] != sorted(nonce_values)
    ):
        raise CarryForwardError("group2_reboot_prior_changed")
    if (
        set(prior["host_files"]) != set(authority_paths["host"])
        or set(prior["vm_files"]) != set(authority_paths["vm"])
        or set(prior["installed"]) != {"host", "vm"}
        or set(prior["installed"]["host"]) != GROUP2_CONTROLLER_FILES
        or set(prior["installed"]["vm"])
        != GROUP2_POST_FINALIZE_REBOOT_VM_INSTALLED
    ):
        raise CarryForwardError("group2_reboot_prior_changed")
    for roster in (prior["host_files"], prior["vm_files"],
                   prior["installed"]["host"], prior["installed"]["vm"]):
        for descriptor in roster.values():
            _group2_reboot_file_descriptor(descriptor, code)
    if prior["host_files"]["attention.json"] != {"exists": False}:
        raise CarryForwardError("group2_reboot_prior_changed")
    for side in ("host", "vm"):
        for path, old in prior_success[side].items():
            descriptor = prior[f"{side}_files"][path]
            if (
                not isinstance(old, dict)
                or old.get("exists") != descriptor["exists"]
                or (
                    old.get("exists") is True
                    and old.get("sha256") != descriptor.get("sha256")
                )
            ):
                raise CarryForwardError("group2_reboot_prior_changed")
    prepare = _closed_object(
        platform["prepare_script"],
        {"sha256", "content_b64", "source_mode", "host", "vm", "parameters"},
        code,
    )
    host_prepare = _group2_reboot_file_descriptor(prepare["host"], code)
    vm_prepare = _group2_reboot_file_descriptor(prepare["vm"], code)
    prepare_raw = _validate_embedded_bytes(
        {"sha256": prepare["sha256"], "content_b64": prepare["content_b64"]}, code
    )
    if (
        prepare["source_mode"] != "100755"
        or prepare["sha256"] != GROUP2_POST_FINALIZE_REBOOT_PREPARE_SHA256
        or hashlib.sha256(prepare_raw).hexdigest()
        != GROUP2_POST_FINALIZE_REBOOT_PREPARE_SHA256
        or host_prepare != prior["host_files"]["prepare-sandbox-host.sh"]
        or vm_prepare != prior["vm_files"]["prepare-sandbox-host.sh"]
        or host_prepare.get("sha256") != prepare["sha256"]
        or vm_prepare.get("sha256") != prepare["sha256"]
        or not host_prepare.get("mode", 0) & 0o111
        or not vm_prepare.get("mode", 0) & 0o111
        or prepare["parameters"] != prior["parameters"].get("keeper")
    ):
        raise CarryForwardError("group2_reboot_prepare_script_changed")
    source_bundle = _closed_object(
        parsed["source-review.json"], {"schema", "source", "ci"}, code
    )
    source = source_bundle["source"]
    ci = source_bundle["ci"]
    partial_tool = {key: tool[key] for key in (
        "sha", "controller_files", "source_scope_digest",
        "review_report_sha256", "ci_evidence_sha256",
    )}
    _validate_group2_partial_tool(partial_tool, source, ci)
    if (
        source_bundle["schema"] != "group2-post-finalize-reboot-source-review-v1"
        or source["source_scope"]["to_tree"] != tool["tree"]
        or source["tool_review"]["sha256"] != tool["review_report_sha256"]
        or digest(ci) != tool["ci_evidence_sha256"]
    ):
        raise CarryForwardError("group2_reboot_source_invalid")
    scope = _closed_object(
        parsed["scope-approval.json"],
        {"schema", "status", "execution", "request_scope"},
        code,
    )
    if (
        scope["schema"] != "group2-post-finalize-reboot-scope-approval-v1"
        or scope["status"] != "APPROVED"
        or scope["execution"] is not False
        or scope["request_scope"] != "implementation-review-ci-only"
    ):
        raise CarryForwardError("group2_reboot_execution_approval_missing")
    approval = _closed_object(
        approval,
        {"schema", "request_digest", "tool_sha", "boot_id", "actions",
         "user_record_sha256"},
        code,
    )
    if (
        approval["schema"] != "group2-post-finalize-reboot-approval-v1"
        or approval["request_digest"] != request["request_digest"]
        or approval["tool_sha"] != tool["sha"]
        or approval["boot_id"] != request["boot_id"]
        or approval["actions"] != GROUP2_POST_FINALIZE_REBOOT_ACTIONS
    ):
        raise CarryForwardError("group2_reboot_execution_approval_missing")
    _digest_text(approval["user_record_sha256"], code)
    try:
        record = json.loads(user_record)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise CarryForwardError("group2_reboot_user_record_invalid") from error
    record = _closed_object(
        record,
        {"schema", "request_digest", "tool_sha", "boot_id", "actions",
         "presented_request", "user_reply"},
        "group2_reboot_user_record_invalid",
    )
    if (
        hashlib.sha256(user_record).hexdigest() != approval["user_record_sha256"]
        or record["schema"] != "group2-post-finalize-reboot-user-record-v1"
        or record["request_digest"] != request["request_digest"]
        or record["tool_sha"] != tool["sha"]
        or record["boot_id"] != request["boot_id"]
        or record["actions"] != GROUP2_POST_FINALIZE_REBOOT_ACTIONS
        or record["presented_request"] != " ".join(
            (request["request_digest"], tool["sha"], request["boot_id"],
             *GROUP2_POST_FINALIZE_REBOOT_ACTIONS)
        )
        or not isinstance(record["user_reply"], str)
        or not record["user_reply"].strip()
    ):
        raise CarryForwardError("group2_reboot_user_record_invalid")
    return {
        "request": request,
        "approval": approval,
        "user_record": record,
        "platform": platform,
        "authority_paths": authority_paths,
        "prior_success": prior_success,
        "parent": parent_value,
    }


def validate_group2_post_finalize_reboot_stopped(
    validated: Any, stopped: Any
) -> dict[str, Any]:
    code = "group2_reboot_stopped_invalid"
    validated = _closed_object(
        validated,
        {"request", "approval", "user_record", "platform", "authority_paths",
         "prior_success", "parent"}, code,
    )
    stopped = _closed_object(
        stopped,
        {"schema", "boot_id", "files", "logs", "containers", "images", "storage",
         "container_inspect", "authority", "postgres", "platform",
         "window_start_ns", "window_end_ns"},
        code,
    )
    request = validated["request"]
    parent = validated["parent"]["snapshot"]
    parent_current = validated["parent"]["evidence"]["current"]
    parent_postgres = validated["parent"]["evidence"]["postgres"]
    platform = _closed_object(
        stopped["platform"], {"boot_id", "unit"}, code
    )
    unit = _closed_object(
        platform["unit"], {"load_state", "active_state", "sub_state"}, code
    )
    _validate_group2_reboot_postgres(
        stopped["postgres"], parent_postgres,
        request["prior"]["containers"]["postgres"]["image_id"], running=False,
    )
    if (
        stopped["schema"] != "group2-post-finalize-reboot-stopped-v1"
        or stopped["boot_id"] != request["boot_id"]
        or platform["boot_id"] != request["boot_id"]
        or not isinstance(parent_current.get("kernel"), dict)
        or parent_current["kernel"].get("boot_id") == request["boot_id"]
        or unit != {"load_state": "not-found", "active_state": "inactive",
                    "sub_state": "dead"}
        or type(stopped["window_start_ns"]) is not int
        or type(stopped["window_end_ns"]) is not int
        or stopped["window_start_ns"] < request["window"]["not_before_ns"]
        or stopped["window_end_ns"] < stopped["window_start_ns"]
        or stopped["window_end_ns"] > request["window"]["deadline_ns"]
        or stopped["files"] != parent["files"]
        or stopped["containers"] != {
            name: _reboot_expected_container(item)
            for name, item in request["prior"]["containers"].items()
        }
        or stopped["images"] != request["prior"]["images"]
        or stopped["storage"] != request["prior"]["storage"]
        or stopped["authority"] != {
            "host_files": request["prior"]["host_files"],
            "vm_files": request["prior"]["vm_files"],
            "installed": request["prior"]["installed"],
            "keeper": "missing",
        }
    ):
        raise CarryForwardError(code)
    for service, item in stopped["containers"].items():
        if not isinstance(item, dict) or item.get("status") == "running":
            raise CarryForwardError(code)
        if service == "account-web" and item != _reboot_expected_container(
            request["prior"]["containers"][service]
        ):
            raise CarryForwardError(code)
        try:
            _validate_reboot_container_raw(
                stopped["container_inspect"][service], request["prior"]["containers"][service],
                item,
                stopped=True,
            )
        except (KeyError, CarryForwardError) as error:
            raise CarryForwardError(code) from error
    if set(stopped["container_inspect"]) != set(stopped["containers"]):
        raise CarryForwardError(code)
    try:
        _validate_log_link(
            parent_current["logs"], stopped["logs"],
            validated["platform"]["account_entry"]["profile_digest"],
        )
        _validate_reconcile_quiet_logs(stopped["logs"])
    except (KeyError, CarryForwardError) as error:
        raise CarryForwardError(code) from error
    return stopped


def _validate_group2_reboot_postgres(
    value: Any, expected: Any, image_id: str, *, running: bool,
    baseline: Any | None = None,
) -> dict[str, Any]:
    code = "group2_reboot_postgres_invalid"
    fields = {
        "database_read", "binary_version", "data_pg_version",
        "image_id", "rootfs_layers",
    }
    if running:
        fields.update(("schema", "server_version"))
    value = _closed_object(value, fields, code)
    expected = _closed_object(
        expected, {"schema", "binary_version", "server_version", "data_pg_version"},
        code,
    )
    layers = value["rootfs_layers"]
    if (
        value["database_read"] is not running
        or value["binary_version"] != expected["binary_version"]
        or value["data_pg_version"] != expected["data_pg_version"]
        or value["image_id"] != image_id
        or not isinstance(layers, list) or not layers
        or any(not isinstance(layer, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", layer) is None
               for layer in layers)
        or (running and (
            value["schema"] != expected["schema"]
            or value["server_version"] != expected["server_version"]
        ))
    ):
        raise CarryForwardError(code)
    if baseline is not None:
        if not isinstance(baseline, dict):
            raise CarryForwardError(code)
        baseline_value = _validate_group2_reboot_postgres(
            baseline, expected, image_id, running=bool(baseline.get("database_read"))
        )
        if (
            value["rootfs_layers"] != baseline_value["rootfs_layers"]
            or value["binary_version"] != baseline_value["binary_version"]
            or value["data_pg_version"] != baseline_value["data_pg_version"]
        ):
            raise CarryForwardError(code)
    return value


def _group2_admission_ns(value: Any, code: str) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, dt.datetime):
        value = value.isoformat()
    if isinstance(value, dict) and set(value) == {"$datetime"}:
        value = value["$datetime"]
    if not isinstance(value, str):
        raise CarryForwardError(code)
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise CarryForwardError(code) from error
    if parsed.tzinfo is None:
        raise CarryForwardError(code)
    epoch = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
    delta = parsed.astimezone(dt.timezone.utc) - epoch
    return (
        delta.days * 86_400 * 1_000_000_000
        + delta.seconds * 1_000_000_000
        + delta.microseconds * 1_000
    )


def derive_group2_reboot_configuration(
    control_inspect: Any, worker_inspect: Any, workers: Any
) -> dict[str, Any]:
    code = "group2_reboot_configuration_invalid"

    def env_values(raw: Any, *, folded: bool) -> tuple[dict[str, str], list[str]]:
        config = raw.get("Config") if isinstance(raw, dict) else None
        env = config.get("Env") if isinstance(config, dict) else None
        if not isinstance(env, list) or any(not isinstance(item, str) or "=" not in item for item in env):
            raise CarryForwardError(code)
        result: dict[str, str] = {}
        original: dict[str, str] = {}
        for item in env:
            name, value = item.split("=", 1)
            key = name.casefold() if folded else name
            if not name:
                raise CarryForwardError(code)
            if key in result:
                if not folded or original[key] == name or result[key] != value:
                    raise CarryForwardError(code)
                continue
            result[key] = value
            original[key] = name
        return result, env

    def integer(env: dict[str, str], name: str, default: int, minimum: int,
                maximum: int | None = None) -> int:
        raw = env.get(name.casefold(), str(default))
        if re.fullmatch(r"[+]?[0-9]+", raw) is None:
            raise CarryForwardError(code)
        value = int(raw)
        if value < minimum or (maximum is not None and value > maximum):
            raise CarryForwardError(code)
        return value

    control, control_raw = env_values(control_inspect, folded=True)
    worker, worker_raw = env_values(worker_inspect, folded=False)
    policies = {
        "webhook": {
            "days": integer(control, "DLR_EXECUTION_RETENTION_WEBHOOK_DAYS", 30, 1),
            "max_per_adapter": integer(control, "DLR_EXECUTION_RETENTION_WEBHOOK_MAX_PER_ADAPTER", 100, 1),
        },
        "manual": {
            "days": integer(control, "DLR_EXECUTION_RETENTION_TASK_DAYS", 30, 1),
            "max_per_adapter": integer(control, "DLR_EXECUTION_RETENTION_TASK_MAX_PER_ADAPTER", 1000, 1),
        },
        "schedule": {
            "days": integer(control, "DLR_EXECUTION_RETENTION_SCHEDULE_DAYS", 90, 1),
            "max_per_adapter": integer(control, "DLR_EXECUTION_RETENTION_SCHEDULE_MAX_PER_ADAPTER", 1000, 1),
        },
    }
    alert = integer(control, "DLR_ARTIFACT_DELETE_ALERT_THRESHOLD", 5, 1, 100)
    def floating(name: str, default: float, minimum: float, maximum: float) -> float:
        raw = control.get(name.casefold(), str(default))
        try:
            value = float(raw)
        except ValueError as error:
            raise CarryForwardError(code) from error
        if not math.isfinite(value) or value <= minimum or value > maximum:
            raise CarryForwardError(code)
        return value

    worker_name = worker.get("DLR_WORKER_NAME", "worker-1")
    if not worker_name:
        raise CarryForwardError(code)
    slots_raw = worker.get("DLR_WORKER_EXECUTION_SLOTS", "2")
    if re.fullmatch(r"[+-]?[0-9]+", slots_raw) is None:
        raise CarryForwardError(code)
    slots = max(1, int(slots_raw))
    if not isinstance(workers, list) or any(
        not isinstance(row, dict) or set(row) != {"id", "name"} for row in workers
    ):
        raise CarryForwardError(code)
    matches = [row for row in workers if row["name"] == worker_name]
    if len(matches) != 1 or not isinstance(matches[0]["id"], int) or isinstance(matches[0]["id"], bool):
        raise CarryForwardError(code)
    rabbit_url = control.get("dlr_rabbitmq_url")
    worker_url = worker.get("DLR_RABBITMQ_URL")
    if not rabbit_url or not worker_url:
        raise CarryForwardError(code)
    parsed = urlsplit(rabbit_url)
    raw_vhost = control.get("dlr_rabbitmq_vhost")
    if raw_vhost is not None:
        if parsed.path not in {"", "/"} or not raw_vhost:
            raise CarryForwardError(code)
        effective = parsed._replace(path=f"/{quote(raw_vhost, safe='')}")
        vhost = raw_vhost
    else:
        effective = parsed
        vhost = unquote(parsed.path[1:] if parsed.path.startswith("/") else parsed.path) or "/"
    worker_parsed = urlsplit(worker_url)
    worker_vhost = unquote(
        worker_parsed.path[1:] if worker_parsed.path.startswith("/") else worker_parsed.path
    ) or "/"
    try:
        effective_port = effective.port
        worker_port = worker_parsed.port
    except ValueError as error:
        raise CarryForwardError(code) from error
    effective_port = effective_port or (5671 if effective.scheme == "amqps" else 5672)
    worker_port = worker_port or (5671 if worker_parsed.scheme == "amqps" else 5672)
    if (
        effective.scheme not in {"amqp", "amqps"}
        or not effective.hostname or not effective.username
        or not effective.password or unquote(effective.username).casefold() == "guest"
        or not worker_parsed.password
        or (worker_parsed.scheme, worker_parsed.hostname, worker_port,
            unquote(worker_parsed.username or ""), unquote(worker_parsed.password),
            worker_vhost)
        != (effective.scheme, effective.hostname, effective_port,
            unquote(effective.username or ""), unquote(effective.password), vhost)
    ):
        raise CarryForwardError(code)
    queue_max_length = integer(control, "DLR_RABBITMQ_QUEUE_MAX_LENGTH", 2000, 1, 1_000_000)
    queue_max_bytes = integer(control, "DLR_RABBITMQ_QUEUE_MAX_BYTES", 64 * 1024 * 1024, 1024, 10 * 1024 * 1024 * 1024)
    delivery_limit = integer(control, "DLR_RABBITMQ_DELIVERY_LIMIT", 5, 1, 100)
    consumer_timeout = integer(control, "DLR_RABBITMQ_CONSUMER_TIMEOUT_MS", 300_000, 1000, 900_000)
    retry_base = floating("DLR_RABBITMQ_RETRY_BASE_SECONDS", 1.0, 0, 300)
    retry_max = floating("DLR_RABBITMQ_RETRY_MAX_SECONDS", 60.0, 0, 3600)
    if retry_base > retry_max:
        raise CarryForwardError(code)
    delayed_type = control.get("dlr_rabbitmq_delayed_retry_type", "all")
    if delayed_type not in {"all", "returned"}:
        raise CarryForwardError(code)
    common = {
        "x-queue-type": "quorum", "x-max-length": queue_max_length,
        "x-max-length-bytes": queue_max_bytes, "x-overflow": "reject-publish",
        "x-delivery-limit": delivery_limit,
    }
    work_arguments = {
        **common, "x-dead-letter-exchange": "dlr.execution.infrastructure.dlx",
        "x-dead-letter-routing-key": "infrastructure",
        "x-dead-letter-strategy": "at-least-once",
        "x-consumer-timeout": consumer_timeout,
        "x-delayed-retry-type": delayed_type,
        "x-delayed-retry-min": max(1, int(retry_base * 1000)),
        "x-delayed-retry-max": max(1, int(retry_max * 1000)),
    }
    queue_scope = [
        {"vhost": vhost, "name": f"dlr.worker.{row['id']}.q", "type": "quorum",
         "state": "running", "arguments": work_arguments}
        for row in workers
    ] + [{
        "vhost": vhost, "name": "dlr.execution.infrastructure.dlq",
        "type": "quorum", "state": "running", "arguments": common,
    }]
    source_digest = digest({
        "derivation": "group2-old3d-env-and-worker-v1",
        "control_inspect_digest": digest(control_inspect),
        "worker_inspect_digest": digest(worker_inspect),
        "control_env_digest": digest(control_raw),
        "worker_env_digest": digest(worker_raw),
        "source_blobs": GROUP2_POST_FINALIZE_REBOOT_SOURCES,
        "workers_raw_digest": digest(workers),
    })
    settings = {
        "master_key_configured": bool(control.get("dlr_master_key", "")),
        "retention": policies, "artifact_delete_alert_threshold": alert,
        "orphan_grace_seconds": 300, "journal_retry_backoff_seconds": 60,
        "source_digest": source_digest,
    }
    return {
        "schema": "group2-old3d-derived-configuration-v1",
        "control_inspect": control_inspect, "worker_inspect": worker_inspect,
        "raw_workers": workers,
        "worker_name": worker_name, "worker_id": matches[0]["id"],
        "execution_slots": slots, "effective_settings": settings,
        "rabbitmq": {
            "vhost": vhost, "scheme": effective.scheme,
            "host": effective.hostname,
            "port": effective_port,
            "user": unquote(effective.username or ""), "queues": queue_scope,
        },
        "source_digest": source_digest,
    }


def derive_group2_reboot_start_admission(
    raw_tables: Any, raw_files: Any, settings: Any, *, t0_ns: int,
    horizon_ns: int, preserved_queued_ids: list[int], worker_id: int,
) -> dict[str, Any]:
    """Derive old-3d immediate and timed mutations from fixed raw facts."""
    code = "group2_reboot_start_admission_invalid"
    if not isinstance(raw_tables, dict) or set(raw_tables) != GROUP2_REBOOT_ADMISSION_TABLES:
        raise CarryForwardError(code)
    if any(not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows)
           for rows in raw_tables.values()):
        raise CarryForwardError(code)
    for name, rows in raw_tables.items():
        expected_columns = GROUP2_REBOOT_ADMISSION_COLUMNS.get(name)
        primary_key = GROUP2_REBOOT_ADMISSION_PRIMARY_KEYS[name]
        if (
            any(
                (set(row) != expected_columns if expected_columns is not None
                 else "id" not in row)
                for row in rows
            )
            or len({canonical_bytes([row[key] for key in primary_key]) for row in rows})
            != len(rows)
        ):
            raise CarryForwardError(code)
    settings = _closed_object(
        settings,
        {"master_key_configured", "retention", "artifact_delete_alert_threshold",
         "orphan_grace_seconds", "journal_retry_backoff_seconds", "source_digest"},
        code,
    )
    if (
        not isinstance(settings["master_key_configured"], bool)
        or settings["orphan_grace_seconds"] != 300
        or settings["journal_retry_backoff_seconds"] != 60
        or not isinstance(settings["artifact_delete_alert_threshold"], int)
        or isinstance(settings["artifact_delete_alert_threshold"], bool)
        or settings["artifact_delete_alert_threshold"] <= 0
    ):
        raise CarryForwardError(code)
    _digest_text(settings["source_digest"], code)
    policies = _closed_object(settings["retention"], {"webhook", "manual", "schedule"}, code)
    for policy in policies.values():
        policy = _closed_object(policy, {"days", "max_per_adapter"}, code)
        if any(not isinstance(policy[key], int) or isinstance(policy[key], bool)
               or policy[key] < 0 for key in policy):
            raise CarryForwardError(code)
    hard: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []

    def immediate(kind: str, origin: str, primary_key: Any) -> None:
        events.append({"kind": kind, "origin": origin, "primary_key": primary_key,
                       "threshold_ns": t0_ns, "operator": "immediate"})

    def timed(kind: str, origin: str, primary_key: Any, threshold: Any,
              operator: str = "le") -> None:
        events.append({"kind": kind, "origin": origin, "primary_key": primary_key,
                       "threshold_ns": _group2_admission_ns(threshold, code),
                       "operator": operator})

    if worker_id not in {row["id"] for row in raw_tables["workers"]}:
        hard.append({"kind": "worker_identity_missing", "primary_key": worker_id})
    if sum(row["name"] == "expired_attempts"
           for row in raw_tables["runtime_reconciliation_cursors"]) != 1:
        hard.append({"kind": "attempt_cursor_missing", "primary_key": "expired_attempts"})

    users = raw_tables["users"]
    credentials = raw_tables["credentials"]
    if not any(row.get("username") == "admin" for row in users):
        immediate("admin_missing", "users", "admin")
    if settings["master_key_configured"]:
        for name in ("demo-passwd", "demo-token"):
            if not any(row.get("name") == name for row in credentials):
                immediate("demo_credential_missing", "credentials", name)
    for row in raw_tables["adapter_schedules"]:
        if row.get("enabled") is True:
            hard.append({"kind": "enabled_schedule", "primary_key": row.get("id")})
    executions = {row.get("id"): row for row in raw_tables["executions"]}
    adapters = {row.get("id") for row in raw_tables["adapters"]}
    adapter_expected = {adapter: [0, 0] for adapter in adapters}
    rabbit_active = {"queued", "running", "retry_wait"}
    retention_terminal = {"succeeded", "dead_letter", "cancelled", "expired"}
    idempotency_terminal = retention_terminal | {"failed", "timeout"}
    for row in executions.values():
        if row.get("dispatch_backend") == "rabbitmq" and row.get("status") in rabbit_active:
            bucket = adapter_expected.setdefault(row.get("adapter_id"), [0, 0])
            bucket[0] += 1; bucket[1] += int(row.get("logical_input_bytes") or 0)
        if (row.get("dispatch_backend") == "rabbitmq"
                and row.get("status") in retention_terminal
                and row.get("admission_released_at") is None):
            immediate("terminal_rabbit_unreleased", "executions", row.get("id"))
    counters = {row.get("adapter_id"): row for row in raw_tables["adapter_execution_admission"]}
    for adapter in sorted(adapters):
        row = counters.get(adapter); expected = adapter_expected.get(adapter, [0, 0])
        if row is None or [row.get("outstanding_count"), row.get("outstanding_bytes")] != expected:
            immediate("adapter_counter_mismatch", "adapter_execution_admission", adapter)
    global_rows = raw_tables["global_execution_admission"]
    expected_global = [sum(x[0] for x in adapter_expected.values()),
                       sum(x[1] for x in adapter_expected.values())]
    if len(global_rows) != 1 or global_rows[0].get("singleton_key") != "global" or [
        global_rows[0].get("outstanding_count"), global_rows[0].get("outstanding_bytes")
    ] != expected_global:
        immediate("global_counter_mismatch", "global_execution_admission", "global")
    for row in raw_tables["execution_attempts"]:
        if row.get("status") in {"claimed", "running"}:
            hard.append({"kind": "active_attempt", "primary_key": row.get("id")})
            timed("expired_attempt", "execution_attempts", row.get("id"), row.get("lease_expires_at"))
    for row in raw_tables["adapter_execution_slots"]:
        if row.get("active_attempt_id") is not None:
            hard.append({"kind": "occupied_slot", "primary_key": [row.get("adapter_id"), row.get("slot_no")]})
    for row in executions.values():
        if row.get("dispatch_backend") == "rabbitmq" and row.get("status") == "retry_wait":
            hard.append({"kind": "retry_wait", "primary_key": row.get("id")})
            if row.get("next_attempt_at") is not None:
                timed("retry_dispatch", "executions", row.get("id"), row["next_attempt_at"])
    holds = raw_tables["execution_artifact_holds"]
    for row in holds:
        if row.get("purged_at") is None:
            timed("expire_artifact_hold", "execution_artifact_holds", row.get("id"), row.get("expires_at"))
    outbox = raw_tables["execution_outbox"]
    for row in outbox:
        if row.get("status") == "pending":
            hard.append({"kind": "pending_outbox", "primary_key": row.get("id")})
            threshold = row.get("available_at")
            if row.get("lease_expires_at") is not None and _group2_admission_ns(row["lease_expires_at"], code) > _group2_admission_ns(threshold, code):
                threshold = row["lease_expires_at"]
            timed("outbox_publish", "execution_outbox", row.get("id"), threshold)
    for execution_id in preserved_queued_ids:
        row = executions.get(execution_id)
        matches = [item for item in outbox if item.get("execution_id") == execution_id
                   and item.get("dispatch_generation") == (row or {}).get("dispatch_generation")]
        if row is None or len(matches) != 1 or matches[0].get("status") != "published":
            hard.append({"kind": "current_generation_unpublished", "primary_key": execution_id})
    idempotency = raw_tables["execution_idempotency_records"]
    idem_by_execution = {row.get("execution_id") for row in idempotency}
    pending_by_execution = {row.get("execution_id") for row in outbox if row.get("status") == "pending"}
    for row in idempotency:
        execution = executions.get(row.get("execution_id"))
        if execution is not None and execution.get("status") in idempotency_terminal:
            timed("expired_idempotency", "execution_idempotency_records", row.get("id"), row.get("expires_at"))
    eligible: list[tuple[dict[str, Any], dict[str, int]]] = []
    day_ns = 86_400 * 1_000_000_000
    for row in executions.values():
        policy = policies.get(row.get("trigger"))
        if (policy is not None and row.get("status") in retention_terminal
                and row.get("admission_released_at") is not None
                and row.get("id") not in idem_by_execution
                and row.get("id") not in pending_by_execution):
            eligible.append((row, policy))
            threshold = _group2_admission_ns(row.get("created_at"), code) + policy["days"] * day_ns
            timed("execution_retention_age", "executions", row.get("id"), threshold, "lt")
    grouped: dict[tuple[Any, Any], list[tuple[dict[str, Any], dict[str, int]]]] = {}
    for item in eligible:
        grouped.setdefault((item[0].get("adapter_id"), item[0].get("trigger")), []).append(item)
    for rows in grouped.values():
        rows.sort(key=lambda item: (_group2_admission_ns(item[0].get("created_at"), code), item[0].get("id")), reverse=True)
        max_rows = rows[0][1]["max_per_adapter"] if rows else 0
        for row, _policy in rows[max_rows:]:
            immediate("execution_retention_count", "executions", row.get("id"))
    artifacts = {row.get("id"): row for row in raw_tables["managed_input_artifacts"]}
    bindings = raw_tables["adapter_input_artifact_bindings"]
    configs = {row.get("adapter_id"): row for row in raw_tables["adapter_input_configs"]}
    bound_ids = {row.get("artifact_id") for row in bindings}
    for row in raw_tables["managed_input_upload_reservations"]:
        if row.get("status") == "ACTIVE":
            timed("upload_reservation_expiry", "managed_input_upload_reservations", row.get("id"), row.get("expires_at"))
    for binding in bindings:
        artifact = artifacts.get(binding.get("artifact_id")); config = configs.get(binding.get("adapter_id"))
        if artifact is None or config is None or artifact.get("adapter_id") != binding.get("adapter_id"):
            hard.append({"kind": "invalid_binding", "primary_key": [binding.get("adapter_id"), binding.get("artifact_id")]})
        elif config.get("source_type") == "managed_files":
            checksum = artifact.get("sha256")
            if artifact.get("status") != "READY" or not isinstance(checksum, str) or re.fullmatch(r"[0-9a-fA-F]{64}", checksum) is None:
                immediate("binding_invalid", "adapter_input_artifact_bindings", [binding.get("adapter_id"), binding.get("artifact_id")])
            elif artifact.get("expires_at") is not None:
                timed("binding_expiry", "managed_input_artifacts", artifact.get("id"), artifact["expires_at"])
    for row in artifacts.values():
        if row.get("status") == "STAGED" and row.get("id") not in bound_ids and row.get("expires_at") is not None:
            timed("staged_artifact_expiry", "managed_input_artifacts", row.get("id"), row["expires_at"])
    leases = raw_tables["execution_input_artifact_leases"]
    alert = settings["artifact_delete_alert_threshold"]
    for row in artifacts.values():
        candidate = row.get("status") in {"PENDING_DELETE", "DELETING"} or (row.get("status") == "DELETE_FAILED" and int(row.get("delete_attempts") or 0) < alert)
        protected = any(lease.get("artifact_id") == row.get("id") and (executions.get(lease.get("execution_id")) or {}).get("status") in {"pending", "queued", "running", "retry_wait"} for lease in leases)
        if candidate and not protected:
            thresholds = [t0_ns if row.get("delete_lease_until") is None else _group2_admission_ns(row["delete_lease_until"], code)]
            thresholds.extend(_group2_admission_ns(h["expires_at"], code) for h in holds if h.get("artifact_id") == row.get("id") and h.get("purged_at") is None)
            timed("artifact_delete", "managed_input_artifacts", row.get("id"), max(thresholds))
    for row in raw_tables["artifact_deletion_jobs"]:
        if row.get("status") in {"PENDING", "DELETING"} or (row.get("status") == "DELETE_FAILED" and int(row.get("delete_attempts") or 0) < alert):
            timed("deletion_job", "artifact_deletion_jobs", row.get("id"), row.get("delete_lease_until") or t0_ns)
    if not isinstance(raw_files, dict) or set(raw_files) != {"journal_facts", "artifact_store"}:
        raise CarryForwardError(code)
    journal = _closed_object(raw_files["journal_facts"], {"attempt", "cleanup", "sandbox_recovery"}, code)
    if any(journal.values()):
        hard.append({"kind": "worker_journal_candidate", "primary_key": "journal"})
    known = {row.get("storage_key") for row in artifacts.values() if row.get("status") != "DELETED"} | {row.get("storage_key") for row in raw_tables["artifact_deletion_jobs"]}
    if not isinstance(raw_files["artifact_store"], list):
        raise CarryForwardError(code)
    for item in raw_files["artifact_store"]:
        if not isinstance(item, dict) or item.get("storage_key") is None or item.get("mtime_ns") is None:
            raise CarryForwardError(code)
        if item["storage_key"] not in known:
            timed("artifact_orphan", "artifact_store", item["storage_key"], int(item["mtime_ns"]) + 300 * 1_000_000_000, "lt")
    for row in raw_tables["worker_cleanup_requests"]:
        if row.get("worker_id") == worker_id and row.get("status") == "pending":
            immediate("adapter_cleanup_claim", "worker_cleanup_requests", row.get("id"))
        if row.get("status") == "running":
            hard.append({"kind": "cleanup_in_progress", "primary_key": row.get("id")})
    events.sort(key=lambda item: (item["threshold_ns"], item["kind"], str(item["primary_key"])))
    current = [item for item in events if item["operator"] == "immediate"
               or item["threshold_ns"] < t0_ns
               or item["operator"] == "le" and item["threshold_ns"] == t0_ns]
    horizon = [item for item in events if item["operator"] == "immediate" or item["threshold_ns"] <= horizon_ns]
    earliest = ({"kind": "none", "origins": []} if not events else {"kind": "bounded", "event": events[0]})
    return {"hard_precondition_failures": hard, "current": current,
            "within_horizon": horizon, "earliest_natural_change": earliest,
            "evaluated_through_ns": horizon_ns,
            "source_digest": digest({"tables": raw_tables, "files": raw_files,
                                     "settings": settings})}


def _validate_group2_reboot_raw_database_binding(
    raw_tables: dict[str, list[dict[str, Any]]], database_rows: Any, database: Any
) -> None:
    code = "group2_reboot_start_admission_invalid"
    if not isinstance(database, dict) or not isinstance(database_rows, dict):
        raise CarryForwardError(code)
    projection = database.get("projection")
    asset_projection = database.get("asset_projection")
    if not isinstance(projection, dict) or not isinstance(asset_projection, dict):
        raise CarryForwardError(code)
    expected_names = set(projection) | set(asset_projection)
    if set(database_rows) != expected_names:
        raise CarryForwardError(code)
    if (
        project_rows(database_rows, required=tuple(projection)) != projection
        or project_rows(database_rows, required=tuple(asset_projection))
        != asset_projection
    ):
        raise CarryForwardError(code)
    for name in set(raw_tables) & set(database_rows):
        rows = raw_tables[name]
        source = database_rows[name]
        primary_key = source.get("primary_key") if isinstance(source, dict) else None
        full_rows = source.get("rows") if isinstance(source, dict) else None
        if not isinstance(primary_key, list) or not isinstance(full_rows, list):
            raise CarryForwardError(code)
        full_by_pk = {
            canonical_bytes([row[key] for key in primary_key]): canonical(row)
            for row in full_rows if isinstance(row, dict)
        }
        if len(full_by_pk) != len(full_rows) or len(rows) != len(full_rows):
            raise CarryForwardError(code)
        for row in rows:
            try:
                full = full_by_pk[
                    canonical_bytes([row[key] for key in primary_key])
                ]
            except (KeyError, TypeError) as error:
                raise CarryForwardError(code) from error
            if any(canonical(item) != full.get(key) for key, item in row.items()):
                raise CarryForwardError(code)


def _validate_group2_reboot_mutation_guard(
    value: Any, raw_tables: dict[str, list[dict[str, Any]]] | None = None
) -> dict[str, Any]:
    code = "group2_reboot_start_admission_invalid"
    if not isinstance(value, dict) or set(value) != set(GROUP2_REBOOT_MUTATION_GUARD_TABLES):
        raise CarryForwardError(code)
    for name, source in value.items():
        source = _closed_object(source, {"columns", "primary_key", "rows"}, code)
        columns, primary_key, rows = source["columns"], source["primary_key"], source["rows"]
        if (
            not isinstance(columns, list) or not columns
            or len(columns) != len(set(columns)) or any(not isinstance(item, str) for item in columns)
            or not isinstance(primary_key, list) or not primary_key
            or not set(primary_key).issubset(columns)
            or not isinstance(rows, list)
            or any(not isinstance(row, dict) or set(row) != set(columns) for row in rows)
            or len({canonical_bytes([row[key] for key in primary_key]) for row in rows}) != len(rows)
        ):
            raise CarryForwardError(code)
        if raw_tables is not None:
            full = {
                canonical_bytes([row[key] for key in primary_key]): canonical(row)
                for row in rows
            }
            subset = raw_tables.get(name)
            if not isinstance(subset, list) or len(subset) != len(rows):
                raise CarryForwardError(code)
            for row in subset:
                try:
                    target = full[canonical_bytes([row[key] for key in primary_key])]
                except (KeyError, TypeError) as error:
                    raise CarryForwardError(code) from error
                if any(canonical(item) != target.get(key) for key, item in row.items()):
                    raise CarryForwardError(code)
    return value


def validate_group2_reboot_start_admission(
    value: Any, database: Any, parameters: Any, deadline_ns: int,
    successor_deadline_ns: int, rabbitmq_image_id: str,
    expected_queued_ids: list[int] | None = None,
    prior_containers: dict[str, Any] | None = None,
) -> dict[str, Any]:
    code = "group2_reboot_start_admission_invalid"
    value = _closed_object(
        value,
        {"schema", "database_digest", "parameters_digest", "clock", "deadline_ns",
         "successor_deadline_ns", "sources", "raw_tables", "database_rows", "raw_files",
         "configuration", "effective_settings", "preserved_queued_ids", "worker_id", "rabbitmq",
         "mutation_guard_rows", "derived"}, code,
    )
    clock = _closed_object(value["clock"], {"db_utc_ns", "vm_lower_ns", "vm_upper_ns"}, code)
    if (
        value["schema"] != "group2-post-finalize-reboot-start-admission-v1"
        or value["database_digest"] != digest(database)
        or value["parameters_digest"] != digest(parameters)
        or value["deadline_ns"] != deadline_ns
        or value["successor_deadline_ns"] != successor_deadline_ns
        or not all(isinstance(clock[key], int) and not isinstance(clock[key], bool) for key in clock)
        or not clock["vm_lower_ns"] <= clock["db_utc_ns"] <= clock["vm_upper_ns"]
        or not clock["db_utc_ns"] <= deadline_ns < successor_deadline_ns
        or value["sources"] != GROUP2_POST_FINALIZE_REBOOT_SOURCES
        or not isinstance(value["preserved_queued_ids"], list)
        or any(not isinstance(item, int) or isinstance(item, bool) for item in value["preserved_queued_ids"])
        or (expected_queued_ids is not None
            and value["preserved_queued_ids"] != expected_queued_ids)
        or not isinstance(value["worker_id"], int) or isinstance(value["worker_id"], bool)
        or not isinstance(value["effective_settings"], dict)
        or value["worker_id"] not in {
            row.get("id") for row in value["raw_tables"].get("workers", [])
            if isinstance(row, dict)
        }
    ):
        raise CarryForwardError(code)
    configuration = derive_group2_reboot_configuration(
        value["configuration"].get("control_inspect")
        if isinstance(value["configuration"], dict) else None,
        value["configuration"].get("worker_inspect")
        if isinstance(value["configuration"], dict) else None,
        value["raw_tables"]["workers"],
    )
    if (
        value["configuration"] != configuration
        or value["worker_id"] != configuration["worker_id"]
        or value["effective_settings"] != configuration["effective_settings"]
    ):
        raise CarryForwardError(code)
    _validate_group2_reboot_raw_database_binding(
        value["raw_tables"], value["database_rows"], database
    )
    _validate_group2_reboot_mutation_guard(
        value["mutation_guard_rows"], value["raw_tables"]
    )
    derived = derive_group2_reboot_start_admission(
        value["raw_tables"], value["raw_files"], value["effective_settings"],
        t0_ns=clock["db_utc_ns"], horizon_ns=successor_deadline_ns,
        preserved_queued_ids=value["preserved_queued_ids"], worker_id=value["worker_id"],
    )
    if value["derived"] != derived or derived["hard_precondition_failures"] or derived["current"] or derived["within_horizon"]:
        raise CarryForwardError("group2_reboot_consumption_candidate")
    rabbit = _closed_object(
        value["rabbitmq"], {"image_id", "version", "plugins", "config_digest",
         "queue_scope", "raw_response_digest", "queues",
         "topology", "feature_flags", "external_before", "external_after", "network"}, code,
    )
    rabbit_configuration = configuration["rabbitmq"]
    expected_queues = rabbit_configuration["queues"]
    if (
        rabbit["image_id"] != rabbitmq_image_id or rabbit["version"] != "4.3.5"
        or rabbit["config_digest"] != digest(rabbit_configuration)
        or rabbit["queue_scope"] != expected_queues or not isinstance(expected_queues, list)
        or not expected_queues
        or not isinstance(rabbit["plugins"], list)
        or DIGEST.fullmatch(str(rabbit["raw_response_digest"])) is None
        or not isinstance(rabbit["queues"], list)
        or not isinstance(rabbit["topology"], dict)
        or not isinstance(rabbit["feature_flags"], list)
        or rabbit["raw_response_digest"]
        != digest({"queues": rabbit["queues"], "topology": rabbit["topology"],
                   "feature_flags": rabbit["feature_flags"],
                   "external_before": rabbit["external_before"],
                   "external_after": rabbit["external_after"],
                   "network": rabbit["network"]})
    ):
        raise CarryForwardError("group2_reboot_queue_unsafe")
    _validate_group2_rabbit_topology(
        rabbit["topology"], configuration, rabbit["plugins"]
    )
    flags = {}
    for row in rabbit["feature_flags"]:
        row = _closed_object(row, {"name", "state"}, code)
        if not isinstance(row["name"], str) or row["name"] in flags \
                or row["state"] not in {"enabled", "disabled", "unavailable"}:
            raise CarryForwardError("group2_reboot_queue_unsafe")
        flags[row["name"]] = row["state"]
    if any(flags.get(name) != "enabled" for name in (
        "feature_flags_v2", "quorum_queue", "stream_queue", "rabbitmq_4.3.0"
    )):
        raise CarryForwardError("group2_reboot_queue_unsafe")
    if prior_containers is not None:
        _validate_group2_reboot_network_boundary(rabbit["network"], prior_containers)
    for external in (rabbit["external_before"], rabbit["external_after"]):
        if external != {"connections": [], "channels": [], "consumers": []}:
            raise CarryForwardError("group2_reboot_queue_unsafe")
    expected = {(item.get("vhost"), item.get("name")) for item in expected_queues if isinstance(item, dict)}
    expected_names = [
        *(f"dlr.worker.{row['id']}.q" for row in value["raw_tables"]["workers"]),
        "dlr.execution.infrastructure.dlq",
    ]
    if [item.get("name") for item in expected_queues] != expected_names:
        raise CarryForwardError("group2_reboot_queue_unsafe")
    seen = set()
    for queue in rabbit["queues"]:
        queue = _closed_object(
            queue, {"name", "vhost", "type", "state", "arguments",
                    "messages_total", "messages_ready", "messages_unacknowledged",
                    "effective_policy_definition", "ra"}, code
        )
        identity = (queue["vhost"], queue["name"])
        expected_item = next(
            (item for item in expected_queues if isinstance(item, dict)
             and (item.get("vhost"), item.get("name")) == identity), None
        )
        ra = _closed_object(queue["ra"], {"total", "dlx", "checked_out"}, code)
        if (
            identity in seen or identity not in expected
            or not isinstance(expected_item, dict)
            or {key: queue[key] for key in ("vhost", "name", "type", "state")}
            != {key: expected_item[key] for key in ("vhost", "name", "type", "state")}
            or _group2_rabbit_amqp_table(queue["arguments"])
            != expected_item["arguments"]
            or queue["effective_policy_definition"] != {}
            or _rabbit_ra_result(ra["total"], "total") != 0
            or _rabbit_ra_result(ra["dlx"], "dlx") != [0, 0]
            or _rabbit_ra_result(ra["checked_out"], "checked_out") != 0
        ):
            raise CarryForwardError("group2_reboot_queue_unsafe")
        seen.add(identity)
    if seen != expected or len(expected) != len(expected_queues):
        raise CarryForwardError("group2_reboot_queue_unsafe")
    return value


def _rabbit_ra_result(value: Any, kind: str) -> Any:
    code = "group2_reboot_queue_unsafe"
    value = _closed_object(value, {"raw", "value"}, code)
    raw = value["raw"]
    if not isinstance(raw, str):
        raise CarryForwardError(code)
    if kind in {"total", "checked_out"}:
        match = re.fullmatch(r"\{ok,(\d+),\{.+\}\}", raw)
        if match is None:
            raise CarryForwardError(code)
        parsed: Any = int(match.group(1))
    elif kind == "dlx":
        match = re.fullmatch(r"\{ok,\{(\d+),(\d+)\},\{.+\}\}", raw)
        if match is None:
            raise CarryForwardError(code)
        parsed = [int(match.group(1)), int(match.group(2))]
    else:
        raise CarryForwardError(code)
    if value["value"] != parsed:
        raise CarryForwardError(code)
    return parsed


def _validate_group2_rabbit_topology(
    topology: Any, configuration: dict[str, Any], plugins: Any
) -> None:
    code = "group2_reboot_queue_unsafe"
    topology = _closed_object(
        topology, {"exchanges", "bindings", "policies", "operator_policies"}, code
    )
    if not isinstance(plugins, list) or any(
        not isinstance(item, str)
        or re.search(r"(?i)(federation|shovel|delayed_message_exchange)", item)
        for item in plugins
    ):
        raise CarryForwardError(code)
    exchanges = [
        item for item in topology["exchanges"]
        if isinstance(item, dict) and str(item.get("name", "")).startswith("dlr.")
    ]
    if sorted(exchanges, key=lambda item: item.get("name", "")) != [
        {"name": "dlr.execution.dispatch.v1", "type": "direct", "durable": True},
        {"name": "dlr.execution.infrastructure.dlx", "type": "direct", "durable": True},
    ]:
        raise CarryForwardError(code)
    expected_bindings = [
        {"source_name": "dlr.execution.dispatch.v1",
         "destination_name": f"dlr.worker.{row['id']}.q",
         "destination_kind": "queue", "routing_key": f"worker.{row['id']}",
         "arguments": []}
        for row in configuration["raw_workers"]
    ] + [{
        "source_name": "dlr.execution.infrastructure.dlx",
        "destination_name": "dlr.execution.infrastructure.dlq",
        "destination_kind": "queue", "routing_key": "infrastructure",
        "arguments": [],
    }] + [
        {"source_name": "", "destination_name": item["name"],
         "destination_kind": "queue", "routing_key": item["name"],
         "arguments": []}
        for item in configuration["rabbitmq"]["queues"]
    ]
    bindings = [
        item for item in topology["bindings"] if isinstance(item, dict)
        and (str(item.get("source_name", "")).startswith("dlr.")
             or str(item.get("destination_name", "")).startswith("dlr."))
    ]
    if sorted(bindings, key=canonical_bytes) != sorted(expected_bindings, key=canonical_bytes):
        raise CarryForwardError(code)
    if topology["policies"] != [] or topology["operator_policies"] != []:
        raise CarryForwardError(code)


def _group2_rabbit_amqp_table(value: Any) -> dict[str, Any]:
    code = "group2_reboot_queue_unsafe"
    if not isinstance(value, list):
        raise CarryForwardError(code)
    result = {}
    for item in value:
        if (
            not isinstance(item, list) or len(item) != 3
            or not isinstance(item[0], str) or item[0] in result
            or item[1] not in {"longstr", "signedint"}
            or (item[1] == "longstr" and not isinstance(item[2], str))
            or (item[1] == "signedint" and (
                not isinstance(item[2], int) or isinstance(item[2], bool)
            ))
        ):
            raise CarryForwardError(code)
        result[item[0]] = item[2]
    return result


def _validate_group2_reboot_network_boundary(
    value: Any, prior_containers: Any
) -> dict[str, Any]:
    code = "group2_reboot_network_unsafe"
    value = _closed_object(
        value, {"network_id", "container_inspect", "network_inspect",
                "container_states", "raw_digest"}, code,
    )
    if (
        not isinstance(prior_containers, dict)
        or set(prior_containers) != {"postgres", "rabbitmq", "control", "worker", "web", "account-web"}
        or not isinstance(value["network_id"], str) or not value["network_id"]
        or not isinstance(value["container_inspect"], dict)
        or set(value["container_inspect"]) != set(prior_containers)
        or not isinstance(value["container_states"], dict)
        or set(value["container_states"]) != set(prior_containers)
        or value["raw_digest"] != digest({
            "network_id": value["network_id"],
            "container_inspect": value["container_inspect"],
            "network_inspect": value["network_inspect"],
            "container_states": value["container_states"],
        })
    ):
        raise CarryForwardError(code)
    network = value["network_inspect"]
    if not isinstance(network, dict) or network.get("Id") != value["network_id"]:
        raise CarryForwardError(code)
    endpoints = network.get("Containers")
    if not isinstance(endpoints, dict):
        raise CarryForwardError(code)
    expected_ids = {
        prior_containers[name].get("container_id") for name in ("postgres", "rabbitmq")
    }
    if None in expected_ids or set(endpoints) != expected_ids:
        raise CarryForwardError(code)
    for service, expected in prior_containers.items():
        raw = value["container_inspect"][service]
        current = value["container_states"][service]
        should_run = service in {"postgres", "rabbitmq"}
        _validate_reboot_container_raw(raw, expected, current, stopped=False)
        networks = (raw.get("NetworkSettings") or {}).get("Networks")
        if not isinstance(networks, dict) or len(networks) != 1:
            raise CarryForwardError(code)
        attached = next(iter(networks.values()))
        endpoint = endpoints.get(expected["container_id"])
        if (
            not isinstance(attached, dict)
            or attached.get("NetworkID") != value["network_id"]
            or bool(current.get("status") == "running") != should_run
            or (should_run and current.get("health") != "healthy")
        ):
            raise CarryForwardError(code)
        if should_run and (
            not isinstance(endpoint, dict)
            or attached.get("EndpointID") != endpoint.get("EndpointID")
            or attached.get("IPAddress")
            != str(endpoint.get("IPv4Address", "")).split("/", 1)[0]
            or endpoint.get("Name") != str(raw.get("Name", "")).removeprefix("/")
        ):
            raise CarryForwardError(code)
        if not should_run and endpoint is not None:
            raise CarryForwardError(code)
        if not should_run and (
            attached.get("EndpointID") not in {"", None}
            or attached.get("IPAddress") not in {"", None}
        ):
            raise CarryForwardError(code)
    rabbit_host = value["container_inspect"]["rabbitmq"].get("HostConfig") or {}
    if rabbit_host.get("PortBindings") not in ({}, None):
        raise CarryForwardError(code)
    return value


def _capture_group2_reboot_network_boundary(
    request: dict[str, Any], project: str
) -> dict[str, Any]:
    prior = request["prior"]["containers"]
    inspections = {
        service: _inspect_reboot_container_raw(item["container_id"])
        for service, item in prior.items()
    }
    states = {service: _inspect_container(project, service) for service in prior}
    networks = (inspections["rabbitmq"].get("NetworkSettings") or {}).get("Networks")
    if not isinstance(networks, dict) or len(networks) != 1:
        raise CarryForwardError("group2_reboot_network_unsafe")
    network_id = next(iter(networks.values())).get("NetworkID")
    if not isinstance(network_id, str) or not network_id:
        raise CarryForwardError("group2_reboot_network_unsafe")
    try:
        raw = json.loads(_checked_output(["docker", "network", "inspect", network_id]))
    except (json.JSONDecodeError, OSError, subprocess.SubprocessError) as error:
        raise CarryForwardError("group2_reboot_network_unsafe") from error
    if not isinstance(raw, list) or len(raw) != 1 or not isinstance(raw[0], dict):
        raise CarryForwardError("group2_reboot_network_unsafe")
    value = {
        "network_id": network_id, "container_inspect": inspections,
        "network_inspect": raw[0], "container_states": states,
    }
    value["raw_digest"] = digest(value)
    return _validate_group2_reboot_network_boundary(value, prior)


def _group2_reboot_container_endpoint(raw: Any) -> tuple[str, str]:
    networks = (raw.get("NetworkSettings") or {}).get("Networks") if isinstance(raw, dict) else None
    if not isinstance(networks, dict) or len(networks) != 1:
        raise CarryForwardError("group2_reboot_rabbit_identity_invalid")
    endpoint = next(iter(networks.values()))
    network_id = endpoint.get("NetworkID") if isinstance(endpoint, dict) else None
    address = endpoint.get("IPAddress") if isinstance(endpoint, dict) else None
    if not isinstance(network_id, str) or not network_id or not isinstance(address, str) or not address:
        raise CarryForwardError("group2_reboot_rabbit_identity_invalid")
    return network_id, address


def _group2_reboot_rabbit_address(value: Any) -> str:
    code = "group2_reboot_rabbit_identity_invalid"
    if not isinstance(value, list) or any(
        not isinstance(item, int) or isinstance(item, bool) for item in value
    ):
        raise CarryForwardError(code)
    try:
        if len(value) == 4 and all(0 <= item <= 255 for item in value):
            return str(ipaddress.IPv4Address(bytes(value)))
        if len(value) == 8 and all(0 <= item <= 65535 for item in value):
            numeric = 0
            for item in value:
                numeric = (numeric << 16) | item
            address = ipaddress.IPv6Address(numeric)
            return str(address.ipv4_mapped or address)
    except ipaddress.AddressValueError as error:
        raise CarryForwardError(code) from error
    raise CarryForwardError(code)


def _validate_group2_reboot_rabbit_identity_sample(
    sample: Any, configuration: dict[str, Any], *, rabbit_ip: str,
    control_ip: str, worker_ip: str,
) -> dict[str, Any]:
    code = "group2_reboot_rabbit_identity_invalid"
    sample = _closed_object(sample, {"connections", "channels", "consumers"}, code)
    connection_keys = {"pid", "peer_host", "peer_port", "host", "port", "user", "vhost",
                       "protocol", "client_properties", "state"}
    channel_keys = {"pid", "connection", "user", "vhost", "number", "consumer_count",
                    "messages_unacknowledged", "prefetch_count"}
    consumer_keys = {"queue_name", "channel_pid", "consumer_tag", "ack_required",
                     "prefetch_count", "active", "arguments"}
    for rows, keys in ((sample["connections"], connection_keys),
                       (sample["channels"], channel_keys),
                       (sample["consumers"], consumer_keys)):
        if not isinstance(rows, list) or any(
            not isinstance(row, dict) or set(row) != keys for row in rows
        ):
            raise CarryForwardError(code)
    connections = {row["pid"]: row for row in sample["connections"]}
    channels = {row["pid"]: row for row in sample["channels"]}
    if (
        len(connections) != len(sample["connections"])
        or len(channels) != len(sample["channels"])
        or len({row["consumer_tag"] for row in sample["consumers"]})
        != len(sample["consumers"])
    ):
        raise CarryForwardError(code)
    rabbit = configuration["rabbitmq"]
    connection_roles = {}
    for pid, row in connections.items():
        peer = _group2_reboot_rabbit_address(row["peer_host"])
        host = _group2_reboot_rabbit_address(row["host"])
        if not isinstance(row["client_properties"], list) or any(
            not isinstance(item, list) or len(item) != 3 for item in row["client_properties"]
        ):
            raise CarryForwardError(code)
        if (
            peer not in {worker_ip, control_ip}
            or host != rabbit_ip or row["port"] != rabbit["port"]
            or row["user"] != rabbit["user"] or row["vhost"] != rabbit["vhost"]
            or row["protocol"] != [0, 9, 1] or row["state"] != "running"
            or not isinstance(row["peer_port"], int) or isinstance(row["peer_port"], bool)
        ):
            raise CarryForwardError(code)
        connection_roles[pid] = "worker" if peer == worker_ip else "control"
    worker_connections = [pid for pid, role in connection_roles.items() if role == "worker"]
    if len(worker_connections) != 1:
        raise CarryForwardError(code)
    for row in channels.values():
        if (
            row["connection"] not in connections
            or row["user"] != rabbit["user"] or row["vhost"] != rabbit["vhost"]
            or any(not isinstance(row[key], int) or isinstance(row[key], bool) or row[key] < 0
                   for key in ("number", "consumer_count", "messages_unacknowledged", "prefetch_count"))
        ):
            raise CarryForwardError(code)
    worker_id = configuration["worker_id"]
    slots = configuration["execution_slots"]
    expected_queue = f"dlr.worker.{worker_id}.q"
    observed_slots: dict[int, int] = {}
    epochs = set()
    for row in sample["consumers"]:
        channel = channels.get(row["channel_pid"])
        connection = connections.get(channel["connection"]) if channel else None
        match = re.fullmatch(
            rf"dlr-worker-{worker_id}-e([1-9][0-9]*)-s([0-9]+)-t([1-9][0-9]*)",
            str(row["consumer_tag"]),
        )
        if (
            channel is None or connection is None
            or connection_roles.get(channel["connection"]) != "worker"
            or row["queue_name"] != expected_queue or match is None
            or row["ack_required"] is not True or row["prefetch_count"] != 1
            or row["active"] is not True or row["arguments"] != []
        ):
            raise CarryForwardError(code)
        epoch, slot, ticket = map(int, match.groups())
        if slot in observed_slots:
            raise CarryForwardError(code)
        observed_slots[slot] = ticket
        epochs.add(epoch)
    if (
        not isinstance(slots, int) or isinstance(slots, bool) or slots <= 0
        or set(observed_slots) != set(range(slots)) or len(epochs) != 1
    ):
        raise CarryForwardError(code)
    for row in channels.values():
        joined = [item for item in sample["consumers"] if item["channel_pid"] == row["pid"]]
        connection_role = connection_roles[row["connection"]]
        if (
            row["consumer_count"] != len(joined)
            or row["messages_unacknowledged"] != 0
            or (connection_role == "control" and joined)
        ):
            raise CarryForwardError(code)
    worker_connection = connections[worker_connections[0]]
    worker_channels = sorted(
        (row for row in channels.values() if row["connection"] == worker_connections[0]),
        key=canonical_bytes,
    )
    if len(worker_channels) != 1 or worker_channels[0]["consumer_count"] != slots:
        raise CarryForwardError(code)
    worker_consumers = sorted(sample["consumers"], key=canonical_bytes)
    return {"connection": worker_connection, "channels": worker_channels,
            "consumers": worker_consumers}


def validate_group2_reboot_rabbit_identity(
    value: Any, configuration: Any, container_inspect: Any
) -> dict[str, Any]:
    code = "group2_reboot_rabbit_identity_invalid"
    value = _closed_object(
        value, {"schema", "captured_start_ns", "captured_end_ns", "container_binding",
                "samples", "raw_digest"}, code,
    )
    if (
        value["schema"] != "group2-post-finalize-reboot-rabbit-identity-v1"
        or not isinstance(value["captured_start_ns"], int)
        or isinstance(value["captured_start_ns"], bool)
        or not isinstance(value["captured_end_ns"], int)
        or isinstance(value["captured_end_ns"], bool)
        or value["captured_end_ns"] < value["captured_start_ns"]
        or not isinstance(configuration, dict)
        or not isinstance(container_inspect, dict)
        or set(container_inspect) != {"postgres", "rabbitmq", "control", "worker", "web", "account-web"}
        or not isinstance(value["samples"], list) or len(value["samples"]) != 2
    ):
        raise CarryForwardError(code)
    rabbit_network, rabbit_ip = _group2_reboot_container_endpoint(container_inspect["rabbitmq"])
    control_network, control_ip = _group2_reboot_container_endpoint(container_inspect["control"])
    worker_network, worker_ip = _group2_reboot_container_endpoint(container_inspect["worker"])
    state_binding = {}
    for service in ("rabbitmq", "control", "worker"):
        raw = container_inspect[service]
        state = raw.get("State") if isinstance(raw, dict) else None
        if not isinstance(state, dict) or state.get("Status") != "running" \
                or not isinstance(state.get("Pid"), int) or state.get("Pid") <= 0 \
                or not isinstance(state.get("StartedAt"), str) or not state.get("StartedAt"):
            raise CarryForwardError(code)
        network_id, address = _group2_reboot_container_endpoint(raw)
        state_binding[service] = {
            "container_id": raw.get("Id"), "image_id": raw.get("Image"),
            "network_id": network_id, "ip": address, "pid": state["Pid"],
            "started_at": state["StartedAt"], "config_env_digest": digest((raw.get("Config") or {}).get("Env")),
        }
    if (
        rabbit_network != control_network or rabbit_network != worker_network
        or value["container_binding"] != state_binding
        or state_binding["control"]["config_env_digest"]
        != digest((configuration.get("control_inspect", {}).get("Config") or {}).get("Env"))
        or state_binding["worker"]["config_env_digest"]
        != digest((configuration.get("worker_inspect", {}).get("Config") or {}).get("Env"))
        or value["raw_digest"] != digest({
            key: item for key, item in value.items() if key != "raw_digest"
        })
    ):
        raise CarryForwardError(code)
    graphs = [
        _validate_group2_reboot_rabbit_identity_sample(
            sample, configuration, rabbit_ip=rabbit_ip,
            control_ip=control_ip, worker_ip=worker_ip,
        )
        for sample in value["samples"]
    ]
    if graphs[0] != graphs[1]:
        raise CarryForwardError(code)
    return value


def capture_group2_reboot_rabbit_identity(
    request: dict[str, Any], configuration: dict[str, Any]
) -> dict[str, Any]:
    rabbit_id = request["prior"]["containers"]["rabbitmq"]["container_id"]
    vhost = configuration["rabbitmq"]["vhost"]

    def query(*arguments: str) -> list[dict[str, Any]]:
        try:
            raw = subprocess.check_output(
                ["docker", "exec", rabbit_id, "rabbitmqctl", "-q", *arguments,
                 "--formatter", "json"],
                text=True, timeout=_reboot_timeout(request, 10),
            )
            parsed = json.loads(raw)
        except (json.JSONDecodeError, OSError, subprocess.SubprocessError) as error:
            raise CarryForwardError("group2_reboot_rabbit_identity_invalid") from error
        if not isinstance(parsed, list) or any(not isinstance(row, dict) for row in parsed):
            raise CarryForwardError("group2_reboot_rabbit_identity_invalid")
        return parsed

    def sample() -> dict[str, Any]:
        return {
            "connections": query(
                "list_connections", "pid", "peer_host", "peer_port", "host", "port",
                "user", "vhost", "protocol", "client_properties", "state",
            ),
            "channels": query(
                "list_channels", "pid", "connection", "user", "vhost", "number",
                "consumer_count", "messages_unacknowledged", "prefetch_count",
            ),
            "consumers": query(
                "list_consumers", "-p", vhost, "queue_name", "channel_pid", "consumer_tag",
                "ack_required", "prefetch_count", "active", "arguments",
            ),
        }

    while True:
        started = time.time_ns()
        inspections = {
            service: _inspect_reboot_container_raw(
                request["prior"]["containers"][service]["container_id"]
            )
            for service in request["prior"]["containers"]
        }
        samples = [sample(), sample()]
        binding = {}
        for service in ("rabbitmq", "control", "worker"):
            raw = inspections[service]
            network_id, address = _group2_reboot_container_endpoint(raw)
            state = raw.get("State") or {}
            binding[service] = {
                "container_id": raw.get("Id"), "image_id": raw.get("Image"),
                "network_id": network_id, "ip": address, "pid": state.get("Pid"),
                "started_at": state.get("StartedAt"),
                "config_env_digest": digest((raw.get("Config") or {}).get("Env")),
            }
        value = {
            "schema": "group2-post-finalize-reboot-rabbit-identity-v1",
            "captured_start_ns": started, "captured_end_ns": time.time_ns(),
            "container_binding": binding, "samples": samples,
        }
        value["raw_digest"] = digest(value)
        try:
            return validate_group2_reboot_rabbit_identity(value, configuration, inspections)
        except CarryForwardError:
            retryable = samples[0] != samples[1]
            for current in samples:
                channel_ids = {
                    row.get("pid") for row in current.get("channels", [])
                    if isinstance(row, dict)
                }
                connection_ids = {
                    row.get("pid") for row in current.get("connections", [])
                    if isinstance(row, dict)
                }
                retryable = retryable or len(current.get("consumers", [])) != configuration["execution_slots"]
                retryable = retryable or any(
                    isinstance(row, dict) and row.get("connection") not in connection_ids
                    for row in current.get("channels", [])
                ) or any(
                    isinstance(row, dict) and row.get("channel_pid") not in channel_ids
                    for row in current.get("consumers", [])
                )
            if not retryable:
                raise
            if time.time_ns() >= request["window"]["deadline_ns"]:
                raise
            time.sleep(min(1.0, _reboot_timeout(request, 1)))


def _group2_reboot_artifact_inventory(artifact_entries: Any) -> list[dict[str, Any]]:
    code = "group2_reboot_start_admission_invalid"
    if not isinstance(artifact_entries, list):
        raise CarryForwardError(code)
    inventory = []
    for item in artifact_entries:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise CarryForwardError(code)
        path = item["path"]
        if item.get("type") == "directory" and (
            path in {"objects", "parts", "quarantine"}
            or re.fullmatch(r"(?:objects|parts|quarantine)/[0-9a-f]{2}", path)
        ):
            continue
        file_match = re.fullmatch(
            r"(objects|parts|quarantine)/([0-9a-f]{2})/([0-9a-f]{64})(\.part)?",
            path,
        )
        if item.get("type") != "file" or file_match is None:
            raise CarryForwardError(code)
        namespace, prefix, storage_key, suffix = file_match.groups()
        if (
            prefix != storage_key[:2]
            or (namespace == "objects" and suffix is not None)
            or (namespace == "parts" and suffix != ".part")
        ):
            raise CarryForwardError(code)
        if namespace == "quarantine":
            continue
        inventory.append({"storage_key": storage_key, "mtime_ns": item.get("mtime_ns"),
                          "stat_digest": digest(item), "path": path})
    return inventory


def capture_group2_reboot_start_admission(
    root: Path, directory: Path, request: dict[str, Any], database: dict[str, Any],
    files: dict[str, Any], seed: Any, project: str, parent_selection: dict[str, Any],
) -> dict[str, Any]:
    """Complete the same-transaction DB seed with fresh file and broker facts."""
    code = "group2_reboot_start_admission_invalid"
    seed = _closed_object(
        seed, {"raw_tables", "database_rows", "mutation_guard_rows",
               "db_clock", "vm_lower_ns", "vm_upper_ns",
               "database_digest", "files_digest"}, code
    )
    if seed["database_digest"] != digest(database) or seed["files_digest"] != digest(files):
        raise CarryForwardError(code)
    parameters = request["prior"]["parameters"]
    control_raw = _inspect_reboot_container_raw(
        request["prior"]["containers"]["control"]["container_id"]
    )
    worker_raw = _inspect_reboot_container_raw(
        request["prior"]["containers"]["worker"]["container_id"]
    )
    for service, raw in (("control", control_raw), ("worker", worker_raw)):
        current = _inspect_container(project, service)
        _validate_reboot_container_raw(
            raw, request["prior"]["containers"][service], current, stopped=True
        )
    configuration = derive_group2_reboot_configuration(
        control_raw, worker_raw, seed["raw_tables"]["workers"]
    )
    settings = configuration["effective_settings"]
    worker_id = configuration["worker_id"]
    queued = [item["execution_id"] for item in parent_selection.get("queued", [])]
    journal = files.get("journal_facts")
    materials = files.get("materials", {})
    artifact_entries = materials.get("artifacts", {}).get("entries", []) if isinstance(materials, dict) else []
    inventory = _group2_reboot_artifact_inventory(artifact_entries)

    rabbit_parameters = configuration["rabbitmq"]
    rabbit_id = request["prior"]["containers"]["rabbitmq"]["container_id"]

    def rabbit_json(*arguments: str) -> Any:
        try:
            return json.loads(_checked_output(["docker", "exec", rabbit_id, *arguments]))
        except (json.JSONDecodeError, OSError, subprocess.SubprocessError) as error:
            raise CarryForwardError("group2_reboot_queue_unsafe") from error

    def rabbit_external() -> dict[str, Any]:
        return {
            "connections": rabbit_json("rabbitmqctl", "-q", "list_connections", "name", "--formatter", "json"),
            "channels": rabbit_json("rabbitmqctl", "-q", "list_channels", "pid", "--formatter", "json"),
            "consumers": rabbit_json("rabbitmqctl", "-q", "list_consumers", "-p", rabbit_parameters["vhost"], "queue_name", "consumer_tag", "--formatter", "json"),
        }

    def rabbit_ra(queue_name: str, function: str, kind: str) -> dict[str, Any]:
        vhost = base64.b64encode(rabbit_parameters["vhost"].encode()).decode()
        name = base64.b64encode(queue_name.encode()).decode()
        expression = (
            f'V=base64:decode(<<"{vhost}">>),N=base64:decode(<<"{name}">>),'
            '{ok,Q}=rabbit_amqqueue:lookup(rabbit_misc:r(V,queue,N)),'
            f'ra:consistent_query(amqqueue:get_pid(Q),{{rabbit_fifo,{function},[]}},5000).'
        )
        raw = subprocess.check_output(
            ["docker", "exec", rabbit_id, "rabbitmqctl", "-q", "eval", expression],
            text=True, timeout=_reboot_timeout(request, 10),
        ).strip()
        total_match = re.fullmatch(r"\{ok,(\d+),\{.+\}\}", raw)
        dlx_match = re.fullmatch(r"\{ok,\{(\d+),(\d+)\},\{.+\}\}", raw)
        provisional = {"raw": raw, "value": (
            [int(item) for item in dlx_match.groups()] if kind == "dlx" and dlx_match
            else int(total_match.group(1)) if total_match else None
        )}
        _rabbit_ra_result(provisional, kind)
        return provisional

    topology = {
        "exchanges": rabbit_json("rabbitmqctl", "-q", "list_exchanges", "-p", rabbit_parameters["vhost"], "name", "type", "durable", "--formatter", "json"),
        "bindings": rabbit_json("rabbitmqctl", "-q", "list_bindings", "-p", rabbit_parameters["vhost"], "source_name", "destination_name", "destination_kind", "routing_key", "arguments", "--formatter", "json"),
        "policies": rabbit_json("rabbitmqctl", "-q", "list_policies", "-p", rabbit_parameters["vhost"], "--formatter", "json"),
        "operator_policies": rabbit_json("rabbitmqctl", "-q", "list_operator_policies", "-p", rabbit_parameters["vhost"], "--formatter", "json"),
    }
    feature_flags = rabbit_json(
        "rabbitmqctl", "-q", "list_feature_flags", "name", "state", "--formatter", "json"
    )
    external_before = rabbit_external()
    queue_rows = rabbit_json(
        "rabbitmqctl", "-q", "list_queues", "-p", rabbit_parameters["vhost"],
        "name", "type", "state", "arguments", "messages", "messages_ready",
        "messages_unacknowledged", "effective_policy_definition", "--formatter", "json",
    )
    by_name = {row.get("name"): row for row in queue_rows if isinstance(row, dict)}
    queues = []
    for expected_queue in rabbit_parameters["queues"]:
        row = by_name.get(expected_queue["name"])
        if row is None:
            raise CarryForwardError("group2_reboot_queue_unsafe")
        queues.append({
            "vhost": rabbit_parameters["vhost"], "name": row["name"],
            "type": row["type"], "state": row["state"],
            "arguments": row["arguments"], "messages_total": row["messages"],
            "messages_ready": row["messages_ready"],
            "messages_unacknowledged": row["messages_unacknowledged"],
            "effective_policy_definition": row["effective_policy_definition"],
            "ra": {
                "total": rabbit_ra(row["name"], "query_messages_total", "total"),
                "dlx": rabbit_ra(row["name"], "query_stat_dlx", "dlx"),
                "checked_out": rabbit_ra(row["name"], "query_messages_checked_out", "checked_out"),
            },
        })
    if set(by_name) != {item["name"] for item in rabbit_parameters["queues"]}:
        raise CarryForwardError("group2_reboot_queue_unsafe")
    external_after = rabbit_external()
    network = _capture_group2_reboot_network_boundary(request, project)
    t0_ns = _group2_admission_ns(seed["db_clock"], code)
    raw_files = {"journal_facts": journal, "artifact_store": inventory}
    derived = derive_group2_reboot_start_admission(
        seed["raw_tables"], raw_files, settings, t0_ns=t0_ns,
        horizon_ns=request["window"]["successor_deadline_ns"],
        preserved_queued_ids=queued, worker_id=worker_id,
    )
    rabbit = {
        "image_id": request["prior"]["containers"]["rabbitmq"]["image_id"],
        "version": _checked_output(["docker", "exec", rabbit_id, "rabbitmqctl", "version"]),
        "plugins": _checked_output(["docker", "exec", rabbit_id, "rabbitmq-plugins", "list", "-e", "-m"]).splitlines(),
        "config_digest": digest(rabbit_parameters),
        "queue_scope": rabbit_parameters["queues"],
        "raw_response_digest": digest({"queues": queues, "topology": topology,
                                       "feature_flags": feature_flags,
                                       "external_before": external_before,
                                       "external_after": external_after,
                                       "network": network}),
        "queues": queues,
        "topology": topology, "feature_flags": feature_flags,
        "external_before": external_before, "external_after": external_after,
        "network": network,
    }
    value = {
        "schema": "group2-post-finalize-reboot-start-admission-v1",
        "database_digest": digest(database), "parameters_digest": digest(parameters),
        "clock": {"db_utc_ns": t0_ns, "vm_lower_ns": seed["vm_lower_ns"],
                  "vm_upper_ns": seed["vm_upper_ns"]},
        "deadline_ns": request["window"]["deadline_ns"],
        "successor_deadline_ns": request["window"]["successor_deadline_ns"],
        "sources": GROUP2_POST_FINALIZE_REBOOT_SOURCES,
        "raw_tables": seed["raw_tables"], "database_rows": seed["database_rows"],
        "mutation_guard_rows": seed["mutation_guard_rows"],
        "raw_files": raw_files, "configuration": configuration,
        "effective_settings": settings, "preserved_queued_ids": queued,
        "worker_id": worker_id, "rabbitmq": rabbit, "derived": derived,
    }
    validate_group2_reboot_start_admission(
        value, database, parameters, request["window"]["deadline_ns"],
        request["window"]["successor_deadline_ns"], rabbit["image_id"],
        queued, request["prior"]["containers"],
    )
    _atomic_incident_json(directory / "start-admission.json", value)
    return value


def _post_finalize_reboot_stage(value: Any, phase: str) -> dict[str, Any]:
    fields = {
        "schema", "phase", "boot_id", "files", "logs", "containers", "images",
        "container_inspect", "storage", "authority", "window_start_ns", "window_end_ns",
    }
    if phase in {"keeper_ready", "applications_started", "verified"}:
        fields.add("kernel")
    if phase in {"stopped", "keeper_ready"}:
        fields.add("postgres")
    if phase == "stopped":
        fields.add("platform")
    if phase in {"database_ready", "applications_started", "verified"}:
        fields.update(("db", "postgres", "start_admission"))
    if phase in {"applications_started", "verified"}:
        fields.update(("startup_request", "startup_proof", "startup_files",
                       "rabbit_identity", "mutation_guard"))
    value = _closed_object(value, fields, "group2_reboot_stage_invalid")
    if (
        value["schema"] != "group2-post-finalize-reboot-stage-v1"
        or value["phase"] != phase
        or not isinstance(value["window_start_ns"], int)
        or isinstance(value["window_start_ns"], bool)
        or not isinstance(value["window_end_ns"], int)
        or isinstance(value["window_end_ns"], bool)
        or value["window_end_ns"] < value["window_start_ns"]
    ):
        raise CarryForwardError("group2_reboot_stage_invalid")
    return value


def _validate_group2_reboot_empty_log_segment(before: Any, after: Any) -> None:
    try:
        start, end = _validate_log_link(before, after)
        _validate_log_segment_transition(before, after, start, end)
    except CarryForwardError as error:
        raise CarryForwardError("group2_reboot_transition_invalid") from error
    if any(
        not isinstance(item, dict) or item.get("appended_text") != ""
        for item in after.get("files", [])
    ):
        raise CarryForwardError("group2_reboot_transition_invalid")


def _derive_group2_reboot_backing_mount(
    mountpoint: Any, device: Any, mounts: Any, code: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    try:
        mountpoint_path = Path(mountpoint)
        if (
            not isinstance(mountpoint, str) or not mountpoint
            or not mountpoint_path.is_absolute()
            or type(device) is not int or device < 0
            or not isinstance(mounts, list)
        ):
            raise ValueError("backing input shape")
        major_minor = f"{os.major(device)}:{os.minor(device)}"
        candidates = []
        for host_mount in mounts:
            if not isinstance(host_mount, dict):
                raise ValueError("mountinfo row shape")
            try:
                relative = mountpoint_path.relative_to(Path(host_mount["mountpoint"]))
            except ValueError:
                continue
            if host_mount.get("major_minor") != major_minor:
                continue
            candidates.append((
                len(Path(host_mount["mountpoint"]).parts), relative, host_mount,
            ))
        if not candidates:
            raise ValueError("backing mount missing")
        depth = max(value[0] for value in candidates)
        deepest = [value for value in candidates if value[0] == depth]
        if len(deepest) != 1:
            raise ValueError("ambiguous backing mount")
        _depth, relative, raw_selected = deepest[0]
        host_mount = dict(raw_selected)
        mount = dict(raw_selected)
        mount["root"] = str(Path(raw_selected["root"]) / relative)
        derived = {
            "filesystem": mount["filesystem"],
            "major_minor": mount["major_minor"],
            "root": mount["root"],
            "mountpoint": mountpoint,
        }
    except (KeyError, TypeError, ValueError) as error:
        raise CarryForwardError(code) from error
    return host_mount, mount, derived


def _validate_group2_reboot_keeper_kernel(
    kernel: Any, request: dict[str, Any]
) -> dict[str, Any]:
    code = "group2_reboot_kernel_changed"
    fields = {
        "boot_id", "unit", "control_group", "keeper_pid", "keeper_starttime",
        "description", "parent_device", "parent_inode", "children",
        "old_worker_authority", "namespace_evidence", "retired_markers",
        "unit_properties", "cgroup_limits", "volume_backings",
    }
    if not isinstance(kernel, dict) or set(kernel) != fields:
        raise CarryForwardError(code)
    params = request["prior"]["parameters"]["keeper"]
    children = kernel.get("children")
    agent = children.get("agent") if isinstance(children, dict) else None
    namespace = kernel.get("namespace_evidence")
    properties = kernel["unit_properties"]
    limits = kernel["cgroup_limits"]
    backings = kernel["volume_backings"]
    scans = _validate_group2_reboot_namespace_evidence(
        namespace, "keeper_idle", backings
    )
    expected_description = (
        f"DataLinkRuntime Sandbox {params['unit']} CPU={params['cpu_quota']} "
        f"Memory={params['memory_max']}"
    )
    cpu_match = re.fullmatch(r"([1-9][0-9]*)%", params["cpu_quota"])
    memory_match = re.fullmatch(r"([1-9][0-9]*)([KMG])", params["memory_max"])
    try:
        cpu_quota, cpu_period = map(int, str(limits["."]["cpu.max"]).split())
        memory_bytes = int(memory_match.group(1)) * {
            "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3,
        }[memory_match.group(2)]
    except (AttributeError, KeyError, TypeError, ValueError) as error:
        raise CarryForwardError(code) from error
    volumes = backings.get("volumes") if isinstance(backings, dict) else None
    if (
        kernel.get("boot_id") != request["boot_id"]
        or kernel.get("unit") != params["unit"]
        or kernel.get("control_group") != f"/system.slice/{params['unit']}"
        or type(kernel.get("keeper_pid")) is not int or kernel["keeper_pid"] <= 0
        or re.fullmatch(r"[1-9][0-9]*", str(kernel.get("keeper_starttime"))) is None
        or kernel.get("description") != expected_description
        or type(kernel.get("parent_device")) is not int or kernel["parent_device"] < 0
        or type(kernel.get("parent_inode")) is not int or kernel["parent_inode"] <= 0
        or not isinstance(children, dict) or set(children) != {"agent"}
        or not isinstance(agent, dict)
        or set(agent) != {"populated", "process_count", "process_digest", "device", "inode"}
        or any(type(agent.get(name)) is not int for name in (
            "populated", "process_count", "device", "inode"
        ))
        or agent["device"] < 0 or agent["inode"] <= 0
        or agent.get("populated") != 1 or agent.get("process_count") != 1
        or agent.get("process_digest") != digest([kernel["keeper_pid"]])
        or any(scan["related"] or scan["pins"] for scan in scans)
        or kernel.get("retired_markers") != []
        or kernel.get("old_worker_authority") is not None
        or not isinstance(properties, dict)
        or set(properties) != {"ActiveState", "Delegate", "ControlGroup", "MainPID",
                               "Description", "InvocationID"}
        or properties != {
            "ActiveState": "active", "Delegate": "yes",
            "ControlGroup": f"/system.slice/{params['unit']}",
            "MainPID": str(kernel["keeper_pid"]), "Description": expected_description,
            "InvocationID": properties.get("InvocationID"),
        }
        or re.fullmatch(r"[0-9a-fA-F]{32}", str(properties.get("InvocationID"))) is None
        or not isinstance(limits, dict) or set(limits) != {".", "agent"}
        or any(not isinstance(item, dict) or set(item) != {
            "cpu.max", "memory.max", "memory.swap.max", "pids.max",
            "cgroup.subtree_control", "cgroup.procs", "uid", "gid",
        } for item in limits.values())
        or limits["."]["uid"] != 0 or limits["."]["gid"] != 0
        or limits["."]["cgroup.procs"] != ""
        or cpu_match is None or cpu_quota <= 0 or cpu_period <= 0
        or cpu_quota * 100 != int(cpu_match.group(1)) * cpu_period
        or limits["."]["memory.max"] != str(memory_bytes)
        or limits["."]["memory.swap.max"] != "0"
        or limits["."]["pids.max"] != "max"
        or not {"cpu", "memory", "pids"}.issubset(
            set(str(limits["."]["cgroup.subtree_control"]).split())
        )
        or not isinstance(backings, dict)
        or backings.get("schema") != "group2-post-finalize-reboot-volume-backings-v1"
        or backings.get("boot_id") != request["boot_id"]
        or re.fullmatch(r"mnt:\[[1-9][0-9]*\]",
                        str(backings.get("host_mount_namespace"))) is None
        or not isinstance(backings.get("host_mountinfo"), list)
        or not isinstance(volumes, dict) or set(volumes) != {"runtime", "journal"}
    ):
        raise CarryForwardError(code)
    for key, service, destination in (
        ("runtime", "worker", "/var/lib/dlr/runtime"),
        ("journal", "worker", "/var/lib/dlr/journal"),
    ):
        item = volumes[key]
        mount = item.get("mount") if isinstance(item, dict) else None
        inspect = item.get("inspect") if isinstance(item, dict) else None
        mountpoint = inspect.get("Mountpoint") if isinstance(inspect, dict) else None
        selected, derived_mount, derived = _derive_group2_reboot_backing_mount(
            mountpoint, item.get("device") if isinstance(item, dict) else None,
            backings["host_mountinfo"], code,
        )
        if (
            not isinstance(item, dict)
            or item.get("name") != _reboot_volume(request["prior"]["storage"], service, destination)
            or type(item.get("device")) is not int or item["device"] < 0
            or type(item.get("inode")) is not int or item["inode"] <= 0
            or not isinstance(mount, dict)
            or any(not isinstance(mount.get(name), str) or not mount[name]
                   for name in ("filesystem", "major_minor", "root", "mountpoint"))
            or not isinstance(item.get("inspect"), dict)
            or item["inspect"].get("Name") != item.get("name")
            or not isinstance(item.get("mountpoint_stat"), dict)
            or item["mountpoint_stat"].get("type") != "directory"
            or item["mountpoint_stat"].get("device") != item.get("device")
            or item["mountpoint_stat"].get("inode") != item.get("inode")
            or not isinstance(item.get("host_mount"), dict)
            or item.get("derived_mount") != derived
            or item.get("host_mount") != selected
            or mount != derived_mount
            or item["mountpoint_stat"].get("device") < 0
            or selected.get("major_minor") != (
                f"{os.major(item['mountpoint_stat']['device'])}:"
                f"{os.minor(item['mountpoint_stat']['device'])}"
            )
        ):
            raise CarryForwardError(code)
    return kernel


def _validate_group2_reboot_namespace_evidence(
    value: Any, mode: str, backings: Any,
) -> list[dict[str, Any]]:
    code = "group2_reboot_kernel_changed"
    value = _closed_object(value, {"schema", "mode", "targets", "scans"}, code)
    volumes = backings.get("volumes") if isinstance(backings, dict) else None
    if not isinstance(volumes, dict):
        raise CarryForwardError(code)
    targets = sorted(
        (item["mount"]["filesystem"], item["mount"]["major_minor"], item["mount"]["root"])
        for item in volumes.values()
    )
    if (
        value["schema"] != "group2-post-finalize-reboot-namespace-evidence-v1"
        or value["mode"] != mode or canonical(value["targets"]) != canonical(targets)
        or not isinstance(value["scans"], list) or len(value["scans"]) != 2
    ):
        raise CarryForwardError(code)
    scans = []
    keys = {
        "task_count", "namespace_count", "mountinfo_bytes", "fd_entries",
        "related_count", "pin_count", "target_digest", "related", "pins",
    }
    for raw in value["scans"]:
        scan = _closed_object(raw, keys, code)
        if (
            any(type(scan[name]) is not int or scan[name] < (1 if name in {
                "task_count", "namespace_count"
            } else 0) for name in (
                "task_count", "namespace_count", "mountinfo_bytes", "fd_entries",
                "related_count", "pin_count",
            ))
            or scan["task_count"] > 8192
            or scan["namespace_count"] > 2048
            or scan["mountinfo_bytes"] > 128 * 1024 * 1024
            or scan["fd_entries"] > 65536
            or not isinstance(scan["related"], list) or not isinstance(scan["pins"], list)
            or scan["related_count"] != len(scan["related"])
            or scan["pin_count"] != len(scan["pins"])
            or scan["target_digest"] != digest({
                "related": scan["related"], "pins": scan["pins"]
            })
        ):
            raise CarryForwardError(code)
        scans.append(scan)
    if any(scans[0][name] != scans[1][name] for name in (
        "related", "pins", "related_count", "pin_count", "target_digest"
    )):
        raise CarryForwardError(code)
    return scans


def _validate_group2_reboot_running_kernel(
    keeper: Any, current: Any, proof: dict[str, Any], worker: dict[str, Any],
    raw_worker: Any, profile: dict[str, Any], request: dict[str, Any],
) -> dict[str, Any]:
    code = "group2_reboot_kernel_changed"
    keeper = _validate_group2_reboot_keeper_kernel(keeper, request)
    if not isinstance(current, dict) or set(current) != set(keeper):
        raise CarryForwardError(code)
    for key in (
        "boot_id", "unit", "control_group", "keeper_pid", "keeper_starttime",
        "description", "parent_device", "parent_inode",
    ):
        if current.get(key) != keeper.get(key):
            raise CarryForwardError(code)
    authority = current.get("old_worker_authority")
    children = current.get("children")
    container_id = proof["container_id"]
    agent = children.get("agent") if isinstance(children, dict) else None
    worker_root = children.get(container_id) if isinstance(children, dict) else None
    worker_agent = children.get(f"{container_id}/agent") if isinstance(children, dict) else None
    current_backings = current.get("volume_backings")
    keeper_backings = keeper["volume_backings"]
    if (
        current["unit_properties"] != keeper["unit_properties"]
        or not isinstance(current_backings, dict)
        or set(current_backings) != set(keeper_backings)
        or any(current_backings.get(key) != keeper_backings.get(key) for key in (
            "schema", "boot_id", "host_mount_namespace", "volumes",
        ))
        or any(current["cgroup_limits"].get(key) != keeper["cgroup_limits"].get(key)
               for key in (".", "agent"))
    ):
        raise CarryForwardError(code)
    scans = _validate_group2_reboot_namespace_evidence(
        current["namespace_evidence"], "worker_running", current["volume_backings"]
    )
    keeper_volumes = keeper["volume_backings"]["volumes"]
    live_volumes = authority.get("volumes") if isinstance(authority, dict) else None
    volume_keys = ("name", "device", "inode")
    mount_keys = ("filesystem", "major_minor", "root")
    volumes_match = isinstance(live_volumes, dict) and set(live_volumes) == set(keeper_volumes)
    if volumes_match:
        for name in keeper_volumes:
            before, after = keeper_volumes[name], live_volumes[name]
            volumes_match = volumes_match and all(before.get(key) == after.get(key) for key in volume_keys)
            volumes_match = volumes_match and all(
                before.get("mount", {}).get(key) == after.get("mount", {}).get(key)
                for key in mount_keys
            )
    state = raw_worker.get("State") if isinstance(raw_worker, dict) else None
    config = raw_worker.get("Config") if isinstance(raw_worker, dict) else None
    host_config = raw_worker.get("HostConfig") if isinstance(raw_worker, dict) else None
    try:
        expected_runtime_config = _worker_runtime_config(config)
        nano_cpus = host_config["NanoCpus"]
        memory = host_config["Memory"]
        memory_swap = host_config["MemorySwap"]
        pids_limit = host_config["PidsLimit"]
        worker_limits = current["cgroup_limits"][container_id]
        quota, period = map(int, worker_limits["cpu.max"].split())
    except (KeyError, TypeError, ValueError, CarryForwardError) as error:
        raise CarryForwardError(code) from error
    limit_fields = {
        "cpu.max", "memory.max", "memory.swap.max", "pids.max",
        "cgroup.subtree_control", "cgroup.procs", "uid", "gid",
    }
    expected_cgroup = f"0::{current['control_group']}/{container_id}/agent"
    expected_targets = {
        (item["mount"]["filesystem"], item["mount"]["major_minor"],
         item["mount"]["root"])
        for item in current["volume_backings"]["volumes"].values()
    }
    for scan in scans:
        related = scan["related"][0] if len(scan["related"]) == 1 else None
        members = related.get("members") if isinstance(related, dict) else None
        mounts = related.get("mounts") if isinstance(related, dict) else None
        if (
            not isinstance(members, list) or not members
            or any(
                not isinstance(member, dict)
                or set(member) != {"pid", "tid", "pid_starttime", "cgroup"}
                or member["pid"] != authority.get("pid")
                or type(member["tid"]) is not int or member["tid"] <= 0
                or re.fullmatch(r"[1-9][0-9]*", str(member["pid_starttime"])) is None
                or member["cgroup"] != expected_cgroup
                for member in members
            )
            or not any(
                member["tid"] == authority.get("pid")
                and member["pid_starttime"] == authority.get("pid_starttime")
                for member in members
            )
            or not isinstance(mounts, list) or len(mounts) != 2
            or related.get("mounts_digest") != digest(mounts)
            or any(not _selected_mount_matches(mount, expected_targets) for mount in mounts)
            or {
                (mount.get("filesystem"), mount.get("major_minor"), mount.get("root"))
                for mount in mounts
            } != expected_targets
        ):
            raise CarryForwardError(code)
    if (
        not isinstance(authority, dict)
        or any(authority.get(key) != proof[key]
               for key in ("container_id", "image_id", "started_at"))
        or any(authority.get(key) != worker.get(key)
               for key in ("container_id", "image_id", "started_at"))
        or authority.get("labels") != {
            key: worker.get("labels", {}).get(key)
            for key in ("com.docker.compose.project", "com.docker.compose.service")
        }
        or type(authority.get("pid")) is not int or authority["pid"] <= 0
        or not isinstance(authority.get("pid_starttime"), str)
        or re.fullmatch(r"mnt:\[[1-9][0-9]*\]", str(authority.get("mount_namespace"))) is None
        or re.fullmatch(r"cgroup:\[[1-9][0-9]*\]", str(authority.get("cgroup_namespace"))) is None
        or any(
            scan["pin_count"] != 0 or len(scan["related"]) != 1
            or scan["related"][0].get("namespace") != authority.get("mount_namespace")
            for scan in scans
        )
        or authority.get("parent_device") != keeper.get("parent_device")
        or authority.get("parent_inode") != keeper.get("parent_inode")
        or not isinstance(state, dict) or state.get("Pid") != authority.get("pid")
        or state.get("StartedAt") != authority.get("started_at")
        or authority.get("runtime_config") != expected_runtime_config
        or type(nano_cpus) is not int or nano_cpus <= 0
        or quota * 1_000_000_000 != nano_cpus * period
        or type(memory) is not int or memory <= 0
        or worker_limits["memory.max"] != str(memory)
        or type(memory_swap) is not int or memory_swap < memory
        or worker_limits["memory.swap.max"] != str(memory_swap - memory)
        or type(pids_limit) is not int or pids_limit <= 0
        or worker_limits["pids.max"] != str(pids_limit)
        or set(current["cgroup_limits"])
        != {".", "agent", container_id, f"{container_id}/agent"}
        or any(
            not isinstance(item, dict) or set(item) != limit_fields
            or type(item["uid"]) is not int or type(item["gid"]) is not int
            for item in current["cgroup_limits"].values()
        )
        or not volumes_match
        or not _container_matches_profile(
            worker, profile["old_profiles"]["worker"], profile["project"], "worker"
        )
        or (authority.get("root_device"), authority.get("root_inode")) != (
            proof["preflight_receipt"]["namespace_identity"]["root_device"],
            proof["preflight_receipt"]["namespace_identity"]["root_inode"],
        )
        or proof["preflight_receipt"]["namespace_identity"].get("boot_id")
        != keeper["boot_id"]
        or proof["preflight_receipt"]["namespace_identity"].get("parent_device")
        != keeper["parent_device"]
        or proof["preflight_receipt"]["namespace_identity"].get("parent_inode")
        != keeper["parent_inode"]
        or not isinstance(children, dict)
        or set(children) != {"agent", container_id, f"{container_id}/agent"}
        or not isinstance(agent, dict)
        or set(agent) != {"populated", "process_count", "process_digest", "device", "inode"}
        or agent.get("populated") != 1
        or agent.get("process_count") != 1
        or agent.get("process_digest") != digest([current["keeper_pid"]])
        or (agent.get("device"), agent.get("inode"))
        != (keeper["children"]["agent"].get("device"), keeper["children"]["agent"].get("inode"))
        or not isinstance(worker_root, dict)
        or set(worker_root) != {"populated", "process_count", "process_digest", "device", "inode"}
        or worker_root.get("populated") != 1
        or worker_root.get("process_count") != 0
        or worker_root.get("process_digest") != digest([])
        or (worker_root.get("device"), worker_root.get("inode"))
        != (authority.get("root_device"), authority.get("root_inode"))
        or not isinstance(worker_agent, dict)
        or set(worker_agent) != {"populated", "process_count", "process_digest", "device", "inode"}
        or worker_agent.get("populated") != 1
        or worker_agent.get("process_count") != 1
        or worker_agent.get("process_digest") != digest([authority.get("pid")])
        or current.get("retired_markers") != []
    ):
        raise CarryForwardError(code)
    return current


def _validate_group2_reboot_parent_volume_identity(
    parent_kernel: Any, current_kernel: Any
) -> None:
    code = "group2_reboot_kernel_changed"
    parent_authority = (
        parent_kernel.get("old_worker_authority")
        if isinstance(parent_kernel, dict) else None
    )
    parent_volumes = (
        parent_authority.get("volumes") if isinstance(parent_authority, dict) else None
    )
    current_backings = (
        current_kernel.get("volume_backings")
        if isinstance(current_kernel, dict) else None
    )
    current_volumes = (
        current_backings.get("volumes") if isinstance(current_backings, dict) else None
    )
    if (
        not isinstance(parent_volumes, dict)
        or not isinstance(current_volumes, dict)
        or set(parent_volumes) != {"runtime", "journal"}
        or set(current_volumes) != set(parent_volumes)
        or any(
            current_volumes[name].get(key) != parent_volumes[name].get(key)
            for name in parent_volumes for key in ("name", "device", "inode")
        )
    ):
        raise CarryForwardError(code)


def validate_group2_post_finalize_reboot_transition(
    validated: Any, previous: Any, current: Any, phase: str
) -> dict[str, Any]:
    code = "group2_reboot_transition_invalid"
    if phase not in GROUP2_POST_FINALIZE_REBOOT_PHASES[1:-1]:
        raise CarryForwardError(code)
    previous_phase = GROUP2_POST_FINALIZE_REBOOT_PHASES[
        GROUP2_POST_FINALIZE_REBOOT_PHASES.index(phase) - 1
    ]
    previous = _post_finalize_reboot_stage(previous, previous_phase)
    current = _post_finalize_reboot_stage(current, phase)
    request = validated["request"]
    if (
        previous["boot_id"] != request["boot_id"]
        or current["boot_id"] != request["boot_id"]
        or current["images"] != previous["images"]
        or current["storage"] != previous["storage"]
        or current["window_start_ns"] < request["window"]["not_before_ns"]
        or current["window_end_ns"] > request["window"]["deadline_ns"]
        or current["window_start_ns"] < previous["window_end_ns"]
    ):
        raise CarryForwardError(code)
    if set(current["container_inspect"]) != set(request["prior"]["containers"]):
        raise CarryForwardError(code)
    for service, raw in current["container_inspect"].items():
        _validate_reboot_container_raw(
            raw, request["prior"]["containers"][service],
            current["containers"][service], stopped=False
        )
    if phase == "keeper_ready":
        _validate_group2_reboot_empty_log_segment(previous["logs"], current["logs"])
        _validate_group2_reboot_keeper_kernel(current["kernel"], request)
        _validate_group2_reboot_parent_volume_identity(
            validated["parent"]["evidence"]["current"].get("kernel"),
            current["kernel"],
        )
        if (
            {key: value for key, value in current["authority"].items() if key != "keeper"}
            != {key: value for key, value in previous["authority"].items() if key != "keeper"}
            or previous["authority"].get("keeper") != "missing"
            or current["authority"].get("keeper") != current["kernel"]
            or current["files"] != previous["files"]
            or current["containers"] != previous["containers"]
            or current["postgres"] != previous["postgres"]
        ):
            raise CarryForwardError(code)
    elif phase == "database_ready":
        _validate_group2_reboot_empty_log_segment(previous["logs"], current["logs"])
        if (
            current["authority"] != previous["authority"]
            or current["db"] != validated["parent"]["snapshot"]["db"]
            or current["files"] != previous["files"]
        ):
            raise CarryForwardError(code)
        validate_group2_reboot_start_admission(
            current["start_admission"], current["db"], request["prior"]["parameters"],
            request["window"]["deadline_ns"],
            request["window"]["successor_deadline_ns"],
            current["containers"]["rabbitmq"].get("image_id"),
            [item["execution_id"] for item in validated["parent"]["snapshot"].get("selection", {}).get("queued", [])],
            request["prior"]["containers"],
        )
        _validate_group2_reboot_postgres(
            current["postgres"], validated["parent"]["evidence"]["postgres"],
            request["prior"]["containers"]["postgres"]["image_id"],
            running=True, baseline=previous["postgres"],
        )
        for service in ("postgres", "rabbitmq"):
            before, after = previous["containers"][service], current["containers"][service]
            if (
                before.get("container_id") != after.get("container_id")
                or before.get("image_id") != after.get("image_id")
                or after.get("status") != "running"
                or after.get("health") != "healthy"
            ):
                raise CarryForwardError(code)
        if any(
            current["containers"][name] != previous["containers"][name]
            for name in ("control", "worker", "web", "account-web")
        ):
            raise CarryForwardError(code)
    elif phase == "applications_started":
        if (
            current["authority"] != previous["authority"]
            or current["db"] != previous["db"]
            or current["postgres"] != previous["postgres"]
            or current["start_admission"] != previous["start_admission"]
        ):
            raise CarryForwardError(code)
        profile = validated["platform"]["account_entry"]
        worker_profile = profile["old_profiles"]["worker"]
        proof = _same_container_worker_startup_proof(
            current["startup_request"], profile,
            previous["containers"]["worker"]["image_id"], worker_profile,
            validated["platform"]["prior_nonces"],
        )
        if current["startup_proof"] != proof:
            raise CarryForwardError(code)
        keeper_kernel = current["authority"].get("keeper")
        _validate_group2_reboot_running_kernel(
            keeper_kernel, current["kernel"], proof,
            current["containers"]["worker"], current["container_inspect"]["worker"],
            profile, request,
        )
        startup_request = current["startup_request"]
        if (
            startup_request.get("profile") != profile
            or startup_request.get("logs_before") != previous["logs"]
            or startup_request.get("logs_after") != current["logs"]
            or startup_request.get("container_before") != previous["containers"]["worker"]
            or startup_request.get("container_after") != current["containers"]["worker"]
            or startup_request.get("window_start_ns") < current["window_start_ns"]
            or startup_request.get("window_end_ns") > current["window_end_ns"]
        ):
            raise CarryForwardError(code)
        try:
            _validate_log_link(previous["logs"], current["logs"], profile["profile_digest"])
            _validate_reconcile_log_activity(current["logs"], allow_registration=True)
        except CarryForwardError as error:
            raise CarryForwardError(code) from error
        _validate_group2_reboot_postgres(
            current["postgres"], validated["parent"]["evidence"]["postgres"],
            request["prior"]["containers"]["postgres"]["image_id"],
            running=True, baseline=previous["postgres"],
        )
        _validate_group2_reboot_mutation_guard(current["mutation_guard"])
        if current["mutation_guard"] != previous["start_admission"]["mutation_guard_rows"]:
            raise CarryForwardError(code)
        validate_group2_reboot_rabbit_identity(
            current["rabbit_identity"], current["start_admission"]["configuration"],
            current["container_inspect"],
        )
        if not (
            current["window_start_ns"] <= current["rabbit_identity"]["captured_start_ns"]
            <= current["rabbit_identity"]["captured_end_ns"] <= current["window_end_ns"]
        ):
            raise CarryForwardError(code)
        result = compare_group2_startup_files(
            previous["files"], current["files"], proof
        )
        if current["startup_files"] != result:
            raise CarryForwardError(code)
        for service in ("control", "worker", "web"):
            before, after = previous["containers"][service], current["containers"][service]
            if (
                before.get("container_id") != after.get("container_id")
                or before.get("image_id") != after.get("image_id")
                or after.get("status") != "running"
                or after.get("health") != "healthy"
                or after.get("restart_count") != 0
            ):
                raise CarryForwardError(code)
        if current["containers"]["account-web"] != previous["containers"]["account-web"]:
            raise CarryForwardError(code)
    else:
        _validate_group2_reboot_mutation_guard(current["mutation_guard"])
        validate_group2_reboot_rabbit_identity(
            current["rabbit_identity"], current["start_admission"]["configuration"],
            current["container_inspect"],
        )
        if not (
            current["window_start_ns"] <= current["rabbit_identity"]["captured_start_ns"]
            <= current["rabbit_identity"]["captured_end_ns"] <= current["window_end_ns"]
        ):
            raise CarryForwardError(code)
        if (
            current["authority"] != previous["authority"]
            or current["db"] != previous["db"]
            or current["files"] != previous["files"]
            or current["startup_proof"] != previous["startup_proof"]
            or current["startup_files"] != previous["startup_files"]
            or current["startup_request"] != previous["startup_request"]
            or current["postgres"] != previous["postgres"]
            or current["start_admission"] != previous["start_admission"]
            or current["mutation_guard"] != previous["mutation_guard"]
            or current["containers"] != previous["containers"]
        ):
            raise CarryForwardError(code)
        keeper_kernel = current["authority"].get("keeper")
        _validate_group2_reboot_running_kernel(
            keeper_kernel, current["kernel"], current["startup_proof"],
            current["containers"]["worker"], current["container_inspect"]["worker"],
            validated["platform"]["account_entry"], request,
        )
        if any(
            current["kernel"][key] != previous["kernel"][key]
            for key in set(current["kernel"]) - {"namespace_evidence"}
        ):
            raise CarryForwardError(code)
        current_namespace = current["kernel"]["namespace_evidence"]
        previous_namespace = previous["kernel"]["namespace_evidence"]
        if (
            {key: current_namespace[key] for key in ("schema", "mode", "targets")}
            != {key: previous_namespace[key] for key in ("schema", "mode", "targets")}
            or any(
                current_namespace["scans"][index][key]
                != previous_namespace["scans"][index][key]
                for index in (0, 1)
                for key in ("related", "pins", "related_count", "pin_count", "target_digest")
            )
        ):
            raise CarryForwardError(code)
        _validate_group2_reboot_postgres(
            current["postgres"], validated["parent"]["evidence"]["postgres"],
            request["prior"]["containers"]["postgres"]["image_id"],
            running=True, baseline=previous["postgres"],
        )
        try:
            _validate_log_link(
                previous["logs"], current["logs"],
                validated["platform"]["account_entry"]["profile_digest"],
            )
            _validate_reconcile_quiet_logs(current["logs"])
        except CarryForwardError as error:
            raise CarryForwardError(code) from error
    return current


def validate_group2_post_finalize_reboot_result(
    validated: Any, evidence: Any
) -> dict[str, Any]:
    code = "group2_reboot_result_invalid"
    evidence = _closed_object(
        evidence,
        {"stopped", "keeper_ready", "database_ready", "applications_started",
         "verified", "authority", "commands"},
        code,
    )
    stopped_raw = evidence["stopped"]
    stopped = validate_group2_post_finalize_reboot_stopped(validated, stopped_raw)
    stopped_stage = {
        **stopped, "schema": "group2-post-finalize-reboot-stage-v1",
        "phase": "stopped",
    }
    current = stopped_stage
    for phase in GROUP2_POST_FINALIZE_REBOOT_PHASES[1:-1]:
        current = validate_group2_post_finalize_reboot_transition(
            validated, current, evidence[phase], phase
        )
    authority = _closed_object(
        evidence["authority"], {"before", "after"}, code
    )
    if (
        authority["before"] != stopped["authority"]
        or authority["after"] != current["authority"]
        or authority["before"].get("keeper") != "missing"
        or authority["after"].get("keeper") != evidence["keeper_ready"]["kernel"]
        or any(
            authority["before"].get(key) != authority["after"].get(key)
            for key in ("host_files", "vm_files", "installed")
        )
    ):
        raise CarryForwardError("group2_reboot_authority_changed")
    request = validated["request"]
    _validate_group2_reboot_commands(evidence["commands"], request, evidence)
    receipt = {
        "schema": "group2-post-finalize-reboot-receipt-v1",
        "incident_id": request["incident_id"],
        "finalize_id": request["finalize_id"],
        "boot_id": request["boot_id"],
        "request_digest": request["request_digest"],
        "tool_sha": request["tool"]["sha"],
        "parent_snapshot_digest": digest(validated["parent"]["snapshot"]),
        "startup_proof": current["startup_proof"],
        "startup_files": current["startup_files"],
        "stage_digests": {
            name: digest(evidence[name])
            for name in (
                "stopped", "keeper_ready", "database_ready",
                "applications_started", "verified",
            )
        },
        "authority_digest": digest(authority),
        "evidence_digest": digest(evidence),
    }
    receipt["receipt_digest"] = digest(receipt)
    return receipt


def _validate_group2_reboot_commands(
    value: Any, request: dict[str, Any], evidence: dict[str, Any]
) -> None:
    code = "group2_reboot_command_evidence_invalid"
    actions = ("prepare-keeper", "start-postgres", "start-rabbitmq",
               "start-control", "start-worker", "start-web")
    value = _closed_object(value, set(actions), code)
    previous_end = request["window"]["not_before_ns"]
    params = request["prior"]["parameters"]["keeper"]
    for action in actions:
        pair = _closed_object(value[action], {"intent", "result"}, code)
        try:
            intent = json.loads(_validate_embedded_bytes(pair["intent"], code))
            result = json.loads(_validate_embedded_bytes(pair["result"], code))
        except (UnicodeError, json.JSONDecodeError) as error:
            raise CarryForwardError(code) from error
        intent = _closed_object(
            intent, {"schema", "action", "argv", "started_at_ns"}, code
        )
        result = _closed_object(
            result,
            {"schema", "action", "argv", "started_at_ns", "ended_at_ns",
             "returncode", "stdout", "stderr"}, code,
        )
        if action == "prepare-keeper":
            argv = intent["argv"]
            valid_argv = (
                isinstance(argv, list) and len(argv) == 7
                and isinstance(argv[0], str)
                and Path(argv[0]).name == "prepare-sandbox-host.sh"
                and argv[1:] == ["--unit", params["unit"], "--cpu-quota",
                                  params["cpu_quota"], "--memory-max",
                                  params["memory_max"]]
            )
        else:
            service = action.removeprefix("start-")
            valid_argv = intent["argv"] == [
                "docker", "start", request["prior"]["containers"][service]["container_id"]
            ]
        if (
            intent["schema"] != "group2-post-finalize-reboot-command-intent-v1"
            or result["schema"] != "group2-post-finalize-reboot-command-result-v1"
            or intent["action"] != action or result["action"] != action
            or result["argv"] != intent["argv"] or not valid_argv
            or result["started_at_ns"] != intent["started_at_ns"]
            or not isinstance(intent["started_at_ns"], int)
            or not isinstance(result["ended_at_ns"], int)
            or intent["started_at_ns"] < previous_end
            or result["ended_at_ns"] < intent["started_at_ns"]
            or result["ended_at_ns"] > request["window"]["deadline_ns"]
            or result["returncode"] != 0
            or not isinstance(result["stdout"], str)
            or not isinstance(result["stderr"], str)
        ):
            raise CarryForwardError(code)
        if action == "prepare-keeper":
            phase_start = evidence["keeper_ready"]["window_start_ns"]
            phase_end = evidence["keeper_ready"]["window_end_ns"]
        elif action in {"start-postgres", "start-rabbitmq"}:
            phase_start = evidence["database_ready"]["window_start_ns"]
            phase_end = evidence["database_ready"]["window_end_ns"]
        else:
            phase_start = evidence["applications_started"]["window_start_ns"]
            phase_end = evidence["applications_started"]["window_end_ns"]
            if intent["started_at_ns"] < evidence["database_ready"]["window_end_ns"]:
                raise CarryForwardError(code)
        if (
            intent["started_at_ns"] < phase_start
            or result["ended_at_ns"] > phase_end
        ):
            raise CarryForwardError(code)
        previous_end = result["ended_at_ns"]


def validate_group2_post_finalize_reboot_preservation(chain: Any) -> dict[str, Any]:
    code = "group2_reboot_chain_invalid"
    chain = _closed_object(
        chain,
        {"schema", "request", "approval", "user_record", "source_artifacts", "result"},
        code,
    )
    if chain["schema"] != "group2-post-finalize-reboot-chain-v1":
        raise CarryForwardError(code)
    source = _closed_object(
        chain["source_artifacts"], GROUP2_POST_FINALIZE_REBOOT_FILES, code
    )
    raw = {name: _validate_embedded_bytes(value, code) for name, value in source.items()}
    user_record = _validate_embedded_bytes(chain["user_record"], code)
    validated = validate_group2_post_finalize_reboot_request(
        chain["request"], chain["approval"], user_record, raw
    )
    result = _closed_object(chain["result"], {"schema", "evidence", "receipt"}, code)
    if result["schema"] != "group2-post-finalize-reboot-result-v1":
        raise CarryForwardError(code)
    receipt = validate_group2_post_finalize_reboot_result(validated, result["evidence"])
    if result["receipt"] != receipt:
        raise CarryForwardError(code)
    parent = validated["parent"]["snapshot"]
    request = validated["request"]
    return {
        "selection": parent["selection"],
        "db": parent["db"],
        "files": result["evidence"]["verified"]["files"],
        "lineage": [
            *parent["lineage"],
            {
                "name": (
                    f"incident/{request['incident_id']}/finalize/{request['finalize_id']}"
                    f"/reboot/{request['boot_id']}/request"
                ),
                "sha256": request["request_digest"],
            },
            {
                "name": (
                    f"incident/{request['incident_id']}/finalize/{request['finalize_id']}"
                    f"/reboot/{request['boot_id']}/receipt"
                ),
                "sha256": receipt["receipt_digest"],
            },
            {
                "name": (
                    f"incident/{request['incident_id']}/finalize/{request['finalize_id']}"
                    f"/reboot/{request['boot_id']}/chain"
                ),
                "sha256": streaming_digest(chain),
            },
        ],
    }


def _validate_group2_partial_finalize_preservation(
    chain: Any, original_reference: Any, validated: Any, *, include_context: bool = False
) -> dict[str, Any]:
    code = "group2_partial_finalize_chain_invalid"
    chain = _closed_object(
        chain,
        {"schema", "request", "approval", "user_record", "source_artifacts", "result"},
        code,
    )
    if chain["schema"] != "group2-partial-finalize-chain-v1":
        raise CarryForwardError(code)
    source_artifacts = _closed_object(
        chain["source_artifacts"], GROUP2_PARTIAL_FINALIZE_FILES, code
    )
    user_record = _validate_embedded_bytes(chain["user_record"], code)
    if validated is None:
        raw_artifacts = {
            name: _validate_embedded_bytes(value, code)
            for name, value in source_artifacts.items()
        }
        validated = validate_group2_partial_finalize_request(
            chain["request"], chain["approval"], user_record, raw_artifacts
        )
    else:
        validated = _closed_object(
            validated,
            {"request", "approval", "user_record", "artifacts", "context"},
            code,
        )
        try:
            parsed_user_record = json.loads(user_record)
        except (UnicodeError, json.JSONDecodeError) as error:
            raise CarryForwardError(code) from error
        if (
            chain["request"] != validated["request"]
            or chain["approval"] != validated["approval"]
            or parsed_user_record != validated["user_record"]
            or hashlib.sha256(user_record).hexdigest()
            != validated["approval"]["user_record_sha256"]
        ):
            raise CarryForwardError(code)
        for name, value in source_artifacts.items():
            _validate_embedded_digest(
                value, validated["request"]["evidence_files"][name], code
            )
    result = _closed_object(
        chain["result"], {"schema", "evidence", "receipt"}, code
    )
    if result["schema"] != "group2-partial-finalize-result-v1":
        raise CarryForwardError(code)
    receipt = validate_group2_partial_finalize_result(
        validated, result["evidence"]
    )
    reference = validated["context"]["originals"]["manifest"]["review_scope"][
        "preservation_reference"
    ]
    if original_reference != reference or result["receipt"] != receipt:
        raise CarryForwardError(code)
    original = reference["snapshot"]
    request = validated["request"]
    chain_digest = streaming_digest(chain)
    snapshot = {
        "selection": original["selection"],
        "db": original["db"],
        "files": result["evidence"]["current"]["files"],
        "lineage": [
            *original["lineage"],
            {
                "name": f"incident/{request['incident_id']}/partial-attempt",
                "sha256": request["evidence_files"]["partial-attempt.json"],
            },
            {
                "name": (
                    f"incident/{request['incident_id']}/finalize/"
                    f"{request['finalize_id']}/request"
                ),
                "sha256": request["request_digest"],
            },
            {
                "name": (
                    f"incident/{request['incident_id']}/finalize/"
                    f"{request['finalize_id']}/receipt"
                ),
                "sha256": receipt["receipt_digest"],
            },
            {
                "name": (
                    f"incident/{request['incident_id']}/finalize/"
                    f"{request['finalize_id']}/chain"
                ),
                "sha256": chain_digest,
            },
        ],
    }
    if include_context:
        return {"snapshot": snapshot, "validated": validated}
    return snapshot


def validate_group2_reconcile_request(
    request: Any, approval: Any, artifacts: Any
) -> dict[str, Any]:
    """Validate the closed, separately approved incident request without I/O."""
    request = _closed_object(
        request,
        {
            "schema", "mode", "action", "incident_id", "repo", "pr", "failed",
            "prior", "tool", "restore", "evidence_files", "request_digest",
        },
        "group2_reconcile_request_invalid",
    )
    if (
        request["schema"] != "group2-starting-reconcile-request-v1"
        or request["mode"] != GROUP2_MODE
        or request["action"] != "restore-prior-software"
        or MANIFEST_ID.fullmatch(str(request["incident_id"])) is None
        or not isinstance(request["repo"], str)
        or re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", request["repo"]) is None
    ):
        raise CarryForwardError("group2_reconcile_request_invalid")
    _positive(request["pr"], "group2_reconcile_request_invalid")
    expected_digest = digest({k: v for k, v in request.items() if k != "request_digest"})
    if request["request_digest"] != expected_digest:
        raise CarryForwardError("group2_reconcile_request_digest_mismatch")
    failed = _closed_object(
        request["failed"],
        {"sha", "manifest_id", "manifest_digest", "manifest_sha256", "scope_digest",
         "installed_controller_files_digest", "initial_evidence_digest"},
        "group2_reconcile_failed_invalid",
    )
    prior = _closed_object(
        request["prior"],
        {"sha", "manifest_id", "manifest_digest", "receipt_sha256", "consumed_sha256",
         "images_digest", "schema"},
        "group2_reconcile_prior_invalid",
    )
    tool = _closed_object(
        request["tool"],
        {"sha", "controller_files", "source_scope_digest", "review_report_sha256",
         "ci_evidence_sha256"},
        "group2_reconcile_tool_invalid",
    )
    restore = _closed_object(
        request["restore"],
        {"up", "stop_only", "retain", "storage_identity_digest", "account_policy"},
        "group2_reconcile_restore_invalid",
    )
    digest_fields = (
        (failed, ("manifest_digest", "manifest_sha256", "scope_digest",
                  "installed_controller_files_digest", "initial_evidence_digest")),
        (prior, ("manifest_digest", "receipt_sha256", "consumed_sha256", "images_digest")),
        (tool, ("source_scope_digest", "review_report_sha256", "ci_evidence_sha256")),
        (restore, ("storage_identity_digest",)),
    )
    for owner, names in digest_fields:
        for name in names:
            _digest_text(owner[name], "group2_reconcile_request_invalid")
    for owner, name in ((failed, "sha"), (prior, "sha"), (tool, "sha")):
        _sha_text(owner[name], "group2_reconcile_request_invalid")
    if (
        prior["sha"] != GROUP2_FROM_SHA
        or prior["schema"] != "0040_issue152_dispositions"
        or failed["sha"] == prior["sha"]
        or tool["sha"] == failed["sha"]
        or restore
        != {
            "up": ["postgres", "control", "worker", "web"],
            "stop_only": ["account-web"],
            "retain": ["rabbitmq"],
            "storage_identity_digest": restore["storage_identity_digest"],
            "account_policy": "stop-current-candidate-preserve-binding",
        }
    ):
        raise CarryForwardError("group2_reconcile_request_invalid")
    files = tool["controller_files"]
    if not isinstance(files, dict) or set(files) != GROUP2_CONTROLLER_FILES:
        raise CarryForwardError("group2_reconcile_tool_invalid")
    for item in files.values():
        _digest_text(item, "group2_reconcile_tool_invalid")
    evidence_files = request["evidence_files"]
    if not isinstance(evidence_files, dict) or set(evidence_files) != GROUP2_RECONCILE_EVIDENCE_FILES:
        raise CarryForwardError("group2_reconcile_artifact_invalid")
    if not isinstance(artifacts, dict) or set(artifacts) != GROUP2_RECONCILE_EVIDENCE_FILES:
        raise CarryForwardError("group2_reconcile_artifact_invalid")
    parsed = {}
    raw_hashes = {}
    for name, value in artifacts.items():
        parsed[name], actual = _reconcile_artifact_value(value)
        raw_hashes[name] = actual
        if evidence_files[name] != actual:
            raise CarryForwardError("group2_reconcile_artifact_digest_mismatch")
    _validate_reconcile_wrappers(request, parsed, raw_hashes)
    approval = _closed_object(
        approval,
        {"schema", "status", "request_digest", "tool_sha", "actions", "user_reply",
         "user_record_sha256"},
        "group2_reconcile_approval_invalid",
    )
    _digest_text(approval["user_record_sha256"], "group2_reconcile_approval_invalid")
    if (
        approval["schema"] != "group2-starting-reconcile-approval-v1"
        or approval["status"] != "USER_APPROVED"
        or approval["request_digest"] != request["request_digest"]
        or approval["tool_sha"] != tool["sha"]
        or approval["actions"] != GROUP2_RECONCILE_ACTIONS
        or not isinstance(approval["user_reply"], str)
        or not approval["user_reply"].strip()
    ):
        raise CarryForwardError("group2_reconcile_approval_invalid")
    return {"request": request, "approval": approval, "artifacts": parsed}


def _group2_reconcile_originals(validated: Any) -> dict[str, Any]:
    validated = _closed_object(
        validated, {"request", "approval", "artifacts"},
        "group2_reconcile_originals_invalid",
    )
    artifacts = validated["artifacts"]
    if not isinstance(artifacts, dict) or set(artifacts) != GROUP2_RECONCILE_EVIDENCE_FILES:
        raise CarryForwardError("group2_reconcile_originals_invalid")
    manifest = validate_manifest(artifacts["failed-manifest.json"])
    validate_group2_manifest_extensions(manifest)
    first = artifacts["first-startup.json"]
    check = first["check_raw"]
    reference = manifest["review_scope"]["preservation_reference"]["snapshot"]
    if (
        reference["db"] != first["db"]
        or reference["files"] != manifest["file_evidence"]
        or check["log-baseline.json"] != manifest["log_evidence"]
    ):
        raise CarryForwardError("group2_reconcile_originals_changed")
    previous_log = manifest["log_evidence"]
    for name in ("preflight", "control-stopped", "stopped", "after-backup", "after-migration"):
        request_value = check[f"{name}/log-request.json"]
        current_log = check[f"{name}/log.json"]["log_evidence"]
        if (
            request_value.get("baseline") != previous_log
            or check[f"{name}/db.json"] != first["db"]
            or check[f"{name}/files.json"] != manifest["file_evidence"]
        ):
            raise CarryForwardError("group2_reconcile_originals_changed")
        _validate_log_link(previous_log, current_log)
        previous_log = current_log
    before_log = check["group2/log-before-start.json"]["log_evidence"]
    after_log = first["log_read_diagnostic"]["log_append"]
    account = check["group2/account-check.json"]["account_check"]
    window = check["group2/start-window.json"]
    first_request = {
        "mode": GROUP2_MODE,
        "operation": "startup-proof",
        "profile": manifest["account_entry"],
        "logs_before": before_log,
        "logs_after": after_log,
        "container_before": manifest["account_entry"]["old_containers"]["worker"],
        "container_after": account["containers"]["worker"],
        "window_start_ns": window["window_start_ns"],
        "window_end_ns": window["window_end_ns"],
    }
    proof = _startup_proof(first_request)
    before_files = check["after-migration/files.json"]
    result = compare_group2_startup_files(before_files, first["files"], proof)
    if (
        before_log != previous_log
        or check["group2/log-after-start-request.json"].get("baseline") != before_log
        or before_files != manifest["file_evidence"]
        or first["actual_results"]["startup_reconstruction"].get("proof") != proof
    ):
        raise CarryForwardError("group2_reconcile_originals_changed")
    return {
        "manifest": manifest,
        "db": first["db"],
        "files_before_startup": before_files,
        "files_after_startup": first["files"],
        "logs_after_startup": after_log,
        "first_startup": {
            "request": first_request,
            "proof": proof,
            "before_files": before_files,
            "after_files": first["files"],
            "result": result,
        },
    }


GROUP2_RECONCILE_STAGE_FIELDS = {
    "db", "files", "logs", "kernel", "containers", "storage",
    "window_start_ns", "window_end_ns",
}


def _validate_reconcile_log_activity(
    logs: dict[str, Any], *, allow_registration: bool
) -> None:
    control = _appended_text(logs, "/control/control.log")
    registrations = 0
    for line in control.splitlines():
        match = _ACCESS.search(line)
        if match is None:
            if " /api/" in line and "HTTP/" in line:
                raise CarryForwardError("group2_reconcile_startup_changed")
            continue
        method = match.group("method")
        path = match.group("path")
        status = int(match.group("status"))
        if method not in {"POST", "PATCH", "DELETE"}:
            continue
        if (
            allow_registration
            and method == "POST"
            and path == "/api/workers/register"
            and status == 200
        ):
            registrations += 1
            continue
        if (
            method == "POST"
            and status == 204
            and re.fullmatch(
                r"/api/workers/[1-9][0-9]*/(?:heartbeat|v3/claim|cleanups/claim)",
                path,
            )
        ):
            continue
        raise CarryForwardError("group2_reconcile_startup_changed")
    if registrations != int(allow_registration):
        raise CarryForwardError("group2_reconcile_startup_changed")


def _validate_reconcile_quiet_logs(logs: dict[str, Any]) -> None:
    _validate_reconcile_log_activity(logs, allow_registration=False)
    worker = _appended_text(logs, "/worker/worker.log")
    if (
        "sandbox preflight receipt:" in worker
        or "sandbox preflight passed; rabbitmq execution gate=True" in worker
    ):
        raise CarryForwardError("group2_reconcile_startup_changed")


def _validate_reconcile_stage(value: Any) -> dict[str, Any]:
    value = _closed_object(
        value, GROUP2_RECONCILE_STAGE_FIELDS, "group2_reconcile_stage_invalid"
    )
    _validate_capture_digests(value["files"])
    if (
        not isinstance(value["db"], dict)
        or not isinstance(value["logs"], dict)
        or not isinstance(value["kernel"], dict)
        or not isinstance(value["containers"], dict)
        or set(value["containers"])
        != {"postgres", "rabbitmq", "control", "worker", "web", "account-web"}
        or not all(isinstance(item, dict) for item in value["containers"].values())
        or type(value["window_start_ns"]) is not int
        or type(value["window_end_ns"]) is not int
        or value["window_start_ns"] <= 0
        or value["window_end_ns"] < value["window_start_ns"]
    ):
        raise CarryForwardError("group2_reconcile_stage_invalid")
    validate_storage_identity(value["storage"])
    return value


def validate_group2_reconcile_transition(
    previous: Any,
    current: Any,
    *,
    kernel_baseline: dict[str, Any],
    require_idle: bool,
) -> dict[str, Any]:
    previous = _validate_reconcile_stage(previous)
    current = _validate_reconcile_stage(current)
    _compare_group2_db_strict(previous["db"], current["db"])
    if previous["db"] != current["db"]:
        raise CarryForwardError("group2_reconcile_db_changed")
    if previous["files"] != current["files"]:
        raise CarryForwardError("group2_reconcile_files_changed")
    _validate_log_link(previous["logs"], current["logs"])
    if previous["storage"] != current["storage"]:
        raise CarryForwardError("group2_reconcile_storage_changed")
    if current["window_start_ns"] < previous["window_end_ns"]:
        raise CarryForwardError("group2_reconcile_window_invalid")
    _validate_reconcile_quiet_logs(current["logs"])
    if require_idle:
        compare_kernel(kernel_baseline, current["kernel"])
        namespace = current["kernel"].get("namespace_evidence")
        markers = (
            current["files"].get("journal_facts", {}).get("sandbox_recovery", [])
        )
        if (
            not isinstance(namespace, dict)
            or namespace.get("target_digest") != digest({"related": [], "pins": []})
            or validate_retired_markers(
                markers,
                boot_id=current["kernel"].get("boot_id"),
                parent_device=current["kernel"].get("parent_device"),
                parent_inode=current["kernel"].get("parent_inode"),
                children=current["kernel"].get("children", {}),
            )
            != current["kernel"].get("retired_markers")
        ):
            raise CarryForwardError("group2_reconcile_kernel_changed")
    else:
        for key in (
            "boot_id", "unit", "control_group", "keeper_pid", "keeper_starttime",
            "description", "parent_device", "parent_inode", "children",
            "old_worker_authority",
        ):
            if kernel_baseline.get(key) != current["kernel"].get(key):
                raise CarryForwardError("group2_reconcile_kernel_changed")
        if (
            current["kernel"].get("namespace_evidence") is not None
            or current["kernel"].get("retired_markers") != []
        ):
            raise CarryForwardError("group2_reconcile_kernel_changed")
    return current


def _validate_reconcile_preflight_root(
    request: dict[str, Any], stage: Any, validated: Any
) -> tuple[dict[str, Any], dict[str, Any]]:
    stage = _validate_reconcile_stage(stage)
    historical = _group2_reconcile_originals(validated)
    authority = validated["artifacts"]["authority.json"]["vm_inventory"]
    original_kernel = authority.get("current_kernel")
    platform = validated["artifacts"]["platform.json"]
    candidate_images = {
        service: platform["images"]["candidate"][service].get("Id")
        for service in ("postgres", "control", "worker", "web")
    }
    if (
        stage["db"] != historical["db"]
        or stage["files"] != historical["files_after_startup"]
        or stage["kernel"] != original_kernel
        or stage["containers"]["worker"]
        != historical["first_startup"]["request"]["container_after"]
        or any(
            stage["containers"][service].get("image_id")
            != candidate_images[service]
            for service in candidate_images
        )
        or digest(stage["storage"]) != request["restore"]["storage_identity_digest"]
    ):
        raise CarryForwardError("group2_reconcile_originals_changed")
    kernel_authority = stage["kernel"].get("old_worker_authority")
    if not isinstance(kernel_authority, dict) or any(
        kernel_authority.get(key) != stage["containers"]["worker"].get(key)
        for key in ("container_id", "image_id", "started_at")
    ):
        raise CarryForwardError("group2_reconcile_kernel_changed")
    _validate_log_link(historical["logs_after_startup"], stage["logs"])
    _validate_reconcile_quiet_logs(stage["logs"])
    return stage, historical


def validate_group2_reconcile_preflight(
    request: Any, fresh: Any, originals: Any
) -> dict[str, Any]:
    """Compare live preflight evidence with the frozen incident originals."""
    _closed_object(request, {"schema", "mode", "action", "incident_id", "repo", "pr",
        "failed", "prior", "tool", "restore", "evidence_files", "request_digest"},
        "group2_reconcile_request_invalid")
    fresh = _closed_object(
        fresh,
        {"stage", "authority", "postgres", "images"},
        "group2_reconcile_preflight_invalid",
    )
    originals = _closed_object(
        originals,
        {"validated", "authority", "postgres", "images", "kernel"},
        "group2_reconcile_originals_invalid",
    )
    stage, _historical = _validate_reconcile_preflight_root(
        request, fresh["stage"], originals["validated"]
    )
    authority = fresh["authority"]
    if (
        not isinstance(authority, dict)
        or authority.get("enabled") is not False
        or authority.get("attention_phase") != "switching"
        or authority.get("transaction_phase") != "starting"
        or authority.get("failed_sha") != request["failed"]["sha"]
        or authority.get("current_sha") != request["prior"]["sha"]
        or authority.get("installed_controller_files_digest")
        != request["failed"]["installed_controller_files_digest"]
    ):
        raise CarryForwardError("group2_reconcile_authority_changed")
    if fresh["authority"] != originals["authority"]:
        raise CarryForwardError("group2_reconcile_authority_changed")
    if stage["kernel"] != originals["kernel"]:
        raise CarryForwardError("group2_reconcile_originals_changed")
    for key in ("postgres", "images"):
        if fresh[key] != originals[key]:
            raise CarryForwardError(f"group2_reconcile_{key}_changed")
    if any(
        stage["containers"][service].get("image_id") != fresh["images"][service]
        for service in ("postgres", "control", "worker", "web")
    ):
        raise CarryForwardError("group2_reconcile_images_changed")
    if fresh["postgres"].get("schema") != request["prior"]["schema"]:
        raise CarryForwardError("group2_reconcile_postgres_changed")
    if digest(stage["storage"]) != request["restore"]["storage_identity_digest"]:
        raise CarryForwardError("group2_reconcile_storage_changed")
    return stage


def _incident_startup_proof(request: Any) -> dict[str, Any]:
    """Reuse the normal startup oracle with the prior Worker bound as candidate."""
    value = _closed_request(
        request,
        "startup-proof",
        {"profile", "logs_before", "logs_after", "container_before", "container_after",
         "window_start_ns", "window_end_ns"},
    )
    profile = validate_group2_account_entry(value["profile"])
    return _worker_startup_proof(
        value,
        profile,
        profile["old_containers"]["worker"]["image_id"],
        profile["old_profiles"]["worker"],
    )


def _same_container_identity(before: dict[str, Any], after: dict[str, Any]) -> bool:
    return all(
        before.get(key) == after.get(key)
        for key in (
            "container_id", "image_id", "started_at", "restart_count", "command",
            "labels", "port_bindings", "mounts", "networks",
        )
    )


def _validate_reconcile_container_transition(
    previous: dict[str, Any], current: dict[str, Any], phase: str
) -> None:
    before = previous["containers"]
    after = current["containers"]
    if phase == "control-stopped":
        if (
            not _same_container_identity(before["control"], after["control"])
            or after["control"].get("status") == "running"
            or any(after[name] != before[name] for name in before if name != "control")
        ):
            raise CarryForwardError("group2_reconcile_container_changed")
        return
    if phase == "apps-stopped":
        if any(
            not _same_container_identity(before[name], after[name])
            or after[name].get("status") == "running"
            for name in ("control", "worker", "web", "account-web")
        ) or any(after[name] != before[name] for name in ("postgres", "rabbitmq")):
            raise CarryForwardError("group2_reconcile_container_changed")
        return
    if phase == "postgres-restored":
        if any(
            after[name] != before[name]
            for name in ("rabbitmq", "control", "worker", "web", "account-web")
        ):
            raise CarryForwardError("group2_reconcile_container_changed")
        return
    raise CarryForwardError("group2_reconcile_stage_invalid")


def _validate_restored_running_kernel(
    before: dict[str, Any], after: dict[str, Any], proof: dict[str, Any],
    worker: dict[str, Any], profile: dict[str, Any],
) -> None:
    for key in (
        "boot_id", "unit", "control_group", "keeper_pid", "keeper_starttime",
        "description", "parent_device", "parent_inode",
    ):
        if before.get(key) != after.get(key):
            raise CarryForwardError("group2_reconcile_kernel_changed")
    authority = after.get("old_worker_authority")
    original_authority = before.get("old_worker_authority")
    children = after.get("children")
    container_id = proof["container_id"]
    keeper = children.get("agent") if isinstance(children, dict) else None
    worker_root = children.get(container_id) if isinstance(children, dict) else None
    worker_agent = (
        children.get(f"{container_id}/agent") if isinstance(children, dict) else None
    )
    if (
        not isinstance(authority, dict)
        or not isinstance(original_authority, dict)
        or any(
            authority.get(key) != proof[key]
            for key in ("container_id", "image_id", "started_at")
        )
        or any(
            authority.get(key) != worker.get(key)
            for key in ("container_id", "image_id", "started_at")
        )
        or authority.get("labels")
        != {
            key: worker.get("labels", {}).get(key)
            for key in (
                "com.docker.compose.project",
                "com.docker.compose.service",
            )
        }
        or authority.get("runtime_config") != original_authority.get("runtime_config")
        or authority.get("volumes") != original_authority.get("volumes")
        or authority.get("parent_device") != original_authority.get("parent_device")
        or authority.get("parent_inode") != original_authority.get("parent_inode")
        or not isinstance(children, dict)
        or set(children) != {"agent", container_id, f"{container_id}/agent"}
        or not _container_matches_profile(
            worker, profile["old_profiles"]["worker"], profile["project"], "worker"
        )
        or (authority.get("root_device"), authority.get("root_inode"))
        != (
            proof["preflight_receipt"]["namespace_identity"]["root_device"],
            proof["preflight_receipt"]["namespace_identity"]["root_inode"],
        )
        or not isinstance(keeper, dict)
        or keeper.get("populated") != 1
        or keeper.get("process_count") != 1
        or keeper.get("process_digest") != digest([after["keeper_pid"]])
        or not isinstance(worker_root, dict)
        or worker_root.get("populated") != 1
        or worker_root.get("process_count") != 0
        or worker_root.get("process_digest") != digest([])
        or (worker_root.get("device"), worker_root.get("inode"))
        != (authority.get("root_device"), authority.get("root_inode"))
        or not isinstance(worker_agent, dict)
        or worker_agent.get("populated") != 1
        or worker_agent.get("process_count") != 1
        or worker_agent.get("process_digest") != digest([authority.get("pid")])
        or after.get("namespace_evidence") is not None
        or after.get("retired_markers") != []
    ):
        raise CarryForwardError("group2_reconcile_kernel_changed")


def validate_group2_reconcile_result(
    request: Any, evidence: Any, validated_originals: Any
) -> dict[str, Any]:
    evidence = _closed_object(
        evidence,
        {"preflight", "stopped", "restored_postgres", "restore_startup", "restored",
         "account", "images", "storage", "token_health"},
        "group2_reconcile_result_invalid",
    )
    if validated_originals.get("request") != request:
        raise CarryForwardError("group2_reconcile_originals_changed")
    preflight, originals = _validate_reconcile_preflight_root(
        request, evidence["preflight"], validated_originals
    )
    stopped = _closed_object(
        evidence["stopped"], {"control", "apps"}, "group2_reconcile_result_invalid"
    )
    control = _validate_reconcile_stage(stopped["control"])
    apps = _validate_reconcile_stage(stopped["apps"])
    restored_postgres = _validate_reconcile_stage(evidence["restored_postgres"])
    restored = _validate_reconcile_stage(evidence["restored"])
    if (
        preflight["db"] != originals["db"]
        or preflight["files"] != originals["files_after_startup"]
    ):
        raise CarryForwardError("group2_reconcile_originals_changed")
    _validate_log_link(originals["logs_after_startup"], preflight["logs"])
    validate_group2_reconcile_transition(
        preflight, control, kernel_baseline=preflight["kernel"], require_idle=False
    )
    _validate_reconcile_log_activity(control["logs"], allow_registration=False)
    validate_group2_reconcile_transition(
        control, apps, kernel_baseline=preflight["kernel"], require_idle=True
    )
    _validate_reconcile_log_activity(apps["logs"], allow_registration=False)
    validate_group2_reconcile_transition(
        apps, restored_postgres,
        kernel_baseline=preflight["kernel"], require_idle=True,
    )
    _validate_reconcile_log_activity(
        restored_postgres["logs"], allow_registration=False
    )
    for item in (control, apps, restored_postgres, restored):
        if item["db"] != originals["db"]:
            raise CarryForwardError("group2_reconcile_db_changed")
    _validate_reconcile_container_transition(preflight, control, "control-stopped")
    _validate_reconcile_container_transition(control, apps, "apps-stopped")
    _validate_reconcile_container_transition(apps, restored_postgres, "postgres-restored")
    proof = _incident_startup_proof(evidence["restore_startup"]["request"])
    startup_request = evidence["restore_startup"]["request"]
    first_proof = originals["first_startup"]["proof"]
    if (
        proof != evidence["restore_startup"].get("proof")
        or startup_request.get("logs_before") != restored_postgres["logs"]
        or startup_request.get("logs_after") != restored["logs"]
        or startup_request.get("container_before")
        != restored_postgres["containers"]["worker"]
        or startup_request.get("container_after") != restored["containers"]["worker"]
        or startup_request.get("window_start_ns") < restored_postgres["window_end_ns"]
        or startup_request.get("window_end_ns") > restored["window_start_ns"]
        or first_proof["window_end_ns"] >= preflight["window_start_ns"]
        or proof["nonce"] == first_proof["nonce"]
        or proof["container_id"] == first_proof["container_id"]
        or proof["container_id"]
        == originals["first_startup"]["request"]["container_before"]["container_id"]
    ):
        raise CarryForwardError("group2_reconcile_startup_changed")
    file_result = compare_group2_startup_files(
        restored_postgres["files"], restored["files"], proof
    )
    _validate_log_link(restored_postgres["logs"], restored["logs"])
    _validate_reconcile_log_activity(restored["logs"], allow_registration=True)
    if restored_postgres["storage"] != restored["storage"]:
        raise CarryForwardError("group2_reconcile_storage_changed")
    _validate_restored_running_kernel(
        preflight["kernel"], restored["kernel"], proof,
        restored["containers"]["worker"],
        originals["manifest"]["account_entry"],
    )
    account = _closed_object(evidence["account"], {"before", "after"}, "group2_reconcile_account_invalid")
    if (
        not isinstance(account["before"], dict)
        or not isinstance(account["after"], dict)
        or not _same_container_identity(account["before"], account["after"])
        or account["after"].get("status") == "running"
    ):
        raise CarryForwardError("group2_reconcile_account_changed")
    if evidence["storage"] != request["restore"]["storage_identity_digest"]:
        raise CarryForwardError("group2_reconcile_storage_changed")
    expected_images = evidence["images"]
    if (
        not isinstance(expected_images, dict)
        or set(expected_images)
        != {"prior", "restored", "rabbitmq_before", "rabbitmq_after", "postgres"}
        or not isinstance(expected_images["prior"], dict)
        or not isinstance(expected_images["restored"], dict)
        or set(expected_images["restored"]) != {"postgres", "control", "worker", "web"}
        or expected_images["rabbitmq_after"] != expected_images["rabbitmq_before"]
        or digest(expected_images["prior"]) != request["prior"]["images_digest"]
        or expected_images["postgres"]
        != {
            "preflight": {
                "schema": request["prior"]["schema"],
                "binary_version": validated_originals["artifacts"]["platform.json"]["postgres_format"]["candidate_binary_version"],
                "server_version": validated_originals["artifacts"]["platform.json"]["postgres_format"]["current_server_version"],
                "data_pg_version": validated_originals["artifacts"]["platform.json"]["postgres_format"]["data_pg_version"],
            },
            "restored": {
                "schema": request["prior"]["schema"],
                "binary_version": validated_originals["artifacts"]["platform.json"]["postgres_format"]["prior_binary_version"],
                "server_version": validated_originals["artifacts"]["platform.json"]["postgres_format"]["current_server_version"],
                "data_pg_version": validated_originals["artifacts"]["platform.json"]["postgres_format"]["data_pg_version"],
            },
        }
    ):
        raise CarryForwardError("group2_reconcile_images_changed")
    for service, actual in expected_images["restored"].items():
        matches = [
            image for tag, image in expected_images["prior"].items()
            if tag.endswith(f"-{service}:{request['prior']['sha']}")
        ]
        if matches != [actual]:
            raise CarryForwardError("group2_reconcile_images_changed")
        if restored["containers"][service].get("image_id") != actual:
            raise CarryForwardError("group2_reconcile_images_changed")
    if (
        evidence["account"]
        != {
            "before": preflight["containers"]["account-web"],
            "after": restored["containers"]["account-web"],
        }
        or expected_images["rabbitmq_before"]
        != preflight["containers"]["rabbitmq"].get("image_id")
        or expected_images["rabbitmq_after"]
        != restored["containers"]["rabbitmq"].get("image_id")
        or restored["containers"]["rabbitmq"]
        != preflight["containers"]["rabbitmq"]
        or restored["containers"]["postgres"]
        != restored_postgres["containers"]["postgres"]
        or any(
            not _container_matches_profile(
                restored["containers"][service],
                originals["manifest"]["account_entry"]["old_profiles"][service],
                originals["manifest"]["account_entry"]["project"],
                service,
            )
            for service in ("control", "worker", "web")
        )
        or any(
            restored["containers"][service].get("status") != "running"
            for service in ("postgres", "rabbitmq", "control", "worker", "web")
        )
    ):
        raise CarryForwardError("group2_reconcile_images_changed")
    if evidence["token_health"] != {"status": 200, "database": True}:
        raise CarryForwardError("group2_reconcile_health_failed")
    payload = {
        "schema": "group2-software-reconcile-v1",
        "result": "restored_prior_software",
        "incident_id": request["incident_id"],
        "tool_sha": request["tool"]["sha"],
        "failed_sha": request["failed"]["sha"],
        "restored_sha": request["prior"]["sha"],
        "request_digest": request["request_digest"],
        "restored": restored,
        "startup_proof": proof,
        "startup_files": file_result,
        "account": account["after"],
        "images": expected_images,
        "storage_identity_digest": evidence["storage"],
        "evidence_digest": digest(evidence),
    }
    payload["receipt_digest"] = digest(payload)
    return payload


def validate_group2_reconcile_preservation(
    record: Any, original_reference: Any
) -> dict[str, Any]:
    record = _closed_object(
        record,
        {"schema", "request", "approval", "failed_manifest", "prior_success",
         "first_startup", "pre_rollback", "stopped", "restore_startup", "restored", "receipt"},
        "group2_reconcile_chain_invalid",
    )
    if record["schema"] != "group2-reconcile-chain-v1":
        raise CarryForwardError("group2_reconcile_chain_invalid")
    original_reference = _closed_object(
        original_reference, {"snapshot", "snapshot_digest", "review_report_sha256"},
        "group2_preservation_reference_invalid",
    )
    original = _closed_object(
        original_reference["snapshot"], {"selection", "db", "files", "lineage"},
        "group2_preservation_reference_invalid",
    )
    _digest_text(
        original_reference["review_report_sha256"],
        "group2_preservation_reference_invalid",
    )
    if (
        original_reference["snapshot_digest"] != digest(original)
        or not isinstance(original["lineage"], list)
        or any(
            not isinstance(item, dict)
            or set(item) != {"name", "sha256"}
            or not isinstance(item["name"], str)
            or DIGEST.fullmatch(str(item["sha256"])) is None
            for item in original["lineage"]
        )
    ):
        raise CarryForwardError("group2_preservation_reference_invalid")
    request = record["request"]
    approval = record["approval"]
    prior_success = _closed_object(
        record["prior_success"],
        {"sha", "manifest_id", "manifest_digest", "receipt_sha256",
         "consumed_sha256", "images_digest", "schema"},
        "group2_reconcile_chain_invalid",
    )
    restored = _closed_object(
        record["restored"],
        {"postgres", "final", "account", "images", "storage", "token_health"},
        "group2_reconcile_chain_invalid",
    )
    first = _closed_object(
        record["first_startup"],
        {"source_artifacts", "request", "proof", "before_files", "after_files", "result"},
        "group2_reconcile_chain_invalid",
    )
    source_artifacts = _closed_object(
        first["source_artifacts"], GROUP2_RECONCILE_EVIDENCE_FILES,
        "group2_reconcile_chain_invalid",
    )
    raw_artifacts = {
        name: _validate_embedded_bytes(value, "group2_reconcile_chain_invalid")
        for name, value in source_artifacts.items()
    }
    validated = validate_group2_reconcile_request(request, approval, raw_artifacts)
    originals = _group2_reconcile_originals(validated)
    failed_manifest = originals["manifest"]
    if (
        record["failed_manifest"] != failed_manifest
        or failed_manifest.get("manifest_id") != request.get("failed", {}).get("manifest_id")
        or failed_manifest.get("manifest_digest") != request.get("failed", {}).get("manifest_digest")
        or failed_manifest.get("to_sha") != request.get("failed", {}).get("sha")
        or failed_manifest["review_scope"].get("preservation_reference") != original_reference
        or prior_success != request.get("prior")
    ):
        raise CarryForwardError("group2_reconcile_chain_invalid")
    if {key: value for key, value in first.items() if key != "source_artifacts"} != originals["first_startup"]:
        raise CarryForwardError("group2_reconcile_chain_changed")
    evidence = {
        "preflight": record["pre_rollback"],
        "stopped": record["stopped"],
        "restored_postgres": restored["postgres"],
        "restore_startup": record["restore_startup"],
        "restored": restored["final"],
        "account": restored["account"],
        "images": restored["images"],
        "storage": restored["storage"],
        "token_health": restored["token_health"],
    }
    receipt = validate_group2_reconcile_result(
        request,
        evidence,
        validated,
    )
    if (
        receipt != record["receipt"]
        or original["db"] != record["pre_rollback"]["db"]
        or original["db"] != receipt["restored"]["db"]
        or original["files"] != originals["files_before_startup"]
    ):
        raise CarryForwardError("group2_reconcile_chain_changed")
    chain_digest = digest(record)
    snapshot = {
        "selection": original["selection"],
        "db": original["db"],
        "files": receipt["restored"]["files"],
        "lineage": [
            *original["lineage"],
            {"name": f"incident/{request['incident_id']}/request", "sha256": request["request_digest"]},
            {"name": f"incident/{request['incident_id']}/receipt", "sha256": receipt["receipt_digest"]},
            {"name": f"incident/{request['incident_id']}/chain", "sha256": chain_digest},
        ],
    }
    return snapshot


def group2_runtime(request: Any) -> dict[str, Any]:
    if not isinstance(request, dict) or request.get("mode") != GROUP2_MODE:
        raise CarryForwardError("group2_runtime_request_invalid")
    operation = request.get("operation")
    if operation == "account-capture":
        return {"account_entry": capture_group2_account_entry(request)}
    if operation == "account-check":
        return {"account_check": check_group2_entry_boundaries(request)}
    if operation == "entry-probe":
        return {"entry_probe": _entry_probe(request)}
    if operation == "log-capture":
        value = _closed_request(request, operation, {"profile"})
        return {"log_evidence": capture_log_prefix(value["profile"])}
    if operation == "log-append":
        value = _closed_request(request, operation, {"baseline"})
        return {"log_evidence": read_log_append(value["baseline"])}
    if operation == "startup-proof":
        return {"startup_proof": _startup_proof(request)}
    if operation == "probe-proof":
        return {"probe_proof": derive_group2_probe_provenance(request)}
    if operation == "probe-cleanup":
        return {"cleanup": wait_group2_probe_cleanup(request)}
    raise CarryForwardError("group2_runtime_operation_invalid")


def _command_group2_runtime(args: argparse.Namespace) -> dict[str, Any]:
    request = read_private(args.request)
    result = group2_runtime(request)
    write_private(args.output, result)
    return {"code": "group2_runtime_ok", "operation": request["operation"]}


def _atomic_incident_json(path: Path, value: Any) -> None:
    write_private(path, value)
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _checked_output(arguments: list[str]) -> str:
    return subprocess.check_output(arguments, text=True, timeout=180).strip()


def _compose_arguments(root: Path, project: str, sha: str) -> list[str]:
    release = root / "releases" / sha
    return [
        "docker", "compose", "--project-name", project,
        "--env-file", str(root / "preview.env"),
        "-f", str(release / "docker-compose.yml"),
        "-f", str(release / "compose.preview.json"),
    ]


def _container_image(project: str, service: str) -> str:
    return _checked_output(
        ["docker", "inspect", f"{project}-{service}-1", "--format", "{{.Image}}"]
    )


def _live_storage_identity(project: str) -> list[dict[str, Any]]:
    actual = []
    for service in ("postgres", "rabbitmq", "control", "worker"):
        value = json.loads(_checked_output(["docker", "inspect", f"{project}-{service}-1"]))[0]
        for item in value.get("Mounts", []):
            if item.get("Type") not in {"volume", "bind", "tmpfs"}:
                continue
            actual.append(
                {
                    "service": service,
                    "type": item["Type"],
                    "source": item.get("Name") if item["Type"] == "volume" else item.get("Source", "") if item["Type"] == "bind" else "",
                    "destination": item.get("Destination"),
                    "read_only": not bool(item.get("RW")),
                }
            )
    actual.sort(key=lambda item: (item["service"], item["destination"]))
    return validate_storage_identity(actual)


def _read_token_health(root: Path, profile: dict[str, Any]) -> dict[str, Any]:
    env = _read_explicit_env(root / "preview.env")
    token_port = profile["ports"]["token"]
    if not isinstance(token_port, list) or len(token_port) != 1:
        raise CarryForwardError("group2_reconcile_health_failed")
    request = urllib_request.Request(
        f"http://127.0.0.1:{token_port[0]['published']}/api/health",
        headers={"Authorization": "Bearer " + env["DLR_ADMIN_TOKEN"]},
    )
    try:
        with urllib_request.build_opener(urllib_request.ProxyHandler({})).open(
            request, timeout=10
        ) as response:
            body = json.load(response)
            return {"status": response.status, "database": body.get("database")}
    except (OSError, ValueError, KeyError) as error:
        raise CarryForwardError("group2_reconcile_health_failed") from error


def _incident_phase(directory: Path, phase: str, request: dict[str, Any]) -> None:
    _atomic_incident_json(
        directory / "phase.json",
        {"schema": "group2-starting-reconcile-phase-v1", "incident_id": request["incident_id"], "phase": phase},
    )


def _capture_reconcile_state(
    root: Path,
    directory: Path,
    name: str,
    manifest: dict[str, Any],
    project: str,
    *,
    admission_output: Path | None = None,
    timeout: int = 300,
) -> tuple[dict[str, Any], dict[str, Any]]:
    output = directory / name
    output.mkdir(mode=0o700)
    write_private(output / "ids.json", manifest["selection"])
    storage = manifest["storage_identity"]
    def volume(service: str, destination: str) -> str:
        matches = [
            item["source"] for item in storage
            if item["service"] == service and item["destination"] == destination
            and item["type"] == "volume"
        ]
        if len(matches) != 1:
            raise CarryForwardError("group2_reconcile_storage_changed")
        return matches[0]
    runtime = volume("worker", "/var/lib/dlr/runtime")
    journal = volume("worker", "/var/lib/dlr/journal")
    builtin = volume("control", "/var/lib/dlr/builtin-packages")
    artifact_entries = [
        item for item in storage
        if item["service"] == "control" and item["type"] == "volume"
        and item["destination"] != "/var/lib/dlr/builtin-packages"
    ]
    if len(artifact_entries) != 1:
        raise CarryForwardError("group2_reconcile_storage_changed")
    artifact = artifact_entries[0]
    worker_user = _checked_output(
        ["docker", "inspect", f"{project}-worker-1", "--format", "{{.Config.User}}"]
    )
    worker_uid = (worker_user.split(":", 1)[0] or "0")
    if not worker_uid.isdigit():
        raise CarryForwardError("group2_reconcile_storage_changed")
    control = json.loads(_checked_output(["docker", "inspect", f"{project}-control-1"]))[0]
    allowed_env = []
    for item in control["Config"]["Env"]:
        key = item.partition("=")[0]
        if key in {"DATABASE_URL", "PGOPTIONS"}:
            allowed_env.append(item)
    if not any(item.startswith("DATABASE_URL=") for item in allowed_env):
        raise CarryForwardError("group2_reconcile_db_capture_invalid")
    networks = list(control["NetworkSettings"]["Networks"])
    if len(networks) != 1:
        raise CarryForwardError("group2_reconcile_db_capture_invalid")
    old_control = [
        image for tag, image in manifest["old_image_ids"].items()
        if tag.endswith(f"-control:{GROUP2_FROM_SHA}")
    ]
    if len(old_control) != 1:
        raise CarryForwardError("group2_reconcile_images_changed")
    arguments = [
        "docker", "run", "--rm", "--read-only", "--network", networks[0],
        "--cap-drop", "ALL", "--cap-add", "DAC_READ_SEARCH", "--user", "0:0",
        "--security-opt", "no-new-privileges=true", "--pids-limit", "64",
        "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=16m",
        "--env", "PYTHONDONTWRITEBYTECODE=1",
    ]
    for item in allowed_env:
        arguments.extend(("--env", item))
    arguments.extend(
        (
            "--mount", f"type=bind,source={directory / 'tool' / 'carry_forward.py'},target=/opt/dlr/carry_forward.py,readonly",
            "--mount", f"type=bind,source={output},target=/evidence",
            "--mount", f"type=volume,source={runtime},target=/var/lib/dlr/runtime,readonly,volume-nocopy",
            "--mount", f"type=volume,source={journal},target=/var/lib/dlr/journal,readonly,volume-nocopy",
            "--mount", f"type=volume,source={builtin},target=/var/lib/dlr/builtin-packages,readonly,volume-nocopy",
            "--mount", f"type=volume,source={artifact['source']},target={artifact['destination']},readonly,volume-nocopy",
            "--entrypoint", "python", old_control[0], "/opt/dlr/carry_forward.py", "capture-state",
            "--ids", "/evidence/ids.json", "--schema-phase", "before", "--mode", GROUP2_MODE,
            "--runtime-root", "/var/lib/dlr/runtime", "--journal-root", "/var/lib/dlr/journal",
            "--material-root", "builtin=/var/lib/dlr/builtin-packages",
            "--material-root", f"artifacts={artifact['destination']}", "--expected-uid", worker_uid,
            "--db-output", "/evidence/db.json", "--files-output", "/evidence/files.json",
        )
    )
    if admission_output is not None:
        arguments.extend(("--admission-output", f"/evidence/{admission_output.name}"))
    subprocess.run(arguments, check=True, timeout=timeout)
    return read_private(output / "db.json"), read_private(output / "files.json")


def _reboot_volume(
    storage: list[dict[str, Any]], service: str, destination: str
) -> str:
    matches = [
        item.get("source") for item in storage
        if isinstance(item, dict) and item.get("service") == service
        and item.get("destination") == destination and item.get("type") == "volume"
    ]
    if len(matches) != 1 or not isinstance(matches[0], str) or not matches[0]:
        raise CarryForwardError("group2_reboot_storage_changed")
    return matches[0]


def _group2_reboot_volume_backings(
    request: dict[str, Any]
) -> dict[str, Any]:
    mounts = _mountinfo(Path("/proc/1/mountinfo"))
    boot_id = _read_text(Path("/proc/sys/kernel/random/boot_id"))
    host_namespace = os.readlink("/proc/1/ns/mnt")
    if host_namespace != os.readlink("/proc/self/ns/mnt"):
        raise CarryForwardError("kernel_identity_unknown")
    result = {}
    for key, service, destination in (
        ("runtime", "worker", "/var/lib/dlr/runtime"),
        ("journal", "worker", "/var/lib/dlr/journal"),
    ):
        name = _reboot_volume(request["prior"]["storage"], service, destination)
        try:
            raw = json.loads(_checked_output(["docker", "volume", "inspect", name]))
            if not isinstance(raw, list) or len(raw) != 1:
                raise ValueError("volume inspect shape")
            inspect = raw[0]
            mountpoint = inspect["Mountpoint"]
            source_raw = Path(mountpoint)
            source = source_raw.resolve(strict=True)
            if (
                not source_raw.is_absolute() or source_raw.is_symlink()
                or source != source_raw or not source.is_dir()
                or inspect.get("Name") != name
            ):
                raise ValueError("volume mountpoint shape")
            info = source.lstat()
        except (KeyError, ValueError, OSError, json.JSONDecodeError,
                subprocess.SubprocessError) as error:
            raise CarryForwardError("kernel_identity_unknown") from error
        host_mount, selected, derived = _derive_group2_reboot_backing_mount(
            str(source), info.st_dev, mounts, "kernel_identity_unknown",
        )
        result[key] = {
            "name": name, "device": info.st_dev, "inode": info.st_ino,
            "mount": selected,
            "inspect": inspect,
            "mountpoint_stat": {
                "device": info.st_dev, "inode": info.st_ino, "type": "directory",
                "mode": stat.S_IMODE(info.st_mode), "uid": info.st_uid, "gid": info.st_gid,
            },
            "host_mount": host_mount,
            "derived_mount": derived,
        }
    return {
        "schema": "group2-post-finalize-reboot-volume-backings-v1",
        "boot_id": boot_id, "host_mount_namespace": host_namespace,
        "host_mountinfo": mounts, "volumes": result,
    }


def _group2_reboot_backing_authority(backings: dict[str, Any]) -> dict[str, Any]:
    return {
        "volumes": {
            name: {
                "name": item["name"], "device": item["device"],
                "inode": item["inode"], "mount": item["mount"],
            }
            for name, item in backings["volumes"].items()
        }
    }


def _group2_reboot_backings_stable(
    before: dict[str, Any], after: dict[str, Any]
) -> bool:
    keys = ("schema", "boot_id", "host_mount_namespace", "volumes")
    return all(before.get(key) == after.get(key) for key in keys)


def _group2_reboot_kernel_capture_stable(
    before: dict[str, Any], after: dict[str, Any]
) -> bool:
    if not isinstance(before, dict) or not isinstance(after, dict):
        return False
    ignored = {
        "namespace_evidence", "volume_backings", "unit_properties", "cgroup_limits",
    }
    return {
        key: value for key, value in before.items() if key not in ignored
    } == {
        key: value for key, value in after.items() if key not in ignored
    }


def _augment_group2_reboot_kernel(
    kernel: dict[str, Any], request: dict[str, Any]
) -> None:
    params = request["prior"]["parameters"]["keeper"]
    properties = (
        "ActiveState", "Delegate", "ControlGroup", "MainPID", "Description",
        "InvocationID",
    )
    try:
        values = {
            name: subprocess.run(
                ["systemctl", "show", params["unit"], f"--property={name}", "--value"],
                check=True, capture_output=True, text=True,
                timeout=_reboot_timeout(request, 10),
            ).stdout.strip()
            for name in properties
        }
        parent = Path("/sys/fs/cgroup") / kernel["control_group"].removeprefix("/")
        limits = {}
        for relative in [".", *sorted(kernel["children"])]:
            path = parent if relative == "." else parent / relative
            limits[relative] = {
                name: _read_text(path / name)
                for name in (
                    "cpu.max", "memory.max", "memory.swap.max", "pids.max",
                    "cgroup.subtree_control", "cgroup.procs",
                )
            }
            info = path.lstat()
            limits[relative].update(uid=info.st_uid, gid=info.st_gid)
    except (KeyError, OSError, subprocess.SubprocessError) as error:
        raise CarryForwardError("group2_reboot_kernel_changed") from error
    repeated = {
        name: subprocess.run(
            ["systemctl", "show", params["unit"], f"--property={name}", "--value"],
            check=True, capture_output=True, text=True,
            timeout=_reboot_timeout(request, 10),
        ).stdout.strip()
        for name in properties
    }
    if repeated != values:
        raise CarryForwardError("group2_reboot_kernel_changed")
    kernel["unit_properties"] = values
    kernel["cgroup_limits"] = limits


def _capture_group2_reboot_kernel(
    request: dict[str, Any], *, idle: bool,
    expected_description: str, worker_container: str | None = None,
) -> dict[str, Any]:
    params = request["prior"]["parameters"]["keeper"]
    if idle:
        kernel = capture_kernel(
            params["unit"], require_idle=False,
            expected_description=expected_description,
        )
        children = kernel.get("children")
        if not isinstance(children, dict) or set(children) != {"agent"}:
            raise CarryForwardError("kernel_not_idle")
        backings = _group2_reboot_volume_backings(request)
        targets = {
            (item["mount"]["filesystem"], item["mount"]["major_minor"],
             item["mount"]["root"])
            for item in backings["volumes"].values()
        }
        first = _scan_related_mount_namespaces(targets, set(), include_records=True)
        second = _scan_related_mount_namespaces(targets, set(), include_records=True)
        repeated_kernel = capture_kernel(
            params["unit"], require_idle=False,
            expected_description=expected_description,
        )
        repeated_backings = _group2_reboot_volume_backings(request)
        if (
            any(first.get(key) != second.get(key)
                for key in ("related_count", "pin_count", "target_digest"))
            or second["related_count"] != 0 or second["pin_count"] != 0
            or not _group2_reboot_kernel_capture_stable(kernel, repeated_kernel)
            or not _group2_reboot_backings_stable(backings, repeated_backings)
        ):
            raise CarryForwardError("kernel_namespace_active")
        kernel["old_worker_authority"] = None
        kernel["namespace_evidence"] = {
            "schema": "group2-post-finalize-reboot-namespace-evidence-v1",
            "mode": "keeper_idle", "targets": sorted(targets),
            "scans": [first, second],
        }
        kernel["retired_markers"] = []
        kernel["volume_backings"] = backings
        _augment_group2_reboot_kernel(kernel, request)
        return kernel
    if not isinstance(worker_container, str) or not worker_container:
        raise CarryForwardError("kernel_identity_unknown")
    kernel = capture_kernel(
        params["unit"], require_idle=False,
        expected_description=expected_description,
        worker_container=worker_container,
        runtime_volume=_reboot_volume(
            request["prior"]["storage"], "worker", "/var/lib/dlr/runtime"
        ),
        journal_volume=_reboot_volume(
            request["prior"]["storage"], "worker", "/var/lib/dlr/journal"
        ),
    )
    authority = kernel["old_worker_authority"]
    kernel["volume_backings"] = _group2_reboot_volume_backings(request)
    first = _scan_related_mount_namespaces(
        {
            (item["mount"]["filesystem"], item["mount"]["major_minor"],
             item["mount"]["root"])
            for item in authority["volumes"].values()
        },
        {authority["mount_namespace"], authority["cgroup_namespace"]},
        include_records=True,
    )
    second = _scan_related_mount_namespaces(
        {
            (item["mount"]["filesystem"], item["mount"]["major_minor"],
             item["mount"]["root"])
            for item in authority["volumes"].values()
        },
        {authority["mount_namespace"], authority["cgroup_namespace"]},
        include_records=True,
    )
    repeated_kernel = capture_kernel(
        params["unit"], require_idle=False,
        expected_description=expected_description,
        worker_container=worker_container,
        runtime_volume=_reboot_volume(
            request["prior"]["storage"], "worker", "/var/lib/dlr/runtime"
        ),
        journal_volume=_reboot_volume(
            request["prior"]["storage"], "worker", "/var/lib/dlr/journal"
        ),
    )
    repeated_backings = _group2_reboot_volume_backings(request)
    stable = ("related_count", "pin_count", "target_digest", "related", "pins")
    if (
        any(first.get(key) != second.get(key) for key in stable)
        or not _group2_reboot_kernel_capture_stable(kernel, repeated_kernel)
        or not _group2_reboot_backings_stable(kernel["volume_backings"], repeated_backings)
    ):
        raise CarryForwardError("kernel_identity_unknown")
    targets = {
        (item["mount"]["filesystem"], item["mount"]["major_minor"],
         item["mount"]["root"])
        for item in authority["volumes"].values()
    }
    kernel["namespace_evidence"] = {
        "schema": "group2-post-finalize-reboot-namespace-evidence-v1",
        "mode": "worker_running", "targets": sorted(targets),
        "scans": [first, second],
    }
    _augment_group2_reboot_kernel(kernel, request)
    return kernel


def _capture_group2_reboot_stopped_platform(
    request: dict[str, Any], unit: str
) -> dict[str, Any]:
    try:
        boot_id = _read_text(Path("/proc/sys/kernel/random/boot_id"))
        values = [
            subprocess.run(
                ["systemctl", "show", unit, f"--property={name}", "--value"],
                check=True, capture_output=True, text=True,
                timeout=_reboot_timeout(request, 10),
            ).stdout.strip()
            for name in ("LoadState", "ActiveState", "SubState")
        ]
    except (OSError, subprocess.SubprocessError) as error:
        raise CarryForwardError("group2_reboot_stopped_invalid") from error
    return {
        "boot_id": boot_id,
        "unit": dict(zip(("load_state", "active_state", "sub_state"), values)),
    }


def _capture_group2_reboot_postgres(
    request: dict[str, Any], raw: dict[str, Any], *, running: bool,
) -> dict[str, Any]:
    expected = request["prior"]["containers"]["postgres"]
    image_id = expected["image_id"]
    timeout = _reboot_timeout(request, 30)
    try:
        image_values = json.loads(subprocess.check_output(
            ["docker", "image", "inspect", image_id], text=True, timeout=timeout,
        ))
        if not isinstance(image_values, list) or len(image_values) != 1:
            raise ValueError("image inspect shape")
        layers = image_values[0]["RootFS"]["Layers"]
        if running:
            container_id = expected["container_id"]
            binary = subprocess.check_output(
                ["docker", "exec", container_id, "postgres", "--version"],
                text=True, timeout=timeout,
            ).strip()
            data_version = subprocess.check_output(
                ["docker", "exec", container_id, "cat",
                 "/var/lib/postgresql/data/PG_VERSION"],
                text=True, timeout=timeout,
            ).strip()
            env = (raw.get("Config") or {}).get("Env")
            if not isinstance(env, list):
                raise ValueError("postgres env shape")
            variables = {}
            for item in env:
                if not isinstance(item, str) or "=" not in item:
                    raise ValueError("postgres env shape")
                name, value = item.split("=", 1)
                if name in {"POSTGRES_USER", "POSTGRES_DB"}:
                    variables[name] = value
            user = variables.get("POSTGRES_USER", "postgres")
            database = variables.get("POSTGRES_DB", user)
            server = subprocess.check_output(
                ["docker", "exec", container_id, "psql", "-U", user, "-d", database,
                 "-Atqc", "SHOW server_version"],
                text=True, timeout=timeout,
            ).strip()
            schema = subprocess.check_output(
                ["docker", "exec", container_id, "psql", "-U", user, "-d", database,
                 "-Atqc", "SELECT version_num FROM alembic_version"],
                text=True, timeout=timeout,
            ).strip()
        else:
            binary = subprocess.check_output(
                ["docker", "run", "--rm", "--network", "none", "--read-only",
                 "--entrypoint", "postgres", image_id, "--version"],
                text=True, timeout=timeout,
            ).strip()
            volume = _reboot_volume(
                request["prior"]["storage"], "postgres", "/var/lib/postgresql/data"
            )
            data_version = subprocess.check_output(
                ["docker", "run", "--rm", "--network", "none", "--read-only",
                 "--mount", f"type=volume,source={volume},target=/data,readonly,volume-nocopy",
                 "--entrypoint", "cat", image_id, "/data/PG_VERSION"],
                text=True, timeout=timeout,
            ).strip()
            server = None
            schema = None
    except (KeyError, ValueError, OSError, subprocess.SubprocessError) as error:
        raise CarryForwardError("group2_reboot_postgres_invalid") from error
    value = {
        "database_read": running,
        "binary_version": binary,
        "data_pg_version": data_version,
        "image_id": image_id,
        "rootfs_layers": layers,
    }
    if running:
        value["server_version"] = server
        value["schema"] = schema
    return value


def _capture_group2_reboot_files(
    directory: Path, request: dict[str, Any], project: str, name: str
) -> dict[str, Any]:
    """Read stopped volumes with the frozen old Control image and no network."""
    output = directory / name
    output.mkdir(mode=0o700)
    storage = request["prior"]["storage"]
    runtime = _reboot_volume(storage, "worker", "/var/lib/dlr/runtime")
    journal = _reboot_volume(storage, "worker", "/var/lib/dlr/journal")
    builtin = _reboot_volume(storage, "control", "/var/lib/dlr/builtin-packages")
    artifacts = [
        item for item in storage if isinstance(item, dict)
        and item.get("service") == "control" and item.get("type") == "volume"
        and item.get("destination") != "/var/lib/dlr/builtin-packages"
    ]
    if len(artifacts) != 1:
        raise CarryForwardError("group2_reboot_storage_changed")
    worker_user = _checked_output([
        "docker", "inspect", request["prior"]["containers"]["worker"]["container_id"],
        "--format", "{{.Config.User}}",
    ]).split(":", 1)[0] or "0"
    if not worker_user.isdigit():
        raise CarryForwardError("group2_reboot_storage_changed")
    arguments = [
        "docker", "run", "--rm", "--read-only", "--network", "none",
        "--cap-drop", "ALL", "--cap-add", "DAC_READ_SEARCH", "--user", "0:0",
        "--security-opt", "no-new-privileges=true", "--pids-limit", "64",
        "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=16m",
        "--env", "PYTHONDONTWRITEBYTECODE=1",
        "--mount", f"type=bind,source={directory / 'tool' / 'carry_forward.py'},target=/opt/dlr/carry_forward.py,readonly",
        "--mount", f"type=bind,source={output},target=/evidence",
        "--mount", f"type=volume,source={runtime},target=/var/lib/dlr/runtime,readonly,volume-nocopy",
        "--mount", f"type=volume,source={journal},target=/var/lib/dlr/journal,readonly,volume-nocopy",
        "--mount", f"type=volume,source={builtin},target=/var/lib/dlr/builtin-packages,readonly,volume-nocopy",
        "--mount", f"type=volume,source={artifacts[0]['source']},target={artifacts[0]['destination']},readonly,volume-nocopy",
        "--entrypoint", "python", request["prior"]["containers"]["control"]["image_id"],
        "/opt/dlr/carry_forward.py", "capture",
        "--runtime-root", "/var/lib/dlr/runtime", "--journal-root", "/var/lib/dlr/journal",
        "--material-root", "builtin=/var/lib/dlr/builtin-packages",
        "--material-root", f"artifacts={artifacts[0]['destination']}",
        "--expected-uid", worker_user, "--output", "/evidence/files.json",
    ]
    subprocess.run(arguments, check=True, timeout=_reboot_timeout(request, 300))
    return read_private(output / "files.json")


def _reboot_timeout(request: dict[str, Any], maximum: int) -> int:
    remaining = (request["window"]["deadline_ns"] - time.time_ns()) // 1_000_000_000
    if remaining < 5:
        raise CarryForwardError("group2_reboot_window_expired")
    return max(1, min(maximum, int(remaining)))


def _wait_reboot_container(
    request: dict[str, Any], project: str, service: str
) -> dict[str, Any]:
    deadline = time.monotonic() + _reboot_timeout(request, 180)
    while True:  # tick first; sleeping never delays the first observation
        current = _inspect_container(project, service)
        if current.get("status") == "running" and current.get("health") == "healthy":
            return current
        if current.get("status") in {"dead", "removing"} or time.monotonic() >= deadline:
            raise CarryForwardError(f"group2_reboot_{service}_start_failed")
        time.sleep(0.25)


def _start_reboot_container(
    directory: Path, request: dict[str, Any], project: str, service: str
) -> dict[str, Any]:
    expected = request["prior"]["containers"][service]
    before = _inspect_container(project, service)
    if before != _reboot_expected_container(expected) or before.get("status") == "running":
        raise CarryForwardError("group2_reboot_container_changed")
    _run_group2_reboot_action(
        directory, f"start-{service}", ["docker", "start", expected["container_id"]],
        _reboot_timeout(request, 180),
    )
    after = _wait_reboot_container(request, project, service)
    if (
        after.get("container_id") != expected.get("container_id")
        or after.get("image_id") != expected.get("image_id")
    ):
        raise CarryForwardError("group2_reboot_container_changed")
    return after


def _run_group2_reboot_action(
    directory: Path, name: str, arguments: list[str], timeout: int
) -> dict[str, Any]:
    if not re.fullmatch(r"(?:prepare-keeper|start-(?:postgres|rabbitmq|control|worker|web))", name):
        raise CarryForwardError("group2_reboot_action_invalid")
    intent = directory / f"command-{name}-intent.json"
    result_path = directory / f"command-{name}-result.json"
    if intent.exists() or result_path.exists():
        raise CarryForwardError("group2_reboot_replay_rejected")
    started = time.time_ns()
    _atomic_incident_json(intent, {
        "schema": "group2-post-finalize-reboot-command-intent-v1",
        "action": name, "argv": arguments, "started_at_ns": started,
    })
    try:
        completed = subprocess.run(
            arguments, check=False, capture_output=True, text=True, timeout=timeout,
        )
        record = {
            "schema": "group2-post-finalize-reboot-command-result-v1",
            "action": name, "argv": arguments, "started_at_ns": started,
            "ended_at_ns": time.time_ns(), "returncode": completed.returncode,
            "stdout": completed.stdout, "stderr": completed.stderr,
        }
    except subprocess.TimeoutExpired as error:
        record = {
            "schema": "group2-post-finalize-reboot-command-result-v1",
            "action": name, "argv": arguments, "started_at_ns": started,
            "ended_at_ns": time.time_ns(), "returncode": None,
            "stdout": error.stdout or "", "stderr": error.stderr or "",
            "error": "timeout",
        }
    _atomic_incident_json(result_path, record)
    if record.get("returncode") != 0:
        raise CarryForwardError(f"group2_reboot_{name.replace('-', '_')}_failed")
    return record


def _inspect_reboot_container_raw(container_id: str) -> dict[str, Any]:
    try:
        values = json.loads(_checked_output(["docker", "inspect", container_id]))
    except (json.JSONDecodeError, OSError, subprocess.SubprocessError) as error:
        raise CarryForwardError("group2_reboot_container_changed") from error
    if not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], dict):
        raise CarryForwardError("group2_reboot_container_changed")
    return values[0]


def _reboot_container_static(value: dict[str, Any]) -> dict[str, Any]:
    result = {
        key: value.get(key)
        for key in ("Id", "Image", "Name", "Config", "HostConfig", "Mounts")
    }
    if isinstance(result["Mounts"], list):
        result["Mounts"] = sorted(
            result["Mounts"], key=lambda item: (
                item.get("Destination", "") if isinstance(item, dict) else ""
            )
        )
    return result


def _reboot_expected_container(value: dict[str, Any]) -> dict[str, Any]:
    return {
        key: item for key, item in value.items()
        if key not in {"inspect_static_digest", "stopped_state_digest"}
    }


def _validate_reboot_container_raw(
    raw: Any, expected: dict[str, Any], current: dict[str, Any], *, stopped: bool
) -> None:
    if (
        not isinstance(raw, dict)
        or digest(_reboot_container_static(raw)) != expected.get("inspect_static_digest")
        or raw.get("Id") != expected.get("container_id")
        or raw.get("Image") != expected.get("image_id")
        or not isinstance(raw.get("State"), dict)
        or raw["State"].get("Status") != current.get("status")
        or raw.get("RestartCount", 0) != current.get("restart_count")
        or (stopped and digest(raw["State"]) != expected.get("stopped_state_digest"))
    ):
        raise CarryForwardError("group2_reboot_container_changed")


def _capture_reconcile_stage(
    root: Path,
    directory: Path,
    name: str,
    manifest: dict[str, Any],
    project: str,
    previous_logs: dict[str, Any],
    deployment: dict[str, Any],
    *,
    require_idle: bool,
    baseline_authority: dict[str, Any],
    runtime_volume: str,
    journal_volume: str,
    admission_output: Path | None = None,
    timeout: int = 300,
    reboot_request: dict[str, Any] | None = None,
) -> dict[str, Any]:
    window_start_ns = time.time_ns()
    logs = read_log_append(previous_logs)
    db, files = _capture_reconcile_state(
        root, directory, name, manifest, project,
        admission_output=admission_output, timeout=timeout
    )
    kernel_arguments: dict[str, Any] = {
        "require_idle": require_idle,
        "expected_description": (
            f"DataLinkRuntime Sandbox {deployment['sandbox_unit']} "
            f"CPU={deployment['sandbox_cpu_quota']} "
            f"Memory={deployment['sandbox_memory_max']}"
        ),
        "baseline_authority": baseline_authority,
        "recovery_markers": files
        .get("journal_facts", {})
        .get("sandbox_recovery", []),
    }
    if not require_idle:
        kernel_arguments.update(
            worker_container=f"{project}-worker-1",
            runtime_volume=runtime_volume,
            journal_volume=journal_volume,
        )
    if reboot_request is None:
        kernel = capture_kernel(deployment["sandbox_unit"], **kernel_arguments)
    else:
        if require_idle:
            raise CarryForwardError("group2_reboot_kernel_changed")
        kernel = _capture_group2_reboot_kernel(
            reboot_request, idle=False,
            expected_description=kernel_arguments["expected_description"],
            worker_container=reboot_request["prior"]["containers"]["worker"]["container_id"],
        )
    containers = {
        service: _inspect_container(project, service)
        for service in (
            "postgres", "rabbitmq", "control", "worker", "web", "account-web"
        )
    }
    storage = _live_storage_identity(project)
    result = {
        "db": db,
        "files": files,
        "logs": logs,
        "kernel": kernel,
        "containers": containers,
        "storage": storage,
        "window_start_ns": window_start_ns,
        "window_end_ns": time.time_ns(),
    }
    if admission_output is not None:
        seed = read_private(admission_output)
        result["mutation_guard"] = _validate_group2_reboot_mutation_guard(
            seed.get("mutation_guard_rows") if isinstance(seed, dict) else None
        )
    return result


def reconcile_group2_vm(root: Path, incident_id: str) -> dict[str, Any]:
    """VM-only one-shot orchestration.  It waits for the host receipt check before tx commit."""
    root = root.resolve(strict=True)
    directory = (root / "incidents" / incident_id).resolve(strict=True)
    if directory.parent != root / "incidents":
        raise CarryForwardError("group2_reconcile_replay_rejected")
    request = read_private(directory / "request.json")
    if read_private(directory / "phase.json") != {
        "schema": "group2-starting-reconcile-phase-v1",
        "incident_id": incident_id,
        "phase": "prepared",
    }:
        raise CarryForwardError("group2_reconcile_replay_rejected")
    approval = read_private(directory / "approval.json")
    user_record = (directory / "USER-APPROVAL.txt").read_bytes()
    artifacts = {
        name: (directory / "evidence" / name).read_bytes()
        for name in GROUP2_RECONCILE_EVIDENCE_FILES
    }
    validated = validate_group2_reconcile_request(request, approval, artifacts)
    validate_group2_reconcile_user_record(request, approval, user_record)
    tool_directory = directory / "tool"
    if (
        not tool_directory.is_dir()
        or tool_directory.is_symlink()
        or {item.name for item in tool_directory.iterdir()}
        != {"deploy.sh", "carry_forward.py"}
    ):
        raise CarryForwardError("group2_reconcile_tool_changed")
    for name in ("deploy.sh", "carry_forward.py"):
        path = tool_directory / name
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or hashlib.sha256(path.read_bytes()).hexdigest()
            != request["tool"]["controller_files"][name]
        ):
            raise CarryForwardError("group2_reconcile_tool_changed")
    manifest = validated["artifacts"]["failed-manifest.json"]
    first = validated["artifacts"]["first-startup.json"]
    authority = read_private(directory / "preflight-authority.json")
    if (root / "current-sha").read_text().strip() != request["prior"]["sha"]:
        raise CarryForwardError("group2_reconcile_authority_changed")
    tx = read_private(root / "transaction.json")
    if tx.get("phase") != "starting" or tx.get("sha") != request["failed"]["sha"]:
        raise CarryForwardError("group2_reconcile_authority_changed")
    expected_vm_files = _reconcile_vm_installed_files(
        validated["artifacts"]["authority.json"]
    )
    installed = {
        name: hashlib.sha256((root / name).read_bytes()).hexdigest()
        for name in expected_vm_files
    }
    if installed != expected_vm_files:
        raise CarryForwardError("group2_reconcile_authority_changed")
    project = manifest["account_entry"]["project"]
    live_storage = _live_storage_identity(project)
    if live_storage != manifest["storage_identity"] or digest(live_storage) != request["restore"]["storage_identity_digest"]:
        raise CarryForwardError("group2_reconcile_storage_changed")
    volumes = [item for item in manifest["storage_identity"] if item["type"] == "volume"]
    runtime_volume = next(item["source"] for item in volumes if item["service"] == "worker" and item["destination"] == "/var/lib/dlr/runtime")
    journal_volume = next(item["source"] for item in volumes if item["service"] == "worker" and item["destination"] == "/var/lib/dlr/journal")
    deployment = read_private(root / "deployment.json")
    original_kernel = validated["artifacts"]["authority.json"]["vm_inventory"].get(
        "current_kernel"
    )
    preflight_stage = _capture_reconcile_stage(
        root, directory, "preflight", manifest, project,
        first["log_read_diagnostic"]["log_append"], deployment,
        require_idle=False,
        baseline_authority=manifest["kernel_evidence"]["old_worker_authority"],
        runtime_volume=runtime_volume,
        journal_volume=journal_volume,
    )
    current_images = {
        service: _container_image(project, service)
        for service in ("postgres", "control", "worker", "web")
    }
    platform = validated["artifacts"]["platform.json"]
    postgres_format = platform["postgres_format"]
    expected_current_images = {
        service: platform["images"]["candidate"][service]["Id"]
        for service in ("postgres", "control", "worker", "web")
    }
    if current_images != expected_current_images:
        raise CarryForwardError("group2_reconcile_images_changed")
    prior_images = {}
    for service in ("postgres", "control", "worker", "web"):
        matches = [
            image for tag, image in manifest["old_image_ids"].items()
            if tag.endswith(f"-{service}:{request['prior']['sha']}")
        ]
        if len(matches) != 1:
            raise CarryForwardError("group2_reconcile_images_changed")
        prior_images[service] = matches[0]
    preflight_postgres = {
            "schema": _checked_output(["docker", "exec", f"{project}-postgres-1", "psql", "-U", "dlr", "-d", "dlr", "-Atc", "SELECT version_num FROM alembic_version"]),
            "binary_version": _checked_output(["docker", "exec", f"{project}-postgres-1", "postgres", "--version"]),
            "server_version": _checked_output(["docker", "exec", f"{project}-postgres-1", "psql", "-U", "dlr", "-d", "dlr", "-Atc", "SHOW server_version"]),
            "data_pg_version": _checked_output(["docker", "exec", f"{project}-postgres-1", "cat", "/var/lib/postgresql/data/PG_VERSION"]),
    }
    preflight = {
        "stage": preflight_stage,
        "authority": authority,
        "postgres": preflight_postgres,
        "images": current_images,
    }
    originals = {
        "validated": validated,
        "authority": authority,
        "postgres": {
            "schema": request["prior"]["schema"],
            "binary_version": postgres_format["candidate_binary_version"],
            "server_version": postgres_format["current_server_version"],
            "data_pg_version": postgres_format["data_pg_version"],
        },
        "images": current_images,
        "kernel": original_kernel,
    }
    preflight_stage = validate_group2_reconcile_preflight(request, preflight, originals)
    _incident_phase(directory, "stopping-control", request)
    compose = _compose_arguments(root, project, request["prior"]["sha"])
    subprocess.run([*compose, "stop", "control"], check=True, timeout=180)
    control_stage = _capture_reconcile_stage(
        root, directory, "control-stopped", manifest, project,
        preflight_stage["logs"], deployment,
        require_idle=False,
        baseline_authority=preflight_stage["kernel"]["old_worker_authority"],
        runtime_volume=runtime_volume,
        journal_volume=journal_volume,
    )
    validate_group2_reconcile_transition(
        preflight_stage, control_stage,
        kernel_baseline=preflight_stage["kernel"], require_idle=False,
    )
    _validate_reconcile_container_transition(
        preflight_stage, control_stage, "control-stopped"
    )
    _incident_phase(directory, "stopping-apps", request)
    subprocess.run([*compose, "stop", "worker", "web", "account-web"], check=True, timeout=180)
    apps_stage = _capture_reconcile_stage(
        root, directory, "apps-stopped", manifest, project,
        control_stage["logs"], deployment,
        require_idle=True,
        baseline_authority=preflight_stage["kernel"]["old_worker_authority"],
        runtime_volume=runtime_volume,
        journal_volume=journal_volume,
    )
    validate_group2_reconcile_transition(
        control_stage, apps_stage,
        kernel_baseline=preflight_stage["kernel"], require_idle=True,
    )
    _validate_reconcile_container_transition(control_stage, apps_stage, "apps-stopped")
    _incident_phase(directory, "restoring-postgres", request)
    subprocess.run([*compose, "up", "-d", "--no-build", "--no-deps", "--force-recreate", "--wait", "--wait-timeout", "180", "postgres"], check=True, timeout=240)
    if _live_storage_identity(project) != live_storage:
        raise CarryForwardError("group2_reconcile_storage_changed")
    restored_postgres_info = {
        "schema": _checked_output(["docker", "exec", f"{project}-postgres-1", "psql", "-U", "dlr", "-d", "dlr", "-Atc", "SELECT version_num FROM alembic_version"]),
        "binary_version": _checked_output(["docker", "exec", f"{project}-postgres-1", "postgres", "--version"]),
        "server_version": _checked_output(["docker", "exec", f"{project}-postgres-1", "psql", "-U", "dlr", "-d", "dlr", "-Atc", "SHOW server_version"]),
        "data_pg_version": _checked_output(["docker", "exec", f"{project}-postgres-1", "cat", "/var/lib/postgresql/data/PG_VERSION"]),
    }
    if (
        _container_image(project, "postgres") != prior_images["postgres"]
        or restored_postgres_info
        != {
            "schema": request["prior"]["schema"],
            "binary_version": postgres_format["prior_binary_version"],
            "server_version": postgres_format["current_server_version"],
            "data_pg_version": postgres_format["data_pg_version"],
        }
    ):
        raise CarryForwardError("group2_reconcile_postgres_changed")
    restored_postgres_stage = _capture_reconcile_stage(
        root, directory, "restored-postgres", manifest, project,
        apps_stage["logs"], deployment,
        require_idle=True,
        baseline_authority=preflight_stage["kernel"]["old_worker_authority"],
        runtime_volume=runtime_volume,
        journal_volume=journal_volume,
    )
    validate_group2_reconcile_transition(
        apps_stage, restored_postgres_stage,
        kernel_baseline=preflight_stage["kernel"], require_idle=True,
    )
    _validate_reconcile_container_transition(
        apps_stage, restored_postgres_stage, "postgres-restored"
    )
    _incident_phase(directory, "restoring-apps", request)
    before_worker = restored_postgres_stage["containers"]["worker"]
    window_start = time.time_ns()
    subprocess.run(
        [*compose, "up", "-d", "--no-build", "--no-deps", "--force-recreate",
         "--wait", "--wait-timeout", "180", "control", "worker", "web"],
        check=True,
        timeout=240,
    )
    window_end = time.time_ns()
    restored_stage = _capture_reconcile_stage(
        root, directory, "restored", manifest, project,
        restored_postgres_stage["logs"], deployment,
        require_idle=False,
        baseline_authority=preflight_stage["kernel"]["old_worker_authority"],
        runtime_volume=runtime_volume,
        journal_volume=journal_volume,
    )
    after_worker = restored_stage["containers"]["worker"]
    startup_request = {
        "mode": GROUP2_MODE, "operation": "startup-proof", "profile": manifest["account_entry"],
        "logs_before": restored_postgres_stage["logs"], "logs_after": restored_stage["logs"],
        "container_before": before_worker, "container_after": after_worker,
        "window_start_ns": window_start, "window_end_ns": window_end,
    }
    proof = _incident_startup_proof(startup_request)
    restored_images = {service: _container_image(project, service) for service in ("postgres", "control", "worker", "web")}
    rabbit = _container_image(project, "rabbitmq")
    token_health = _read_token_health(root, manifest["account_entry"])
    evidence = {
        "preflight": preflight_stage,
        "stopped": {"control": control_stage, "apps": apps_stage},
        "restored_postgres": restored_postgres_stage,
        "restore_startup": {"request": startup_request, "proof": proof},
        "restored": restored_stage,
        "account": {"before": preflight_stage["containers"]["account-web"], "after": restored_stage["containers"]["account-web"]},
        "images": {"prior": manifest["old_image_ids"], "restored": restored_images, "rabbitmq_before": rabbit, "rabbitmq_after": _container_image(project, "rabbitmq"), "postgres": {"preflight": preflight_postgres, "restored": restored_postgres_info}},
        "storage": request["restore"]["storage_identity_digest"], "token_health": token_health,
    }
    receipt = validate_group2_reconcile_result(request, evidence, validated)
    result = {"evidence": evidence, "receipt": receipt}
    _atomic_incident_json(directory / "result.json", result)
    _atomic_incident_json(directory / "receipt.json", receipt)
    deadline = time.monotonic() + 300
    while not (directory / "host-validated.json").exists():
        if time.monotonic() >= deadline:
            raise CarryForwardError("group2_reconcile_host_validation_timeout")
        time.sleep(0.25)
    acknowledgement = read_private(directory / "host-validated.json")
    if acknowledgement != {"incident_id": incident_id, "receipt_digest": receipt["receipt_digest"]}:
        raise CarryForwardError("group2_reconcile_host_validation_invalid")
    prior_state = validated["artifacts"]["authority.json"]["snapshot"]["state"]
    transaction = {
        "phase": "ready", "sha": request["prior"]["sha"],
        "operation": "incident_software_restore",
        "reconciled_by": {"incident_id": incident_id, "receipt_digest": receipt["receipt_digest"]},
        "backup": prior_state["backup"],
        "carry_forward": prior_state["carry_forward"],
        "at": time.time(),
    }
    _atomic_incident_json(root / "transaction.json", transaction)
    return {"code": "group2_reconcile_vm_ok", "receipt_digest": receipt["receipt_digest"]}


def _embedded_file_value(path: Path) -> dict[str, str]:
    raw = path.read_bytes()
    return {
        "sha256": hashlib.sha256(raw).hexdigest(),
        "content_b64": base64.b64encode(raw).decode("ascii"),
    }


def _validate_group2_partial_finalize_directory(
    root: Path, incident_id: str, finalize_id: str
) -> Path:
    code = "group2_partial_finalize_replay_rejected"
    finalize_root = root / "incidents" / incident_id / "finalize"
    directory = finalize_root / finalize_id
    try:
        root_info = finalize_root.lstat()
        directory_info = directory.lstat()
        children = {item.name for item in finalize_root.iterdir()}
        canonical_root = finalize_root.resolve(strict=True)
        canonical_directory = directory.resolve(strict=True)
    except OSError as error:
        raise CarryForwardError(code) from error
    if (
        incident_id != GROUP2_PARTIAL_INCIDENT_ID
        or MANIFEST_ID.fullmatch(finalize_id) is None
        or not stat.S_ISDIR(root_info.st_mode)
        or stat.S_ISLNK(root_info.st_mode)
        or not stat.S_ISDIR(directory_info.st_mode)
        or stat.S_ISLNK(directory_info.st_mode)
        or canonical_root != finalize_root
        or canonical_directory != directory
        or children != {finalize_id}
    ):
        raise CarryForwardError(code)
    return directory


def _validate_group2_post_finalize_reboot_directory(
    root: Path, incident_id: str, finalize_id: str, boot_id: str
) -> Path:
    """Resolve the sole prepared reboot attempt without following a symlink."""
    code = "group2_reboot_replay_rejected"
    reboot_root = root / "incidents" / incident_id / "finalize" / finalize_id / "reboot"
    directory = reboot_root / boot_id
    try:
        root_info = reboot_root.lstat()
        directory_info = directory.lstat()
        canonical_root = reboot_root.resolve(strict=True)
        canonical_directory = directory.resolve(strict=True)
        children = {item.name for item in reboot_root.iterdir()}
    except OSError as error:
        raise CarryForwardError(code) from error
    if (
        incident_id != GROUP2_PARTIAL_INCIDENT_ID
        or finalize_id != GROUP2_POST_FINALIZE_REBOOT_PARENT_FINALIZE_ID
        or BOOT_ID.fullmatch(boot_id) is None
        or not stat.S_ISDIR(root_info.st_mode)
        or stat.S_ISLNK(root_info.st_mode)
        or not stat.S_ISDIR(directory_info.st_mode)
        or stat.S_ISLNK(directory_info.st_mode)
        or canonical_root != reboot_root
        or canonical_directory != directory
        or children != {boot_id}
    ):
        raise CarryForwardError(code)
    return directory


def finalize_group2_partial_vm(
    root: Path, incident_id: str, finalize_id: str
) -> dict[str, Any]:
    root = root.resolve(strict=True)
    directory = _validate_group2_partial_finalize_directory(
        root, incident_id, finalize_id
    )
    request = read_private(directory / "request.json")
    approval = read_private(directory / "approval.json")
    user_record = (directory / "USER-APPROVAL.txt").read_bytes()
    artifacts = {
        name: (directory / "evidence" / name).read_bytes()
        for name in GROUP2_PARTIAL_FINALIZE_FILES
    }
    validated = validate_group2_partial_finalize_request(
        request, approval, user_record, artifacts
    )
    if (
        incident_id != GROUP2_PARTIAL_INCIDENT_ID
        or finalize_id != request["finalize_id"]
        or read_private(directory / "phase.json")
        != {
            "schema": "group2-partial-finalize-phase-v1",
            "incident_id": incident_id,
            "finalize_id": finalize_id,
            "phase": "prepared",
        }
    ):
        raise CarryForwardError("group2_partial_finalize_replay_rejected")
    _atomic_incident_json(
        directory / "phase.json",
        {
            "schema": "group2-partial-finalize-phase-v1",
            "incident_id": incident_id,
            "finalize_id": finalize_id,
            "phase": "collecting",
        },
    )
    tool_directory = directory / "tool"
    if {item.name for item in tool_directory.iterdir()} != {
        "deploy.sh", "carry_forward.py"
    }:
        raise CarryForwardError("group2_partial_finalize_tool_changed")
    for name in ("deploy.sh", "carry_forward.py"):
        info = (tool_directory / name).lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or hashlib.sha256((tool_directory / name).read_bytes()).hexdigest()
            != request["tool"]["controller_files"][name]
        ):
            raise CarryForwardError("group2_partial_finalize_tool_changed")
    manifest = validated["context"]["originals"]["manifest"]
    diagnostic = validated["context"]["diagnostic"]["stage"]
    project = manifest["account_entry"]["project"]
    volumes = [
        item for item in manifest["storage_identity"] if item["type"] == "volume"
    ]
    runtime_volume = next(
        item["source"] for item in volumes
        if item["service"] == "worker"
        and item["destination"] == "/var/lib/dlr/runtime"
    )
    journal_volume = next(
        item["source"] for item in volumes
        if item["service"] == "worker"
        and item["destination"] == "/var/lib/dlr/journal"
    )
    current = _capture_reconcile_stage(
        root, directory, "current-capture", manifest, project,
        diagnostic["logs"], read_private(root / "deployment.json"),
        require_idle=False,
        baseline_authority=validated["context"]["originals"]["manifest"]
        ["kernel_evidence"]["old_worker_authority"],
        runtime_volume=runtime_volume, journal_volume=journal_volume,
    )
    _atomic_incident_json(directory / "current.json", current)
    postgres_format = validated["context"]["validated_original"]["artifacts"][
        "platform.json"
    ]["postgres_format"]
    postgres = {
        "schema": _checked_output([
            "docker", "exec", f"{project}-postgres-1", "psql", "-U", "dlr",
            "-d", "dlr", "-Atc", "SELECT version_num FROM alembic_version",
        ]),
        "binary_version": _checked_output([
            "docker", "exec", f"{project}-postgres-1", "postgres", "--version",
        ]),
        "server_version": _checked_output([
            "docker", "exec", f"{project}-postgres-1", "psql", "-U", "dlr",
            "-d", "dlr", "-Atc", "SHOW server_version",
        ]),
        "data_pg_version": _checked_output([
            "docker", "exec", f"{project}-postgres-1", "cat",
            "/var/lib/postgresql/data/PG_VERSION",
        ]),
    }
    if postgres != {
        "schema": "0040_issue152_dispositions",
        "binary_version": postgres_format["prior_binary_version"],
        "server_version": postgres_format["current_server_version"],
        "data_pg_version": postgres_format["data_pg_version"],
    }:
        raise CarryForwardError("group2_partial_finalize_health_changed")
    host_authority = read_private(directory / "host-authority.json")
    prior = validated["context"]["validated_original"]["artifacts"][
        "prior-success.json"
    ]
    vm_prior = {}
    for relative, expected in prior["vm"].items():
        path = root / relative
        exists = path.is_file() and not path.is_symlink()
        actual = (
            {"exists": True, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            if exists else {"exists": False}
        )
        expected_small = (
            {"exists": True, "sha256": expected.get("sha256")}
            if expected.get("exists") is True else {"exists": False}
        )
        if actual != expected_small:
            raise CarryForwardError("group2_partial_finalize_authority_changed")
        vm_prior[relative] = actual
    authority = {
        **host_authority,
        "prior_success": {**host_authority["prior_success"], "vm": vm_prior},
        "vm_files": {
            "transaction.json": _embedded_file_value(root / "transaction.json"),
            "current-sha": _embedded_file_value(root / "current-sha"),
            "phase.json": _embedded_file_value(
                root / "incidents" / incident_id / "phase.json"
            ),
        },
        "vm_installed": {
            name: hashlib.sha256((root / name).read_bytes()).hexdigest()
            for name in ("deploy.sh", "carry_forward.py", "verify.py", "assets.py")
        },
    }
    evidence = {
        "current": current,
        "authority": authority,
        "postgres": postgres,
        "token_health": _read_token_health(root, manifest["account_entry"]),
    }
    receipt = validate_group2_partial_finalize_result(validated, evidence)
    result = {
        "schema": "group2-partial-finalize-result-v1",
        "evidence": evidence,
        "receipt": receipt,
    }
    _atomic_incident_json(directory / "result.json", result)
    _atomic_incident_json(directory / "receipt.json", receipt)
    deadline = time.monotonic() + 300
    while not (directory / "host-validated.json").exists():
        if time.monotonic() >= deadline:
            raise CarryForwardError("group2_partial_finalize_host_timeout")
        time.sleep(0.25)
    acknowledgement = read_private(directory / "host-validated.json")
    source_artifacts = {}
    for name in sorted(tuple(artifacts)):
        raw = artifacts.pop(name)
        source_artifacts[name] = {
            "sha256": hashlib.sha256(raw).hexdigest(),
            "content_b64": base64.b64encode(raw).decode("ascii"),
        }
    gc.collect()
    chain = {
        "schema": "group2-partial-finalize-chain-v1",
        "request": request,
        "approval": approval,
        "user_record": {
            "sha256": hashlib.sha256(user_record).hexdigest(),
            "content_b64": base64.b64encode(user_record).decode("ascii"),
        },
        "source_artifacts": source_artifacts,
        "result": result,
    }
    _validate_group2_partial_finalize_preservation(
        chain, manifest["review_scope"]["preservation_reference"], validated
    )
    expected_acknowledgement = {
        "finalize_id": finalize_id,
        "request_digest": request["request_digest"],
        "result_digest": digest(result),
        "receipt_digest": receipt["receipt_digest"],
        "chain_digest": streaming_digest(chain),
    }
    if acknowledgement != expected_acknowledgement:
        raise CarryForwardError("group2_partial_finalize_host_invalid")
    _validate_group2_partial_finalize_directory(root, incident_id, finalize_id)
    current_vm_files = {
        "transaction.json": _embedded_file_value(root / "transaction.json"),
        "current-sha": _embedded_file_value(root / "current-sha"),
        "phase.json": _embedded_file_value(
            root / "incidents" / incident_id / "phase.json"
        ),
    }
    current_vm_installed = {
        name: hashlib.sha256((root / name).read_bytes()).hexdigest()
        for name in ("deploy.sh", "carry_forward.py", "verify.py", "assets.py")
    }
    current_vm_prior = {}
    for relative in prior["vm"]:
        path = root / relative
        current_vm_prior[relative] = (
            {"exists": True, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            if path.is_file() and not path.is_symlink() else {"exists": False}
        )
    if (
        current_vm_files != authority["vm_files"]
        or current_vm_installed != authority["vm_installed"]
        or current_vm_prior != authority["prior_success"]["vm"]
        or {
            service: _inspect_container(project, service)
            for service in ("postgres", "rabbitmq", "control", "worker", "web", "account-web")
        }
        != current["containers"]
    ):
        raise CarryForwardError("group2_partial_finalize_authority_changed")
    prior_state = validated["context"]["validated_original"]["artifacts"][
        "authority.json"
    ]["snapshot"]["state"]
    _atomic_incident_json(
        root / "transaction.json",
        {
            "phase": "ready", "sha": GROUP2_FROM_SHA,
            "operation": "incident_partial_finalize",
            "reconciled_by": {
                "incident_id": incident_id, "finalize_id": finalize_id,
                "receipt_digest": receipt["receipt_digest"],
            },
            "backup": prior_state["backup"],
            "carry_forward": prior_state["carry_forward"],
            "at": time.time(),
        },
    )
    _atomic_incident_json(
        directory / "phase.json",
        {
            "schema": "group2-partial-finalize-phase-v1",
            "incident_id": incident_id, "finalize_id": finalize_id,
            "phase": "vm-committed",
        },
    )
    return {"code": "group2_partial_finalize_vm_ok",
            "receipt_digest": receipt["receipt_digest"]}


def _reboot_phase(
    directory: Path, request: dict[str, Any], phase: str,
    completed: list[str], pending_action: str | None,
    evidence_digests: dict[str, str],
) -> None:
    _atomic_incident_json(
        directory / "phase.json",
        {
            "schema": "group2-post-finalize-reboot-phase-v1",
            "incident_id": request["incident_id"],
            "finalize_id": request["finalize_id"],
            "boot_id": request["boot_id"],
            "phase": phase,
            "completed_stages": completed,
            "pending_action": pending_action,
            "evidence_digests": evidence_digests,
        },
    )


def _recover_group2_post_finalize_reboot_vm_once(
    root: Path, incident_id: str, finalize_id: str, boot_id: str
) -> dict[str, Any]:
    """Execute the one prepared same-container reboot recovery, once."""
    root = root.resolve(strict=True)
    directory = _validate_group2_post_finalize_reboot_directory(
        root, incident_id, finalize_id, boot_id
    )
    request = read_private(directory / "request.json")
    approval = read_private(directory / "USER-APPROVAL.json")
    user_record = (directory / "USER-APPROVAL.txt").read_bytes()
    artifacts = {
        name: (directory / "evidence" / name).read_bytes()
        for name in GROUP2_POST_FINALIZE_REBOOT_FILES
    }
    validated = validate_group2_post_finalize_reboot_request(
        request, approval, user_record, artifacts
    )
    attempt = _closed_object(
        read_private(directory / "attempt.json"),
        {"schema", "incident_id", "finalize_id", "boot_id", "request_digest",
         "tool_sha", "window", "status"},
        "group2_reboot_replay_rejected",
    )
    if (
        request["incident_id"] != incident_id
        or request["finalize_id"] != finalize_id
        or request["boot_id"] != boot_id
        or attempt != {
            "schema": "group2-post-finalize-reboot-attempt-v1",
            "incident_id": incident_id, "finalize_id": finalize_id,
            "boot_id": boot_id, "request_digest": request["request_digest"],
            "tool_sha": request["tool"]["sha"], "window": request["window"],
            "status": "claimed",
        }
        or read_private(directory / "phase.json")
        != {
            "schema": "group2-post-finalize-reboot-phase-v1",
            "incident_id": incident_id, "finalize_id": finalize_id,
            "boot_id": boot_id, "phase": "prepared", "completed_stages": [],
            "pending_action": None, "evidence_digests": {},
        }
    ):
        raise CarryForwardError("group2_reboot_replay_rejected")
    tool_directory = directory / "tool"
    if (
        not tool_directory.is_dir() or tool_directory.is_symlink()
        or {item.name for item in tool_directory.iterdir()}
        != {"deploy.sh", "carry_forward.py"}
    ):
        raise CarryForwardError("group2_reboot_tool_changed")
    for name in ("deploy.sh", "carry_forward.py"):
        path = tool_directory / name
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or hashlib.sha256(path.read_bytes()).hexdigest()
            != request["tool"]["controller_files"][name]
        ):
            raise CarryForwardError("group2_reboot_tool_changed")
    profile = validated["platform"]["account_entry"]
    project = profile["project"]
    params = request["prior"]["parameters"]["keeper"]
    stopped_authority = _verify_group2_reboot_vm_authority(root, validated)
    completed: list[str] = []
    digests: dict[str, str] = {}
    _reboot_phase(directory, request, "stopped", completed, "capture-stopped", digests)
    stopped_start_ns = time.time_ns()
    stopped_raw = {
        service: _inspect_reboot_container_raw(
            request["prior"]["containers"][service]["container_id"]
        )
        for service in ("postgres", "rabbitmq", "control", "worker", "web", "account-web")
    }
    stopped_containers = {
        service: _inspect_container(project, service) for service in stopped_raw
    }
    stopped = {
        "schema": "group2-post-finalize-reboot-stopped-v1",
        "boot_id": boot_id,
        "files": _capture_group2_reboot_files(directory, request, project, "stopped-files"),
        "logs": read_log_append(validated["parent"]["evidence"]["current"]["logs"]),
        "containers": stopped_containers,
        "container_inspect": stopped_raw,
        "images": {name: item["image_id"] for name, item in stopped_containers.items()},
        "storage": _live_storage_identity(project),
        "authority": {**stopped_authority, "keeper": "missing"},
        "postgres": _capture_group2_reboot_postgres(
            request, stopped_raw["postgres"], running=False
        ),
        "platform": _capture_group2_reboot_stopped_platform(request, params["unit"]),
        "window_start_ns": stopped_start_ns,
        "window_end_ns": time.time_ns(),
    }
    validate_group2_post_finalize_reboot_stopped(validated, stopped)
    _atomic_incident_json(directory / "stopped.json", stopped)
    completed.append("stopped"); digests["stopped"] = digest(stopped)
    script = root / "prepare-sandbox-host.sh"
    _verify_group2_reboot_vm_authority(root, validated)
    if (
        _capture_group2_reboot_stopped_platform(request, params["unit"])
        != stopped["platform"]
        or _live_storage_identity(project) != stopped["storage"]
        or {
            service: _inspect_container(project, service)
            for service in stopped["containers"]
        } != stopped["containers"]
        or {
            service: _inspect_reboot_container_raw(
                request["prior"]["containers"][service]["container_id"]
            )
            for service in stopped["containers"]
        } != stopped["container_inspect"]
    ):
        raise CarryForwardError("group2_reboot_stopped_invalid")
    _reboot_phase(directory, request, "stopped", completed, "prepare-bound-sandbox-keeper", digests)
    keeper_start_ns = time.time_ns()
    _run_group2_reboot_action(
        directory, "prepare-keeper",
        [str(script), "--unit", params["unit"], "--cpu-quota", params["cpu_quota"],
         "--memory-max", params["memory_max"]],
        _reboot_timeout(request, 180),
    )
    expected_description = (
        f"DataLinkRuntime Sandbox {params['unit']} CPU={params['cpu_quota']} "
        f"Memory={params['memory_max']}"
    )
    kernel = _capture_group2_reboot_kernel(
        request, idle=True, expected_description=expected_description,
    )
    keeper = {
        "schema": "group2-post-finalize-reboot-stage-v1", "phase": "keeper_ready",
        "boot_id": boot_id, "files": stopped["files"],
        "logs": read_log_append(stopped["logs"]),
        "containers": stopped["containers"], "images": stopped["images"],
        "container_inspect": stopped["container_inspect"],
        "storage": stopped["storage"],
        "postgres": stopped["postgres"],
        "authority": {**stopped["authority"], "keeper": kernel},
        "kernel": kernel, "window_start_ns": keeper_start_ns,
        "window_end_ns": time.time_ns(),
    }
    stopped_stage = {
        **stopped, "schema": "group2-post-finalize-reboot-stage-v1",
        "phase": "stopped",
    }
    validate_group2_post_finalize_reboot_transition(
        validated, stopped_stage, keeper, "keeper_ready"
    )
    _atomic_incident_json(directory / "keeper-ready.json", keeper)
    completed.append("keeper_ready"); digests["keeper_ready"] = digest(keeper)
    _verify_group2_reboot_vm_authority(root, validated)
    _reboot_phase(directory, request, "keeper_ready", completed, "start-bound-postgres-rabbitmq", digests)
    database_start_ns = time.time_ns()
    for service in ("postgres", "rabbitmq"):
        _start_reboot_container(directory, request, project, service)
    manifest = {
        "selection": validated["parent"]["snapshot"]["selection"],
        "storage_identity": request["prior"]["storage"],
        "old_image_ids": {
            f"dlr-preview-control:{GROUP2_FROM_SHA}": request["prior"]["containers"]["control"]["image_id"]
        },
    }
    admission_seed_path = directory / "database-state" / "admission-seed.json"
    db, files = _capture_reconcile_state(
        root, directory, "database-state", manifest, project,
        admission_output=admission_seed_path,
        timeout=_reboot_timeout(request, 300),
    )
    admission = capture_group2_reboot_start_admission(
        root, directory, request, db, files, read_private(admission_seed_path), project,
        validated["parent"]["snapshot"]["selection"],
    )
    database = {
        "schema": "group2-post-finalize-reboot-stage-v1", "phase": "database_ready",
        "boot_id": boot_id, "files": files, "logs": read_log_append(keeper["logs"]),
        "containers": {service: _inspect_container(project, service) for service in stopped["containers"]},
        "container_inspect": {
            service: _inspect_reboot_container_raw(
                request["prior"]["containers"][service]["container_id"]
            ) for service in stopped["containers"]
        },
        "images": stopped["images"], "storage": _live_storage_identity(project),
        "authority": keeper["authority"], "db": db,
        "postgres": _capture_group2_reboot_postgres(
            request, _inspect_reboot_container_raw(
                request["prior"]["containers"]["postgres"]["container_id"]
            ), running=True,
        ),
        "start_admission": admission, "window_start_ns": database_start_ns,
        "window_end_ns": time.time_ns(),
    }
    validate_group2_post_finalize_reboot_transition(
        validated, keeper, database, "database_ready"
    )
    _atomic_incident_json(directory / "database-ready.json", database)
    completed.append("database_ready"); digests["database_ready"] = digest(database)
    _verify_group2_reboot_vm_authority(root, validated)
    _reboot_phase(directory, request, "database_ready", completed, "start-bound-control-worker-web", digests)
    start_ns = time.time_ns()
    for service in ("control", "worker", "web"):
        _start_reboot_container(directory, request, project, service)
    startup_end_ns = time.time_ns()
    live = _capture_reconcile_stage(
        root, directory, "applications-state", manifest, project, database["logs"],
        {"sandbox_unit": params["unit"], "sandbox_cpu_quota": params["cpu_quota"],
         "sandbox_memory_max": params["memory_max"]}, require_idle=False,
        baseline_authority=None,
        runtime_volume=_reboot_volume(request["prior"]["storage"], "worker", "/var/lib/dlr/runtime"),
        journal_volume=_reboot_volume(request["prior"]["storage"], "worker", "/var/lib/dlr/journal"),
        admission_output=directory / "applications-state" / "mutation-seed.json",
        timeout=_reboot_timeout(request, 300),
        reboot_request=request,
    )
    startup_request = {
        "mode": GROUP2_MODE, "operation": "startup-proof", "profile": profile,
        "logs_before": database["logs"], "logs_after": live["logs"],
        "container_before": database["containers"]["worker"],
        "container_after": live["containers"]["worker"],
        "window_start_ns": start_ns, "window_end_ns": startup_end_ns,
    }
    proof = _same_container_worker_startup_proof(
        startup_request, profile,
        request["prior"]["containers"]["worker"]["image_id"],
        profile["old_profiles"]["worker"], validated["platform"]["prior_nonces"],
    )
    applications = {
        "schema": "group2-post-finalize-reboot-stage-v1", "phase": "applications_started",
        "boot_id": boot_id, **live, "images": stopped["images"],
        "container_inspect": {
            service: _inspect_reboot_container_raw(
                request["prior"]["containers"][service]["container_id"]
            ) for service in stopped["containers"]
        },
        "authority": keeper["authority"], "postgres": _capture_group2_reboot_postgres(
            request, _inspect_reboot_container_raw(
                request["prior"]["containers"]["postgres"]["container_id"]
            ), running=True,
        ),
        "start_admission": admission, "startup_request": startup_request,
        "startup_proof": proof,
        "startup_files": compare_group2_startup_files(database["files"], live["files"], proof),
    }
    applications["rabbit_identity"] = capture_group2_reboot_rabbit_identity(
        request, admission["configuration"]
    )
    applications["window_start_ns"] = start_ns
    applications["window_end_ns"] = time.time_ns()
    validate_group2_post_finalize_reboot_transition(
        validated, database, applications, "applications_started"
    )
    _atomic_incident_json(directory / "applications-started.json", applications)
    completed.append("applications_started"); digests["applications_started"] = digest(applications)
    fresh = _capture_reconcile_stage(
        root, directory, "verified-state", manifest, project, applications["logs"],
        {"sandbox_unit": params["unit"], "sandbox_cpu_quota": params["cpu_quota"],
         "sandbox_memory_max": params["memory_max"]}, require_idle=False,
        baseline_authority=applications["kernel"].get("old_worker_authority"),
        runtime_volume=_reboot_volume(request["prior"]["storage"], "worker", "/var/lib/dlr/runtime"),
        journal_volume=_reboot_volume(request["prior"]["storage"], "worker", "/var/lib/dlr/journal"),
        admission_output=directory / "verified-state" / "mutation-seed.json",
        timeout=_reboot_timeout(request, 300),
        reboot_request=request,
    )
    verified = {
        "schema": "group2-post-finalize-reboot-stage-v1", "phase": "verified",
        "boot_id": boot_id, **fresh, "images": stopped["images"],
        "container_inspect": {
            service: _inspect_reboot_container_raw(
                request["prior"]["containers"][service]["container_id"]
            ) for service in stopped["containers"]
        },
        "authority": keeper["authority"], "postgres": _capture_group2_reboot_postgres(
            request, _inspect_reboot_container_raw(
                request["prior"]["containers"]["postgres"]["container_id"]
            ), running=True,
        ),
        "start_admission": admission, "startup_request": startup_request,
        "startup_proof": proof, "startup_files": applications["startup_files"],
    }
    verified["rabbit_identity"] = capture_group2_reboot_rabbit_identity(
        request, admission["configuration"]
    )
    verified["window_end_ns"] = time.time_ns()
    validate_group2_post_finalize_reboot_transition(validated, applications, verified, "verified")
    _atomic_incident_json(directory / "verified.json", verified)
    completed.append("verified"); digests["verified"] = digest(verified)
    final_authority = _verify_group2_reboot_vm_authority(root, validated)
    if {**final_authority, "keeper": keeper["kernel"]} != verified["authority"]:
        raise CarryForwardError("group2_reboot_authority_changed")
    evidence = {
        "stopped": stopped, "keeper_ready": keeper, "database_ready": database,
        "applications_started": applications, "verified": verified,
        "authority": {"before": stopped["authority"], "after": verified["authority"]},
        "commands": {
            action: {
                "intent": _embedded_file_value(directory / f"command-{action}-intent.json"),
                "result": _embedded_file_value(directory / f"command-{action}-result.json"),
            }
            for action in ("prepare-keeper", "start-postgres", "start-rabbitmq",
                           "start-control", "start-worker", "start-web")
        },
    }
    receipt = validate_group2_post_finalize_reboot_result(validated, evidence)
    result = {"schema": "group2-post-finalize-reboot-result-v1",
              "evidence": evidence, "receipt": receipt}
    source_artifacts = {
        name: _embedded_file_value(directory / "evidence" / name)
        for name in GROUP2_POST_FINALIZE_REBOOT_FILES
    }
    chain = {
        "schema": "group2-post-finalize-reboot-chain-v1", "request": request,
        "approval": approval, "user_record": _embedded_file_value(directory / "USER-APPROVAL.txt"),
        "source_artifacts": source_artifacts, "result": result,
    }
    snapshot = validate_group2_post_finalize_reboot_preservation(chain)
    for name, value in (("result.json", result), ("receipt.json", receipt),
                        ("chain.json", chain), ("preservation-snapshot.json", snapshot)):
        _atomic_incident_json(directory / name, value)
    _reboot_phase(directory, request, "verified", completed, "host-readback", digests)
    deadline = time.monotonic() + _reboot_timeout(request, 300)
    acknowledgement_path = directory / "host-validated.json"
    while True:
        if acknowledgement_path.exists():
            acknowledgement = read_private(acknowledgement_path)
            break
        if time.monotonic() >= deadline:
            raise CarryForwardError("group2_reboot_host_validation_timeout")
        time.sleep(0.25)
    if acknowledgement != {
        "incident_id": incident_id, "finalize_id": finalize_id, "boot_id": boot_id,
        "receipt_digest": receipt["receipt_digest"],
    }:
        raise CarryForwardError("group2_reboot_host_validation_invalid")
    completed.append("host_verified")
    _reboot_phase(directory, request, "host_verified", completed, None,
                  receipt["stage_digests"])
    return {"code": "group2_post_finalize_reboot_vm_ok",
            "receipt_digest": receipt["receipt_digest"]}


def _write_group2_reboot_failure(
    directory: Path, request: Any, error: BaseException
) -> None:
    """Persist the last durable boundary without replacing an earlier failure."""
    failure_path = directory / "failure.json"
    if failure_path.exists():
        return
    phase_path = directory / "phase.json"
    try:
        phase = read_private(phase_path)
    except (CarryForwardError, OSError):
        phase = None
    action_records: dict[str, Any] = {}
    for action in (
        "prepare-keeper", "start-postgres", "start-rabbitmq",
        "start-control", "start-worker", "start-web",
    ):
        records: dict[str, Any] = {}
        for kind in ("intent", "result"):
            path = directory / f"command-{action}-{kind}.json"
            if path.is_file() and not path.is_symlink():
                records[kind] = _embedded_file_value(path)
        if records:
            action_records[action] = records
    captured: dict[str, Any] = {}
    for name in (
        "stopped.json", "keeper-ready.json", "database-ready.json",
        "applications-started.json", "verified.json", "result.json",
        "receipt.json", "chain.json", "preservation-snapshot.json",
    ):
        path = directory / name
        if path.is_file() and not path.is_symlink():
            captured[name] = {
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "size": path.stat().st_size,
            }
    code = error.code if isinstance(error, CarryForwardError) else "verifier_internal_error"
    now = time.time_ns()
    value = {
        "schema": "group2-post-finalize-reboot-failure-v1",
        "incident_id": request.get("incident_id") if isinstance(request, dict) else None,
        "finalize_id": request.get("finalize_id") if isinstance(request, dict) else None,
        "boot_id": request.get("boot_id") if isinstance(request, dict) else None,
        "request_digest": request.get("request_digest") if isinstance(request, dict) else None,
        "failed_at_ns": now,
        "code": code,
        "last_phase": phase,
        "action_records": action_records,
        "captured_files": captured,
    }
    data = canonical_bytes(value) + b"\n"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(failure_path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
    finally:
        os.close(descriptor)
    parent = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(parent)
    finally:
        os.close(parent)
    if isinstance(phase, dict) and isinstance(request, dict):
        failed_phase = dict(phase)
        failed_phase.update({
            "phase": "failed", "pending_action": "failure.json",
            "error_code": code, "failure_digest": digest(value),
        })
        _atomic_incident_json(phase_path, failed_phase)


def recover_group2_post_finalize_reboot_vm(
    root: Path, incident_id: str, finalize_id: str, boot_id: str
) -> dict[str, Any]:
    directory: Path | None = None
    request: Any = None
    previous_handler: Any = None

    def interrupted(_signum: int, _frame: Any) -> None:
        raise CarryForwardError("group2_reboot_interrupted")

    try:
        resolved = root.resolve(strict=True)
        directory = _validate_group2_post_finalize_reboot_directory(
            resolved, incident_id, finalize_id, boot_id
        )
        request = read_private(directory / "request.json")
        if (directory / "failure.json").exists():
            raise CarryForwardError("group2_reboot_replay_rejected")
        if hasattr(signal, "SIGTERM"):
            previous_handler = signal.signal(signal.SIGTERM, interrupted)
        return _recover_group2_post_finalize_reboot_vm_once(
            resolved, incident_id, finalize_id, boot_id
        )
    except BaseException as error:
        if directory is not None and isinstance(error, (Exception, KeyboardInterrupt)):
            _write_group2_reboot_failure(directory, request, error)
        raise
    finally:
        if previous_handler is not None:
            signal.signal(signal.SIGTERM, previous_handler)


def _command_reconcile_vm(args: argparse.Namespace) -> dict[str, Any]:
    return reconcile_group2_vm(args.root, args.incident_id)


def _command_finalize_partial_vm(args: argparse.Namespace) -> dict[str, Any]:
    return finalize_group2_partial_vm(args.root, args.incident_id, args.finalize_id)


def _command_post_finalize_reboot_vm(args: argparse.Namespace) -> dict[str, Any]:
    return recover_group2_post_finalize_reboot_vm(
        args.root, args.incident_id, args.finalize_id, args.boot_id
    )


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    db = commands.add_parser("check-db")
    db.add_argument("--ids", type=Path)
    db.add_argument("--output", type=Path, required=True)
    db.add_argument("--baseline", type=Path)
    db.add_argument("--schema-phase", choices=("before", "after"))
    db.add_argument("--mode", choices=(AUDITED_MODE, GROUP2_MODE))
    capture = commands.add_parser("capture")
    capture.add_argument("--runtime-root", type=Path, required=True)
    capture.add_argument("--journal-root", type=Path, required=True)
    capture.add_argument("--db", type=Path)
    capture.add_argument("--material-root", action="append", default=[])
    capture.add_argument("--expected-uid", type=int)
    capture.add_argument("--baseline", type=Path)
    capture.add_argument("--output", type=Path, required=True)
    state = commands.add_parser("capture-state")
    state.add_argument("--ids", type=Path)
    state.add_argument("--baseline", type=Path)
    state.add_argument("--schema-phase", choices=("before", "after"))
    state.add_argument("--mode", choices=(AUDITED_MODE, GROUP2_MODE))
    state.add_argument("--runtime-root", type=Path, required=True)
    state.add_argument("--journal-root", type=Path, required=True)
    state.add_argument("--material-root", action="append", default=[])
    state.add_argument("--expected-uid", type=int)
    state.add_argument("--db-output", type=Path, required=True)
    state.add_argument("--files-output", type=Path, required=True)
    state.add_argument("--admission-output", type=Path)
    kernel = commands.add_parser("check-kernel")
    kernel.add_argument("--unit", required=True)
    kernel.add_argument("--expected-description")
    kernel.add_argument("--worker-container")
    kernel.add_argument("--runtime-volume")
    kernel.add_argument("--journal-volume")
    kernel.add_argument("--require-idle", action="store_true")
    kernel.add_argument("--baseline", type=Path)
    kernel.add_argument("--output", type=Path, required=True)
    plan = commands.add_parser("plan")
    for name in ("ids", "context", "db", "files", "kernel", "output"):
        plan.add_argument(f"--{name}", type=Path, required=True)
    compare = commands.add_parser("compare")
    compare.add_argument("--before", type=Path, required=True)
    compare.add_argument("--after", type=Path, required=True)
    runtime = commands.add_parser("group2-runtime")
    runtime.add_argument("--request", type=Path, required=True)
    runtime.add_argument("--output", type=Path, required=True)
    reconcile = commands.add_parser("reconcile-vm")
    reconcile.add_argument("--root", type=Path, required=True)
    reconcile.add_argument("--incident-id", required=True)
    finalize = commands.add_parser("finalize-partial-vm")
    finalize.add_argument("--root", type=Path, required=True)
    finalize.add_argument("--incident-id", required=True)
    finalize.add_argument("--finalize-id", required=True)
    reboot = commands.add_parser("post-finalize-reboot-vm")
    reboot.add_argument("--root", type=Path, required=True)
    reboot.add_argument("--incident-id", required=True)
    reboot.add_argument("--finalize-id", required=True)
    reboot.add_argument("--boot-id", required=True)
    return root


def main() -> None:
    args = parser().parse_args()
    try:
        if args.command == "check-db":
            result = _command_check_db(args)
        elif args.command == "capture":
            result = _command_capture(args)
        elif args.command == "capture-state":
            result = _command_capture_state(args)
        elif args.command == "check-kernel":
            result = _command_kernel(args)
        elif args.command == "plan":
            result = _command_plan(args)
        elif args.command == "compare":
            result = _command_compare(args)
        elif args.command == "group2-runtime":
            result = _command_group2_runtime(args)
        elif args.command == "reconcile-vm":
            if MANIFEST_ID.fullmatch(args.incident_id) is None:
                raise CarryForwardError("group2_reconcile_request_invalid")
            result = _command_reconcile_vm(args)
        elif args.command == "finalize-partial-vm":
            if (
                MANIFEST_ID.fullmatch(args.incident_id) is None
                or MANIFEST_ID.fullmatch(args.finalize_id) is None
            ):
                raise CarryForwardError("group2_partial_finalize_request_invalid")
            result = _command_finalize_partial_vm(args)
        else:
            if (
                args.incident_id != GROUP2_PARTIAL_INCIDENT_ID
                or args.finalize_id != GROUP2_POST_FINALIZE_REBOOT_PARENT_FINALIZE_ID
                or BOOT_ID.fullmatch(args.boot_id) is None
            ):
                raise CarryForwardError("group2_reboot_request_invalid")
            result = _command_post_finalize_reboot_vm(args)
        print(json.dumps(result, sort_keys=True))
    except CarryForwardError as error:
        print(json.dumps({"code": error.code}, sort_keys=True), file=sys.stderr)
        raise SystemExit(2) from None
    except Exception:  # noqa: BLE001 - fail closed without exposing private details
        print(
            json.dumps({"code": "verifier_internal_error"}, sort_keys=True),
            file=sys.stderr,
        )
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
