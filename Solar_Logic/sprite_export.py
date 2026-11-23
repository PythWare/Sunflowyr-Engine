from __future__ import annotations
import json, math
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable
from .binary import EndianReader, read_range
from .objects import ResourcePayload, read_texture2d
from .serialized import SerializedAsset, SerializedObject
from .texture_convert import TextureDecodeError, decode_texture

class SpriteExportError(ValueError):
    pass

ObjectKey = tuple[str, int]
ReservePath = Callable[[Path, str], Path]

@dataclass(frozen=True, slots=True)
class PPtr:
    file_id: int
    path_id: int
    file_id_offset: int
    path_id_offset: int

    def as_json(self) -> dict:
        return {
            "m_FileID": self.file_id,
            "m_PathID": self.path_id,
            "file_id_offset": self.file_id_offset,
            "path_id_offset": self.path_id_offset,
        }

@dataclass(slots=True)
class SpriteSettings:
    raw: int
    packed: int
    packing_mode: str
    packing_rotation: str
    mesh_type: str

    def as_json(self) -> dict:
        return {
            "raw": self.raw,
            "packed": self.packed,
            "packing_mode": self.packing_mode,
            "packing_rotation": self.packing_rotation,
            "mesh_type": self.mesh_type,
        }

@dataclass(slots=True)
class SpriteRenderData:
    texture: PPtr
    alpha_texture: PPtr | None
    vertices: list[dict]
    indices: list[int]
    texture_rect: dict[str, float]
    texture_rect_offset: tuple[float, float]
    settings: SpriteSettings
    uv_transform: tuple[float, float, float, float] | None
    downscale_multiplier: float

@dataclass(slots=True)
class SpriteData:
    name: str
    rect: dict[str, float]
    offset: tuple[float, float]
    border: tuple[float, float, float, float]
    pixels_to_units: float
    pivot: tuple[float, float]
    extrude: int
    is_polygon: bool | None
    render_data: SpriteRenderData

@dataclass(slots=True)
class SpriteExportResult:
    png_path: Path
    json_path: Path
    png_metadata: dict
    json_metadata: dict
    conversion: dict

class SpriteExportContext:
    def __init__(self, assets: Iterable[SerializedAsset]) -> None:
        self.assets = {norm(asset.relative_path): asset for asset in assets}
        self.aliases: dict[str, str] = {}
        self.objects: dict[ObjectKey, SerializedObject] = {}
        for asset_key, asset in self.assets.items():
            self.aliases.setdefault(asset_key, asset_key)
            self.aliases.setdefault(norm(asset.path.name), asset_key)
            for obj in asset.objects:
                self.objects[(asset_key, obj.path_id)] = obj

    def key_for(self, asset: SerializedAsset, obj: SerializedObject) -> ObjectKey:
        return (norm(asset.relative_path), obj.path_id)

    def resolve(self, asset: SerializedAsset, pointer: PPtr | None) -> ObjectKey | None:
        if pointer is None or pointer.path_id == 0 or pointer.file_id < 0:
            return None
        asset_key = norm(asset.relative_path)
        if pointer.file_id == 0:
            return (asset_key, pointer.path_id)
        external_index = pointer.file_id - 1
        if not (0 <= external_index < len(asset.externals)):
            return None
        external = asset.externals[external_index]
        target_key = self.aliases.get(norm(external)) or self.aliases.get(norm(Path(external).name))
        if target_key is None:
            return None
        return (target_key, pointer.path_id)

    def asset_for_key(self, key: ObjectKey) -> SerializedAsset | None:
        return self.assets.get(key[0])

    def object_for_key(self, key: ObjectKey) -> SerializedObject | None:
        return self.objects.get(key)

