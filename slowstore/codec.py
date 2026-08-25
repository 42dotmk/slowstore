"""Pluggable model <-> dict (de)serialization strategies.

``Store`` never assumes a particular model system: it delegates converting a
record to/from a plain ``dict`` -- and discovering that model's blob/ref fields --
to a :class:`Codec`. Three backends ship, and :func:`codec_for` auto-selects:

* :class:`PydanticCodec` -- Pydantic v2 ``BaseModel`` (the historical default;
  full type revival on load for free).
* :class:`DataclassCodec` -- ``@dataclass`` models (constructed via their kwargs
  ``__init__``).
* :class:`PlainCodec` -- any other class; instances are revived *without* calling
  ``__init__`` (attributes are set directly, pickle-style), so no particular
  constructor signature is required.

Blobs and references round-trip in **every** backend: a :class:`~slowstore.blob.BlobRef`
serializes to its ``{"__blob__": ...}`` descriptor and a :class:`~slowstore.ref.Ref`
to its ``{"__ref__": ...}`` descriptor, and both are rebuilt from those markers on
load. The dataclass/plain backends additionally revive ``datetime``/``date`` fields
from ISO strings (mirroring :func:`~slowstore.utils.json_default_serializer` on the
write side) and recurse into nested dataclasses, but do not coerce other rich types
the way Pydantic does.
"""

from __future__ import annotations

import dataclasses
import datetime
import decimal
import enum
import uuid
from abc import ABC, abstractmethod
from typing import Any, get_args, get_origin, get_type_hints

from .blob import (
    BlobRef,
    _annotation_is_blob,
    _annotation_is_blob_list,
    field_is_blob,
    field_is_blob_list,
)
from .ref import (
    Ref,
    _annotation_is_reference,
    _annotation_is_reference_list,
    field_is_reference,
    field_is_reference_list,
)

_LIST_ORIGINS = (list, tuple, set, frozenset)


def _is_pydantic_model(cls: Any) -> bool:
    """Duck-typed Pydantic v2 model check that never imports pydantic.

    Lets Slowstore keep pydantic an *optional* dependency: a Pydantic ``BaseModel``
    subclass exposes ``model_fields`` (class-level) and ``model_validate``, so we
    can recognize one without importing pydantic (which may not be installed).
    """
    return (
        isinstance(cls, type)
        and hasattr(cls, "model_fields")
        and hasattr(cls, "model_validate")
    )


@dataclasses.dataclass
class SpecialFields:
    """Which of a model's fields are blob/ref (and their list forms).

    ``ref``/``blob`` are the full sets; ``ref_list``/``blob_list`` are the subsets
    declared as lists (``list[Ref]`` / ``list[BlobRef]``).
    """

    ref: set[str]
    blob: set[str]
    ref_list: set[str]
    blob_list: set[str]

    @classmethod
    def empty(cls) -> "SpecialFields":
        return cls(set(), set(), set(), set())


# -- shared special-value encode/decode ---------------------------------------


def _encode_special(value: Any) -> Any:
    """Serialize blob/ref (and nested models/lists) to JSON-native descriptors.

    ``datetime``/``date`` pass through untouched -- the layout's JSON dump handles
    them via ``json_default_serializer``.
    """
    if isinstance(value, BlobRef):
        return value.__to_descriptor__()
    if isinstance(value, Ref):
        return value.__to_descriptor__()
    if isinstance(value, list):
        return [_encode_special(v) for v in value]
    if _is_pydantic_model(type(value)):
        return value.model_dump(by_alias=True)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            f.name: _encode_special(getattr(value, f.name))
            for f in dataclasses.fields(value)
        }
    return value


def _decode_special(value: Any) -> Any:
    """Rebuild a ``BlobRef``/``Ref`` (or list thereof) from its descriptor marker.

    Dicts without a ``__blob__``/``__ref__`` marker pass through unchanged (they
    may be a plain dict field or a nested object handled later by type revival).
    """
    if isinstance(value, dict):
        if value.get("__blob__") is True:
            return BlobRef.__from_value__(value)
        if value.get("__ref__") is True:
            return Ref.__from_value__(value)
        return value
    if isinstance(value, list):
        return [_decode_special(v) for v in value]
    return value


def _revive_typed(annotation: Any, value: Any) -> Any:
    """Best-effort coercion of a JSON-native value back to its annotated type.

    Handles ``datetime``/``date`` (from ISO strings), nested ``@dataclass`` models,
    and recurses through ``Optional``/``list``. Anything else is returned as-is --
    the dataclass/plain codecs do not attempt Pydantic-grade coercion.
    """
    if value is None:
        return value
    origin = get_origin(annotation)
    if origin is None:
        if isinstance(annotation, type):
            if issubclass(annotation, datetime.datetime) and isinstance(value, str):
                try:
                    return datetime.datetime.fromisoformat(value)
                except ValueError:
                    return value
            if (
                issubclass(annotation, datetime.date)
                and not issubclass(annotation, datetime.datetime)
                and isinstance(value, str)
            ):
                try:
                    return datetime.date.fromisoformat(value)
                except ValueError:
                    return value
            if issubclass(annotation, decimal.Decimal) and isinstance(
                value, (str, int, float)
            ):
                try:
                    return decimal.Decimal(str(value))
                except decimal.InvalidOperation:
                    return value
            if issubclass(annotation, uuid.UUID) and isinstance(value, str):
                try:
                    return uuid.UUID(value)
                except ValueError:
                    return value
            if issubclass(annotation, enum.Enum):
                try:
                    return annotation(value)
                except ValueError:
                    return value
            if dataclasses.is_dataclass(annotation) and isinstance(value, dict):
                return _dataclass_from_dict(annotation, value)
        return value
    if origin in _LIST_ORIGINS and isinstance(value, list):
        args = get_args(annotation)
        return [_revive_typed(args[0], v) for v in value] if args else value
    # Optional/Union (or any other wrapper): try each non-None arm.
    for arg in get_args(annotation):
        if arg is type(None):
            continue
        revived = _revive_typed(arg, value)
        if revived is not value:
            return revived
    return value


