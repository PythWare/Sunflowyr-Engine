from __future__ import annotations
import hashlib, json, base64
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from .binary import EndianReader
from .serialized import SerializedAsset, SerializedObject

class FontExportError(ValueError):
    pass

@dataclass(slots=True)
class FontExportResult:
    json_path: Path
    json_metadata: dict[str, Any]
    font_path: Path | None
    font_metadata: dict[str, Any] | None

def export_font_view(
    asset: SerializedAsset,
    obj: SerializedObject,
    output_root: Path,
    reserve_path: Callable[[Path, str], Path],
    object_metadata: dict[str, Any],
) -> FontExportResult:
    with asset.path.open("rb") as handle:
        reader = EndianReader(handle, asset.endian)
        document, conversion, font_bytes = read_font_json(asset, reader, obj)

    class_dir = f"{obj.class_id:03d}_{safe_name(obj.type_name)}"
    base_name = safe_name(document.get("m_Name") or obj.type_name) or obj.type_name
    json_path = reserve_path(
        output_root / "converted" / "font_json" / class_dir / f"{base_name}__path{obj.path_id}.json",
        asset.relative_path,
    )

    font_path = None
    font_metadata = None
    if font_bytes:
        extension = font_extension(font_bytes)
        font_path = reserve_path(
            output_root / "converted" / "font" / class_dir / f"{base_name}__path{obj.path_id}{extension}",
            asset.relative_path,
        )
        font_path.parent.mkdir(parents=True, exist_ok=True)
        font_path.write_bytes(font_bytes)
        document["m_FontData"] = {
            "externalFile": relative_path(font_path, json_path.parent),
            "size": len(font_bytes),
            "format": extension.lstrip("."),
            "sha256": hashlib.sha256(font_bytes).hexdigest(),
        }
        font_metadata = dict(object_metadata)
        font_metadata.update(
            {
                "kind": "font_data",
                "view_of_kind": "font_json",
                "font_data_size": len(font_bytes),
                "font_data_size_field_offset": conversion["fontDataSizeFieldOffset"],
                "font_data_offset_absolute": conversion["fontDataOffsetAbsolute"],
                "font_data_offset_relative": conversion["fontDataOffsetRelative"],
                "font_data_extension": extension,
                "conversion": {
                    "format": "font_data_file",
                    "sourceKind": "font_json",
                    "fontDataSize": len(font_bytes),
                },
                "writeback_note": "Font file is a direct editable view, repacking replaces the Font object's embedded font data.",
            }
        )
    else:
        document["m_FontData"] = {
            "externalFile": None,
            "size": 0,
            "format": "none",
            "sha256": "",
        }

    json_path.parent.mkdir(parents=True, exist_ok=True)
    with json_path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(document, handle, ensure_ascii=False, indent=2)
        handle.write("\n")

    json_metadata = dict(object_metadata)
    json_metadata.update(
        {
            "kind": "font_json",
            "font_data_size": len(font_bytes),
            "font_data_size_field_offset": conversion["fontDataSizeFieldOffset"],
            "font_data_offset_absolute": conversion["fontDataOffsetAbsolute"],
            "font_data_offset_relative": conversion["fontDataOffsetRelative"],
            "conversion": conversion,
            "writeback_note": "Font JSON rebuilds Unity Font object bytes and can reference the sibling exported font file.",
        }
    )
    return FontExportResult(json_path, json_metadata, font_path, font_metadata)

