"""Typed references to unstructured ("blob") content stored outside the record.

A :class:`BlobRef` is a small, serializable descriptor that lives *inside* a
record's JSON while the actual bytes live in a sidecar managed by the store's
:class:`~slowstore.layout.StorageLayout`.  Bytes are read lazily on first access
and, depending on ``cache``, kept in memory or re-read every time.

The descriptor serializes compactly (``model_dump(by_alias=True)``)::

    {"__blob__": true, "id": "avatar", "content_type": "image/png",
     "size": 20481, "cache": "reference"}

Runtime context (which store/key/field owns the blob) is *not* serialized; the
owning :class:`~slowstore.proxy.Proxy` injects it via :meth:`_bind` when the
record is wrapped or the field is (re)assigned.
"""

from __future__ import annotations

import io
import os
from typing import TYPE_CHECKING, Any, get_args, get_origin

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

if TYPE_CHECKING:
    from .proxy import Proxy


class CacheMode:
    """How a lazily-loaded value behaves after its first read.

    Shared by :class:`BlobRef` and :class:`slowstore.ref.Ref`.
    """

    REFERENCE: str = "reference"
    """Keep the resolved value in memory after the first read."""

    EVERY_TIME: str = "every_time"
    """Re-read from disk / re-resolve on every access."""


class BlobRef(BaseModel):
    """A lazily-loaded pointer to blob content stored beside the record."""

    model_config = ConfigDict(populate_by_name=True)

    # marker so the loader can recognize a blob descriptor in raw JSON.
    marker: bool = Field(default=True, alias="__blob__")
    id: str | None = None
    content_type: str | None = None
    size: int = 0
    cache: str = CacheMode.REFERENCE

    # runtime-only binding; excluded from serialization.
    _proxy: "Proxy[Any] | None" = PrivateAttr(default=None)
    _store: Any = PrivateAttr(default=None)
    _key: str | None = PrivateAttr(default=None)
    _field: str | None = PrivateAttr(default=None)
    _data: bytes | None = PrivateAttr(default=None)
    _pending: bool = PrivateAttr(default=False)

    # -- construction ----------------------------------------------------------

    @classmethod
    def from_bytes(
        cls,
        data: bytes,
        content_type: str | None = None,
        id: str | None = None,
        cache: str = CacheMode.REFERENCE,
    ) -> "BlobRef":
        """Create an unsaved blob holding ``data`` in memory until commit."""
        blob = cls(id=id, content_type=content_type, size=len(data), cache=cache)
        blob._data = data
        blob._pending = True
        return blob

    @classmethod
    def from_text(
        cls,
        text: str,
        content_type: str = "text/plain",
        encoding: str = "utf-8",
        id: str | None = None,
        cache: str = CacheMode.REFERENCE,
    ) -> "BlobRef":
        """Create an unsaved text blob (a convenience over :meth:`from_bytes`)."""
        return cls.from_bytes(
            text.encode(encoding), content_type=content_type, id=id, cache=cache
        )

    @classmethod
    def from_path(
        cls,
        path: str,
        content_type: str | None = None,
        id: str | None = None,
        cache: str = CacheMode.REFERENCE,
    ) -> "BlobRef":
        """Create an unsaved blob from the contents of a file on disk."""
        with open(path, "rb") as f:
            data = f.read()
        if content_type is None:
            # cheap best-effort hint from the extension; None is fine.
            ext = os.path.splitext(path)[1].lstrip(".")
            content_type = ext or None
        return cls.from_bytes(data, content_type=content_type, id=id, cache=cache)

    # -- binding (called by the Proxy) -----------------------------------------

    def _bind(self, proxy: "Proxy[Any]", field: str) -> None:
        self._proxy = proxy
        self._store = proxy.store
        self._key = proxy.__key__
        self._field = field
        if self.id is None:
            # scalar blob-per-field: the field name is a stable id, and keeps the
            # sidecar filename as ``{key}.{field}.bin``.
            self.id = field

    # -- reading ---------------------------------------------------------------

    @property
    def bytes(self) -> bytes:
        """The blob content, read lazily from the layout on first access."""
        if self._data is not None:
            return self._data
        if self._store is None:
            raise RuntimeError("BlobRef is not bound to a store")
        data = self._store.layout.read_blob(self._key, self._field, self.id)
        if self.cache == CacheMode.REFERENCE:
            self._data = data
        return data

    def open(self) -> io.BytesIO:
        """A binary stream over the blob content."""
        return io.BytesIO(self.bytes)

    @property
    def text(self) -> str:
        """The blob content decoded as UTF-8 text (convenience for reading)."""
        return self.bytes.decode("utf-8")

    @text.setter
    def text(self, value: str) -> None:
        self.set(value, content_type=self.content_type or "text/plain")

    def read_text(self, encoding: str = "utf-8") -> str:
        """Decode the blob content with an explicit ``encoding``."""
        return self.bytes.decode(encoding)

    def write_text(
        self,
        value: str,
        encoding: str = "utf-8",
        content_type: str | None = None,
    ) -> "BlobRef":
        """Replace the content with text; flushed to disk on the next commit."""
        return self.set(
            value.encode(encoding),
            content_type=content_type or self.content_type or "text/plain",
        )

    # -- writing ---------------------------------------------------------------

    def set(self, data: bytes | str, content_type: str | None = None) -> "BlobRef":
        """Replace the blob content; flushed to disk on the next commit."""
        if isinstance(data, str):
            data = data.encode("utf-8")
        self._data = data
        self._pending = True
        self.size = len(data)
        if content_type is not None:
            self.content_type = content_type
        # mutating the blob in place doesn't pass through Proxy.__setattr__, so
        # mark the owning record dirty ourselves and honor save_on_change.
        if self._proxy is not None:
            self._proxy.is_dirty = True
            if self._store is not None and getattr(self._store, "save_on_change", False):
                self._proxy.commit()
        return self

    # -- persistence (called by the Store on commit) ---------------------------

    def _flush(self) -> None:
        if self._pending and self._data is not None and self._store is not None:
            self._store.layout.write_blob(self._key, self._field, self.id, self._data)
            self._pending = False
            if self.cache != CacheMode.REFERENCE:
                # don't retain bytes we were told not to cache.
                self._data = None


