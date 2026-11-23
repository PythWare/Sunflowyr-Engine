from __future__ import annotations
import io, re, struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from .binary import EndianReader
from .material_export import read_editor_extension
from .serialized import SerializedAsset, SerializedObject

class ShaderExportError(ValueError):
    pass

DXBC_PROGRAM_TYPES = {
    9,
    10,
    11,
    12,
    13,
    14,
    15,
    16,
    17,
    18,
    19,
    20,
    21,
    22,
}

TEXT_PROGRAM_TYPES = {1, 2, 3, 4, 5, 6, 7, 8, 26, 27, 28, 29, 30}

@dataclass(slots=True)
class ShaderObject:
    name: str
    script: bytes
    path_name: str
    decompressed_size: int
    subprogram_blob: bytes
    trailing: bytes

@dataclass(slots=True)
class ShaderSubProgramEntry:
    offset: int
    length: int
    segment: int

@dataclass(slots=True)
class ShaderSubProgram:
    version: int
    program_type: int
    keywords: list[str]
    local_keywords: list[str]
    program_code: bytes
    stats: tuple[int, int, int] = (0, 0, 0)
    extra_stat: bytes = b""
    parameter_table: dict | None = None
    source_offset: int | None = None

def read_shader_object(asset: SerializedAsset, obj: SerializedObject) -> ShaderObject:
    with asset.path.open("rb") as handle:
        reader = EndianReader(handle, asset.endian)
        reader.seek(obj.byte_start)
        read_editor_extension(reader, asset)
        name = reader.aligned_string(max_length=4096)
        script = read_byte_array(reader, obj, "m_Script")
        path_name = reader.aligned_string(max_length=4096)
        decompressed_size = reader.u32()
        subprogram_blob = read_byte_array(reader, obj, "m_SubProgramBlob")
        object_end = obj.byte_start + obj.byte_size
        if reader.tell() > object_end:
            raise ShaderExportError("Shader reader ran past object end")
        trailing = reader.read(object_end - reader.tell()) if reader.tell() < object_end else b""
    return ShaderObject(name, script, path_name, decompressed_size, subprogram_blob, trailing)

def convert_shader_object(asset: SerializedAsset, shader: ShaderObject) -> tuple[str, dict[str, Any]]:
    script_text = shader.script.decode("utf-8", "replace").replace("\r\n", "\n").replace("\r", "\n")
    conversion: dict[str, Any] = {
        "format": "shader_text_unity5_3_5_4",
        "sourceLayout": "m_Script_m_PathName_decompressedSize_m_SubProgramBlob",
        "programReplacementCount": 0,
        "programParseErrorCount": 0,
        "lz4Decoded": False,
    }
    if not shader.subprogram_blob:
        return script_text, conversion

    try:
        decompressed = lz4_decompress_block(shader.subprogram_blob, shader.decompressed_size)
        program = read_shader_program(decompressed, unity_version_tuple(asset.unity_version))
        script_text, replacements = replace_gpu_program_indices(script_text, program)
        conversion["lz4Decoded"] = True
        conversion["programCapacity"] = len(program)
        conversion["programReplacementCount"] = replacements
    except Exception as exc:
        conversion["programParseErrorCount"] = 1
        conversion["programParseError"] = str(exc)
    return script_text, conversion

def read_shader_program(data: bytes, version: tuple[int, int, int]) -> list[ShaderSubProgram | None]:
    reader = EndianReader(io.BytesIO(data), "<")
    count = read_count(reader, "shader subprogram entry count", 1_000_000)
    entries: list[ShaderSubProgramEntry] = []
    has_segment = version >= (2019, 3, 0)
    for item_index in range(count):
        offset = reader.i32()
        length = reader.i32()
        segment = reader.i32() if has_segment else 0
        if offset < 0 or length < 0 or offset + length > len(data):
            raise ShaderExportError(f"invalid shader subprogram entry {item_index}")
        entries.append(ShaderSubProgramEntry(offset, length, segment))

    subprograms: list[ShaderSubProgram | None] = []
    for entry in entries:
        if entry.segment != 0:
            subprograms.append(None)
            continue
        reader.seek(entry.offset)
        subprogram = read_shader_subprogram(reader, entry.offset + entry.length)
        subprogram.source_offset = entry.offset
        subprograms.append(subprogram)
    return subprograms

def read_shader_subprogram(reader: EndianReader, end: int | None = None) -> ShaderSubProgram:
    shader_version = reader.i32()
    program_type = reader.i32()
    stats = (reader.i32(), reader.i32(), reader.i32())
    extra_stat = b""
    if shader_version >= 201608170:
        extra_stat = reader.read(4)
    keywords = read_string_array(reader, "shader keywords", 100_000)
    local_keywords: list[str] = []
    if 201806140 <= shader_version < 202012090:
        local_keywords = read_string_array(reader, "local shader keywords", 100_000)
    program_code = read_raw_byte_array(reader, "shader program code", 256 * 1024 * 1024)
    reader.align()
    parameter_table = None
    if end is not None and reader.tell() < end:
        from .shader_params import parse_parameter_table

        parameter_table = parse_parameter_table(reader.read(end - reader.tell()), program_type)
    return ShaderSubProgram(
        shader_version,
        program_type,
        keywords,
        local_keywords,
        program_code,
        stats,
        extra_stat,
        parameter_table,
    )

