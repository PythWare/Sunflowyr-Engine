from __future__ import annotations
import json, struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from .asset_bundle_writer import pack_aligned_string

MATERIAL_FORMAT_MARKER = "sunflowyr-new-material"
NULL_POINTER = struct.pack("<iq", 0, 0)

class NewMaterialAssetError(ValueError):
    pass

@dataclass(frozen=True, slots=True)
class BundlePointerRef:
    payload_offset: int
    source_name: str
    slot: str

def try_build_material_asset(
    json_path: Path,
    fallback_name: str,
) -> tuple[bytes, str, dict[str, object], tuple[BundlePointerRef, ...]] | None:
    forced = json_path.name.lower().endswith(".material.json")
    try:
        document = json.loads(Path(json_path).read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        if forced:
            raise NewMaterialAssetError(f"couldnt read Material JSON: {exc}") from exc
        return None
    if not isinstance(document, dict):
        if forced:
            raise NewMaterialAssetError("Material JSON root must be an object")
        return None
    if document.get("format") != MATERIAL_FORMAT_MARKER:
        if forced:
            raise NewMaterialAssetError(
                f'Material JSON must set "format": "{MATERIAL_FORMAT_MARKER}"'
            )
        return None

    name = clean_name(document.get("name", fallback_name), fallback_name)
    shader_name = str(document.get("shaderName") or "")
    shader_asset = str(document.get("shaderAsset") or "")
    if not shader_name and not shader_asset:
        raise NewMaterialAssetError(
            'Material JSON needs a "shaderName"'
        )
    shader_keywords = str(document.get("shaderKeywords") or "")
    lightmap_flags = int(document.get("lightmapFlags", 4))
    custom_render_queue = int(document.get("customRenderQueue", -1))
    string_tag_map = read_string_map(document.get("stringTagMap"), "stringTagMap")
    textures = read_texture_slots(document.get("textures"))
    floats = read_float_map(document.get("floats"), "floats")
    colors = read_color_map(document.get("colors"), "colors")

    output = bytearray(pack_aligned_string(name))
    pointer_refs: list[BundlePointerRef] = []
    if shader_asset:
        pointer_refs.append(
            BundlePointerRef(
                payload_offset=len(output),
                source_name=shader_asset,
                slot="shader",
            )
        )
    output.extend(NULL_POINTER)
    output.extend(pack_aligned_string(shader_keywords))
    output.extend(struct.pack("<I", lightmap_flags & 0xFFFFFFFF))
    output.extend(struct.pack("<i", custom_render_queue))
    output.extend(struct.pack("<i", len(string_tag_map)))
    for key, value in string_tag_map:
        output.extend(pack_aligned_string(key))
        output.extend(pack_aligned_string(value))

    output.extend(struct.pack("<i", len(textures)))
    for slot, entry in textures:
        output.extend(pack_aligned_string(slot))
        if entry["file"]:
            pointer_refs.append(
                BundlePointerRef(
                    payload_offset=len(output),
                    source_name=str(entry["file"]),
                    slot=f"textures.{slot}",
                )
            )
        output.extend(NULL_POINTER)
        output.extend(struct.pack("<ff", *entry["scale"]))
        output.extend(struct.pack("<ff", *entry["offset"]))

    output.extend(struct.pack("<i", len(floats)))
    for key, value in floats:
        output.extend(pack_aligned_string(key))
        output.extend(struct.pack("<f", value))

    output.extend(struct.pack("<i", len(colors)))
    for key, value in colors:
        output.extend(pack_aligned_string(key))
        output.extend(struct.pack("<ffff", *value))

    details: dict[str, object] = {
        "shaderName": shader_name,
        "shaderKeywords": shader_keywords,
        "lightmapFlags": lightmap_flags,
        "customRenderQueue": custom_render_queue,
        "textureSlots": {slot: entry["file"] for slot, entry in textures},
        "floatCount": len(floats),
        "colorCount": len(colors),
    }
    return bytes(output), name, details, tuple(pointer_refs)

def read_texture_slots(value: Any) -> list[tuple[str, dict[str, Any]]]:
    if value is None:
        return []
    if not isinstance(value, dict):
        raise NewMaterialAssetError('"textures" must be an object of slot name -> texture entry')
    slots: list[tuple[str, dict[str, Any]]] = []
    for slot, raw in value.items():
        if raw is None:
            entry: dict[str, Any] = {"file": "", "scale": (1.0, 1.0), "offset": (0.0, 0.0)}
        elif isinstance(raw, str):
            entry = {"file": raw, "scale": (1.0, 1.0), "offset": (0.0, 0.0)}
        elif isinstance(raw, dict):
            entry = {
                "file": str(raw.get("file") or ""),
                "scale": read_vector2(raw.get("scale"), (1.0, 1.0), f"textures.{slot}.scale"),
                "offset": read_vector2(raw.get("offset"), (0.0, 0.0), f"textures.{slot}.offset"),
            }
        else:
            raise NewMaterialAssetError(f'texture slot "{slot}" must be a file name, object, or null')
        slots.append((str(slot), entry))
    return slots

def read_vector2(value: Any, default: tuple[float, float], label: str) -> tuple[float, float]:
    if value is None:
        return default
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise NewMaterialAssetError(f"{label} must be a two-value list")
    return float(value[0]), float(value[1])

def read_string_map(value: Any, label: str) -> list[tuple[str, str]]:
    if value is None:
        return []
    if not isinstance(value, dict):
        raise NewMaterialAssetError(f'"{label}" must be an object of string -> string')
    return [(str(key), str(item)) for key, item in value.items()]

def read_float_map(value: Any, label: str) -> list[tuple[str, float]]:
    if value is None:
        return []
    if not isinstance(value, dict):
        raise NewMaterialAssetError(f'"{label}" must be an object of property name -> number')
    output: list[tuple[str, float]] = []
    for key, item in value.items():
        try:
            output.append((str(key), float(item)))
        except (TypeError, ValueError) as exc:
            raise NewMaterialAssetError(f'"{label}.{key}" must be a number') from exc
    return output

def read_color_map(value: Any, label: str) -> list[tuple[str, tuple[float, float, float, float]]]:
    if value is None:
        return []
    if not isinstance(value, dict):
        raise NewMaterialAssetError(f'"{label}" must be an object of property name -> [r, g, b, a]')
    output: list[tuple[str, tuple[float, float, float, float]]] = []
    for key, item in value.items():
        if isinstance(item, dict):
            color = (
                float(item.get("r", 0.0)),
                float(item.get("g", 0.0)),
                float(item.get("b", 0.0)),
                float(item.get("a", 1.0)),
            )
        elif isinstance(item, (list, tuple)) and len(item) == 4:
            color = (float(item[0]), float(item[1]), float(item[2]), float(item[3]))
        else:
            raise NewMaterialAssetError(f'"{label}.{key}" must be [r, g, b, a] or an r/g/b/a object')
        output.append((str(key), color))
    return output

def clean_name(value: Any, fallback: str) -> str:
    text = str(value or "").strip()
    return text or fallback
