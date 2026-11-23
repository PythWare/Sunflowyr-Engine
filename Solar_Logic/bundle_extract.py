from __future__ import annotations
import lzma, struct
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path, PurePosixPath

class BundleExtractError(ValueError):
    pass

@dataclass(slots=True)
class BundleNode:
    path: str
    offset: int
    size: int
    flags: int = 0

class BundleReader:
    def __init__(self, data: bytes, endian: str = ">") -> None:
        self.stream = BytesIO(data)
        self.endian = endian

    def tell(self) -> int:
        return self.stream.tell()

    def seek(self, offset: int, whence: int = 0) -> int:
        return self.stream.seek(offset, whence)

    def read(self, size: int) -> bytes:
        data = self.stream.read(size)
        if len(data) != size:
            raise BundleExtractError(f"bundle ended at 0x{self.tell():x}")
        return data

    def cstring(self) -> str:
        output = bytearray()
        while True:
            value = self.read(1)
            if value == b"\0":
                return output.decode("utf-8", "replace")
            output.extend(value)

    def u16(self) -> int:
        return struct.unpack(self.endian + "H", self.read(2))[0]

    def u32(self) -> int:
        return struct.unpack(self.endian + "I", self.read(4))[0]

    def i32(self) -> int:
        return struct.unpack(self.endian + "i", self.read(4))[0]

    def i64(self) -> int:
        return struct.unpack(self.endian + "q", self.read(8))[0]

    def align(self, alignment: int) -> None:
        padding = (-self.tell()) % alignment
        if padding:
            self.seek(padding, 1)

