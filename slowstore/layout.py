"""Pluggable on-disk layouts for a :class:`slowstore.Store`.

A ``StorageLayout`` owns *where and how* records and their blobs live on disk.
``Store`` never touches the filesystem directly -- it hands the layout plain
dicts (for records) and bytes (for blobs) and lets the layout decide the path
scheme.  Available layouts:

* :class:`FilePerRecordLayout` (default) -- one JSON file per record, blob
  sidecars beside it. Reproduces the historical behaviour.
* :class:`JsonCollectionLayout` -- every record in a single ``collection.json``.
* :class:`JsonlCollectionLayout` -- every record on its own line in
  ``collection.jsonl`` (line-delimited JSON; cheap to scan/append).
* :class:`DirPerObjectLayout` -- one directory per record (named by its key)
  holding the record JSON and that record's blobs together.

Records are loaded in bulk via :meth:`StorageLayout.load_records`; writes and
deletes are per-key so any layout can service them (collection layouts do a
read-modify-write of their single file).
"""

import json
import os
import shutil
import tempfile
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from .utils import json_default_serializer

if TYPE_CHECKING:
    from .store import Store

# A cheap "did the file change" fingerprint: (mtime_ns, size). Coarser than a
# content hash but a single ``os.stat`` -- ``size`` catches same-mtime edits of a
# different length, which covers the common hand-edit case.
def _stat_stamp(path: str) -> "tuple[int, int] | None":
    """``(mtime_ns, size)`` for ``path``, or ``None`` if it doesn't exist."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _atomic_write(path: str, payload: "str | bytes", encoding: str | None = None) -> None:
    """Write ``payload`` to ``path`` via a temp file + ``os.replace``.

    Readers never observe a partially written file, and a crash mid-write
    leaves the previous version intact instead of a truncated one. The ``.tmp``
    suffix keeps a leftover temp file invisible to ``load_records`` filters.
    (No fsync: crash-atomicity for the file's content, not power-loss
    durability — in keeping with what a JSON-on-disk store can promise.)
    """
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(
        dir=directory, prefix=f".{os.path.basename(path)}-", suffix=".tmp"
    )
    try:
        if isinstance(payload, str):
            with os.fdopen(fd, "w", encoding=encoding) as f:
                _ = f.write(payload)
        else:
            with os.fdopen(fd, "wb") as f:
                _ = f.write(payload)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


class StorageLayout(ABC):
    """Strategy that persists records and blobs for a store.

    ``bind`` is called once by the store during construction so the layout can
    read ``directory``/``encoding``/``ensure_ascii`` off it.
    """

    def bind(self, store: "Store[Any]") -> None:
        self.store = store
        self.directory: str = store.directory
        self.encoding: str = store.encoding
        self.ensure_ascii: bool = store.ensure_ascii
        os.makedirs(self.directory, exist_ok=True)

    def sanitize(self, name: str | int) -> str:
        """Turn a record key into a filesystem-safe token (lossy)."""
        name = str(name)
        return (
            name.replace("/", "_")
            .replace("\\", "_")
            .replace(":", "_")
            .replace(" ", "_")
            .replace(".", "_")
            .replace("!", "_")
            .replace("?", "_")
            .replace("&", "_")
            .replace(";", "_")
            .replace("|", "_")
            .replace("/", "_")
            .lower()
        )

    def __blob_suffix__(self, field: str, blob_id: str) -> str:
        # scalar blob-per-field keeps the name ``{field}``; a distinct blob id
        # (reserved for future list-of-blobs) is appended.
        return field if blob_id == field else f"{field}.{blob_id}"

    def __dumps__(self, data: dict[str, Any], indent: int | None = 2) -> str:
        return json.dumps(
            data,
            indent=indent,
            default=json_default_serializer,
            ensure_ascii=self.ensure_ascii,
        )

    # -- records ---------------------------------------------------------------

    @abstractmethod
    def load_records(
        self,
    ) -> "tuple[dict[str, dict[str, Any]], list[tuple[str, Exception]]]":
        """Return ``{key: raw_record_dict}`` for all records, plus a list of
        ``(locator, error)`` for records that could not be read/parsed."""

    @abstractmethod
    def write_record(self, key: str, data: dict[str, Any]) -> None:
        """Persist ``data`` (already includes ``__key__``/``__changes__``)."""

    @abstractmethod
    def delete_record(self, key: str) -> None:
        """Remove the record's storage. Missing storage is not an error."""

    # -- blobs -----------------------------------------------------------------

    @abstractmethod
    def read_blob(self, key: str, field: str, blob_id: str) -> bytes: ...

    @abstractmethod
    def write_blob(self, key: str, field: str, blob_id: str, data: bytes) -> None: ...

    @abstractmethod
    def delete_blob(self, key: str, field: str, blob_id: str) -> None: ...

    @abstractmethod
    def has_blob(self, key: str, field: str, blob_id: str) -> bool: ...

    @abstractmethod
    def blob_uri(self, key: str, field: str, blob_id: str) -> str:
        """A stable locator for the blob (e.g. a file path). For diagnostics."""

    # -- change detection / freshness ------------------------------------------

    per_record_files: bool = True
    """Whether each record has its own backing file. ``False`` for collection
    layouts, where every record shares one file (so a stamp is file-granular)."""

    def __record_locator__(self, key: str) -> str | None:
        """Filesystem path whose mtime/size fingerprints the record, or ``None``
        if the layout has no such path. Overridden by the concrete layouts."""
        return None

    def record_stamp(self, key: str) -> "tuple[int, int] | None":
        """Current ``(mtime_ns, size)`` for the record's backing file."""
        loc = self.__record_locator__(key)
        return _stat_stamp(loc) if loc else None

    def blob_stamp(
        self, key: str, field: str, blob_id: str
    ) -> "tuple[int, int] | None":
        """Current ``(mtime_ns, size)`` for a blob's sidecar file."""
        if not self.has_blob(key, field, blob_id):
            return None
        return _stat_stamp(self.blob_uri(key, field, blob_id))

    def invalidate_cache(self) -> None:
        """Drop any in-memory cache so the next read hits disk. No-op unless the
        layout caches (the collection layouts override this)."""

    def load_record(self, key: str) -> dict[str, Any] | None:
        """Re-read a single record's raw dict from disk (bypassing caches), or
        ``None`` if it is no longer there. Default re-reads everything; the
        per-file layouts override to read just the one file."""
        records, _ = self.load_records()
        return records.get(key)


