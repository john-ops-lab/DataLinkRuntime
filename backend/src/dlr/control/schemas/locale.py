"""Typed locale contracts for the deployment-wide system language."""

from typing import Literal

from pydantic import BaseModel, ConfigDict

SystemLocale = Literal["zh-CN", "en"]
DEFAULT_SYSTEM_LOCALE: SystemLocale = "zh-CN"
SUPPORTED_SYSTEM_LOCALES: tuple[SystemLocale, ...] = ("zh-CN", "en")


class SystemLocaleUpdate(BaseModel):
    """Administrator request to replace the deployment locale."""

    model_config = ConfigDict(extra="forbid")

    locale: SystemLocale


class SystemLocaleResponse(BaseModel):
    """The only setting exposed by the public locale endpoint."""

    model_config = ConfigDict(extra="forbid")

    locale: SystemLocale