def read_font_json(asset: SerializedAsset, reader: EndianReader, obj: SerializedObject) -> tuple[dict[str, Any], dict[str, Any], bytes]:
    version = unity_version_tuple(asset.unity_version)
    if version[0] > 5 or (version[0] == 5 and version[1] >= 5):
        raise FontExportError("Unity 5.5+ Font layout is not supporte for RWBY yet")

    reader.seek(obj.byte_start)
    object_header = read_object_header(reader, asset)
    name = reader.aligned_string(max_length=4096)
    ascii_start_offset = reader.i32()
    kerning = reader.f32()
    line_spacing = reader.f32()
    character_spacing = reader.i32()
    character_padding = reader.i32()
    convert_case = reader.i32()
    default_material = read_pptr(reader, asset)
    character_rects = read_character_rects(reader, version)
    texture = read_pptr(reader, asset)
    kerning_values = read_kerning_values(reader)
    pixel_scale = reader.f32()
    font_data_size_field_offset = reader.tell()
    font_data_size = reader.i32()
    if font_data_size < 0 or font_data_size > obj.byte_size:
        raise FontExportError(f"invalid font data size {font_data_size}")
    font_data_offset_absolute = reader.tell()
    font_bytes = reader.read(font_data_size) if font_data_size else b""
    reader.align()
    font_size = reader.f32()
    ascent = reader.f32()
    descent = reader.f32()
    default_style = reader.u32()
    font_names = [reader.aligned_string(max_length=4096) for _ in range(read_count(reader, 4096))]
    fallback_fonts = [read_pptr(reader, asset) for _ in range(read_count(reader, 4096))]
    reader.align()
    font_rendering_mode = reader.i32()
    object_end = obj.byte_start + obj.byte_size
    if reader.tell() > object_end:
        raise FontExportError(f"Font reader ran past object end at 0x{reader.tell():x}; expected 0x{object_end:x}")
    trailing = reader.read(object_end - reader.tell()) if reader.tell() < object_end else b""

    document = {
        "m_ObjectHideFlags": object_header.get("m_ObjectHideFlags"),
        "m_PrefabParentObject": object_header.get("m_PrefabParentObject"),
        "m_PrefabInternal": object_header.get("m_PrefabInternal"),
        "m_Name": name,
        "m_AsciiStartOffset": ascii_start_offset,
        "m_Tracking": kerning,
        "m_LineSpacing": line_spacing,
        "m_CharacterSpacing": character_spacing,
        "m_CharacterPadding": character_padding,
        "m_ConvertCase": convert_case,
        "m_DefaultMaterial": default_material,
        "m_CharacterRects": character_rects,
        "m_Texture": texture,
        "m_KerningValues": kerning_values,
        "m_PixelScale": pixel_scale,
        "m_FontSize": font_size,
        "m_Ascent": ascent,
        "m_Descent": descent,
        "m_DefaultStyle": default_style,
        "m_FontNames": font_names,
        "m_FallbackFonts": fallback_fonts,
        "m_FontRenderingMode": font_rendering_mode,
    }
    if trailing:
        document["solarUnparsedTail"] = {
            "encoding": "base64",
            "size": len(trailing),
            "data": base64.b64encode(trailing).decode("ascii"),
        }
    conversion = {
        "format": "font_json_unity5_pre55",
        "characterRectCount": len(character_rects),
        "kerningValueCount": len(kerning_values),
        "fontDataSize": len(font_bytes),
        "fontDataSizeFieldOffset": font_data_size_field_offset,
        "fontDataOffsetAbsolute": font_data_offset_absolute,
        "fontDataOffsetRelative": font_data_offset_absolute - obj.byte_start,
        "fontDataExtension": font_extension(font_bytes) if font_bytes else "",
        "unparsedTailSize": len(trailing),
    }
    return document, conversion, font_bytes

def read_object_header(reader: EndianReader, asset: SerializedAsset) -> dict[str, Any]:
    if asset.platform != -2:
        return {}
    return {
        "m_ObjectHideFlags": reader.u32(),
        "m_PrefabParentObject": read_pptr(reader, asset),
        "m_PrefabInternal": read_pptr(reader, asset),
    }

def read_pptr(reader: EndianReader, asset: SerializedAsset) -> dict[str, int]:
    file_id = reader.i32()
    path_id = reader.i64() if asset.format_version >= 14 else reader.i32()
    return {"m_FileID": file_id, "m_PathID": path_id}

def read_character_rects(reader: EndianReader, version: tuple[int, int, int, int]) -> list[dict[str, Any]]:
    count = read_count(reader, 1_000_000)
    values = []
    for _ in range(count):
        item = {
            "index": reader.i32(),
            "uv": read_rect(reader),
            "vert": read_rect(reader),
            "advance": reader.f32(),
        }
        if version[0] >= 4:
            item["flipped"] = reader.bool()
            reader.align()
        values.append(item)
    return values

def read_rect(reader: EndianReader) -> dict[str, float]:
    return {
        "x": reader.f32(),
        "y": reader.f32(),
        "width": reader.f32(),
        "height": reader.f32(),
    }

def read_kerning_values(reader: EndianReader) -> list[dict[str, Any]]:
    count = read_count(reader, 1_000_000)
    values = []
    for _ in range(count):
        values.append(
            {
                "first": reader.i16(),
                "second": reader.i16(),
                "amount": reader.f32(),
            }
        )
    return values

def read_count(reader: EndianReader, maximum: int) -> int:
    count = reader.i32()
    if count < 0 or count > maximum:
        raise FontExportError(f"invalid serialized array count {count}")
    return count

def font_extension(data: bytes) -> str:
    if data.startswith(b"OTTO"):
        return ".otf"
    if data.startswith(b"\x00\x01\x00\x00") or data.startswith(b"true"):
        return ".ttf"
    if data.startswith(b"ttcf"):
        return ".ttc"
    if data.startswith(b"wOFF"):
        return ".woff"
    if data.startswith(b"wOF2"):
        return ".woff2"
    return ".fontdata"

def relative_path(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)

def safe_name(name: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in " ._-" else "_" for ch in str(name)).strip()
    return cleaned.strip(". ")[:120] or "unnamed"

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
