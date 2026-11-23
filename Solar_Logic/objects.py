from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from .binary import EndianReader
from .class_ids import AUDIO_COMPRESSION_EXT, NAMED_OBJECT_CLASS_IDS, class_name
from .serialized import SerializedAsset, SerializedObject

@dataclass(slots=True)
class ResourcePayload:
    kind: str
    name: str
    extension: str
    source_path: str
    source_offset: int
    source_size: int
    offset_field_offset: int | None
    size_field_offset: int | None
    details: dict

@dataclass(slots=True)
class ObjectDescription:
    name: str
    payloads: list[ResourcePayload]
    details: dict

def describe_object(asset: SerializedAsset, reader: EndianReader, obj: SerializedObject) -> ObjectDescription:
    name = read_object_name(asset, reader, obj)
    details: dict = {}
    payloads: list[ResourcePayload] = []

    try:
        if obj.class_id == 49:
            payload = read_text_asset(asset, reader, obj, name)
            if payload:
                payloads.append(payload)
        elif obj.class_id == 83:
            payload = read_audio_clip(asset, reader, obj, name)
            if payload:
                payloads.append(payload)
        elif obj.class_id in (28, 89):
            payload = read_texture2d(asset, reader, obj, name)
            if payload:
                if obj.class_id == 89:
                    payload.kind = "cubemap"
                payloads.append(payload)
                details.update(payload.details)
    except Exception as exc:
        details["payload_error"] = f"{type(exc).__name__}: {exc}"

    return ObjectDescription(name=name, payloads=payloads, details=details)

def read_object_name(asset: SerializedAsset, reader: EndianReader, obj: SerializedObject) -> str:
    try:
        if obj.class_id == 1:
            return read_game_object_name(asset, reader, obj)
        if obj.class_id == 114:
            return read_mono_behaviour_name(asset, reader, obj)
        if obj.class_id in NAMED_OBJECT_CLASS_IDS:
            reader.seek(obj.byte_start)
            return reader.aligned_string(max_length=4096)
    except Exception:
        return ""
    return ""

def read_text_asset(
    asset: SerializedAsset,
    reader: EndianReader,
    obj: SerializedObject,
    name: str,
) -> ResourcePayload | None:
    reader.seek(obj.byte_start)
    parsed_name = reader.aligned_string(max_length=4096)
    name = name or parsed_name
    script_size_field = reader.tell()
    script_size = reader.i32()
    script_offset = reader.tell()
    if script_size < 0 or script_offset + script_size > obj.byte_start + obj.byte_size:
        return None
    return ResourcePayload(
        kind="text_asset",
        name=name,
        extension=text_extension(name),
        source_path=asset.relative_path,
        source_offset=script_offset,
        source_size=script_size,
        offset_field_offset=None,
        size_field_offset=script_size_field,
        details={"inline": True},
    )

def read_audio_clip(
    asset: SerializedAsset,
    reader: EndianReader,
    obj: SerializedObject,
    name: str,
) -> ResourcePayload | None:
    reader.seek(obj.byte_start)
    parsed_name = reader.aligned_string(max_length=4096)
    name = name or parsed_name
    version = unity_version_tuple(asset.unity_version)
    if version[0] < 5:
        return None

    reader.i32()
    channels = reader.i32()
    frequency = reader.i32()
    bits_per_sample = reader.i32()
    length = reader.f32()
    reader.bool()
    reader.align()
    reader.i32()
    reader.bool()
    reader.bool()
    reader.bool()
    reader.align()

    source = reader.aligned_string(max_length=4096)
    offset_field = reader.tell()
    source_offset = reader.i64()
    size_field = reader.tell()
    source_size = reader.i64()
    compression = reader.i32()

    if source_size <= 0:
        return None

    if source:
        source_path = resource_name(source)
    else:
        source_path = asset.relative_path
        source_offset = reader.tell()

    return ResourcePayload(
        kind="audio_clip",
        name=name,
        extension=AUDIO_COMPRESSION_EXT.get(compression, ".audiodata"),
        source_path=source_path,
        source_offset=source_offset,
        source_size=source_size,
        offset_field_offset=offset_field if source else None,
        size_field_offset=size_field if source else None,
        details={
            "channels": channels,
            "frequency": frequency,
            "bits_per_sample": bits_per_sample,
            "length": length,
            "compression": compression,
            "inline": not bool(source),
        },
    )

