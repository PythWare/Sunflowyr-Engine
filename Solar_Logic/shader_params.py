from __future__ import annotations
import struct

D3D9_PROGRAM_TYPES = frozenset({9, 10, 11, 12})
VECTOR_FIELDS = 6
TEXTURE_FIELDS = 3
BUFFER_FIELDS = 2
BINDING_FIELDS = 3
CONSTANT_BUFFER_PARAM_FIELDS = 6

class ShaderParameterError(ValueError):
    pass

class TableReader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    def i32(self) -> int:
        if self.pos + 4 > len(self.data):
            raise ShaderParameterError("parameter table ended early")
        value = struct.unpack_from("<i", self.data, self.pos)[0]
        self.pos += 4
        return value

    def name(self) -> str:
        length = self.i32()
        if length < 0 or self.pos + length > len(self.data):
            raise ShaderParameterError(f"parameter table has a bad name length {length}")
        value = self.data[self.pos : self.pos + length].decode("utf-8", "replace")
        self.pos += length
        self.pos = (self.pos + 3) & ~3
        return value

class TableWriter:
    def __init__(self) -> None:
        self.parts = bytearray()

    def i32(self, value: int) -> None:
        self.parts += struct.pack("<i", int(value))

    def name(self, value: str) -> None:
        encoded = str(value).encode("utf-8")
        self.parts += struct.pack("<i", len(encoded))
        self.parts += encoded
        while len(self.parts) % 4:
            self.parts += b"\0"

    def bytes(self) -> bytes:
        return bytes(self.parts)

def read_section(reader: TableReader, fields: int) -> list[dict]:
    count = reader.i32()
    if not 0 <= count <= 65536:
        raise ShaderParameterError(f"parameter section count {count} is out of range")
    return [
        {"name": reader.name(), "values": [reader.i32() for _ in range(fields)]}
        for _ in range(count)
    ]

def write_section(writer: TableWriter, records: list[dict], fields: int) -> None:
    writer.i32(len(records))
    for record in records:
        writer.name(record["name"])
        values = record["values"]
        if len(values) != fields:
            raise ShaderParameterError(
                f"parameter {record['name']!r} carries {len(values)} values, expected {fields}"
            )
        for value in values:
            writer.i32(value)

def parse_parameter_table(data: bytes, program_type: int) -> dict:
    reader = TableReader(data)
    mask = reader.i32()
    pair_count = reader.i32()
    if not 0 <= pair_count <= 4096:
        raise ShaderParameterError(f"parameter table pair count {pair_count} is out of range")
    pairs = [[reader.i32(), reader.i32()] for _ in range(pair_count)]
    table: dict = {"mask": mask, "pairs": pairs}

    if program_type in D3D9_PROGRAM_TYPES:
        table["unknownA"] = reader.i32()
        table["unknownB"] = reader.i32()
        table["unknownC"] = reader.i32()
        table["vectors"] = read_section(reader, VECTOR_FIELDS)
        table["textures"] = read_section(reader, TEXTURE_FIELDS)
    else:
        slots = reader.i32()
        table["constantBufferSlots"] = slots
        table["vectors"] = read_section(reader, VECTOR_FIELDS)
        table["textures"] = read_section(reader, TEXTURE_FIELDS)
        table["buffers"] = read_section(reader, BUFFER_FIELDS)
        constant_buffers = []
        for _ in range(max(0, slots - 1)):
            name = reader.name()
            byte_size = reader.i32()
            param_count = reader.i32()
            if not 0 <= param_count <= 65536:
                raise ShaderParameterError(
                    f"constant buffer {name!r} declares {param_count} parameters"
                )
            params = [
                {
                    "name": reader.name(),
                    "values": [reader.i32() for _ in range(CONSTANT_BUFFER_PARAM_FIELDS)],
                }
                for _ in range(param_count)
            ]
            constant_buffers.append({"name": name, "byteSize": byte_size, "params": params})
        table["constantBuffers"] = constant_buffers
        table["bindings"] = read_section(reader, BINDING_FIELDS)

    if reader.pos != len(data):
        raise ShaderParameterError(
            f"parameter table consumed {reader.pos} of {len(data)} bytes"
        )
    return table

MATRIX_CLASSES = frozenset({2, 3})
UNITY_PARAM_TYPES = {1: 2, 2: 1, 3: 0, 19: 1}

