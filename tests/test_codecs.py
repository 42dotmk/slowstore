"""Codec behavior: rich-type round-trips, the serializer's no-silent-null
guarantee, and pydantic remaining an optional dependency."""

import dataclasses
import decimal
import enum
import subprocess
import sys
import uuid

import pytest
from pydantic import BaseModel

from slowstore import Store


class Money(BaseModel):
    amount: decimal.Decimal
    currency: str


class Mode(enum.Enum):
    OCEAN = 1
    ROAD = 2


class PydModel(BaseModel):
    id: str
    price: Money
    mode: Mode
    ref: uuid.UUID


@dataclasses.dataclass
class DcModel:
    id: str
    amount: decimal.Decimal
    mode: Mode
    ref: uuid.UUID


def test_decimal_round_trip_pydantic(tmp_path):
    d = str(tmp_path / "pyd")
    with Store(PydModel, d) as store:
        store.upsert(
            "a",
            PydModel(
                id="a",
                price=Money(amount=decimal.Decimal("12.34"), currency="EUR"),
                mode=Mode.OCEAN,
                ref=uuid.UUID(int=7),
            ),
        )
    reloaded = Store(PydModel, d).get_model("a")
    assert reloaded is not None
    assert reloaded.price.amount == decimal.Decimal("12.34")
    assert isinstance(reloaded.price.amount, decimal.Decimal)
    assert reloaded.mode is Mode.OCEAN
    assert reloaded.ref == uuid.UUID(int=7)


def test_decimal_round_trip_dataclass(tmp_path):
    d = str(tmp_path / "dc")
    with Store(DcModel, d) as store:
        store.upsert(
            "a",
            DcModel(
                id="a",
                amount=decimal.Decimal("0.1"),
                mode=Mode.ROAD,
                ref=uuid.UUID(int=9),
            ),
        )
    reloaded = Store(DcModel, d).get_model("a")
    assert reloaded is not None
    assert reloaded.amount == decimal.Decimal("0.1")
    assert isinstance(reloaded.amount, decimal.Decimal)
    assert reloaded.mode is Mode.ROAD
    assert reloaded.ref == uuid.UUID(int=9)


class Opaque:
    """Not JSON serializable by any rule the serializer knows."""


def test_unknown_type_raises_instead_of_null(tmp_path):
    @dataclasses.dataclass
    class Holder:
        id: str
        payload: object

    store = Store(Holder, str(tmp_path / "opaque"))
    with pytest.raises(TypeError, match="Opaque"):
        store.upsert("a", Holder(id="a", payload=Opaque()))


def test_pydantic_is_optional(tmp_path):
    """`import slowstore` + a dataclass store must work with pydantic absent."""
    code = """
import sys
import importlib.abc

class Blocker(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name == "pydantic" or name.startswith("pydantic.") or name.startswith("pydantic_"):
            raise ImportError("pydantic is blocked for this test")
        return None

sys.meta_path.insert(0, Blocker())

import dataclasses
from slowstore import Store

@dataclasses.dataclass
class Item:
    id: str
    qty: int

store = Store(Item, sys.argv[1])
store.upsert("x", Item(id="x", qty=3))
again = Store(Item, sys.argv[1]).get_model("x")
assert again is not None and again.qty == 3
"""
    subprocess.run(
        [sys.executable, "-c", code, str(tmp_path / "nopyd")], check=True
    )
