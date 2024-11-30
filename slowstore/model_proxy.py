from logging import getLogger as get_logger
import sys
import datetime
import slowstore
from typing import Any, Generic, List, TypeVar, cast

logger = get_logger("SLOWSTORE")

T = TypeVar("T")

__special_fields__ = [
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
    "__orig_class__"
]

class ChangeKind:
    ADD = "ADD"
    UPDATE = "UPDATE"
    DELETE = "DELETE"


class Change(Generic[T]):
    """A property change that can be undone or redone on a model"""

    # Not supporting this yet, since i dont need it
    transaction: str = ""

    def __init__(self, **kwargs):
        if "kind" not in kwargs:
            self.kind = ChangeKind.UPDATE
        else:
            self.kind = kwargs["kind"]

        if "key" not in kwargs:
            raise ValueError("key is required")

        if self.kind == ChangeKind.UPDATE:
            if "prop_name" not in kwargs:
                raise ValueError("prop_name is required")
            if "prev_val" not in kwargs:
                raise ValueError("prev_val is required")
            if "new_val" not in kwargs:
                raise ValueError("new_val is required")

            self.prop_name: str = kwargs["prop_name"]
            self.prev_val: Any = kwargs["prev_val"]
            self.new_val: Any = kwargs["new_val"]
        elif self.kind == ChangeKind.ADD:
            if "model" not in kwargs:
                raise ValueError("model is required")
            self.model: T = kwargs["model"]
        elif self.kind == ChangeKind.DELETE:
            if "model" not in kwargs:
                raise ValueError("model is required")
            self.model: T = kwargs["model"]
        else:
            raise ValueError("Invalid change kind")

        self.key: str = kwargs["key"]
        self.date: datetime.datetime = datetime.datetime.now()

    def undo(self, model: "ModelProxy[T]"):
        if self.kind == ChangeKind.UPDATE:
            model.__setattr__(self.prop_name, self.prev_val)
        elif self.kind == ChangeKind.ADD:
            model.store.delete(self.key)
        elif self.kind == ChangeKind.DELETE:
            model.store.upsert(self.key, self.model)

    def redo(self, model: "ModelProxy[T]"):
        if self.kind == ChangeKind.UPDATE:
            model.__setattr__(self.prop_name, self.new_val)
        elif self.kind == ChangeKind.ADD:
            model.store.upsert(self.key, self.model)
        elif self.kind == ChangeKind.DELETE:
            model.store.delete(self.key)

class ModelProxy(Generic[T]):

    def __init__(self, store: "slowstore.Slowstore[T]", key: str, model: T):
        self.store: "slowstore.Slowstore[T]" = store
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
                func = cast(Any, attr).__func__

                def wrapper(*args, **kwargs):
                    return func(self, *args, **kwargs)

                wrapper.__name__ = func_name
                return wrapper

    def __setattr__(self, name, value):

        if name in __special_fields__:
            logger.debug(f"Setting proxy: {name}={value}")
            super().__setattr__(name, value)
        else:
            self.__update_model_prop__(name, value, False, True)

    def __update_model_prop__(
        self, name, value, skip_auto_save=False, notify_changes=True
    ):
        logger.debug(f"Setting model: {name}={value}")
        prev = self.model.__getattribute__(name)
        if prev == value:
            return None

        setattr(self.model, name, value)
        self.is_dirty = True

        change = self.__add_change__(name, prev, value, notify_changes=notify_changes)

        if self.store.save_on_change and not skip_auto_save:
            self.commit()


        return change

    def commit(self):
        self.store.commit(self)

    def __add_change__(
        self, prop_name: str, prev_val: Any, new_val: Any, notify_changes=True
    ):
        logger.debug(f"Adding change: {prop_name}={prev_val} -> {new_val}")
        change = Change(
            kind=ChangeKind.UPDATE,
            key=self.__key__,
            prop_name=prop_name,
            prev_val=prev_val,
            new_val=new_val,
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

    def __repr__(self):
        return "ModelProxy(" + self.model.__repr__() + ")"


