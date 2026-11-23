from __future__ import annotations
import io, struct, wave, zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from .binary import EndianReader
from .fmod_vorbis_profiles import RWBY_FMOD_VORBIS_PROFILES
from .media_settings import apply_media_settings
from .mono_repack import taildata_value
from .serialized import SerializedAsset, SerializedObject, load_serialized_asset

class AudioRepackError(ValueError):
    pass

FREQUENCY_IDS = {
    4000: 0,
    8000: 1,
    11000: 2,
    12000: 3,
    16000: 4,
    22050: 5,
    24000: 6,
    32000: 7,
    44100: 8,
    48000: 9,
    96000: 10,
}
CHANNEL_IDS = {
    1: 0,
    2: 1,
    6: 2,
    8: 3,
}
PCM_AUDIO_TYPES = {
    8: 1,
    16: 2,
    24: 3,
    32: 4,
}
FSB5_VORBIS_AUDIO_TYPE = 15
FSB5_HEADER_SIZE = 60
CHUNK_CHANNELS = 1
CHUNK_FREQUENCY = 2
CHUNK_VORBISDATA = 11

asset_cache: dict[tuple[str, str], SerializedAsset] = {}

@dataclass(slots=True)
class AudioInput:
    fsb: bytes
    channels: int
    frequency: int
    bits_per_sample: int
    sample_count: int
    compression: int
    source_format: str
    vorbis_crc32: int | None = None

@dataclass(slots=True)
class AudioReplacement:
    object_payload: bytes
    resource_payload: bytes
    resource_container: str
    resource_offset_field_offset: int
    resource_alignment: int
    source_format: str

def repack_audio_view(
    audio_path: Path,
    taildata: dict[str, Any],
    data_root: Path,
) -> AudioReplacement:
    class_id = int(taildata_value(taildata, "classId", 0) or 0)
    if class_id != 83:
        raise AudioRepackError(
            f"audio taildata expected AudioClip class 83, found {class_id}"
        )
    container = str(taildata_value(taildata, "container") or "")
    path_id = int(taildata_value(taildata, "pathId", 0) or 0)
    if not container or not path_id:
        raise AudioRepackError("audio taildata is missing container or pathId")

    asset = load_audio_asset(Path(data_root), container)
    obj = find_audio_object(asset, path_id)
    original = read_object_bytes(asset, obj)
    layout = read_audio_layout(original, asset)
    if not layout.source:
        raise AudioRepackError(
            "RWBY AudioClip replacement requires the original external resource path"
        )
    original_fsb = read_original_fsb_header(Path(data_root), asset, layout)
    header_identity = original_fsb[36:60]

    suffix = Path(audio_path).suffix.lower()
    if suffix == ".wav":
        audio_input = build_fsb5_pcm_from_wav(
            Path(audio_path),
            header_identity=header_identity,
        )
    elif suffix == ".ogg":
        audio_input = build_fsb5_vorbis_from_ogg(
            Path(audio_path),
            header_identity=header_identity,
        )
    else:
        raise AudioRepackError(
            f"unsupported audio view extension {suffix or '<none>'}; use PCM WAV"
        )

    rebuilt = bytearray(original)
    pack_i32(rebuilt, layout.channels_offset, audio_input.channels, asset.endian)
    pack_i32(rebuilt, layout.frequency_offset, audio_input.frequency, asset.endian)
    pack_i32(
        rebuilt,
        layout.bits_per_sample_offset,
        audio_input.bits_per_sample,
        asset.endian,
    )
    duration = (
        audio_input.sample_count / audio_input.frequency
        if audio_input.frequency > 0
        else 0.0
    )
    pack_f32(rebuilt, layout.length_offset, duration, asset.endian)
    pack_i64(rebuilt, layout.resource_offset_offset, 0, asset.endian)
    pack_i64(rebuilt, layout.resource_size_offset, len(audio_input.fsb), asset.endian)
    pack_i32(rebuilt, layout.compression_offset, audio_input.compression, asset.endian)
    return AudioReplacement(
        object_payload=apply_media_settings(bytes(rebuilt), 83, asset.endian, Path(audio_path)),
        resource_payload=audio_input.fsb,
        resource_container=layout.source,
        resource_offset_field_offset=layout.resource_offset_offset,
        resource_alignment=32,
        source_format=audio_input.source_format,
    )

@dataclass(frozen=True, slots=True)
class AudioLayout:
    channels_offset: int
    frequency_offset: int
    bits_per_sample_offset: int
    length_offset: int
    source: str
    resource_offset: int
    resource_size: int
    resource_offset_offset: int
    resource_size_offset: int
    compression_offset: int

