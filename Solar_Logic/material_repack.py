from __future__ import annotations
import base64, json
from pathlib import Path
from typing import Any
from .mono_repack import EndianWriter, taildata_value
from .serialized import SerializedAsset, load_serialized_asset

class MaterialRepackError(ValueError):
    pass

def repack_material_json(json_path: Path, taildata: dict[str, Any], data_root: Path) -> bytes:
    document = json.loads(Path(json_path).read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise MaterialRepackError("Material JSON root must be an object")
    container = str(taildata_value(taildata, "container") or "")
    if not container:
        raise MaterialRepackError("taildata record doesnt name a Unity container")
    asset = load_serialized_asset((Path(data_root) / container).resolve(), Path(data_root))
    writer = EndianWriter(asset.endian)
    version = unity_version_tuple(asset.unity_version)

    write_editor_extension(writer, asset, document)
    writer.aligned_string(document.get("m_Name", ""))
    write_pptr(writer, asset, material_dict(document.get("m_Shader")))

    if version[0] >= 5:
        writer.aligned_string(document.get("m_ShaderKeywords", ""))
        writer.u32(int(document.get("m_LightmapFlags", 0)))
    if version[0] > 5 or (version[0] == 5 and version[1] >= 6):
        writer.bool(document.get("m_EnableInstancingVariants", False))
        writer.align()
    if version[0] > 4 or (version[0] == 4 and version[1] >= 3):
        writer.i32(int(document.get("m_CustomRenderQueue", -1)))
    if version[0] > 5 or (version[0] == 5 and version[1] >= 1):
        write_string_tag_map(writer, document.get("stringTagMap", []))
    if version[0] > 5 or (version[0] == 5 and version[1] >= 6):
        write_string_array(writer, document.get("disabledShaderPasses", []))

    saved = document.get("m_SavedProperties")
    if not isinstance(saved, dict):
        raise MaterialRepackError("Material JSON is missing m_SavedProperties")
    write_tex_envs(writer, asset, saved.get("m_TexEnvs", {}))
    if version[0] >= 2021:
        write_ints(writer, saved.get("m_Ints", {}))
    write_floats(writer, saved.get("m_Floats", {}))
    write_colors(writer, saved.get("m_Colors", {}))
    writer.write(decode_optional_tail(document))
    return writer.bytes()

def write_editor_extension(writer: EndianWriter, asset: SerializedAsset, document: dict[str, Any]) -> None:
    if asset.platform != -2:
        return
    writer.u32(int(document.get("m_ObjectHideFlags", 0) or 0))
    write_pptr(writer, asset, material_dict(document.get("m_PrefabParentObject")))
    write_pptr(writer, asset, material_dict(document.get("m_PrefabInternal")))

def write_pptr(writer: EndianWriter, asset: SerializedAsset, value: dict[str, Any]) -> None:
    writer.i32(int(value.get("m_FileID", value.get("file_id", 0))))
    path_id = int(value.get("m_PathID", value.get("path_id", 0)))
    if asset.format_version >= 14:
        writer.i64(path_id)
    else:
        writer.i32(path_id)

def write_string_tag_map(writer: EndianWriter, value: Any) -> None:
    pairs = string_pairs(value)
    writer.i32(len(pairs))
    for first, second in pairs:
        writer.aligned_string(first)
        writer.aligned_string(second)

def write_string_array(writer: EndianWriter, value: Any) -> None:
    if not isinstance(value, list):
        value = []
    writer.i32(len(value))
    for item in value:
        writer.aligned_string(item)

def write_tex_envs(writer: EndianWriter, asset: SerializedAsset, value: Any) -> None:
    items = mapping_items(value)
    writer.i32(len(items))
    for name, env in items:
        if not isinstance(env, dict):
            raise MaterialRepackError(f"texture environment '{name}' must be an object")
        writer.aligned_string(name)
        write_pptr(writer, asset, material_dict(env.get("m_Texture")))
        write_vector2(writer, env.get("m_Scale", [1.0, 1.0]))
        write_vector2(writer, env.get("m_Offset", [0.0, 0.0]))

def write_ints(writer: EndianWriter, value: Any) -> None:
    items = mapping_items(value)
    writer.i32(len(items))
    for name, item in items:
        writer.aligned_string(name)
        writer.i32(int(item))

def write_floats(writer: EndianWriter, value: Any) -> None:
    items = mapping_items(value)
    writer.i32(len(items))
    for name, item in items:
        writer.aligned_string(name)
        writer.f32(float(item))

def write_colors(writer: EndianWriter, value: Any) -> None:
    items = mapping_items(value)
    writer.i32(len(items))
    for name, color in items:
        writer.aligned_string(name)
        components = vector_values(color, 4, [0.0, 0.0, 0.0, 0.0])
        for item in components:
            writer.f32(item)

def write_vector2(writer: EndianWriter, value: Any) -> None:
    for item in vector_values(value, 2, [0.0, 0.0]):
        writer.f32(item)

def decode_optional_tail(document: dict[str, Any]) -> bytes:
    tail = document.get("solarUnparsedTail")
    if not isinstance(tail, dict):
        return b""
    if tail.get("encoding") != "base64":
        raise MaterialRepackError("solarUnparsedTail encoding must be base64")
    data_text = tail.get("data")
    if not isinstance(data_text, str):
        raise MaterialRepackError("solarUnparsedTail.data must be base64 text")
    data = base64.b64decode(data_text)
    expected_size = tail.get("size")
    if expected_size is not None and len(data) != int(expected_size):
        raise MaterialRepackError("decoded material tail bytes dont match solarUnparsedTail.size")
    return data

def material_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}

def mapping_items(value: Any) -> list[tuple[str, Any]]:
    if isinstance(value, dict):
        return [(str(key), item) for key, item in value.items()]
    if isinstance(value, list):
        out: list[tuple[str, Any]] = []
        for item in value:
            if isinstance(item, dict) and "name" in item:
                out.append((str(item["name"]), item.get("value")))
        return out
    return []

def string_pairs(value: Any) -> list[tuple[str, str]]:
    if isinstance(value, dict):
        return [(str(key), str(item)) for key, item in value.items()]
    if isinstance(value, list):
        pairs: list[tuple[str, str]] = []
        for item in value:
            if isinstance(item, dict):
                pairs.append((str(item.get("first", "")), str(item.get("second", ""))))
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                pairs.append((str(item[0]), str(item[1])))
        return pairs
    return []

def vector_values(value: Any, count: int, default: list[float]) -> list[float]:
    if isinstance(value, dict):
        keys = ["x", "y", "z", "w"][:count]
        if count == 4 and all(key in value for key in ("r", "g", "b", "a")):
            keys = ["r", "g", "b", "a"]
        return [float(value.get(key, default[index])) for index, key in enumerate(keys)]
    if isinstance(value, list):
        return [float(value[index]) if index < len(value) else default[index] for index in range(count)]
    return list(default[:count])

def unity_version_tuple(version: str) -> tuple[int, int, int, int]:
    nums: list[int] = []
    current = ""
    for char in version:
        if char.isdigit():
            current += char
        elif current:
            nums.append(int(current))
            current = ""
    if current:
        nums.append(int(current))
    nums.extend([0] * (4 - len(nums)))
    return tuple(nums[:4])
