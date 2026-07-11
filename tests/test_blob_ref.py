import os
from typing import Optional

import pytest
from pydantic import BaseModel

from slowstore import BlobRef, CacheMode, Ref, Reference, Registry, Store

DATA_DIR = "./test_data"


class Doc(BaseModel):
    title: str
    body: Optional[BlobRef] = None
    author: Optional[Ref] = None


class Person(BaseModel):
    name: str
    age: int = 0


class Article(BaseModel):
    title: str
    # ergonomic, autocompleting reference: typed as Person, stored as a Ref
    author: Optional[Reference[Person]] = None


@pytest.fixture()
def docs():
    store = Store[Doc](Doc, os.path.join(DATA_DIR, "docs"), key_selector=lambda d: d.title)
    store.clear()
    yield store


def fresh_docs() -> Store[Doc]:
    return Store[Doc](
        Doc, os.path.join(DATA_DIR, "docs"), key_selector=lambda d: d.title
    ).load()


# -- blobs --------------------------------------------------------------------


def test_blob_stored_as_sidecar_not_inline(docs: Store[Doc]):
    d = docs.set(Doc(title="a"))
    d.body = BlobRef.from_bytes(b"hello world", content_type="text/plain")

    record_path = os.path.join(docs.directory, "a.json")
    sidecar_path = os.path.join(docs.directory, "a.body.bin")

    assert os.path.exists(sidecar_path)
    with open(record_path) as f:
        contents = f.read()
    # the record JSON holds only the descriptor, not the raw content
    assert "__blob__" in contents
    assert "hello world" not in contents


def test_blob_lazy_read_after_reload(docs: Store[Doc]):
    d = docs.set(Doc(title="a"))
    d.body = BlobRef.from_bytes(b"payload-bytes")

    reloaded = fresh_docs().get("a")
    assert reloaded is not None
    # descriptor is present but bytes are only pulled from disk on access
    assert reloaded.body._data is None
    assert reloaded.body.bytes == b"payload-bytes"


def test_blob_cache_reference_vs_every_time(docs: Store[Doc]):
    d = docs.set(Doc(title="a"))
    d.body = BlobRef.from_bytes(b"v1", cache=CacheMode.REFERENCE)

    reloaded = fresh_docs().get("a")
    assert reloaded.body.bytes == b"v1"  # caches after first read

    # mutate the sidecar behind the store's back
    with open(os.path.join(docs.directory, "a.body.bin"), "wb") as f:
        f.write(b"v2")

    # REFERENCE keeps the cached value
    assert reloaded.body.bytes == b"v1"
    # EVERY_TIME re-reads from disk
    reloaded.body.cache = CacheMode.EVERY_TIME
    reloaded.body._data = None
    assert reloaded.body.bytes == b"v2"


def test_blob_in_place_set_autosaves(docs: Store[Doc]):
    d = docs.set(Doc(title="a"))
    d.body = BlobRef.from_bytes(b"first")
    d.body.set(b"second")

    reloaded = fresh_docs().get("a")
    assert reloaded.body.bytes == b"second"


def test_blob_direct_assign_text_and_bytes(docs: Store[Doc]):
    # assigning a str/bytes to a BlobRef field auto-wraps it
    d = docs.set(Doc(title="a"))
    d.body = "just text"
    assert isinstance(d.body, BlobRef)
    assert d.body.text == "just text"

    d.body = b"raw bytes"
    assert d.body.bytes == b"raw bytes"

    # survives reload
    assert fresh_docs().get("a").body.bytes == b"raw bytes"


def test_blob_text_roundtrip(docs: Store[Doc]):
    d = docs.set(Doc(title="a"))
    d.body = BlobRef.from_text("hello, text", content_type="text/markdown")

    reloaded = fresh_docs().get("a")
    assert reloaded is not None
    assert reloaded.body.text == "hello, text"
    assert reloaded.body.read_text() == "hello, text"

    # in-place text write is convenience over set() and auto-saves
    reloaded.body.text = "rewritten"
    assert fresh_docs().get("a").body.text == "rewritten"


def test_delete_cascades_to_blob(docs: Store[Doc]):
    d = docs.set(Doc(title="a"))
    d.body = BlobRef.from_bytes(b"data")
    sidecar_path = os.path.join(docs.directory, "a.body.bin")
    assert os.path.exists(sidecar_path)

    docs.delete("a")
    assert not os.path.exists(sidecar_path)


# -- references ---------------------------------------------------------------


@pytest.fixture()
def linked():
    reg = Registry()
    people = Store[Person](
        Person, os.path.join(DATA_DIR, "people"), key_selector=lambda p: p.name, registry=reg
    )
    docs = Store[Doc](
        Doc, os.path.join(DATA_DIR, "docs"), key_selector=lambda d: d.title, registry=reg
    )
    people.clear()
    docs.clear()
    yield reg, people, docs


