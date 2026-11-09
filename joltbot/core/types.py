"""Small shared types.

Discord IDs ("snowflakes") are 64-bit numbers. JavaScript numbers can only hold
53 bits exactly, so an ID sent to a browser as a number gets silently rounded
to a different ID. We therefore keep IDs as ``int`` in Python but always write
them as strings in JSON (settings stored in the DB, API responses, forms).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any

from pydantic import BeforeValidator, PlainSerializer


def _parse_snowflake(value: Any) -> int:
    if isinstance(value, bool):  # bool is a subclass of int; True is not an ID
        raise ValueError("must be a Discord ID")
    if isinstance(value, int):
        number = value
    elif isinstance(value, str) and value.strip().isdigit():
        number = int(value.strip())
    else:
        raise ValueError("must be a Discord ID (a long number)")
    if not 0 < number < 2**64:
        raise ValueError("is not a valid Discord ID")
    return number


def _empty_to_none(value: Any) -> Any:
    # An empty <select> in a form submits "", which means "nothing picked".
    return None if value in (None, "", 0, "0") else value


Snowflake = Annotated[
    int,
    BeforeValidator(_parse_snowflake),
    PlainSerializer(str, return_type=str, when_used="json"),
]
OptionalSnowflake = Annotated[Snowflake | None, BeforeValidator(_empty_to_none)]


def utcnow() -> datetime:
    """Current time as a timezone-aware UTC datetime.

    Returns:
        ``datetime`` with ``tzinfo=timezone.utc`` (required by ``UTCDateTime``).
    """
    return datetime.now(timezone.utc)
