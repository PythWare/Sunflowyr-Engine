from __future__ import annotations
import json, math, struct
from pathlib import Path
from typing import Any
from .mono_repack import taildata_value
from .serialized import SerializedAsset, SerializedObject, load_serialized_asset

class TerrainGlbRepackError(ValueError):
    pass

GLB_JSON_CHUNK = 0x4E4F534A
GLB_BIN_CHUNK = 0x004E4942
GL_FLOAT = 5126

def repack_terrain_glb(
    glb_path: Path,
    taildata: dict[str, Any],
    data_root: Path,
) -> bytes:
    class_id = int(taildata_value(taildata, "classId", 0) or 0)
    if class_id != 156:
        raise TerrainGlbRepackError(
            f"Terrain GLB taildata expected class 156, found {class_id}"
        )
    container = str(taildata_value(taildata, "container") or "")
    path_id = int(taildata_value(taildata, "pathId", 0) or 0)
    if not container or not path_id:
        raise TerrainGlbRepackError("Terrain GLB taildata is missing container or pathId")

    root = Path(data_root).resolve()
    asset_path = (root / container).resolve()
    try:
        asset_path.relative_to(root)
    except ValueError as exc:
        raise TerrainGlbRepackError(
            f"Terrain GLB container escapes the game data folder: {container}"
        ) from exc
    asset = load_serialized_asset(asset_path, root)
    obj = find_terrain_object(asset, path_id)
    original = read_object_bytes(asset, obj)
    layout = locate_heightmap_layout(original, asset.endian)
    document, binary = read_glb(Path(glb_path))
    positions = terrain_positions(document, binary, path_id)
    heights = positions_to_heights(
        positions,
        int(layout["width"]),
        int(layout["height"]),
        tuple(layout["scale"]),
    )
    rebuilt = bytearray(original)
    height_offset = int(layout["height_data_offset"])
    height_bytes = encode_u16_values(heights, asset.endian)
    rebuilt[height_offset : height_offset + len(height_bytes)] = height_bytes
    return bytes(rebuilt)

def locate_heightmap_layout(data: bytes, endian: str) -> dict[str, Any]:
    if len(data) < 28:
        raise TerrainGlbRepackError("TerrainData object is too small")
    footer_offset = len(data) - 28
    width, height = struct.unpack_from(endian + "ii", data, footer_offset)
    thickness = struct.unpack_from(endian + "f", data, footer_offset + 8)[0]
    levels = struct.unpack_from(endian + "i", data, footer_offset + 12)[0]
    scale = struct.unpack_from(endian + "3f", data, footer_offset + 16)
    if width <= 1 or height <= 1 or width > 8193 or height > 8193:
        raise TerrainGlbRepackError(f"invalid TerrainData dimensions {width}x{height}")
    height_count = width * height
    count_bytes = struct.pack(endian + "i", height_count)
    candidates: list[dict[str, Any]] = []
    search_offset = 0
    while True:
        count_offset = data.find(count_bytes, search_offset, footer_offset)
        if count_offset < 0:
            break
        search_offset = count_offset + 1
        height_data_offset = count_offset + 4
        after_heights = align_value(height_data_offset + height_count * 2, 4)
        if after_heights + 4 > footer_offset:
            continue
        error_count = struct.unpack_from(endian + "i", data, after_heights)[0]
        if error_count < 0:
            continue
        minmax_count_offset = after_heights + 4 + error_count * 4
        if minmax_count_offset + 4 > footer_offset:
            continue
        minmax_count = struct.unpack_from(endian + "i", data, minmax_count_offset)[0]
        if minmax_count < 0:
            continue
        if minmax_count_offset + 4 + minmax_count * 4 != footer_offset:
            continue
        candidates.append(
            {
                "width": width,
                "height": height,
                "thickness": thickness,
                "levels": levels,
                "scale": scale,
                "height_count_offset": count_offset,
                "height_data_offset": height_data_offset,
                "error_count": error_count,
                "minmax_count": minmax_count,
            }
        )
    if len(candidates) != 1:
        raise TerrainGlbRepackError(
            f"TerrainData height array locator found {len(candidates)} candidates"
        )
    return candidates[0]

