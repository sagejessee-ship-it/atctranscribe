"""Source-relative path rules.

Corpus identity is ``(source, relative_path)``. Relative paths always use ``/``
separators, are NFC-normalised, and can never escape the source root. Absolute
mount paths are machine configuration and never appear here.
"""

from __future__ import annotations

import posixpath
import re
import unicodedata

_DRIVE = re.compile(r"^[A-Za-z]:")


class InvalidRelativePath(ValueError):
    pass


def _check_components(value: str, *, allow_empty: bool) -> str:
    if value == "":
        if allow_empty:
            return value
        raise InvalidRelativePath("relative path must not be empty")
    if "\x00" in value:
        raise InvalidRelativePath("relative path contains NUL")
    if "\\" in value:
        raise InvalidRelativePath(f"relative path must use '/' separators: {value!r}")
    if value.startswith("/") or _DRIVE.match(value):
        raise InvalidRelativePath(f"relative path must not be absolute: {value!r}")
    for part in value.split("/"):
        if part in ("", ".", ".."):
            raise InvalidRelativePath(f"relative path has an invalid component: {value!r}")
    return value


def normalize_relative_path(value: str) -> str:
    """Validate and normalise a source-relative file path."""
    return _check_components(unicodedata.normalize("NFC", value), allow_empty=False)


def normalize_relative_dir(value: str) -> str:
    """Validate a source-relative directory. ``""`` is the source root."""
    value = unicodedata.normalize("NFC", value).rstrip("/")
    return _check_components(value, allow_empty=True)


def join_relative(directory: str, name: str) -> str:
    return normalize_relative_path(f"{directory}/{name}" if directory else name)


def parent_dir(relative_path: str) -> str:
    return posixpath.dirname(relative_path)


def basename(relative_path: str) -> str:
    return posixpath.basename(relative_path)


def is_within(relative_dir: str, scope_prefix: str) -> bool:
    """True when ``relative_dir`` is ``scope_prefix`` or lies beneath it."""
    if scope_prefix == "":
        return True
    return relative_dir == scope_prefix or relative_dir.startswith(scope_prefix + "/")