class _SidecarBlobs(StorageLayout):
    """Blob mixin: each blob is a sidecar ``{directory}/{skey}.{suffix}.bin``.

    Shared by the layouts that keep blobs in the collection directory
    (:class:`FilePerRecordLayout` and the collection-file layouts).
    """

    def __blob_path__(self, key: str, field: str, blob_id: str) -> str:
        stem = self.sanitize(key)
        return f"{self.directory}/{stem}.{self.__blob_suffix__(field, blob_id)}.bin"

    def read_blob(self, key: str, field: str, blob_id: str) -> bytes:
        with open(self.__blob_path__(key, field, blob_id), "rb") as f:
            return f.read()

    def write_blob(self, key: str, field: str, blob_id: str, data: bytes) -> None:
        _atomic_write(self.__blob_path__(key, field, blob_id), data)

    def delete_blob(self, key: str, field: str, blob_id: str) -> None:
        path = self.__blob_path__(key, field, blob_id)
        if os.path.exists(path):
            os.remove(path)

    def has_blob(self, key: str, field: str, blob_id: str) -> bool:
        return os.path.exists(self.__blob_path__(key, field, blob_id))

    def blob_uri(self, key: str, field: str, blob_id: str) -> str:
        return self.__blob_path__(key, field, blob_id)


