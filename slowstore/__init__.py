import json
from logging import getLogger as get_logger
import os
import shutil
import sys
import datetime
from pydantic import BaseModel
from functools import wraps

from typing import Any, Callable, Generic, List, Literal, TypeVar, cast, Sized

T = TypeVar("T")

logger = get_logger("SLOWSTORE")


def ensure_loaded(func):
    @wraps(func)
    def wrapper(self: "Slowstore", *args, **kwargs):
        if not self.loaded:
            self.load()
        return func(self, *args, **kwargs)

    return wrapper


class Change(Generic[T]):
    """A property change that can be undone or redone on a model"""

    # Not supporting this yet, since i dont need it
    transaction: str = ""

    def __init__(self, **kwargs):
        if "key" not in kwargs:
            raise ValueError("key is required")
        if "prop_name" not in kwargs:
            raise ValueError("prop_name is required")
        if "prev_val" not in kwargs:
            raise ValueError("prev_val is required")
        if "new_val" not in kwargs:
            raise ValueError("new_val is required")

        self.key: str = kwargs["key"]
        self.prop_name: str = kwargs["prop_name"]
        self.prev_val: Any = kwargs["prev_val"]
        self.new_val: Any = kwargs["new_val"]
        self.date: datetime.datetime = datetime.datetime.now()

    def undo(self, model: T):
        model.__setattr__(self.prop_name, self.prev_val)

    def redo(self, model: T):
        model.__setattr__(self.prop_name, self.new_val)


__special_fields__ = [
    "store",
    "model",
    "is_dirty",
    "commit",
    "__key__",
    "__changes__",
    "__add_change__",
    "__reset__",
]


class ModelProxy(Generic[T]):

    def __init__(self, store: "Slowstore[T]", key: str, model: T):
        self.store: "Slowstore[T]" = store
        self.model: T = model
        self.is_dirty: bool = False

        self.__key__: str = key
        self.__changes__: List[Change] = []

    def __getattr__(self, name):
        if name in __special_fields__:
            logger.debug(f"Getting proxy.{name}")
            return super().__getattribute__(name)
        else:
            logger.debug(f"Getting model.{name}")
            attr = getattr(self.model, name)
            if not callable(attr):
                return attr
            else:
                # when we have instance methods we need to send the proxy as 'self' so we can track changes
                func_name = attr.__name__
                func = attr.__func__

                def wrapper(*args, **kwargs):
                    return func(self, *args, **kwargs)

                wrapper.__name__ = func_name
                return wrapper

    def __setattr__(self, name, value):
        if name in __special_fields__:
            logger.debug(f"Setting proxy: {name}={value}")
            super().__setattr__(name, value)
        else:
            logger.debug(f"Setting model: {name}={value}")
            prev = self.model.__getattribute__(name)
            if prev == value:
                return
            setattr(self.model, name, value)
            self.__add_change__(name, prev, value)

            self.is_dirty = True

            if self.store.save_on_change:
                self.commit()

    def commit(self):
        self.store.commit(self)

    def __add_change__(self, prop_name: str, prev_val: Any, new_val: Any):
        logger.debug(f"Adding change: {prop_name}={prev_val} -> {new_val}")
        change = Change(
            key=self.__key__, prop_name=prop_name, prev_val=prev_val, new_val=new_val
        )
        self.store.__changes__.insert(0, change)
        self.__changes__.insert(0, change)

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

    def __repr__(self):
        return "ModelProxy(" + self.model.__repr__() + ")"


