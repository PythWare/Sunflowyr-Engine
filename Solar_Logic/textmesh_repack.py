from __future__ import annotations
import base64, json
from pathlib import Path
from typing import Any
from .mono_repack import EndianWriter, taildata_value, write_pptr
from .serialized import load_serialized_asset

class TextMeshRepackError(ValueError):
    pass

def repack_textmesh_json(json_path: Path, taildata: dict[str, Any], data_root: Path) -> bytes:
    document = json.loads(Path(json_path).read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise TextMeshRepackError("TextMesh JSON root must be an object")

    container = str(taildata_value(taildata, "container") or "")
    if not container:
        raise TextMeshRepackError("taildata record doesnt name a Unity container")
    asset_path = (Path(data_root) / container).resolve()
    asset = load_serialized_asset(asset_path, Path(data_root))
    writer = EndianWriter(asset.endian)
    write_text_mesh(writer, asset, document)

    trailing = document.get("solarUnparsedTail")
    if isinstance(trailing, dict) and trailing.get("encoding") == "base64":
        data_text = trailing.get("data")
        if not isinstance(data_text, str):
            raise TextMeshRepackError("solarUnparsedTail.data must be base64 text")
        writer.write(base64.b64decode(data_text))
    return writer.bytes()

def build_text_mesh(fields: dict[str, Any], asset: Any) -> bytes:
    writer = EndianWriter(asset.endian)
    write_text_mesh(writer, asset, fields)
    return writer.bytes()

def write_text_mesh(writer: EndianWriter, asset: Any, document: dict[str, Any]) -> None:
    write_pptr(writer, asset, object_dict(document.get("m_GameObject")))
    writer.aligned_string(document.get("m_Text", ""))
    writer.f32(document.get("m_OffsetZ", 0.0))
    writer.f32(document.get("m_CharacterSize", 1.0))
    writer.f32(document.get("m_LineSpacing", 1.0))
    writer.u16(document.get("m_Anchor", 0))
    writer.u16(document.get("m_Alignment", 0))
    writer.f32(document.get("m_TabSize", 4.0))
    writer.i32(document.get("m_FontSize", 0))
    writer.i32(document.get("m_FontStyle", 0))
    writer.bool(document.get("m_RichText", False))
    writer.align()
    write_pptr(writer, asset, object_dict(document.get("m_Font")))
    color = object_dict(document.get("m_Color"))
    writer.u8(color_byte(color.get("r", 255)))
    writer.u8(color_byte(color.get("g", 255)))
    writer.u8(color_byte(color.get("b", 255)))
    writer.u8(color_byte(color.get("a", 255)))

def object_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}

def color_byte(value: Any) -> int:
    return max(0, min(255, int(value)))