def extract_bundle(bundle_path: Path, output_root: Path) -> list[Path]:
    bundle_path = Path(bundle_path)
    data = bundle_path.read_bytes()
    signature = data.split(b"\0", 1)[0].decode("ascii", "replace")
    if signature == "UnityFS":
        nodes, blocks = read_unity_fs(data)
    elif signature in {"UnityWeb", "UnityRaw"}:
        nodes, blocks = read_unity_web(data)
    else:
        raise BundleExtractError(f"{bundle_path.name} isnt a supported Unity bundle")
    output_root = Path(output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    outputs: list[Path] = []
    for node in nodes:
        relative = safe_node_path(node.path)
        output_path = output_root.joinpath(*relative.parts)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        end = node.offset + node.size
        if node.offset < 0 or end > len(blocks):
            raise BundleExtractError(f"bundle node {node.path} is outside decompressed data")
        output_path.write_bytes(blocks[node.offset:end])
        outputs.append(output_path)
    return outputs

def read_unity_fs(data: bytes) -> tuple[list[BundleNode], bytes]:
    reader = BundleReader(data)
    signature = reader.cstring()
    version = reader.u32()
    reader.cstring()
    reader.cstring()
    bundle_size = reader.i64()
    compressed_info_size = reader.u32()
    uncompressed_info_size = reader.u32()
    flags = reader.u32()
    if signature not in {"UnityFS", "UnityWeb", "UnityRaw"} or bundle_size > len(data):
        raise BundleExtractError("invalid UnityFS header")
    if signature != "UnityFS":
        reader.read(1)
    if version >= 7:
        reader.align(16)
    data_start = reader.tell()
    if flags & 0x80:
        info_position = len(data) - compressed_info_size
        compressed_info = data[info_position : info_position + compressed_info_size]
        reader.seek(data_start)
    else:
        compressed_info = reader.read(compressed_info_size)
    info_data = decompress_bundle_block(
        compressed_info,
        uncompressed_info_size,
        flags & 0x3F,
    )
    info = BundleReader(info_data)
    info.read(16)
    block_count = info.i32()
    blocks_info: list[tuple[int, int, int]] = []
    for block_index in range(block_count):
        blocks_info.append((info.u32(), info.u32(), info.u16()))
    node_count = info.i32()
    nodes: list[BundleNode] = []
    for node_index in range(node_count):
        offset = info.i64()
        size = info.i64()
        node_flags = info.u32()
        path = info.cstring()
        nodes.append(BundleNode(path=path, offset=offset, size=size, flags=node_flags))
    if flags & 0x200:
        reader.align(16)
    output = bytearray()
    for uncompressed_size, compressed_size, block_flags in blocks_info:
        compressed = reader.read(compressed_size)
        output.extend(
            decompress_bundle_block(
                compressed,
                uncompressed_size,
                block_flags & 0x3F,
            )
        )
    return nodes, bytes(output)

def read_unity_web(data: bytes) -> tuple[list[BundleNode], bytes]:
    reader = BundleReader(data)
    signature = reader.cstring()
    version = reader.u32()
    reader.cstring()
    reader.cstring()
    if version == 6:
        return read_unity_fs(data)
    if version >= 4:
        reader.read(16)
        reader.u32()
    reader.u32()
    header_size = reader.u32()
    reader.u32()
    level_count = reader.i32()
    levels = [(reader.u32(), reader.u32()) for level_index in range(level_count)]
    if version >= 2:
        reader.u32()
    if version >= 3:
        reader.u32()
    if not levels:
        raise BundleExtractError("UnityWeb bundle has no storage levels")
    compressed_size, expected_size = levels[-1]
    reader.seek(header_size)
    compressed = reader.read(compressed_size)
    blocks = (
        lzma.decompress(compressed, format=lzma.FORMAT_ALONE)
        if signature == "UnityWeb"
        else compressed
    )
    if expected_size and len(blocks) != expected_size:
        raise BundleExtractError(
            f"UnityWeb block decoded to {len(blocks)} bytes, expected {expected_size}"
        )
    directory = BundleReader(blocks)
    node_count = directory.i32()
    nodes = [
        BundleNode(
            path=directory.cstring(),
            offset=directory.u32(),
            size=directory.u32(),
        )
        for node_index in range(node_count)
    ]
    return nodes, blocks

def decompress_bundle_block(data: bytes, expected_size: int, compression_type: int) -> bytes:
    if compression_type == 0:
        output = data
    elif compression_type == 1:
        if len(data) < 5:
            raise BundleExtractError("LZMA block is missing decoder properties")
        property_value = data[0]
        lc = property_value % 9
        remainder = property_value // 9
        lp = remainder % 5
        pb = remainder // 5
        dictionary_size = int.from_bytes(data[1:5], "little")
        output = lzma.decompress(
            data[5:],
            format=lzma.FORMAT_RAW,
            filters=[
                {
                    "id": lzma.FILTER_LZMA1,
                    "dict_size": max(4096, dictionary_size),
                    "lc": lc,
                    "lp": lp,
                    "pb": pb,
                }
            ],
        )
    elif compression_type in {2, 3}:
        output = decompress_lz4(data, expected_size)
    else:
        raise BundleExtractError(f"unsupported bundle compression type {compression_type}")
    if expected_size and len(output) != expected_size:
        raise BundleExtractError(
            f"bundle block decoded to {len(output)} bytes, expected {expected_size}"
        )
    return output

def decompress_lz4(data: bytes, expected_size: int) -> bytes:
    output = bytearray()
    source = 0
    while source < len(data):
        token = data[source]
        source += 1
        literal_length = token >> 4
        if literal_length == 15:
            while source < len(data):
                value = data[source]
                source += 1
                literal_length += value
                if value != 255:
                    break
        if source + literal_length > len(data):
            raise BundleExtractError("LZ4 literal run exceeds the compressed block")
        output.extend(data[source : source + literal_length])
        source += literal_length
        if source >= len(data):
            break
        if source + 2 > len(data):
            raise BundleExtractError("LZ4 match offset is truncated")
        match_offset = data[source] | (data[source + 1] << 8)
        source += 2
        if match_offset <= 0 or match_offset > len(output):
            raise BundleExtractError("LZ4 match offset is invalid")
        match_length = token & 0x0F
        if match_length == 15:
            while source < len(data):
                value = data[source]
                source += 1
                match_length += value
                if value != 255:
                    break
        match_length += 4
        match_start = len(output) - match_offset
        for index in range(match_length):
            output.append(output[match_start + index])
    if expected_size and len(output) != expected_size:
        raise BundleExtractError(
            f"LZ4 block decoded to {len(output)} bytes, expected {expected_size}"
        )
    return bytes(output)

def safe_node_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value.replace("\\", "/"))
    parts = [part for part in path.parts if part not in {"", ".", "..", "/"}]
    if not parts:
        raise BundleExtractError("bundle node has an empty path")
    return PurePosixPath(*parts)
