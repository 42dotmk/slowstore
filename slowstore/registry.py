"""A shared catalog of stores so references can be resolved and joined.

Every :class:`~slowstore.store.Store` that participates in cross-collection
references registers itself into a ``Registry`` under its collection name.  A
:class:`~slowstore.ref.Ref` resolves through the registry, and the registry
provides a minimal query surface (:meth:`resolve`, :meth:`join`) across all
registered collections.
"""

from typing import TYPE_CHECKING, Any, Iterator

from .ref import Ref

if TYPE_CHECKING:
    from .store import Store


class Registry:
    def __init__(self) -> None:
        self.__stores__: dict[str, "Store[Any]"] = {}

    def register(self, store: "Store[Any]", name: str | None = None) -> str:
        """Register ``store`` under ``name`` (defaults to the store's name)."""
        name = name or store.name
        self.__stores__[name] = store
        return name

    def get_store(self, name: str) -> "Store[Any] | None":
        return self.__stores__.get(name)

    def collections(self) -> list[str]:
        return list(self.__stores__.keys())

    def resolve(self, collection: str, key: str) -> Any:
        """Look up a single record by ``(collection, key)`` or ``None``."""
        store = self.__stores__.get(collection)
        if store is None:
            return None
        return store.get(key)

    def join(self, collection: str, field: str) -> Iterator[tuple[Any, Any]]:
        """Yield ``(record, referenced_record)`` for each record in ``collection``.

        ``field`` names a :class:`Ref` on the records; unresolved refs yield a
        ``None`` partner rather than being skipped.
        """
        store = self.__stores__.get(collection)
        if store is None:
            return
        for record in store.values():
            ref = getattr(record, field, None)
            if isinstance(ref, Ref):
                yield (record, ref.get())


# Process-wide registry that stores fall back to when none is passed explicitly,
# so references resolve out of the box without any wiring.
_DEFAULT_REGISTRY = Registry()


def default_registry() -> Registry:
    """The shared registry used by stores created without an explicit one."""
    return _DEFAULT_REGISTRY
