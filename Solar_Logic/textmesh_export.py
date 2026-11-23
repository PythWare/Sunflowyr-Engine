from __future__ import annotations
import base64, json
from pathlib import Path
from typing import Any
from .binary import EndianReader
from .material_export import read_pptr
from .serialized import SerializedAsset, SerializedObject
from .paths import ensure_parent

class TextMeshExportError(ValueError):
    pass

def export_textmesh_json(
    asset: SerializedAsset,
    obj: SerializedObject,
    out_path: Path,
    metadata: dict[str, Any],
    game_object_name: str = "",
) -> dict[str, Any]:
    with asset.path.open("rb") as handle:
        reader = EndianReader(handle, asset.endian)
        document, conversion = read_textmesh_json(asset, reader, obj, game_object_name)

    ensure_parent(out_path)
    with out_path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(document, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    conversion["text"] = document.get("m_Text", "")
    conversion["gameObjectName"] = game_object_name
    metadata["conversion"] = conversion
    metadata["writeback_note"] = "TextMesh JSON is editable and can be rebuilt into Unity TextMesh object data."
    return conversion

def read_textmesh_preview_name(asset: SerializedAsset, obj: SerializedObject) -> str:
    try:
        with asset.path.open("rb") as handle:
            reader = EndianReader(handle, asset.endian)
            reader.seek(obj.byte_start)
            read_pptr(reader, asset)
            return reader.aligned_string(max_length=4096)
    except Exception:
        return ""

def read_textmesh_json(
    asset: SerializedAsset,
    reader: EndianReader,
    obj: SerializedObject,
    game_object_name: str = "",
) -> tuple[dict[str, Any], dict[str, Any]]:
    reader.seek(obj.byte_start)
    game_object = read_pptr(reader, asset)
    text = reader.aligned_string(max_length=1_000_000)
    offset_z = reader.f32()
    character_size = reader.f32()
    line_spacing = reader.f32()
    anchor = reader.u16()
    alignment = reader.u16()
    tab_size = reader.f32()
    font_size = reader.i32()
    font_style = reader.i32()
    rich_text = reader.bool()
    reader.align()
    font = read_pptr(reader, asset)
    color = {
        "r": reader.u8(),
        "g": reader.u8(),
        "b": reader.u8(),
        "a": reader.u8(),
    }
    object_end = obj.byte_start + obj.byte_size
    if reader.tell() > object_end:
        raise TextMeshExportError("TextMesh reader ran past object end")
    trailing = reader.read(object_end - reader.tell()) if reader.tell() < object_end else b""
    document: dict[str, Any] = {
        "m_GameObject": game_object,
        "solarGameObjectName": game_object_name,
        "m_Text": text,
        "m_OffsetZ": offset_z,
        "m_CharacterSize": character_size,
        "m_LineSpacing": line_spacing,
        "m_Anchor": anchor,
        "m_Alignment": alignment,
        "m_TabSize": tab_size,
        "m_FontSize": font_size,
        "m_FontStyle": font_style,
        "m_RichText": rich_text,
        "m_Font": font,
        "m_Color": color,
    }
    if trailing:
        document["solarUnparsedTail"] = {
            "encoding": "base64",
            "size": len(trailing),
            "data": base64.b64encode(trailing).decode("ascii"),
        }
    return document, {
        "format": "textmesh_json_unity5",
        "colorFormat": "rgba8",
        "textLength": len(text),
        "unparsedTailSize": len(trailing),
    }
