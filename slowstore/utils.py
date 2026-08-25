import dataclasses
import datetime
import decimal
import enum
import uuid
from functools import wraps
from typing import Any, Callable, cast
import slowstore

def json_default_serializer(o: Any):
    """``json.dumps(default=...)`` hook for record writes.

    Handles the rich types that commonly appear in models; anything else raises
    ``TypeError`` (matching stdlib ``json`` behavior). Earlier versions returned
    ``None`` for unknown types, which json wrote as ``null`` — silent data loss
    (Decimal fields were the usual victim).
    """
    if isinstance(o, (datetime.date, datetime.datetime, datetime.time)):
        return o.isoformat()
    if isinstance(o, decimal.Decimal):
        # str, not float: floats corrupt exact decimal values, and every codec
        # revives a Decimal-annotated field from the string form.
        return str(o)
    if isinstance(o, uuid.UUID):
        return str(o)
    if isinstance(o, enum.Enum):
        return o.value
    if isinstance(o, (set, frozenset)):
        return list(o)
    # Change entries carry raw prev/new values, which can be a BlobRef/Ref
    # (serialize via descriptor), a Pydantic model, or a dataclass. Duck-typed
    # so this module imports neither pydantic nor the blob/ref modules.
    if hasattr(o, "__to_descriptor__"):
        return o.__to_descriptor__()
    if hasattr(o, "model_dump") and hasattr(type(o), "model_fields"):
        return o.model_dump(by_alias=True)
    if dataclasses.is_dataclass(o) and not isinstance(o, type):
        return {f.name: getattr(o, f.name) for f in dataclasses.fields(o)}
    raise TypeError(
        f"Object of type {type(o).__name__} is not JSON serializable "
        "(slowstore < 2.3 silently wrote null here)"
    )

def ensure_loaded(func:Callable[..., Any]) -> Callable[..., Any]:
    @wraps(func)
    def wrapper(self: "slowstore.Store", *args:Any, **kwargs:Any) -> Any:  # pyright: ignore[reportUnknownParameterType, reportMissingTypeArgument]
        if not self.loaded:
            _ = self.load()  # pyright: ignore[reportUnknownVariableType]
        return func(self, *args, **kwargs)

    return cast(Callable[..., Any], wrapper)
