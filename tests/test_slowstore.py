import os
from slowstore import Store, Proxy
from pydantic import BaseModel
import pytest
from typing import cast

DATA_DIR = "./test_data"


class SampleModel(BaseModel):
    name: str
    age: int = 0

    def sample_instance_method(self):
        self.name = "sample_instance_method"


@pytest.fixture()
def store():
    store = Store[SampleModel](SampleModel, os.path.join(DATA_DIR, "test"))
    store.clear()

    yield store

    # store.clear()
    # shutil.rmtree(DATA_DIR, ignore_errors=True)


def load_store() -> Store[SampleModel]:
    return Store[SampleModel](SampleModel, os.path.join(DATA_DIR, "test")).load()


def populate_store(store: Store[SampleModel]):
    for i in range(0, 10):
        store.upsert(f"test://{i}?", SampleModel(name=f"test{i}"))


def test_commit_all(store: Store[SampleModel]):
    for i in range(0, 10):
        store.upsert(f"test://{i}?", SampleModel(name=f"test{i}"))


def test_undo(store: Store[SampleModel]):
    key = "test://1"
    model = store.upsert(key, SampleModel(name="test1"))
    proxy = cast(Proxy[SampleModel], model)
    model.name = "another"
    print(model.name)
    assert model.name == "another"
    proxy.__reset__(1)
    print(model.name)
    assert model.name == "test1"


def test_query(store:Store[SampleModel]):
    populate_store(store)
    assert len(list(store.filter(lambda v: v.name == "test1"))) == 1
    assert len(list(store.filter(lambda v: v.name == "test10"))) == 0


def test_remove(store:Store[SampleModel]):
    populate_store(store)
    model = store.first(lambda *_: True)
    assert model is not None
    key = store.as_proxy(model).__key__
    store.delete(key)
    assert key not in store


def test_update_from_instance_method(store: Store[SampleModel]):
    model = store.upsert("test://1", SampleModel(name="test1"))
    model.name = "from_test"
    model.sample_instance_method()

    store.load()

    model2 = store.get("test://1")
    assert model2 is not None
    assert model2.name == "sample_instance_method"
    store.clear()


def test_save_changes_on_file(store: Store[SampleModel]):
    key = "test"
    model = store.upsert(key, SampleModel(name="test"))
    model.name = "tito"

    store.load_changes_from_file = True
    store.load()

    model2 = store.get(key)
    assert model2 is not None
    proxy = store.as_proxy(model2)
    # creation (ADD) + the name update (UPDATE) are both tracked
    assert len(proxy.__changes__) == 2
    kinds = {c.kind for c in proxy.__changes__}
    assert kinds == {"ADD", "UPDATE"}


def test_change_dates_preserved_on_reload(store: Store[SampleModel]):
    key = "dated"
    model = store.upsert(key, SampleModel(name="a"))
    model.name = "b"
    proxy = store.as_proxy(model)
    original = [c.date for c in proxy.__changes__]

    store.load_changes_from_file = True
    store.load()

    reloaded = [c.date for c in store.as_proxy(store.get(key)).__changes__]
    # timestamps survive the round-trip instead of being reset to now()
    assert reloaded == original

def test_notifications_on_create(store: Store[SampleModel]):
    hook_called = False
    def on_change(_, *changes):
        nonlocal hook_called
        assert len(changes) == 1
        assert not hook_called 
        hook_called = True

    store.add_change_hook(on_change)
    store.upsert("test://1", SampleModel(name="test1"))
    assert hook_called


def test_notifications_on_delete(store: Store[SampleModel]):
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



def test_max_changes_caps_persisted_history(tmp_path):
    d = str(tmp_path / "capped")
    store = Store(SampleModel, d, max_changes=5, load_changes_from_file=True)
    s = store.upsert("a", SampleModel(name="a", age=0))
    for i in range(20):
        s.age = i + 1
    proxy = store.as_proxy(s)
    assert len(proxy.__changes__) == 5
    assert proxy.__changes__[0].new_val == 20  # newest kept

    again = Store(SampleModel, d, load_changes_from_file=True)
    reloaded = again.get_proxy("a")
    assert reloaded is not None
    assert len(again.as_proxy(reloaded).__changes__) == 5


class MethodModel(BaseModel):
    name: str
    age: int = 0

    def rename(self, name: str):
        self.name = name

    @classmethod
    def kind(cls) -> str:
        return cls.__name__

    @staticmethod
    def shout(text: str) -> str:
        return text.upper()


@pytest.fixture()
def method_store():
    store = Store[MethodModel](MethodModel, os.path.join(DATA_DIR, "test_methods"))
    store.clear()
    yield store
    store.clear()


def test_proxy_pydantic_methods_run_on_the_model(method_store: Store[MethodModel]):
    proxy = method_store.upsert("a", MethodModel(name="a", age=3))

    assert proxy.model_dump() == {"name": "a", "age": 3}
    assert '"name":"a"' in proxy.model_dump_json()

    copy = proxy.model_copy(update={"age": 4})
    assert type(copy) is MethodModel
    assert copy.age == 4
    # the copy is detached: the stored record is untouched
    assert proxy.age == 3


def test_proxy_instance_method_writes_are_tracked(method_store: Store[MethodModel]):
    proxy = method_store.upsert("a", MethodModel(name="a"))
    proxy.rename("b")

    changes = cast(Proxy[MethodModel], proxy).__changes__
    assert changes[0].prop_name == "name"
    assert changes[0].new_val == "b"
    assert method_store.load().get("a").name == "b"


def test_proxy_class_and_static_methods(method_store: Store[MethodModel]):
    proxy = method_store.upsert("a", MethodModel(name="a"))
    assert proxy.kind() == "MethodModel"
    assert proxy.shout("hi") == "HI"


def test_proxy_attribute_reads_do_not_log(method_store: Store[MethodModel], caplog):
    import logging

    proxy = method_store.upsert("a", MethodModel(name="a"))
    with caplog.at_level(logging.DEBUG, logger="SLOWSTORE"):
        caplog.clear()
        _ = proxy.name
        _ = proxy.age
        _ = [p.name for p in method_store.values()]
    assert caplog.records == []