def pack_shader_subprogram(subprogram: ShaderSubProgram) -> bytes:
    from .shader_params import pack_parameter_table

    out = bytearray()
    out += struct.pack("<ii", subprogram.version, subprogram.program_type)
    out += struct.pack("<3i", *subprogram.stats)
    out += subprogram.extra_stat
    out += struct.pack("<i", len(subprogram.keywords))
    for keyword in subprogram.keywords:
        encoded = keyword.encode("utf-8")
        out += struct.pack("<i", len(encoded)) + encoded
        while len(out) % 4:
            out += b"\0"
    if 201806140 <= subprogram.version < 202012090:
        out += struct.pack("<i", len(subprogram.local_keywords))
        for keyword in subprogram.local_keywords:
            encoded = keyword.encode("utf-8")
            out += struct.pack("<i", len(encoded)) + encoded
            while len(out) % 4:
                out += b"\0"
    out += struct.pack("<i", len(subprogram.program_code))
    out += subprogram.program_code
    while len(out) % 4:
        out += b"\0"
    if subprogram.parameter_table is not None:
        out += pack_parameter_table(subprogram.parameter_table, subprogram.program_type)
    return bytes(out)

def pack_shader_program(subprograms: list[ShaderSubProgram | None]) -> bytes:
    bodies = [
        b"" if subprogram is None else pack_shader_subprogram(subprogram)
        for subprogram in subprograms
    ]
    header_size = 4 + len(bodies) * 8
    stored = [
        None if subprogram is None else subprogram.source_offset for subprogram in subprograms
    ]
    offsets = None
    if bodies and all(offset is not None for offset in stored):
        candidate = [int(offset) for offset in stored]
        if source_offsets_fit(candidate, bodies, header_size):
            offsets = candidate
    if offsets is None:
        offsets = []
        cursor = header_size
        for body in bodies:
            offsets.append(cursor)
            cursor += len(body)
            cursor = (cursor + 3) & ~3

    out = bytearray()
    out += struct.pack("<i", len(bodies))
    for offset, body in zip(offsets, bodies):
        out += struct.pack("<ii", offset, len(body))
    for offset, body in sorted(zip(offsets, bodies), key=lambda item: item[0]):
        while len(out) < offset:
            out += b"\0"
        out[offset : offset + len(body)] = body
    while len(out) % 4:
        out += b"\0"
    return bytes(out)

def source_offsets_fit(offsets: list[int], bodies: list[bytes], header_size: int) -> bool:
    placed = sorted(zip(offsets, (len(body) for body in bodies)))
    for index, (offset, length) in enumerate(placed):
        if offset < header_size:
            return False
        if index + 1 < len(placed) and offset + length > placed[index + 1][0]:
            return False
    return True

def replace_gpu_program_indices(script_text: str, subprograms: list[ShaderSubProgram | None]) -> tuple[str, int]:
    replacements = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal replacements
        index = int(match.group(1))
        if not 0 <= index < len(subprograms) or subprograms[index] is None:
            return f"\"// missing shader subprogram {index}\""
        replacements += 1
        return export_shader_subprogram(subprograms[index])

    return re.sub(r"GpuProgramIndex\s+(\d+)", replace, script_text), replacements

def export_shader_subprogram(subprogram: ShaderSubProgram) -> str:
    lines: list[str] = []
    if subprogram.keywords:
        lines.append("Keywords { " + "".join(f"\"{keyword}\" " for keyword in subprogram.keywords) + "}")
    if subprogram.local_keywords:
        lines.append("Local Keywords { " + "".join(f"\"{keyword}\" " for keyword in subprogram.local_keywords) + "}")
    lines.append("\"" + export_program_code(subprogram) + "\"")
    return "\n".join(lines)

def export_program_code(subprogram: ShaderSubProgram) -> str:
    if not subprogram.program_code:
        return ""
    if subprogram.program_type in TEXT_PROGRAM_TYPES:
        return subprogram.program_code.decode("utf-8", "replace")
    if subprogram.program_type in DXBC_PROGRAM_TYPES:
        return "// shader disassembly not supported on DXBC"
    if subprogram.program_type in {23, 24}:
        return export_metal_program(subprogram.program_code)
    if subprogram.program_type == 25:
        return "// shader disassembly not supported on SPIR-V"
    return f"//shader disassembly not supported on {subprogram.program_type}"

def export_metal_program(program_code: bytes) -> str:
    reader = EndianReader(io.BytesIO(program_code), "<")
    if len(program_code) >= 8 and reader.u32() == 0xF00DCAFE:
        offset = reader.i32()
        if 0 <= offset <= len(program_code):
            reader.seek(offset)
    else:
        reader.seek(0)
    while reader.tell() < len(program_code):
        if reader.read(1) == b"\x00":
            break
    return program_code[reader.tell() :].decode("utf-8", "replace")

