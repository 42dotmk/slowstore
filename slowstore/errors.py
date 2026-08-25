"""Errors and policies for on-disk change detection.

Lives in its own module (no slowstore imports) so both :mod:`slowstore.store`
and :mod:`slowstore.blob` can use it without an import cycle.
"""

from __future__ import annotations

from typing import Any


class ConflictPolicy:
    """What ``commit`` does when a record/blob changed on disk under it.

    Passed to :class:`~slowstore.store.Store` as ``on_conflict=...``.
    """

    OVERWRITE: str = "overwrite"
    """Write our version anyway, clobbering the on-disk change (default; this is
    the historical behavior)."""

    ERROR: str = "error"
    """Raise :class:`StaleWriteError` and leave disk untouched."""

    RELOAD: str = "reload"
    """Discard our version and re-read the record/blob from disk (disk wins)."""


class StaleWriteError(RuntimeError):
    """A record or blob changed on disk since it was loaded, and the store's
    ``on_conflict`` policy is :data:`ConflictPolicy.ERROR`."""

    def __init__(
        self,
        key: Any = None,
        field: str | None = None,
        collection: str | None = None,
        message: str | None = None,
    ) -> None:
        self.key = key
        self.field = field
        self.collection = collection
        if message is None:
            where = f"'{key}'"
            if field:
                where += f".{field}"
            if collection:
                where = f"{collection}:{where}"
            message = f"{where} changed on disk since it was loaded"
        super().__init__(message)
