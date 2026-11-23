from __future__ import annotations
import io, struct
from pathlib import Path
from typing import Any
from .binary import EndianReader
from .crunch_encode import CrunchEncodeError, encode_crn_from_dxt
from .media_settings import apply_media_settings
from .mono_repack import taildata_value, unity_version_tuple
from .serialized import SerializedAsset, SerializedObject, load_serialized_asset
from .texture_convert import TEXTURE_FORMAT_NAMES

BLOCK_COMPRESSED_BYTES = {10: 8, 12: 16}
UNCOMPRESSED_PIXEL_BYTES = {1: 1, 2: 2, 3: 3, 4: 4, 5: 4, 7: 2, 8: 2, 13: 2, 14: 4, 62: 2, 63: 1, 72: 4, 73: 6, 74: 8}
BLOCK_FORMATS = {10, 12, 28, 29}
CRUNCH_FALLBACK_FORMATS = {28: 10, 29: 12}
MAX_TEXTURE_SIZE = 0x4000

class TextureRepackError(ValueError):
    pass

SUPPORTED_TEXTURE_FORMATS = {
    1,
    2,
    3,
    4,
    5,
    7,
    8,
    10,
    12,
    13,
    14,
    28,
    29,
    62,
    63,
    72,
    73,
    74,
}

asset_cache: dict[tuple[str, str], SerializedAsset] = {}

def repack_texture_png(
    png_path: Path,
    taildata: dict[str, Any],
    data_root: Path,
) -> bytes:
    class_id = int(taildata_value(taildata, "classId", 0) or 0)
    if class_id not in (28, 89):
        raise TextureRepackError(
            f"texture PNG taildata expected Texture2D or Cubemap, found class {class_id}"
        )
    container = str(taildata_value(taildata, "container") or "")
    path_id = int(taildata_value(taildata, "pathId", 0) or 0)
    if not container or not path_id:
        raise TextureRepackError("texture PNG taildata is missing container or pathId")

    details = taildata_value(taildata, "resourceDetails", {})
    if not isinstance(details, dict):
        raise TextureRepackError("texture PNG taildata is missing resourceDetails")
    width = int(taildata_value(details, "width", 0) or 0)
    height = int(taildata_value(details, "height", 0) or 0)
    image_count = int(taildata_value(details, "imageCount", 1) or 1)
    texture_format = int(taildata_value(details, "textureFormat", 0) or 0)
    if width <= 0 or height <= 0 or image_count <= 0:
        raise TextureRepackError("texture PNG taildata has invalid dimensions")
    if texture_format not in SUPPORTED_TEXTURE_FORMATS:
        format_name = TEXTURE_FORMAT_NAMES.get(texture_format, f"TextureFormat_{texture_format}")
        raise TextureRepackError(
            f"{format_name} isnt a texture format RWBY GE uses, check that the PNG taildata wasnt edited"
        )

    asset = load_texture_asset(Path(data_root), container)
    obj = find_texture_object(asset, path_id, class_id)
    original = read_object_bytes(asset, obj)
    layout = read_texture_layout(original, asset)
    if layout["width"] != width or layout["height"] != height:
        raise TextureRepackError(
            f"the unpack says {width}x{height} but the game texture is {layout['width']}x{layout['height']}, "
            "unpack this game version again"
        )
    if layout["image_count"] != image_count:
        raise TextureRepackError(
            f"texture taildata image count {image_count} doesnt match Unity object "
            f"{layout['image_count']}"
        )
    if layout["texture_format"] != texture_format:
        raise TextureRepackError(
            f"texture taildata format {texture_format} doesnt match Unity object "
            f"{layout['texture_format']}"
        )

    new_width, new_height = png_layer_size(Path(png_path), image_count)
    validate_texture_size(new_width, new_height, texture_format, class_id)
    rgba_layers = load_png_layers(
        Path(png_path),
        new_width,
        new_height,
        image_count,
        flip_vertical=class_id != 89,
    )
    produced = rebuild_texture_rgba_layers(asset, obj, rgba_layers, new_width, new_height)
    return apply_media_settings(produced, class_id, asset.endian, Path(png_path))