LZ4_MIN_MATCH = 4
LZ4_LAST_LITERALS = 5
LZ4_MATCH_LIMIT = 12
LZ4_MAX_OFFSET = 65535
LZ4_HASH_BITS = 16

def write_lz4_length(out: bytearray, value: int) -> None:
    while value >= 255:
        out.append(255)
        value -= 255
    out.append(value)

def emit_lz4_literals(out: bytearray, data: bytes, start: int, end: int) -> None:
    length = end - start
    if length < 15:
        out.append(length << 4)
    else:
        out.append(0xF0)
        write_lz4_length(out, length - 15)
    out += data[start:end]

def lz4_compress_block(data: bytes) -> bytes:
    length = len(data)
    out = bytearray()
    if length < LZ4_MATCH_LIMIT + LZ4_MIN_MATCH:
        emit_lz4_literals(out, data, 0, length)
        return bytes(out)

    table = [-1] * (1 << LZ4_HASH_BITS)
    anchor = 0
    position = 0
    limit = length - LZ4_MATCH_LIMIT

    while position < limit:
        chunk = data[position : position + 4]
        key = (int.from_bytes(chunk, "little") * 2654435761) >> (32 - LZ4_HASH_BITS)
        key &= (1 << LZ4_HASH_BITS) - 1
        candidate = table[key]
        table[key] = position
        if (
            candidate < 0
            or position - candidate > LZ4_MAX_OFFSET
            or data[candidate : candidate + 4] != chunk
        ):
            position += 1
            continue

        match_length = 4
        maximum = length - LZ4_LAST_LITERALS - position
        while match_length < maximum and data[candidate + match_length] == data[position + match_length]:
            match_length += 1

        literal_length = position - anchor
        token_index = len(out)
        token = 0xF0 if literal_length >= 15 else (literal_length << 4)
        out.append(token)
        if literal_length >= 15:
            write_lz4_length(out, literal_length - 15)
        out += data[anchor:position]
        out += (position - candidate).to_bytes(2, "little")
        encoded = match_length - LZ4_MIN_MATCH
        if encoded >= 15:
            out[token_index] |= 0x0F
            write_lz4_length(out, encoded - 15)
        else:
            out[token_index] |= encoded

        position += match_length
        anchor = position

    emit_lz4_literals(out, data, anchor, length)
    return bytes(out)

def lz4_decompress_block(data: bytes, expected_size: int) -> bytes:
    if expected_size < 0:
        raise ShaderExportError("negative LZ4 output size")
    out = bytearray()
    index = 0
    data_size = len(data)
    while index < data_size:
        token = data[index]
        index += 1
        literal_length = token >> 4
        if literal_length == 15:
            extra, index = read_lz4_length(data, index)
            literal_length += extra
        if index + literal_length > data_size:
            raise ShaderExportError("truncated LZ4 literal run")
        out.extend(data[index : index + literal_length])
        index += literal_length
        if index >= data_size:
            break
        if index + 2 > data_size:
            raise ShaderExportError("truncated LZ4 match offset")
        offset = data[index] | (data[index + 1] << 8)
        index += 2
        if offset <= 0 or offset > len(out):
            raise ShaderExportError("invalid LZ4 match offset")
        match_length = (token & 0x0F) + 4
        if (token & 0x0F) == 15:
            extra, index = read_lz4_length(data, index)
            match_length += extra
        for _ in range(match_length):
            out.append(out[-offset])
        if expected_size and len(out) > expected_size:
            raise ShaderExportError("LZ4 output exceeded expected size")
    if expected_size and len(out) != expected_size:
        raise ShaderExportError(f"LZ4 output size {len(out)} doesnt match expected {expected_size}")
    return bytes(out)

def read_lz4_length(data: bytes, index: int) -> tuple[int, int]:
    total = 0
    while True:
        if index >= len(data):
            raise ShaderExportError("truncated LZ4 length")
        value = data[index]
        index += 1
        total += value
        if value != 255:
            return total, index

def read_byte_array(reader: EndianReader, obj: SerializedObject, label: str) -> bytes:
    remaining = obj.byte_start + obj.byte_size - reader.tell()
    return read_raw_byte_array(reader, label, max(0, remaining))

def read_raw_byte_array(reader: EndianReader, label: str, maximum: int) -> bytes:
    count = reader.i32()
    if count < 0 or count > maximum:
        raise ShaderExportError(f"invalid {label} byte count {count}")
    data = reader.read(count)
    reader.align()
    return data

def read_string_array(reader: EndianReader, label: str, maximum: int) -> list[str]:
    count = read_count(reader, label, maximum)
    return [reader.aligned_string(max_length=4096) for _ in range(count)]

def read_count(reader: EndianReader, label: str, maximum: int) -> int:
    count = reader.i32()
    if count < 0 or count > maximum:
        raise ShaderExportError(f"invalid {label} count {count}")
    return count

def unity_version_tuple(version: str) -> tuple[int, int, int]:
    match = re.match(r"(\d+)\.(\d+)\.(\d+)", version or "")
    if not match:
        return (0, 0, 0)
    return tuple(int(part) for part in match.groups())
