# Slowstore

[![PyPI version](https://img.shields.io/pypi/v/slowstore.svg)](https://pypi.org/project/slowstore/)
[![Python versions](https://img.shields.io/pypi/pyversions/slowstore.svg)](https://pypi.org/project/slowstore/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**A zero-setup, on-disk object store for Python.** Slowstore maps your Pydantic
models straight to JSON files, auto-syncs every change to disk, and keeps a full
undo/redo history — no server, no connection string, no schema migration. Point
it at a directory and go.

> ⚠️ Slowstore is deliberately **slow** and single-threaded. It exists to give you
> a great developer experience while exploring, prototyping, and debugging — not
> to be a production database. See [When (not) to use it](#when-not-to-use-it).

```python
from slowstore import Store
from pydantic import BaseModel

class User(BaseModel):
    name: str
    age: int = 0

store = Store[User](User, "mydata", key_selector=lambda u: u.name)

user = store.set(User(name="ada", age=36))  # writes mydata/ada.json immediately
user.age += 1                               # change is persisted on assignment
```

---

## Table of contents

- [Why Slowstore?](#why-slowstore)
- [Installation](#installation)
- [Quickstart](#quickstart)
- [Core concepts](#core-concepts)
- [Configuring the store](#configuring-the-store)
- [Reading & writing records](#reading--writing-records)
- [Querying](#querying)
- [Undo / Redo / Dirty](#undo--redo--dirty)
- [Committing manually](#committing-manually)
- [Change hooks & audit trail](#change-hooks--audit-trail)
- [Blobs (unstructured content)](#blobs-unstructured-content)
- [References (relationships across collections)](#references-relationships-across-collections)
- [Storage layouts](#storage-layouts)
- [How data is organized on disk](#how-data-is-organized-on-disk)
- [When (not) to use it](#when-not-to-use-it)
- [How it works](#how-it-works)
- [Feature status & roadmap](#feature-status--roadmap)
- [Development](#development)
- [License](#license)

---

## Why Slowstore?

- **No setup.** No database server, no ORM configuration, no connection string —
  just a directory on disk.
- **Transparent persistence.** Mutating a tracked object (`user.age += 1`) writes
  it to disk automatically. Turn this off and commit manually when you prefer.
- **Human-readable storage.** Everything is plain JSON you can open, diff, and
  edit by hand.
- **Built-in history.** Every record carries its own change log with undo/redo,
  dirty tracking, and an optional actor/audit trail.
- **Pluggable on-disk layout.** One file per record (default), one file per
  collection, or one directory per object — swap it without touching your models.
- **Blobs & relationships.** Keep large/binary content out of the JSON, and link
  records across collections with lazily-resolved references.

## Installation

```bash
pip install slowstore
```

Slowstore requires **Python 3.10+** and builds on **Pydantic v2**.

Using Poetry or uv:

```bash
poetry add slowstore
# or
uv add slowstore
```

## Quickstart

```python
from slowstore import Store
from pydantic import BaseModel

class SampleModel(BaseModel):
    name: str
    age: int = 0

    def birthday(self):
        self.age += 1

# `key_selector` derives the key each object is tracked/stored by.
store = Store[SampleModel](SampleModel, "mydata", key_selector=lambda m: m.name)

m = SampleModel(name="john", age=30)
dennis = store.set(SampleModel(name="denis", age=32))  # returns a tracked proxy
store.set(m)

dennis.name = "DENIS"   # the value in denis.json changes from "denis" to "DENIS"
dennis.birthday()       # bumps age -> also reflected in the JSON file
```

`mydata/denis.json` after running this program looks like:

```json
{
  "name": "DENIS",
  "age": 33,
  "__key__": "denis",
  "__changes__": [
    { "kind": "UPDATE", "prop_name": "age",  "prev_val": 32, "new_val": 33, "key": "denis", "date": "2024-08-28T19:04:12.840353" },
    { "kind": "UPDATE", "prop_name": "name", "prev_val": "denis", "new_val": "DENIS", "key": "denis", "date": "2024-08-28T19:04:12.840216" },
    { "kind": "ADD", "key": "denis", "date": "2024-08-28T19:04:12.840100" }
  ]
}
```

Slowstore tracks what happened in your program at all times. A runnable version of
this example lives in [`examples/simple.py`](examples/simple.py); blobs and
references are demonstrated in [`examples/blobs_and_refs.py`](examples/blobs_and_refs.py).

## Core concepts

- **Store** — a typed, dictionary-like collection of records of a single model
  type, backed by a directory on disk.
- **Proxy** — `store.set(...)` and the getters return a *proxy* that wraps your
  model. It behaves like the model (attribute access, method calls) but
  intercepts mutations to persist them and record history. Reach the underlying
  model with `proxy.model`.
- **Change** — every add/update/delete is recorded as a `Change` on the record's
  `__changes__` log, powering undo and the audit trail.
- **Key** — every record is stored under a string key. Provide a `key_selector`
  so `store.set(obj)` can derive the key, or use the explicit
  `insert`/`update`/`upsert(key, obj)` methods.

## Configuring the store

All options are keyword arguments to the constructor:

```python
store = Store[User](
    User,
    "mydata",
    key_selector=lambda u: u.name,   # derive the key from the object
    save_on_change=True,             # persist on every mutation (default)
    save_on_exit=True,               # flush on `with` block / context exit (default)
    load_on_start=False,             # eagerly load the directory in __init__
    encoding="utf-8",                # file encoding (default)
    ensure_ascii=False,              # JSON escaping (default)
    layout=None,                     # StorageLayout; defaults to FilePerRecordLayout
    registry=None,                   # shared Registry for cross-collection refs
    name=None,                       # collection name (defaults to directory basename)
    get_identity=None,               # callable returning the actor for each change
    save_changes_to_file=True,       # write the __changes__ log to disk (default)
    load_changes_from_file=False,    # restore the change log on load
)
```

Toggle behaviour at runtime, too:

```python
store.save_on_change = False   # switch to manual commits
```

Slowstore is also a context manager. When `save_on_exit=True` (the default), any
pending changes are flushed when the block ends:

```python
with Store[User](User, "mydata", key_selector=lambda u: u.name) as store:
    store.set(User(name="grace"))
# changes are committed here
```

## Reading & writing records

```python
store.set(obj)                  # upsert using key_selector, returns a proxy
store.insert("k", obj)          # add; raises if key "k" already exists
store.update("k", obj)          # update an existing record; raises if missing
store.upsert("k", obj)          # insert or update by explicit key
store.add_range([o1, o2, o3])   # bulk upsert (committed together)

store.get("k")                  # proxy (typed as the model), or None
store.get_model("k")            # the raw model (no proxy), or None
store.get_proxy("k")            # the proxy object, or None
store["k"]                      # same as get("k")
store["k"] = obj                # same as upsert("k", obj)
```

Deleting a record also removes its file(s) on disk (and any blob sidecars):

```python
store.delete("k")
store.delete(proxy)             # accepts a proxy or a key
del store["k"]

if obj in store:                # membership by proxy or key
    ...
```

## Querying

```python
store.values()                        # iterator of all records (as proxies)
store.raw_values()                    # iterator of the underlying raw models
store.keys()                          # the record keys
len(store)                            # number of records
for record in store: ...              # iterate records

store.filter(lambda x: x.age > 30)    # yield records matching a predicate
store.first(lambda x: x.age > 30)     # first match, or None

# bulk mutate everything matching a predicate (persists per save_on_change)
store.update_where(lambda x: x.age > 30, lambda x: setattr(x, "age", 30))
```

## Undo & dirty tracking

Because every record is a proxy over your object, it carries its own change log
and can roll itself back:

```python
user.is_dirty          # True if there are uncommitted changes
user.__reset__(1)      # undo the last change
user.__reset__(3)      # undo the last 3 changes
user.__reset__()       # undo the entire recorded history
```

`__reset__(n)` replays the recorded changes in reverse, restoring previous values
(and re-creating/removing records for undone deletes/adds). Each `Change` also
supports `undo`/`redo` individually if you walk `proxy.__changes__` yourself.

## Committing manually

With `save_on_change=False`, mutations are held in memory until you commit:

```python
store.save_on_change = False
user.age += 1

store.commit(user)          # persist one (or several) records
store.commit(u1, u2)
store.commit_all()          # persist everything that's dirty
```

## Change hooks & audit trail

Subscribe to changes as they happen — handy for logging, cache invalidation, or
syncing to another system:

```python
def on_change(proxy, changes):
    for c in changes:
        print(c.kind, c.key, getattr(c, "prop_name", None))

store.add_change_hook(on_change)
store.remove_change_hook(on_change)
store.clear_change_hooks()
```

To attribute each change to a user/actor, pass `get_identity` — its return value
is stored on every `Change` as `actor`:

```python
store = Store[User](User, "mydata",
                    key_selector=lambda u: u.name,
                    get_identity=lambda: current_user())
```

## Blobs (unstructured content)

Sometimes a record needs to carry content that shouldn't live inline in the JSON
(images, files, large text). Declare the field as a `BlobRef`: the record JSON keeps
only a small descriptor, while the bytes live in a **sidecar file** beside the record.
Bytes are read lazily on first access.

```python
from slowstore import Store, BlobRef, CacheMode
from pydantic import BaseModel
from typing import Optional

class Document(BaseModel):
    title: str
    body: Optional[BlobRef] = None

docs = Store[Document](Document, "docs", key_selector=lambda d: d.title)
d = docs.set(Document(title="intro"))

# attach content; the bytes are flushed to docs/intro.body.bin on commit.
# assigning str/bytes directly auto-wraps it in a BlobRef:
d.body = "lots of content..."       # str  -> BlobRef (text/plain)
d.body = b"lots of content..."      # bytes -> BlobRef
# or build one explicitly for options like content-type:
d.body = BlobRef.from_bytes(b"lots of content...", content_type="text/plain")
d.body = BlobRef.from_path("cover.png")
d.body = BlobRef.from_text("lots of content...")

# read lazily (only pulled from disk when accessed)
print(d.body.bytes)          # b"lots of content..."
d.body.set(b"new content")   # replace content; re-flushed on save
```

Text sidecars are convenient to read and write directly:

```python
d.body = BlobRef.from_text("first draft")
print(d.body.text)                      # "first draft"
d.body.text = "second draft"            # write convenience; re-flushed on save
d.body.write_text("v3", encoding="utf-8")
d.body.read_text(encoding="utf-8")
```

`cache=CacheMode.REFERENCE` (default) keeps the bytes in memory after the first read;
`cache=CacheMode.EVERY_TIME` re-reads from disk on every access. Deleting a record also
deletes its blob sidecars.

> **On-disk name.** A blob's filename is derived from the record key and field name
> (e.g. `docs/intro.body.bin`), not a name you pass. `from_path("cover.png")` reads
> the file's bytes but does not adopt its name. See
> [Storage layouts](#storage-layouts) for how each layout names blob files.

## References (relationships across collections)

A reference points at a record in another collection by `(collection, key)` and is
resolved **lazily** through a shared `Registry` that the participating stores register
into.

Declare the field as `Reference[Target]`: it is **typed as `Target`** — so your editor
autocompletes the target's fields — while at runtime it holds a `Ref` that
**transparently forwards** attribute access to the resolved record. No `.value()` /
`.get()` needed:

```python
from slowstore import Store, Ref, Reference, Registry, CacheMode
from pydantic import BaseModel
from typing import Optional

class Person(BaseModel):
    name: str

class Document(BaseModel):
    title: str
    author: Optional[Reference[Person]] = None   # typed as Person, stored as a Ref

# passing `registry` is optional — stores without one share a process-wide
# default registry, so references resolve out of the box.
registry = Registry()
people = Store[Person](Person, "people", key_selector=lambda p: p.name, registry=registry)
docs   = Store[Document](Document, "docs", key_selector=lambda d: d.title, registry=registry)

ada = people.set(Person(name="ada"))
d = docs.set(Document(title="intro"))
d.author = ada                               # assign the record directly — auto-wrapped in a Ref
# equivalently: d.author = Ref.to(ada)       # explicit form (needed for cache=... options)

d.author.name                                # "ada" — resolved on demand, autocompletes
registry.resolve("people", "ada")            # direct lookup by (collection, key)

# query across collections
for document, author in registry.join("docs", "author"):
    print(document.title, "by", author.name)
```

Prefer the explicit form when you want the raw record or a `None` check: declare the
field as `Ref` (or `Optional[Ref]`) and call `.get()` / `.value` yourself. As with blobs,
`cache=CacheMode.REFERENCE` (default) caches the resolved record while
`cache=CacheMode.EVERY_TIME` re-resolves on each access (always fresh).

> Because attribute access forwards to the target, `Ref`'s own members (`get`, `value`,
> `collection`, `key`, `cache`) shadow same-named fields on the referenced record — reach
> those via `.get()`.

## Storage layouts

How records and blobs are laid out on disk is a pluggable `StorageLayout`. Pass one via
`layout=` to change the scheme without touching your models:

```python
from slowstore import Store, JsonlCollectionLayout
store = Store[Document](Document, "docs", layout=JsonlCollectionLayout())
```

Built-in layouts:

| Layout | On disk | Notes |
|---|---|---|
| `FilePerRecordLayout` *(default)* | `{key}.json` per record, `{key}.{field}.bin` blob sidecars | Historical behavior; easy to inspect per record |
| `JsonCollectionLayout` | one `collection.json` (object keyed by record key), blob sidecars | Whole collection in a single file |
| `JsonlCollectionLayout` | one `collection.jsonl`, one record per line, blob sidecars | Line-delimited JSON — cheap to scan/append |
| `DirPerObjectLayout` | one directory per record (`{key}/record.json`) with that record's blobs inside (`{key}/{field}.bin`) | Each object self-contained; deleting a record drops its whole directory |

The collection layouts (`Json`/`Jsonl`) keep every record in one file, so a write is a
read-modify-write of that file — fine for Slowstore's debugging use, but not built for
high write throughput. All layouts support blobs and reload identically; you can switch
layouts by pointing a new store at a fresh directory (there is no automatic migration
between on-disk formats).

## How data is organized on disk

By default each record is a JSON file named after its key, inside the store's directory
(the `FilePerRecordLayout`). This is configurable — see [Storage layouts](#storage-layouts)
for single-file (`collection.json` / `collection.jsonl`) and directory-per-object schemes.
Unstructured content is stored outside the record JSON as [blobs](#blobs-unstructured-content),
and [references](#references-relationships-across-collections) link records across
collections. Record keys are sanitized into filesystem-safe filenames.

## When (not) to use it

**Great for:**

- Prototypes, spikes, and exploratory scripts
- Debugging program behavior with a human-readable, diffable data trail
- Small CLIs, notebooks, and local tools where a database is overkill
- Fixtures and test data you want to inspect by hand

**Not for:**

- High write throughput or large datasets (every change is a file write)
- Multi-threaded or multi-process concurrent access (no locking)
- Anything performance-sensitive or safety-critical

Slowstore is **slow** by design, and single-threaded. It optimizes for developer
experience, not throughput.

## How it works

A Slowstore instance behaves like a dictionary of the objects you add to it. Instead of
storing the object directly, it wraps it in a **proxy** that records the object's state
and the changes made to it.

When you commit, the proxy's current state is serialized to disk along with its change
log. `__reset__` replays those recorded changes in reverse to move the proxy back
through its history, persisting the result. See the `Proxy` class for the details.

## Feature status & roadmap

- [x] **Save on change** — persist automatically on every mutation
- [x] **Undo** — roll a record back through its history (`__reset__`)
- [x] **Dirty tracking** — know whether a record has uncommitted changes
- [x] **Filtering / querying** — `filter`, `first`, `update_where`
- [x] **Deleting** — remove records and their files
- [x] **Commit** — manual `commit` / `commit_all`
- [x] **Change hooks & audit trail** — subscribe to changes; attribute an actor
- [x] **Relationships** — cross-collection `Ref` resolved via a shared `Registry`
- [x] **Blobs** — store unstructured content outside the record JSON, read lazily
- [x] **Pluggable layouts** — swap how records/blobs are laid out on disk
- [x] **Non-Pydantic objects** — support for other JSON-serializable objects
- [ ] **Partial / lazy load** — load only the records you need
- [ ] **Transactions**
- [ ] **Indexes** — speed up queries
- [ ] **Thread locking / multi-threading**
- [ ] **Object expiration** — use as a cache / session store
- [ ] **Custom serialization** hooks
- [ ] **Performance tests & comparisons**

## Development

```bash
git clone https://github.com/42dotmk/slowstore
cd slowstore
poetry install          # install dependencies
poetry run pytest -v    # run the test suite  (or: make test)
```

Handy `make` targets: `make test`, `make build`, `make publish`.

Contributions are welcome — please open an issue to discuss substantial changes
first, and make sure `poetry run pytest` passes before opening a pull request.

## License

This project is licensed under the MIT License — see the [LICENSE](LICENSE) file for details.
