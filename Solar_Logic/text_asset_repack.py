from __future__ import annotations
from pathlib import Path
from typing import Any
from .binary import EndianReader
from .mono_repack import EndianWriter, taildata_value
from .serialized import SerializedAsset, SerializedObject, load_serialized_asset

class TextAssetRepackError(ValueError):
    pass

asset_cache: dict[tuple[str, str], SerializedAsset] = {}

def repack_text_asset(
    payload_path: Path,
    taildata: dict[str, Any],
    data_root: Path,
) -> bytes:
    container = str(taildata_value(taildata, "container") or "")
    if not container:
        raise TextAssetRepackError("taildata record doesnt name a Unity container")
    path_id = int(taildata_value(taildata, "pathId", 0) or 0)
    asset = load_text_asset_container(Path(data_root), container)
    obj = next((item for item in asset.objects if item.path_id == path_id), None)
    if obj is None or obj.class_id != 49:
        raise TextAssetRepackError(
            f"TextAsset path {path_id} wasnt found in {container}"
        )
    name, path_name = read_text_asset_identity(asset, obj)
    payload = Path(payload_path).read_bytes()
    writer = EndianWriter(asset.endian)
    writer.aligned_string(name)
    writer.i32(len(payload))
    writer.write(payload)
    writer.align()
    writer.aligned_string(path_name)
    return writer.bytes()

def load_text_asset_container(
    data_root: Path,
    container: str,
) -> SerializedAsset:
    root = data_root.resolve()
    key = (str(root).lower(), normalize_container(container))
    asset = asset_cache.get(key)
    if asset is None:
        path = (root / container).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise TextAssetRepackError(
                f"TextAsset container escapes the game data folder: {container}"
            ) from exc
        if not path.is_file():
            raise TextAssetRepackError(f"TextAsset container wasnt found: {path}")
        asset = load_serialized_asset(path, root)
        asset_cache[key] = asset
    return asset

def read_text_asset_identity(
    asset: SerializedAsset,
    obj: SerializedObject,
) -> tuple[str, str]:
    with asset.path.open("rb") as handle:
        reader = EndianReader(handle, asset.endian)
        reader.seek(obj.byte_start)
        name = reader.aligned_string(max_length=4096)
        payload_size = reader.i32()
        if payload_size < 0 or reader.tell() + payload_size > obj.byte_start + obj.byte_size:
            raise TextAssetRepackError("TextAsset payload size is invalid")
        reader.seek(payload_size, 1)
        reader.align()
        path_name = reader.aligned_string(max_length=4096)
        if reader.tell() != obj.byte_start + obj.byte_size:
            raise TextAssetRepackError(
                f"TextAsset identity reader didnt consume path {obj.path_id}"
            )
    return name, path_name

def normalize_container(value: str) -> str:
    return value.replace("\\", "/").lower()
