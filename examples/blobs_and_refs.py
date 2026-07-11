"""Demonstrates blob content and cross-collection references.

Run with:  python examples/blobs_and_refs.py
Creates two collections under ./example_data and prints what lands on disk.
"""

import os
import shutil
from typing import Optional

from pydantic import BaseModel

from slowstore import BlobRef, CacheMode, Ref, Reference, Registry, Store

BASE = "./example_data"
shutil.rmtree(BASE, ignore_errors=True)


class Person(BaseModel):
    name: str


class Document(BaseModel):
    title: str
    # unstructured content: kept in a sidecar file, not inline in the JSON
    body: Optional[BlobRef] = None
    # a lazy, generic pointer to a Person in another collection. Typed as Person
    # (so editors autocomplete Person's fields) but stored as a Ref.
    author: Optional[Reference[Person]] = None


# A shared registry lets references resolve across collections.
registry = Registry()

people = Store[Person](
    Person, os.path.join(BASE, "people"), key_selector=lambda p: p.name, registry=registry
)
docs = Store[Document](
    Document, os.path.join(BASE, "docs"), key_selector=lambda d: d.title, registry=registry
)

# --- create records ----------------------------------------------------------
ada = people.set(Person(name="ada"))

doc = docs.set(Document(title="intro"))
# direct assignment: text/bytes become a BlobRef, a record becomes a Ref.
doc.body = "# Intro\n\nlots of content..."
doc.author = ada
# use the explicit forms when you need options, e.g. content-type or cache mode:
#   doc.body = BlobRef.from_text("...", content_type="text/markdown")
#   doc.author = Ref.to(ada, cache=CacheMode.EVERY_TIME)

print("Record JSON (docs/intro.json) — note body/author are compact descriptors:")
with open(os.path.join(BASE, "docs", "intro.json")) as f:
    print(f.read())

print("Blob bytes live in the sidecar docs/intro.body.bin:")
print("  ", open(os.path.join(BASE, "docs", "intro.body.bin"), "rb").read())

# --- reload in fresh stores (simulating a new process) -----------------------
registry2 = Registry()
Store[Person](Person, people.directory, key_selector=lambda p: p.name, registry=registry2).load()
docs2 = Store[Document](
    Document, docs.directory, key_selector=lambda d: d.title, registry=registry2
).load()

reloaded = docs2.get("intro")
print("\nLazily-read blob text after reload:", repr(reloaded.body.text))
# transparent forwarding: reloaded.author behaves like the referenced Person
print("Lazily-resolved reference:", reloaded.author.name)

print("\nJoin docs -> author across collections:")
for document, author in registry2.join("docs", "author"):
    print("  ", document.title, "written by", author.name if author else "<missing>")
