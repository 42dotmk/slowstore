import json
from logging import getLogger as get_logger
import os
import shutil
import sys
import datetime
from pydantic import BaseModel
from functools import wraps

from typing import Any, Callable, Generic, List, Literal, TypeVar, cast, Sequence

T = TypeVar("T")

from logging import getLogger as get_logger

logger = get_logger(__name__)

def ensure_loaded(func):
    @wraps(func)
    def wrapper(self:"Slowstore", *args, **kwargs):
        if not self.loaded:
            self.load()
        return func(self, *args, **kwargs)
    return wrapper

class Change(Generic[T]):
    """A property change that can be undone or redone on a model"""

    key: str
    prop_name: str
    prev_val: Any
    new_val: Any
    date: datetime.datetime = datetime.datetime.now()

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

        self.__dict__.update(kwargs)
        self.date = datetime.datetime.now()

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
    store: "Slowstore[T]"
    model: T
    __key__: str

    is_dirty: bool = False
    __changes__: List[Change] = []

    def __init__(self, store: "Slowstore[T]", key: str, model: T):
        self.store = store
        self.model = model
        self.__key__ = key

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
        change = Change(
            key=self.__key__, prop_name=prop_name, prev_val=prev_val, new_val=new_val
        )
        self.store.__changes__.insert(0, change)
        self.__changes__.insert(0, change)

    def __reset__(self, count: int = sys.maxsize):
        size = len(self.__changes__)
        while size > 0 and count > 0:
            change = self.__changes__.pop(0)
            count -= 1
            size -= 1
            change.undo(self)
        if count > 0 and self.store.save_on_change:
            self.commit()


class Slowstore(Generic[T]):
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
    """

    directory: str
    cls: type
    save_on_change: bool = True
    save_on_exit: bool = True
    load_changes_from_file: bool = False
    save_changes_to_file: bool = True
    key_selector: Callable[["Slowstore[T]", T], str] | None = None
    loaded: bool = False
    __data__: dict[str, ModelProxy[T]]
    __changes__: List[Change]

    def __init__(self, cls: type, directory: str, **kwargs):
        """Creates a new Slowstore instance"""
        super().__init__()
        self.directory = directory
        self.cls = cls
        self.__data__ = {}
        self.__changes__ = []
        # get the model type from the generic
        self.save_on_change = kwargs.get("save_on_change", self.save_on_change)
        self.save_on_exit = kwargs.get("save_on_exit", self.save_on_exit)
        self.load_changes_from_file = kwargs.get(
            "load_changes_from_file", self.load_changes_from_file
        )
        self.save_changes_to_file = kwargs.get(
            "save_changes_to_file", self.save_changes_to_file
        )

        self.key_selector = kwargs.get("key_selector", None)

        if kwargs.get("load_on_start", True):
            self.load()

    @ensure_loaded
    def get(self, key) -> T | None:
        """gets an object from the story"""
        return cast(T, self.__data__.get(key))

    @ensure_loaded
    def add(self, value: T, skip_autosave: bool = False):
        key = self.key_for(value)
        self.upsert(key, value, skip_autosave)

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
        values: Sequence[T],
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
    def filter(self, filter: Callable[[str, T], bool]):
        """yield all models that satisfy the filter function"""
        for key, proxy in self.__data__.items():
            if filter(key, cast(T, proxy)):
                yield cast(T, proxy)

    @ensure_loaded
    def first(
        self,
        filter: Callable[[str, T], bool | Literal[True] | Literal[False]] | None = None,
    ) -> T | None:
        """return the model that satisfy the filter function otherwise return None"""
        for key, proxy in self.__data__.items():
            if filter is not None and filter(key, cast(T, proxy)):
                return cast(T, proxy)
            return cast(T, proxy)
        return None

    @ensure_loaded
    def update(
        self,
        filter: Callable[[str, ModelProxy], bool],
        updater: Callable[[str, ModelProxy], None],
    ):
        for key, proxy in self.__data__.items():
            if filter(key, proxy):
                updater(key, proxy)

    @ensure_loaded
    def values(self):
        for x in self.__data__:
            yield self.get(x)

    @ensure_loaded
    def commit_all(self):
        for proxy in self.__data__.values():
            self.commit(proxy)

    @ensure_loaded
    def commit(self, *items: ModelProxy[T]):
        for item in items:
            if item.store != self:
                raise ValueError("Item does not belong to this store")
            if item.is_dirty:
                with open(
                    f"{self.directory}/{self.__sanitize_file_name__(item.__key__)}.json",
                    "w",
                ) as f:
                    d = item.model.__dict__
                    if isinstance(item.model, BaseModel):
                        d = cast(BaseModel, item.model).model_dump()
                    d["__key__"] = item.__key__
                    if self.save_changes_to_file:
                        d["__changes__"] = [x.__dict__ for x in item.__changes__]
                    f.write(json.dumps(d, indent=2, default=json_default))
                item.is_dirty = False

    @ensure_loaded
    def __contains__(self, key: str | ModelProxy[T]) -> bool:
        if isinstance(key, ModelProxy):
            return key.__key__ in self.__data__

        return key in self.__data__

    @ensure_loaded
    def __iter__(self):
        return iter(self.__data__.keys())

    def __getitem__(self, key: str):
        return cast(T, self.get(key))

    def __setitem__(self, key: str, value: T):
        return cast(T, self.upsert(key, value))

    def __delitem__(self, key: str):
        return self.delete(key)

    def load(self):
        if not os.path.exists(self.directory):
            os.makedirs(self.directory, exist_ok=True)

        for filename in os.listdir(self.directory):
            with open(f"{self.directory}/{filename}", "r") as f:
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
            shutil.rmtree(self.directory, ignore_errors=True)
        self.__data__ = {}
        self.__changes__ = []
        self.loaded = False

    def key_for(
        self,
        value: T,
        key_or_selector: Callable[["Slowstore", T], str] | str | None = None,
    ) -> str:
        key = None
        if key_or_selector is not None:
            if callable(key_or_selector):
                key = key_or_selector(self, value)
            else:
                key = key_or_selector
        if self.key_selector is not None:
            key = self.key_selector(self, value)
        elif value.__dict__.get("__key__") is not None:
            key = value.__dict__.get("__key__")
        elif value.__dict__.get("id") is not None:
            key = value.__dict__.get("id")
        if key is None:
            raise ValueError("Could not determine key for value")
        return key

    def __sanitize_file_name__(self, name: str):
        return (
            name.replace("/", "_")
            .replace("\\", "_")
            .replace(":", "_")
            .replace(" ", "_")
            .replace(".", "_")
            .replace("!", "_")
            .replace("?", "_")
            .lower()
        )


    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):  # pyright: ignore

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