def png_layer_size(png_path: Path, image_count: int) -> tuple[int, int]:
    try:
        from PIL import Image
    except ImportError as exc:
        raise TextureRepackError("Pillow is required for PNG texture writeback") from exc
    with Image.open(png_path) as source:
        width, total_height = source.size
    if total_height % image_count:
        raise TextureRepackError(
            f"PNG height {total_height} must hold {image_count} equal faces stacked top to bottom"
        )
    return width, total_height // image_count

def validate_texture_size(width: int, height: int, texture_format: int, class_id: int) -> None:
    if not (1 <= width <= MAX_TEXTURE_SIZE and 1 <= height <= MAX_TEXTURE_SIZE):
        raise TextureRepackError(
            f"texture is {width}x{height}, Unity 5.4 accepts 1 to {MAX_TEXTURE_SIZE} pixels per side"
        )
    if class_id == 89 and (width != height or width & (width - 1)):
        raise TextureRepackError(
            f"Cubemap faces are {width}x{height}, Unity 5.4 needs square power of two faces"
        )
    if texture_format in BLOCK_FORMATS and (width % 4 or height % 4):
        format_name = TEXTURE_FORMAT_NAMES.get(texture_format, f"TextureFormat_{texture_format}")
        raise TextureRepackError(
            f"{format_name} textures need a width and height that are multiples of 4, "
            f"found {width}x{height}"
        )

def resized_mip_count(mip_count: int, old_width: int, old_height: int, width: int, height: int) -> int:
    if mip_count <= 1:
        return 1
    full_chain = max(width, height).bit_length()
    if mip_count >= max(old_width, old_height).bit_length():
        return full_chain
    return min(mip_count, full_chain)

def rebuild_texture_rgba_layers(
    asset: SerializedAsset,
    obj: SerializedObject,
    rgba_layers: list[bytes],
    width: int | None = None,
    height: int | None = None,
) -> bytes:
    original = read_object_bytes(asset, obj)
    layout = read_texture_layout(original, asset)
    width = int(width or layout["width"])
    height = int(height or layout["height"])
    image_count = int(layout["image_count"])
    texture_format = int(layout["texture_format"])
    expected_layer_size = width * height * 4
    if len(rgba_layers) != image_count:
        raise TextureRepackError(
            f"texture requires {image_count} image layers, found {len(rgba_layers)}"
        )
    if any(len(layer) != expected_layer_size for layer in rgba_layers):
        raise TextureRepackError(
            f"texture RGBA layers must each contain {expected_layer_size} bytes"
        )
    mip_count = resized_mip_count(
        max(1, int(layout["mip_count"])), int(layout["width"]), int(layout["height"]), width, height
    )
    texture_format, encoded_layers = encode_layers(rgba_layers, width, height, texture_format, mip_count)
    face_size = len(encoded_layers[0])
    if any(len(layer) != face_size for layer in encoded_layers):
        raise TextureRepackError("encoded texture layers have inconsistent sizes")
    encoded = b"".join(encoded_layers)

    prefix_end = int(layout["image_size_offset"]) + 4
    trailing = original[int(layout["trailing_offset"]):]
    rebuilt = bytearray(original[:prefix_end])
    pack_i32(rebuilt, int(layout["width_offset"]), width, asset.endian)
    pack_i32(rebuilt, int(layout["width_offset"]) + 4, height, asset.endian)
    pack_i32(rebuilt, int(layout["complete_size_offset"]), face_size, asset.endian)
    pack_i32(rebuilt, int(layout["format_offset"]), texture_format, asset.endian)
    pack_i32(rebuilt, int(layout["mip_count_offset"]), mip_count, asset.endian)
    pack_i32(rebuilt, int(layout["image_size_offset"]), len(encoded), asset.endian)
    rebuilt.extend(encoded)
    write_empty_stream_data(rebuilt, asset.endian)
    rebuilt.extend(trailing)
    payload = bytes(rebuilt)
    validate_rebuilt_texture(payload, asset, len(encoded), face_size, mip_count, trailing, layout)
    return payload