def read_audio_layout(data: bytes, asset: SerializedAsset) -> AudioLayout:
    reader = EndianReader(io.BytesIO(data), asset.endian)
    reader.aligned_string(max_length=4096)
    reader.i32()
    channels_offset = reader.tell()
    reader.i32()
    frequency_offset = reader.tell()
    reader.i32()
    bits_per_sample_offset = reader.tell()
    reader.i32()
    length_offset = reader.tell()
    reader.f32()
    reader.bool()
    reader.align()
    reader.i32()
    reader.bool()
    reader.bool()
    reader.bool()
    reader.align()
    source = reader.aligned_string(max_length=4096)
    resource_offset_offset = reader.tell()
    resource_offset = reader.i64()
    resource_size_offset = reader.tell()
    resource_size = reader.i64()
    compression_offset = reader.tell()
    reader.i32()
    return AudioLayout(
        channels_offset=channels_offset,
        frequency_offset=frequency_offset,
        bits_per_sample_offset=bits_per_sample_offset,
        length_offset=length_offset,
        source=source,
        resource_offset=resource_offset,
        resource_size=resource_size,
        resource_offset_offset=resource_offset_offset,
        resource_size_offset=resource_size_offset,
        compression_offset=compression_offset,
    )

def build_fsb5_pcm_from_wav(
    wav_path: Path,
    *,
    header_identity: bytes | None = None,
) -> AudioInput:
    try:
        with wave.open(str(wav_path), "rb") as source:
            if source.getcomptype() != "NONE":
                raise AudioRepackError(
                    f"WAV compression {source.getcomptype()} is unsupported, use PCM WAV"
                )
            channels = source.getnchannels()
            frequency = source.getframerate()
            sample_width = source.getsampwidth()
            sample_count = source.getnframes()
            pcm = source.readframes(sample_count)
    except (wave.Error, EOFError) as exc:
        raise AudioRepackError(f"invalid PCM WAV: {exc}") from exc

    bits_per_sample = sample_width * 8
    audio_type = PCM_AUDIO_TYPES.get(bits_per_sample)
    if audio_type is None:
        raise AudioRepackError(
            f"unsupported PCM WAV bit depth {bits_per_sample}; use 8, 16, 24, or 32 bit"
        )
    if channels <= 0 or channels > 255:
        raise AudioRepackError(f"unsupported WAV channel count {channels}")
    if frequency <= 0:
        raise AudioRepackError(f"invalid WAV frequency {frequency}")
    expected_size = sample_count * channels * sample_width
    if len(pcm) != expected_size:
        raise AudioRepackError(
            f"WAV PCM payload has {len(pcm)} bytes, expected {expected_size}"
        )
    fsb = build_fsb5(
        audio_type=audio_type,
        channels=channels,
        frequency=frequency,
        sample_count=sample_count,
        sample_data=pcm,
        chunks=[],
        header_identity=header_identity,
    )
    return AudioInput(
        fsb=fsb,
        channels=channels,
        frequency=frequency,
        bits_per_sample=bits_per_sample,
        sample_count=sample_count,
        compression=0,
        source_format="wav_pcm",
    )

def build_fsb5_vorbis_from_ogg(
    ogg_path: Path,
    *,
    header_identity: bytes | None = None,
) -> AudioInput:
    return build_fsb5_vorbis(
        Path(ogg_path).read_bytes(),
        header_identity=header_identity,
    )