def read_glb(path: Path) -> tuple[dict[str, Any], bytes]:
    data = path.read_bytes()
    if len(data) < 20:
        raise TerrainGlbRepackError("GLB is truncated")
    magic, version, total_length = struct.unpack_from("<4sII", data, 0)
    if magic != b"glTF" or version != 2 or total_length != len(data):
        raise TerrainGlbRepackError("file is not a complete glTF 2.0 GLB")
    cursor = 12
    document = None
    binary = b""
    while cursor + 8 <= len(data):
        chunk_length, chunk_type = struct.unpack_from("<II", data, cursor)
        cursor += 8
        chunk_end = cursor + chunk_length
        if chunk_end > len(data):
            raise TerrainGlbRepackError("GLB chunk exceeds the file length")
        chunk = data[cursor:chunk_end]
        cursor = chunk_end
        if chunk_type == GLB_JSON_CHUNK:
            document = json.loads(chunk.rstrip(b" \0").decode("utf-8"))
        elif chunk_type == GLB_BIN_CHUNK:
            binary = chunk
    if not isinstance(document, dict):
        raise TerrainGlbRepackError("GLB has no JSON document")
    if not binary:
        raise TerrainGlbRepackError("GLB has no binary geometry chunk")
    return document, binary

def terrain_positions(
    document: dict[str, Any],
    binary: bytes,
    path_id: int,
) -> list[tuple[float, float, float]]:
    meshes = document.get("meshes")
    if not isinstance(meshes, list) or not meshes:
        raise TerrainGlbRepackError("Terrain GLB has no meshes")
    selected_mesh = None
    for index, mesh in enumerate(meshes):
        if not isinstance(mesh, dict):
            continue
        extras = mesh.get("extras")
        if isinstance(extras, dict) and int(extras.get("unity_path_id", 0) or 0) == path_id:
            selected_mesh = index
            break
    if selected_mesh is None:
        selected_mesh = 0
    mesh = meshes[selected_mesh]
    primitives = mesh.get("primitives") if isinstance(mesh, dict) else None
    if not isinstance(primitives, list) or not primitives:
        raise TerrainGlbRepackError("Terrain GLB mesh has no primitives")
    primitive = primitives[0]
    attributes = primitive.get("attributes") if isinstance(primitive, dict) else None
    if not isinstance(attributes, dict) or not isinstance(attributes.get("POSITION"), int):
        raise TerrainGlbRepackError("Terrain GLB primitive has no POSITION accessor")
    positions = read_position_accessor(document, binary, int(attributes["POSITION"]))
    transform = mesh_world_transform(document, selected_mesh)
    return [transform_point(transform, value) for value in positions]

def read_position_accessor(
    document: dict[str, Any],
    binary: bytes,
    accessor_index: int,
) -> list[tuple[float, float, float]]:
    accessors = require_list(document.get("accessors"), "GLB accessors")
    views = require_list(document.get("bufferViews"), "GLB bufferViews")
    if not 0 <= accessor_index < len(accessors):
        raise TerrainGlbRepackError("Terrain POSITION accessor index is invalid")
    accessor = require_dict(accessors[accessor_index], "Terrain POSITION accessor")
    if int(accessor.get("componentType", 0)) != GL_FLOAT or accessor.get("type") != "VEC3":
        raise TerrainGlbRepackError("Terrain POSITION accessor must be float VEC3")
    if accessor.get("sparse") is not None:
        raise TerrainGlbRepackError("sparse Terrain POSITION accessors are unsupported")
    view_index = int(accessor.get("bufferView", -1))
    if not 0 <= view_index < len(views):
        raise TerrainGlbRepackError("Terrain POSITION buffer view is invalid")
    view = require_dict(views[view_index], "Terrain POSITION buffer view")
    count = int(accessor.get("count", 0))
    if count <= 0:
        raise TerrainGlbRepackError("Terrain POSITION accessor is empty")
    stride = int(view.get("byteStride", 12))
    if stride < 12:
        raise TerrainGlbRepackError("Terrain POSITION byte stride is too small")
    start = int(view.get("byteOffset", 0)) + int(accessor.get("byteOffset", 0))
    output = []
    for index in range(count):
        offset = start + index * stride
        if offset + 12 > len(binary):
            raise TerrainGlbRepackError("Terrain POSITION data exceeds the GLB binary chunk")
        output.append(struct.unpack_from("<3f", binary, offset))
    return output