class Slowstore(Sized, Generic[T]):
    """A simple key-value store that persists data to disk,
    it uses a pydantic model as the value type
    :param cls: The class
    :type cls: type

    :param directory: The directory where the store will be stored
    :type directory: str

    :param load_on_start: If the store should load all data on start
    :type load_on_start: bool, Defaults to True

    :param save_on_change: If the store should save data on change
    :type save_on_change: bool, Defaults to False

    :param save_on_exit: If the store should save data on exit
    :type save_on_exit: bool, Defaults to True

    :param load_changes_from_file: If the store should load changes from file
    :type load_changes_from_file: bool, Defaults to False

    :param save_changes_to_file: If the store should save changes to file
    :type save_changes_to_file: bool, Defaults to True

    :param key_selector: A function that generates a key for a model
    :type key_selector: Callable[[Slowstore, T], str], Defaults to None

    :param encoding: The encoding to use when reading and writing files
    :type encoding: str, Defaults to "utf-8"

    :param ensure_ascii: If the store should ensure ascii when saving
    :type ensure_ascii: bool, Defaults to False

    """

    def __init__(self, cls: type, directory: str, **kwargs):
        """Creates a new Slowstore instance"""

        self.directory: str = directory
        self.cls: type = cls
        self.save_on_change: bool = kwargs.get("save_on_change", True)
        self.save_on_exit: bool = kwargs.get("save_on_exit", True)
        self.load_changes_from_file: bool = kwargs.get("load_changes_from_file", False)
        self.save_changes_to_file: bool = kwargs.get("save_changes_to_file", True)
        self.key_selector: Callable[["Slowstore[T]", T], str] | None = None

        self.encoding: str = kwargs.get("encoding", "utf-8")
        self.ensure_ascii: bool = kwargs.get("ensure_ascii", False)

        self.key_selector: Callable[["Slowstore[T]", T], str] | None = kwargs.get(
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
    def set(self, value: T, skip_autosave: bool = False):
        """same as upsert but it generates the key using the key_selector function"""
        key = self.key_for(value)
        return self.upsert(key, value, skip_autosave)

    def __len__(self):
        return len(self.__data__)

    @ensure_loaded
    def create(self, *args, **kwargs) -> T:
        """
        Creates a new object of the model type and adds it to the store
        Note: create can only be used if:
            - store has a key_selector function
            - model has a __key__
            - model has an id field
        """
        value = self.cls(*args, **kwargs)
        key = self.key_for(value)
        return self.upsert(key, value)

    @ensure_loaded
    def upsert(self, key: str, value: T, skip_autosave=False) -> T:
        """sets a new object in the store and returns it's proxy
        if we overwrite the key it will completely change the underlying model and this can cause data inconsistencies
        """
        if key in self.__data__:
            proxy = self.__data__[key]
            if proxy.model != value:
                self.__data__[key].model = value
                proxy.is_dirty = True

            if self.save_on_change and not skip_autosave:
                self.commit(proxy)

            return cast(T, proxy)

        proxy = ModelProxy[T](self, key, value)
        proxy.is_dirty = True

        if self.save_on_change:
            self.commit(proxy)

        self.__data__[key] = proxy
        return cast(T, proxy)

    @ensure_loaded
    def add_range(
        self,
        values: List[T],
        key_selector: Callable[["Slowstore", T], str] | None = None,
    ):
        proxies = []
        for value in values:
            key = self.key_for(value, key_selector)
            proxies.append(self.upsert(key, value, skip_autosave=True))
        if self.save_on_change:
            self.commit(*proxies)

    @ensure_loaded
    def delete(self, key: str) -> bool:
        if key in self.__data__:
            del self.__data__[key]
            os.remove(f"{self.directory}/{self.__sanitize_file_name__(key)}.json")
            return True
        return False

    @ensure_loaded
    def filter(self, filter: Callable[[T], bool]):
        """yield all models that satisfy the filter function"""
        for proxy in self.values():
            if filter(cast(T, proxy)):
                yield cast(T, proxy)

    def as_proxy(self, item: T) -> ModelProxy[T]:
        if isinstance(item, ModelProxy):
            return cast(ModelProxy[T], item)

    def as_model(self, item: ModelProxy[T] | None) -> T | None:
        if item is None:
            return None
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
    def update(
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
            self.commit(cast(ModelProxy[T], proxy))

    @ensure_loaded
    def commit(self, *items: ModelProxy[T]):
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
                            default=json_default,
                            ensure_ascii=self.ensure_ascii,
                        )
                    )
                item.is_dirty = False

    @ensure_loaded
    def __contains__(self, key: str | ModelProxy[T]) -> bool:
        if isinstance(key, ModelProxy):
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

                    proxy = ModelProxy[T](store=self, key=key, model=self.cls(**d))

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
        key_or_selector: Callable[["Slowstore", T], str] | str | None = None,
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


def json_default(o):
    if isinstance(o, (datetime.date, datetime.datetime)):
        return o.isoformat()
