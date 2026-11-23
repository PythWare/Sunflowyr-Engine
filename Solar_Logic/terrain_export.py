from __future__ import annotations
import json, math, sys
from array import array
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from .binary import EndianReader
from .class_ids import class_name
from .model_context import ModelContextIndex, PPtr, TerrainColliderRecord, TerrainComponentRecord
from .serialized import SerializedAsset, SerializedObject

ReservePath = Callable[[Path, str], Path]

class TerrainExportError(Exception):
    pass

@dataclass(slots=True)
class TerrainDataRecord:
    name: str
    width: int
    height: int
    thickness: float
    levels: int
    scale: tuple[float, float, float]
    heights: array
    document: dict
    binary_payloads: dict[str, bytes]

@dataclass(slots=True)
class TerrainSidecarResult:
    json_path: Path
    binary_paths: dict[str, Path]
    document: dict
    metrics: dict

def read_terrain_data(
    context: ModelContextIndex,
    asset: SerializedAsset,
    obj: SerializedObject,
) -> TerrainDataRecord:
    with asset.path.open("rb") as handle:
        reader = EndianReader(handle, asset.endian)
        reader.seek(obj.byte_start)
        object_end = obj.byte_start + obj.byte_size

        name = reader.aligned_string(max_length=4096)
        splats = [read_splat(reader, context, asset) for index in range(read_count(reader, 1024, "terrain splats"))]
        alpha_textures = [
            pointer_document(context, asset.relative_path, read_pointer(reader, asset))
            for index in range(read_count(reader, 1024, "terrain alpha textures"))
        ]
        splat_database = {
            "m_Splats": splats,
            "m_AlphaTextures": alpha_textures,
            "m_AlphamapResolution": reader.i32(),
            "m_BaseMapResolution": reader.i32(),
            "m_ColorSpace": reader.i32(),
            "m_MaterialRequiresMetallic": reader.bool(),
            "m_MaterialRequiresSmoothness": reader.bool(),
        }
        reader.align()

        layer_bytes = bytearray()
        object_bytes = bytearray()
        patches: list[dict] = []
        for index in range(read_count(reader, 100_000, "terrain detail patches")):
            bounds = {
                "m_Center": read_vector(reader, 3),
                "m_Extent": read_vector(reader, 3),
            }
            layer_data = read_byte_array(reader, 100_000_000, "terrain detail layer indices")
            layer_offset = len(layer_bytes)
            layer_bytes.extend(layer_data)
            object_data = read_byte_array(reader, 100_000_000, "terrain detail object counts")
            object_offset = len(object_bytes)
            object_bytes.extend(object_data)
            patches.append(
                {
                    "bounds": bounds,
                    "layerIndices": binary_slice("detailLayerIndices", layer_offset, len(layer_data), "uint8"),
                    "numberOfObjects": binary_slice(
                        "detailObjectCounts",
                        object_offset,
                        len(object_data),
                        "uint8",
                    ),
                }
            )

        detail_prototypes = [
            read_detail_prototype(reader, context, asset)
            for index in range(read_count(reader, 4096, "terrain detail prototypes"))
        ]
        patch_count = reader.i32()
        patch_samples = reader.i32()
        random_rotations = [
            read_vector(reader, 4)
            for index in range(read_count(reader, 1_000_000, "terrain random rotations"))
        ]
        detail_database = {
            "serializedVersion": 2,
            "m_Patches": patches,
            "m_DetailPrototypes": detail_prototypes,
            "m_PatchCount": patch_count,
            "m_PatchSamples": patch_samples,
            "m_RandomRotations": random_rotations,
            "WavingGrassTint": read_color(reader),
            "m_WavingGrassStrength": clean_float(reader.f32()),
            "m_WavingGrassAmount": clean_float(reader.f32()),
            "m_WavingGrassSpeed": clean_float(reader.f32()),
        }

        tree_instances = [
            read_tree_instance(reader)
            for index in range(read_count(reader, 10_000_000, "terrain tree instances"))
        ]
        tree_prototypes = [
            {
                "prefab": pointer_document(context, asset.relative_path, read_pointer(reader, asset)),
                "bendFactor": clean_float(reader.f32()),
            }
            for index in range(read_count(reader, 4096, "terrain tree prototypes"))
        ]
        preload_textures = [
            pointer_document(context, asset.relative_path, read_pointer(reader, asset))
            for index in range(read_count(reader, 4096, "terrain preload textures"))
        ]

        height_count = read_count(reader, 100_000_000, "terrain heights")
        height_raw = reader.read(height_count * 2)
        reader.align()
        error_count = read_count(reader, 100_000_000, "terrain precomputed errors")
        error_raw = reader.read(error_count * 4)
        minmax_count = read_count(reader, 100_000_000, "terrain min/max patch heights")
        minmax_raw = reader.read(minmax_count * 4)
        width = reader.i32()
        height = reader.i32()
        thickness = reader.f32()
        levels = reader.i32()
        scale_values = tuple(reader.f32() for index in range(3))

        if reader.tell() != object_end:
            raise TerrainExportError(
                f"TerrainData parser ended at 0x{reader.tell():x}, expected 0x{object_end:x}"
            )
        if width <= 1 or height <= 1 or width > 8193 or height > 8193:
            raise TerrainExportError(f"invalid TerrainData dimensions {width}x{height}")
        if height_count != width * height:
            raise TerrainExportError(
                f"height sample count {height_count} doesnt match {width}x{height}"
            )
        if patch_count > 0 and len(patches) != patch_count * patch_count:
            raise TerrainExportError(
                f"detail patch count {len(patches)} doesnt match {patch_count}x{patch_count}"
            )

    heights, height_bytes = numeric_array(height_raw, "H", asset.endian)
    error_values, error_bytes = numeric_array(error_raw, "f", asset.endian)
    minmax_values, minmax_bytes = numeric_array(minmax_raw, "f", asset.endian)
    terrain_key = context.key_for(asset, obj.path_id)
    components = [
        terrain_component_document(context, component)
        for component in context.terrain_components_by_data.get(terrain_key, [])
    ]
    colliders = [
        terrain_collider_document(context, collider)
        for collider in context.terrain_colliders_by_data.get(terrain_key, [])
    ]
    document = {
        "format": "sunflowyr-terrain-data",
        "version": 1,
        "game": "rwby-ge",
        "unityVersion": asset.unity_version,
        "source": {
            "container": asset.relative_path,
            "pathId": obj.path_id,
            "classId": obj.class_id,
            "className": class_name(obj.class_id),
            "byteOffset": obj.byte_start,
            "byteSize": obj.byte_size,
        },
        "m_Name": name,
        "m_SplatDatabase": splat_database,
        "m_DetailDatabase": detail_database,
        "m_TreeInstances": tree_instances,
        "m_TreePrototypes": tree_prototypes,
        "m_PreloadTextureAtlasData": preload_textures,
        "m_Heightmap": {
            "m_Heights": binary_array("heights", height_count, "uint16", [height, width]),
            "m_PrecomputedError": binary_array("precomputedError", error_count, "float32"),
            "m_MinMaxPatchHeights": binary_array("minMaxPatchHeights", minmax_count, "float32"),
            "m_Width": width,
            "m_Height": height,
            "m_Thickness": clean_float(thickness),
            "m_Levels": levels,
            "m_Scale": vector_document(scale_values),
            "heightNormalizationDivisor": 32767.0,
        },
        "terrainComponents": components,
        "terrainColliders": colliders,
        "binaryFiles": {},
    }
    payloads = {
        "heights": height_bytes,
        "precomputedError": error_bytes,
        "minMaxPatchHeights": minmax_bytes,
        "detailLayerIndices": bytes(layer_bytes),
        "detailObjectCounts": bytes(object_bytes),
    }
    document["summary"] = {
        "splatLayerCount": len(splats),
        "alphaTextureCount": len(alpha_textures),
        "detailPatchCount": len(patches),
        "detailPrototypeCount": len(detail_prototypes),
        "treeInstanceCount": len(tree_instances),
        "treePrototypeCount": len(tree_prototypes),
        "terrainComponentCount": len(components),
        "terrainColliderCount": len(colliders),
        "heightSampleCount": len(heights),
        "precomputedErrorCount": len(error_values),
        "minMaxPatchHeightCount": len(minmax_values),
    }
    return TerrainDataRecord(
        name=name,
        width=width,
        height=height,
        thickness=thickness,
        levels=levels,
        scale=scale_values,
        heights=heights,
        document=document,
        binary_payloads=payloads,
    )