_LIST_ORIGINS = (list, tuple, set, frozenset)


def _annotation_is_blob(annotation: Any) -> bool:
    if isinstance(annotation, type) and issubclass(annotation, BlobRef):
        return True
    return any(
        arg is not type(None) and _annotation_is_blob(arg)
        for arg in get_args(annotation)
    )


def _annotation_is_blob_list(annotation: Any) -> bool:
    origin = get_origin(annotation)
    if origin in _LIST_ORIGINS:
        return any(_annotation_is_blob(arg) for arg in get_args(annotation))
    if origin is not None:
        # unwrap Optional/Union around a list (e.g. ``Optional[list[BlobRef]]``)
        return any(
            arg is not type(None) and _annotation_is_blob_list(arg)
            for arg in get_args(annotation)
        )
    return False


def field_is_blob(field_info: Any) -> bool:
    """Whether a Pydantic ``FieldInfo`` declares a ``BlobRef`` field.

    Recognizes ``BlobRef``, ``Optional[BlobRef]`` and ``list[BlobRef]``.
    """
    return _annotation_is_blob(getattr(field_info, "annotation", None))


def field_is_blob_list(field_info: Any) -> bool:
    """Whether a field declares a *list* of blobs (``list[BlobRef]``).

    A list-blob field is also a blob field (:func:`field_is_blob` returns True),
    so callers check this to decide between scalar and per-element handling.
    """
    return _annotation_is_blob_list(getattr(field_info, "annotation", None))
