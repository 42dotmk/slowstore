# CLAUDE.md

Guidance for Claude Code (and other agents) working in this repository.

## What this is

**Slowstore** is a zero-setup, on-disk object store for Python. It maps Pydantic
models to JSON files on disk, auto-syncs mutations, and keeps a per-record change
log with undo. It is deliberately **slow and single-threaded** — built for
prototyping/debugging DX, not production throughput. Published to PyPI as
`slowstore`. Requires Python 3.10+, Pydantic v2.

User-facing docs live in [`README.md`](README.md) — keep it in sync when you
change public behavior.

## Module map (`slowstore/`)

| File | Responsibility |
|---|---|
| `store.py` | `Store[T]` — the collection: CRUD, querying, commit, load, change hooks, context manager. The main entry point. |
| `proxy.py` | `Proxy[T]` — transparent wrapper around a model. Intercepts attribute sets to record changes and persist; forwards everything else to `.model`. |
| `change.py` | `Change` + `ChangeKind` (ADD/UPDATE/DELETE). One entry in a record's audit log; knows how to `undo`/`redo` itself. |
| `blob.py` | `BlobRef` + `CacheMode` — unstructured/binary content stored in sidecar files, read lazily. |
| `ref.py` | `Ref` + `Reference[T]` — cross-collection relationships, resolved lazily; `Reference[T]` types the field as `T` for editor autocomplete. |
| `registry.py` | `Registry` + `default_registry()` — resolves/join references across stores. |
| `layout.py` | `StorageLayout` and the four built-in layouts. Owns the on-disk path scheme and blob read/write. |
| `utils.py` | `json_default_serializer` (datetime → ISO, etc.). |

`__init__.py` is the public API surface — anything exported there is public.

## Architecture & key invariants

- **Everything is a proxy.** `store.set/get/...` return a `Proxy[T]` (typed as `T`).
  It looks like the model but is not — `proxy.model` is the real Pydantic object.
  `Proxy.__setattr__` routes non-special attribute writes through
  `__update_model_prop__`, which records a `Change`, marks `is_dirty`, and commits
  if `save_on_change`.
- **`__special_fields__` in `proxy.py`** is the allowlist of names that belong to
  the proxy itself (e.g. `model`, `store`, `is_dirty`, `commit`, `__changes__`).
  **If you add a public method/attribute to `Proxy`, add its name here**, or
  `__getattr__`/`__setattr__` will forward it to the wrapped model and it will break.
- **Undo is `proxy.__reset__(n)`** — reverts the last `n` changes (default: all).
  There is **no public `proxy.undo()`/`proxy.redo()`** — `Change.undo/redo` are
  per-change internals. Don't document or call `proxy.undo()`.
- **Change log** lives on both `store.__changes__` and each `proxy.__changes__`,
  newest first (`insert(0, ...)`). Persisted into the record JSON as `__changes__`
  when `save_changes_to_file` (default on).
- **Blob/Ref auto-wrapping.** Assigning a plain `str`/`bytes` to a `BlobRef` field,
  or a record to a `Reference` field, auto-wraps it (`__coerce_special__`). Fields
  declared as blob/ref are discovered from the model's `model_fields` annotations
  in `__bind_special_fields__`.
- **Blob filenames are derived, not chosen.** A blob's on-disk name comes from
  `{key}.{field}` (+ blob `id`, which defaults to the field name), not from any
  caller-supplied filename. `BlobRef.from_path()` reads bytes but does not adopt
  the source filename. Naming logic lives in `StorageLayout.__blob_suffix__` /
  `__blob_path__`.
- **Layouts own on-disk shape.** Default is `FilePerRecordLayout` (`{key}.json`
  per record). Others: `JsonCollectionLayout`, `JsonlCollectionLayout` (single
  file), `DirPerObjectLayout` (dir per record). All support blobs and reload
  identically; there is no migration between formats.
- **Keys are sanitized** into filesystem-safe names via `StorageLayout.sanitize`
  (lowercases, replaces unsafe chars with `_`).
- **Single-threaded, no locking.** Do not add concurrency assumptions.

## Store construction

`Store(cls, directory, **kwargs)`. Options (all keyword): `key_selector`,
`save_on_change` (True), `save_on_exit` (True), `load_on_start` (False),
`encoding` ("utf-8"), `ensure_ascii` (False), `layout`, `registry`, `name`,
`get_identity`, `save_changes_to_file` (True), `load_changes_from_file` (False).
`Store` is a context manager and flushes on exit when `save_on_exit`.

## Development workflow

```bash
poetry install            # deps
poetry run pytest -v      # tests  (also: make test)
poetry build              # build  (make build)
```

- Tests are in `tests/` (`test_slowstore.py`, `test_blob_ref.py`,
  `test_layouts.py`). Runnable examples in `examples/`.
- **Always run `poetry run pytest` before committing** behavior changes.
- Versioning is manual via Poetry: `poetry version patch|minor|major`. The single
  source of truth is `version` in `pyproject.toml`. `make publish` runs
  build → test → version bump → tag → push → publish.
- Release tags are the bare semver (e.g. `2.1.0`), created with `git tag -a`.

## Conventions & gotchas

- Match the existing style: internal/dunder names (`__reset__`, `__changes__`,
  `__blob_fields__`) are used liberally for proxy/model separation — follow that
  pattern rather than introducing new public surface casually.
- When adding public symbols, export them from `slowstore/__init__.py` and
  document them in `README.md`.
- Keep `README.md` accurate — it previously drifted (e.g. documented a
  nonexistent `store.all()` / `proxy.undo()`). Verify against the code before
  documenting an API.
- The `test_data/` directory is git-ignored scratch output from tests/examples.
