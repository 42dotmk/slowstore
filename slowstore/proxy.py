from logging import getLogger as get_logger
import sys
import slowstore
from typing import Any, Generic, TypeVar, cast, override
from .change import Change, ChangeKind

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
    "__orig_class__",
]


class Proxy(Generic[T]):

    def __init__(self, store: "slowstore.Store[T]", key: str, model: T):
        self.store: "slowstore.Store[T]" = store
        self.model: T = model
        self.is_dirty: bool = False

        self.__key__: str = key
        self.__changes__: list[Change[T]] = []

    def __getattr__(self, name: str):
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

                def wrapper(*args: list[Any], **kwargs: dict[str, Any]) -> Any:
                    return func(self, *args, **kwargs)

                wrapper.__name__ = func_name
                return wrapper

    @override
    def __setattr__(self, name: str, value: Any) -> None:
        if name in __special_fields__:
            logger.debug(f"Setting proxy: {name}={value}")
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
        self, prop_name: str, prev_val: Any, new_val: Any, notify_changes: bool = True
    ) -> Change[T]:
        logger.debug(f"Adding change: {prop_name}={prev_val} -> {new_val}")
        change = Change[T](
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

    @override
    def __repr__(self)->str:
        return "ModelProxy(" + self.model.__repr__() + ")"