def build_fsb5_vorbis(
    ogg_data: bytes,
    *,
    header_identity: bytes | None = None,
) -> AudioInput:
    packets, sample_count = read_ogg_packets(ogg_data)
    if len(packets) < 4:
        raise AudioRepackError("OGG Vorbis stream has no audio packets")
    identification = packets[0]
    if len(identification) < 16 or identification[:7] != b"\x01vorbis":
        raise AudioRepackError("OGG identification packet isnt Vorbis")
    channels = identification[11]
    frequency = struct.unpack_from("<I", identification, 12)[0]
    if packets[1][:7] != b"\x03vorbis" or packets[2][:7] != b"\x05vorbis":
        raise AudioRepackError("OGG Vorbis headers are incomplete or out of order")
    setup_packet = packets[2]
    crc32 = zlib.crc32(setup_packet) & 0xFFFFFFFF
    if crc32 not in RWBY_FMOD_VORBIS_PROFILES:
        raise AudioRepackError(
            f"OGG setup profile 0x{crc32:08x} isnt one RWBY uses, save the edit as a WAV with the same name instead"
        )
    if sample_count <= 0:
        raise AudioRepackError("OGG stream doesnt provide a final sample count")

    audio_packets = packets[3:]
    packet_stream = bytearray()
    for packet in audio_packets:
        if not packet:
            continue
        if len(packet) >= 0xFFFF:
            raise AudioRepackError(
                f"OGG packet size {len(packet)} exceeds FSB5 Vorbis limits"
            )
        packet_stream.extend(struct.pack("<H", len(packet)))
        packet_stream.extend(packet)
    packet_stream.extend(b"\0\0")
    if len(packet_stream) <= 2:
        raise AudioRepackError("OGG stream contains no usable Vorbis audio packets")

    fsb = build_fsb5(
        audio_type=FSB5_VORBIS_AUDIO_TYPE,
        channels=channels,
        frequency=frequency,
        sample_count=sample_count,
        sample_data=bytes(packet_stream),
        chunks=[(CHUNK_VORBISDATA, struct.pack("<I", crc32))],
        header_identity=header_identity,
    )
    return AudioInput(
        fsb=fsb,
        channels=channels,
        frequency=frequency,
        bits_per_sample=16,
        sample_count=sample_count,
        compression=1,
        source_format="ogg_vorbis",
        vorbis_crc32=crc32,
    )

def build_fsb5(
    *,
    audio_type: int,
    channels: int,
    frequency: int,
    sample_count: int,
    sample_data: bytes,
    chunks: list[tuple[int, bytes]],
    header_identity: bytes | None = None,
) -> bytes:
    if sample_count <= 0 or sample_count >= (1 << 30):
        raise AudioRepackError(f"FSB5 sample count {sample_count} is invalid")
    frequency_id = FREQUENCY_IDS.get(frequency)
    channel_id = CHANNEL_IDS.get(channels)
    sample_chunks = list(chunks)
    if frequency_id is None:
        frequency_id = 0
        sample_chunks.insert(0, (CHUNK_FREQUENCY, struct.pack("<I", frequency)))
    if channel_id is None:
        channel_id = 0
        sample_chunks.insert(0, (CHUNK_CHANNELS, bytes((channels,))))

    encoded = 0
    if sample_chunks:
        encoded |= 1
    encoded |= int(frequency_id) << 1
    encoded |= int(channel_id) << 5
    encoded |= int(sample_count) << 34
    sample_header = bytearray(struct.pack("<Q", encoded))
    for index, (chunk_type, body) in enumerate(sample_chunks):
        more_chunks = index + 1 < len(sample_chunks)
        chunk_header = (
            (1 if more_chunks else 0)
            | (len(body) << 1)
            | (int(chunk_type) << 25)
        )
        sample_header.extend(struct.pack("<I", chunk_header))
        sample_header.extend(body)
    sample_header_size = align_value(
        FSB5_HEADER_SIZE + len(sample_header),
        32,
    ) - FSB5_HEADER_SIZE
    sample_header.extend(b"\0" * (sample_header_size - len(sample_header)))

    padded_data_size = align_value(len(sample_data), 32) + 32
    padded_data = sample_data + b"\0" * (padded_data_size - len(sample_data))
    header = bytearray(FSB5_HEADER_SIZE)
    struct.pack_into(
        "<4sIIIIII",
        header,
        0,
        b"FSB5",
        1,
        1,
        sample_header_size,
        0,
        padded_data_size,
        audio_type,
    )
    struct.pack_into("<I", header, 28, 1 if audio_type == 15 else 0)
    if header_identity is not None:
        if len(header_identity) != 24:
            raise AudioRepackError("FSB5 header identity must contain 24 bytes")
        header[36:60] = header_identity
    return bytes(header) + bytes(sample_header) + padded_data

def read_original_fsb_header(
    data_root: Path,
    asset: SerializedAsset,
    layout: AudioLayout,
) -> bytes:
    resource_path = resolve_audio_resource(data_root, asset, layout.source)
    with resource_path.open("rb") as handle:
        handle.seek(layout.resource_offset)
        header = handle.read(FSB5_HEADER_SIZE)
    if len(header) != FSB5_HEADER_SIZE or not header.startswith(b"FSB5"):
        raise AudioRepackError(
            f"original AudioClip resource isnt FSB5: {layout.source}"
        )
    return header

