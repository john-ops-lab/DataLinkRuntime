"""Platform settings loaded from environment variables."""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """DLR platform settings.

    Environment variables are matched case-insensitively, e.g.
    ``database_url`` is set via ``DATABASE_URL``.
    """

    database_url: str = "postgresql+psycopg://dlr:dlr@localhost:5432/dlr"


settings = Settings()