class FilePerRecordLayout(_SidecarBlobs):
    """One JSON file per record at ``{directory}/{sanitize(key)}.json``."""

    def __record_path__(self, key: str) -> str:
        return f"{self.directory}/{self.sanitize(key)}.json"

    def load_records(
        self,
    ) -> "tuple[dict[str, dict[str, Any]], list[tuple[str, Exception]]]":
        records: dict[str, dict[str, Any]] = {}
        errors: list[tuple[str, Exception]] = []
        for filename in os.listdir(self.directory):
            if not filename.endswith(".json"):
                continue
            try:
                with open(
                    f"{self.directory}/{filename}", "r", encoding=self.encoding
                ) as f:
                    d = json.load(f)
                records[d["__key__"]] = d
            except Exception as e:
                errors.append((filename, e))
        return records, errors

    def write_record(self, key: str, data: dict[str, Any]) -> None:
        _atomic_write(self.__record_path__(key), self.__dumps__(data), self.encoding)

    def delete_record(self, key: str) -> None:
        path = self.__record_path__(key)
        if os.path.exists(path):
            os.remove(path)

    def __record_locator__(self, key: str) -> str | None:
        return self.__record_path__(key)

    def load_record(self, key: str) -> dict[str, Any] | None:
        path = self.__record_path__(key)
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding=self.encoding) as f:
            return json.load(f)


class _CollectionFileLayout(_SidecarBlobs):
    """Base for layouts that keep every record in a single collection file.

    Writes are read-modify-write of the whole file (Slowstore is deliberately
    slow); an in-memory cache avoids re-parsing the file on every write.
    """

    filename: str = ""  # overridden by subclasses

    def bind(self, store: "Store[Any]") -> None:
        super().bind(store)
        self.__path__: str = f"{self.directory}/{self.filename}"
        self.__cache__: dict[str, dict[str, Any]] | None = None

    @abstractmethod
    def __read_file__(
        self,
    ) -> "tuple[dict[str, dict[str, Any]], list[tuple[str, Exception]]]": ...

    @abstractmethod
    def __write_file__(self, records: dict[str, dict[str, Any]]) -> None: ...

    # every record shares one file, so a stamp fingerprints the whole file.
    per_record_files: bool = False

    def __record_locator__(self, key: str) -> str | None:
        return self.__path__

    def invalidate_cache(self) -> None:
        self.__cache__ = None

    def load_record(self, key: str) -> dict[str, Any] | None:
        # re-read the file from disk so an external edit is picked up, and
        # refresh the cache to match.
        self.__cache__ = None
        records, _ = self.__read_file__()
        self.__cache__ = dict(records)
        return records.get(key)

    def __ensure_cache__(self) -> dict[str, dict[str, Any]]:
        if self.__cache__ is None:
            self.__cache__, _ = self.__read_file__()
        return self.__cache__

    def load_records(
        self,
    ) -> "tuple[dict[str, dict[str, Any]], list[tuple[str, Exception]]]":
        records, errors = self.__read_file__()
        self.__cache__ = dict(records)
        return records, errors

    def write_record(self, key: str, data: dict[str, Any]) -> None:
        cache = self.__ensure_cache__()
        cache[key] = data
        self.__write_file__(cache)

    def delete_record(self, key: str) -> None:
        cache = self.__ensure_cache__()
        if key in cache:
            del cache[key]
            self.__write_file__(cache)


class JsonCollectionLayout(_CollectionFileLayout):
    """All records in a single ``collection.json`` keyed by record key."""

    filename: str = "collection.json"

    def __read_file__(
        self,
    ) -> "tuple[dict[str, dict[str, Any]], list[tuple[str, Exception]]]":
        records: dict[str, dict[str, Any]] = {}
        errors: list[tuple[str, Exception]] = []
        if not os.path.exists(self.__path__):
            return records, errors
        try:
            with open(self.__path__, "r", encoding=self.encoding) as f:
                raw = json.load(f)
        except Exception as e:
            errors.append((self.filename, e))
            return records, errors
        for outer_key, d in raw.items():
            try:
                records[d["__key__"]] = d
            except Exception as e:
                errors.append((f"{self.filename}[{outer_key}]", e))
        return records, errors

    def __write_file__(self, records: dict[str, dict[str, Any]]) -> None:
        obj = {key: data for key, data in records.items()}
        _atomic_write(self.__path__, self.__dumps__(obj), self.encoding)


