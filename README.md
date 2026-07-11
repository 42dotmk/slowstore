# Slowstore
Easy to use, object store that stores your objects as JSON on disk.
It directly maps your python objects to JSON and every object is auto synced to disk (by default). The on-disk layout is pluggable — one file per record (default), a single file per collection, or a directory per object (see [Storage layouts](#storage-layouts)). 
It is designed to be easy to plug into your program, without need of servers, connection strings, or any setup just set the directory where you would like to store your files.

**Slowstore*  it is intended to be used for exploration and debugging of my program behaviour.

#### Installation
```bash
pip install slowstore
```

## Usage
You can create a new Slowstore instance by calling the constructor:

```python
from slowstore import Store
from pydantic import BaseModel

class SampleModel(BaseModel):
    name: str
    age: int = 0
    def birthday(self):
        self.age += 1

store = Store[SampleModel](SampleModel, "mydata", key_selector=lambda x: x.name)  # `key_selector` function is used to compose the key by which the object is tracked in the store)
m = SampleModel(name="john", age=30)
dennis = store.set(SampleModel(name="denis", age=32))  # returns a tracked proxy
store.set(m)
dennis.name = "DENIS"  # here the name in the json will also change from "dennis" to "DENIS"
dennis.birthday()      # will also trigger another change in the age field and it will be reflected in the json file. 
```
`mydata/dennis.json` after running this program will look like:
```json
{
  "name": "DENIS",
  "age": 33,
  "__key__": "denis",
  "__changes__": [
    {
      "kind": "UPDATE",
      "prop_name": "age",
      "prev_val": 32,
      "new_val": 33,
      "key": "denis",
      "date": "2024-08-28T19:04:12.840353"
    },
    {
      "kind": "UPDATE",
      "prop_name": "name",
      "prev_val": "denis",
      "new_val": "DENIS",
      "key": "denis",
      "date": "2024-08-28T19:04:12.840216"
    },
    {
      "kind": "ADD",
      "key": "denis",
      "date": "2024-08-28T19:04:12.840100"
    }
  ]
}
```
`Store` tracks what happened in your small program at all times.
You can toggle the `save_on_change` flat at any time after the store is created, 
or you can also set the `save_on_change` flag to your liking in the constructor of the `store`

```python
store.save_on_change = False 
```

After this code is executed, you will need to run `store.commit(some_model)` or store.commit_all() in order to persist the changes. 

### Commit the changes to the store
If the store's `save_on_change` flag is not set to `True`, you can commit the changes manually.
```python
store.commit(s1)
store.commit(s2)
# or just commit all changes
store.commit_all() 
```

### Undo/Redo

Every slowstore item is a proxy object around the object you added to the store. This allows you to access the object's attributes and adds undo/redo/dirty functionality

```python
s1.undo()
s1.redo()
```

### Querying the store

```python
# get all items, returns an iterator
all_items = store.all()

# yield all items that match a condition
filtered_items = store.filter(lambda x: x.age > 30)

# get the first item that matches a condition
first_item = store.first(lambda x: x.age > 30)
```

### Check if some object or it's key is in the store

```python
if s2 in store:
    print("s2 is in store")
else:
    print("s2 is not in store")
```

### Deleting items
Any of the following will delete the item from the store, along with the file that contains the data

```python
store.delete(s1)
del store[s2]
key = "some_key"
store.delete(key)
```
### Blobs (unstructured content)

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

### References (relationships across collections)

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

### Storage layouts

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

## How is the data organized?

By default each record is a JSON file named after its key, inside the store's directory
(the `FilePerRecordLayout`). This is configurable — see [Storage layouts](#storage-layouts)
for single-file (`collection.json` / `collection.jsonl`) and directory-per-object schemes.
Unstructured content is stored outside the record JSON as [blobs](#blobs-unstructured-content),
and [references](#references-relationships-across-collections) link records across
collections.

## Features

At the moment Slowstore works with Pydantic models, but I plan to add support for other types of objects that are serializable to JSON.

- [X] **Save on change**: Set the store to save the data every time a property is changed
- [X] **Undo/Redo**: Undo and redo changes to the object
- [X] **Dirty**: Check if the object has been changed
- [X] **Filtering**: Filter/Query the items in the store
- [X] **Deleting**: Delete items from the store
- [X] **Commit**: Commit changes to the store
- [ ] **Partial load**: Load only the items you need, and lazy load the rest
- [ ] **Transactions**: Add support for transactions
- [X] **Non-Pydantic objects**: Add support for other types of objects
- [ ] **Indexes**: Add indexes to the store to speed up queries
- [X] **Relationships**: Reference records in other collections (`Ref`), resolved lazily through a shared `Registry`
- [X] **Blobs**: Store unstructured content outside the record JSON (`BlobRef`), read lazily
- [X] **Pluggable layouts**: Swap how records/blobs are laid out on disk (`StorageLayout`)
- [ ] Thread locking/Multi-threading support
- [ ] Object expiration support (so you can use it as a cache and session store)
- [ ] Performance tests and comparisons
- [ ] **Custom serialization**: Add support for custom serialization/deserialization


## How it works

The slowtore instance behaves like a dictionary of objects you added to it. Instead of storing the object it creates a proxy object that stores the object's state and the changes you made to it. 

When you commit the changes, the proxy object is updated with the new state, and the changes are saved to disk.

When you undo/redo the changes, the proxy object is updated with the previous state, and the changes are saved to disk.

Check the ModelProxy class for more details.

## Important

Slow store is **slow** and it is not intended to be used in multithreaded contexts. It's primarily created to provide a good DX while working on a certain feature.

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.



