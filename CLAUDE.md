# CLAUDE.md

Guidance for Claude Code (and other agents) working in this repository.

## What this is

**Slowstore** is a zero-setup, on-disk object store for Python. It maps Pydantic
models (or dataclasses / plain classes — see the codec note below) to JSON files
on disk, auto-syncs mutations, and keeps a per-record change log with undo. It is
deliberately **slow and single-threaded** — built for prototyping/debugging DX,
not production throughput. Published to PyPI as `slowstore`. Requires Python
3.10+; **Pydantic is optional** (`slowstore[pydantic]`, only for Pydantic models).

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
| `codec.py` | `Codec` + `PydanticCodec`/`DataclassCodec`/`PlainCodec` + `codec_for()`. Owns model↔dict (de)serialization and blob/ref **field discovery**, decoupling the store from any one model system. |
| `utils.py` | `json_default_serializer` (datetime → ISO, etc.). |

`__init__.py` is the public API surface — anything exported there is public.

## Architecture & key invariants

- **The model system is pluggable via `codec.py`.** `Store` never touches
  `model_dump`/`model_fields` directly — it goes through `self.codec` for
  serialization (`to_dict`/`from_dict`) and blob/ref field discovery
  (`special_fields`). `codec_for(cls)` auto-selects: Pydantic `BaseModel` →
  `PydanticCodec` (full type revival), `@dataclass` → `DataclassCodec`, anything
  else → `PlainCodec` (revives via `__new__` + `setattr`, no `__init__` call).
  Pass `codec=` to override. Blobs/refs round-trip in all backends via their
  `__blob__`/`__ref__` markers; the non-Pydantic backends also revive
  `datetime`/`date` and nested dataclasses but not arbitrary rich types.
- **`BlobRef` and `Ref` are plain classes** (not `BaseModel`). Their persisted
  state lives in ordinary attributes; `__to_descriptor__()`/`__from_value__()`
  do (de)serialization for the codec. They stay usable as fields inside a Pydantic
  model via a lazy `__get_pydantic_core_schema__` hook (pydantic is imported only
  when that hook actually runs).
- **Pydantic is an optional dependency** (`slowstore[pydantic]` extra). No module
  imports it at load time: `blob.py`/`ref.py` import `pydantic_core` lazily inside
  the schema hook, and `codec.py` detects Pydantic models by **duck-typing**
  (`_is_pydantic_model`: has `model_fields` + `model_validate`) rather than
  `isinstance(cls, BaseModel)`. So `import slowstore` and dataclass/plain stores
  work with pydantic uninstalled. Keep it that way — don't add a top-level
  `import pydantic` anywhere; `tests/test_codecs.py::test_pydantic_is_optional`
  guards this by running in an interpreter with `pydantic*` imports blocked.
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
- **Lists of blobs/refs** are supported: declare `list[BlobRef]` or
  `list[Reference[T]]`/`list[Ref]`. Each element is auto-wrapped, bound, and
  persisted independently. List-field names are tracked in
  `__blob_list_field_names__`/`__ref_list_field_names__` (subsets of the scalar
  name sets). Mutation is by **whole-list reassignment** — in-place `.append()` on
  the returned list is not tracked (same proxy limitation as elsewhere). List
  blobs get stable per-element ids (`__assign_blob_list_ids__`); `Store.commit`
  reconciles the previous vs current id set (`proxy.__blob_list_ids__`) to delete
  the sidecars of removed elements.
- **Blob filenames are derived, not chosen.** A blob's on-disk name comes from
  `{key}.{field}` (+ blob `id`, which defaults to the field name; list blobs use a
  per-element id like `{key}.{field}.0.bin`), not from any caller-supplied
  filename. `BlobRef.from_path()` reads bytes but does not adopt the source
  filename. Naming logic lives in `StorageLayout.__blob_suffix__` / `__blob_path__`.
- **Layouts own on-disk shape.** Default is `FilePerRecordLayout` (`{key}.json`
  per record). Others: `JsonCollectionLayout`, `JsonlCollectionLayout` (single
  file), `DirPerObjectLayout` (dir per record). All support blobs and reload
  identically; there is no migration between formats.
- **Keys are sanitized** into filesystem-safe names via `StorageLayout.sanitize`
  (lowercases, replaces unsafe chars with `_`).
- **Single-threaded within a process.** Do not add thread-safety assumptions.
  Cross-*process* safety exists as an opt-in: `lock.py`'s reentrant `FileLock`
  (flock; msvcrt on Windows) guards commits when `lock_writes=True`, and all
  record/blob writes are atomic (`layout._atomic_write`: temp + `os.replace`).
- **The serializer never writes silent nulls** (since 2.3):
  `utils.json_default_serializer` handles datetime/date/time, Decimal (as str),
  UUID, Enum, set, BlobRef/Ref descriptors and pydantic/dataclass values
  (duck-typed), and raises `TypeError` for anything else. Persisted change
  entries exclude dunder instance attrs (`__orig_class__`). The
  dataclass/plain codecs revive Decimal/UUID/Enum in `_revive_typed`.
- **On-disk change detection (opt-in).** The store never polls for external file
  changes, but two mechanisms guard against silently clobbering them:
  - `store.reload()` / `store.reload(key)` re-read from disk (invalidating the
    collection layout cache); `reload(keep_dirty=True)` preserves uncommitted
    local edits. `layout.load_record` / `invalidate_cache` back this.
  - Optimistic concurrency on write: the store stamps each record with its file's
    `(mtime, size)` in `__stamps__` (see `layout.record_stamp` / `blob_stamp`),
    captured at load and refreshed after every write (`__refresh_stamp__` —
    collection layouts re-baseline **all** keys since one file backs them). On
    commit, `__detect_conflict__` compares the stamp; the `on_conflict` policy
    (`ConflictPolicy` in `errors.py`: `overwrite` default / `error` / `reload`)
    decides. Blobs carry their own `_stamp` and run the same check in
    `BlobRef._flush`. `overwrite` = pre-existing behavior, so this is inert until
    a caller opts in. No cross-file atomicity (record and blob are separate files).

## Store construction

`Store(cls, directory, **kwargs)`. Options (all keyword): `key_selector`,
`save_on_change` (True), `save_on_exit` (True), `load_on_start` (False),
`encoding` ("utf-8"), `ensure_ascii` (False), `layout`, `codec`, `registry`,
`name`, `get_identity`, `on_conflict` (`"overwrite"`),
`save_changes_to_file` (True), `load_changes_from_file` (False),
`max_changes` (None — cap on per-record change history, trimmed at commit),
`lock_writes` (False — wrap each commit/delete in the inter-process
`FileLock`; `store.lock()` exposes it for wider critical sections),
`lazy` (False — point-read mode: no directory scan on load, records read on
first access, enumeration completes the scan; per-record layouts only).
`codec` defaults to `codec_for(cls)`.
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
