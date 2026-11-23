from __future__ import annotations
import base64, json
from pathlib import Path
from typing import Any
from .binary import EndianReader
from .serialized import SerializedAsset, SerializedObject
from .paths import ensure_parent

class MaterialExportError(ValueError):
    pass

def export_material_json(
    asset: SerializedAsset,
    obj: SerializedObject,
    out_path: Path,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    with asset.path.open("rb") as handle:
        reader = EndianReader(handle, asset.endian)
        document, conversion = read_material_json(asset, reader, obj)

    ensure_parent(out_path)
    with out_path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(document, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    conversion["name"] = document.get("m_Name", "")
    return conversion

def read_material_json(asset: SerializedAsset, reader: EndianReader, obj: SerializedObject) -> tuple[dict[str, Any], dict[str, Any]]:
    reader.seek(obj.byte_start)
    editor_extension = read_editor_extension(reader, asset)
    name = reader.aligned_string(max_length=4096)
    shader = read_pptr(reader, asset)
    shader_keywords = reader.aligned_string(max_length=1_000_000)
    lightmap_flags = reader.u32()
    custom_render_queue = reader.i32()

    disabled_passes = []
    for _ in range(read_count(reader, 1_000_000)):
        disabled_passes.append(
            {
                "first": reader.aligned_string(max_length=4096),
                "second": reader.aligned_string(max_length=4096),
            }
        )

    textures: dict[str, Any] = {}
    for _ in range(read_count(reader, 1_000_000)):
        property_name = reader.aligned_string(max_length=4096)
        reject_duplicate_property(textures, property_name, "m_TexEnvs", name)
        textures[property_name] = {
            "m_Texture": read_pptr(reader, asset),
            "m_Scale": read_vector(reader, 2),
            "m_Offset": read_vector(reader, 2),
        }

    floats: dict[str, float] = {}
    for _ in range(read_count(reader, 1_000_000)):
        property_name = reader.aligned_string(max_length=4096)
        reject_duplicate_property(floats, property_name, "m_Floats", name)
        floats[property_name] = reader.f32()

    colors: dict[str, Any] = {}
    for _ in range(read_count(reader, 1_000_000)):
        property_name = reader.aligned_string(max_length=4096)
        reject_duplicate_property(colors, property_name, "m_Colors", name)
        colors[property_name] = read_vector(reader, 4)

    object_end = obj.byte_start + obj.byte_size
    if reader.tell() > object_end:
        raise MaterialExportError("Material reader ran past the object end")
    trailing = reader.read(object_end - reader.tell()) if reader.tell() < object_end else b""
    raw = read_object_bytes(asset, obj)
    document = {
        "m_Name": name,
        "m_ObjectHideFlags": None,
        "m_PrefabParentObject": editor_extension.get("m_PrefabParentObject"),
        "m_PrefabInternal": editor_extension.get("m_PrefabInternal"),
        "m_Shader": shader,
        "m_ShaderKeywords": shader_keywords,
        "m_LightmapFlags": lightmap_flags,
        "m_CustomRenderQueue": custom_render_queue,
        "stringTagMap": disabled_passes,
        "m_SavedProperties": {
            "m_TexEnvs": textures,
            "m_Floats": floats,
            "m_Colors": colors,
        },
    }
    if trailing:
        document["solarUnparsedTail"] = {
            "encoding": "base64",
            "size": len(trailing),
            "data": base64.b64encode(trailing).decode("ascii"),
        }
    return document, {
        "format": "material_json_unity5",
        "texturePropertyCount": len(textures),
        "floatPropertyCount": len(floats),
        "colorPropertyCount": len(colors),
        "rawFallbackSize": len(raw),
        "unparsedTailSize": len(trailing),
    }

def read_editor_extension(reader: EndianReader, asset: SerializedAsset) -> dict[str, Any]:
    if asset.platform != -2:
        return {}
    return {
        "m_ObjectHideFlags": reader.u32(),
        "m_PrefabParentObject": read_pptr(reader, asset),
        "m_PrefabInternal": read_pptr(reader, asset),
    }

def reject_duplicate_property(
    seen: dict[str, Any],
    property_name: str,
    section: str,
    material_name: str,
) -> None:
    if property_name not in seen:
        return
    raise MaterialExportError(
        f"material {material_name!r} lists {property_name!r} twice in {section}, export it as a raw object instead"
    )

def read_pptr(reader: EndianReader, asset: SerializedAsset) -> dict[str, int]:
    file_id = reader.i32()
    path_id = reader.i64() if asset.format_version >= 14 else reader.i32()
    return {"m_FileID": file_id, "m_PathID": path_id}

def read_vector(reader: EndianReader, length: int) -> list[float]:
    return [reader.f32() for _ in range(length)]

def read_count(reader: EndianReader, maximum: int) -> int:
    count = reader.i32()
    if count < 0 or count > maximum:
        raise MaterialExportError(f"invalid serialized array count {count}")
    return count

def read_object_bytes(asset: SerializedAsset, obj: SerializedObject) -> bytes:
    with asset.path.open("rb") as handle:
        handle.seek(obj.byte_start)
        data = handle.read(obj.byte_size)
    if len(data) != obj.byte_size:
        raise MaterialExportError("Material raw object data is truncated")
    return data
