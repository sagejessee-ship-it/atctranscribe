"""Read-only access to a mounted corpus source.

This module is the only code in AeroChorus that touches source audio. It lists
directories, stats entries and opens files with mode ``"rb"``. It has no code
path that writes, renames, deletes or changes metadata, and it never follows
symbolic links. Tests enforce this.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path


class SourceUnavailable(OSError):
    """The source root cannot be trusted right now (unmounted, offline, empty)."""


@dataclass(frozen=True)
class Availability:
    ok: bool
    reason: str | None = None


@dataclass(frozen=True)
class FileEntry:
    name: str
    size: int
    mtime_ns: int


@dataclass(frozen=True)
class DirectoryListing:
    relative_dir: str
    mtime_ns: int
    files: list[FileEntry] = field(default_factory=list)
    subdirs: list[str] = field(default_factory=list)
    # Symlinks, special files and hidden entries: never followed or read.
    skipped: list[str] = field(default_factory=list)


class ReadOnlyCorpusReader:
    def __init__(self, root: Path, sentinel_paths: Sequence[str] = ()) -> None:
        self.root = Path(root)
        self.sentinel_paths = list(sentinel_paths)

    def _local(self, relative: str) -> Path:
        return self.root.joinpath(*relative.split("/")) if relative else self.root

    def check_available(self) -> Availability:
        try:
            st = os.stat(self.root)
            if not stat.S_ISDIR(st.st_mode):
                return Availability(False, f"source root is not a directory: {self.root}")
            with os.scandir(self.root) as entries:
                if next(entries, None) is None:
                    return Availability(
                        False, f"source root is empty (share not mounted?): {self.root}"
                    )
            for sentinel in self.sentinel_paths:
                if not os.path.exists(self._local(sentinel)):
                    return Availability(False, f"sentinel path missing: {sentinel}")
        except OSError as exc:
            return Availability(False, f"{type(exc).__name__}: {exc}")
        return Availability(True)

    def require_available(self) -> None:
        availability = self.check_available()
        if not availability.ok:
            raise SourceUnavailable(availability.reason)

    def is_dir(self, relative_dir: str) -> bool:
        return os.path.isdir(self._local(relative_dir)) and not os.path.islink(
            self._local(relative_dir)
        )

    def dir_mtime_ns(self, relative_dir: str) -> int:
        return os.stat(self._local(relative_dir)).st_mtime_ns

    def list_directory(self, relative_dir: str) -> DirectoryListing:
        path = self._local(relative_dir)
        # Stat before listing: an entry created in between makes the stored
        # mtime older than reality, which only causes a harmless re-list later.
        mtime_ns = os.stat(path).st_mtime_ns
        files: list[FileEntry] = []
        subdirs: list[str] = []
        skipped: list[str] = []
        with os.scandir(path) as entries:
            for entry in entries:
                if entry.name.startswith(".") or entry.is_symlink():
                    skipped.append(entry.name)
                elif entry.is_dir(follow_symlinks=False):
                    subdirs.append(entry.name)
                elif entry.is_file(follow_symlinks=False):
                    st = entry.stat(follow_symlinks=False)
                    files.append(FileEntry(entry.name, st.st_size, st.st_mtime_ns))
                else:
                    skipped.append(entry.name)
        files.sort(key=lambda f: f.name)
        return DirectoryListing(relative_dir, mtime_ns, files, sorted(subdirs), sorted(skipped))

    def read_bytes(self, relative_path: str) -> bytes:
        with open(self._local(relative_path), "rb") as fh:
            return fh.read()