def _safe_hints(cls: type) -> dict[str, Any]:
    """Resolved type hints (with ``Annotated`` metadata), or ``{}`` if they can't
    be resolved (e.g. unresolvable forward refs) so discovery degrades gracefully."""
    try:
        return get_type_hints(cls, include_extras=True)
    except Exception:
        return {}


def _classify_hints(hints: dict[str, Any]) -> SpecialFields:
    sf = SpecialFields.empty()
    for name, ann in hints.items():
        if _annotation_is_reference(ann):
            sf.ref.add(name)
            if _annotation_is_reference_list(ann):
                sf.ref_list.add(name)
        elif _annotation_is_blob(ann):
            sf.blob.add(name)
            if _annotation_is_blob_list(ann):
                sf.blob_list.add(name)
    return sf


def _dataclass_from_dict(cls: type, data: dict[str, Any]) -> Any:
    hints = _safe_hints(cls)
    names = {f.name for f in dataclasses.fields(cls)}
    kwargs: dict[str, Any] = {}
    for key, raw in data.items():
        if key not in names:
            continue  # ignore keys the dataclass doesn't declare (schema drift)
        val = _decode_special(raw)
        if key in hints:
            val = _revive_typed(hints[key], val)
        kwargs[key] = val
    return cls(**kwargs)


# -- codecs -------------------------------------------------------------------


class Codec(ABC):
    """Strategy converting a model instance to/from a plain ``dict`` and reporting
    its special (blob/ref) fields."""

    @abstractmethod
    def to_dict(self, model: Any) -> dict[str, Any]:
        """Serialize ``model`` to a JSON-native dict (blob/ref -> descriptors)."""

    @abstractmethod
    def from_dict(self, cls: type, data: dict[str, Any]) -> Any:
        """Reconstruct a ``cls`` instance from ``data`` (descriptors -> blob/ref)."""

    @abstractmethod
    def special_fields(self, cls: type) -> SpecialFields:
        """Classify ``cls``'s blob/ref fields for the proxy's auto-wrapping."""


class PydanticCodec(Codec):
    """Backend for Pydantic v2 ``BaseModel`` models (full type revival on load)."""

    def to_dict(self, model: Any) -> dict[str, Any]:
        return model.model_dump(by_alias=True)

    def from_dict(self, cls: type, data: dict[str, Any]) -> Any:
        return cls(**data)

    def special_fields(self, cls: type) -> SpecialFields:
        sf = SpecialFields.empty()
        for name, field_info in getattr(cls, "model_fields", {}).items():
            if field_is_reference(field_info):
                sf.ref.add(name)
                if field_is_reference_list(field_info):
                    sf.ref_list.add(name)
            elif field_is_blob(field_info):
                sf.blob.add(name)
                if field_is_blob_list(field_info):
                    sf.blob_list.add(name)
        return sf


class DataclassCodec(Codec):
    """Backend for ``@dataclass`` models (constructed via their kwargs ``__init__``)."""

    def to_dict(self, model: Any) -> dict[str, Any]:
        return {
            f.name: _encode_special(getattr(model, f.name))
            for f in dataclasses.fields(model)
        }

    def from_dict(self, cls: type, data: dict[str, Any]) -> Any:
        return _dataclass_from_dict(cls, data)

    def special_fields(self, cls: type) -> SpecialFields:
        return _classify_hints(_safe_hints(cls))


class PlainCodec(Codec):
    """Backend for arbitrary classes: instances are revived without calling
    ``__init__`` (attributes set directly), so no specific constructor is required."""

    def to_dict(self, model: Any) -> dict[str, Any]:
        return {k: _encode_special(v) for k, v in vars(model).items()}

    def from_dict(self, cls: type, data: dict[str, Any]) -> Any:
        hints = _safe_hints(cls)
        obj = cls.__new__(cls)  # bypass __init__: we set state directly
        for key, raw in data.items():
            val = _decode_special(raw)
            if key in hints:
                val = _revive_typed(hints[key], val)
            setattr(obj, key, val)
        return obj

    def special_fields(self, cls: type) -> SpecialFields:
        return _classify_hints(_safe_hints(cls))


def codec_for(cls: type) -> Codec:
    """Pick a default codec for ``cls``: Pydantic > dataclass > plain."""
    if _is_pydantic_model(cls):
        return PydanticCodec()
    if dataclasses.is_dataclass(cls):
        return DataclassCodec()
    return PlainCodec()
