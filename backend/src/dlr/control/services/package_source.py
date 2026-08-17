"""Platform-managed PyPI, npm and Maven dependency sources.

Workers prepare version dependencies offline-first; when the local cache is
not enough they install from the platform default source. The default
source's index URL (with embedded basic auth when a password credential is
bound) travels inside the TaskPayload at claim time.
"""

from urllib import error as url_error
from urllib import parse as url_parse
from urllib import request as url_request

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from dlr.control.models.platform import Credential, PackageSource
from dlr.control.package_source_defaults import (
    DEFAULT_PACKAGE_SOURCES,
    PackageSourceDefault,
    defaults_for_kind,
    preset_id_for_source,
)
from dlr.control.schemas.package_source import (
    DefaultPackageSourceInfo,
    PackageSourceCreate,
    PackageSourceDefaultsResponse,
    PackageSourceResponse,
    PackageSourceUpdate,
)
from dlr.control.services import secrets as secrets_service
from dlr.control.services.adapter import domain_error

# Control-side reachability probes must stay fast and bounded.
REACHABILITY_TIMEOUT_SECONDS = 5.0

# The domestic defaults remain the API's canonical single-value summary. The
# full domestic + official set is shared with migrations through the module
# above; no frontend or test copy is authoritative.
DOMESTIC_DEFAULTS: dict[str, PackageSourceDefault] = {
    source.kind: source for source in DEFAULT_PACKAGE_SOURCES if source.is_domestic
}
DEFAULT_SOURCE_NAME: dict[str, str] = {
    kind: source.name for kind, source in DOMESTIC_DEFAULTS.items()
}
DEFAULT_SOURCE_URL: dict[str, str] = {
    kind: source.index_url for kind, source in DOMESTIC_DEFAULTS.items()
}


def default_source_info(kind: str) -> DefaultPackageSourceInfo:
    if kind not in DEFAULT_SOURCE_URL:
        raise domain_error(
            422,
            "package_source_kind_invalid",
            f"Unknown package source kind: {kind}",
            {"kind": kind},
        )
    return DefaultPackageSourceInfo(
        preset_id=DOMESTIC_DEFAULTS[kind].preset_id,
        kind=kind,
        name=DEFAULT_SOURCE_NAME[kind],
        index_url=DEFAULT_SOURCE_URL[kind],
    )


def list_default_sources() -> PackageSourceDefaultsResponse:
    """Canonical defaults for every kind, independent of the local database."""
    return PackageSourceDefaultsResponse(
        pypi=default_source_info("pypi"),
        npm=default_source_info("npm"),
        maven=default_source_info("maven"),
    )


def _available_default_name(session: Session, preferred_name: str) -> str:
    """Choose a deterministic unused name without touching user-owned rows."""
    name = preferred_name
    suffix = 1
    while session.scalar(select(PackageSource.id).where(PackageSource.name == name)) is not None:
        suffix += 1
        name = f"{preferred_name}（平台默认 {suffix}）"
    return name


def _canonical_source(session: Session, default: PackageSourceDefault) -> PackageSource | None:
    return session.scalar(
        select(PackageSource).where(
            PackageSource.kind == default.kind,
            PackageSource.index_url == default.index_url,
        )
    )


def _ensure_default_sources(session: Session, kind: str) -> tuple[PackageSource, ...]:
    """Ensure both system rows exist while preserving every existing field."""
    defaults = defaults_for_kind(kind)
    if len(defaults) != 2:
        raise domain_error(
            422,
            "package_source_kind_invalid",
            f"Unknown package source kind: {kind}",
            {"kind": kind},
        )

    has_existing_default = (
        session.scalar(
            select(PackageSource.id).where(
                PackageSource.kind == kind, PackageSource.is_default.is_(True)
            )
        )
        is not None
    )
    sources: list[PackageSource] = []
    for default in defaults:
        source = _canonical_source(session, default)
        if source is None:
            source = PackageSource(
                name=_available_default_name(session, default.name),
                kind=default.kind,
                index_url=default.index_url,
                # A pre-existing user default wins during migration-like
                # initialization; restore_default_source explicitly selects
                # the domestic row below.
                is_default=default.is_domestic and not has_existing_default,
                credential_id=None,
            )
            session.add(source)
            session.flush()
        sources.append(source)
    return tuple(sources)


