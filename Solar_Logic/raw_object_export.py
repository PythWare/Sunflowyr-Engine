from __future__ import annotations
import io, struct
from dataclasses import dataclass
from pathlib import Path
from .binary import EndianReader, read_range
from .mono_repack import EndianWriter
from .objects import ResourcePayload
from .serialized import SerializedAsset, SerializedObject
from .texture_repack import read_texture_layout

class RawObjectExportError(ValueError):
    pass

@dataclass(frozen=True, slots=True)
class CompleteRawObject:
    data: bytes
    inlined_resource_bytes: int
    resource_kind: str | None

def build_complete_raw_object(
    asset: SerializedAsset,
    obj: SerializedObject,
    payloads: list[ResourcePayload],
    original: bytes | None = None,
) -> CompleteRawObject:
    original_data = original if original is not None else read_object_bytes(asset, obj)
    if len(original_data) != obj.byte_size:
        raise RawObjectExportError(f"object path {obj.path_id} is truncated")
    for payload in payloads:
        if bool(payload.details.get("inline")):
            continue
        if payload.kind not in {"texture2d", "cubemap", "audio_clip"}:
            continue
        resource_path = resolve_raw_resource(asset, payload)
        resource_data = b"".join(
            read_range(resource_path, payload.source_offset, payload.source_size)
        )
        if len(resource_data) != payload.source_size:
            raise RawObjectExportError(
                f"resource payload for path {obj.path_id} is truncated"
            )
        if payload.kind in {"texture2d", "cubemap"}:
            complete = inline_texture_resource(original_data, asset, resource_data)
        else:
            complete = inline_audio_resource(original_data, asset, resource_data)
        return CompleteRawObject(complete, len(resource_data), payload.kind)
    return CompleteRawObject(original_data, 0, None)

def read_object_bytes(asset: SerializedAsset, obj: SerializedObject) -> bytes:
    with asset.path.open("rb") as handle:
        handle.seek(obj.byte_start)
        data = handle.read(obj.byte_size)
    if len(data) != obj.byte_size:
        raise RawObjectExportError(f"object path {obj.path_id} is truncated")
    return data

def resolve_raw_resource(asset: SerializedAsset, payload: ResourcePayload) -> Path:
    if payload.source_path == asset.relative_path:
        return asset.path
    candidate = asset.path.parent / payload.source_path
    if candidate.is_file():
        return candidate
    matches = list(asset.path.parent.rglob(Path(payload.source_path).name))
    if matches:
        return matches[0]
    raise RawObjectExportError(
        f"resource for path {payload.name} wasnt found: {payload.source_path}"
    )

def inline_texture_resource(
    original: bytes,
    asset: SerializedAsset,
    resource_data: bytes,
) -> bytes:
    layout = read_texture_layout(original, asset)
    image_size_offset = int(layout["image_size_offset"])
    prefix_end = image_size_offset + 4
    rebuilt = bytearray(original[:prefix_end])
    struct.pack_into(asset.endian + "i", rebuilt, image_size_offset, len(resource_data))
    rebuilt.extend(resource_data)
    padding = (-len(rebuilt)) % 4
    if padding:
        rebuilt.extend(b"\0" * padding)
    rebuilt.extend(struct.pack(asset.endian + "IIi", 0, 0, 0))
    rebuilt.extend(original[int(layout["trailing_offset"]):])
    return bytes(rebuilt)

def inline_audio_resource(
    original: bytes,
    asset: SerializedAsset,
    resource_data: bytes,
) -> bytes:
    reader = EndianReader(io.BytesIO(original), asset.endian)
    reader.aligned_string(max_length=4096)
    for field_index in range(4):
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
    source = reader.aligned_string(max_length=4096)
    reader.i64()
    reader.i64()
    compression = reader.i32()
    if not source:
        return original
    writer = EndianWriter(asset.endian)
    writer.write(original[:source_start])
    writer.aligned_string("")
    writer.i64(0)
    writer.i64(len(resource_data))
    writer.i32(compression)
    writer.write(resource_data)
    return writer.bytes()
