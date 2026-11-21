"""Shared ORM metadata, independent of runtime settings and connections."""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Declarative base shared by all Control Node models."""
