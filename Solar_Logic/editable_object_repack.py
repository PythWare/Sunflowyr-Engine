from __future__ import annotations
import json
from pathlib import Path
from typing import Any
from .editable_object_export import EDITABLE_ASSET_SCHEMAS
from .mono_repack import taildata_value
from .serialized import SerializedAsset, load_serialized_asset
from .unity_typetree_codec import load_schema, write_typetree

class EditableObjectRepackError(ValueError):
    pass

asset_cache: dict[tuple[str, str], SerializedAsset] = {}

def repack_editable_asset_json(
    json_path: Path,
    taildata: dict[str, Any],
    data_root: Path,
) -> bytes:
    document = json.loads(Path(json_path).read_text(encoding="utf-8-sig"))
    if not isinstance(document, dict):
        raise EditableObjectRepackError("editable asset JSON root must be an object")
    fields = document.get("fields")
    if not isinstance(fields, dict):
        raise EditableObjectRepackError("editable asset JSON is missing fields")

    class_id = int(taildata_value(taildata, "classId", 0) or 0)
    definition = EDITABLE_ASSET_SCHEMAS.get(class_id)
    if definition is None:
        raise EditableObjectRepackError(f"unsupported editable asset class {class_id}")
    container = str(taildata_value(taildata, "container") or "")
    if not container:
        raise EditableObjectRepackError("taildata record doesnt name a Unity container")

    source = document.get("source")
    if isinstance(source, dict):
        source_class = int(source.get("classId", class_id))
        source_container = str(source.get("container") or container)
        if source_class != class_id:
            raise EditableObjectRepackError(
                f"editable asset class mismatch: JSON has {source_class}, taildata has {class_id}"
            )
        if normalize_container(source_container) != normalize_container(container):
            raise EditableObjectRepackError(
                f"editable asset container mismatch: JSON has {source_container}, taildata has {container}"
            )

    asset = load_editable_asset(Path(data_root), container)
    schema_name = definition[1]
    return write_typetree(fields, load_schema(schema_name), asset.endian)

def load_editable_asset(data_root: Path, container: str) -> SerializedAsset:
    root = data_root.resolve()
    key = (str(root).lower(), normalize_container(container))
    asset = asset_cache.get(key)
    if asset is None:
        asset_path = (root / container).resolve()
        try:
            asset_path.relative_to(root)
        except ValueError as exc:
            raise EditableObjectRepackError(
                f"editable asset container escapes the game data folder: {container}"
            ) from exc
        if not asset_path.is_file():
            raise EditableObjectRepackError(
                f"editable asset container wasnt found: {asset_path}"
            )
        asset = load_serialized_asset(asset_path, root)
        asset_cache[key] = asset
    return asset

def normalize_container(value: str) -> str:
    return value.replace("\\", "/").lower()