def restore_default_source(session: Session, kind: str) -> PackageSource:
    """Restore both system defaults and select the domestic source.

    Existing custom rows are never renamed, retargeted or deleted. If an
    administrator used a system default name for a custom URL, the restored
    canonical row receives a deterministic suffix instead of overwriting that
    row. Only the per-kind selected-default flag changes, as requested by the
    explicit restore action.
    """
    sources = _ensure_default_sources(session, kind)
    domestic = next(source for source in sources if source.index_url == DEFAULT_SOURCE_URL[kind])
    _clear_other_defaults(session, kind, keep_id=domestic.id)
    domestic.is_default = True
    session.commit()
    session.refresh(domestic)
    return domestic


def _get_credential(session: Session, credential_id: int) -> Credential:
    credential = session.get(Credential, credential_id)
    if credential is None:
        raise domain_error(404, "credential_not_found", "Credential not found")
    return credential


def _validate_credential_kind(kind: str, credential: Credential) -> None:
    allowed = {"password", "token"} if kind == "npm" else {"password"}
    if credential.type not in allowed:
        raise domain_error(
            422,
            "package_source_credential_incompatible",
            f"{kind} sources require {'password or token' if kind == 'npm' else 'password'} "
            "credentials",
            {
                "kind": kind,
                "allowed_types": ["password", "token"] if kind == "npm" else ["password"],
                "actual_type": credential.type,
            },
        )


def _clear_other_defaults(session: Session, kind: str, keep_id: int | None = None) -> None:
    """Keep at most one default source of a kind: the candidate wins."""
    statement = update(PackageSource).where(
        PackageSource.kind == kind, PackageSource.is_default.is_(True)
    )
    if keep_id is not None:
        statement = statement.where(PackageSource.id != keep_id)
    session.execute(statement.values(is_default=False))


def get_package_source(session: Session, package_source_id: int) -> PackageSource:
    source = session.get(PackageSource, package_source_id)
    if source is None:
        raise domain_error(404, "package_source_not_found", "Package source not found")
    return source


def package_source_response(session: Session, source: PackageSource) -> PackageSourceResponse:
    credential_name = None
    if source.credential_id is not None:
        credential = session.get(Credential, source.credential_id)
        credential_name = credential.name if credential is not None else None
    return PackageSourceResponse(
        id=source.id,
        name=source.name,
        preset_id=preset_id_for_source(source.kind, source.index_url),
        kind=source.kind,
        index_url=source.index_url,
        is_default=source.is_default,
        credential_id=source.credential_id,
        credential_name=credential_name,
        created_at=source.created_at,
        updated_at=source.updated_at,
    )


def create_package_source(session: Session, data: PackageSourceCreate) -> PackageSource:
    existing = session.scalar(select(PackageSource).where(PackageSource.name == data.name))
    if existing is not None:
        raise domain_error(
            409,
            "package_source_name_conflict",
            "Package source name already exists",
            {"name": data.name},
        )
    if data.credential_id is not None:
        _validate_credential_kind(data.kind, _get_credential(session, data.credential_id))
    # Clear any previous default before inserting so the partial unique index
    # never sees two defaults, even transiently.
    if data.is_default:
        _clear_other_defaults(session, data.kind)
    source = PackageSource(
        name=data.name,
        kind=data.kind,
        index_url=data.index_url,
        is_default=data.is_default,
        credential_id=data.credential_id,
    )
    session.add(source)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise domain_error(
            409,
            "package_source_name_conflict",
            "Package source name already exists",
            {"name": data.name},
        ) from None
    session.refresh(source)
    return source


def list_package_sources(session: Session) -> list[PackageSource]:
    return list(session.scalars(select(PackageSource).order_by(PackageSource.id.asc())).all())


