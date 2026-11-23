from __future__ import annotations
import json, re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from .taildata import EXTERNAL_TAILDATA_FILENAME

@dataclass(slots=True)
class StagedPayload:
    path: Path
    source_root: Path

@dataclass(slots=True)
class CatalogIndex:
    path: Path
    root: Path
    records: dict[str, Any]
    basename_records: dict[str, list[tuple[str, dict[str, Any]]]]
    stem_records: dict[str, list[tuple[str, dict[str, Any]]]]
    path_id_records: dict[int, list[tuple[str, dict[str, Any]]]]

def collect_payload_files(paths: Iterable[str]) -> list[StagedPayload]:
    files: list[StagedPayload] = []
    seen: set[Path] = set()
    for text in paths:
        if not text:
            continue
        path = Path(text)
        if path.is_dir():
            source_root = path.resolve()
            candidates = [(item, source_root) for item in sorted(path.rglob("*")) if item.is_file()]
        elif path.is_file():
            candidates = [(path, path.parent.resolve())]
        else:
            candidates = []
        for candidate, source_root in candidates:
            if candidate.name == EXTERNAL_TAILDATA_FILENAME or candidate.suffix.lower() in {".rwbygep", ".rwbygei"}:
                continue
            if candidate.name.lower().endswith(".pvs.bin"):
                continue
            resolved = candidate.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            files.append(StagedPayload(path=candidate, source_root=source_root))
    return files

def collect_catalog_paths(
    payload_paths: Iterable[str],
    staged_files: list[StagedPayload],
    explicit_taildata_paths: Iterable[str],
) -> list[Path]:
    catalogs: dict[Path, Path] = {}
    for text in [*payload_paths, *explicit_taildata_paths]:
        if not text:
            continue
        path = Path(text)
        if path.is_file() and path.name == EXTERNAL_TAILDATA_FILENAME:
            catalogs[path.resolve()] = path.resolve()
            continue
        if path.is_dir():
            for folder in [path.resolve(), *path.resolve().parents]:
                catalog_path = folder / EXTERNAL_TAILDATA_FILENAME
                if catalog_path.is_file():
                    catalogs[catalog_path.resolve()] = catalog_path.resolve()
                    break
    for staged in staged_files:
        target = staged.path.resolve()
        for folder in [target.parent, *target.parents]:
            catalog_path = folder / EXTERNAL_TAILDATA_FILENAME
            if catalog_path.is_file():
                catalogs[catalog_path.resolve()] = catalog_path.resolve()
                break
    return sorted(catalogs.values(), key=lambda item: str(item).lower())

def load_catalog_indexes(catalog_paths: Iterable[Path]) -> list[CatalogIndex]:
    indexes: list[CatalogIndex] = []
    for catalog_path in catalog_paths:
        catalog_path = Path(catalog_path).resolve()
        try:
            catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(catalog, dict):
            continue
        files = catalog.get("files")
        if not isinstance(files, dict):
            continue
        basename_records: dict[str, list[tuple[str, dict[str, Any]]]] = {}
        stem_records: dict[str, list[tuple[str, dict[str, Any]]]] = {}
        path_id_records: dict[int, list[tuple[str, dict[str, Any]]]] = {}
        for key, value in files.items():
            if not isinstance(value, dict):
                continue
            basename = Path(str(key)).name.lower()
            basename_records.setdefault(basename, []).append((str(key), value))
            stem_records.setdefault(Path(basename).stem, []).append((str(key), value))
            path_id = path_id_from_name(Path(basename).stem)
            if path_id is not None:
                path_id_records.setdefault(path_id, []).append((str(key), value))
        indexes.append(
            CatalogIndex(
                path=Path(catalog_path).resolve(),
                root=catalog_path.parent,
                records=files,
                basename_records=basename_records,
                stem_records=stem_records,
                path_id_records=path_id_records,
            )
        )
    return indexes

def find_catalog_record(staged: StagedPayload, catalogs: list[CatalogIndex]) -> tuple[Path, dict[str, Any] | None, str]:
    target = staged.path.resolve()
    basename_matches: list[tuple[CatalogIndex, str, dict[str, Any]]] = []
    stem_matches: list[tuple[CatalogIndex, str, dict[str, Any]]] = []
    path_id_matches: list[tuple[CatalogIndex, str, dict[str, Any]]] = []
    staged_path_id = path_id_from_name(target.stem)
    for catalog in catalogs:
        for relative in candidate_relative_paths(target, staged.source_root, catalog.root):
            record = catalog.records.get(relative)
            if isinstance(record, dict):
                return catalog.path, record, relative
        for key, record in catalog.basename_records.get(target.name.lower(), []):
            basename_matches.append((catalog, key, record))
        for key, record in catalog.stem_records.get(target.stem.lower(), []):
            if stem_kind_matches(target.suffix.lower(), record):
                stem_matches.append((catalog, key, record))
        if staged_path_id is not None:
            for key, record in catalog.path_id_records.get(staged_path_id, []):
                if path_id_kind_matches(target.suffix.lower(), record):
                    path_id_matches.append((catalog, key, record))
    basename_matches = unique_target_matches(basename_matches)
    stem_matches = unique_target_matches(stem_matches)
    path_id_matches = unique_target_matches(path_id_matches)
    if len(basename_matches) == 1:
        catalog, key, record = basename_matches[0]
        if isinstance(record, dict):
            return catalog.path, record, key
    if not basename_matches and len(stem_matches) == 1:
        catalog, key, record = stem_matches[0]
        if isinstance(record, dict):
            return catalog.path, record, key
    if not basename_matches and not stem_matches and len(path_id_matches) == 1:
        catalog, key, record = path_id_matches[0]
        if isinstance(record, dict):
            return catalog.path, record, key
    return target.parent / EXTERNAL_TAILDATA_FILENAME, None, target.name

def path_id_from_name(stem: str) -> int | None:
    match = re.search(r"__path(-?\d+)(?:__|$)", stem, re.IGNORECASE)
    return int(match.group(1)) if match else None

def path_id_kind_matches(suffix: str, record: dict[str, Any]) -> bool:
    kind = str(record.get("kind") or "")
    if suffix in {".wav", ".ogg"}:
        return kind in {"audio_wav", "audio_ogg"}
    if suffix == ".glb":
        return kind in {"mesh_glb", "reconstructed_terrain_glb"}
    if suffix == ".sanim":
        return kind == "animation_clip_package"
    return False

def stem_kind_matches(suffix: str, record: dict[str, Any]) -> bool:
    if suffix in {".wav", ".ogg", ".sanim"}:
        return path_id_kind_matches(suffix, record)
    return True

def unique_target_matches(
    matches: list[tuple[CatalogIndex, str, dict[str, Any]]],
) -> list[tuple[CatalogIndex, str, dict[str, Any]]]:
    unique: dict[tuple[str, int, str], tuple[CatalogIndex, str, dict[str, Any]]] = {}
    for catalog, key, record in matches:
        target = (
            str(record.get("appendTarget") or record.get("container") or "").lower(),
            int(record.get("pathId", 0)),
            str(record.get("kind") or ""),
        )
        unique.setdefault(target, (catalog, key, record))
    return list(unique.values())

def candidate_relative_paths(target: Path, source_root: Path, catalog_root: Path) -> list[str]:
    candidates: list[str] = []
    for root in (catalog_root, source_root):
        try:
            value = target.relative_to(root).as_posix()
        except ValueError:
            continue
        if value not in candidates:
            candidates.append(value)
    return candidates
