from __future__ import annotations
import json, struct
from pathlib import Path
from typing import Any
from .mono_repack import EndianWriter, taildata_value
from .navmesh_export import NAVMESH_TRAILING_FIELDS, navmesh_schema_field
from .serialized import load_serialized_asset
from .unity_typetree_codec import TypeTreeCodecError, write_value

TILE_HEADER_FORMAT = "<11i7f"
TILE_HEADER_SIZE = 72
POLYGON_SIZE = 32
VERTEX_SIZE = 12

class NavMeshRepackError(ValueError):
    pass

def repack_navmesh_data_json(
    json_path: Path,
    taildata: dict[str, Any],
    data_root: Path,
) -> bytes:
    document = json.loads(Path(json_path).read_text(encoding="utf-8-sig"))
    if not isinstance(document, dict) or document.get("format") != "sunflowyr-navmesh-data":
        raise NavMeshRepackError("file is not a Sunflowyr NavMeshData JSON document")
    class_id = int(taildata_value(taildata, "classId", 0) or 0)
    if class_id != 238:
        raise NavMeshRepackError(f"NavMeshData taildata expected class 238, found {class_id}")
    container = str(taildata_value(taildata, "container") or "")
    if not container:
        source = document.get("source")
        if isinstance(source, dict):
            container = str(source.get("container") or "")
    if not container:
        raise NavMeshRepackError("NavMeshData JSON doesnt name its Unity container")
    root = Path(data_root).resolve()
    asset_path = (root / container).resolve()
    try:
        asset_path.relative_to(root)
    except ValueError as exc:
        raise NavMeshRepackError(f"NavMeshData container escapes the game data folder: {container}") from exc
    asset = load_serialized_asset(asset_path, root)
    tile_data = read_tile_sidecar(document, Path(json_path))
    tiles = require_list(document.get("m_NavMeshTiles"), "m_NavMeshTiles")

    writer = EndianWriter(asset.endian)
    writer.aligned_string(document.get("m_Name", ""))
    writer.i32(len(tiles))
    for index, value in enumerate(tiles):
        tile = require_dict(value, f"m_NavMeshTiles[{index}]")
        blob = require_dict(tile.get("blob"), f"m_NavMeshTiles[{index}].blob")
        offset = int(blob.get("byteOffset", -1))
        size = int(blob.get("byteLength", -1))
        if offset < 0 or size < 0 or offset + size > len(tile_data):
            raise NavMeshRepackError(f"NavMesh tile {index} has an invalid binary slice")
        check_tile_view(index, tile, tile_data[offset : offset + size])
        writer.i32(size)
        writer.write(tile_data[offset : offset + size])
        writer.align()

    parameters = require_dict(document.get("m_NavMeshParams"), "m_NavMeshParams")
    for name in ("tileSize", "walkableHeight", "walkableRadius", "walkableClimb", "cellSize"):
        writer.f32(float(parameters.get(name, 0.0)))
    for name in NAVMESH_TRAILING_FIELDS:
        values = require_list(document.get(name, []), name)
        try:
            write_value(values, navmesh_schema_field(name), writer)
        except (TypeTreeCodecError, TypeError, ValueError) as exc:
            raise NavMeshRepackError(f"{name} couldnt be written: {exc}") from exc
    return writer.bytes()

def check_tile_view(index: int, tile: dict[str, Any], blob: bytes) -> None:
    if len(blob) < TILE_HEADER_SIZE:
        raise NavMeshRepackError(f"NavMesh tile {index} blob is shorter than a tile header")
    header = struct.unpack_from(TILE_HEADER_FORMAT, blob, 0)
    stored: dict[str, Any] = {
        "x": header[2],
        "y": header[3],
        "layer": header[4],
        "polygonCount": header[5],
        "vertexCount": header[6],
        "detailMeshCount": header[7],
        "detailVertexCount": header[8],
        "detailTriangleCount": header[9],
        "bvNodeCount": header[10],
        "boundsMin": vector_view(header[11:14]),
        "boundsMax": vector_view(header[14:17]),
        "bvQuantFactor": header[17],
        "triangleCount": tile_triangle_count(blob, header[5], header[6]),
    }
    for name, value in stored.items():
        if name not in tile:
            continue
        if not same_value(tile[name], value):
            raise NavMeshRepackError(
                f"NavMesh tile {index} field {name!r} is read only, edit the GLB and rebake with: solar navmesh-from-glb"
            )

def tile_triangle_count(blob: bytes, polygon_count: int, vertex_count: int) -> int:
    base = TILE_HEADER_SIZE + vertex_count * VERTEX_SIZE
    total = 0
    for index in range(polygon_count):
        offset = base + index * POLYGON_SIZE + 28
        if offset + 4 > len(blob):
            return total
        total += struct.unpack_from("<i", blob, offset)[0] - 2
    return total

def vector_view(values) -> dict[str, float]:
    return dict(zip(("x", "y", "z"), (float(value) for value in values)))

def same_value(edited: Any, stored: Any) -> bool:
    if isinstance(stored, dict):
        if not isinstance(edited, dict):
            return False
        return all(same_value(edited.get(key), value) for key, value in stored.items())
    if isinstance(stored, float):
        try:
            return abs(float(edited) - stored) <= max(1e-6, abs(stored) * 1e-6)
        except (TypeError, ValueError):
            return False
    try:
        return int(edited) == int(stored)
    except (TypeError, ValueError):
        return edited == stored

def read_tile_sidecar(document: dict[str, Any], json_path: Path) -> bytes:
    files = require_dict(document.get("binaryFiles"), "binaryFiles")
    entry = require_dict(files.get("navmeshTiles"), "binaryFiles.navmeshTiles")
    relative = Path(str(entry.get("path") or ""))
    base = json_path.resolve().parent
    sidecar = (base / relative).resolve()
    try:
        sidecar.relative_to(base)
    except ValueError as exc:
        raise NavMeshRepackError("NavMesh tile sidecar path escapes the JSON folder") from exc
    if not sidecar.is_file():
        raise NavMeshRepackError(f"NavMesh tile sidecar wasnt found: {sidecar}")
    data = sidecar.read_bytes()
    expected_size = int(entry.get("byteLength", len(data)))
    if len(data) != expected_size:
        raise NavMeshRepackError(
            f"NavMesh tile sidecar expected {expected_size} bytes, found {len(data)}"
        )
    return data

def require_dict(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise NavMeshRepackError(f"{label} must be an object")
    return value

def require_list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise NavMeshRepackError(f"{label} must be a list")
    return value
