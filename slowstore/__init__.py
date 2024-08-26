import json
from logging import getLogger as get_logger
import os
import shutil
import sys
import datetime
from pydantic import BaseModel

from typing import Any, Callable, Generic, List, Literal, TypeVar, cast

T = TypeVar("T", bound=BaseModel)

from logging import getLogger as get_logger

logger = get_logger(__name__)


def json_default(o):
    if isinstance(o, (datetime.date, datetime.datetime)):
        return o.isoformat()


class Change(BaseModel):
    """A property change that can be undone or redone on a model"""

    key: str
    prop_name: str
    prev_val: Any
    new_val: Any
    date: datetime.datetime = datetime.datetime.now()

    # Not supporting this yet, since i dont need it
    transaction_id: str = ""

    def __init__(self, key, prop_name, prev_val, new_val):
        super().__init__(
            key=key, prop_name=prop_name, prev_val=prev_val, new_val=new_val
        )

    def undo(self, model):
        setattr(model, self.prop_name, self.prev_val)

    def redo(self, model):
        setattr(model, self.prop_name, self.new_val)


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


class ModelProxy(BaseModel, Generic[T]):
    store: "Slowstore[T]"
    model: T
    is_dirty: bool = False
    __changes__: List[Change] = []
    __key__: str

    def __init__(self, store: "Slowstore[T]", key: str, model: T):
        self.model = model
        self.store = store
        self.is_dirty = False
        self.__key__ = key
        self.__changes__ = []

    def __getattr__(self, name):
        if name in __special_fields__:
            return super().__getattribute__(name)
        else:
            return getattr(self.model, name)

    def __setattr__(self, name, value):
        if name == "model":
            super().__setattr__(name, value)
            self.is_dirty = False
            self.__changes__ = []
        elif name in __special_fields__:
            super().__setattr__(name, value)
        else:
            prev = self.model.__getattribute__(name)
            if prev == value:
                return
            setattr(self.model, name, value)
            self.__add_change__(name, prev, value)
            self.__changes__.insert(0, Change(self.__key__, name, prev, value))
            self.is_dirty = True
            if self.store.save_on_change:
                self.commit()

    def commit(self):
        self.store.commit(self)

    def __add_change__(self, prop_name: str, prev_val: Any, new_val: Any):
        change = Change(self.__key__, prop_name, prev_val, new_val)
        self.store.__changes__.append(change)
        self.__changes__.append(change)

    def __reset__(self, count: int = sys.maxsize):
        size = len(self.__changes__)
        while size > 0 and count > 0:
            change = self.__changes__.pop(0)
            count -= 1
            size -= 1
            change.undo(self.model)


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
    save_on_change: bool = False
    save_on_exit: bool = False
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
        if kwargs.get("load_on_start", True):
            self.load()
        self.save_on_change = kwargs.get("save_on_change", False)
        self.save_on_exit = kwargs.get("save_on_exit", True)

    def get(self, key) -> T:
        """gets an object from the story"""
        self.__ensure_loaded__()
        return cast(T, self.__data__[key])

    def upsert(self, key: str, value: T) -> T:
        """sets a new object in the store and returns it's proxy
        if we overwrite the key it will completely change the underlying model and this can cause data inconsistencies
        """
        self.__ensure_loaded__()
        if key in self.__data__:
            proxy = self.__data__[key]
            if proxy.model != value:
                self.__data__[key].model = value
                proxy.is_dirty = True

            if self.save_on_change:
                self.commit(proxy)

            return cast(T, proxy)

        proxy = ModelProxy[T](self, key, value)
        proxy.is_dirty = True

        if self.save_on_change:
            self.commit(proxy)

        self.__data__[key] = proxy
        return cast(T, proxy)

    def delete(self, key: str) -> bool:
        self.__ensure_loaded__()
        if key in self.__data__:
            del self.__data__[key]
            os.remove(f"{self.directory}/{self.__sanitize_file_name__(key)}.json")
            return True
        return False

    def __contains__(self, key: str | ModelProxy[T]) -> bool:
        self.__ensure_loaded__()

        if isinstance(key, ModelProxy):
            return key.__key__ in self.__data__

        return key in self.__data__

    def __getitem__(self, key: str):
        return cast(T, self.get(key))

    def __setitem__(self, key: str, value: T):
        return cast(T, self.upsert(key, value))

    def __delitem__(self, key: str):
        return self.delete(key)

    def filter(self, filter: Callable[[str, ModelProxy], bool]):
        """yield all models that satisfy the filter function"""
        for key, model in self.__data__.items():
            if filter(key, model):
                yield cast(T, model)

    def first(
        self,
        filter: Callable[[str, ModelProxy[T]], bool | Literal[True] | Literal[False]],
    ) -> T | None:
        """return the model that satisfy the filter function otherwise return None"""
        self.__ensure_loaded__()
        for key, model in self.__data__.items():
            if filter(key, model):
                return cast(T, model)
        return None

    def update(
        self,
        filter: Callable[[str, ModelProxy], bool],
        updater: Callable[[str, ModelProxy], None],
    ):
        for key, model in self.__data__.items():
            if filter(key, model):
                updater(key, model)

    def all(self):
        self.__ensure_loaded__()
        for x in self.__data__:
            yield self.get(x)

    def load(self):
        if not os.path.exists(self.directory):
            os.makedirs(self.directory, exist_ok=True)

        for filename in os.listdir(self.directory):
            with open(f"{self.directory}/{filename}", "r") as f:
                try:
                    d = json.load(f)
                    key: str = d["__key__"]
                    del d["__key__"]
                    proxy = ModelProxy[T](store=self, key=key, model=self.cls(**d))
                    self.__data__[key] = proxy
                except Exception as e:
                    logger.error(f"Error loading {filename}: {e}")
        self.loaded = True
        return self

    def clear(self):
        shutil.rmtree(self.directory)
        self.__data__ = {}
        self.loaded = False
        self.__ensure_loaded__()

    def commit_all(self):
        self.__ensure_loaded__()
        for proxy in self.__data__.values():
            self.commit(proxy)

    def commit(self, item: ModelProxy[T]):
        if item.store != self:
            raise ValueError("Item does not belong to this store")
        if item.is_dirty:
            with open(
                f"{self.directory}/{self.__sanitize_file_name__(item.__key__)}.json",
                "w",
            ) as f:
                d = item.model.model_dump()
                d["__key__"] = item.__key__
                f.write(json.dumps(d, indent=2, default=json_default))
            item.is_dirty = False
            item.__changes__ = []

    def __ensure_loaded__(self):
        if not self.loaded:
            self.load()

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

    def __iter__(self):
        self.__ensure_loaded__()
        return iter(self.__data__.keys())

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):  # pyright:ignore
        if exc_type is None and self.save_on_exit:
            self.commit_all()
            self.__data__ = {}
            self.loaded = False
        else:
            self.__data__ = {}
            self.loaded = False
        return False