def shape_for_size(byte_size: int) -> tuple[int, int]:
    if byte_size <= 0:
        return 1, 1
    if byte_size > 16 and byte_size % 16 == 0:
        return byte_size // 16, 4
    return 1, max(1, min(4, (byte_size + 3) // 4))

def parameter_values(variable: dict) -> list[int]:
    offset = int(variable.get("offset", 0))
    if "columns" in variable:
        rows = max(1, int(variable.get("rows", 1)))
        columns = max(1, int(variable.get("columns", 1)))
        is_matrix = 1 if int(variable.get("class", 0)) in MATRIX_CLASSES else 0
        elements = int(variable.get("elements", 0))
        param_type = UNITY_PARAM_TYPES.get(int(variable.get("type", 3)), 0)
        return [param_type, rows, columns, is_matrix, elements, offset]
    rows, columns = shape_for_size(int(variable.get("size", 0)))
    return [0, rows, columns, 1 if rows > 1 else 0, 0, offset]

def reflected_constant_buffers(reflection: dict) -> list[dict]:
    constant_buffers = []
    for buffer in reflection.get("constantBuffers") or []:
        params = [
            {"name": variable["name"], "values": parameter_values(variable)}
            for variable in buffer.get("variables") or []
        ]
        constant_buffers.append(
            {"name": buffer["name"], "byteSize": int(buffer.get("size", 0)), "params": params}
        )
    return constant_buffers

def reflected_resource_counts(reflection: dict) -> dict[str, int]:
    counts = {"cbuffers": 0, "textures": 0, "samplers": 0, "other": 0}
    for binding in reflection.get("bindings") or []:
        kind = int(binding.get("type", 0))
        if kind == 0:
            counts["cbuffers"] += 1
        elif kind == 2:
            counts["textures"] += 1
        elif kind == 3:
            counts["samplers"] += 1
        else:
            counts["other"] += 1
    return counts

def build_parameter_table_from_reflection(
    reflection: dict,
    program_type: int,
    previous: dict | None = None,
) -> dict:
    constant_buffers = reflected_constant_buffers(reflection)
    if previous is not None:
        table = json_copy(previous)
        if program_type not in D3D9_PROGRAM_TYPES:
            table["constantBuffers"] = constant_buffers
        return table

    bindings = []
    slot = 0
    for binding in reflection.get("bindings") or []:
        kind = int(binding.get("type", 0))
        if kind == 2:
            index = int(binding.get("bindPoint", 0))
            bindings.append({"name": binding["name"], "values": [0, index, (index << 8) | 2]})
    for binding in reflection.get("bindings") or []:
        if int(binding.get("type", 0)) == 0:
            bindings.append({"name": binding["name"], "values": [1, slot, 0]})
            slot += 1
    table = {"mask": 0, "pairs": [], "vectors": [], "textures": []}
    if program_type in D3D9_PROGRAM_TYPES:
        table.update({"unknownA": 0, "unknownB": 0, "unknownC": 0})
    else:
        table.update(
            {"buffers": [], "constantBuffers": constant_buffers, "bindings": bindings}
        )
    return table

def json_copy(value):
    if isinstance(value, dict):
        return {key: json_copy(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_copy(item) for item in value]
    return value

def pack_parameter_table(table: dict, program_type: int) -> bytes:
    writer = TableWriter()
    writer.i32(table.get("mask", 0))
    pairs = table.get("pairs") or []
    writer.i32(len(pairs))
    for pair in pairs:
        writer.i32(pair[0])
        writer.i32(pair[1])

    if program_type in D3D9_PROGRAM_TYPES:
        writer.i32(table.get("unknownA", 0))
        writer.i32(table.get("unknownB", 0))
        writer.i32(table.get("unknownC", 0))
        write_section(writer, table.get("vectors") or [], VECTOR_FIELDS)
        write_section(writer, table.get("textures") or [], TEXTURE_FIELDS)
    else:
        constant_buffers = table.get("constantBuffers") or []
        writer.i32(len(constant_buffers) + 1)
        write_section(writer, table.get("vectors") or [], VECTOR_FIELDS)
        write_section(writer, table.get("textures") or [], TEXTURE_FIELDS)
        write_section(writer, table.get("buffers") or [], BUFFER_FIELDS)
        for buffer in constant_buffers:
            writer.name(buffer["name"])
            writer.i32(buffer["byteSize"])
            params = buffer.get("params") or []
            writer.i32(len(params))
            for param in params:
                writer.name(param["name"])
                values = param["values"]
                if len(values) != CONSTANT_BUFFER_PARAM_FIELDS:
                    raise ShaderParameterError(
                        f"constant buffer parameter {param['name']!r} carries "
                        f"{len(values)} values, expected {CONSTANT_BUFFER_PARAM_FIELDS}"
                    )
                for value in values:
                    writer.i32(value)
        write_section(writer, table.get("bindings") or [], BINDING_FIELDS)
    return writer.bytes()
