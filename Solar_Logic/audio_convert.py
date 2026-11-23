from __future__ import annotations
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from .fmod_vorbis import FmodVorbisError, rebuild_fsb5_vorbis_ogg

class AudioDecodeError(ValueError):
    pass

@dataclass(frozen=True, slots=True)
class Fsb5Header:
    version: int
    sample_count: int
    sample_header_size: int
    name_table_size: int
    data_size: int
    audio_type: int
    flags: int
    data_offset: int

FSB5_HEADER_SIZE = 60
FSB5_PCM_AUDIO_TYPES = {1: 8, 2: 16, 3: 24, 4: 32}
FSB5_VORBIS_AUDIO_TYPE = 15

def audio_view_extension(data: bytes, details: dict[str, Any]) -> str:
    if data.startswith(b"FSB5"):
        header = parse_fsb5_header(data)
        compression = int(details.get("compression", -1))
        if header.audio_type == FSB5_VORBIS_AUDIO_TYPE or compression == 1:
            return ".ogg"
        if header.audio_type in FSB5_PCM_AUDIO_TYPES and compression == 0:
            return ".wav"
        raise AudioDecodeError(f"unsupported FSB5 audio type {header.audio_type} compression {compression}")
    if data.startswith(b"RIFF") and data[8:12] == b"WAVE":
        return ".wav"
    raise AudioDecodeError(f"unsupported audio payload magic {data[:4]!r}")

def save_audio_view(data: bytes, details: dict[str, Any], out_path: Path) -> dict[str, Any]:
    if data.startswith(b"FSB5"):
        return save_fsb5_audio(data, details, out_path)
    if data.startswith(b"RIFF") and data[8:12] == b"WAVE":
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(data)
        return {"format": "wav_passthrough", "source_container_format": "RIFF/WAVE"}
    raise AudioDecodeError(f"unsupported audio payload magic {data[:4]!r}")

def parse_fsb5_header(data: bytes) -> Fsb5Header:
    if len(data) < FSB5_HEADER_SIZE:
        raise AudioDecodeError(f"FSB5 payload is too small: {len(data)} bytes")
    magic, version, sample_count, sample_header_size, name_table_size, data_size, audio_type, flags = struct.unpack_from(
        "<4sIIIIIII", data, 0
    )
    if magic != b"FSB5":
        raise AudioDecodeError(f"not an FSB5 payload: {magic!r}")
    if sample_count != 1:
        raise AudioDecodeError(f"unsupported FSB5 sample count {sample_count}")
    header_size = 0x40 if version == 0 else FSB5_HEADER_SIZE
    data_offset = header_size + sample_header_size + name_table_size
    if data_offset > len(data):
        raise AudioDecodeError(f"FSB5 data offset {data_offset} exceeds payload size {len(data)}")
    return Fsb5Header(
        version=version,
        sample_count=sample_count,
        sample_header_size=sample_header_size,
        name_table_size=name_table_size,
        data_size=data_size,
        audio_type=audio_type,
        flags=flags,
        data_offset=data_offset,
    )

def save_fsb5_audio(data: bytes, details: dict[str, Any], out_path: Path) -> dict[str, Any]:
    header = parse_fsb5_header(data)
    compression = int(details.get("compression", -1))
    if header.audio_type == FSB5_VORBIS_AUDIO_TYPE or compression == 1:
        try:
            ogg, info = rebuild_fsb5_vorbis_ogg(data)
        except FmodVorbisError as exc:
            raise AudioDecodeError(str(exc)) from exc
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(ogg)
        return {
            "format": "ogg_vorbis_from_fsb5",
            "source_container_format": "FSB5",
            "fsb_version": header.version,
            "fsb_audio_type": header.audio_type,
            "fsb_flags": header.flags,
            "channels": info.channels,
            "frequency": info.frequency,
            "sample_count": info.sample_count,
            "vorbis_crc32": f"0x{info.crc32:08x}",
            "packet_count": info.packet_count,
            "audio_data_offset": info.audio_data_offset,
            "audio_data_size": info.audio_data_size,
            "trimmed_padding_bytes": info.padding_bytes,
        }
    if header.audio_type not in FSB5_PCM_AUDIO_TYPES or compression != 0:
        raise AudioDecodeError(f"unsupported FSB5 audio type {header.audio_type} compression {compression}")

    channels = int(details.get("channels", 0))
    frequency = int(details.get("frequency", 0))
    bits_per_sample = FSB5_PCM_AUDIO_TYPES[header.audio_type]
    if channels <= 0 or frequency <= 0 or bits_per_sample <= 0 or bits_per_sample % 8:
        raise AudioDecodeError(
            f"invalid PCM details channels={channels} frequency={frequency} bits={bits_per_sample}"
        )

    available = max(0, min(header.data_size, len(data) - header.data_offset))
    block_align = channels * (bits_per_sample // 8)
    pcm_size = pcm_size_from_clip_length(details, available, block_align)
    pcm = data[header.data_offset : header.data_offset + pcm_size]
    write_pcm_wav(out_path, pcm, channels, frequency, bits_per_sample)
    return {
        "format": "wav_pcm_from_fsb5",
        "source_container_format": "FSB5",
        "fsb_version": header.version,
        "fsb_audio_type": header.audio_type,
        "fsb_flags": header.flags,
        "channels": channels,
        "frequency": frequency,
        "bits_per_sample": bits_per_sample,
        "pcm_data_offset": header.data_offset,
        "pcm_data_size": pcm_size,
        "fsb_data_size": header.data_size,
        "trimmed_padding_bytes": available - pcm_size,
    }

def pcm_size_from_clip_length(details: dict[str, Any], available: int, block_align: int) -> int:
    if available <= 0:
        return 0
    length = float(details.get("length", 0.0) or 0.0)
    frequency = int(details.get("frequency", 0) or 0)
    channels = int(details.get("channels", 0) or 0)
    bits_per_sample = int(details.get("bits_per_sample", 0) or 0)
    expected = 0
    if length > 0 and frequency > 0 and channels > 0 and bits_per_sample > 0:
        frames = max(0, round(length * frequency))
        expected = frames * block_align
    if expected <= 0 or expected > available:
        expected = available
    if block_align > 0:
        expected -= expected % block_align
    return expected

def write_pcm_wav(out_path: Path, pcm: bytes, channels: int, frequency: int, bits_per_sample: int) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    data_size = len(pcm)
    block_align = channels * (bits_per_sample // 8)
    byte_rate = frequency * block_align
    riff_size = 36 + data_size
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        riff_size,
        b"WAVE",
        b"fmt ",
        16,
        1,
        channels,
        frequency,
        byte_rate,
        block_align,
        bits_per_sample,
        b"data",
        data_size,
    )
    with out_path.open("wb") as handle:
        handle.write(header)
        handle.write(pcm)
