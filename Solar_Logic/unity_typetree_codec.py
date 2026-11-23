from __future__ import annotations
import base64, io, json, math, struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from .binary import EndianReader
from .mono_repack import EndianWriter

ALIGN_BYTES_FLAG = 0x4000

class TypeTreeCodecError(ValueError):
    pass

@dataclass(slots=True)
class TypeTreeNode:
    level: int
    type_name: str
    name: str
    meta_flag: int
    children: list["TypeTreeNode"] = field(default_factory=list)

    @property
    def aligned(self) -> bool:
        return bool(self.meta_flag & ALIGN_BYTES_FLAG)

schema_cache: dict[str, TypeTreeNode] = {}

def load_schema(file_name: str) -> TypeTreeNode:
    cached = schema_cache.get(file_name)
    if cached is not None:
        return cached
    path = Path(__file__).with_name(file_name)
    document = json.loads(path.read_text(encoding="utf-8"))
    records = document.get("nodes")
    if not isinstance(records, list) or not records:
        raise TypeTreeCodecError(f"type-tree schema {file_name} has no nodes")
    root = build_tree(records)
    schema_cache[file_name] = root
    return root

def build_tree(records: list[Any]) -> TypeTreeNode:
    root: TypeTreeNode | None = None
    stack: list[TypeTreeNode] = []
    for record in records:
        if not isinstance(record, list) or len(record) != 4:
            raise TypeTreeCodecError("type-tree node records must have four items")
        level, type_name, name, meta_flag = record
        node = TypeTreeNode(int(level), str(type_name), str(name), int(meta_flag))
        while stack and stack[-1].level >= node.level:
            stack.pop()
        if stack:
            stack[-1].children.append(node)
        elif root is None:
            root = node
        else:
            raise TypeTreeCodecError("type-tree schema contains multiple roots")
        stack.append(node)
    if root is None:
        raise TypeTreeCodecError("type-tree schema root is missing")
    return root

def read_typetree(data: bytes, root: TypeTreeNode, endian: str) -> dict[str, Any]:
    reader = EndianReader(io.BytesIO(data), endian)
    value = read_value(root, reader)
    if reader.tell() != len(data):
        raise TypeTreeCodecError(
            f"type-tree reader consumed {reader.tell()} of {len(data)} bytes"
        )
    if not isinstance(value, dict):
        raise TypeTreeCodecError("type-tree root didnt decode to an object")
    return value

def write_typetree(value: dict[str, Any], root: TypeTreeNode, endian: str) -> bytes:
    writer = EndianWriter(endian)
    write_value(value, root, writer)
    return writer.bytes()

def read_value(node: TypeTreeNode, reader: EndianReader) -> Any:
    align = node.aligned
    primitive = read_primitive(node.type_name, reader)
    if primitive is not no_primitive:
        value = primitive
    elif node.type_name == "pair":
        value = [
            read_value(node.children[0], reader),
            read_value(node.children[1], reader),
        ]
    elif node.children and node.children[0].type_name == "Array":
        array_node = node.children[0]
        align = align or array_node.aligned
        size = reader.i32()
        if size < 0 or size > 100_000_000:
            raise TypeTreeCodecError(f"invalid array length {size} for {node.name}")
        subtype = array_node.children[1]
        packed = read_packed_array(subtype, reader, size)
        if packed is not no_primitive:
            value = packed
        elif subtype.aligned:
            value = read_value_array(subtype, reader, size)
        else:
            value = [read_value(subtype, reader) for _ in range(size)]
    else:
        value = {child.name: read_value(child, reader) for child in node.children}
    if align:
        reader.align()
    return value

def read_value_array(node: TypeTreeNode, reader: EndianReader, size: int) -> list[Any]:
    primitive_reader = primitive_reader_for(node.type_name)
    if primitive_reader is not None:
        values = [primitive_reader(reader) for _ in range(size)]
    elif node.type_name == "pair":
        values = [
            [
                read_value(node.children[0], reader),
                read_value(node.children[1], reader),
            ]
            for _ in range(size)
        ]
    elif node.children and node.children[0].type_name == "Array":
        subtype = node.children[0].children[1]
        values = []
        for _ in range(size):
            item_size = reader.i32()
            if item_size < 0 or item_size > 100_000_000:
                raise TypeTreeCodecError(f"invalid nested array length {item_size}")
            if subtype.aligned:
                values.append(read_value_array(subtype, reader, item_size))
            else:
                values.append([read_value(subtype, reader) for _ in range(item_size)])
    else:
        values = [
            {child.name: read_value(child, reader) for child in node.children}
            for _ in range(size)
        ]
    if node.aligned:
        reader.align()
    return values

