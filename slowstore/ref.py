"""Lazy, generic references to records in other collections.

A :class:`Ref` points at a record in another store by ``(collection, key)`` and
is resolved on demand through the shared :class:`~slowstore.registry.Registry`
the owning store was registered with.  ``cache`` controls whether the resolved
record is remembered or re-resolved on every access.

Two ways to use it:

* **Ergonomic / autocompleting** -- declare the field as ``Reference[Target]``.
  The field is *typed* as ``Target`` (so editors autocomplete its attributes)
  while at runtime it holds a ``Ref`` that transparently forwards attribute
  access to the resolved record::

      class Book(BaseModel):
          reader: Reference[Person]

      book.reader = Ref.to(some_person)   # type-checks (Ref.to -> Target)
      book.reader.name                    # forwards to the resolved Person

* **Explicit** -- declare the field as ``Ref`` (or ``Optional[Ref]``) and call
  ``.get()`` / ``.value`` yourself when you want the raw record or a ``None``
  check.

Serialized shape (stored inside the record JSON)::

    {"__ref__": true, "collection": "people", "key": "ada", "cache": "reference"}

Runtime context (which store owns the ref) is not serialized; the owning
:class:`~slowstore.proxy.Proxy` injects it via :meth:`_bind`.

Note: because attribute access forwards to the target, ``Ref``'s own members
(``collection``, ``key``, ``cache``, ``get``, ``value``, ``to``) shadow any
same-named attribute on the referenced record -- reach those via ``.get()``.
"""

from __future__ import annotations

from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    Generic,
    TypeVar,
    cast,
    get_args,
    get_origin,
)

from pydantic import GetCoreSchemaHandler
from pydantic_core import core_schema as cs

from .blob import CacheMode

if TYPE_CHECKING:
    from .proxy import Proxy

T = TypeVar("T")


def _is_record(value: Any) -> bool:
    """A stored record is a proxy: it exposes its owning ``store`` and ``__key__``."""
    return hasattr(value, "store") and hasattr(value, "__key__")


class Ref(Generic[T]):
    """A lazily-resolved pointer to a record in another collection."""

    def __init__(self, collection: str, key: str, cache: str = CacheMode.REFERENCE):
        self.collection: str = collection
        self.key: str = key
        self.cache: str = cache
        # runtime-only binding; never serialized.
        self._proxy: "Proxy[Any] | None" = None
        self._store: Any = None
        self._resolved: Any = None

    # -- construction ----------------------------------------------------------

    @classmethod
    def to(cls, target: T, cache: str = CacheMode.REFERENCE) -> T:
        """Build a ref from a target record.

        Typed as returning ``T`` so it can be assigned to a ``Reference[T]``
        field; at runtime it returns a ``Ref`` that behaves like ``T``.
        """
        rec = cast(Any, target)
        ref = cls(rec.store.name, rec.__key__, cache)
        return cast(T, ref)

    # -- binding (called by the Proxy) -----------------------------------------

    def _bind(self, proxy: "Proxy[Any]", field: str) -> None:
        self._proxy = proxy
        self._store = proxy.store
        # a freshly bound ref may point somewhere new; drop any stale cache.
        self._resolved = None

    # -- resolution ------------------------------------------------------------

    def get(self) -> T | None:
        """Resolve the referenced record, or ``None`` if it is missing."""
        if self._resolved is not None and self.cache == CacheMode.REFERENCE:
            return cast("T | None", self._resolved)
        if self._store is None:
            raise RuntimeError("Ref is not bound to a store")
        registry = getattr(self._store, "registry", None)
        if registry is None:
            raise RuntimeError(
                "Ref resolution requires the owning store to have a registry"
            )
        resolved = registry.resolve(self.collection, self.key)
        if self.cache == CacheMode.REFERENCE:
            self._resolved = resolved
        return cast("T | None", resolved)

    @property
    def value(self) -> T | None:
        return self.get()

    # -- transparent forwarding ------------------------------------------------

    def __getattr__(self, name: str) -> Any:
        # only reached when ``name`` is not a real attribute of the Ref itself,
        # so forward it to the resolved record. Underscore/dunder names are left
        # to fail normally (protects pickling, copy, and binding bootstrap).
        if name.startswith("_"):
            raise AttributeError(name)
        target = self.get()
        if target is None:
            raise AttributeError(
                f"reference {self.collection}/{self.key} could not be resolved"
            )
        return getattr(target, name)

    # -- value semantics -------------------------------------------------------

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Ref):
            return (self.collection, self.key, self.cache) == (
                other.collection,
                other.key,
                other.cache,
            )
        return NotImplemented

    def __hash__(self) -> int:
        return hash((self.collection, self.key, self.cache))

    def __repr__(self) -> str:
        return f"Ref(collection={self.collection!r}, key={self.key!r})"

    # -- (de)serialization -----------------------------------------------------

    def __to_descriptor__(self) -> dict[str, Any]:
        return {
            "__ref__": True,
            "collection": self.collection,
            "key": self.key,
            "cache": self.cache,
        }

    @classmethod
    def __from_value__(cls, v: Any) -> "Ref[Any]":
        if isinstance(v, Ref):
            return v
        if isinstance(v, dict):
            return cls(
                collection=v["collection"],
                key=v["key"],
                cache=v.get("cache", CacheMode.REFERENCE),
            )
        if _is_record(v):
            # a stored record was passed directly (e.g. Article(author=some_person))
            return cast("Ref[Any]", cls.to(v))
        raise ValueError(f"cannot interpret {v!r} as a Ref")

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source: Any, handler: GetCoreSchemaHandler
    ) -> cs.CoreSchema:
        # validate any Ref/descriptor-dict into a Ref instance and serialize a
        # Ref back to its compact descriptor.
        return cs.no_info_plain_validator_function(
            cls.__from_value__,
            serialization=cs.plain_serializer_function_ser_schema(
                lambda v: v.__to_descriptor__(),
                return_schema=cs.dict_schema(
                    keys_schema=cs.str_schema(), values_schema=cs.any_schema()
                ),
            ),
        )