def update_package_source(
    session: Session, package_source_id: int, data: PackageSourceUpdate
) -> PackageSource:
    source = get_package_source(session, package_source_id)
    if data.name is not None and data.name != source.name:
        conflict = session.scalar(
            select(PackageSource).where(
                PackageSource.name == data.name, PackageSource.id != package_source_id
            )
        )
        if conflict is not None:
            raise domain_error(
                409,
                "package_source_name_conflict",
                "Package source name already exists",
                {"name": data.name},
            )
        source.name = data.name
    if data.index_url is not None:
        source.index_url = data.index_url
    next_kind = data.kind if data.kind is not None else source.kind
    next_credential_id = (
        data.credential_id if "credential_id" in data.model_fields_set else source.credential_id
    )
    if next_credential_id is not None:
        _validate_credential_kind(next_kind, _get_credential(session, next_credential_id))
    if data.kind is not None:
        source.kind = data.kind
    if "credential_id" in data.model_fields_set:
        source.credential_id = data.credential_id
    if data.is_default is not None:
        if data.is_default:
            _clear_other_defaults(session, next_kind, keep_id=source.id)
        source.is_default = data.is_default
    elif data.kind is not None and source.is_default:
        _clear_other_defaults(session, next_kind, keep_id=source.id)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise domain_error(
            409,
            "package_source_name_conflict",
            "Package source name already exists",
            {"name": data.name},
        ) from None
    session.refresh(source)
    return source


def delete_package_source(session: Session, package_source_id: int) -> None:
    source = get_package_source(session, package_source_id)
    session.delete(source)
    session.commit()


# --- claim-time index resolution -------------------------------------------------


def _embed_auth(index_url: str, credential: Credential) -> str:
    """Inline password or token auth into an http(s) repository URL."""
    parts = url_parse.urlsplit(index_url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return index_url
    fields = secrets_service.decrypt_fields(credential.ciphertext)
    if credential.type == "password":
        username = fields.get("username", "")
        password = fields.get("password", "")
    elif credential.type == "token":
        username = fields.get("token", "")
        password = ""
    else:
        return index_url
    userinfo = f"{url_parse.quote(username, safe='')}:{url_parse.quote(password, safe='')}"
    return url_parse.urlunsplit(
        (parts.scheme, f"{userinfo}@{parts.netloc}", parts.path, parts.query, parts.fragment)
    )


def resolve_source_url(session: Session, source: PackageSource) -> str:
    """Effective index URL of one source (basic auth embedded when bound).

    A bound password credential is embedded as basic auth so Workers need no
    extra credential channel; other credential types cannot authenticate a
    pip index and are ignored here.
    """
    if source.credential_id is not None:
        credential = session.get(Credential, source.credential_id)
        if credential is not None:
            return _embed_auth(source.index_url, credential)
    return source.index_url


def resolve_default_index_url(session: Session, kind: str = "pypi") -> str | None:
    """The default source URL for one dependency kind (None if unset)."""
    source = session.scalar(
        select(PackageSource).where(
            PackageSource.kind == kind,
            PackageSource.is_default.is_(True),
        )
    )
    if source is None:
        return None
    return resolve_source_url(session, source)


# --- reachability probe -----------------------------------------------------------


def probe_index_url(index_url: str) -> tuple[bool, int | None, str | None]:
    """Best-effort Control-side probe using only the standard library.

    Any HTTP answer counts as reachable (authenticated indexes may answer
    401/403 without their credentials); only transport-level failures mark
    the source unreachable.
    """
    try:
        request = url_request.Request(index_url, method="GET")
        with url_request.urlopen(  # noqa: S310 - admin-managed http(s) URL
            request, timeout=REACHABILITY_TIMEOUT_SECONDS
        ) as response:
            return True, response.status, None
    except url_error.HTTPError as error:
        # A real HTTP answer: the endpoint is reachable.
        return True, error.code, None
    except (url_error.URLError, TimeoutError, ValueError) as error:
        reason = getattr(error, "reason", None) or error
        return False, None, str(reason)