def mesh_world_transform(document: dict[str, Any], mesh_index: int) -> tuple[float, ...]:
    nodes = document.get("nodes")
    if not isinstance(nodes, list):
        return identity_matrix()
    node_index = next(
        (
            index
            for index, node in enumerate(nodes)
            if isinstance(node, dict) and int(node.get("mesh", -1)) == mesh_index
        ),
        None,
    )
    if node_index is None:
        return identity_matrix()
    parents: dict[int, int] = {}
    for parent_index, node in enumerate(nodes):
        if not isinstance(node, dict):
            continue
        for child in node.get("children") or []:
            if isinstance(child, int):
                parents[child] = parent_index
    chain = []
    current = node_index
    seen = set()
    while current not in seen:
        seen.add(current)
        chain.append(current)
        if current not in parents:
            break
        current = parents[current]
    transform = identity_matrix()
    for index in reversed(chain):
        transform = multiply_matrix(transform, node_matrix(require_dict(nodes[index], "GLB node")))
    return transform

def node_matrix(node: dict[str, Any]) -> tuple[float, ...]:
    matrix = node.get("matrix")
    if isinstance(matrix, list) and len(matrix) == 16:
        return tuple(float(value) for value in matrix)
    translation = vector_list(node.get("translation"), 3, (0.0, 0.0, 0.0))
    rotation = vector_list(node.get("rotation"), 4, (0.0, 0.0, 0.0, 1.0))
    scale = vector_list(node.get("scale"), 3, (1.0, 1.0, 1.0))
    return trs_matrix(translation, rotation, scale)

def positions_to_heights(
    positions: list[tuple[float, float, float]],
    width: int,
    height: int,
    scale: tuple[float, float, float],
) -> list[int]:
    scale_x, scale_y, scale_z = scale
    if abs(scale_x) < 1e-12 or abs(scale_y) < 1e-12 or abs(scale_z) < 1e-12:
        raise TerrainGlbRepackError("TerrainData scale contains zero")
    expected = width * height
    if len(positions) < expected:
        raise TerrainGlbRepackError(
            f"Terrain GLB contains {len(positions)} vertices, expected at least {expected}"
        )
    tolerance_x = max(abs(scale_x) * 0.05, 1e-4)
    tolerance_z = max(abs(scale_z) * 0.05, 1e-4)
    heights: list[int | None] = [None] * expected
    x_sign = -1.0 if min(position[0] for position in positions) < -tolerance_x else 1.0
    for position in positions:
        row = round(x_sign * position[0] / scale_x)
        column = round(position[2] / scale_z)
        if not 0 <= row < height or not 0 <= column < width:
            raise TerrainGlbRepackError(
                "Terrain GLB contains vertices outside the original heightfield grid"
            )
        expected_x = x_sign * row * scale_x
        expected_z = column * scale_z
        if abs(position[0] - expected_x) > tolerance_x or abs(position[2] - expected_z) > tolerance_z:
            raise TerrainGlbRepackError(
                "Terrain GLB moved vertices horizontally, TerrainData supports height only grid edits"
            )
        value = round(position[1] / scale_y * 32767.0)
        value = max(0, min(65535, value))
        grid_index = column + row * width
        existing = heights[grid_index]
        if existing is not None and existing != value:
            raise TerrainGlbRepackError(
                "Terrain GLB has duplicate grid vertices with conflicting heights"
            )
        heights[grid_index] = value
    missing = sum(value is None for value in heights)
    if missing:
        raise TerrainGlbRepackError(
            f"Terrain GLB is missing {missing} heightfield grid vertices"
        )
    return [int(value) for value in heights]

