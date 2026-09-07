"""Persistent administrator-owned offline materials, separate from task attachments."""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Identity,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from dlr.control.db import Base


class BuiltinPackageSettings(Base):
    __tablename__ = "builtin_package_settings"
    __table_args__ = (CheckConstraint("id = 1 AND quota_bytes > 0", name="ck_builtin_settings"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    quota_bytes: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="1073741824"
    )


class BuiltinPackage(Base):
    __tablename__ = "builtin_packages"
    __table_args__ = (
        UniqueConstraint("kind", "name", "version", "environment", name="uq_builtin_identity"),
        UniqueConstraint("kind", "repository_path", name="uq_builtin_path"),
        CheckConstraint(
            "kind IN ('pypi', 'npm', 'maven', 'goproxy') AND size_bytes > 0",
            name="ck_builtin_package",
        ),
        CheckConstraint("status IN ('uploaded', 'deleting')", name="ck_builtin_status"),
    )
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(String(256))
    version: Mapped[str] = mapped_column(String(128))
    environment: Mapped[str] = mapped_column(String(512))
    filename: Mapped[str] = mapped_column(String(256))
    repository_path: Mapped[str] = mapped_column(String(512))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64))
    storage_key: Mapped[str] = mapped_column(String(36), unique=True)
    package_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb")
    )
    status: Mapped[str] = mapped_column(String(16), server_default="uploaded")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class BuiltinPackageUpload(Base):
    __tablename__ = "builtin_package_uploads"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('pypi', 'npm', 'maven', 'goproxy') AND size_bytes > 0",
            name="ck_builtin_upload",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))
    filename: Mapped[str] = mapped_column(String(256))
    repository_path: Mapped[str] = mapped_column(String(512), default="")
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