def encode_layers(
    rgba_layers: list[bytes],
    width: int,
    height: int,
    texture_format: int,
    mip_count: int,
) -> tuple[int, list[bytes]]:
    if texture_format not in CRUNCH_FALLBACK_FORMATS:
        return texture_format, [
            b"".join(encode_mip_levels(layer, width, height, texture_format, mip_count))
            for layer in rgba_layers
        ]
    block_format = CRUNCH_FALLBACK_FORMATS[texture_format]
    dxt_layers = [encode_mip_levels(layer, width, height, block_format, mip_count) for layer in rgba_layers]
    try:
        return texture_format, [
            encode_crn_from_dxt(levels, width, height, texture_format) for levels in dxt_layers
        ]
    except CrunchEncodeError:
        return block_format, [b"".join(levels) for levels in dxt_layers]

def encode_mip_levels(
    rgba: bytes,
    width: int,
    height: int,
    texture_format: int,
    mip_count: int,
) -> list[bytes]:
    levels = [encode_texture(rgba, width, height, texture_format)]
    if mip_count <= 1:
        return levels
    try:
        from PIL import Image
    except ImportError as exc:
        raise TextureRepackError("Pillow is required to rebuild a texture mip chain") from exc

    level_image = Image.frombytes("RGBA", (width, height), rgba)
    for level in range(1, mip_count):
        level_width = max(1, width >> level)
        level_height = max(1, height >> level)
        level_image = level_image.resize((level_width, level_height), Image.LANCZOS)
        levels.append(encode_texture(level_image.tobytes(), level_width, level_height, texture_format))
    return levels

def mip_chain_size(width: int, height: int, texture_format: int, mip_count: int) -> int:
    total = 0
    for level in range(max(1, mip_count)):
        level_width = max(1, width >> level)
        level_height = max(1, height >> level)
        total += encoded_level_size(level_width, level_height, texture_format)
    return total

