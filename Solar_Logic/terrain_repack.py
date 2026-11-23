from __future__ import annotations
import json, math, struct
from pathlib import Path
from typing import Any
from .mono_repack import EndianWriter, taildata_value, write_pptr
from .serialized import load_serialized_asset

class TerrainRepackError(ValueError):
    pass

def repack_terrain_data_json(
    json_path: Path,
    taildata: dict[str, Any],
    data_root: Path,
) -> bytes:
    document = json.loads(Path(json_path).read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise TerrainRepackError("TerrainData JSON root must be an object")
    if document.get("format") != "sunflowyr-terrain-data":
        raise TerrainRepackError("TerrainData JSON format marker is missing")

    class_id = int(taildata_value(taildata, "classId", 0) or 0)
    if class_id != 156:
        raise TerrainRepackError(
            f"TerrainData JSON taildata expected class 156, found {class_id}"
        )
    container = str(taildata_value(taildata, "container") or "")
    if not container:
        source = document.get("source")
        if isinstance(source, dict):
            container = str(source.get("container") or "")
    if not container:
        raise TerrainRepackError("TerrainData JSON is missing its Unity container")

    root = Path(data_root).resolve()
    asset_path = (root / container).resolve()
    try:
        asset_path.relative_to(root)
    except ValueError as exc:
        raise TerrainRepackError(
            f"TerrainData container escapes the game data folder: {container}"
        ) from exc
    asset = load_serialized_asset(asset_path, root)
    writer = EndianWriter(asset.endian)

    writer.aligned_string(document.get("m_Name", ""))
    write_splat_database(writer, asset, require_dict(document.get("m_SplatDatabase"), "m_SplatDatabase"))
    write_detail_database(
        writer,
        asset,
        require_dict(document.get("m_DetailDatabase"), "m_DetailDatabase"),
        document,
        Path(json_path).parent,
    )
    write_tree_instances(writer, require_list(document.get("m_TreeInstances"), "m_TreeInstances"))
    write_tree_prototypes(
        writer,
        asset,
        require_list(document.get("m_TreePrototypes"), "m_TreePrototypes"),
    )
    write_pointer_array(
        writer,
        asset,
        require_list(document.get("m_PreloadTextureAtlasData"), "m_PreloadTextureAtlasData"),
    )
    write_heightmap(
        writer,
        asset.endian,
        require_dict(document.get("m_Heightmap"), "m_Heightmap"),
        document,
        Path(json_path).parent,
    )
    return writer.bytes()

def write_splat_database(writer: EndianWriter, asset: Any, database: dict[str, Any]) -> None:
    splats = require_list(database.get("m_Splats"), "m_SplatDatabase.m_Splats")
    writer.i32(len(splats))
    for index, value in enumerate(splats):
        splat = require_dict(value, f"m_SplatDatabase.m_Splats[{index}]")
        write_pointer(writer, asset, splat.get("texture"))
        write_pointer(writer, asset, splat.get("normalMap"))
        write_vector(writer, splat.get("tileSize"), 2, f"splat {index} tileSize")
        write_vector(writer, splat.get("tileOffset"), 2, f"splat {index} tileOffset")
        write_vector(
            writer,
            splat.get("specularMetallic"),
            4,
            f"splat {index} specularMetallic",
        )
        writer.f32(float_value(splat.get("smoothness", 0.0)))

    write_pointer_array(
        writer,
        asset,
        require_list(database.get("m_AlphaTextures"), "m_SplatDatabase.m_AlphaTextures"),
    )
    writer.i32(int(database.get("m_AlphamapResolution", 0)))
    writer.i32(int(database.get("m_BaseMapResolution", 0)))
    writer.i32(int(database.get("m_ColorSpace", 0)))
    writer.bool(database.get("m_MaterialRequiresMetallic", False))
    writer.bool(database.get("m_MaterialRequiresSmoothness", False))
    writer.align()

def write_detail_database(
    writer: EndianWriter,
    asset: Any,
    database: dict[str, Any],
    document: dict[str, Any],
    json_folder: Path,
) -> None:
    binaries = load_binary_files(document, json_folder)
    patches = require_list(database.get("m_Patches"), "m_DetailDatabase.m_Patches")
    writer.i32(len(patches))
    for index, value in enumerate(patches):
        patch = require_dict(value, f"m_DetailDatabase.m_Patches[{index}]")
        bounds = require_dict(patch.get("bounds"), f"detail patch {index} bounds")
        write_vector(
            writer,
            require_dict(bounds.get("m_Center"), f"detail patch {index} center"),
            3,
            f"detail patch {index} center",
        )
        write_vector(
            writer,
            require_dict(bounds.get("m_Extent"), f"detail patch {index} extent"),
            3,
            f"detail patch {index} extent",
        )
        write_byte_array(
            writer,
            binary_slice_bytes(
                binaries,
                require_dict(patch.get("layerIndices"), f"detail patch {index} layerIndices"),
            ),
        )
        write_byte_array(
            writer,
            binary_slice_bytes(
                binaries,
                require_dict(patch.get("numberOfObjects"), f"detail patch {index} numberOfObjects"),
            ),
        )

    prototypes = require_list(
        database.get("m_DetailPrototypes"),
        "m_DetailDatabase.m_DetailPrototypes",
    )
    writer.i32(len(prototypes))
    for index, value in enumerate(prototypes):
        prototype = require_dict(value, f"m_DetailDatabase.m_DetailPrototypes[{index}]")
        write_pointer(writer, asset, prototype.get("prototype"))
        write_pointer(writer, asset, prototype.get("prototypeTexture"))
        for key in (
            "minWidth",
            "maxWidth",
            "minHeight",
            "maxHeight",
            "noiseSpread",
            "bendFactor",
        ):
            writer.f32(float_value(prototype.get(key, 0.0)))
        write_color(writer, prototype.get("healthyColor"), f"detail prototype {index} healthyColor")
        write_color(writer, prototype.get("dryColor"), f"detail prototype {index} dryColor")
        writer.f32(float_value(prototype.get("lightmapFactor", 0.0)))
        writer.i32(int(prototype.get("renderMode", 0)))
        writer.bool(prototype.get("usePrototypeMesh", False))
        writer.align()

    writer.i32(int(database.get("m_PatchCount", 0)))
    writer.i32(int(database.get("m_PatchSamples", 0)))
    rotations = require_list(database.get("m_RandomRotations"), "m_DetailDatabase.m_RandomRotations")
    writer.i32(len(rotations))
    for index, rotation in enumerate(rotations):
        write_vector(writer, rotation, 4, f"random rotation {index}")
    write_color(writer, database.get("WavingGrassTint"), "WavingGrassTint")
    writer.f32(float_value(database.get("m_WavingGrassStrength", 0.0)))
    writer.f32(float_value(database.get("m_WavingGrassAmount", 0.0)))
    writer.f32(float_value(database.get("m_WavingGrassSpeed", 0.0)))

def write_tree_instances(writer: EndianWriter, instances: list[Any]) -> None:
    writer.i32(len(instances))
    for index, value in enumerate(instances):
        instance = require_dict(value, f"m_TreeInstances[{index}]")
        write_vector(writer, instance.get("position"), 3, f"tree instance {index} position")
        writer.f32(float_value(instance.get("widthScale", 1.0)))
        writer.f32(float_value(instance.get("heightScale", 1.0)))
        writer.f32(float_value(instance.get("rotation", 0.0)))
        writer.u32(color32_value(instance.get("color"), f"tree instance {index} color"))
        writer.u32(
            color32_value(
                instance.get("lightmapColor"),
                f"tree instance {index} lightmapColor",
            )
        )
        writer.i32(int(instance.get("index", 0)))

def write_tree_prototypes(
    writer: EndianWriter,
    asset: Any,
    prototypes: list[Any],
) -> None:
    writer.i32(len(prototypes))
    for index, value in enumerate(prototypes):
        prototype = require_dict(value, f"m_TreePrototypes[{index}]")
        write_pointer(writer, asset, prototype.get("prefab"))
        writer.f32(float_value(prototype.get("bendFactor", 0.0)))

def write_heightmap(
    writer: EndianWriter,
    endian: str,
    heightmap: dict[str, Any],
    document: dict[str, Any],
    json_folder: Path,
) -> None:
    binaries = load_binary_files(document, json_folder)
    heights = binary_array_bytes(
        binaries,
        require_dict(heightmap.get("m_Heights"), "m_Heightmap.m_Heights"),
        2,
        endian,
    )
    errors = binary_array_bytes(
        binaries,
        require_dict(heightmap.get("m_PrecomputedError"), "m_Heightmap.m_PrecomputedError"),
        4,
        endian,
    )
    minmax = binary_array_bytes(
        binaries,
        require_dict(heightmap.get("m_MinMaxPatchHeights"), "m_Heightmap.m_MinMaxPatchHeights"),
        4,
        endian,
    )
    writer.i32(len(heights) // 2)
    writer.write(heights)
    writer.align()
    writer.i32(len(errors) // 4)
    writer.write(errors)
    writer.i32(len(minmax) // 4)
    writer.write(minmax)
    writer.i32(int(heightmap.get("m_Width", 0)))
    writer.i32(int(heightmap.get("m_Height", 0)))
    writer.f32(float_value(heightmap.get("m_Thickness", 0.0)))
    writer.i32(int(heightmap.get("m_Levels", 0)))
    write_vector(writer, heightmap.get("m_Scale"), 3, "m_Heightmap.m_Scale")

def write_pointer_array(writer: EndianWriter, asset: Any, values: list[Any]) -> None:
    writer.i32(len(values))
    for value in values:
        write_pointer(writer, asset, value)

def write_pointer(writer: EndianWriter, asset: Any, value: Any) -> None:
    pointer = require_dict(value, "PPtr")
    write_pptr(
        writer,
        asset,
        {
            "m_FileID": int(pointer.get("fileId", pointer.get("m_FileID", 0))),
            "m_PathID": int(pointer.get("pathId", pointer.get("m_PathID", 0))),
        },
    )

def write_vector(writer: EndianWriter, value: Any, length: int, label: str) -> None:
    if isinstance(value, dict):
        labels = ("x", "y", "z", "w")
        values = [value.get(labels[index], 0.0) for index in range(length)]
    elif isinstance(value, list):
        values = value
    else:
        raise TerrainRepackError(f"{label} must be an object or array")
    if len(values) != length:
        raise TerrainRepackError(f"{label} must contain {length} values")
    for item in values:
        writer.f32(float_value(item))

def write_color(writer: EndianWriter, value: Any, label: str) -> None:
    color = require_dict(value, label)
    for key in ("r", "g", "b", "a"):
        writer.f32(float_value(color.get(key, 0.0)))

def write_byte_array(writer: EndianWriter, data: bytes) -> None:
    writer.i32(len(data))
    writer.write(data)
    writer.align()

def load_binary_files(document: dict[str, Any], json_folder: Path) -> dict[str, bytes]:
    records = require_dict(document.get("binaryFiles"), "binaryFiles")
    output: dict[str, bytes] = {}
    for role, value in records.items():
        record = require_dict(value, f"binaryFiles.{role}")
        relative = str(record.get("path") or "")
        if not relative:
            raise TerrainRepackError(f"binaryFiles.{role} is missing its path")
        path = (json_folder / relative).resolve()
        try:
            path.relative_to(json_folder.resolve())
        except ValueError as exc:
            raise TerrainRepackError(
                f"TerrainData binary path escapes the JSON folder: {relative}"
            ) from exc
        if not path.is_file():
            raise TerrainRepackError(f"TerrainData binary companion wasnt found: {path.name}")
        data = path.read_bytes()
        expected = int(record.get("byteLength", len(data)))
        if len(data) != expected:
            raise TerrainRepackError(
                f"{path.name} contains {len(data)} bytes, expected {expected}"
            )
        output[str(role)] = data
    return output

def binary_slice_bytes(binaries: dict[str, bytes], record: dict[str, Any]) -> bytes:
    role = str(record.get("binaryRole") or "")
    data = binaries.get(role)
    if data is None:
        if int(record.get("byteLength", 0)) == 0:
            return b""
        raise TerrainRepackError(f"TerrainData binary role is missing: {role}")
    offset = int(record.get("byteOffset", 0))
    length = int(record.get("byteLength", 0))
    if offset < 0 or length < 0 or offset + length > len(data):
        raise TerrainRepackError(f"TerrainData binary slice exceeds {role}")
    return data[offset : offset + length]

def binary_array_bytes(
    binaries: dict[str, bytes],
    record: dict[str, Any],
    component_size: int,
    endian: str,
) -> bytes:
    role = str(record.get("binaryRole") or "")
    data = binaries.get(role)
    if data is None:
        if int(record.get("count", -1)) == 0:
            return b""
        raise TerrainRepackError(f"TerrainData binary role is missing: {role}")
    count = int(record.get("count", -1))
    expected = count * component_size
    if count < 0 or len(data) != expected:
        raise TerrainRepackError(
            f"TerrainData {role} contains {len(data)} bytes, expected {expected}"
        )
    if endian == "<" or component_size == 1:
        return data
    swapped = bytearray(len(data))
    for offset in range(0, len(data), component_size):
        swapped[offset : offset + component_size] = data[offset : offset + component_size][::-1]
    return bytes(swapped)

def color32_value(value: Any, label: str) -> int:
    color = require_dict(value, label)
    return int(color.get("rgba", 0)) & 0xFFFFFFFF

def float_value(value: Any) -> float:
    if isinstance(value, str):
        if value == "NaN":
            return struct.unpack("<f", b"\x01\x00\x80\x7f")[0]
        if value == "Infinity":
            return math.inf
        if value == "-Infinity":
            return -math.inf
    return float(value)

def require_dict(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TerrainRepackError(f"{label} must be an object")
    return value

def require_list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise TerrainRepackError(f"{label} must be an array")
    return value