def export_sprite_view(
    context: SpriteExportContext,
    asset: SerializedAsset,
    obj: SerializedObject,
    output_root: Path,
    *,
    write_embedded_taildata: bool,
    reserve_path: ReservePath,
    object_metadata: dict,
) -> SpriteExportResult:
    with asset.path.open("rb") as handle:
        sprite = read_sprite(asset, EndianReader(handle, asset.endian), obj)

    texture_key = context.resolve(asset, sprite.render_data.texture)
    if texture_key is None:
        raise SpriteExportError("sprite texture pointer couldnt be resolved")
    texture_asset = context.asset_for_key(texture_key)
    texture_obj = context.object_for_key(texture_key)
    if texture_asset is None or texture_obj is None or texture_obj.class_id != 28:
        raise SpriteExportError(f"resolved sprite texture isnt Texture2D: {texture_key!r}")

    texture_payload, texture_name = read_texture_payload(texture_asset, texture_obj)
    resource_path = resolve_resource(texture_asset, texture_payload)
    if resource_path is None:
        raise SpriteExportError(f"sprite texture resource wasnt found: {texture_payload.source_path}")
    texture_bytes = b"".join(read_range(resource_path, texture_payload.source_offset, texture_payload.source_size))
    decoded = decode_texture(
        texture_bytes,
        int(texture_payload.details["width"]),
        int(texture_payload.details["height"]),
        int(texture_payload.details["texture_format"]),
    )
    conversion = write_sprite_png(sprite, decoded.rgba, decoded.width, decoded.height, output_root, asset, obj, reserve_path)

    class_dir = f"{obj.class_id:03d}_{safe_name(obj.type_name)}"
    json_path = reserve_path(
        output_root
        / "converted"
        / "sprite_json"
        / class_dir
        / f"{safe_name(sprite.name) or obj.type_name}__path{obj.path_id}.json",
        f"{asset.relative_path}:{obj.path_id}:sprite_json",
    )
    json_metadata = dict(object_metadata)
    json_metadata.update(
        {
            "kind": "sprite_json",
            "writeback_note": (
                "Sprite JSON is a view/edit format. Rect, pivot, render-data, texture pointer, and source IDs "
                "are preserved so the AssetBundle generator can encode changes back into the Unity Sprite object."
            ),
        }
    )
    png_metadata = dict(object_metadata)
    png_metadata.update(
        {
            "kind": "sprite_png",
            "view_of_kind": "sprite",
            "sprite_writeback_supported": True,
            "sprite_writeback_format": "sprite_png_to_texture2d_atlas",
            "writeback_note": (
                "Sprite PNG is an editable crop. Keep its exported dimensions, the AssetBundle generator places it back into "
                "the source atlas and rebuilds the Texture2D object. Multiple edits sharing an atlas are grouped."
            ),
        }
    )
    texture_info = {
        "container": texture_key[0],
        "path_id": texture_key[1],
        "name": texture_name,
        "width": decoded.width,
        "height": decoded.height,
        "texture_format": decoded.texture_format,
        "texture_format_name": decoded.format_name,
        "payload": {
            "source_path": texture_payload.source_path,
            "source_offset": texture_payload.source_offset,
            "source_size": texture_payload.source_size,
            "resource_path": rel(resource_path, texture_asset.data_root),
        },
    }
    document = {
        "m_Name": sprite.name,
        "m_Rect": sprite.rect,
        "m_Offset": list(sprite.offset),
        "m_Border": list(sprite.border),
        "m_PixelsToUnits": sprite.pixels_to_units,
        "m_Pivot": list(sprite.pivot),
        "m_Extrude": sprite.extrude,
        "m_IsPolygon": sprite.is_polygon,
        "m_RD": render_data_json(sprite.render_data),
        "source_texture": texture_info,
        "outputs": {
            "png": str(conversion["png_path"]),
        },
        "conversion": {key: value for key, value in conversion.items() if key != "png_path"},
    }
    json_path.parent.mkdir(parents=True, exist_ok=True)
    with json_path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(document, handle, ensure_ascii=False, indent=2)
        handle.write("\n")

    png_path = Path(conversion["png_path"])
    json_metadata["export_size"] = json_path.stat().st_size
    json_metadata["conversion"] = {key: value for key, value in conversion.items() if key != "png_path"}
    return SpriteExportResult(png_path, json_path, png_metadata, json_metadata, json_metadata["conversion"])

def read_sprite(asset: SerializedAsset, reader: EndianReader, obj: SerializedObject) -> SpriteData:
    version = unity_version_tuple(asset.unity_version)
    reader.seek(obj.byte_start)
    name = reader.aligned_string(max_length=4096)
    rect = read_rect(reader)
    offset = read_vector(reader, 2)
    border = read_vector(reader, 4) if version_ge(version, (4, 5, 0, 0)) else (0.0, 0.0, 0.0, 0.0)
    pixels_to_units = reader.f32()
    pivot = (0.5, 0.5)
    if has_541p3_pivot(version):
        pivot = read_vector(reader, 2)
    extrude = reader.u32()
    is_polygon = None
    if version_ge(version, (5, 3, 0, 0)):
        is_polygon = reader.bool()
        reader.align()
    if version[0] >= 2017:
        raise SpriteExportError("SpriteAtlas-backed 2017+ Sprite layout isnt supported by this exporter yet")
    render_data = read_sprite_render_data(asset, reader, version)
    return SpriteData(name, rect, offset, border, pixels_to_units, pivot, extrude, is_polygon, render_data)

