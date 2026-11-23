from __future__ import annotations
import base64, struct, zlib
from dataclasses import dataclass
from functools import lru_cache
from .fmod_vorbis_profiles import RWBY_FMOD_VORBIS_PROFILES

class FmodVorbisError(ValueError):
    pass

@dataclass(frozen=True, slots=True)
class Fsb5VorbisInfo:
    channels: int
    frequency: int
    sample_count: int
    crc32: int
    packet_count: int
    audio_data_offset: int
    audio_data_size: int
    padding_bytes: int

FREQUENCIES = {
    0: 4000,
    1: 8000,
    2: 11000,
    3: 12000,
    4: 16000,
    5: 22050,
    6: 24000,
    7: 32000,
    8: 44100,
    9: 48000,
    10: 96000,
}

CHUNK_CHANNELS = 1
CHUNK_FREQUENCY = 2
CHUNK_VORBISDATA = 11

OGG_PAGE_MAX_SEGMENTS = 255
OGG_PAGE_MAX_BODY = 65025
OGG_SERIAL = 1

def rebuild_fsb5_vorbis_ogg(data: bytes) -> tuple[bytes, Fsb5VorbisInfo]:
    header = parse_fsb5_header(data)
    metadata = parse_sample_metadata(data, header)
    packets, padding_bytes = read_fsb_vorbis_packets(metadata.sample_bytes)
    if not packets:
        raise FmodVorbisError("FSB5 Vorbis sample has no packets")

    setup_packet = profile_setup_packet(metadata.crc32)
    block_flags = profile_block_flags(setup_packet, profile_seek_bit(metadata.crc32))
    ogg_packets = [
        (build_identification_packet(metadata.channels, metadata.frequency), 0, False),
        (build_comment_packet("Sunflowyr Engine"), 0, False),
        (setup_packet, 0, False),
    ]
    audio_packets = build_audio_packet_entries(packets, block_flags, metadata.sample_count)
    if not audio_packets:
        raise FmodVorbisError("FSB5 Vorbis sample produced no OGG audio packets")
    ogg_packets.extend(audio_packets)

    ogg_bytes = write_ogg_stream(ogg_packets)
    info = Fsb5VorbisInfo(
        channels=metadata.channels,
        frequency=metadata.frequency,
        sample_count=metadata.sample_count,
        crc32=metadata.crc32,
        packet_count=len(packets),
        audio_data_offset=metadata.audio_data_offset,
        audio_data_size=len(metadata.sample_bytes),
        padding_bytes=padding_bytes,
    )
    return ogg_bytes, info

@dataclass(frozen=True, slots=True)
class FsbHeader:
    version: int
    sample_count: int
    sample_header_size: int
    name_table_size: int
    data_size: int
    audio_type: int
    header_size: int
    data_offset: int

@dataclass(frozen=True, slots=True)
class VorbisMetadata:
    channels: int
    frequency: int
    sample_count: int
    crc32: int
    audio_data_offset: int
    sample_bytes: bytes

def parse_fsb5_header(data: bytes) -> FsbHeader:
    if len(data) < 60:
        raise FmodVorbisError(f"FSB5 payload is too small: {len(data)} bytes")
    magic, version, sample_count, sample_header_size, name_table_size, data_size, audio_type = struct.unpack_from(
        "<4sIIIIII", data, 0
    )
    if magic != b"FSB5":
        raise FmodVorbisError(f"not an FSB5 payload: {magic!r}")
    if sample_count != 1:
        raise FmodVorbisError(f"unsupported FSB5 sample count {sample_count}")
    header_size = 0x40 if version == 0 else 0x3C
    data_offset = header_size + sample_header_size + name_table_size
    if data_offset > len(data):
        raise FmodVorbisError(f"FSB5 data offset {data_offset} exceeds payload size {len(data)}")
    return FsbHeader(
        version=version,
        sample_count=sample_count,
        sample_header_size=sample_header_size,
        name_table_size=name_table_size,
        data_size=data_size,
        audio_type=audio_type,
        header_size=header_size,
        data_offset=data_offset,
    )

