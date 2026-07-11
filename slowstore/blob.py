"""Typed references to unstructured ("blob") content stored outside the record.

A :class:`BlobRef` is a small, serializable descriptor that lives *inside* a
record's JSON while the actual bytes live in a sidecar managed by the store's
:class:`~slowstore.layout.StorageLayout`.  Bytes are read lazily on first access
and, depending on ``cache``, kept in memory or re-read every time.

``BlobRef`` is a plain Python object (not a Pydantic model); it serializes to a
compact descriptor via :meth:`BlobRef.__to_descriptor__`::

    {"__blob__": true, "id": "avatar", "content_type": "image/png",
     "size": 20481, "cache": "reference"}

so the store's codec (or Pydantic, via the core-schema hook below) can round-trip
it. Runtime context (which store/key/field owns the blob) is *not* serialized; the
owning :class:`~slowstore.proxy.Proxy` injects it via :meth:`_bind` when the
record is wrapped or the field is (re)assigned.
"""

from __future__ import annotations

import io
import os
from typing import TYPE_CHECKING, Any, get_args, get_origin

from .errors import ConflictPolicy, StaleWriteError

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


class BlobRef:
    """A lazily-loaded pointer to blob content stored beside the record.

    A plain Python object -- the persisted fields (``id``/``content_type``/
    ``size``/``cache``) are ordinary attributes, and the runtime binding
    (``_store``/``_key``/``_field``/``_data``/``_pending``/``_stamp``) is never
    serialized. It can still be used as a field in a Pydantic model thanks to
    :meth:`__get_pydantic_core_schema__`.
    """

    def __init__(
        self,
        id: str | None = None,
        content_type: str | None = None,
        size: int = 0,
        cache: str = CacheMode.REFERENCE,
    ) -> None:
        self.id: str | None = id
        self.content_type: str | None = content_type
        self.size: int = size
        self.cache: str = cache
        # runtime-only binding; never serialized.
        self._proxy: "Proxy[Any] | None" = None
        self._store: Any = None
        self._key: str | None = None
        self._field: str | None = None
        self._data: bytes | None = None
        self._pending: bool = False
        # ``(mtime_ns, size)`` of the sidecar this ref is based on, captured when
        # the bytes are read or the record is loaded. Lets ``_flush`` detect a
        # concurrent external write before overwriting it. ``None`` = no baseline.
        self._stamp: Any = None

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
        # Baseline stamp for a lazily-loaded blob (descriptor came from disk, bytes
        # not yet materialized) so a later _flush can detect a concurrent external
        # write even if we never read the sidecar. Freshly-created blobs are
        # ``_pending`` and have no on-disk baseline, so they skip this.
        if self._stamp is None and self._data is None and not self._pending:
            self._stamp = self._store.layout.blob_stamp(self._key, field, self.id)

    # -- reading ---------------------------------------------------------------

    @property
    def bytes(self) -> bytes:
        """The blob content, read lazily from the layout on first access."""
        if self._data is not None:
            return self._data
        if self._store is None:
            raise RuntimeError("BlobRef is not bound to a store")
        data = self._store.layout.read_blob(self._key, self._field, self.id)
        # remember what we read so a later _flush can tell if the sidecar was
        # rewritten by someone else in the meantime.
        self._stamp = self._store.layout.blob_stamp(self._key, self._field, self.id)
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
            layout = self._store.layout
            policy = getattr(self._store, "on_conflict", ConflictPolicy.OVERWRITE)
            # optimistic concurrency: if the sidecar changed since we based our
            # edit on it, don't silently clobber it -- honor the store's policy.
            if policy != ConflictPolicy.OVERWRITE and self._stamp is not None:
                current = layout.blob_stamp(self._key, self._field, self.id)
                if current is not None and current != self._stamp:
                    if policy == ConflictPolicy.ERROR:
                        raise StaleWriteError(
                            key=self._key,
                            field=self._field,
                            collection=getattr(self._store, "name", None),
                        )
                    if policy == ConflictPolicy.RELOAD:
                        # disk wins: drop our pending bytes and re-point at it.
                        self._data = None
                        self._pending = False
                        self._stamp = current
                        return
            layout.write_blob(self._key, self._field, self.id, self._data)
            self._pending = False
            self._stamp = layout.blob_stamp(self._key, self._field, self.id)
            if self.cache != CacheMode.REFERENCE:
                # don't retain bytes we were told not to cache.
                self._data = None

    # -- (de)serialization -----------------------------------------------------

    def __repr__(self) -> str:
        return (
            f"BlobRef(id={self.id!r}, content_type={self.content_type!r}, "
            f"size={self.size})"
        )

    def __to_descriptor__(self) -> "dict[str, Any]":
        """The compact, JSON-native descriptor stored inside the record."""
        return {
            "__blob__": True,
            "id": self.id,
            "content_type": self.content_type,
            "size": self.size,
            "cache": self.cache,
        }

    @classmethod
    def __from_value__(cls, v: Any) -> "BlobRef":
        """Build a BlobRef from a descriptor dict (or pass one through unchanged)."""
        if isinstance(v, BlobRef):
            return v
        if isinstance(v, dict):
            return cls(
                id=v.get("id"),
                content_type=v.get("content_type"),
                size=v.get("size", 0),
                cache=v.get("cache", CacheMode.REFERENCE),
            )
        raise ValueError(f"cannot interpret {v!r} as a BlobRef")

    @classmethod
    def __get_pydantic_core_schema__(cls, source: Any, handler: Any) -> Any:
        # lets a BlobRef be a field in a Pydantic model without BlobRef itself
        # being a Pydantic model: validate a descriptor-dict/BlobRef into a BlobRef
        # and serialize a BlobRef back to its descriptor. Imported lazily so
        # importing slowstore does not require pydantic until it's actually used.
        from pydantic_core import core_schema as cs

        return cs.no_info_plain_validator_function(
            cls.__from_value__,
            serialization=cs.plain_serializer_function_ser_schema(
                lambda v: v.__to_descriptor__(),
                return_schema=cs.dict_schema(
                    keys_schema=cs.str_schema(), values_schema=cs.any_schema()
                ),
            ),
        )


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