def read_sprite_render_data(asset: SerializedAsset, reader: EndianReader, version: tuple[int, int, int, int]) -> SpriteRenderData:
    texture = read_pptr(reader, asset)
    alpha_texture = read_pptr(reader, asset) if version_ge(version, (5, 2, 0, 0)) else None
    if version_ge(version, (5, 6, 0, 0)):
        raise SpriteExportError("Unity 5.6+ SpriteRenderData vertex layout isnt supported by this exporter yet")
    vertices: list[dict] = []
    for _ in range(read_count(reader, 1_000_000)):
        vertices.append({"pos": list(read_vector(reader, 3))})
        if version[0] < 4 or (version[0] == 4 and version[1] <= 3):
            vertices[-1]["uv"] = list(read_vector(reader, 2))
    indices = [reader.u16() for _ in range(read_count(reader, 10_000_000))]
    reader.align()
    texture_rect = read_rect(reader)
    texture_rect_offset = read_vector(reader, 2)
    settings = read_sprite_settings(reader)
    uv_transform = read_vector(reader, 4) if version_ge(version, (4, 5, 0, 0)) else None
    downscale_multiplier = reader.f32() if version[0] >= 2017 else 1.0
    return SpriteRenderData(texture, alpha_texture, vertices, indices, texture_rect, texture_rect_offset, settings, uv_transform, downscale_multiplier)

def read_texture_payload(asset: SerializedAsset, obj: SerializedObject) -> tuple[ResourcePayload, str]:
    with asset.path.open("rb") as handle:
        reader = EndianReader(handle, asset.endian)
        payload = read_texture2d(asset, reader, obj, "")
    if payload is None:
        raise SpriteExportError(f"Texture2D path {obj.path_id} has no readable image payload")
    return payload, payload.name

def write_sprite_png(
    sprite: SpriteData,
    rgba: bytes,
    texture_width: int,
    texture_height: int,
    output_root: Path,
    asset: SerializedAsset,
    obj: SerializedObject,
    reserve_path: ReservePath,
) -> dict:
    from PIL import Image

    image = Image.frombytes("RGBA", (texture_width, texture_height), rgba)
    downscale = sprite.render_data.downscale_multiplier
    if downscale > 0 and not math.isclose(downscale, 1.0):
        image = image.resize((int(texture_width / downscale), int(texture_height / downscale)))
    rect = sprite.render_data.texture_rect
    left = max(0, int(math.floor(rect["x"])))
    top = max(0, int(math.floor(rect["y"])))
    right = min(image.width, int(math.ceil(rect["x"] + rect["width"])))
    bottom = min(image.height, int(math.ceil(rect["y"] + rect["height"])))
    if right <= left or bottom <= top:
        raise SpriteExportError(f"sprite texture rect is empty: {rect}")
    sprite_image = image.crop((left, top, right, bottom))

    if sprite.render_data.settings.packed:
        rotation = sprite.render_data.settings.packing_rotation
        if rotation == "FlipHorizontal":
            sprite_image = sprite_image.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
        elif rotation == "FlipVertical":
            sprite_image = sprite_image.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
        elif rotation == "Rotate180":
            sprite_image = sprite_image.rotate(180, expand=True)
        elif rotation == "Rotate90":
            sprite_image = sprite_image.rotate(270, expand=True)

    tight_mask_applied = False
    if sprite.render_data.settings.packing_mode == "Tight":
        sprite_image, tight_mask_applied = apply_tight_mask(sprite_image, sprite)

    sprite_image = sprite_image.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
    class_dir = f"{obj.class_id:03d}_{safe_name(obj.type_name)}"
    png_path = reserve_path(
        output_root
        / "converted"
        / "sprite_png"
        / class_dir
        / f"{safe_name(sprite.name) or obj.type_name}__path{obj.path_id}.png",
        f"{asset.relative_path}:{obj.path_id}:sprite_png",
    )
    png_path.parent.mkdir(parents=True, exist_ok=True)
    sprite_image.save(png_path, "PNG")
    return {
        "format": "sprite_png",
        "png_path": str(png_path),
        "texture_rect": rect,
        "crop_box": [left, top, right, bottom],
        "output_width": sprite_image.width,
        "output_height": sprite_image.height,
        "packing": sprite.render_data.settings.as_json(),
        "tight_mesh_mask_applied": tight_mask_applied,
    }

