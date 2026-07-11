import json
import os
import shutil
from typing import Optional

import pytest
from pydantic import BaseModel

from slowstore import (
    BlobRef,
    DirPerObjectLayout,
    FilePerRecordLayout,
    JsonCollectionLayout,
    JsonlCollectionLayout,
    Store,
)

DATA_DIR = "./test_data/layouts"

LAYOUTS = [
    FilePerRecordLayout,
    JsonCollectionLayout,
    JsonlCollectionLayout,
    DirPerObjectLayout,
]


class Item(BaseModel):
    name: str
    value: int = 0
    doc: Optional[BlobRef] = None


def make_store(layout_cls, name: str):
    d = os.path.join(DATA_DIR, f"{layout_cls.__name__}_{name}")
    shutil.rmtree(d, ignore_errors=True)
    store = Store[Item](Item, d, key_selector=lambda i: i.name, layout=layout_cls())
    return store, d


def reopen(layout_cls, directory: str) -> Store[Item]:
    return Store[Item](
        Item, directory, key_selector=lambda i: i.name, layout=layout_cls()
    ).load()


# -- behavior parity across every layout --------------------------------------


@pytest.fixture(params=LAYOUTS, ids=[c.__name__ for c in LAYOUTS])
def layout_cls(request):
    return request.param


def test_records_roundtrip(layout_cls):
    store, d = make_store(layout_cls, "roundtrip")
    for i in range(5):
        store.upsert(f"k{i}", Item(name=f"k{i}", value=i))

    store2 = reopen(layout_cls, d)
    assert len(store2) == 5
    assert store2.get("k3").value == 3


def test_update_persists(layout_cls):
    store, d = make_store(layout_cls, "update")
    it = store.upsert("a", Item(name="a", value=1))
    it.value = 99
    assert reopen(layout_cls, d).get("a").value == 99


def test_delete_keeps_other_records(layout_cls):
    # exercises collection layouts' read-modify-write: deleting one record must
    # not drop the rest of the file.
    store, d = make_store(layout_cls, "delete")
    for i in range(3):
        store.upsert(f"k{i}", Item(name=f"k{i}", value=i))
    store.delete("k1")

    store2 = reopen(layout_cls, d)
    assert set(store2.keys()) == {"k0", "k2"}


def test_blob_roundtrip_and_delete_cascade(layout_cls):
    store, d = make_store(layout_cls, "blob")
    it = store.upsert("a", Item(name="a"))
    it.doc = "hello blob"

    store2 = reopen(layout_cls, d)
    a = store2.get("a")
    assert a.doc.text == "hello blob"

    uri = store2.layout.blob_uri("a", "doc", "doc")
    assert os.path.exists(uri)

    store2.delete("a")
    assert not os.path.exists(uri)


# -- layout-specific on-disk structure ----------------------------------------


def test_json_collection_is_single_file():
    store, d = make_store(JsonCollectionLayout, "struct")
    store.upsert("a", Item(name="a", value=1))
    store.upsert("b", Item(name="b", value=2))

    path = os.path.join(d, "collection.json")
    assert os.path.exists(path)
    with open(path) as f:
        data = json.load(f)
    assert set(data.keys()) == {"a", "b"}
    assert data["a"]["value"] == 1


def test_jsonl_collection_is_one_line_per_record():
    store, d = make_store(JsonlCollectionLayout, "struct")
    store.upsert("a", Item(name="a"))
    store.upsert("b", Item(name="b"))

    path = os.path.join(d, "collection.jsonl")
    with open(path) as f:
        lines = [ln for ln in f.read().splitlines() if ln.strip()]
    assert len(lines) == 2
    # each line is a standalone JSON object
    assert all(json.loads(ln)["__key__"] in {"a", "b"} for ln in lines)


def test_dir_per_object_structure():
    store, d = make_store(DirPerObjectLayout, "struct")
    it = store.upsert("a", Item(name="a"))
    it.doc = b"blobbytes"

    assert os.path.isdir(os.path.join(d, "a"))
    assert os.path.exists(os.path.join(d, "a", "record.json"))
    # the blob lives inside the object's own directory
    assert os.path.exists(os.path.join(d, "a", "doc.bin"))

    # deleting the record removes the whole object directory (blobs included)
    store.delete("a")
    assert not os.path.isdir(os.path.join(d, "a"))
