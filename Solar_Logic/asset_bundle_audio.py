from __future__ import annotations
import hashlib, struct
from io import BytesIO
from pathlib import Path
from .asset_bundle_writer import align_buffer, pack_aligned_string
from .audio_repack import (
    AudioInput,
    AudioRepackError,
    build_fsb5_pcm_from_wav,
    build_fsb5_vorbis_from_ogg,
)
from .binary import EndianReader

class NewAudioAssetError(ValueError):
    pass

def build_audio_asset(audio_path: Path, name: str) -> tuple[bytes, bytes, dict[str, object]]:
    identity = make_audio_identity(audio_path, name)
    suffix = audio_path.suffix.lower()
    try:
        if suffix == ".wav":
            audio_input = build_fsb5_pcm_from_wav(audio_path, header_identity=identity)
        elif suffix == ".ogg":
            audio_input = build_fsb5_vorbis_from_ogg(audio_path, header_identity=identity)
        else:
            raise NewAudioAssetError(f"unsupported AudioClip input {audio_path.name}")
    except AudioRepackError as exc:
        raise NewAudioAssetError(str(exc)) from exc
    payload = build_audio_clip_object(name, audio_input)
    details = {
        "sourceFormat": audio_input.source_format,
        "channels": audio_input.channels,
        "frequency": audio_input.frequency,
        "bitsPerSample": audio_input.bits_per_sample,
        "sampleCount": audio_input.sample_count,
        "compression": audio_input.compression,
    }
    return payload, audio_input.fsb, details

def build_audio_clip_object(name: str, audio_input: AudioInput) -> bytes:
    duration = (
        audio_input.sample_count / audio_input.frequency
        if audio_input.frequency > 0
        else 0.0
    )
    output = bytearray(pack_aligned_string(name))
    output.extend(
        struct.pack(
            "<iiiif",
            1,
            audio_input.channels,
            audio_input.frequency,
            audio_input.bits_per_sample,
            duration,
        )
    )
    output.extend(b"\0")
    align_buffer(output, 4)
    output.extend(struct.pack("<i", 0))
    output.extend(b"\1\0\1")
    align_buffer(output, 4)
    output.extend(pack_aligned_string(""))
    output.extend(struct.pack("<qqi", 0, len(audio_input.fsb), audio_input.compression))
    return bytes(output)

def make_audio_identity(audio_path: Path, name: str) -> bytes:
    digest = hashlib.sha256()
    digest.update(name.encode("utf-8"))
    with Path(audio_path).open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.digest()[:24]

def patch_audio_resource(
    object_payload: bytes,
    source: str,
    resource_offset: int,
    resource_size: int,
) -> bytes:
    reader = EndianReader(BytesIO(object_payload), "<")
    reader.aligned_string(max_length=4096)
    reader.i32()
    reader.i32()
    reader.i32()
    reader.i32()
    reader.f32()
    reader.bool()
    reader.align()
    reader.i32()
    reader.bool()
    reader.bool()
    reader.bool()
    reader.align()
    source_start = reader.tell()
    reader.aligned_string(max_length=4096)
    reader.i64()
    reader.i64()
    compression_start = reader.tell()
    output = bytearray(object_payload[:source_start])
    output.extend(pack_aligned_string(source))
    output.extend(struct.pack("<q", resource_offset))
    output.extend(struct.pack("<q", resource_size))
    output.extend(object_payload[compression_start:])
    return bytes(output)
