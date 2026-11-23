from __future__ import annotations
import struct
from pathlib import Path
from typing import BinaryIO

class BinaryReadError(ValueError):
    pass

class EndianReader:

    def __init__(self, stream: BinaryIO, endian: str = ">"):
        self.stream = stream
        self.endian = endian

    @classmethod
    def open(cls, path: Path, endian: str = ">") -> "EndianReader":
        return cls(path.open("rb"), endian)

    def close(self) -> None:
        self.stream.close()

    def tell(self) -> int:
        return self.stream.tell()

    def seek(self, offset: int, whence: int = 0) -> int:
        return self.stream.seek(offset, whence)

    def align(self, alignment: int = 4) -> None:
        pos = self.tell()
        mod = pos % alignment
        if mod:
            self.seek(alignment - mod, 1)

    def read(self, size: int) -> bytes:
        data = self.stream.read(size)
        if len(data) != size:
            raise EOFError(f"wanted {size} bytes at 0x{self.tell():x}, got {len(data)}")
        return data

    def u8(self) -> int:
        return self.read(1)[0]

    def bool(self) -> bool:
        return self.u8() != 0

    def i16(self) -> int:
        return struct.unpack(self.endian + "h", self.read(2))[0]

    def u16(self) -> int:
        return struct.unpack(self.endian + "H", self.read(2))[0]

    def i32(self) -> int:
        return struct.unpack(self.endian + "i", self.read(4))[0]

    def u32(self) -> int:
        return struct.unpack(self.endian + "I", self.read(4))[0]

    def i64(self) -> int:
        return struct.unpack(self.endian + "q", self.read(8))[0]

    def u64(self) -> int:
        return struct.unpack(self.endian + "Q", self.read(8))[0]

    def f32(self) -> float:
        return struct.unpack(self.endian + "f", self.read(4))[0]

    def cstring(self, max_length: int = 32767) -> str:
        out = bytearray()
        count = 0
        while count < max_length:
            b = self.stream.read(1)
            if not b or b == b"\x00":
                break
            out.extend(b)
            count += 1
        return out.decode("utf-8", "replace")

    def aligned_string(self, max_length: int = 16 * 1024 * 1024) -> str:
        length = self.i32()
        if length == 0:
            self.align()
            return ""
        remaining = self.remaining()
        if length < 0 or length > remaining or length > max_length:
            raise BinaryReadError(
                f"aligned string at offset {self.tell() - 4} declares {length} bytes but "
                f"{remaining} remain and the max is {max_length}"
            )
        data = self.read(length)
        self.align()
        return data.decode("utf-8", "replace")

    def remaining(self) -> int:
        pos = self.tell()
        self.seek(0, 2)
        end = self.tell()
        self.seek(pos)
        return end - pos

def read_range(path: Path, offset: int, size: int, chunk_size: int = 1024 * 1024):
    with path.open("rb") as handle:
        handle.seek(offset)
        left = size
        while left > 0:
            chunk = handle.read(min(chunk_size, left))
            if not chunk:
                raise EOFError(f"unexpected EOF in {path} at 0x{handle.tell():x}")
            left -= len(chunk)
            yield chunk