def write_terrain_sidecars(
    terrain: TerrainDataRecord,
    output_root: Path,
    asset: SerializedAsset,
    obj: SerializedObject,
    reserve_path: ReservePath,
) -> TerrainSidecarResult:
    source_key = f"{asset.relative_path}:{obj.path_id}"
    safe_name = safe_filename(terrain.name) or "TerrainData"
    json_path = reserve_path(
        output_root
        / "converted"
        / "terrain_json"
        / "156_TerrainData"
        / f"{safe_name}__path{obj.path_id}.json",
        f"{source_key}:terrain-json",
    )
    json_path.parent.mkdir(parents=True, exist_ok=True)
    role_suffixes = {
        "heights": "heights.u16le.bin",
        "precomputedError": "precomputed_error.f32le.bin",
        "minMaxPatchHeights": "minmax_patch_heights.f32le.bin",
        "detailLayerIndices": "detail_layer_indices.u8.bin",
        "detailObjectCounts": "detail_object_counts.u8.bin",
    }
    binary_paths: dict[str, Path] = {}
    for role, payload in terrain.binary_payloads.items():
        if not payload:
            continue
        suffix = role_suffixes[role]
        path = reserve_path(
            json_path.with_name(f"{json_path.stem}.{suffix}"),
            f"{source_key}:terrain-{role}",
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        binary_paths[role] = path
        terrain.document["binaryFiles"][role] = {
            "path": path.name,
            "byteLength": len(payload),
            "byteOrder": "little-endian",
        }
    json_path.write_text(
        json.dumps(terrain.document, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    metrics = dict(terrain.document["summary"])
    metrics.update(
        {
            "width": terrain.width,
            "height": terrain.height,
            "scale": list(terrain.scale),
            "binaryFileCount": len(binary_paths),
        }
    )
    return TerrainSidecarResult(json_path, binary_paths, terrain.document, metrics)

def read_splat(reader: EndianReader, context: ModelContextIndex, asset: SerializedAsset) -> dict:
    return {
        "texture": pointer_document(context, asset.relative_path, read_pointer(reader, asset)),
        "normalMap": pointer_document(context, asset.relative_path, read_pointer(reader, asset)),
        "tileSize": read_vector(reader, 2),
        "tileOffset": read_vector(reader, 2),
        "specularMetallic": read_vector(reader, 4),
        "smoothness": clean_float(reader.f32()),
    }

def read_detail_prototype(
    reader: EndianReader,
    context: ModelContextIndex,
    asset: SerializedAsset,
) -> dict:
    document = {
        "serializedVersion": 2,
        "prototype": pointer_document(context, asset.relative_path, read_pointer(reader, asset)),
        "prototypeTexture": pointer_document(context, asset.relative_path, read_pointer(reader, asset)),
        "minWidth": clean_float(reader.f32()),
        "maxWidth": clean_float(reader.f32()),
        "minHeight": clean_float(reader.f32()),
        "maxHeight": clean_float(reader.f32()),
        "noiseSpread": clean_float(reader.f32()),
        "bendFactor": clean_float(reader.f32()),
        "healthyColor": read_color(reader),
        "dryColor": read_color(reader),
        "lightmapFactor": clean_float(reader.f32()),
        "renderMode": reader.i32(),
        "usePrototypeMesh": reader.bool(),
    }
    reader.align()
    return document

def read_tree_instance(reader: EndianReader) -> dict:
    return {
        "position": read_vector(reader, 3),
        "widthScale": clean_float(reader.f32()),
        "heightScale": clean_float(reader.f32()),
        "rotation": clean_float(reader.f32()),
        "color": {"serializedVersion": 2, "rgba": reader.u32()},
        "lightmapColor": {"serializedVersion": 2, "rgba": reader.u32()},
        "index": reader.i32(),
    }

def terrain_component_document(
    context: ModelContextIndex,
    component: TerrainComponentRecord,
) -> dict:
    game_object_key = context.resolve(component.asset_key, component.game_object)
    game_object = context.game_objects.get(game_object_key) if game_object_key is not None else None
    return {
        "container": component.asset_key,
        "pathId": component.key[1],
        "gameObject": pointer_document(context, component.asset_key, component.game_object),
        "gameObjectName": game_object.name if game_object is not None else "",
        "enabled": component.enabled,
        "terrainData": pointer_document(context, component.asset_key, component.terrain_data),
        "materialTemplate": pointer_document(context, component.asset_key, component.material_template),
        "settings": component.settings,
    }

def terrain_collider_document(
    context: ModelContextIndex,
    collider: TerrainColliderRecord,
) -> dict:
    game_object_key = context.resolve(collider.asset_key, collider.game_object)
    game_object = context.game_objects.get(game_object_key) if game_object_key is not None else None
    return {
        "container": collider.asset_key,
        "pathId": collider.key[1],
        "gameObject": pointer_document(context, collider.asset_key, collider.game_object),
        "gameObjectName": game_object.name if game_object is not None else "",
        "material": pointer_document(context, collider.asset_key, collider.material),
        "enabled": collider.enabled,
        "terrainData": pointer_document(context, collider.asset_key, collider.terrain_data),
        "enableTreeColliders": collider.enable_tree_colliders,
    }

def pointer_document(
    context: ModelContextIndex,
    asset_key: str,
    pointer: PPtr,
) -> dict:
    document = {
        "fileId": pointer.file_id,
        "pathId": pointer.path_id,
        "resolved": False,
    }
    resolved_key = context.resolve(asset_key.replace("\\", "/").lower(), pointer)
    resolved_object = context.objects.get(resolved_key) if resolved_key is not None else None
    if resolved_key is None or resolved_object is None:
        return document
    name = ""
    if resolved_key in context.texture_names:
        name = context.texture_names[resolved_key]
    elif resolved_key in context.materials:
        name = context.materials[resolved_key].name
    elif resolved_key in context.game_objects:
        name = context.game_objects[resolved_key].name
    elif resolved_key in context.transforms:
        name = context.transforms[resolved_key].name
    document.update(
        {
            "resolved": True,
            "container": resolved_key[0],
            "resolvedPathId": resolved_key[1],
            "classId": resolved_object.class_id,
            "className": class_name(resolved_object.class_id),
            "name": name,
        }
    )
    return document

def read_pointer(reader: EndianReader, asset: SerializedAsset) -> PPtr:
    file_id = reader.i32()
    path_id = reader.i64() if asset.format_version >= 14 else reader.i32()
    return PPtr(file_id, path_id)

def read_count(reader: EndianReader, maximum: int, label: str) -> int:
    count = reader.i32()
    if count < 0 or count > maximum:
        raise TerrainExportError(f"invalid {label} count {count} at 0x{reader.tell() - 4:x}")
    return count

def read_byte_array(reader: EndianReader, maximum: int, label: str) -> bytes:
    data = reader.read(read_count(reader, maximum, label))
    reader.align()
    return data

def read_vector(reader: EndianReader, length: int) -> dict:
    values = [reader.f32() for index in range(length)]
    return vector_document(values)

def read_color(reader: EndianReader) -> dict:
    values = [clean_float(reader.f32()) for index in range(4)]
    return dict(zip(("r", "g", "b", "a"), values))

def vector_document(values) -> dict:
    labels = ("x", "y", "z", "w")
    return {labels[index]: clean_float(value) for index, value in enumerate(values)}

def clean_float(value: float) -> float | str:
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "Infinity" if value > 0 else "-Infinity"
    return value

def numeric_array(raw: bytes, typecode: str, source_endian: str) -> tuple[array, bytes]:
    values = array(typecode)
    values.frombytes(raw)
    source_little = source_endian == "<"
    host_little = sys.byteorder == "little"
    if source_little != host_little:
        values.byteswap()
    canonical = array(typecode, values)
    if not host_little:
        canonical.byteswap()
    return values, canonical.tobytes()

def binary_array(
    role: str,
    count: int,
    component_type: str,
    shape: list[int] | None = None,
) -> dict:
    document = {
        "binaryRole": role,
        "count": count,
        "componentType": component_type,
        "byteOrder": "little-endian",
    }
    if shape is not None:
        document["shape"] = shape
    return document

def binary_slice(
    role: str,
    offset: int,
    length: int,
    component_type: str,
) -> dict:
    return {
        "binaryRole": role,
        "byteOffset": offset,
        "byteLength": length,
        "componentType": component_type,
    }

def safe_filename(value: str) -> str:
    invalid = '<>:"/\\|?*'
    cleaned = "".join("_" if char in invalid or ord(char) < 32 else char for char in value).strip(" .")
    return cleaned[:180]
