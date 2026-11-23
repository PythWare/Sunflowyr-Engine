from __future__ import annotations
import struct
from pathlib import Path
from .binary import EndianReader
from .objects import describe_object
from .raw_object_export import RawObjectExportError, build_complete_raw_object
from .serialized import load_serialized_asset

CUBEMAP_CLASS_ID = 89
CUBEMAP_ASSET_SUFFIX = ".cubemap.bin"

class NewCubemapAssetError(ValueError):
    pass

def export_game_cubemap(
    data_root: Path, container: str, path_id: int, out_dir: Path
) -> str | None:
    candidate = Path(data_root) / container
    if not candidate.is_file():
        return None
    try:
        asset = load_serialized_asset(candidate, Path(data_root))
    except Exception:
        return None
    obj = next(
        (o for o in asset.objects if o.path_id == path_id and o.class_id == CUBEMAP_CLASS_ID),
        None,
    )
    if obj is None:
        return None
    try:
        with asset.path.open("rb") as handle:
            payloads = describe_object(asset, EndianReader(handle, asset.endian), obj).payloads
        raw = build_complete_raw_object(asset, obj, payloads).data
    except (RawObjectExportError, OSError, ValueError):
        return None
    if len(raw) < 8:
        return None
    length = struct.unpack_from("<i", raw, 0)[0]
    name = ""
    if 0 < length <= len(raw) - 4:
        name = raw[4 : 4 + length].decode("utf-8", "replace")
    safe = "".join(c if c.isalnum() or c in " -_." else "_" for c in (name or f"Cubemap_{path_id}"))
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    file_name = f"{safe}{CUBEMAP_ASSET_SUFFIX}"
    (out_dir / file_name).write_bytes(raw)
    return file_name

def try_build_cubemap_asset(
    path: Path, fallback_name: str
) -> tuple[bytes, str, dict[str, object]] | None:
    path = Path(path)
    if not path.name.lower().endswith(CUBEMAP_ASSET_SUFFIX):
        return None
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise NewCubemapAssetError(f"couldnt read cubemap payload: {exc}") from exc
    if len(payload) < 8:
        raise NewCubemapAssetError("cubemap payload is too small to be a Cubemap object")
    length = struct.unpack_from("<i", payload, 0)[0]
    if length < 0 or 4 + length > len(payload):
        raise NewCubemapAssetError("cubemap payload doesnt start with a valid m_Name")
    try:
        name = payload[4 : 4 + length].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise NewCubemapAssetError(f"cubemap name isnt valid UTF-8: {exc}") from exc
    details: dict[str, object] = {
        "sourceFormat": "game-cubemap-copy",
        "payloadBytes": len(payload),
    }
    return bytes(payload), name or fallback_name, details
