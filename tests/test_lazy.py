"""Lazy point-read mode: open without scanning, load per key, scan on demand."""

from pydantic import BaseModel

from slowstore import Store


class Item(BaseModel):
    id: str
    qty: int


def seeded(tmp_path, n=5):
    d = str(tmp_path / "items")
    with Store(Item, d) as store:
        for i in range(n):
            store.upsert(f"k{i}", Item(id=f"k{i}", qty=i))
    return d


def test_get_loads_only_the_touched_record(tmp_path):
    store = Store(Item, seeded(tmp_path), lazy=True)
    item = store.get("k2")
    assert item is not None and item.qty == 2
    assert not store.fully_loaded
    assert set(store.__data__.keys()) == {"k2"}  # nothing else was read


def test_contains_and_missing_key(tmp_path):
    store = Store(Item, seeded(tmp_path), lazy=True)
    assert "k1" in store
    assert "nope" not in store
    assert store.get("nope") is None
    assert not store.fully_loaded


def test_enumeration_completes_the_scan(tmp_path):
    store = Store(Item, seeded(tmp_path), lazy=True)
    _ = store.get("k0")
    assert len(store) == 5
    assert store.fully_loaded
    assert {i.qty for i in store.raw_values()} == {0, 1, 2, 3, 4}


def test_scan_keeps_already_loaded_proxies(tmp_path):
    store = Store(Item, seeded(tmp_path), lazy=True)
    first = store.get("k3")
    _ = len(store)  # full scan
    assert store.get("k3") is first  # identity survives the scan


def test_upsert_existing_key_updates_instead_of_duplicating(tmp_path):
    store = Store(Item, seeded(tmp_path), lazy=True)
    # without the disk probe this would look like a fresh key and record a
    # second ADD for an existing record
    updated = store.upsert("k1", Item(id="k1", qty=99))
    assert updated.qty == 99
    again = Store(Item, str(tmp_path / "items")).get_model("k1")
    assert again is not None and again.qty == 99


def test_writes_work_before_any_scan(tmp_path):
    store = Store(Item, seeded(tmp_path), lazy=True)
    store.upsert("fresh", Item(id="fresh", qty=42))
    assert not store.fully_loaded
    assert Store(Item, str(tmp_path / "items")).get_model("fresh") is not None
    assert store.delete("k4")
    assert len(store) == 5  # 5 seeded - 1 deleted + 1 fresh
