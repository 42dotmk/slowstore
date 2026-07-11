"""Pluggable on-disk layouts for a :class:`slowstore.Store`.

A ``StorageLayout`` owns *where and how* records and their blob sidecars live on
disk.  ``Store`` no longer touches the filesystem directly -- it hands the layout
plain dicts (for records) and bytes (for blobs) and lets the layout decide the
path scheme.  ``FilePerRecordLayout`` reproduces the historical behaviour: one
JSON file per record at ``{directory}/{sanitized-key}.json`` with blob sidecars
beside it.  New schemes (single file per collection, sharded directories, ...)
can be added without changing ``Store``.
"""

import json
import os
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from .utils import json_default_serializer

if TYPE_CHECKING:
    from .store import Store


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

    # -- records ---------------------------------------------------------------

    @abstractmethod
    def record_locators(self) -> list[str]:
        """Opaque per-record handles used only to enumerate on load.

        For the file layout these are filenames; ``read_record`` maps one back to
        its raw dict.  A malformed record must still yield a locator so the store
        can record the load error against it.
        """

    @abstractmethod
    def read_record(self, locator: str) -> dict[str, Any]:
        """Return the raw dict for a locator produced by ``record_locators``."""

    @abstractmethod
    def write_record(self, key: str, data: dict[str, Any]) -> None:
        """Persist ``data`` (already includes ``__key__``/``__changes__``)."""

    @abstractmethod
    def delete_record(self, key: str) -> None:
        """Remove the record's storage. Missing storage is not an error."""

    # -- blobs -----------------------------------------------------------------

    @abstractmethod
    def read_blob(self, key: str, field: str, blob_id: str) -> bytes:
        ...

    @abstractmethod
    def write_blob(self, key: str, field: str, blob_id: str, data: bytes) -> None:
        ...

    @abstractmethod
    def delete_blob(self, key: str, field: str, blob_id: str) -> None:
        ...

    @abstractmethod
    def has_blob(self, key: str, field: str, blob_id: str) -> bool:
        ...

    @abstractmethod
    def blob_uri(self, key: str, field: str, blob_id: str) -> str:
        """A stable locator for the blob (e.g. a file path). For diagnostics."""


class FilePerRecordLayout(StorageLayout):
    """One JSON file per record, blob sidecars beside it.

    Record:  ``{directory}/{sanitize(key)}.json``
    Blob:    ``{directory}/{sanitize(key)}.{field}.bin``  (scalar blob per field);
             ``{directory}/{sanitize(key)}.{field}.{blob_id}.bin`` when a blob's
             id differs from its field (reserved for future list-of-blobs).
    """

    def __record_path__(self, key: str) -> str:
        return f"{self.directory}/{self.sanitize(key)}.json"

    def __blob_path__(self, key: str, field: str, blob_id: str) -> str:
        stem = self.sanitize(key)
        suffix = field if blob_id == field else f"{field}.{blob_id}"
        return f"{self.directory}/{stem}.{suffix}.bin"

    # -- records ---------------------------------------------------------------

    def record_locators(self) -> list[str]:
        # only the per-record JSON files; blob sidecars (.bin) are skipped.
        return [f for f in os.listdir(self.directory) if f.endswith(".json")]

    def read_record(self, locator: str) -> dict[str, Any]:
        with open(f"{self.directory}/{locator}", "r", encoding=self.encoding) as f:
            return json.load(f)

    def write_record(self, key: str, data: dict[str, Any]) -> None:
        with open(self.__record_path__(key), "w", encoding=self.encoding) as f:
            _ = f.write(
                json.dumps(
                    data,
                    indent=2,
                    default=json_default_serializer,
                    ensure_ascii=self.ensure_ascii,
                )
            )

    def delete_record(self, key: str) -> None:
        path = self.__record_path__(key)
        if os.path.exists(path):
            os.remove(path)

    # -- blobs -----------------------------------------------------------------

    def read_blob(self, key: str, field: str, blob_id: str) -> bytes:
        with open(self.__blob_path__(key, field, blob_id), "rb") as f:
            return f.read()

    def write_blob(self, key: str, field: str, blob_id: str, data: bytes) -> None:
        with open(self.__blob_path__(key, field, blob_id), "wb") as f:
            _ = f.write(data)

    def delete_blob(self, key: str, field: str, blob_id: str) -> None:
        path = self.__blob_path__(key, field, blob_id)
        if os.path.exists(path):
            os.remove(path)

    def has_blob(self, key: str, field: str, blob_id: str) -> bool:
        return os.path.exists(self.__blob_path__(key, field, blob_id))

    def blob_uri(self, key: str, field: str, blob_id: str) -> str:
        return self.__blob_path__(key, field, blob_id)