def encode_u16_values(values: list[int], endian: str) -> bytes:
    output = bytearray(len(values) * 2)
    for index, value in enumerate(values):
        struct.pack_into(endian + "H", output, index * 2, value)
    return bytes(output)

def find_terrain_object(asset: SerializedAsset, path_id: int) -> SerializedObject:
    for obj in asset.objects:
        if obj.path_id == path_id and obj.class_id == 156:
            return obj
    raise TerrainGlbRepackError(
        f"TerrainData path {path_id} wasnt found in {asset.relative_path}"
    )

def read_object_bytes(asset: SerializedAsset, obj: SerializedObject) -> bytes:
    with asset.path.open("rb") as handle:
        handle.seek(obj.byte_start)
        data = handle.read(obj.byte_size)
    if len(data) != obj.byte_size:
        raise TerrainGlbRepackError("TerrainData object data is truncated")
    return data

def identity_matrix() -> tuple[float, ...]:
    return (
        1.0, 0.0, 0.0, 0.0,
        0.0, 1.0, 0.0, 0.0,
        0.0, 0.0, 1.0, 0.0,
        0.0, 0.0, 0.0, 1.0,
    )

def trs_matrix(
    translation: tuple[float, ...],
    rotation: tuple[float, ...],
    scale: tuple[float, ...],
) -> tuple[float, ...]:
    x, y, z, w = rotation
    length = math.sqrt(x * x + y * y + z * z + w * w)
    if length > 1e-12:
        x, y, z, w = x / length, y / length, z / length, w / length
    sx, sy, sz = scale
    return (
        (1 - 2 * (y * y + z * z)) * sx,
        (2 * (x * y + z * w)) * sx,
        (2 * (x * z - y * w)) * sx,
        0.0,
        (2 * (x * y - z * w)) * sy,
        (1 - 2 * (x * x + z * z)) * sy,
        (2 * (y * z + x * w)) * sy,
        0.0,
        (2 * (x * z + y * w)) * sz,
        (2 * (y * z - x * w)) * sz,
        (1 - 2 * (x * x + y * y)) * sz,
        0.0,
        translation[0],
        translation[1],
        translation[2],
        1.0,
    )

def multiply_matrix(left: tuple[float, ...], right: tuple[float, ...]) -> tuple[float, ...]:
    output = [0.0] * 16
    for column in range(4):
        for row in range(4):
            output[column * 4 + row] = sum(
                left[index * 4 + row] * right[column * 4 + index]
                for index in range(4)
            )
    return tuple(output)

def transform_point(
    matrix: tuple[float, ...],
    point: tuple[float, float, float],
) -> tuple[float, float, float]:
    x, y, z = point
    return (
        matrix[0] * x + matrix[4] * y + matrix[8] * z + matrix[12],
        matrix[1] * x + matrix[5] * y + matrix[9] * z + matrix[13],
        matrix[2] * x + matrix[6] * y + matrix[10] * z + matrix[14],
    )

def vector_list(
    value: Any,
    length: int,
    default: tuple[float, ...],
) -> tuple[float, ...]:
    if not isinstance(value, list):
        return default
    if len(value) != length:
        raise TerrainGlbRepackError(f"GLB transform vector must contain {length} values")
    return tuple(float(item) for item in value)

def align_value(value: int, alignment: int) -> int:
    return (value + alignment - 1) // alignment * alignment

def require_dict(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TerrainGlbRepackError(f"{label} must be an object")
    return value

def require_list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise TerrainGlbRepackError(f"{label} must be an array")
    return value
