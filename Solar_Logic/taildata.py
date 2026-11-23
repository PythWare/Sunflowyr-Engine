from __future__ import annotations
import json, mmap
from pathlib import Path
from typing import Any

EXTERNAL_TAILDATA_FILENAME = "sunflowyr_taildata.json"
AUDIO_VIEW_SUFFIXES = (".wav", ".ogg")
BUILD_FOLDER = "sunflowyr_build"
EDITS_FOLDER = "sunflowyr_edits"
WORK_FOLDERS = (BUILD_FOLDER, EDITS_FOLDER)

def is_work_folder(name: str) -> bool:
    return name.startswith(".") or name.lower() in WORK_FOLDERS

def read_taildata(path: Path) -> dict[str, Any] | None:
    return read_external_taildata(path)

CATALOG_CACHE: dict[tuple[str, int, int], dict[str, Any] | None] = {}
LARGE_CATALOG_BYTES = 32 * 1024 * 1024
DIRECT_LOOKUPS = 12
lookups: dict[tuple[str, int, int], int] = {}

def catalog_key(catalog_path: Path) -> tuple[str, int, int] | None:
    try:
        stat = catalog_path.stat()
    except OSError:
        return None
    return (str(catalog_path), stat.st_size, int(stat.st_mtime))

def direct_record(catalog_path: Path, relative: str) -> dict[str, Any] | None:
    needle = b'\n    "' + json.dumps(relative, ensure_ascii=False)[1:-1].encode("utf-8") + b'": {'
    try:
        with catalog_path.open("rb") as handle, mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as data:
            at = data.find(needle)
            if at < 0:
                return None
            start = at + len(needle)
            if data[start : start + 1] != b"\n":
                return None
            close = data.find(b"\n    }", start)
            if close < 0:
                return None
            record = json.loads(b"{" + data[start:close] + b"\n}")
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None

def catalog_files(catalog_path: Path, relatives: list[str]) -> dict[str, Any] | None:
    key = catalog_key(catalog_path)
    if key is None:
        return None
    if key not in CATALOG_CACHE and key[1] > LARGE_CATALOG_BYTES and lookups.get(key, 0) < DIRECT_LOOKUPS:
        lookups[key] = lookups.get(key, 0) + 1
        found = {}
        for relative in relatives:
            record = direct_record(catalog_path, relative)
            if record is not None:
                found[relative] = record
                break
        return found
    catalog = load_catalog(catalog_path)
    files = catalog.get("files") if isinstance(catalog, dict) else None
    return files if isinstance(files, dict) else None

def load_catalog(catalog_path: Path) -> dict[str, Any] | None:
    try:
        stat = catalog_path.stat()
    except OSError:
        return None
    key = (str(catalog_path), stat.st_size, int(stat.st_mtime))
    if key in CATALOG_CACHE:
        return CATALOG_CACHE[key]
    try:
        data = json.loads(catalog_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = None
    if not isinstance(data, dict):
        data = None
    CATALOG_CACHE.clear()
    CATALOG_CACHE[key] = data
    return data

def read_external_taildata(path: Path) -> dict[str, Any] | None:
    target = Path(path).resolve()
    if target.name == EXTERNAL_TAILDATA_FILENAME and target.is_file():
        return load_catalog(target)
    for folder in [target.parent, *target.parents]:
        catalog_path = folder / EXTERNAL_TAILDATA_FILENAME
        if not catalog_path.is_file():
            continue
        try:
            relative = target.relative_to(catalog_path.parent).as_posix()
        except ValueError:
            continue
        if any(is_work_folder(part) for part in relative.split("/")[:-1]):
            continue
        audio =target.suffix.lower() in AUDIO_VIEW_SUFFIXES
        others = [relative[: -len(target.suffix)] + suffix for suffix in AUDIO_VIEW_SUFFIXES] if audio else []
        files = catalog_files(catalog_path, [relative])
        if files is None:
            continue
        record = files.get(relative)
        if isinstance(record, dict):
            return record
        for other in others:
            found = catalog_files(catalog_path, [other])
            record = found.get(other) if found else None
            if isinstance(record, dict) and str(record.get("kind") or "").startswith("audio_"):
                return record
    return None
