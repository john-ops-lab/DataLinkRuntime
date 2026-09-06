"""Allocate readable import names, with database uniqueness as the final arbiter."""

from collections.abc import Callable

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session


def insert_with_available_name(
    session: Session,
    requested: str,
    occupied: Callable[[str], bool],
    insert: Callable[[str], None],
    constraint: str,
) -> None:
    """Retry only name collisions, keeping the rest of the import atomic.

    A concurrent ordinary create/rename can win after the precheck. Isolating
    the insert in a savepoint lets that import choose the next suffix without
    discarding its outer transaction or masking other integrity failures.
    """
    index = 0
    while True:
        suffix = f"({index})" if index else ""
        name = requested[: 128 - len(suffix)] + suffix
        if not occupied(name):
            try:
                with session.begin_nested():
                    insert(name)
                    session.flush()
                return
            except IntegrityError as exc:
                if getattr(getattr(exc.orig, "diag", None), "constraint_name", None) != constraint:
                    raise
        index += 1