def write_value(value: Any, node: TypeTreeNode, writer: EndianWriter) -> None:
    align = node.aligned
    if write_primitive(node.type_name, value, writer):
        pass
    elif node.type_name == "pair":
        if not isinstance(value, list) or len(value) != 2:
            raise TypeTreeCodecError(f"{node.name} must be a two-item list")
        write_value(value[0], node.children[0], writer)
        write_value(value[1], node.children[1], writer)
    elif node.children and node.children[0].type_name == "Array":
        array_node = node.children[0]
        align = align or array_node.aligned
        subtype = array_node.children[1]
        packed = packed_array_bytes(subtype, value, writer.endian)
        if packed is not None:
            writer.i32(packed[0])
            writer.write(packed[1])
        else:
            if not isinstance(value, list):
                raise TypeTreeCodecError(f"{node.name} must be a list")
            writer.i32(len(value))
            for item in value:
                write_value(item, subtype, writer)
    else:
        if not isinstance(value, dict):
            raise TypeTreeCodecError(f"{node.name} must be an object")
        for child in node.children:
            if child.name not in value:
                raise TypeTreeCodecError(f"{node.name} is missing field {child.name}")
            write_value(value[child.name], child, writer)
    if align:
        writer.align()

no_primitive = object()

def read_packed_array(node: TypeTreeNode, reader: EndianReader, size: int) -> Any:
    if node.children:
        return no_primitive
    if node.type_name in {"UInt8", "char", "SInt8"}:
        data = reader.read(size)
        return {
            "encoding": "base64",
            "byteLength": len(data),
            "data": base64.b64encode(data).decode("ascii"),
        }
    format_code = {
        "bool": "?",
        "short": "h",
        "SInt16": "h",
        "unsigned short": "H",
        "UInt16": "H",
        "int": "i",
        "SInt32": "i",
        "unsigned int": "I",
        "UInt32": "I",
        "Type*": "I",
        "long long": "q",
        "SInt64": "q",
        "unsigned long long": "Q",
        "UInt64": "Q",
        "FileSize": "Q",
        "float": "f",
        "double": "d",
    }.get(node.type_name)
    if format_code is None:
        return no_primitive
    item_size = struct.calcsize(format_code)
    data = reader.read(size * item_size)
    values = struct.unpack(reader.endian + f"{size}{format_code}", data)
    if format_code in {"f", "d"} and any(not math.isfinite(value) for value in values):
        sub_reader = EndianReader(io.BytesIO(data), reader.endian)
        return [read_primitive(node.type_name, sub_reader) for _ in range(size)]
    return list(values)

def packed_array_bytes(
    node: TypeTreeNode,
    value: Any,
    endian: str,
) -> tuple[int, bytes] | None:
    if node.children:
        return None
    if node.type_name in {"UInt8", "char", "SInt8"} and isinstance(value, dict):
        if value.get("encoding") != "base64":
            raise TypeTreeCodecError("byte array must use base64 encoding")
        data_text = value.get("data")
        if not isinstance(data_text, str):
            raise TypeTreeCodecError("byte array base64 data is missing")
        data = base64.b64decode(data_text)
        return len(data), data
    if not isinstance(value, list):
        return None
    format_code = {
        "bool": "?",
        "short": "h",
        "SInt16": "h",
        "unsigned short": "H",
        "UInt16": "H",
        "int": "i",
        "SInt32": "i",
        "unsigned int": "I",
        "UInt32": "I",
        "Type*": "I",
        "long long": "q",
        "SInt64": "q",
        "unsigned long long": "Q",
        "UInt64": "Q",
        "FileSize": "Q",
        "float": "f",
        "double": "d",
    }.get(node.type_name)
    if format_code is None:
        return None
    if format_code in {"f", "d"} and any(isinstance(item, dict) for item in value):
        return None
    return len(value), struct.pack(endian + f"{len(value)}{format_code}", *value)

def read_primitive(type_name: str, reader: EndianReader) -> Any:
    function = primitive_reader_for(type_name)
    if function is None:
        if type_name == "string":
            return reader.aligned_string()
        if type_name == "TypelessData":
            size = reader.i32()
            if size < 0 or size > reader.remaining():
                raise TypeTreeCodecError(f"invalid TypelessData length {size}")
            data = reader.read(size)
            return {
                "encoding": "base64",
                "byteLength": len(data),
                "data": base64.b64encode(data).decode("ascii"),
            }
        return no_primitive
    return function(reader)