def test_ref_resolves_across_collections(linked):
    reg, people, docs = linked
    author = people.set(Person(name="ada"))
    d = docs.set(Doc(title="a"))
    d.author = Ref.to(author)

    assert d.author.get().name == "ada"
    assert reg.resolve("people", "ada").name == "ada"


def test_ref_lazy_after_reload(linked):
    reg, people, docs = linked
    author = people.set(Person(name="ada"))
    d = docs.set(Doc(title="a"))
    d.author = Ref.to(author)

    # fresh registry + stores, mirroring a new process
    reg2 = Registry()
    Store[Person](Person, people.directory, key_selector=lambda p: p.name, registry=reg2).load()
    docs2 = Store[Doc](Doc, docs.directory, key_selector=lambda d: d.title, registry=reg2).load()

    resolved = docs2.get("a").author.get()
    assert resolved is not None
    assert resolved.name == "ada"


def test_registry_join(linked):
    reg, people, docs = linked
    author = people.set(Person(name="ada"))
    d = docs.set(Doc(title="a"))
    d.author = Ref.to(author)

    pairs = list(reg.join("docs", "author"))
    assert len(pairs) == 1
    doc_rec, author_rec = pairs[0]
    assert doc_rec.title == "a"
    assert author_rec.name == "ada"


def test_unbound_ref_raises():
    # a Ref that was never attached to a store has no context to resolve from
    ref = Ref(collection="people", key="ada")
    with pytest.raises(RuntimeError):
        ref.get()


def test_default_registry_resolves_without_explicit_registry():
    # neither store is given a registry, so both land in the shared default one
    # and references between them still resolve.
    people = Store[Person](
        Person, os.path.join(DATA_DIR, "def_people"), key_selector=lambda p: p.name
    )
    arts = Store[Article](
        Article, os.path.join(DATA_DIR, "def_articles"), key_selector=lambda a: a.title
    )
    people.clear()
    arts.clear()

    ada = people.set(Person(name="ada", age=36))
    art = arts.set(Article(title="essay"))
    art.author = Ref.to(ada)

    assert art.author.name == "ada"
    assert people.registry is arts.registry  # same shared default


# -- generic Reference with transparent attribute forwarding ------------------


@pytest.fixture()
def articles():
    reg = Registry()
    people = Store[Person](
        Person, os.path.join(DATA_DIR, "people"), key_selector=lambda p: p.name, registry=reg
    )
    arts = Store[Article](
        Article, os.path.join(DATA_DIR, "articles"), key_selector=lambda a: a.title, registry=reg
    )
    people.clear()
    arts.clear()
    yield reg, people, arts


def test_reference_forwards_attributes(articles):
    reg, people, arts = articles
    ada = people.set(Person(name="ada", age=36))
    art = arts.set(Article(title="essay"))
    art.author = Ref.to(ada)

    # transparent forwarding: no .get()/.value needed
    assert art.author.name == "ada"
    assert art.author.age == 36


def test_reference_direct_assign_record(articles):
    reg, people, arts = articles
    ada = people.set(Person(name="ada", age=36))
    art = arts.set(Article(title="essay"))

    # assign the record directly instead of Ref.to(ada)
    art.author = ada
    assert isinstance(art.author, Ref)
    assert art.author.name == "ada"

    # and it resolves after reload
    reg2 = Registry()
    Store[Person](Person, people.directory, key_selector=lambda p: p.name, registry=reg2).load()
    arts2 = Store[Article](
        Article, arts.directory, key_selector=lambda a: a.title, registry=reg2
    ).load()
    assert arts2.get("essay").author.name == "ada"


def test_reference_persists_as_descriptor_and_reloads(articles):
    reg, people, arts = articles
    ada = people.set(Person(name="ada", age=36))
    art = arts.set(Article(title="essay"))
    art.author = Ref.to(ada)

    # stored compactly as a ref descriptor, not an inlined Person
    with open(os.path.join(arts.directory, "essay.json")) as f:
        contents = f.read()
    assert "__ref__" in contents

    reg2 = Registry()
    Store[Person](Person, people.directory, key_selector=lambda p: p.name, registry=reg2).load()
    arts2 = Store[Article](
        Article, arts.directory, key_selector=lambda a: a.title, registry=reg2
    ).load()

    assert arts2.get("essay").author.name == "ada"


def test_reference_every_time_reresolves(articles):
    reg, people, arts = articles
    ada = people.set(Person(name="ada", age=36))
    art = arts.set(Article(title="essay"))
    art.author = Ref.to(ada, cache=CacheMode.EVERY_TIME)

    assert art.author.age == 36
    ada.age = 37  # mutate the target record
    # EVERY_TIME re-resolves, so the fresh value is seen
    assert art.author.age == 37
