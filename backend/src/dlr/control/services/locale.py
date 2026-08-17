"""Deployment-wide locale persistence service."""

from typing import cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from dlr.control.models import SystemSetting
from dlr.control.schemas.locale import (
    DEFAULT_SYSTEM_LOCALE,
    SystemLocale,
    SystemLocaleResponse,
)

_SINGLETON_ID = 1


def get_system_locale(session: Session) -> SystemLocale:
    """Read the authoritative deployment locale, with a safe pre-row default."""
    setting = session.scalar(select(SystemSetting).where(SystemSetting.id == _SINGLETON_ID))
    if setting is None:
        return DEFAULT_SYSTEM_LOCALE
    return cast(SystemLocale, setting.locale)


def system_locale_response(session: Session) -> SystemLocaleResponse:
    """Build the intentionally narrow locale response."""
    return SystemLocaleResponse(locale=get_system_locale(session))


def update_system_locale(session: Session, locale: SystemLocale) -> SystemLocaleResponse:
    """Persist the singleton locale without touching any Adapter state."""
    setting = session.get(SystemSetting, _SINGLETON_ID)
    if setting is None:
        setting = SystemSetting(id=_SINGLETON_ID, locale=locale)
        session.add(setting)
    else:
        setting.locale = locale
    session.commit()
    return system_locale_response(session)
