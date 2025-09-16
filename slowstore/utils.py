import datetime
from functools import wraps
from typing import Any, Callable, cast
import slowstore

def json_default_serializer(o:Any):
    if isinstance(o, (datetime.date, datetime.datetime)):
        return o.isoformat()

def ensure_loaded(func:Callable[..., Any]) -> Callable[..., Any]:
    @wraps(func)  
    def wrapper(self: "slowstore.Store", *args:Any, **kwargs:Any) -> Any:  # pyright: ignore[reportUnknownParameterType, reportMissingTypeArgument]
        if not self.loaded:
            _ = self.load()  # pyright: ignore[reportUnknownVariableType]
        return func(self, *args, **kwargs)

    return cast(Callable[..., Any], wrapper)