class JsonlCollectionLayout(_CollectionFileLayout):
    """All records in ``collection.jsonl``, one JSON object per line."""

    filename: str = "collection.jsonl"

    def __read_file__(
        self,
    ) -> "tuple[dict[str, dict[str, Any]], list[tuple[str, Exception]]]":
        records: dict[str, dict[str, Any]] = {}
        errors: list[tuple[str, Exception]] = []
        if not os.path.exists(self.__path__):
            return records, errors
        with open(self.__path__, "r", encoding=self.encoding) as f:
            for lineno, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    records[d["__key__"]] = d
                except Exception as e:
                    # a single malformed line is skipped, not the whole file.
                    errors.append((f"{self.filename}:{lineno}", e))
        return records, errors

    def __write_file__(self, records: dict[str, dict[str, Any]]) -> None:
        lines = [self.__dumps__(data, indent=None) for data in records.values()]
        text = "\n".join(lines) + ("\n" if lines else "")
        _atomic_write(self.__path__, text, self.encoding)


class DirPerObjectLayout(StorageLayout):
    """One directory per record (named by its key); the record JSON and that
    record's blobs live inside it.

    Record:  ``{directory}/{sanitize(key)}/record.json``
    Blob:    ``{directory}/{sanitize(key)}/{field}.bin``
    """

    record_filename: str = "record.json"

    def __object_dir__(self, key: str) -> str:
        return f"{self.directory}/{self.sanitize(key)}"

    def __record_path__(self, key: str) -> str:
        return f"{self.__object_dir__(key)}/{self.record_filename}"

    def __blob_path__(self, key: str, field: str, blob_id: str) -> str:
        return f"{self.__object_dir__(key)}/{self.__blob_suffix__(field, blob_id)}.bin"

    # -- records ---------------------------------------------------------------

    def load_records(
        self,
    ) -> "tuple[dict[str, dict[str, Any]], list[tuple[str, Exception]]]":
        records: dict[str, dict[str, Any]] = {}
        errors: list[tuple[str, Exception]] = []
        for name in os.listdir(self.directory):
            obj_dir = f"{self.directory}/{name}"
            record_path = f"{obj_dir}/{self.record_filename}"
            if not os.path.isdir(obj_dir) or not os.path.exists(record_path):
                continue
            try:
                with open(record_path, "r", encoding=self.encoding) as f:
                    d = json.load(f)
                records[d["__key__"]] = d
            except Exception as e:
                errors.append((name, e))
        return records, errors

    def write_record(self, key: str, data: dict[str, Any]) -> None:
        os.makedirs(self.__object_dir__(key), exist_ok=True)
        _atomic_write(self.__record_path__(key), self.__dumps__(data), self.encoding)

    def delete_record(self, key: str) -> None:
        # removing the object directory takes the record's blobs with it.
        obj_dir = self.__object_dir__(key)
        if os.path.isdir(obj_dir):
            shutil.rmtree(obj_dir)

    def __record_locator__(self, key: str) -> str | None:
        return self.__record_path__(key)

    def load_record(self, key: str) -> dict[str, Any] | None:
        path = self.__record_path__(key)
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding=self.encoding) as f:
            return json.load(f)

    # -- blobs (inside the object directory) -----------------------------------

    def read_blob(self, key: str, field: str, blob_id: str) -> bytes:
        with open(self.__blob_path__(key, field, blob_id), "rb") as f:
            return f.read()

    def write_blob(self, key: str, field: str, blob_id: str, data: bytes) -> None:
        os.makedirs(self.__object_dir__(key), exist_ok=True)
        _atomic_write(self.__blob_path__(key, field, blob_id), data)

    def delete_blob(self, key: str, field: str, blob_id: str) -> None:
        path = self.__blob_path__(key, field, blob_id)
        if os.path.exists(path):
            os.remove(path)

    def has_blob(self, key: str, field: str, blob_id: str) -> bool:
        return os.path.exists(self.__blob_path__(key, field, blob_id))

    def blob_uri(self, key: str, field: str, blob_id: str) -> str:
        return self.__blob_path__(key, field, blob_id)
