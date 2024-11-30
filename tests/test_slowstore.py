import os
from slowstore import Change, Slowstore, ModelProxy
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

    # store.clear()
    # shutil.rmtree(DATA_DIR, ignore_errors=True)


def load_store() -> Slowstore[SampleModel]:
    return Slowstore[SampleModel](SampleModel, os.path.join(DATA_DIR, "test")).load()


def populate_store(store: Slowstore[SampleModel]):
    for i in range(0, 10):
        store.upsert(f"test://{i}?", SampleModel(name=f"test{i}"))


def test_commit_all(store: Slowstore[SampleModel]):
    for i in range(0, 10):
        store.upsert(f"test://{i}?", SampleModel(name=f"test{i}"))


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
    assert len(list(store.filter(lambda v: v.name == "test1"))) == 1
    assert len(list(store.filter(lambda v: v.name == "test10"))) == 0


def test_remove(store:Slowstore[SampleModel]):
    populate_store(store)
    model = store.first(lambda *_: True)
    assert model is not None
    key = store.as_proxy(model).__key__
    store.delete(key)
    assert key not in store


def test_update_from_instance_method(store: Slowstore[SampleModel]):
    model = store.upsert("test://1", SampleModel(name="test1"))
    model.name = "from_test"
    model.sample_instance_method()

    store.load()

    model2 = store.get("test://1")
    assert model2 is not None
    assert model2.name == "sample_instance_method"
    store.clear()


def test_save_changes_on_file(store: Slowstore[SampleModel]):
    key = "test"
    model = store.upsert(key, SampleModel(name="test"))
    model.name = "tito"

    store.load_changes_from_file = True
    store.load()

    model2 = store.get(key)
    assert model2 is not None
    proxy = store.as_proxy(model2)
    assert len(proxy.__changes__) == 1

def test_notifications_on_create(store: Slowstore[SampleModel]):
    hook_called = False
    def on_change(_, *changes):
        nonlocal hook_called
        assert len(changes) == 1
        assert not hook_called 
        hook_called = True

    store.add_change_hook(on_change)
    store.upsert("test://1", SampleModel(name="test1"))
    assert hook_called


def test_notifications_on_delete(store: Slowstore[SampleModel]):
    hook_called = False
    def on_change(_, *changes):
        nonlocal hook_called
        assert len(changes) == 1
        assert not hook_called 
        hook_called = True

    store.upsert("test://1", SampleModel(name="test1"))
    store.add_change_hook(on_change)
    store.delete("test://1")
    assert hook_called

