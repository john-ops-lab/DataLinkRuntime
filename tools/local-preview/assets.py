"""Run inside Control image; emit only hashes of persisted user assets."""

import hashlib
import json
import os
import sys
from collections import Counter
from pathlib import Path

from sqlalchemy import create_engine, inspect, text

TABLES = (
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
)
engine = create_engine(os.environ["DATABASE_URL"])
baseline = json.loads(Path(sys.argv[1]).read_text()) if len(sys.argv) > 1 else None
result = {}
with engine.connect() as connection:
    for table in TABLES:
        columns = (
            baseline[table]["columns"]
            if baseline
            else [c["name"] for c in inspect(engine).get_columns(table)]
        )
        projection = ",".join('"' + name.replace('"', '""') + '"' for name in columns)
        values = connection.execute(
            text(f'SELECT {projection} FROM "{table}"')
        ).mappings()
        hashes = sorted(
            hashlib.sha256(
                json.dumps(dict(row), sort_keys=True, default=str).encode()
            ).hexdigest()
            for row in values
        )
        result[table] = {"columns": columns, "rows": hashes}
if baseline is not None:
    changed = [
        t for t in TABLES if Counter(baseline[t]["rows"]) - Counter(result[t]["rows"])
    ]
    if changed:
        raise RuntimeError("Persisted asset fingerprint changed: " + ", ".join(changed))
print(json.dumps(result, sort_keys=True))