def parse_sample_metadata(data: bytes, header: FsbHeader) -> VorbisMetadata:
    sample_header_start = header.header_size
    sample_header_end = sample_header_start + header.sample_header_size
    if sample_header_end > len(data):
        raise FmodVorbisError("FSB5 sample header exceeds payload size")
    if sample_header_end - sample_header_start < 8:
        raise FmodVorbisError("FSB5 sample header is too small")

    encoded = struct.unpack_from("<Q", data, sample_header_start)[0]
    has_chunks = bool(encoded & 1)
    frequency_id = bits(encoded, 1, 4)
    channel_bits = bits(encoded, 5, 2)
    channels = {0: 1, 1: 2, 2: 6, 3: 8}.get(channel_bits, 0)
    data_offset = bits(encoded, 7, 27) * 32
    sample_count = bits(encoded, 34, 30)
    crc32: int | None = None

    cursor = sample_header_start + 8
    if has_chunks:
        more_chunks = True
        while more_chunks:
            if cursor + 4 > sample_header_end:
                raise FmodVorbisError("FSB5 sample chunk header exceeds sample header")
            chunk_raw = struct.unpack_from("<I", data, cursor)[0]
            cursor += 4
            more_chunks = bool(bits(chunk_raw, 0, 1))
            chunk_size = bits(chunk_raw, 1, 24)
            chunk_type = bits(chunk_raw, 25, 7)
            if cursor + chunk_size > sample_header_end:
                raise FmodVorbisError("FSB5 sample chunk body exceeds sample header")
            chunk = data[cursor : cursor + chunk_size]
            cursor += chunk_size

            if chunk_type == CHUNK_VORBISDATA and len(chunk) >= 4:
                crc32 = struct.unpack_from("<I", chunk, 0)[0]
            elif chunk_type == CHUNK_FREQUENCY and len(chunk) >= 4:
                frequency_id = struct.unpack_from("<I", chunk, 0)[0]
            elif chunk_type == CHUNK_CHANNELS and chunk:
                channels = chunk[0]

    if crc32 is None:
        raise FmodVorbisError("FSB5 Vorbis sample has no VORBISDATA CRC chunk")
    frequency = FREQUENCIES.get(frequency_id, frequency_id)
    if channels <= 0 or frequency <= 0 or sample_count <= 0:
        raise FmodVorbisError(
            f"invalid FSB5 Vorbis metadata channels={channels} frequency={frequency} samples={sample_count}"
        )

    sample_start = header.data_offset + data_offset
    sample_available = max(0, min(header.data_size - data_offset, len(data) - sample_start))
    if sample_available <= 0:
        raise FmodVorbisError("FSB5 Vorbis sample has no audio data")
    return VorbisMetadata(
        channels=channels,
        frequency=frequency,
        sample_count=sample_count,
        crc32=crc32,
        audio_data_offset=sample_start,
        sample_bytes=data[sample_start : sample_start + sample_available],
    )

def read_fsb_vorbis_packets(sample_bytes: bytes) -> tuple[list[bytes], int]:
    packets: list[bytes] = []
    cursor = 0
    while cursor + 2 < len(sample_bytes):
        packet_size = struct.unpack_from("<H", sample_bytes, cursor)[0]
        cursor += 2
        if packet_size == 0 or packet_size == 0xFFFF:
            break
        if cursor + packet_size > len(sample_bytes):
            raise FmodVorbisError(
                f"FSB5 Vorbis packet size {packet_size} exceeds remaining sample data {len(sample_bytes) - cursor}"
            )
        packets.append(sample_bytes[cursor : cursor + packet_size])
        cursor += packet_size
    return packets, max(0, len(sample_bytes) - cursor)

@lru_cache(maxsize=None)
def profile_setup_packet(crc32: int) -> bytes:
    if crc32 not in RWBY_FMOD_VORBIS_PROFILES:
        raise FmodVorbisError(f"unsupported RWBY FMOD Vorbis setup CRC 0x{crc32:08x}")
    seek_bit, compressed = RWBY_FMOD_VORBIS_PROFILES[crc32]
    return zlib.decompress(base64.b64decode(compressed))

def profile_seek_bit(crc32: int) -> int:
    if crc32 not in RWBY_FMOD_VORBIS_PROFILES:
        raise FmodVorbisError(f"unsupported RWBY FMOD Vorbis setup CRC 0x{crc32:08x}")
    seek_bit, compressed = RWBY_FMOD_VORBIS_PROFILES[crc32]
    return seek_bit

