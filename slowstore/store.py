import json
from logging import getLogger as get_logger
import os
import shutil
from pydantic import BaseModel

from .proxy import Proxy, Change, ChangeKind
from .utils import json_default_serializer, ensure_loaded

from typing import Any, Callable, Generic, List, Literal, TypeVar, cast, Sized

T = TypeVar("T")

logger = get_logger("SLOWSTORE")



class Store(Sized, Generic[T]):

    def __init__(self, cls: type, directory: str, **kwargs):
        """Creates a new Slowstore instance"""

        self.directory: str = directory
        self.cls: type = cls
        self.save_on_change: bool = kwargs.get("save_on_change", True)
        self.save_on_exit: bool = kwargs.get("save_on_exit", True)
        self.load_changes_from_file: bool = kwargs.get("load_changes_from_file", False)
        self.save_changes_to_file: bool = kwargs.get("save_changes_to_file", True)
        self.key_selector: Callable[["Store[T]", T], str] | None = None

        self.encoding: str = kwargs.get("encoding", "utf-8")
        self.ensure_ascii: bool = kwargs.get("ensure_ascii", False)
        self.change_hooks: List[Callable[[Proxy[T], List[Change]], None]] = []

        self.key_selector: Callable[["Store[T]", T], str] | None = kwargs.get(
            "key_selector", None
        )
        self.loaded = False

        if kwargs.get("load_on_start", True):
            self.load()

    @ensure_loaded
    def get(self, key) -> T | None:
        """gets an object from the store wrapped in a proxy but still casted as the model"""
        return cast(T, self.__data__.get(key))

    @ensure_loaded
    def get_model(self, key) -> T | None:
        """gets an object from the store but only the raw model"""
        res = self.__data__.get(key)
        if res:
            return res.model
        return None

    @ensure_loaded
    def get_proxy(self, key) -> T | None:
        """gets an object from the store as a proxy"""
        return self.__data__.get(key)

    @ensure_loaded
    def set(self, value: T, skip_autosave: bool = False, skip_notify: bool = False):
        """same as upsert but it generates the key using the key_selector function"""
        key = self.key_for(value)
        return self.upsert(key, value, skip_autosave, skip_notify)

    @ensure_loaded
    def insert(self, key:str, value:T, skip_autosave: bool = False,  skip_notify: bool = False):
        if key in self.__data__:
            raise ValueError(f"Key {key} already exists in the store")
        proxy = Proxy[T](self, key, value)
        proxy.is_dirty = True
        self.__data__[key] = proxy
        change = Change(kind=ChangeKind.ADD, key=key, model=value)

        if self.save_on_change and not skip_autosave:
            self.commit(proxy)

        if not skip_notify:
            self.notify_changes(proxy, [change])

        return cast(T, proxy)

    def update(self, key:str, value:T, skip_autosave: bool = False,  skip_notify: bool = False):
        if key not in self.__data__:
            raise ValueError(f"Key {key} does not exist in the store")

        proxy = self.__data__[key]
        if proxy.model == value:
            return cast(T, proxy)
        change_dict = self.__get_change_dict__(value)
        changes = []
        for prop_name in change_dict:
            if not hasattr(proxy.model, prop_name):
                raise ValueError(
                    f"Property {prop_name} does not exist in model"
                )
            prev_val = getattr(proxy.model, prop_name)
            new_val = change_dict[prop_name]
            if prev_val != new_val:
                change = proxy.__update_model_prop__(
                    prop_name,
                    new_val,
                    skip_auto_save=skip_autosave,
                    notify_changes=False,
                )
                if change is not None:
                    changes.append(change)
        if skip_notify and changes:
            self.notify_changes(proxy, changes)
        return cast(T, proxy)

    @ensure_loaded
    def upsert(self, key: str, value: T, skip_autosave=False, skip_notify=False) -> T:
        """sets a new object in the store and returns it's proxy"""
        if key in self.__data__:
            return self.update(key, value, skip_autosave=skip_autosave, skip_notify=skip_notify)
        else:
            return self.insert(key, value, skip_autosave=skip_autosave, skip_notify=skip_notify)
           


    def __get_change_dict__(self, change_obj: Any):
        if isinstance(change_obj, Proxy):
            return change_obj.model.__dict__
        if hasattr(change_obj, "__dict__"):
            return change_obj.__dict__
        elif isinstance(change_obj, dict):
            return change_obj
        else:
            raise ValueError("Value must be a proxy, dict or an object")

    @ensure_loaded
    def add_range(
        self,
        values: List[T],
        key_selector: Callable[["Store", T], str] | None = None,
        skip_autosave_for_each: bool = True,
        skip_autosave: bool = False,
    ):
        proxies = []
        for value in values:
            key = self.key_for(value, key_selector)
            proxy = self.upsert(
                key, value, skip_autosave=skip_autosave_for_each, skip_notify=False
            )
            proxies.append(proxy)

        if self.save_on_change and not skip_autosave:
            self.commit(*proxies)

    @ensure_loaded
    def delete(self, key: str) -> bool:
        if key in self.__data__:
            proxy = self.__data__[key]
            change = Change(kind=ChangeKind.DELETE, key=key, model=proxy.model)

            del self.__data__[key]

            os.remove(f"{self.directory}/{self.__sanitize_file_name__(key)}.json")
            proxy.__changes__.insert(0, change)
            proxy.is_dirty = True
            self.notify_changes(proxy, [change])
            return True
        return False

    @ensure_loaded
    def filter(self, filter: Callable[[T], bool]):
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
                with open(
                    f"{self.directory}/{self.__sanitize_file_name__(item.__key__)}.json",
                    "w",
                    encoding=self.encoding,
                ) as f:
                    d = item.model.__dict__
                    if isinstance(item.model, BaseModel):
                        d = cast(BaseModel, item.model).model_dump()
                    d["__key__"] = item.__key__
                    if self.save_changes_to_file:
                        d["__changes__"] = [x.__dict__ for x in item.__changes__]
                    f.write(
                        json.dumps(
                            d,
                            indent=2,
                            default=json_default_serializer,
                            ensure_ascii=self.ensure_ascii,
                        )
                    )
                item.is_dirty = False

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
        self.__json_load__()
        return self

    def __json_load__(self):
        if not os.path.exists(self.directory):
            os.makedirs(self.directory, exist_ok=True)
        self.__data__ = {}
        self.__changes__ = []
        self.loaded = False
        for filename in os.listdir(self.directory):
            with open(f"{self.directory}/{filename}", "r", encoding=self.encoding) as f:
                try:
                    d = json.load(f)
                    key: str = d["__key__"]
                    change_dicts = d.get("__changes__", [])

                    del d["__key__"]
                    if "__changes__" in d:
                        del d["__changes__"]

                    proxy = Proxy[T](store=self, key=key, model=self.cls(**d))

                    if self.load_changes_from_file:
                        proxy.__changes__ = [Change(**x) for x in change_dicts]

                    self.__data__[key] = proxy
                except Exception as e:
                    logger.error(f"Error loading {filename}: {e}")
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
        key_or_selector: Callable[["Store", T], str] | str | None = None,
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

    def add_change_hook(self, hook: Callable[[Proxy[T], List[Change]], None]):
        self.change_hooks.append(hook)

    def remove_change_hook(self, hook: Callable[[Proxy[T], List[Change]], None]):
        self.change_hooks.remove(hook)

    def clear_change_hooks(self):
        self.change_hooks = []

    def notify_changes(self, proxy: Proxy[T], changes: List[Change]):
        logger.debug(f"Notifying change to {len(self.change_hooks)} hooks")
        if len(self.change_hooks) == 0 or changes is None or len(changes) == 0:
            return

        for hook in self.change_hooks:
            if hook is not None:
                hook(proxy, changes)

    def __sanitize_file_name__(self, name: str | int):
        name = str(name)

        return (
            name.replace("/", "_")
            .replace("\\", "_")
            .replace(":", "_")
            .replace(" ", "_")
            .replace(".", "_")
            .replace("!", "_")
            .replace("?", "_")
            .replace("&", "_")
            .replace(";", "_")
            .replace("|", "_")
            .replace("/", "_")
            .lower()
        )

    def __enter__(self):
        return self

    def __exit__(self, exc_type, *_):
        if exc_type is None and self.save_on_exit:
            self.commit_all()
            self.__data__ = {}
            self.loaded = False
        else:
            self.__data__ = {}
            self.loaded = False
        return False


