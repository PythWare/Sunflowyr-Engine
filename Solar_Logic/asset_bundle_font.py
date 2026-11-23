from __future__ import annotations
import struct
from pathlib import Path
from .asset_bundle_writer import pack_aligned_string, pack_pointer

FONT_SIZE = 16.0

class NewFontAssetError(ValueError):
    pass

def build_font_asset(font_path: Path, name: str) -> tuple[bytes, dict[str, object]]:
    font_data = Path(font_path).read_bytes()
    tables = read_sfnt_tables(font_data)
    units_per_em = read_units_per_em(font_data, tables)
    ascent_units, descent_units, line_gap_units = read_horizontal_metrics(font_data, tables)
    family_name = read_family_name(font_data, tables) or name
    scale = FONT_SIZE / units_per_em
    ascent = ascent_units * scale
    descent = descent_units * scale
    line_spacing = (ascent_units - descent_units + line_gap_units) * scale
    payload = build_font_object(
        name,
        family_name,
        font_data,
        ascent,
        descent,
        line_spacing,
    )
    return payload, {
        "sourceFormat": font_path.suffix.lower().lstrip("."),
        "byteLength": len(font_data),
        "familyName": family_name,
        "fontSize": FONT_SIZE,
        "ascent": ascent,
        "descent": descent,
        "lineSpacing": line_spacing,
        "dynamicFontData": True,
    }

def build_font_object(
    name: str,
    family_name: str,
    font_data: bytes,
    ascent: float,
    descent: float,
    line_spacing: float,
) -> bytes:
    output = bytearray(pack_aligned_string(name))
    output.extend(struct.pack("<iffiii", 0, 1.0, line_spacing, 0, 1, -2))
    output.extend(pack_pointer(0))
    output.extend(struct.pack("<i", 0))
    output.extend(pack_pointer(0))
    output.extend(struct.pack("<i", 0))
    output.extend(struct.pack("<f", 0.1))
    output.extend(struct.pack("<i", len(font_data)))
    output.extend(font_data)
    output.extend(struct.pack("<fffi", FONT_SIZE, ascent, descent, 0))
    output.extend(struct.pack("<i", 1))
    output.extend(pack_aligned_string(family_name))
    output.extend(struct.pack("<ii", 0, 0))
    return bytes(output)

def read_sfnt_tables(font_data: bytes) -> dict[bytes, tuple[int, int]]:
    if len(font_data) < 12:
        raise NewFontAssetError("font file is too small for an OpenType header")
    signature = font_data[:4]
    if signature not in {b"\x00\x01\x00\x00", b"OTTO", b"true"}:
        raise NewFontAssetError("only standalone TTF and OTF fonts are supported")
    table_count = struct.unpack_from(">H", font_data, 4)[0]
    directory_end = 12 + table_count * 16
    if table_count <= 0 or directory_end > len(font_data):
        raise NewFontAssetError("font table directory is truncated")
    tables: dict[bytes, tuple[int, int]] = {}
    for index in range(table_count):
        entry = 12 + index * 16
        tag = font_data[entry : entry + 4]
        offset, length = struct.unpack_from(">II", font_data, entry + 8)
        if offset > len(font_data) or length > len(font_data) - offset:
            raise NewFontAssetError(f"font table {tag.decode('latin-1')} is truncated")
        tables[tag] = (offset, length)
    return tables

def read_units_per_em(font_data: bytes, tables: dict[bytes, tuple[int, int]]) -> int:
    offset, length = required_table(tables, b"head")
    if length < 20:
        raise NewFontAssetError("font head table is truncated")
    units_per_em = struct.unpack_from(">H", font_data, offset + 18)[0]
    if units_per_em <= 0:
        raise NewFontAssetError("font unitsPerEm must be positive")
    return units_per_em

def read_horizontal_metrics(
    font_data: bytes,
    tables: dict[bytes, tuple[int, int]],
) -> tuple[int, int, int]:
    offset, length = required_table(tables, b"hhea")
    if length < 10:
        raise NewFontAssetError("font hhea table is truncated")
    return struct.unpack_from(">hhh", font_data, offset + 4)

def read_family_name(font_data: bytes, tables: dict[bytes, tuple[int, int]]) -> str | None:
    table = tables.get(b"name")
    if table is None:
        return None
    offset, length = table
    if length < 6:
        return None
    count, string_offset = struct.unpack_from(">HH", font_data, offset + 2)
    records_end = 6 + count * 12
    if records_end > length or string_offset > length:
        return None
    candidates: list[tuple[int, str]] = []
    for index in range(count):
        record = offset + 6 + index * 12
        platform, encoding, language, name_id, text_length, text_offset = struct.unpack_from(
            ">HHHHHH", font_data, record
        )
        if name_id not in {1, 16}:
            continue
        start = offset + string_offset + text_offset
        end = start + text_length
        if start < offset or end > offset + length:
            continue
        raw = font_data[start:end]
        text = decode_font_name(raw, platform, encoding)
        if not text:
            continue
        score = (20 if name_id == 16 else 10) + (4 if language == 0x0409 else 0)
        score += 2 if platform in {0, 3} else 0
        candidates.append((score, text))
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]

def decode_font_name(raw: bytes, platform: int, encoding: int) -> str:
    try:
        if platform in {0, 3}:
            return raw.decode("utf-16-be").strip("\0 ")
        if platform == 1:
            return raw.decode("mac_roman").strip("\0 ")
        return raw.decode("latin-1").strip("\0 ")
    except (UnicodeDecodeError, LookupError):
        return ""

def required_table(
    tables: dict[bytes, tuple[int, int]],
    tag: bytes,
) -> tuple[int, int]:
    table = tables.get(tag)
    if table is None:
        raise NewFontAssetError(f"font is missing its {tag.decode('ascii')} table")
    return table