def tight_mesh_mask(size, sprite: SpriteData):
    if not sprite.render_data.vertices or len(sprite.render_data.indices) < 3:
        return None
    try:
        from PIL import Image, ImageDraw

        mask = Image.new("L", size, 0)
        draw = ImageDraw.Draw(mask)
        offset_x, offset_y = sprite.render_data.texture_rect_offset
        pivot_x, pivot_y = sprite.pivot
        translate_x = sprite.rect["width"] * pivot_x - offset_x
        translate_y = sprite.rect["height"] * pivot_y - offset_y
        positions = [vertex["pos"] for vertex in sprite.render_data.vertices]
        for i in range(0, len(sprite.render_data.indices) - 2, 3):
            try:
                triangle = []
                for index in sprite.render_data.indices[i : i + 3]:
                    vx, vy, vz = positions[index]
                    triangle.append((vx * sprite.pixels_to_units + translate_x, vy * sprite.pixels_to_units + translate_y))
                draw.polygon(triangle, fill=255)
            except (IndexError, TypeError, ValueError):
                continue
        if mask.getbbox() is None:
            return None
        return mask
    except Exception:
        return None

def apply_tight_mask(image, sprite: SpriteData):
    mask = tight_mesh_mask(image.size, sprite)
    if mask is None:
        return image, False
    try:
        from PIL import ImageChops

        output = image.convert("RGBA")
        alpha = output.split()[3]
        output.putalpha(ImageChops.multiply(alpha, mask))
        return output, True
    except Exception:
        return image, False

def render_data_json(render_data: SpriteRenderData) -> dict:
    return {
        "texture": render_data.texture.as_json(),
        "alphaTexture": render_data.alpha_texture.as_json() if render_data.alpha_texture else None,
        "vertices": render_data.vertices,
        "indices": render_data.indices,
        "textureRect": render_data.texture_rect,
        "textureRectOffset": list(render_data.texture_rect_offset),
        "settingsRaw": render_data.settings.as_json(),
        "uvTransform": list(render_data.uv_transform) if render_data.uv_transform else None,
    }

def read_pptr(reader: EndianReader, asset: SerializedAsset) -> PPtr:
    file_id_offset = reader.tell()
    file_id = reader.i32()
    path_id_offset = reader.tell()
    path_id = reader.i64() if asset.format_version >= 14 else reader.i32()
    return PPtr(file_id, path_id, file_id_offset, path_id_offset)

def read_rect(reader: EndianReader) -> dict[str, float]:
    return {
        "x": reader.f32(),
        "y": reader.f32(),
        "width": reader.f32(),
        "height": reader.f32(),
    }

def read_vector(reader: EndianReader, length: int) -> tuple[float, ...]:
    return tuple(reader.f32() for _ in range(length))

def read_sprite_settings(reader: EndianReader) -> SpriteSettings:
    raw = reader.u32()
    packed = raw & 1
    packing_mode = "Rectangle" if ((raw >> 1) & 1) else "Tight"
    rotation_id = (raw >> 2) & 0xF
    mesh_type = "Tight" if ((raw >> 6) & 1) else "FullRect"
    rotations = {
        0: "None",
        1: "FlipHorizontal",
        2: "FlipVertical",
        3: "Rotate180",
        4: "Rotate90",
    }
    return SpriteSettings(raw, packed, packing_mode, rotations.get(rotation_id, f"Rotation_{rotation_id}"), mesh_type)

def read_count(reader: EndianReader, maximum: int) -> int:
    count = reader.i32()
    if count < 0 or count > maximum:
        raise SpriteExportError(f"invalid serialized array count {count}")
    return count

def resolve_resource(asset: SerializedAsset, payload: ResourcePayload) -> Path | None:
    if payload.source_path == asset.relative_path:
        return asset.path
    candidate = asset.path.parent / payload.source_path
    if candidate.exists():
        return candidate
    matches = list(asset.path.parent.rglob(Path(payload.source_path).name))
    return matches[0] if matches else None

def version_ge(version: tuple[int, int, int, int], threshold: tuple[int, int, int, int]) -> bool:
    return version >= threshold

def has_541p3_pivot(version: tuple[int, int, int, int]) -> bool:
    if version[0] > 5:
        return True
    if version[0] < 5:
        return False
    if version[1] > 4:
        return True
    if version[1] < 4:
        return False
    if version[2] > 1:
        return True
    if version[2] < 1:
        return False
    return version[3] >= 3

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

def norm(path: str) -> str:
    return path.replace("\\", "/").lower()

def safe_name(value: str | None, max_len: int = 120) -> str:
    import re

    if not value:
        return ""
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    if not value:
        return ""
    return value[:max_len].rstrip(" .")

def rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.name
