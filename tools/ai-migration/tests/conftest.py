"""B0 uses synthetic configuration and loopback services only."""

import os
import sys
from pathlib import Path

for variable in tuple(os.environ):
    if variable.startswith(
        ("DLR_", "OPENAI_", "ANTHROPIC_", "GOOGLE_", "GEMINI_", "OTEL_", "LOGFIRE_")
    ):
        del os.environ[variable]
os.environ["DLR_RABBITMQ_URL"] = "amqp://probe:probe@broker.invalid/"
os.environ["DLR_RABBITMQ_MANAGEMENT_URL"] = "http://broker.invalid"
os.environ["DATABASE_URL"] = "postgresql+psycopg://probe:probe@database.invalid/probe"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
