import os
import shutil
from logging import getLogger as get_logger
from typing import Any, Callable, Generic, Literal, Sized, TypeVar, cast

from pydantic import BaseModel

from .blob import BlobRef
from .layout import FilePerRecordLayout, StorageLayout
from .registry import Registry, default_registry
from .proxy import (
    Change,  # pyright: ignore[reportPrivateLocalImportUsage]
    ChangeKind,  # pyright: ignore[reportPrivateLocalImportUsage]
    Proxy,
)
from .utils import ensure_loaded

T = TypeVar("T")

logger = get_logger("SLOWSTORE")


class Store(Sized, Generic[T]):

    def __init__(self, cls: type, directory: str, **kwargs: dict[str, Any]):
        """Creates a new Slowstore instance"""

        self.directory: str = directory
        self.cls: type = cls
        self.save_on_change: bool = cast(bool, kwargs.get("save_on_change", True))
        self.save_on_exit: bool = cast(bool, kwargs.get("save_on_exit", True))
        self.load_changes_from_file: bool = cast(
            bool, kwargs.get("load_changes_from_file", False)
        )
        self.save_changes_to_file: bool = cast(
            bool, kwargs.get("save_changes_to_file", True)
        )
        self.encoding: str = cast(str, kwargs.get("encoding", "utf-8"))
        self.ensure_ascii: bool = cast(bool, kwargs.get("ensure_ascii", False))
        self.change_hooks: list[Callable[[Proxy[T], list[Change[T]]], None]] = []

        # Optional identity provider: when set, each change records the actor it
        # returns (e.g. the current request's user). Left None = anonymous changes,
        # exactly as before.
        self.get_identity: Callable[[], Any] | None = cast(
            Callable[[], Any] | None, kwargs.get("get_identity", None)
        )

        self.key_selector: Callable[["Store[T]", T], str] | None = cast(
            Callable[["Store[T]", T], str] | None, kwargs.get("key_selector", None)
        )
        # Pluggable on-disk layout. Defaults to the historical one-file-per-record
        # scheme; ``bind`` reads directory/encoding/ensure_ascii off this store, so
        # it must run after those are set.
        self.layout: StorageLayout = cast(
            StorageLayout, kwargs.get("layout", None) or FilePerRecordLayout()
        )
        self.layout.bind(self)

        # Collection name (used by cross-collection references). Defaults to the
        # directory's basename so refs read naturally (e.g. "employees").
        self.name: str = cast(
            str, kwargs.get("name", None) or os.path.basename(os.path.normpath(directory))
        )
        # Shared registry for resolving/joining references across stores. When
        # none is given we fall back to a process-wide default so references
        # always resolve without extra wiring.
        self.registry: Registry = cast(
            Registry, kwargs.get("registry", None) or default_registry()
        )
        self.registry.register(self, self.name)

        self.load_errors: list[tuple[str, Exception]] = []
        self.loaded: bool = False
        if kwargs.get("load_on_start", False):
            _ = self.load()

    @ensure_loaded
    def get(self, key: str) -> T | None:
        """gets an object from the store wrapped in a proxy but still casted as the model"""
        return cast(T, self.__data__.get(key))

    @ensure_loaded
    def get_model(self, key: str) -> T | None:
        """gets an object from the store but only the raw model"""
        if isinstance(key, Proxy):
            key = key.__key__
        res = self.__data__.get(key)
        if res:
            return res.model
        return None

    @ensure_loaded
    def get_proxy(self, key: str) -> T | None:
        """gets an object from the store as a proxy"""
        return self.__data__.get(key)

    @ensure_loaded
    def set(self, value: T, skip_autosave: bool = False, skip_notify: bool = False):
        """same as upsert but it generates the key using the key_selector function"""
        key = self.key_for(value)
        return self.upsert(key, value, skip_autosave, skip_notify)

    @ensure_loaded
    def insert(
        self, key: str, value: T, skip_autosave: bool = False, skip_notify: bool = False
    ):
        if key in self.__data__:
            raise ValueError(f"Key {key} already exists in the store")
        proxy = Proxy[T](self, key, value)
        proxy.is_dirty = True
        self.__data__[key] = proxy
        change = Change[T](kind=ChangeKind.ADD, key=key, model=value,
                           actor=self.__identity__())
        # record the creation in the audit trail (mirrors UPDATE/DELETE) so the
        # record's first change attributes who created it and when.
        self.__changes__.insert(0, change)
        proxy.__changes__.insert(0, change)

        if self.save_on_change and not skip_autosave:
            self.commit(proxy)

        if not skip_notify:
            self.notify_changes(proxy, [change])

        return cast(T, proxy)

    def update(
        self, key: str, value: T, skip_autosave: bool = False, skip_notify: bool = False
    ):
        if key not in self.__data__:
            raise ValueError(f"Key {key} does not exist in the store")

        proxy = self.__data__[key]
        if proxy.model == value:
            return cast(T, proxy)
        change_dict = self.__get_change_dict__(value)
        changes: list[Change[T]] = []
        for prop_name in change_dict:
            if not hasattr(proxy.model, prop_name):
                raise ValueError(f"Property {prop_name} does not exist in model")
            prev_val = getattr(proxy.model, prop_name)
            new_val = change_dict[prop_name]
            if prev_val != new_val:
                change = cast(
                    Change[T] | None,
                    proxy.__update_model_prop__(
                        prop_name,
                        new_val,
                        skip_auto_save=skip_autosave,
                        notify_changes=False,
                    ),
                )
                if change is not None:
                    changes.append(change)
        if skip_notify and changes:
            self.notify_changes(proxy, changes)
        return cast(T, proxy)

    @ensure_loaded
    def upsert(
        self, key: str, value: T, skip_autosave: bool = False, skip_notify: bool = False
    ) -> T:
        """sets a new object in the store and returns it's proxy"""
        if key in self.__data__:
            return self.update(
                key, value, skip_autosave=skip_autosave, skip_notify=skip_notify
            )
        else:
            return self.insert(
                key, value, skip_autosave=skip_autosave, skip_notify=skip_notify
            )

    def __get_change_dict__(
        self, change_obj: Proxy[T] | dict[str, Any] | Any
    ) -> dict[str, Any]:
        if isinstance(change_obj, Proxy):
            return cast(T, change_obj.model).__dict__
        if hasattr(change_obj, "__dict__"):
            return change_obj.__dict__
        elif isinstance(change_obj, dict):
            return cast(dict[str, Any], change_obj)
        else:
            raise ValueError("Value must be a proxy, dict or an object")

    @ensure_loaded
    def add_range(
        self,
        values: list[T],
        key_selector: Callable[["Store[T]", T], str] | None = None,
        skip_autosave_for_each: bool = True,
        skip_autosave: bool = False,
    ):
        proxies: list[Proxy[T]]= []
        for value in values:
            key = self.key_for(value, key_selector)
            proxy = cast(Proxy[T], self.upsert(
                key, value, skip_autosave=skip_autosave_for_each, skip_notify=False
            ))
            proxies.append(proxy)

        if self.save_on_change and not skip_autosave:
            self.commit(*proxies)

    @ensure_loaded
    def delete(self, key: str) -> bool:
        if key in self.__data__:
            proxy = self.__data__[key]
            change = Change[T](kind=ChangeKind.DELETE, key=key, model=proxy.model,
                               actor=self.__identity__())

            del self.__data__[key]

            self.layout.delete_record(key)
            # cascade: remove the record's blob sidecars too (scalar or list).
            for fname in proxy.__blob_fields__:
                val = getattr(proxy.model, fname, None)
                blobs = val if isinstance(val, list) else [val]
                for blob in blobs:
                    if isinstance(blob, BlobRef):
                        self.layout.delete_blob(key, fname, blob.id or fname)
            proxy.__changes__.insert(0, change)
            proxy.is_dirty = True
            self.notify_changes(proxy, [change])
            return True
        return False

    @ensure_loaded
    def filter(self, filter: Callable[[Proxy[T]], bool]):
        """yield all models that satisfy the filter function"""
        for proxy in self.values():
            if filter(cast(T, proxy)):
                yield cast(T, proxy)

    def as_proxy(self, item: T) -> Proxy[T]:
        if isinstance(item, Proxy):
            return cast(Proxy[T], item)
        raise ValueError("Item is not a proxy")

    def as_model(self, item: Proxy[T]):
        return cast(T, item)

    @ensure_loaded
    def first(
        self,
        filter: Callable[[T], bool | Literal[True] | Literal[False]] | None = None,
    ) -> T | None:
        """return the model that satisfy the filter function otherwise return None"""
        for proxy in self.values():
            if filter is None or filter(proxy):
                return proxy
        return None

    @ensure_loaded
    def update_where(
        self,
        filter: Callable[[T], bool],
        updater: Callable[[T], None],
    ):
        for proxy in self.values():
            if filter(proxy):
                updater(proxy)

    @ensure_loaded
    def values(self):
        for x in self.__data__:
            yield cast(T, self.get(x))

    @ensure_loaded
    def raw_values(self):
        for x in self.__data__:
            yield self.__data__[x].model

    @ensure_loaded
    def keys(self):
        return self.__data__.keys()

    @ensure_loaded
    def commit_all(self):
        for proxy in self.values():
            self.commit(cast(Proxy[T], proxy))

    @ensure_loaded
    def commit(self, *items: Proxy[T]):
        for item in items:
            if item.store != self:
                raise ValueError("Item does not belong to this store")
            if item.is_dirty:
                # flush any blob bytes to their sidecars before the record JSON
                # (which only holds the blob descriptors) is written. A list-blob
                # field flushes each element and drops sidecars for elements that
                # were removed since the last commit.
                for fname in item.__blob_fields__:
                    val = getattr(item.model, fname, None)
                    blobs = val if isinstance(val, list) else [val]
                    for blob in blobs:
                        if isinstance(blob, BlobRef):
                            blob._flush()
                    if fname in item.__blob_list_field_names__:
                        current_ids = {
                            b.id
                            for b in blobs
                            if isinstance(b, BlobRef) and b.id is not None
                        }
                        for stale in item.__blob_list_ids__.get(fname, set()) - current_ids:
                            self.layout.delete_blob(item.__key__, fname, stale)
                        item.__blob_list_ids__[fname] = current_ids
                d = item.model.__dict__
                if isinstance(item.model, BaseModel):
                    d = cast(BaseModel, item.model).model_dump(by_alias=True)
                d["__key__"] = item.__key__
                if self.save_changes_to_file:
                    # ``model`` is the record itself — omit it so ADD entries
                    # stay compact (the file already holds the model); keep
                    # kind/key/prop/val/date/actor.
                    d["__changes__"] = [
                        {k: v for k, v in x.__dict__.items() if k != "model"}
                        for x in item.__changes__
                    ]
                self.layout.write_record(item.__key__, d)
                item.is_dirty = False

    @ensure_loaded
    def __len__(self):
        return len(self.__data__)

    @ensure_loaded
    def __contains__(self, key: str | Proxy[T]) -> bool:
        if isinstance(key, Proxy):
            return key.__key__ in self.__data__

        return key in self.__data__

    @ensure_loaded
    def __iter__(self):
        return iter(self.values())

    @ensure_loaded
    def __getitem__(self, key: str):
        return cast(T, self.get(key))

    @ensure_loaded
    def __setitem__(self, key: str, value: T):
        return cast(T, self.upsert(key, value))

    @ensure_loaded
    def __delitem__(self, key: str):
        return self.delete(key)

    def load(self):
        return self.__json_load__()

    def __json_load__(self):
        if not os.path.exists(self.directory):
            os.makedirs(self.directory, exist_ok=True)
        self.__data__: dict[str, Any] = {}
        self.__changes__: list[Change[T]] = []
        self.loaded = False
        records, errors = self.layout.load_records()
        for locator, d in records.items():
            try:
                key: str = d["__key__"]
                change_dicts = d.get("__changes__", [])

                # build the model input without mutating ``d`` -- collection
                # layouts hand back dicts they keep cached, so removing keys from
                # ``d`` here would corrupt their in-memory copy.
                model_data = {
                    k: v for k, v in d.items() if k not in ("__key__", "__changes__")
                }

                proxy = Proxy[T](store=self, key=key, model=self.cls(**model_data))

                if self.load_changes_from_file:
                    # parse changes individually: a malformed entry is skipped
                    # rather than dropping the whole record from the store.
                    parsed: list[Change[T]] = []
                    for x in change_dicts:
                        try:
                            parsed.append(Change(**x))
                        except Exception:
                            pass
                    proxy.__changes__ = parsed

                self.__data__[key] = proxy
            except Exception as e:
                errors.append((locator, e))
                # logger.error(f"Error loading {locator}: {e}")
        self.load_errors = errors
        if len(errors) > 0:
            logger.error(
                f"loading {self.__class__.__name__} generated {len(errors)} errors, they are stored in load_errors list"
            )
        self.loaded = True
        return self

    def clear(self):
        if os.path.exists(self.directory):
            shutil.rmtree(self.directory, ignore_errors=False)
        self.__data__ = {}
        self.__changes__ = []
        self.loaded = False

    def key_for(
        self,
        value: T,
        key_or_selector: Callable[["Store[T]", T], str] | str | None = None,
    ) -> str:
        """
        Tries to create a key for the provided value by using the following order:
            1. if key_or_selector callable parameter is provided
            2. if store has key_selector set
            3. if the value has a __key__ field
            4. if the value has an id field
            5. raise ValueError
        """
        key = None
        if key_or_selector is not None:
            if callable(key_or_selector):
                key = key_or_selector(self, value)
            else:
                key = key_or_selector
        if self.key_selector is not None:
            key = cast(Any, self.key_selector)(value)
        elif value.__dict__.get("__key__") is not None:
            key = value.__dict__.get("__key__")
        elif value.__dict__.get("id") is not None:
            key = value.__dict__.get("id")
        if key is None:
            raise ValueError("Could not determine key for value")
        return key

    def __identity__(self) -> Any:
        """Current actor from the optional ``get_identity`` provider, or None. A
        failing provider never breaks a write."""
        if self.get_identity is None:
            return None
        try:
            return self.get_identity()
        except Exception:
            return None

    def add_change_hook(self, hook: Callable[[Proxy[T], list[Change[T]]], None]):
        self.change_hooks.append(hook)

    def remove_change_hook(self, hook: Callable[[Proxy[T], list[Change[T]]], None]):
        self.change_hooks.remove(hook)

    def clear_change_hooks(self):
        self.change_hooks = []

    def notify_changes(self, proxy: Proxy[T], changes: list[Change[T]] | None):
        logger.debug(f"Notifying change to {len(self.change_hooks)} hooks")
        if len(self.change_hooks) == 0 or changes is None or len(changes) == 0:
            return

        for hook in self.change_hooks:
            if hook is not None:
                hook(proxy, changes)

    def __sanitize_file_name__(self, name: str | int):
        # kept for backwards compatibility; the layout now owns key sanitization.
        return self.layout.sanitize(name)

    def __enter__(self):
        return self

    def __exit__(self, exc_type: type | None, *_):
        if exc_type is None and self.save_on_exit:
            self.commit_all()
            self.__data__ = {}
            self.loaded = False
        else:
            self.__data__ = {}
            self.loaded = False
        return False
