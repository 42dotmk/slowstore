import datetime
from typing import Any, Generic, TypeVar

import slowstore


T = TypeVar("T")


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

    def undo(self, model: "slowstore.Proxy[T]"):
        if self.kind == ChangeKind.UPDATE:
            model.__setattr__(self.prop_name, self.prev_val)
        elif self.kind == ChangeKind.ADD:
            model.store.delete(self.key)
        elif self.kind == ChangeKind.DELETE:
            model.store.upsert(self.key, self.model)

    def redo(self, model: "slowstore.Proxy[T]"):
        if self.kind == ChangeKind.UPDATE:
            model.__setattr__(self.prop_name, self.new_val)
        elif self.kind == ChangeKind.ADD:
            model.store.upsert(self.key, self.model)
        elif self.kind == ChangeKind.DELETE:
            model.store.delete(self.key)
