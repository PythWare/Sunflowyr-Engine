from __future__ import annotations
from typing import Any
from .model_context import ModelContextIndex, PPtr
from .serialized import SerializedAsset
from .unity_typetree_codec import load_schema, read_typetree, write_typetree

PARTICLE_SYSTEM_SCHEMA = "particle_system_schema.json"

def parse_particle_system(
    data: bytes,
    asset: SerializedAsset,
    context: ModelContextIndex,
) -> dict[str, Any]:
    fields = read_typetree(data, load_schema(PARTICLE_SYSTEM_SCHEMA), asset.endian)
    decorate_pointers(fields, asset, context)
    decorate_packed_colors(fields)
    return fields

def build_particle_system(fields: dict[str, Any], asset: SerializedAsset) -> bytes:
    resolve_packed_colors(fields)
    return write_typetree(fields, load_schema(PARTICLE_SYSTEM_SCHEMA), asset.endian)

def pack_color32(red: int, green: int, blue: int, alpha: int = 255) -> int:
    channels = (red, green, blue, alpha)
    if any(channel < 0 or channel > 255 for channel in channels):
        raise ValueError("Color32 channels must be between 0 and 255")
    return red | (green << 8) | (blue << 16) | (alpha << 24)

def unpack_color32(value: int) -> dict[str, int]:
    packed = int(value) & 0xFFFFFFFF
    return {
        "r": packed & 0xFF,
        "g": (packed >> 8) & 0xFF,
        "b": (packed >> 16) & 0xFF,
        "a": (packed >> 24) & 0xFF,
    }

def decorate_packed_colors(value: Any) -> None:
    if isinstance(value, list):
        for item in value:
            decorate_packed_colors(item)
        return
    if not isinstance(value, dict):
        return
    packed_value = value.get("rgba")
    if isinstance(packed_value, int):
        channels = unpack_color32(packed_value)
        value["rgbaChannels"] = channels
        value["rgbaHex"] = (
            f"#{channels['r']:02X}{channels['g']:02X}"
            f"{channels['b']:02X}{channels['a']:02X}"
        )
    for item in value.values():
        decorate_packed_colors(item)

def resolve_packed_colors(value: Any) -> None:
    if isinstance(value, list):
        for item in value:
            resolve_packed_colors(item)
        return
    if not isinstance(value, dict):
        return
    packed = value.get("rgba")
    if isinstance(packed, int):
        value["rgba"] = resolved_color(value, packed)
    for item in value.values():
        resolve_packed_colors(item)

def resolved_color(value: dict[str, Any], packed: int) -> int:
    from_hex = color_from_hex(value.get("rgbaHex"))
    from_channels = color_from_channels(value.get("rgbaChannels"))
    edited = {item for item in (from_hex, from_channels) if item is not None and item != packed}
    if not edited:
        return packed
    if len(edited) > 1:
        raise ValueError(
            "rgbaHex %r and rgbaChannels %r are different colours. edit one of them, not both"
            % (value.get("rgbaHex"), value.get("rgbaChannels"))
        )
    return edited.pop()

def color_from_hex(text: Any) -> int | None:
    if not isinstance(text, str):
        return None
    digits = text.strip().lstrip("#")
    if len(digits) not in (6, 8):
        return None
    try:
        channels = [int(digits[at : at + 2], 16) for at in range(0, len(digits), 2)]
    except ValueError:
        return None
    if len(channels) == 3:
        channels.append(255)
    return pack_color32(*channels)

def color_from_channels(channels: Any) -> int | None:
    if not isinstance(channels, dict):
        return None
    try:
        values = [int(channels[name]) for name in ("r", "g", "b")]
        values.append(int(channels.get("a", 255)))
    except (KeyError, TypeError, ValueError):
        return None
    if any(item < 0 or item > 255 for item in values):
        return None
    return pack_color32(*values)

def decorate_pointers(value: Any, asset: SerializedAsset, context: ModelContextIndex) -> None:
    if isinstance(value, list):
        for item in value:
            decorate_pointers(item, asset, context)
        return
    if not isinstance(value, dict):
        return
    if "m_FileID" in value and "m_PathID" in value:
        file_id = int(value.get("m_FileID", 0))
        path_id = int(value.get("m_PathID", 0))
        pointer = PPtr(file_id, path_id)
        asset_key = asset.relative_path.replace("\\", "/").lower()
        resolved_key = context.resolve(asset_key, pointer)
        target = context.objects.get(resolved_key) if resolved_key is not None else None
        value["resolved"] = target is not None
        if target is not None and resolved_key is not None:
            name = ""
            if resolved_key in context.game_objects:
                name = context.game_objects[resolved_key].name
            elif resolved_key in context.materials:
                name = context.materials[resolved_key].name
            elif resolved_key in context.texture_names:
                name = context.texture_names[resolved_key]
            elif resolved_key in context.object_names:
                name = context.object_names[resolved_key]
            value.update(
                {
                    "container": resolved_key[0],
                    "resolvedPathId": resolved_key[1],
                    "classId": target.class_id,
                    "className": target.type_name,
                    "name": name,
                }
            )
        return
    for item in value.values():
        decorate_pointers(item, asset, context)
