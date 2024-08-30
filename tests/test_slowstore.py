import os
from slowstore import Slowstore, ModelProxy
from pydantic import BaseModel
import pytest
from typing import cast
import shutil

DATA_DIR = "./test_data"


class SampleModel(BaseModel):
    name: str
    age: int = 0

    def sample_instance_method(self):
        self.name = "sample_instance_method"


@pytest.fixture()
def store():
    store = Slowstore[SampleModel](SampleModel, os.path.join(DATA_DIR, "test"))
    store.clear()

    yield store

    store.clear()
    shutil.rmtree(DATA_DIR, ignore_errors=True)


def load_store() -> Slowstore[SampleModel]:
    return Slowstore[SampleModel](SampleModel, os.path.join(DATA_DIR, "test")).load()


def populate_store(store: Slowstore[SampleModel]):
    for i in range(0, 10):
        store.upsert(f"test://{i}?", SampleModel(name=f"test{i}"))
    store.commit_all()


def test_commit_all(store: Slowstore[SampleModel]):
    for i in range(0, 10):
        store.upsert(f"test://{i}?", SampleModel(name=f"test{i}"))
    store.commit_all()


def test_undo(store: Slowstore[SampleModel]):
    key = "test://1"
    model = store.upsert(key, SampleModel(name="test1"))
    proxy = cast(ModelProxy, model)
    model.name = "another"
    print(model.name)
    assert model.name == "another"
    proxy.__reset__(1)
    print(model.name)
    assert model.name == "test1"


def test_query(store):
    populate_store(store)
    assert len(list(store.filter(lambda _, v: v.name == "test1"))) == 1
    assert len(list(store.filter(lambda _, v: v.name == "test10"))) == 0


def test_remove(store):
    populate_store(store)
    model = store.first(lambda *_: True)
    assert model is not None
    key = cast(ModelProxy, model).__key__
    store.delete(key)

    assert key not in store


def test_update_from_instance_method(store: Slowstore[SampleModel]):
    model = store.upsert("test://1", SampleModel(name="test1"))
    model.name = "from_test"
    model.sample_instance_method()

    store.load()

    model2 = store.get("test://1")
    if model2 is None:
        assert False
    assert model2.name == "sample_instance_method"
    store.clear()


def test_save_changes_on_file(store: Slowstore[SampleModel]):
    key = "test"
    model = store.upsert(key, SampleModel(name="test"))
    model.name = "tito"

    store.load_changes_from_file = True
    store.load()

    model2 = store.get(key)
    if model2 is None:
        assert False
    proxy = cast(ModelProxy, model2)

    assert len(proxy.__changes__) == 1
