from __future__ import annotations
import base64, json, struct
from pathlib import Path
from typing import Any
from .mono_repack import EndianWriter, taildata_value
from .serialized import SerializedAsset, load_serialized_asset

class FontRepackError(ValueError):
    pass

def repack_font_json(json_path: Path, taildata: dict[str, Any], data_root: Path) -> bytes:
    document = json.loads(Path(json_path).read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise FontRepackError("Font JSON root must be an object")
    asset = font_asset(taildata, data_root)
    writer = EndianWriter(asset.endian)
    write_object_header(writer, asset, document)
    writer.aligned_string(document.get("m_Name", ""))
    writer.i32(int(document.get("m_AsciiStartOffset", 0)))
    writer.f32(document.get("m_Tracking", document.get("m_Kerning", 0.0)))
    writer.f32(document.get("m_LineSpacing", 0.0))
    writer.i32(int(document.get("m_CharacterSpacing", 0)))
    writer.i32(int(document.get("m_CharacterPadding", 0)))
    writer.i32(int(document.get("m_ConvertCase", 0)))
    write_pptr(writer, asset, dict_value(document.get("m_DefaultMaterial")))
    write_character_rects(writer, document.get("m_CharacterRects", []))
    write_pptr(writer, asset, dict_value(document.get("m_Texture")))
    write_kerning_values(writer, document.get("m_KerningValues", []))
    writer.f32(document.get("m_PixelScale", 1.0))
    font_bytes = font_data_from_document(Path(json_path), document)
    writer.i32(len(font_bytes))
    writer.write(font_bytes)
    if "m_FontSize" not in document:
        writer.write(decode_optional_tail(document))
        return writer.bytes()
    writer.align()
    writer.f32(document.get("m_FontSize", 16.0))
    writer.f32(document.get("m_Ascent", 0.0))
    writer.f32(document.get("m_Descent", 0.0))
    writer.u32(int(document.get("m_DefaultStyle", 0)))
    names = document.get("m_FontNames") or []
    writer.i32(len(names))
    for name in names:
        writer.aligned_string(str(name))
    fallbacks = document.get("m_FallbackFonts") or []
    writer.i32(len(fallbacks))
    for fallback in fallbacks:
        write_pptr(writer, asset, dict_value(fallback))
    writer.align()
    writer.i32(int(document.get("m_FontRenderingMode", 0)))
    writer.write(decode_optional_tail(document))
    return writer.bytes()

def repack_font_data_file(font_path: Path, taildata: dict[str, Any], data_root: Path) -> bytes:
    asset = font_asset(taildata, data_root)
    container = Path(data_root) / str(taildata_value(taildata, "container") or "")
    byte_start = int(taildata_value(taildata, "byteStartAbsolute", 0))
    byte_size = int(taildata_value(taildata, "byteSize", 0))
    size_offset = int(taildata_value(taildata, "fontDataSizeFieldOffset", 0))
    data_offset = int(taildata_value(taildata, "fontDataOffsetAbsolute", 0))
    old_size = int(taildata_value(taildata, "fontDataSize", 0))
    if not byte_start or not byte_size or not size_offset or not data_offset:
        raise FontRepackError("font_data taildata is missing font data offsets")
    with container.open("rb") as handle:
        handle.seek(byte_start)
        original = handle.read(byte_size)
    if len(original) != byte_size:
        raise FontRepackError("original Font object bytes are truncated")
    size_relative = size_offset - byte_start
    data_relative = data_offset - byte_start
    if size_relative < 0 or data_relative < size_relative + 4 or data_relative + old_size > len(original):
        raise FontRepackError("font_data taildata offsets are outside the original object")
    font_bytes = Path(font_path).read_bytes()
    endian = "<" if asset.endian == "<" else ">"
    return (
        original[:size_relative]
        + struct.pack(endian + "i", len(font_bytes))
        + font_bytes
        + original[data_relative + old_size :]
    )

def write_object_header(writer: EndianWriter, asset: SerializedAsset, document: dict[str, Any]) -> None:
    if asset.platform != -2:
        return
    writer.u32(int(document.get("m_ObjectHideFlags", 0) or 0))
    write_pptr(writer, asset, dict_value(document.get("m_PrefabParentObject")))
    write_pptr(writer, asset, dict_value(document.get("m_PrefabInternal")))

def write_pptr(writer: EndianWriter, asset: SerializedAsset, value: dict[str, Any]) -> None:
    writer.i32(int(value.get("m_FileID", value.get("file_id", 0))))
    path_id = int(value.get("m_PathID", value.get("path_id", 0)))
    if asset.format_version >= 14:
        writer.i64(path_id)
    else:
        writer.i32(path_id)

def write_character_rects(writer: EndianWriter, value: Any) -> None:
    rects = value if isinstance(value, list) else []
    writer.i32(len(rects))
    for item in rects:
        item = dict_value(item)
        writer.i32(int(item.get("index", 0)))
        write_rect(writer, dict_value(item.get("uv")))
        write_rect(writer, dict_value(item.get("vert")))
        writer.f32(item.get("advance", item.get("width", 0.0)))
        writer.bool(item.get("flipped", False))
        writer.align()

def write_rect(writer: EndianWriter, value: dict[str, Any]) -> None:
    writer.f32(value.get("x", 0.0))
    writer.f32(value.get("y", 0.0))
    writer.f32(value.get("width", 0.0))
    writer.f32(value.get("height", 0.0))

def write_kerning_values(writer: EndianWriter, value: Any) -> None:
    kerns = value if isinstance(value, list) else []
    writer.i32(len(kerns))
    for item in kerns:
        item = dict_value(item)
        writer.i16(int(item.get("first", 0)))
        writer.i16(int(item.get("second", 0)))
        writer.f32(item.get("amount", 0.0))

def font_data_from_document(json_path: Path, document: dict[str, Any]) -> bytes:
    info = document.get("m_FontData")
    if isinstance(info, dict):
        external = info.get("externalFile")
        if external:
            path = Path(str(external))
            if not path.is_absolute():
                path = json_path.parent / path
            return path.read_bytes()
        if info.get("encoding") == "base64" and isinstance(info.get("data"), str):
            return base64.b64decode(str(info["data"]))
    return b""

def decode_optional_tail(document: dict[str, Any]) -> bytes:
    tail = document.get("solarUnparsedTail")
    if not isinstance(tail, dict):
        return b""
    if tail.get("encoding") != "base64":
        raise FontRepackError("solarUnparsedTail encoding must be base64")
    data_text = tail.get("data")
    if not isinstance(data_text, str):
        raise FontRepackError("solarUnparsedTail.data must be base64 text")
    data = base64.b64decode(data_text)
    expected_size = tail.get("size")
    if expected_size is not None and len(data) != int(expected_size):
        raise FontRepackError("decoded font tail bytes dont match solarUnparsedTail.size")
    return data

def font_asset(taildata: dict[str, Any], data_root: Path) -> SerializedAsset:
    container = str(taildata_value(taildata, "container") or "")
    if not container:
        raise FontRepackError("taildata record doesnt name a Unity container")
    return load_serialized_asset((Path(data_root) / container).resolve(), Path(data_root))

def dict_value(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}
