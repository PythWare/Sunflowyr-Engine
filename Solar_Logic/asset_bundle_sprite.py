from __future__ import annotations
import json, struct
from pathlib import Path
from typing import Any
from .asset_bundle_material import BundlePointerRef, NULL_POINTER, clean_name
from .asset_bundle_writer import align_buffer, pack_aligned_string

SPRITE_FORMAT_MARKER = "sunflowyr-new-sprite"

class NewSpriteAssetError(ValueError):
    pass

def try_read_sprite_document(json_path: Path) -> dict[str, Any] | None:
    forced = json_path.name.lower().endswith(".sprite.json")
    try:
        document = json.loads(Path(json_path).read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        if forced:
            raise NewSpriteAssetError(f"couldnt read Sprite JSON: {exc}") from exc
        return None
    if not isinstance(document, dict):
        if forced:
            raise NewSpriteAssetError("Sprite JSON root must be an object")
        return None
    if document.get("format") != SPRITE_FORMAT_MARKER:
        if forced:
            raise NewSpriteAssetError(f'Sprite JSON must set "format": "{SPRITE_FORMAT_MARKER}"')
        return None
    return document

def build_sprite_asset(
    document: dict[str, Any],
    json_path: Path,
    fallback_name: str,
    texture_width: int,
    texture_height: int,
) -> tuple[bytes, str, dict[str, object], tuple[BundlePointerRef, ...]]:
    name = clean_name(document.get("name", fallback_name), fallback_name)
    texture_file = str(document.get("texture") or "")
    if not texture_file:
        raise NewSpriteAssetError('Sprite JSON needs "texture" naming a staged image input')

    rect = read_rect(document.get("rect"), texture_width, texture_height)
    pivot = read_pair(document.get("pivot"), (0.5, 0.5), "pivot")
    border = read_quad(document.get("border"), (0.0, 0.0, 0.0, 0.0), "border")
    pixels_to_units = float(document.get("pixelsToUnits", 100.0))
    if pixels_to_units <= 0:
        raise NewSpriteAssetError("pixelsToUnits must be greater than zero")
    extrude = int(document.get("extrude", 1))
    is_polygon = bool(document.get("isPolygon", False))

    output = bytearray(pack_aligned_string(name))
    output.extend(struct.pack("<ffff", *rect))
    output.extend(struct.pack("<ff", 0.0, 0.0))
    output.extend(struct.pack("<ffff", *border))
    output.extend(struct.pack("<f", pixels_to_units))
    output.extend(struct.pack("<ff", *pivot))
    output.extend(struct.pack("<I", extrude & 0xFFFFFFFF))
    output.extend(struct.pack("<B", 1 if is_polygon else 0))
    align_buffer(output, 4)

    texture_ref = BundlePointerRef(
        payload_offset=len(output),
        source_name=texture_file,
        slot="texture",
    )
    output.extend(NULL_POINTER)
    output.extend(NULL_POINTER)
    vertices, indices = full_rect_quad(rect, pivot, pixels_to_units)
    output.extend(struct.pack("<i", len(vertices)))
    for position in vertices:
        output.extend(struct.pack("<fff", *position))
    output.extend(struct.pack("<i", len(indices)))
    for index in indices:
        output.extend(struct.pack("<H", index))
    align_buffer(output, 4)
    output.extend(struct.pack("<ffff", *rect))
    output.extend(struct.pack("<ff", 0.0, 0.0))
    output.extend(struct.pack("<I", 0))
    output.extend(
        struct.pack(
            "<ffff",
            pixels_to_units,
            pivot[0] * rect[2],
            pixels_to_units,
            pivot[1] * rect[3],
        )
    )

    details: dict[str, object] = {
        "texture": texture_file,
        "rect": list(rect),
        "pivot": list(pivot),
        "border": list(border),
        "pixelsToUnits": pixels_to_units,
        "meshType": "FullRect",
        "vertices": len(vertices),
        "indices": len(indices),
    }
    return bytes(output), name, details, (texture_ref,)

def full_rect_quad(
    rect: tuple[float, float, float, float],
    pivot: tuple[float, float],
    pixels_to_units: float,
) -> tuple[list[tuple[float, float, float]], list[int]]:
    left = -pivot[0] * rect[2] / pixels_to_units
    right = (1.0 - pivot[0]) * rect[2] / pixels_to_units
    bottom = -pivot[1] * rect[3] / pixels_to_units
    top = (1.0 - pivot[1]) * rect[3] / pixels_to_units
    corners = [
        (left, top, 0.0),
        (right, bottom, 0.0),
        (right, top, 0.0),
        (left, bottom, 0.0),
    ]
    return corners, [3, 0, 1, 2, 1, 0]

def read_rect(value: Any, texture_width: int, texture_height: int) -> tuple[float, float, float, float]:
    if value is None:
        return 0.0, 0.0, float(texture_width), float(texture_height)
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise NewSpriteAssetError("rect must be [x, y, width, height]")
    x, y, width, height = (float(item) for item in value)
    if width <= 0 or height <= 0:
        raise NewSpriteAssetError("rect width/height must be greater than zero")
    if x < 0 or y < 0 or x + width > texture_width or y + height > texture_height:
        raise NewSpriteAssetError(
            f"rect {value} doesnt fit the {texture_width}x{texture_height} texture"
        )
    return x, y, width, height

def read_pair(value: Any, default: tuple[float, float], label: str) -> tuple[float, float]:
    if value is None:
        return default
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise NewSpriteAssetError(f"{label} must be a two value list")
    return float(value[0]), float(value[1])

def read_quad(
    value: Any,
    default: tuple[float, float, float, float],
    label: str,
) -> tuple[float, float, float, float]:
    if value is None:
        return default
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise NewSpriteAssetError(f"{label} must be a four-value list")
    return float(value[0]), float(value[1]), float(value[2]), float(value[3])