def resolve_audio_resource(
    data_root: Path,
    asset: SerializedAsset,
    source: str,
) -> Path:
    normalized = source.replace("\\", "/")
    direct = (data_root / normalized).resolve()
    if direct.is_file():
        return direct
    beside_asset = (asset.path.parent / normalized).resolve()
    if beside_asset.is_file():
        return beside_asset
    name = normalized.rsplit("/", 1)[-1]
    matches = list(data_root.rglob(name))
    if len(matches) == 1:
        return matches[0]
    raise AudioRepackError(f"AudioClip resource couldnt be resolved: {source}")

def read_ogg_packets(data: bytes) -> tuple[list[bytes], int]:
    packets = []
    pending = bytearray()
    cursor = 0
    final_granule = 0
    serial: int | None = None
    expected_sequence = 0
    while cursor < len(data):
        if cursor + 27 > len(data) or data[cursor : cursor + 4] != b"OggS":
            raise AudioRepackError(f"invalid OGG page at offset {cursor}")
        version = data[cursor + 4]
        if version != 0:
            raise AudioRepackError(f"unsupported OGG version {version}")
        flags = data[cursor + 5]
        granule = struct.unpack_from("<q", data, cursor + 6)[0]
        page_serial = struct.unpack_from("<I", data, cursor + 14)[0]
        sequence = struct.unpack_from("<I", data, cursor + 18)[0]
        segment_count = data[cursor + 26]
        table_start = cursor + 27
        body_start = table_start + segment_count
        if body_start > len(data):
            raise AudioRepackError("OGG segment table exceeds file size")
        laces = data[table_start:body_start]
        body_size = sum(laces)
        body_end = body_start + body_size
        if body_end > len(data):
            raise AudioRepackError("OGG page body exceeds file size")
        if serial is None:
            serial = page_serial
        elif page_serial != serial:
            raise AudioRepackError("chained or multiplexed OGG streams are unsupported")
        if sequence != expected_sequence:
            raise AudioRepackError(
                f"OGG page sequence jumped from {expected_sequence} to {sequence}"
            )
        expected_sequence += 1
        if bool(flags & 1) != bool(pending):
            raise AudioRepackError("OGG continuation flags dont match packet state")

        body_cursor = body_start
        for lace in laces:
            pending.extend(data[body_cursor : body_cursor + lace])
            body_cursor += lace
            if lace < 255:
                packets.append(bytes(pending))
                pending.clear()
        if granule >= 0:
            final_granule = max(final_granule, granule)
        cursor = body_end
    if pending:
        raise AudioRepackError("OGG file ends in a partial packet")
    return packets, final_granule

def load_audio_asset(data_root: Path, container: str) -> SerializedAsset:
    root = data_root.resolve()
    normalized = container.replace("\\", "/").lower()
    key = (str(root).lower(), normalized)
    asset = asset_cache.get(key)
    if asset is not None:
        return asset
    asset_path = (root / container).resolve()
    try:
        asset_path.relative_to(root)
    except ValueError as exc:
        raise AudioRepackError(
            f"AudioClip container escapes the game data folder: {container}"
        ) from exc
    if not asset_path.is_file():
        raise AudioRepackError(f"AudioClip container wasnt found: {asset_path}")
    asset = load_serialized_asset(asset_path, root)
    asset_cache[key] = asset
    return asset

def find_audio_object(asset: SerializedAsset, path_id: int) -> SerializedObject:
    for obj in asset.objects:
        if obj.path_id == path_id and obj.class_id == 83:
            return obj
    raise AudioRepackError(
        f"AudioClip path {path_id} wasnt found in {asset.relative_path}"
    )

def read_object_bytes(asset: SerializedAsset, obj: SerializedObject) -> bytes:
    with asset.path.open("rb") as handle:
        handle.seek(obj.byte_start)
        data = handle.read(obj.byte_size)
    if len(data) != obj.byte_size:
        raise AudioRepackError("AudioClip object data is truncated")
    return data

def pack_i32(buffer: bytearray, offset: int, value: int, endian: str) -> None:
    struct.pack_into(endian + "i", buffer, offset, int(value))

def pack_f32(buffer: bytearray, offset: int, value: float, endian: str) -> None:
    struct.pack_into(endian + "f", buffer, offset, float(value))

def pack_i64(buffer: bytearray, offset: int, value: int, endian: str) -> None:
    struct.pack_into(endian + "q", buffer, offset, int(value))

def align_value(value: int, alignment: int) -> int:
    return (value + alignment - 1) // alignment * alignment