def read_texture2d(
    asset: SerializedAsset,
    reader: EndianReader,
    obj: SerializedObject,
    name: str,
) -> ResourcePayload | None:
    reader.seek(obj.byte_start)
    parsed_name = reader.aligned_string(max_length=4096)
    name = name or parsed_name
    version = unity_version_tuple(asset.unity_version)

    width = reader.i32()
    height = reader.i32()
    complete_image_size = reader.i32()
    texture_format = reader.i32()
    if version[0] < 5 or (version[0] == 5 and version[1] < 2):
        reader.bool()
    else:
        mip_count = reader.i32()
    if version[0] > 2 or (version[0] == 2 and version[1] >= 6):
        reader.bool()
    if version[0] >= 3 and (version[0] < 5 or (version[0] == 5 and version[1] <= 4)):
        reader.bool()
    reader.align()
    image_count = reader.i32()
    reader.i32()
    reader.i32()
    reader.i32()
    reader.f32()
    reader.i32()
    if version[0] >= 3:
        reader.i32()
    if version[0] > 3 or (version[0] == 3 and version[1] >= 5):
        reader.i32()

    image_size_field = reader.tell()
    image_size = reader.i32()
    details = {
        "width": width,
        "height": height,
        "texture_format": texture_format,
        "complete_image_size": complete_image_size,
        "image_count": image_count,
        "image_size_field_offset": image_size_field,
    }

    if image_size == 0 and ((version[0] == 5 and version[1] >= 3) or version[0] > 5):
        offset_field = reader.tell()
        source_offset = reader.u32()
        size_field = reader.tell()
        source_size = reader.u32()
        source = reader.aligned_string(max_length=4096)
        if source_size <= 0 or not source:
            return None
        return ResourcePayload(
            kind="texture2d",
            name=name,
            extension=".texdata",
            source_path=resource_name(source),
            source_offset=source_offset,
            source_size=source_size,
            offset_field_offset=offset_field,
            size_field_offset=size_field,
            details={**details, "inline": False},
        )

    if image_size > 0:
        return ResourcePayload(
            kind="texture2d",
            name=name,
            extension=".texdata",
            source_path=asset.relative_path,
            source_offset=reader.tell(),
            source_size=image_size,
            offset_field_offset=None,
            size_field_offset=image_size_field,
            details={**details, "inline": True},
        )
    return None

def read_game_object_name(asset: SerializedAsset, reader: EndianReader, obj: SerializedObject) -> str:
    reader.seek(obj.byte_start)
    component_count = reader.i32()
    if component_count < 0 or component_count > 100000:
        return ""
    for _ in range(component_count):
        version = unity_version_tuple(asset.unity_version)
        if (version[0] == 5 and version[1] < 5) or version[0] < 5:
            reader.i32()
        skip_pptr(reader, asset)
    reader.i32()
    return reader.aligned_string(max_length=4096)

def read_mono_behaviour_name(asset: SerializedAsset, reader: EndianReader, obj: SerializedObject) -> str:
    game_object, script, name = read_mono_behaviour_base(asset, reader, obj)
    return name

def read_mono_behaviour_base(
    asset: SerializedAsset,
    reader: EndianReader,
    obj: SerializedObject,
) -> tuple[dict[str, int], dict[str, int], str]:
    reader.seek(obj.byte_start)
    game_object = read_pptr(reader, asset)
    reader.u8()
    reader.align()
    script = read_pptr(reader, asset)
    name = reader.aligned_string(max_length=4096)
    return game_object, script, name

def build_game_object_name_index(assets: list[SerializedAsset]) -> dict[tuple[str, int], str]:
    names: dict[tuple[str, int], str] = {}
    for asset in assets:
        game_objects = [obj for obj in asset.objects if obj.class_id == 1]
        if not game_objects:
            continue
        with asset.path.open("rb") as handle:
            reader = EndianReader(handle, asset.endian)
            for obj in game_objects:
                name = read_game_object_name(asset, reader, obj)
                if name:
                    names[(asset.relative_path.replace("\\", "/").lower(), obj.path_id)] = name
    return names

def read_pptr(reader: EndianReader, asset: SerializedAsset) -> dict[str, int]:
    file_id = reader.i32()
    if asset.format_version < 14:
        path_id = reader.i32()
    else:
        path_id = reader.i64()
    return {"file_id": file_id, "path_id": path_id}

def skip_pptr(reader: EndianReader, asset: SerializedAsset) -> None:
    reader.i32()
    if asset.format_version < 14:
        reader.i32()
    else:
        reader.i64()

def resource_name(source: str) -> str:
    source = source.replace("\\", "/")
    return Path(source.split("/")[-1]).name

def text_extension(name: str) -> str:
    suffix = Path(name).suffix
    if suffix and len(suffix) <= 12:
        return suffix
    return ".bytes"

def unity_version_tuple(version: str) -> tuple[int, int, int, int]:
    nums: list[int] = []
    current = ""
    for char in version:
        if char.isdigit():
            current += char
        elif current:
            nums.append(int(current))
            current = ""
    if current:
        nums.append(int(current))
    nums.extend([0] * (4 - len(nums)))
    return tuple(nums[:4])
