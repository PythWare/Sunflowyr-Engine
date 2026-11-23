from __future__ import annotations
import json, math
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from .binary import EndianReader, read_range
from .mono_repack import EndianWriter, taildata_value, unity_version_tuple, write_pptr
from .objects import read_texture2d
from .serialized import SerializedAsset, SerializedObject, load_serialized_asset
from .sprite_export import SpriteData, read_sprite
from .texture_convert import decode_texture
from .texture_repack import rebuild_texture_rgba_layers

class SpriteRepackError(ValueError):
    pass

asset_cache: dict[tuple[str, str], SerializedAsset] = {}

@dataclass(slots=True)
class SpritePngEdit:
    png_path: Path
    sprite_name: str
    texture_asset: SerializedAsset
    texture_object: SerializedObject
    sprite: SpriteData

    @property
    def target_key(self) -> tuple[str, int]:
        return (self.texture_asset.relative_path.lower(), self.texture_object.path_id)

def repack_sprite_json(
    json_path: Path,
    taildata: dict[str, Any],
    data_root: Path,
) -> bytes:
    class_id = int(taildata_value(taildata, "classId", 0) or 0)
    if class_id != 213:
        raise SpriteRepackError(f"sprite JSON taildata expected class 213, found {class_id}")
    container = str(taildata_value(taildata, "container") or "")
    if not container:
        raise SpriteRepackError("sprite JSON taildata is missing its Unity container")
    document = json.loads(Path(json_path).read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise SpriteRepackError("Sprite JSON root must be an object")

    asset = load_asset(Path(data_root), container)
    version = unity_version_tuple(asset.unity_version)
    if version >= (5, 6, 0, 0):
        raise SpriteRepackError("Unity 5.6+ Sprite layout isnt supported by this writer")

    writer = EndianWriter(asset.endian)
    writer.aligned_string(document.get("m_Name", ""))
    write_rect(writer, document.get("m_Rect"), "m_Rect")
    write_vector(writer, document.get("m_Offset"), 2, "m_Offset")
    if version >= (4, 5, 0, 0):
        write_vector(writer, document.get("m_Border"), 4, "m_Border")
    writer.f32(document.get("m_PixelsToUnits", 100.0))
    if has_541p3_pivot(version):
        write_vector(writer, document.get("m_Pivot"), 2, "m_Pivot")
    writer.u32(int(document.get("m_Extrude", 0)))
    if version >= (5, 3, 0, 0):
        writer.bool(document.get("m_IsPolygon", False))
        writer.align()

    render_data = document.get("m_RD")
    if not isinstance(render_data, dict):
        raise SpriteRepackError("Sprite JSON is missing m_RD")
    write_pptr(writer, asset, require_dict(render_data.get("texture"), "m_RD.texture"))
    if version >= (5, 2, 0, 0):
        write_pptr(writer, asset, optional_dict(render_data.get("alphaTexture")))

    vertices = require_list(render_data.get("vertices"), "m_RD.vertices")
    writer.i32(len(vertices))
    for index, vertex in enumerate(vertices):
        value = require_dict(vertex, f"m_RD.vertices[{index}]")
        write_vector(writer, value.get("pos"), 3, f"m_RD.vertices[{index}].pos")
        if version < (4, 4, 0, 0):
            write_vector(writer, value.get("uv"), 2, f"m_RD.vertices[{index}].uv")

    indices = require_list(render_data.get("indices"), "m_RD.indices")
    writer.i32(len(indices))
    for index in indices:
        writer.u16(int(index))
    writer.align()
    write_rect(writer, render_data.get("textureRect"), "m_RD.textureRect")
    write_vector(writer, render_data.get("textureRectOffset"), 2, "m_RD.textureRectOffset")
    writer.u32(sprite_settings_raw(render_data.get("settingsRaw")))
    if version >= (4, 5, 0, 0):
        write_vector(writer, render_data.get("uvTransform"), 4, "m_RD.uvTransform")
    return writer.bytes()

def prepare_sprite_png(
    png_path: Path,
    taildata: dict[str, Any],
    data_root: Path,
) -> SpritePngEdit:
    class_id = int(taildata_value(taildata, "classId", 0) or 0)
    if class_id != 213:
        raise SpriteRepackError(f"sprite PNG taildata expected class 213, found {class_id}")
    container = str(taildata_value(taildata, "container") or "")
    path_id = int(taildata_value(taildata, "pathId", 0) or 0)
    if not container or not path_id:
        raise SpriteRepackError("sprite PNG taildata is missing container or pathId")
    sprite_asset = load_asset(Path(data_root), container)
    sprite_object = find_object(sprite_asset, path_id, 213)
    with sprite_asset.path.open("rb") as handle:
        sprite = read_sprite(sprite_asset, EndianReader(handle, sprite_asset.endian), sprite_object)
    texture_asset, texture_object = resolve_texture_target(
        Path(data_root),
        sprite_asset,
        sprite.render_data.texture.file_id,
        sprite.render_data.texture.path_id,
    )
    return SpritePngEdit(
        png_path=Path(png_path),
        sprite_name=sprite.name,
        texture_asset=texture_asset,
        texture_object=texture_object,
        sprite=sprite,
    )

def repack_sprite_png_group(edits: list[SpritePngEdit]) -> tuple[bytes, dict[str, Any]]:
    if not edits:
        raise SpriteRepackError("sprite PNG group is empty")
    first = edits[0]
    if any(edit.target_key != first.target_key for edit in edits):
        raise SpriteRepackError("sprite PNG group contains more than one texture target")
    image = load_texture_image(first.texture_asset, first.texture_object)
    for edit in edits:
        apply_sprite_edit(image, edit)
    payload = rebuild_texture_rgba_layers(
        first.texture_asset,
        first.texture_object,
        [image.tobytes()],
    )
    return payload, texture_target_record(
        first.texture_asset,
        first.texture_object,
        first.sprite_name,
    )

def apply_sprite_edit(image: Any, edit: SpritePngEdit) -> None:
    try:
        from PIL import Image
    except ImportError as exc:
        raise SpriteRepackError("Pillow is required for Sprite PNG write-back") from exc
    rect = edit.sprite.render_data.texture_rect
    left = max(0, int(math.floor(rect["x"])))
    top = max(0, int(math.floor(rect["y"])))
    right = min(image.width, int(math.ceil(rect["x"] + rect["width"])))
    bottom = min(image.height, int(math.ceil(rect["y"] + rect["height"])))
    if right <= left or bottom <= top:
        raise SpriteRepackError(f"sprite texture rect is empty: {rect}")
    with Image.open(edit.png_path) as source:
        replacement = source.convert("RGBA")
    replacement = replacement.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
    rotation = edit.sprite.render_data.settings.packing_rotation
    if edit.sprite.render_data.settings.packed:
        if rotation == "FlipHorizontal":
            replacement = replacement.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
        elif rotation == "FlipVertical":
            replacement = replacement.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
        elif rotation == "Rotate180":
            replacement = replacement.rotate(180, expand=True)
        elif rotation == "Rotate90":
            replacement = replacement.rotate(90, expand=True)
    expected = (right - left, bottom - top)
    if replacement.size != expected:
        raise SpriteRepackError(
            f"{edit.png_path.name} must remain {expected[0]}x{expected[1]} after unpacking "
            f"its atlas rotation, found {replacement.width}x{replacement.height}"
        )
    check_tight_mask_loss(image, edit, left, top, right, bottom)
    image.paste(replacement, (left, top))

def check_tight_mask_loss(image: Any, edit: SpritePngEdit, left: int, top: int,
                          right: int, bottom: int) -> None:
    sprite = edit.sprite
    if sprite.render_data.settings.packing_mode != "Tight":
        return
    try:
        from PIL import Image, ImageChops

        from .sprite_export import tight_mesh_mask

        region = image.crop((left, top, right, bottom)).convert("RGBA")
        if sprite.render_data.settings.packed:
            rotation = sprite.render_data.settings.packing_rotation
            if rotation == "FlipHorizontal":
                region = region.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
            elif rotation == "FlipVertical":
                region = region.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
            elif rotation == "Rotate180":
                region = region.rotate(180, expand=True)
            elif rotation == "Rotate90":
                region = region.rotate(270, expand=True)
        mask = tight_mesh_mask(region.size, sprite)
        if mask is None:
            return
        alpha = region.split()[3]
        outside = Image.eval(mask, lambda value: 255 if value == 0 else 0)
        bounds = ImageChops.multiply(alpha, outside).getbbox()
    except Exception:
        return
    if bounds is None:
        return
    raise SpriteRepackError(
        f"{edit.png_path.name}: this sprite shares atlas pixels around {bounds}, edit the whole atlas PNG instead"
    )

def load_texture_image(asset: SerializedAsset, obj: SerializedObject) -> Any:
    try:
        from PIL import Image
    except ImportError as exc:
        raise SpriteRepackError("Pillow is required for Sprite PNG writeback") from exc
    with asset.path.open("rb") as handle:
        payload = read_texture2d(asset, EndianReader(handle, asset.endian), obj, "")
    if payload is None:
        raise SpriteRepackError(f"Texture2D path {obj.path_id} has no readable payload")
    if int(payload.details.get("image_count", 1)) != 1:
        raise SpriteRepackError("Sprite PNG write-back currently requires a single-layer Texture2D")
    resource_path = resolve_resource(asset, payload.source_path)
    texture_bytes = b"".join(read_range(resource_path, payload.source_offset, payload.source_size))
    decoded = decode_texture(
        texture_bytes,
        int(payload.details["width"]),
        int(payload.details["height"]),
        int(payload.details["texture_format"]),
    )
    return Image.frombytes("RGBA", (decoded.width, decoded.height), decoded.rgba)

def resolve_texture_target(
    data_root: Path,
    sprite_asset: SerializedAsset,
    file_id: int,
    path_id: int,
) -> tuple[SerializedAsset, SerializedObject]:
    if path_id == 0:
        raise SpriteRepackError("sprite texture pointer is null")
    if file_id == 0:
        texture_asset = sprite_asset
    else:
        external_index = file_id - 1
        if not 0 <= external_index < len(sprite_asset.externals):
            raise SpriteRepackError(f"sprite texture file ID {file_id} is out of range")
        external = sprite_asset.externals[external_index]
        texture_asset = load_external_asset(data_root, external)
    return texture_asset, find_object(texture_asset, path_id, 28)

def load_asset(data_root: Path, container: str) -> SerializedAsset:
    root = data_root.resolve()
    normalized = container.replace("\\", "/").lower()
    key = (str(root).lower(), normalized)
    cached = asset_cache.get(key)
    if cached is not None:
        return cached
    path = (root / container).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise SpriteRepackError(f"sprite container escapes the game data folder: {container}") from exc
    if not path.is_file():
        raise SpriteRepackError(f"sprite container wasnt found: {path}")
    asset = load_serialized_asset(path, root)
    asset_cache[key] = asset
    return asset

def load_external_asset(data_root: Path, external: str) -> SerializedAsset:
    normalized = external.replace("\\", "/")
    direct = data_root / normalized
    if direct.is_file():
        return load_asset(data_root, direct.relative_to(data_root).as_posix())
    name = normalized.rsplit("/", 1)[-1]
    simple = data_root / name
    if simple.is_file():
        return load_asset(data_root, simple.relative_to(data_root).as_posix())
    matches = list(data_root.rglob(name))
    if len(matches) == 1:
        return load_asset(data_root, matches[0].relative_to(data_root).as_posix())
    raise SpriteRepackError(f"sprite texture container couldnt be resolved: {external}")

def find_object(asset: SerializedAsset, path_id: int, class_id: int) -> SerializedObject:
    for obj in asset.objects:
        if obj.path_id == path_id and obj.class_id == class_id:
            return obj
    raise SpriteRepackError(
        f"class {class_id} path {path_id} wasnt found in {asset.relative_path}"
    )

def resolve_resource(asset: SerializedAsset, source_path: str) -> Path:
    if source_path == asset.relative_path:
        return asset.path
    candidate = asset.path.parent / source_path
    if candidate.is_file():
        return candidate
    matches = list(asset.path.parent.rglob(Path(source_path).name))
    if matches:
        return matches[0]
    raise SpriteRepackError(f"sprite texture resource wasnt found: {source_path}")

def texture_target_record(
    asset: SerializedAsset,
    obj: SerializedObject,
    name: str,
) -> dict[str, Any]:
    return {
        "sunflowyrTaildata": 1,
        "kind": "sprite_png",
        "unityVersion": asset.unity_version,
        "serializedFormatVersion": asset.format_version,
        "endian": "little" if asset.endian == "<" else "big",
        "container": asset.relative_path,
        "appendTarget": asset.relative_path,
        "pathId": obj.path_id,
        "classId": obj.class_id,
        "className": obj.type_name,
        "name": name,
        "objectEntryOffset": obj.entry_offset,
        "pathIdOffset": obj.path_id_offset,
        "byteStartFieldOffset": obj.byte_start_field_offset,
        "byteSizeFieldOffset": obj.byte_size_field_offset,
        "byteStartRelative": obj.byte_start - asset.data_offset,
        "byteStartAbsolute": obj.byte_start,
        "byteSize": obj.byte_size,
        "dataOffset": asset.data_offset,
        "fieldWidths": {
            "byteStart": 4 if asset.format_version < 22 else 8,
            "byteSize": 4,
        },
    }

def write_rect(writer: EndianWriter, value: Any, label: str) -> None:
    rect = require_dict(value, label)
    for key in ("x", "y", "width", "height"):
        writer.f32(rect.get(key, 0.0))

def write_vector(writer: EndianWriter, value: Any, length: int, label: str) -> None:
    vector = require_list(value, label)
    if len(vector) != length:
        raise SpriteRepackError(f"{label} must contain {length} values")
    for item in vector:
        writer.f32(item)

def sprite_settings_raw(value: Any) -> int:
    settings = require_dict(value, "m_RD.settingsRaw")
    raw = int(settings.get("raw", 0)) & 0xFFFFFFFF
    raw = (raw & ~1) | (1 if int(settings.get("packed", raw & 1)) else 0)
    packing_mode = str(settings.get("packing_mode", settings.get("packingMode", "")))
    if packing_mode:
        raw = (raw & ~(1 << 1)) | ((1 if packing_mode == "Rectangle" else 0) << 1)
    rotation = str(settings.get("packing_rotation", settings.get("packingRotation", "")))
    rotations = {
        "None": 0,
        "FlipHorizontal": 1,
        "FlipVertical": 2,
        "Rotate180": 3,
        "Rotate90": 4,
    }
    if rotation in rotations:
        raw = (raw & ~(0xF << 2)) | (rotations[rotation] << 2)
    mesh_type = str(settings.get("mesh_type", settings.get("meshType", "")))
    if mesh_type:
        raw = (raw & ~(1 << 6)) | ((1 if mesh_type == "Tight" else 0) << 6)
    return raw

def has_541p3_pivot(version: tuple[int, int, int, int]) -> bool:
    if version[:3] != (5, 4, 1):
        return version[:3] > (5, 4, 1)
    return version[3] >= 3

def require_dict(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SpriteRepackError(f"{label} must be an object")
    return value

def optional_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}

def require_list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise SpriteRepackError(f"{label} must be an array")
    return value