@lru_cache(maxsize=None)
def profile_block_flags(setup_packet: bytes, seek_bit: int) -> tuple[int, ...]:
    reader = BitReader(setup_packet)
    if reader.read_byte() != 5 or reader.read_string(6) != "vorbis":
        raise FmodVorbisError("invalid FMOD Vorbis setup packet")
    reader.seek(seek_bit // 8, seek_bit % 8)
    mode_count = reader.read_byte(6) + 1
    flags: list[int] = []
    for _ in range(mode_count):
        flags.append(reader.read_bit())
        reader.read_bits(16)
        reader.read_bits(16)
        reader.read_bits(8)
    if not flags:
        raise FmodVorbisError("FMOD Vorbis setup packet has no block flags")
    return tuple(flags)

def build_identification_packet(channels: int, frequency: int) -> bytes:
    return struct.pack(
        "<B6sIBIiiiBB",
        1,
        b"vorbis",
        0,
        channels,
        frequency,
        0,
        0,
        0,
        0b1011_1000,
        1,
    )

def build_comment_packet(vendor: str) -> bytes:
    encoded_vendor = vendor.encode("utf-8")
    return b"".join(
        (
            b"\x03vorbis",
            struct.pack("<I", len(encoded_vendor)),
            encoded_vendor,
            struct.pack("<I", 0),
            b"\x01",
        )
    )

def build_audio_packet_entries(
    packets: list[bytes], block_flags: tuple[int, ...], sample_count: int
) -> list[tuple[bytes, int, bool]]:
    entries: list[tuple[bytes, int, bool]] = []
    granule_position = 0
    previous_block_size = 0
    final_index = len(packets) - 1
    for index, packet in enumerate(packets):
        block_size = 0 if not packet else packet_block_size(packet, block_flags)
        if previous_block_size == 0:
            granule_position = 0
        else:
            granule_position += (block_size + previous_block_size) // 4
        if granule_position > sample_count:
            granule_position = sample_count
        previous_block_size = block_size

        is_last = index == final_index or granule_position == sample_count
        entries.append((packet, granule_position, is_last))
        if granule_position == sample_count:
            break
    return entries

def ilog(value: int) -> int:
    count = 0
    while value > 0:
        count += 1
        value >>= 1
    return count

def packet_block_size(packet: bytes, block_flags: tuple[int, ...]) -> int:
    reader = BitReader(packet)
    if reader.read_bit():
        return 0
    mode = 0
    if len(block_flags) > 1:
        mode = reader.read_byte(ilog(len(block_flags) - 1))
    if mode >= len(block_flags):
        raise FmodVorbisError(f"Vorbis mode {mode} outside block flag table of {len(block_flags)}")
    return 2048 if block_flags[mode] else 256

class BitReader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.bit_position = 0

    def seek(self, byte_offset: int, bit_offset: int = 0) -> None:
        self.bit_position = byte_offset * 8 + bit_offset

    def read_bit(self) -> int:
        byte_index, bit_index = divmod(self.bit_position, 8)
        if byte_index >= len(self.data):
            raise FmodVorbisError("bit read exceeds buffer")
        self.bit_position += 1
        return (self.data[byte_index] >> bit_index) & 1

    def read_bits(self, count: int) -> int:
        value = 0
        for bit in range(max(0, min(count, 64))):
            value |= self.read_bit() << bit
        return value

    def read_byte(self, bit_count: int = 8) -> int:
        return self.read_bits(max(0, min(bit_count, 8)))

    def read_string(self, byte_count: int) -> str:
        return bytes(self.read_byte() for _ in range(byte_count)).decode("utf-8", errors="replace")

def write_ogg_stream(packet_entries: list[tuple[bytes, int, bool]]) -> bytes:
    pages: list[bytes] = []
    sequence = 0
    pages.append(build_ogg_page([(packet_entries[0][0], packet_entries[0][1])], 0, 0x02, sequence))
    sequence += 1
    pages.append(
        build_ogg_page(
            [(packet_entries[1][0], packet_entries[1][1]), (packet_entries[2][0], packet_entries[2][1])],
            0,
            0,
            sequence,
        )
    )
    sequence += 1

    pending: list[tuple[bytes, int]] = []
    pending_segments = 0
    pending_body_size = 0
    for packet, granule, is_last in packet_entries[3:]:
        packet_lace_count = len(packet_laces(packet))
        if pending and (
            pending_segments + packet_lace_count > OGG_PAGE_MAX_SEGMENTS
            or pending_body_size + len(packet) > OGG_PAGE_MAX_BODY
        ):
            pages.append(build_ogg_page(pending, pending[-1][1], 0, sequence))
            sequence += 1
            pending = []
            pending_segments = 0
            pending_body_size = 0

        pending.append((packet, granule))
        pending_segments += packet_lace_count
        pending_body_size += len(packet)

        if is_last:
            pages.append(build_ogg_page(pending, granule, 0x04, sequence))
            sequence += 1
            pending = []
            pending_segments = 0
            pending_body_size = 0
            break

    if pending:
        pages.append(build_ogg_page(pending, pending[-1][1], 0x04, sequence))
    return b"".join(pages)

def build_ogg_page(packets: list[tuple[bytes, int]], granule_position: int, flags: int, sequence: int) -> bytes:
    laces = bytearray()
    body = bytearray()
    for packet, packet_granule in packets:
        laces.extend(packet_laces(packet))
        body.extend(packet)
    if len(laces) > OGG_PAGE_MAX_SEGMENTS:
        raise FmodVorbisError("OGG page has too many lacing segments")
    header = struct.pack(
        "<4sBBqIIIB",
        b"OggS",
        0,
        flags,
        granule_position,
        OGG_SERIAL,
        sequence,
        0,
        len(laces),
    ) + bytes(laces)
    crc = ogg_crc(header + body)
    header = header[:22] + struct.pack("<I", crc) + header[26:]
    return header + body

def packet_laces(packet: bytes) -> list[int]:
    full, remainder = divmod(len(packet), 255)
    return [255] * full + [remainder]

@lru_cache(maxsize=1)
def ogg_crc_table() -> tuple[int, ...]:
    table: list[int] = []
    for value in range(256):
        crc = value << 24
        for _ in range(8):
            if crc & 0x80000000:
                crc = ((crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF
            else:
                crc = (crc << 1) & 0xFFFFFFFF
        table.append(crc)
    return tuple(table)

def ogg_crc(data: bytes) -> int:
    crc = 0
    table = ogg_crc_table()
    for value in data:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ table[((crc >> 24) & 0xFF) ^ value]
    return crc

def bits(value: int, lowest_bit: int, num_bits: int) -> int:
    return (value >> lowest_bit) & ((1 << num_bits) - 1)