def primitive_reader_for(type_name: str):
    return {
        "SInt8": read_i8,
        "UInt8": lambda reader: reader.u8(),
        "char": lambda reader: reader.u8(),
        "short": lambda reader: reader.i16(),
        "SInt16": lambda reader: reader.i16(),
        "unsigned short": lambda reader: reader.u16(),
        "UInt16": lambda reader: reader.u16(),
        "int": lambda reader: reader.i32(),
        "SInt32": lambda reader: reader.i32(),
        "unsigned int": lambda reader: reader.u32(),
        "UInt32": lambda reader: reader.u32(),
        "Type*": lambda reader: reader.u32(),
        "long long": lambda reader: reader.i64(),
        "SInt64": lambda reader: reader.i64(),
        "unsigned long long": lambda reader: reader.u64(),
        "UInt64": lambda reader: reader.u64(),
        "FileSize": lambda reader: reader.u64(),
        "float": read_float,
        "double": read_double,
        "bool": lambda reader: reader.bool(),
    }.get(type_name)

def write_primitive(type_name: str, value: Any, writer: EndianWriter) -> bool:
    if type_name == "string":
        writer.aligned_string(value)
        return True
    if type_name == "TypelessData":
        if not isinstance(value, dict) or value.get("encoding") != "base64":
            raise TypeTreeCodecError("TypelessData must be a base64 object")
        data_text = value.get("data")
        if not isinstance(data_text, str):
            raise TypeTreeCodecError("TypelessData base64 data is missing")
        data = base64.b64decode(data_text)
        writer.i32(len(data))
        writer.write(data)
        return True
    function = {
        "SInt8": lambda item: writer.i8(int(item)),
        "UInt8": lambda item: writer.u8(int(item)),
        "char": lambda item: writer.u8(int(item)),
        "short": lambda item: writer.i16(int(item)),
        "SInt16": lambda item: writer.i16(int(item)),
        "unsigned short": lambda item: writer.u16(int(item)),
        "UInt16": lambda item: writer.u16(int(item)),
        "int": lambda item: writer.i32(int(item)),
        "SInt32": lambda item: writer.i32(int(item)),
        "unsigned int": lambda item: writer.u32(int(item)),
        "UInt32": lambda item: writer.u32(int(item)),
        "Type*": lambda item: writer.u32(int(item)),
        "long long": lambda item: writer.i64(int(item)),
        "SInt64": lambda item: writer.i64(int(item)),
        "unsigned long long": lambda item: writer.u64(int(item)),
        "UInt64": lambda item: writer.u64(int(item)),
        "FileSize": lambda item: writer.u64(int(item)),
        "float": lambda item: write_float(item, writer),
        "double": lambda item: write_double(item, writer),
        "bool": lambda item: writer.bool(item),
    }.get(type_name)
    if function is None:
        return False
    function(value)
    return True

def read_i8(reader: EndianReader) -> int:
    return struct.unpack("b", reader.read(1))[0]

def read_float(reader: EndianReader) -> Any:
    data = reader.read(4)
    value = struct.unpack(reader.endian + "f", data)[0]
    if math.isfinite(value):
        return value
    return {"sunflowyrFloatBits": data.hex()}

def read_double(reader: EndianReader) -> Any:
    data = reader.read(8)
    value = struct.unpack(reader.endian + "d", data)[0]
    if math.isfinite(value):
        return value
    return {"sunflowyrDoubleBits": data.hex()}

def write_float(value: Any, writer: EndianWriter) -> None:
    if isinstance(value, dict) and "sunflowyrFloatBits" in value:
        data = bytes.fromhex(str(value["sunflowyrFloatBits"]))
        if len(data) != 4:
            raise TypeTreeCodecError("sunflowyrFloatBits must contain four bytes")
        writer.write(data)
    else:
        writer.f32(float(value))

def write_double(value: Any, writer: EndianWriter) -> None:
    if isinstance(value, dict) and "sunflowyrDoubleBits" in value:
        data = bytes.fromhex(str(value["sunflowyrDoubleBits"]))
        if len(data) != 8:
            raise TypeTreeCodecError("sunflowyrDoubleBits must contain eight bytes")
        writer.write(data)
    else:
        writer.write(struct.pack(writer.endian + "d", float(value)))
