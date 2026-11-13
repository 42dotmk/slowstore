from logging import getLogger as get_logger
import sys
import slowstore
from typing import Any, Generic, TypeVar, override
from .change import Change, ChangeKind
from .blob import BlobRef
from .ref import Ref, _is_record

logger = get_logger("SLOWSTORE")

T = TypeVar("T")

__special_fields__ = frozenset([
    "store",
    "model",
    "is_dirty",
    "commit",
    "__key__",
    "__changes__",
    "__add_change__",
    "__update_prop__",
    "__reset__",
    "__repr__",
    "__setattr__",
    "__getattr__",
    "__orig_class__",
    "__bind_special_fields__",
    "__blob_fields__",
    "__ref_field_names__",
    "__blob_field_names__",
    "__ref_list_field_names__",
    "__blob_list_field_names__",
    "__blob_list_ids__",
    "__coerce_special__",
    "__coerce_blob_item__",
    "__bind_blob_list__",
    "__assign_blob_list_ids__",
])

# Model systems whose own methods must run on the real model, not the proxy.
_MODEL_SYSTEM_MODULES = ("pydantic.", "pydantic_core.")


class Proxy(Generic[T]):

    def __init__(self, store: "slowstore.Store[T]", key: str, model: T):
        self.store: "slowstore.Store[T]" = store
        self.model: T = model
        self.is_dirty: bool = False

        self.__key__: str = key
        self.__changes__: list[Change[T]] = []
        self.__blob_fields__: list[str] = []
        self.__ref_field_names__: set[str] = set()
        self.__blob_field_names__: set[str] = set()
        self.__ref_list_field_names__: set[str] = set()
        self.__blob_list_field_names__: set[str] = set()
        # last-persisted blob ids per list-blob field, so a shrunk/reordered list
        # can delete the sidecars of elements that are no longer present.
        self.__blob_list_ids__: dict[str, set[str]] = {}
        self.__bind_special_fields__()

    def __bind_special_fields__(self) -> None:
        """Give the model's BlobRef/Ref fields their I/O context.

        Runs for both freshly-inserted and loaded records (both go through
        ``__init__``), so lazy blobs/refs know which (store, key, field) they
        belong to. Also records which fields are *declared* as reference/blob
        types (from the model's annotations) so plain records/bytes/text can be
        auto-wrapped on assignment even when the field is currently empty.
        """
        self.__blob_fields__ = []
        model = self.model

        # which fields are blob/ref is a property of the model *type*; the codec
        # knows how to read that off whatever model system is in use.
        special = self.store.codec.special_fields(type(model))
        self.__ref_field_names__ = set(special.ref)
        self.__blob_field_names__ = set(special.blob)
        self.__ref_list_field_names__ = set(special.ref_list)
        self.__blob_list_field_names__ = set(special.blob_list)

        if not hasattr(model, "__dict__"):
            return
        for name, value in list(vars(model).items()):
            if isinstance(value, BlobRef):
                value._bind(self, name)
                self.__blob_fields__.append(name)
            elif isinstance(value, Ref):
                value._bind(self, name)
            elif isinstance(value, list) and name in self.__blob_list_field_names__:
                self.__bind_blob_list__(name, value, snapshot=True)
                self.__blob_fields__.append(name)
            elif isinstance(value, list) and name in self.__ref_list_field_names__:
                for item in value:
                    if isinstance(item, Ref):
                        item._bind(self, name)

    def __assign_blob_list_ids__(self, field: str, blobs: list[Any]) -> None:
        """Give every un-ided ``BlobRef`` in a list a stable, unique id.

        Loaded blobs already carry their id in the descriptor; only freshly
        wrapped ones (id is ``None``) need one. Ids are small integers as
        strings, chosen to avoid colliding with ids already used in the list so
        prepending/appending a new blob never overwrites an existing sidecar.
        """
        used = {
            b.id for b in blobs if isinstance(b, BlobRef) and b.id is not None
        }
        counter = 0
        for b in blobs:
            if isinstance(b, BlobRef) and b.id is None:
                while str(counter) in used:
                    counter += 1
                b.id = str(counter)
                used.add(b.id)

    def __bind_blob_list__(
        self, field: str, blobs: list[Any], snapshot: bool = False
    ) -> None:
        """Assign ids to and bind every ``BlobRef`` in a list-blob field.

        ``snapshot`` records the current set of ids as the last-persisted state;
        it is set only on the initial bind (load/insert) — on reassignment the
        snapshot is left for :meth:`Store.commit` to reconcile so removed blobs'
        sidecars get cleaned up.
        """
        self.__assign_blob_list_ids__(field, blobs)
        for b in blobs:
            if isinstance(b, BlobRef):
                b._bind(self, field)
        if snapshot:
            self.__blob_list_ids__[field] = {
                b.id for b in blobs if isinstance(b, BlobRef) and b.id is not None
            }

    def __coerce_special__(self, name: str, value: Any) -> Any:
        """Auto-wrap a plain assignment into a Ref/BlobRef for special fields.

        ``doc.author = some_record`` -> ``Ref`` to that record; ``doc.body = "x"``
        / ``doc.body = b"..."`` -> a ``BlobRef``. Values already of the right type
        (or ``None``) pass through untouched.
        """
        if value is None:
            return value
        if name in self.__ref_list_field_names__ and isinstance(value, list):
            # ``doc.authors = [person, ref, ...]`` -> wrap each plain record.
            # existing Refs pass through untouched (probing _is_record on a Ref
            # would forward attribute access to its unresolved target).
            return [
                v if isinstance(v, Ref) else (Ref.to(v) if _is_record(v) else v)
                for v in value
            ]
        if name in self.__blob_list_field_names__ and isinstance(value, list):
            # ``doc.attachments = [b"...", "text", blob]`` -> wrap each element.
            return [self.__coerce_blob_item__(v) for v in value]
        if name in self.__ref_field_names__ and not isinstance(value, Ref):
            if _is_record(value):
                return Ref.to(value)
        elif name in self.__blob_field_names__ and not isinstance(value, BlobRef):
            return self.__coerce_blob_item__(value)
        return value

    def __coerce_blob_item__(self, value: Any) -> Any:
        """Wrap a single plain ``str``/``bytes`` into a ``BlobRef`` (pass-through
        for ``None`` and values already a ``BlobRef``)."""
        if value is None or isinstance(value, BlobRef):
            return value
        if isinstance(value, str):
            return BlobRef.from_text(value)
        if isinstance(value, (bytes, bytearray)):
            return BlobRef.from_bytes(bytes(value))
        return value

    def __getattr__(self, name: str):
        # Hot path: every model attribute read through a proxy lands here, so it
        # does no logging and no string formatting.
        if name in __special_fields__:
            return super().__getattribute__(name)
        model = self.model
        attr = getattr(model, name)
        if not callable(attr):
            return attr
        func = getattr(attr, "__func__", None)
        if func is None or getattr(attr, "__self__", None) is not model:
            # callable field value, staticmethod or classmethod
            return attr
        if (getattr(func, "__module__", None) or "").startswith(_MODEL_SYSTEM_MODULES):
            # methods of the model system itself (pydantic's ``model_dump``,
            # ``model_copy``…) reach into the instance's internals, which the
            # proxy does not have: run them on the real model.
            return attr

        # the model's own instance methods get the proxy as 'self' so the
        # attribute writes they make are tracked as changes
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            return func(self, *args, **kwargs)

        wrapper.__name__ = getattr(func, "__name__", name)
        return wrapper

    @override
    def __setattr__(self, name: str, value: Any) -> None:
        if name in __special_fields__:
            super().__setattr__(name, value)
        else:
            _ = self.__update_model_prop__(name, value, False, True)

    def __update_model_prop__(
        self,
        name: str,
        value: Any,
        skip_auto_save: bool = False,
        notify_changes: bool = True,
    ):
        logger.debug("Setting model: %s=%s", name, value)
        # auto-wrap plain records/bytes/text assigned to reference/blob fields.
        value = self.__coerce_special__(name, value)
        prev = self.model.__getattribute__(name)
        if prev == value:
            return None

        setattr(self.model, name, value)
        self.is_dirty = True

        # a newly-assigned blob/ref needs its I/O context too.
        if isinstance(value, BlobRef):
            value._bind(self, name)
            if name not in self.__blob_fields__:
                self.__blob_fields__.append(name)
        elif isinstance(value, Ref):
            value._bind(self, name)
        elif isinstance(value, list) and name in self.__blob_list_field_names__:
            # snapshot=False: let commit reconcile old vs new ids (sidecar cleanup).
            self.__bind_blob_list__(name, value)
            if name not in self.__blob_fields__:
                self.__blob_fields__.append(name)
        elif isinstance(value, list) and name in self.__ref_list_field_names__:
            for item in value:
                if isinstance(item, Ref):
                    item._bind(self, name)

        change = self.__add_change__(name, prev, value, notify_changes=notify_changes)

        if self.store.save_on_change and not skip_auto_save:
            self.commit()

        return change

    def commit(self):
        self.store.commit(self)

    def __add_change__(
        self, prop_name: str, prev_val: Any, new_val: Any, notify_changes: bool = True
    ) -> Change[T]:
        logger.debug("Adding change: %s=%s -> %s", prop_name, prev_val, new_val)
        change = Change[T](
            kind=ChangeKind.UPDATE,
            key=self.__key__,
            prop_name=prop_name,
            prev_val=prev_val,
            new_val=new_val,
            actor=self.store.__identity__(),
        )
        self.store.__changes__.insert(0, change)
        self.__changes__.insert(0, change)

        if notify_changes:
            self.store.notify_changes(self, [change])
        return change

    def __reset__(self, count: int = sys.maxsize):
        size = len(self.__changes__)
        counter = 0
        while size > 0 and counter < count:
            change = self.__changes__.pop(0)
            counter += 1
            size -= 1
            change.undo(self)
        if count > 0 and self.store.save_on_change:
            self.commit()

    @override
    def __repr__(self)->str:
        return "ModelProxy(" + self.model.__repr__() + ")"
