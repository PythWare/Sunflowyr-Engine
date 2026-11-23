from __future__ import annotations
import json, re
from pathlib import Path
from typing import Any
from .taildata import EXTERNAL_TAILDATA_FILENAME, load_catalog, read_taildata

DATA_ROOT_PATTERN = re.compile(r'"dataRoot"\s*:\s*("(?:[^"\\]|\\.)*")')

def nearest_catalog(path: Path) -> Path | None:
    for folder in Path(path).resolve().parents:
        candidate = folder / EXTERNAL_TAILDATA_FILENAME
        if candidate.is_file():
            return candidate
    return None

def catalog_data_root(catalog_path: Path) -> Path | None:
    try:
        with catalog_path.open("r", encoding="utf-8") as handle:
            head = handle.read(8192)
    except OSError:
        return None
    match = DATA_ROOT_PATTERN.search(head)
    if match is None:
        return None
    return Path(json.loads(match.group(1)))

def source_root_for(path: Path, container: str) -> Path | None:
    catalog_path = nearest_catalog(path)
    if catalog_path is None:
        return None
    catalog = load_catalog(catalog_path) or {}
    recorded = str(catalog.get("dataRoot") or "")
    if not recorded:
        return None
    candidates = [Path(recorded)]
    try:
        candidates.insert(0, catalog_path.parent / Path(recorded).relative_to(Path(str(catalog.get("outputRoot") or ""))))
    except ValueError:
        pass
    return next((root for root in candidates if (root / container).is_file()), None)

def from_outside_game(path: Path, data_root: Path) -> bool:
    catalog_path = nearest_catalog(path)
    if catalog_path is None:
        return False
    recorded = catalog_data_root(catalog_path)
    if recorded is None:
        return False
    try:
        return recorded.resolve() != Path(data_root).resolve()
    except OSError:
        return True

def looks_like_unpacked_material(path: Path) -> bool:
    if Path(path).suffix.lower() != ".json" or Path(path).name.lower().endswith(".material.json"):
        return False
    try:
        with Path(path).open("r", encoding="utf-8-sig") as handle:
            head = handle.read(4096)
    except (OSError, UnicodeError):
        return False
    return '"m_Shader"' in head and '"m_SavedProperties"' in head

def material_needs_conversion(path: Path, data_root: Path) -> bool:
    if not looks_like_unpacked_material(path) or not from_outside_game(path, data_root):
        return False
    record = read_taildata(path) or {}
    container = str(record.get("container") or "")
    return bool(container) and not (Path(data_root) / container).is_file()

def convert_unpacked_material(path: Path, out_folder: Path) -> dict[str, Any]:
    from .model_context import build_model_context, norm
    from .prefab_clone import CloneResult, export_material
    from .serialized import discover_serialized_files, load_serialized_asset

    record = read_taildata(path) or {}
    container = str(record.get("container") or "")
    path_id = int(record.get("pathId") or record.get("path_id") or 0)
    root = source_root_for(path, container) if container else None
    if root is None:
        raise ValueError(f"the original file of {Path(path).name} ({container or 'unknown'}) wasnt found next to its unpack")
    assets = [load_serialized_asset(item, root) for item in discover_serialized_files(root)]
    context = build_model_context(assets)
    out_folder = Path(out_folder)
    out_folder.mkdir(parents=True, exist_ok=True)
    result = CloneResult(prefab_path=out_folder / "unused.prefab.json", folder=out_folder, root_name=Path(path).stem)
    file_name = export_material(context, (norm(container), path_id), out_folder, result, {})
    if file_name is None:
        raise ValueError("; ".join(result.warnings) or f"material {path_id} couldnt be rebuilt")
    return {"material": out_folder / file_name, "files": sorted(item for item in out_folder.iterdir() if item.is_file()), "warnings": list(result.warnings)}

def unpack_source_roots(glb_path: Path, data_root: Path) -> list[Path]:
    from .animation_glb_import import read_glb
    from .prefab_from_glb import root_source_key

    try:
        document, unused_binary = read_glb(Path(glb_path).read_bytes())
    except Exception:
        return []
    key = root_source_key(document)
    if key is None or (Path(data_root) / key[0]).is_file():
        return []
    root = source_root_for(glb_path, key[0])
    return [root] if root is not None else []
