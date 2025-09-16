import datetime
from functools import wraps
from typing import Any, Callable
import slowstore

def json_default_serializer(o:Any):
    if isinstance(o, (datetime.date, datetime.datetime)):
        return o.isoformat()

def ensure_loaded(func:Callable[..., Any]) -> Callable[..., Any]:
    @wraps(func)  # pyright: ignore[reportUnknownArgumentType]
    def wrapper(self: "slowstore.Store", *args:list[Any], **kwargs:dict[str, Any]) -> Any:
        if not self.loaded:
            self.load()
        return func(self, *args, **kwargs)

    return wrapper