class _ReferenceMeta:
    """Annotated metadata that makes ``Reference[T]`` persist as a ``Ref``.

    The field's *static* type stays ``T`` (for autocomplete), but Pydantic uses
    the ``Ref`` validation/serialization instead of ``T``'s.
    """

    def __get_pydantic_core_schema__(
        self, source: Any, handler: GetCoreSchemaHandler
    ) -> cs.CoreSchema:
        return Ref.__get_pydantic_core_schema__(source, handler)


# A field annotated ``Reference[Target]`` type-checks as ``Target`` while storing
# a ``Ref`` at runtime. Used for the ergonomic, autocompleting API.
Reference = Annotated[T, _ReferenceMeta()]


_LIST_ORIGINS = (list, tuple, set, frozenset)


def _annotation_is_reference(annotation: Any) -> bool:
    if annotation is Ref:
        return True
    if any(
        isinstance(m, _ReferenceMeta)
        for m in getattr(annotation, "__metadata__", ())
    ):
        return True
    return any(
        arg is not type(None) and _annotation_is_reference(arg)
        for arg in get_args(annotation)
    )


def _annotation_is_reference_list(annotation: Any) -> bool:
    origin = get_origin(annotation)
    if origin in _LIST_ORIGINS:
        return any(_annotation_is_reference(arg) for arg in get_args(annotation))
    if origin is not None:
        # unwrap Optional/Union around a list (e.g. ``Optional[list[Ref]]``)
        return any(
            arg is not type(None) and _annotation_is_reference_list(arg)
            for arg in get_args(annotation)
        )
    return False


def field_is_reference(field_info: Any) -> bool:
    """Whether a Pydantic ``FieldInfo`` declares a reference field.

    Recognizes ``Ref``, ``Optional[Ref]``, ``Reference[T]`` (whose marker Pydantic
    hoists into ``metadata``), ``Optional[Reference[T]]`` (marker nested in the
    union arm) and their list forms (``list[Ref]`` / ``list[Reference[T]]``).
    """
    if any(isinstance(m, _ReferenceMeta) for m in getattr(field_info, "metadata", ())):
        return True
    return _annotation_is_reference(getattr(field_info, "annotation", None))


def field_is_reference_list(field_info: Any) -> bool:
    """Whether a field declares a *list* of references (``list[Reference[T]]``).

    A list-ref field is also a reference field (:func:`field_is_reference` returns
    True), so callers check this to choose between scalar and per-element handling.
    """
    return _annotation_is_reference_list(getattr(field_info, "annotation", None))
