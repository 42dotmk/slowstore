import os
from server.slowstore import Slowstore
from pydantic import BaseModel
import pytest
from typing import Any, cast
import shutil

DATA_DIR = "./test_data"

class SampleModel(BaseModel):
    name: str
    age: int = 0

@pytest.fixture
def store():
    # setup
    store = Slowstore[SampleModel](SampleModel, os.path.join(DATA_DIR, "test")).load()
    store.clear()

    yield store

    # cleanup
    store.clear()
    shutil.rmtree(DATA_DIR, ignore_errors=True)

def load_store()->Slowstore[SampleModel]:
    return Slowstore[SampleModel](SampleModel, os.path.join(DATA_DIR, "test")).load()

def populate_store(store:Slowstore[SampleModel]):
    for i in range(0, 10):
        store.upsert(f"test://{i}?", SampleModel(name=f"test{i}"))
    store.commit_all()

def test_commit_all(store:Slowstore[SampleModel]):
    for i in range(0, 10):
        store.upsert(f"test://{i}?", SampleModel(name=f"test{i}"))
    store.commit_all()
    
    store = load_store()
    assert len(store.all().items()) == 10

def test_undo(store:Slowstore[SampleModel]):
    key = "test://1"
    model = store.upsert(key, SampleModel(name="test1"))
    model.name = "another"
    print(model.name)
    assert model.name == "another"
    model.__reset__(1)
    print(model.name)
    assert model.name == "test1"
    store.commit_all()

    store = load_store()
    model = store.get(key)
    if model is None:
        assert False
    assert model.name == "test1"

def test_query(store:Slowstore[SampleModel]):
    store = load_store()
    populate_store(store)
    assert len(list(store.query(lambda _, v: v.name == "test1"))) == 1
    assert len(list(store.query(lambda _, v: v.name == "test2"))) == 1
    assert len(list(store.query(lambda _, v: v.name == "test3"))) == 1
    assert len(list(store.query(lambda _, v: v.name == "test4"))) == 1
    assert len(list(store.query(lambda _, v: v.name == "test5"))) == 1
    assert len(list(store.query(lambda _, v: v.name == "test6"))) == 1
    assert len(list(store.query(lambda _, v: v.name == "test7"))) == 1
    assert len(list(store.query(lambda _, v: v.name == "test8"))) == 1
    assert len(list(store.query(lambda _, v: v.name == "test9"))) == 1
    assert len(list(store.query(lambda _, v: v.name == "test10"))) == 0

def test_remove(store:Slowstore[SampleModel]):
    store = load_store()
    populate_store(store)
    model = store.first(lambda *_:True)
    assert model is not None
    key = model.__key__
    store.delete(key)

    assert key not in store

def test_generic_args():
    """check if the generic args are properly set"""
    store = load_store()
    assert cast(Any, store).__orig_class__.__args__[0] == SampleModel