def encoded_level_size(width: int, height: int, texture_format: int) -> int:
    if texture_format in BLOCK_COMPRESSED_BYTES:
        blocks = max(1, (width + 3) // 4) * max(1, (height + 3) // 4)
        return blocks * BLOCK_COMPRESSED_BYTES[texture_format]
    return width * height * UNCOMPRESSED_PIXEL_BYTES.get(texture_format, 4)

def write_empty_stream_data(rebuilt: bytearray, endian: str) -> None:
    padding = (-len(rebuilt)) % 4
    if padding:
        rebuilt.extend(b"\0" * padding)
    rebuilt.extend(struct.pack(endian + "II", 0, 0))
    rebuilt.extend(struct.pack(endian + "i", 0))

def stream_data_size(image_end: int) -> int:
    return ((-image_end) % 4) + 12

def validate_rebuilt_texture(
    payload: bytes,
    asset: SerializedAsset,
    encoded_size: int,
    face_size: int,
    mip_count: int,
    trailing: bytes,
    original_layout: dict[str, int],
) -> None:
    rebuilt_layout = read_texture_layout(payload, asset)
    if int(rebuilt_layout["mip_count"]) != mip_count:
        raise TextureRepackError(
            f"rebuilt Texture2D declares {rebuilt_layout['mip_count']} mip levels, "
            f"expected {mip_count}"
        )
    if int(rebuilt_layout["complete_image_size"]) != face_size:
        raise TextureRepackError(
            f"rebuilt Texture2D stores completeImageSize {rebuilt_layout['complete_image_size']}, "
            f"expected {face_size}"
        )
    width = int(rebuilt_layout["width"])
    height = int(rebuilt_layout["height"])
    texture_format = int(rebuilt_layout["texture_format"])
    if texture_format in CRUNCH_FALLBACK_FORMATS:
        validate_crunch_face(payload, rebuilt_layout, mip_count)
    else:
        expected_face = mip_chain_size(width, height, texture_format, mip_count)
        if face_size != expected_face:
            raise TextureRepackError(
                f"rebuilt Texture2D mip chain is {face_size} bytes but {mip_count} levels of "
                f"{width}x{height} should be {expected_face}"
            )
    image_size_offset = int(rebuilt_layout["image_size_offset"])
    expected_offset = int(original_layout["image_size_offset"])
    if image_size_offset != expected_offset:
        raise TextureRepackError(
            f"rebuilt Texture2D image data moved from offset {expected_offset} to {image_size_offset}"
        )
    if image_size_offset + 4 > len(payload):
        raise TextureRepackError("rebuilt Texture2D image-size field is out of bounds")
    stored_size = struct.unpack_from(asset.endian + "i", payload, image_size_offset)[0]
    if stored_size != encoded_size:
        raise TextureRepackError(
            f"rebuilt Texture2D stores image size {stored_size}, expected {encoded_size}"
        )
    image_end = image_size_offset + 4 + stored_size
    stream_end = image_end + stream_data_size(image_end)
    expected_object_size = stream_end + len(trailing)
    if len(payload) != expected_object_size:
        raise TextureRepackError(
            f"rebuilt Texture2D serialized size is {len(payload)} but its fields consume "
            f"{expected_object_size} bytes"
        )
    offset_field, size_field = struct.unpack_from(asset.endian + "II", payload, stream_end - 12)
    path_length = struct.unpack_from(asset.endian + "i", payload, stream_end - 4)[0]
    if offset_field or size_field or path_length:
        raise TextureRepackError(
            "a rebuilt texture cant point at streamed data"
        )
    if payload[stream_end:] != trailing:
        raise TextureRepackError("rebuilt texture lost the fields after m_StreamData (Cubemap m_SourceTextures)")

def validate_crunch_face(payload: bytes, layout: dict[str, int], mip_count: int) -> None:
    from .crunch import CrunchDecodeError, decode_crn_to_dxt, parse_header

    start = int(layout["image_size_offset"]) + 4
    crn = payload[start : start + int(layout["complete_image_size"])]
    try:
        header = parse_header(crn)
        if header.levels != mip_count or header.width != layout["width"] or header.height != layout["height"]:
            raise TextureRepackError(
                f"rebuilt CRN is {header.width}x{header.height} with {header.levels} levels, "
                f"Texture2D declares {layout['width']}x{layout['height']} with {mip_count}"
            )
        for level in range(header.levels):
            decode_crn_to_dxt(crn, expected_unity_format=int(layout["texture_format"]), level_index=level)
    except CrunchDecodeError as exc:
        raise TextureRepackError(f"rebuilt CRN doesnt decode: {exc}") from exc

def load_texture_asset(data_root: Path, container: str) -> SerializedAsset:
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
        raise TextureRepackError(
            f"texture container escapes the game data folder: {container}"
        ) from exc
    if not asset_path.is_file():
        raise TextureRepackError(f"texture container wasnt found: {asset_path}")
    asset = load_serialized_asset(asset_path, root)
    asset_cache[key] = asset
    return asset

def find_texture_object(
    asset: SerializedAsset,
    path_id: int,
    class_id: int,
) -> SerializedObject:
    for obj in asset.objects:
        if obj.path_id == path_id and obj.class_id == class_id:
            return obj
    raise TextureRepackError(
        f"texture object path {path_id} class {class_id} wasnt found in {asset.relative_path}"
    )

def read_object_bytes(asset: SerializedAsset, obj: SerializedObject) -> bytes:
    with asset.path.open("rb") as handle:
        handle.seek(obj.byte_start)
        data = handle.read(obj.byte_size)
    if len(data) != obj.byte_size:
        raise TextureRepackError("texture object data is truncated")
    return data

def read_texture_layout(data: bytes, asset: SerializedAsset) -> dict[str, int]:
    reader = EndianReader(io.BytesIO(data), asset.endian)
    reader.aligned_string(max_length=4096)
    width_offset = reader.tell()
    width = reader.i32()
    height = reader.i32()
    complete_size_offset = reader.tell()
    complete_image_size = reader.i32()
    format_offset = reader.tell()
    texture_format = reader.i32()
    version = unity_version_tuple(asset.unity_version)
    if version[0] < 5 or (version[0] == 5 and version[1] < 2):
        raise TextureRepackError("Unity textures before 5.2 arent supported by this writer")
    mip_count_offset = reader.tell()
    mip_count = reader.i32()
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
    reader.i32()
    reader.i32()
    image_size_offset = reader.tell()
    image_size = reader.i32()
    reader.seek(image_size_offset + 4 + image_size)
    reader.align()
    reader.u32()
    reader.u32()
    reader.aligned_string(max_length=4096)
    trailing_offset = reader.tell()
    if trailing_offset > len(data):
        raise TextureRepackError("texture m_StreamData runs past the object end")
    return {
        "width": width,
        "height": height,
        "texture_format": texture_format,
        "image_count": image_count,
        "mip_count": mip_count,
        "complete_image_size": complete_image_size,
        "image_size": image_size,
        "width_offset": width_offset,
        "format_offset": format_offset,
        "complete_size_offset": complete_size_offset,
        "mip_count_offset": mip_count_offset,
        "image_size_offset": image_size_offset,
        "trailing_offset": trailing_offset,
    }

def load_png_layers(
    png_path: Path,
    width: int,
    height: int,
    image_count: int,
    *,
    flip_vertical: bool,
) -> list[bytes]:
    try:
        from PIL import Image
    except ImportError as exc:
        raise TextureRepackError("Pillow is required for PNG texture writeback") from exc

    with Image.open(png_path) as source:
        image = source.convert("RGBA")
    expected_size = (width, height * image_count)
    if image.size != expected_size:
        raise TextureRepackError(
            f"PNG dimensions must be {expected_size[0]}x{expected_size[1]}, "
            f"found {image.width}x{image.height}"
        )
    layers = []
    for image_index in range(image_count):
        top = image_index * height
        layer = image.crop((0, top, width, top + height))
        if flip_vertical:
            layer = layer.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
        layers.append(layer.tobytes())
    return layers

def encode_texture(rgba: bytes, width: int, height: int, texture_format: int) -> bytes:
    pixel_count = width * height
    if len(rgba) != pixel_count * 4:
        raise TextureRepackError("RGBA buffer size doesnt match texture dimensions")
    if texture_format == 1:
        return bytes(rgba[index + 3] for index in range(0, len(rgba), 4))
    if texture_format == 2:
        return encode_4444(rgba, order="argb")
    if texture_format == 3:
        return strip_channels(rgba, (0, 1, 2))
    if texture_format == 4:
        return rgba
    if texture_format == 5:
        return strip_channels(rgba, (3, 0, 1, 2))
    if texture_format == 7:
        return encode_rgb565_pixels(rgba)
    if texture_format == 8:
        return strip_channels(rgba, (2, 1, 0))
    if texture_format == 10:
        return encode_dxt1(rgba, width, height)
    if texture_format == 12:
        return encode_dxt5(rgba, width, height)
    if texture_format == 13:
        return encode_4444(rgba, order="rgba")
    if texture_format == 14:
        return strip_channels(rgba, (2, 1, 0, 3))
    if texture_format == 62:
        return strip_channels(rgba, (0, 1))
    if texture_format == 63:
        return strip_channels(rgba, (0,))
    if texture_format == 72:
        return encode_u16_channels(rgba, (0, 1))
    if texture_format == 73:
        return encode_u16_channels(rgba, (0, 1, 2))
    if texture_format == 74:
        return encode_u16_channels(rgba, (0, 1, 2, 3))
    raise TextureRepackError(f"unsupported texture format {texture_format}")

def strip_channels(rgba: bytes, channels: tuple[int, ...]) -> bytes:
    output = bytearray((len(rgba) // 4) * len(channels))
    target = 0
    for source in range(0, len(rgba), 4):
        for channel in channels:
            output[target] = rgba[source + channel]
            target += 1
    return bytes(output)

def encode_4444(rgba: bytes, *, order: str) -> bytes:
    output = bytearray(len(rgba) // 2)
    target = 0
    for source in range(0, len(rgba), 4):
        red = rgba[source] >> 4
        green = rgba[source + 1] >> 4
        blue = rgba[source + 2] >> 4
        alpha = rgba[source + 3] >> 4
        if order == "argb":
            value = (alpha << 12) | (red << 8) | (green << 4) | blue
        else:
            value = (red << 12) | (green << 8) | (blue << 4) | alpha
        struct.pack_into("<H", output, target, value)
        target += 2
    return bytes(output)

def encode_rgb565_pixels(rgba: bytes) -> bytes:
    output = bytearray(len(rgba) // 2)
    target = 0
    for source in range(0, len(rgba), 4):
        value = rgb_to_565(rgba[source], rgba[source + 1], rgba[source + 2])
        struct.pack_into("<H", output, target, value)
        target += 2
    return bytes(output)

def encode_u16_channels(rgba: bytes, channels: tuple[int, ...]) -> bytes:
    output = bytearray((len(rgba) // 4) * len(channels) * 2)
    target = 0
    for source in range(0, len(rgba), 4):
        for channel in channels:
            value = rgba[source + channel] * 257
            struct.pack_into("<H", output, target, value)
            target += 2
    return bytes(output)

def encode_dxt1_python(rgba: bytes, width: int, height: int) -> bytes:
    output = bytearray()
    for block_y in range(0, height, 4):
        for block_x in range(0, width, 4):
            pixels = block_pixels(rgba, width, height, block_x, block_y)
            output.extend(encode_dxt1_block(pixels, allow_transparency=True))
    return bytes(output)

def encode_dxt5_python(rgba: bytes, width: int, height: int) -> bytes:
    output = bytearray()
    for block_y in range(0, height, 4):
        for block_x in range(0, width, 4):
            pixels = block_pixels(rgba, width, height, block_x, block_y)
            output.extend(encode_dxt5_alpha(pixels))
            output.extend(encode_dxt1_block(pixels, allow_transparency=False))
    return bytes(output)

def block_pixels(
    rgba: bytes,
    width: int,
    height: int,
    block_x: int,
    block_y: int,
) -> list[tuple[int, int, int, int]]:
    pixels = []
    for local_y in range(4):
        y = min(block_y + local_y, height - 1)
        for local_x in range(4):
            x = min(block_x + local_x, width - 1)
            source = (y * width + x) * 4
            pixels.append(tuple(rgba[source : source + 4]))
    return pixels

def encode_dxt1_block(
    pixels: list[tuple[int, int, int, int]],
    *,
    allow_transparency: bool,
) -> bytes:
    transparent = allow_transparency and any(pixel[3] < 128 for pixel in pixels)
    opaque_pixels = [pixel for pixel in pixels if not transparent or pixel[3] >= 128]
    if not opaque_pixels:
        return struct.pack("<HHI", 0, 0, 0xFFFFFFFF)

    low = min(opaque_pixels, key=color_axis)
    high = max(opaque_pixels, key=color_axis)
    low_565 = rgb_to_565(low[0], low[1], low[2])
    high_565 = rgb_to_565(high[0], high[1], high[2])
    if transparent:
        color0, color1 = sorted((low_565, high_565))
    else:
        color0, color1 = sorted((low_565, high_565), reverse=True)
        if color0 == color1:
            if color0 < 0xFFFF:
                color0 += 1
            elif color1 > 0:
                color1 -= 1

    palette = dxt_color_palette(color0, color1)
    indices = 0
    for index, pixel in enumerate(pixels):
        if transparent and pixel[3] < 128:
            palette_index = 3
        else:
            limit = 3 if transparent else 4
            palette_index = min(
                range(limit),
                key=lambda candidate: color_distance(pixel, palette[candidate]),
            )
        indices |= palette_index << (index * 2)
    return struct.pack("<HHI", color0, color1, indices)

def encode_dxt5_alpha(pixels: list[tuple[int, int, int, int]]) -> bytes:
    alpha_values = [pixel[3] for pixel in pixels]
    alpha0 = max(alpha_values)
    alpha1 = min(alpha_values)
    if alpha0 == alpha1:
        return bytes((alpha0, alpha1)) + b"\0" * 6
    palette = dxt5_alpha_palette(alpha0, alpha1)
    indices = 0
    for index, alpha in enumerate(alpha_values):
        palette_index = min(
            range(8),
            key=lambda candidate: abs(alpha - palette[candidate]),
        )
        indices |= palette_index << (index * 3)
    return bytes((alpha0, alpha1)) + indices.to_bytes(6, "little")

def dxt_color_palette(color0: int, color1: int) -> list[tuple[int, int, int, int]]:
    first = rgb_from_565(color0)
    second = rgb_from_565(color1)
    if color0 > color1:
        third = tuple((2 * first[channel] + second[channel]) // 3 for channel in range(3))
        fourth = tuple((first[channel] + 2 * second[channel]) // 3 for channel in range(3))
        return [
            (*first, 255),
            (*second, 255),
            (*third, 255),
            (*fourth, 255),
        ]
    third = tuple((first[channel] + second[channel]) // 2 for channel in range(3))
    return [
        (*first, 255),
        (*second, 255),
        (*third, 255),
        (0, 0, 0, 0),
    ]

def dxt5_alpha_palette(alpha0: int, alpha1: int) -> list[int]:
    if alpha0 > alpha1:
        return [
            alpha0,
            alpha1,
            (6 * alpha0 + alpha1) // 7,
            (5 * alpha0 + 2 * alpha1) // 7,
            (4 * alpha0 + 3 * alpha1) // 7,
            (3 * alpha0 + 4 * alpha1) // 7,
            (2 * alpha0 + 5 * alpha1) // 7,
            (alpha0 + 6 * alpha1) // 7,
        ]
    return [
        alpha0,
        alpha1,
        (4 * alpha0 + alpha1) // 5,
        (3 * alpha0 + 2 * alpha1) // 5,
        (2 * alpha0 + 3 * alpha1) // 5,
        (alpha0 + 4 * alpha1) // 5,
        0,
        255,
    ]

def color_axis(pixel: tuple[int, int, int, int]) -> int:
    return pixel[0] * 3 + pixel[1] * 6 + pixel[2]

def color_distance(
    pixel: tuple[int, int, int, int],
    color: tuple[int, int, int, int],
) -> int:
    red = pixel[0] - color[0]
    green = pixel[1] - color[1]
    blue = pixel[2] - color[2]
    return red * red + green * green + blue * blue

def rgb_to_565(red: int, green: int, blue: int) -> int:
    return ((red * 31 + 127) // 255 << 11) | ((green * 63 + 127) // 255 << 5) | (
        (blue * 31 + 127) // 255
    )

def rgb_from_565(value: int) -> tuple[int, int, int]:
    red = (value >> 11) & 31
    green = (value >> 5) & 63
    blue = value & 31
    return (
        (red << 3) | (red >> 2),
        (green << 2) | (green >> 4),
        (blue << 3) | (blue >> 2),
    )

def pack_i32(buffer: bytearray, offset: int, value: int, endian: str) -> None:
    struct.pack_into(endian + "i", buffer, offset, int(value))

def fully_opaque(rgba: bytes) -> bool:
    return not bytes(rgba[3::4]).strip(b"\xff")

def encode_block_compressed_native(rgba: bytes, width: int, height: int, fourcc: str) -> bytes | None:
    if width % 4 or height % 4:
        return None
    from PIL import Image

    try:
        image = Image.frombytes("RGBA", (width, height), rgba)
        buffer = io.BytesIO()
        image.save(buffer, format="DDS", pixel_format=fourcc)
    except Exception:
        return None
    data = buffer.getvalue()
    if len(data) < 128:
        return None
    blocks = data[128:]
    expected = (width // 4) * (height // 4) * (8 if fourcc == "DXT1" else 16)
    if len(blocks) != expected:
        return None
    return blocks

def encode_dxt1(rgba: bytes, width: int, height: int) -> bytes:
    if fully_opaque(rgba):
        native = encode_block_compressed_native(rgba, width, height, "DXT1")
        if native is not None:
            return native
    return encode_dxt1_python(rgba, width, height)

def encode_dxt5(rgba: bytes, width: int, height: int) -> bytes:
    native = encode_block_compressed_native(rgba, width, height, "DXT5")
    if native is not None:
        return native
    return encode_dxt5_python(rgba, width, height)
