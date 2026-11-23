from __future__ import annotations
import io, math, struct
from dataclasses import dataclass
from .paths import ensure_parent

@dataclass(slots=True)
class TextureDecodeResult:
    width: int
    height: int
    texture_format: int
    format_name: str
    rgba: bytes

TEXTURE_FORMAT_NAMES = {
    1: "Alpha8",
    2: "ARGB4444",
    3: "RGB24",
    4: "RGBA32",
    5: "ARGB32",
    6: "ARGBFloat",
    7: "RGB565",
    8: "BGR24",
    9: "R16",
    10: "DXT1",
    11: "DXT3",
    12: "DXT5",
    13: "RGBA4444",
    14: "BGRA32",
    15: "RHalf",
    16: "RGHalf",
    17: "RGBAHalf",
    18: "RFloat",
    19: "RGFloat",
    20: "RGBAFloat",
    21: "YUY2",
    22: "RGB9e5Float",
    23: "RGBFloat",
    24: "BC6H",
    25: "BC7",
    26: "BC4",
    27: "BC5",
    28: "DXT1Crunched",
    29: "DXT5Crunched",
    30: "PVRTC_RGB2",
    31: "PVRTC_RGBA2",
    32: "PVRTC_RGB4",
    33: "PVRTC_RGBA4",
    34: "ETC_RGB4",
    35: "ATC_RGB4",
    36: "ATC_RGBA8",
    41: "EAC_R",
    42: "EAC_R_SIGNED",
    43: "EAC_RG",
    44: "EAC_RG_SIGNED",
    45: "ETC2_RGB",
    46: "ETC2_RGBA1",
    47: "ETC2_RGBA8",
    48: "ASTC_RGB_4x4",
    49: "ASTC_RGB_5x5",
    50: "ASTC_RGB_6x6",
    51: "ASTC_RGB_8x8",
    52: "ASTC_RGB_10x10",
    53: "ASTC_RGB_12x12",
    54: "ASTC_RGBA_4x4",
    55: "ASTC_RGBA_5x5",
    56: "ASTC_RGBA_6x6",
    57: "ASTC_RGBA_8x8",
    58: "ASTC_RGBA_10x10",
    59: "ASTC_RGBA_12x12",
    60: "ETC_RGB4_3DS",
    61: "ETC_RGBA8_3DS",
    62: "RG16",
    63: "R8",
    64: "ETC_RGB4Crunched",
    65: "ETC2_RGBA8Crunched",
    66: "ASTC_HDR_4x4",
    67: "ASTC_HDR_5x5",
    68: "ASTC_HDR_6x6",
    69: "ASTC_HDR_8x8",
    70: "ASTC_HDR_10x10",
    71: "ASTC_HDR_12x12",
    72: "RG32",
    73: "RGB48",
    74: "RGBA64",
}

CRUNCH_FORMATS = {28, 29, 64, 65}
PILLOW_DDS_FORMATS = {24, 25}

class TextureDecodeError(ValueError):
    pass

def decode_texture(data: bytes, width: int, height: int, texture_format: int) -> TextureDecodeResult:
    if width <= 0 or height <= 0:
        raise TextureDecodeError("texture has empty dimensions")

    if texture_format == 1:
        rgba = decode_alpha8(data, width, height)
    elif texture_format == 2:
        rgba = decode_argb4444(data, width, height)
    elif texture_format == 3:
        rgba = decode_rgb24(data, width, height)
    elif texture_format == 4:
        rgba = decode_rgba32(data, width, height)
    elif texture_format == 5:
        rgba = decode_argb32(data, width, height)
    elif texture_format == 6:
        rgba = decode_argb_float(data, width, height)
    elif texture_format == 7:
        rgba = decode_rgb565(data, width, height)
    elif texture_format == 8:
        rgba = decode_bgr24(data, width, height)
    elif texture_format == 9:
        rgba = decode_r16(data, width, height)
    elif texture_format == 10:
        rgba = decode_dxt1(data, width, height)
    elif texture_format == 11:
        rgba = decode_dxt3(data, width, height)
    elif texture_format == 12:
        rgba = decode_dxt5(data, width, height)
    elif texture_format == 13:
        rgba = decode_rgba4444(data, width, height)
    elif texture_format == 14:
        rgba = decode_bgra32(data, width, height)
    elif texture_format == 15:
        rgba = decode_r_half(data, width, height)
    elif texture_format == 16:
        rgba = decode_rg_half(data, width, height)
    elif texture_format == 17:
        rgba = decode_rgba_half(data, width, height)
    elif texture_format == 18:
        rgba = decode_r_float(data, width, height)
    elif texture_format == 19:
        rgba = decode_rg_float(data, width, height)
    elif texture_format == 20:
        rgba = decode_rgba_float(data, width, height)
    elif texture_format == 21:
        rgba = decode_yuy2(data, width, height)
    elif texture_format == 22:
        rgba = decode_rgb9e5(data, width, height)
    elif texture_format == 23:
        rgba = decode_rgb_float(data, width, height)
    elif texture_format == 24:
        rgba = decode_dds_via_pillow(data, width, height, dxgi_format=95)
    elif texture_format == 25:
        rgba = decode_dds_via_pillow(data, width, height, dxgi_format=98)
    elif texture_format == 26:
        rgba = decode_bc4(data, width, height, signed=False)
    elif texture_format == 27:
        rgba = decode_bc5(data, width, height, signed=False)
    elif texture_format in CRUNCH_FORMATS:
        rgba = decode_crunch(data, width, height, texture_format)
    elif texture_format == 62:
        rgba = decode_rg16(data, width, height)
    elif texture_format == 63:
        rgba = decode_r8(data, width, height)
    elif texture_format == 72:
        rgba = decode_rg32(data, width, height)
    elif texture_format == 73:
        rgba = decode_rgb48(data, width, height)
    elif texture_format == 74:
        rgba = decode_rgba64(data, width, height)
    else:
        raise TextureDecodeError(f"unsupported texture format {texture_format}")

    return TextureDecodeResult(
        width=width,
        height=height,
        texture_format=texture_format,
        format_name=format_name(texture_format),
        rgba=rgba,
    )

def save_png(
    data: bytes,
    width: int,
    height: int,
    texture_format: int,
    out_path,
    *,
    flip_vertical: bool = True,
    image_count: int = 1,
    complete_image_size: int = 0,
) -> dict:
    from PIL import Image

    if image_count <= 0:
        raise TextureDecodeError(f"texture has invalid image count {image_count}")
    layer_stride = complete_image_size if image_count > 1 else len(data)
    if image_count > 1 and layer_stride <= 0:
        if len(data) % image_count:
            raise TextureDecodeError(f"texture data size {len(data)} isnt divisible by {image_count} images")
        layer_stride = len(data) // image_count

    layers = []
    for image_index in range(image_count):
        layer_start = image_index * layer_stride
        layer_end = layer_start + layer_stride
        if layer_end > len(data):
            raise TextureDecodeError(
                f"texture image {image_index} needs bytes {layer_start}:{layer_end}, got {len(data)}"
            )
        decoded = decode_texture(data[layer_start:layer_end], width, height, texture_format)
        layer = Image.frombytes("RGBA", (decoded.width, decoded.height), decoded.rgba)
        if flip_vertical:
            layer = layer.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
        layers.append(layer)

    image = Image.new("RGBA", (width, height * image_count))
    for image_index, layer in enumerate(layers):
        image.paste(layer, (0, image_index * height))
    ensure_parent(out_path)
    image.save(out_path, "PNG")
    return {
        "width": width,
        "height": height * image_count,
        "face_width": width,
        "face_height": height,
        "image_count": image_count,
        "texture_format": texture_format,
        "texture_format_name": format_name(texture_format),
        "flip_vertical": flip_vertical,
    }

def format_name(texture_format: int) -> str:
    return TEXTURE_FORMAT_NAMES.get(texture_format, f"TextureFormat_{texture_format}")

def need(data: bytes, size: int, fmt: str) -> None:
    if len(data) < size:
        raise TextureDecodeError(f"{fmt} needs {size} bytes, got {len(data)}")

def decode_alpha8(data: bytes, width: int, height: int) -> bytes:
    size = width * height
    need(data, size, "Alpha8")
    out = bytearray(size * 4)
    for i in range(size):
        j = i * 4
        out[j : j + 4] = bytes((255, 255, 255, data[i]))
    return bytes(out)

def decode_argb4444(data: bytes, width: int, height: int) -> bytes:
    size = width * height
    need(data, size * 2, "ARGB4444")
    out = bytearray(size * 4)
    for i in range(size):
        value = int.from_bytes(data[i * 2 : i * 2 + 2], "little")
        b = expand4(value & 0x000F)
        g = expand4((value & 0x00F0) >> 4)
        r = expand4((value & 0x0F00) >> 8)
        a = expand4((value & 0xF000) >> 12)
        out[i * 4 : i * 4 + 4] = bytes((r, g, b, a))
    return bytes(out)

def decode_rgb24(data: bytes, width: int, height: int) -> bytes:
    size = width * height
    need(data, size * 3, "RGB24")
    out = bytearray(size * 4)
    for i in range(size):
        src = i * 3
        dst = i * 4
        out[dst : dst + 4] = bytes((data[src], data[src + 1], data[src + 2], 255))
    return bytes(out)

def decode_rgba32(data: bytes, width: int, height: int) -> bytes:
    size = width * height
    need(data, size * 4, "RGBA32")
    return bytes(data[: size * 4])

def decode_argb32(data: bytes, width: int, height: int) -> bytes:
    size = width * height
    need(data, size * 4, "ARGB32")
    out = bytearray(size * 4)
    for i in range(size):
        src = i * 4
        dst = i * 4
        out[dst : dst + 4] = bytes((data[src + 1], data[src + 2], data[src + 3], data[src]))
    return bytes(out)

def decode_rgb565(data: bytes, width: int, height: int) -> bytes:
    size = width * height
    need(data, size * 2, "RGB565")
    out = bytearray(size * 4)
    for i in range(size):
        value = int.from_bytes(data[i * 2 : i * 2 + 2], "little")
        r = expand5((value >> 11) & 0x1F)
        g = expand6((value >> 5) & 0x3F)
        b = expand5(value & 0x1F)
        out[i * 4 : i * 4 + 4] = bytes((r, g, b, 255))
    return bytes(out)

def decode_rgba4444(data: bytes, width: int, height: int) -> bytes:
    size = width * height
    need(data, size * 2, "RGBA4444")
    out = bytearray(size * 4)
    for i in range(size):
        value = int.from_bytes(data[i * 2 : i * 2 + 2], "little")
        r = expand4((value & 0xF000) >> 12)
        g = expand4((value & 0x0F00) >> 8)
        b = expand4((value & 0x00F0) >> 4)
        a = expand4(value & 0x000F)
        out[i * 4 : i * 4 + 4] = bytes((r, g, b, a))
    return bytes(out)

def decode_bgra32(data: bytes, width: int, height: int) -> bytes:
    size = width * height
    need(data, size * 4, "BGRA32")
    out = bytearray(size * 4)
    for i in range(size):
        src = i * 4
        dst = i * 4
        out[dst : dst + 4] = bytes((data[src + 2], data[src + 1], data[src], data[src + 3]))
    return bytes(out)

def decode_bgr24(data: bytes, width: int, height: int) -> bytes:
    size = width * height
    need(data, size * 3, "BGR24")
    out = bytearray(size * 4)
    for i in range(size):
        src = i * 3
        dst = i * 4
        out[dst : dst + 4] = bytes((data[src + 2], data[src + 1], data[src], 255))
    return bytes(out)

def decode_r8(data: bytes, width: int, height: int) -> bytes:
    size = width * height
    need(data, size, "R8")
    out = bytearray(size * 4)
    for i in range(size):
        dst = i * 4
        out[dst : dst + 4] = bytes((data[i], 0, 0, 255))
    return bytes(out)

def decode_r16(data: bytes, width: int, height: int) -> bytes:
    size = width * height
    need(data, size * 2, "R16")
    out = bytearray(size * 4)
    for i in range(size):
        r = downscale16(int.from_bytes(data[i * 2 : i * 2 + 2], "little"))
        out[i * 4 : i * 4 + 4] = bytes((r, 0, 0, 255))
    return bytes(out)

def decode_rg16(data: bytes, width: int, height: int) -> bytes:
    size = width * height
    need(data, size * 2, "RG16")
    out = bytearray(size * 4)
    for i in range(size):
        src = i * 2
        out[i * 4 : i * 4 + 4] = bytes((data[src], data[src + 1], 0, 255))
    return bytes(out)

def decode_rg32(data: bytes, width: int, height: int) -> bytes:
    size = width * height
    need(data, size * 4, "RG32")
    out = bytearray(size * 4)
    for i in range(size):
        src = i * 4
        r = downscale16(int.from_bytes(data[src : src + 2], "little"))
        g = downscale16(int.from_bytes(data[src + 2 : src + 4], "little"))
        out[i * 4 : i * 4 + 4] = bytes((r, g, 0, 255))
    return bytes(out)

def decode_rgb48(data: bytes, width: int, height: int) -> bytes:
    size = width * height
    need(data, size * 6, "RGB48")
    out = bytearray(size * 4)
    for i in range(size):
        src = i * 6
        r = downscale16(int.from_bytes(data[src : src + 2], "little"))
        g = downscale16(int.from_bytes(data[src + 2 : src + 4], "little"))
        b = downscale16(int.from_bytes(data[src + 4 : src + 6], "little"))
        out[i * 4 : i * 4 + 4] = bytes((r, g, b, 255))
    return bytes(out)

def decode_rgba64(data: bytes, width: int, height: int) -> bytes:
    size = width * height
    need(data, size * 8, "RGBA64")
    out = bytearray(size * 4)
    for i in range(size):
        src = i * 8
        r = downscale16(int.from_bytes(data[src : src + 2], "little"))
        g = downscale16(int.from_bytes(data[src + 2 : src + 4], "little"))
        b = downscale16(int.from_bytes(data[src + 4 : src + 6], "little"))
        a = downscale16(int.from_bytes(data[src + 6 : src + 8], "little"))
        out[i * 4 : i * 4 + 4] = bytes((r, g, b, a))
    return bytes(out)

def decode_r_half(data: bytes, width: int, height: int) -> bytes:
    size = width * height
    need(data, size * 2, "RHalf")
    out = bytearray(size * 4)
    for i in range(size):
        r = float_to_byte(struct.unpack("<e", data[i * 2 : i * 2 + 2])[0])
        out[i * 4 : i * 4 + 4] = bytes((r, 0, 0, 255))
    return bytes(out)

def decode_rg_half(data: bytes, width: int, height: int) -> bytes:
    size = width * height
    need(data, size * 4, "RGHalf")
    out = bytearray(size * 4)
    for i in range(size):
        src = i * 4
        r = float_to_byte(struct.unpack("<e", data[src : src + 2])[0])
        g = float_to_byte(struct.unpack("<e", data[src + 2 : src + 4])[0])
        out[i * 4 : i * 4 + 4] = bytes((r, g, 0, 255))
    return bytes(out)

def decode_rgba_half(data: bytes, width: int, height: int) -> bytes:
    size = width * height
    need(data, size * 8, "RGBAHalf")
    out = bytearray(size * 4)
    for i in range(size):
        src = i * 8
        r = float_to_byte(struct.unpack("<e", data[src : src + 2])[0])
        g = float_to_byte(struct.unpack("<e", data[src + 2 : src + 4])[0])
        b = float_to_byte(struct.unpack("<e", data[src + 4 : src + 6])[0])
        a = float_to_byte(struct.unpack("<e", data[src + 6 : src + 8])[0])
        out[i * 4 : i * 4 + 4] = bytes((r, g, b, a))
    return bytes(out)

def decode_r_float(data: bytes, width: int, height: int) -> bytes:
    size = width * height
    need(data, size * 4, "RFloat")
    out = bytearray(size * 4)
    for i in range(size):
        r = float_to_byte(struct.unpack("<f", data[i * 4 : i * 4 + 4])[0])
        out[i * 4 : i * 4 + 4] = bytes((r, 0, 0, 255))
    return bytes(out)

def decode_rg_float(data: bytes, width: int, height: int) -> bytes:
    size = width * height
    need(data, size * 8, "RGFloat")
    out = bytearray(size * 4)
    for i in range(size):
        src = i * 8
        r = float_to_byte(struct.unpack("<f", data[src : src + 4])[0])
        g = float_to_byte(struct.unpack("<f", data[src + 4 : src + 8])[0])
        out[i * 4 : i * 4 + 4] = bytes((r, g, 0, 255))
    return bytes(out)

def decode_rgba_float(data: bytes, width: int, height: int) -> bytes:
    size = width * height
    need(data, size * 16, "RGBAFloat")
    out = bytearray(size * 4)
    for i in range(size):
        src = i * 16
        r = float_to_byte(struct.unpack("<f", data[src : src + 4])[0])
        g = float_to_byte(struct.unpack("<f", data[src + 4 : src + 8])[0])
        b = float_to_byte(struct.unpack("<f", data[src + 8 : src + 12])[0])
        a = float_to_byte(struct.unpack("<f", data[src + 12 : src + 16])[0])
        out[i * 4 : i * 4 + 4] = bytes((r, g, b, a))
    return bytes(out)

def decode_argb_float(data: bytes, width: int, height: int) -> bytes:
    size = width * height
    need(data, size * 16, "ARGBFloat")
    out = bytearray(size * 4)
    for i in range(size):
        src = i * 16
        a = float_to_byte(struct.unpack("<f", data[src : src + 4])[0])
        r = float_to_byte(struct.unpack("<f", data[src + 4 : src + 8])[0])
        g = float_to_byte(struct.unpack("<f", data[src + 8 : src + 12])[0])
        b = float_to_byte(struct.unpack("<f", data[src + 12 : src + 16])[0])
        out[i * 4 : i * 4 + 4] = bytes((r, g, b, a))
    return bytes(out)

def decode_rgb_float(data: bytes, width: int, height: int) -> bytes:
    size = width * height
    need(data, size * 12, "RGBFloat")
    out = bytearray(size * 4)
    for i in range(size):
        src = i * 12
        r = float_to_byte(struct.unpack("<f", data[src : src + 4])[0])
        g = float_to_byte(struct.unpack("<f", data[src + 4 : src + 8])[0])
        b = float_to_byte(struct.unpack("<f", data[src + 8 : src + 12])[0])
        out[i * 4 : i * 4 + 4] = bytes((r, g, b, 255))
    return bytes(out)

def decode_yuy2(data: bytes, width: int, height: int) -> bytes:
    need(data, width * height * 2, "YUY2")
    out = bytearray(width * height * 4)
    p = 0
    o = 0
    for y in range(height):
        for x in range(0, width, 2):
            y0 = data[p]
            u = data[p + 1]
            y1 = data[p + 2]
            v = data[p + 3]
            p += 4
            out[o : o + 4] = bytes((*yuv_to_rgb(y0, u, v), 255))
            out[o + 4 : o + 8] = bytes((*yuv_to_rgb(y1, u, v), 255))
            o += 8
    return bytes(out)

def decode_rgb9e5(data: bytes, width: int, height: int) -> bytes:
    size = width * height
    need(data, size * 4, "RGB9e5Float")
    out = bytearray(size * 4)
    for i in range(size):
        n = int.from_bytes(data[i * 4 : i * 4 + 4], "little")
        scale = (n >> 27) & 0x1F
        scale_f = 2 ** (scale - 24)
        r = n & 0x1FF
        g = (n >> 9) & 0x1FF
        b = (n >> 18) & 0x1FF
        out[i * 4 : i * 4 + 4] = bytes((float_to_byte(r * scale_f), float_to_byte(g * scale_f), float_to_byte(b * scale_f), 255))
    return bytes(out)

def decode_dxt1_python(data: bytes, width: int, height: int) -> bytes:
    block_w = (width + 3) // 4
    block_h = (height + 3) // 4
    need(data, block_w * block_h * 8, "DXT1")
    out = bytearray(width * height * 4)
    offset = 0
    for by in range(block_h):
        for bx in range(block_w):
            c0 = int.from_bytes(data[offset : offset + 2], "little")
            c1 = int.from_bytes(data[offset + 2 : offset + 4], "little")
            codes = int.from_bytes(data[offset + 4 : offset + 8], "little")
            offset += 8
            colors = dxt1_palette(c0, c1, allow_transparent=True)
            write_dxt_color_block(out, width, height, bx, by, colors, codes)
    return bytes(out)

def decode_dxt3_python(data: bytes, width: int, height: int) -> bytes:
    block_w = (width + 3) // 4
    block_h = (height + 3) // 4
    need(data, block_w * block_h * 16, "DXT3")
    out = bytearray(width * height * 4)
    offset = 0
    for by in range(block_h):
        for bx in range(block_w):
            alpha = int.from_bytes(data[offset : offset + 8], "little")
            c0 = int.from_bytes(data[offset + 8 : offset + 10], "little")
            c1 = int.from_bytes(data[offset + 10 : offset + 12], "little")
            codes = int.from_bytes(data[offset + 12 : offset + 16], "little")
            offset += 16
            colors = dxt1_palette(c0, c1, allow_transparent=False)
            for py in range(4):
                y = by * 4 + py
                if y >= height:
                    continue
                for px in range(4):
                    x = bx * 4 + px
                    if x >= width:
                        continue
                    pixel_index = py * 4 + px
                    color_index = (codes >> (2 * pixel_index)) & 0x03
                    a = expand4((alpha >> (4 * pixel_index)) & 0x0F)
                    r, g, b, old_a = colors[color_index]
                    dst = (y * width + x) * 4
                    out[dst : dst + 4] = bytes((r, g, b, a))
    return bytes(out)

def decode_dxt5_python(data: bytes, width: int, height: int) -> bytes:
    block_w = (width + 3) // 4
    block_h = (height + 3) // 4
    need(data, block_w * block_h * 16, "DXT5")
    out = bytearray(width * height * 4)
    offset = 0
    for by in range(block_h):
        for bx in range(block_w):
            a0 = data[offset]
            a1 = data[offset + 1]
            alpha_bits = int.from_bytes(data[offset + 2 : offset + 8], "little")
            c0 = int.from_bytes(data[offset + 8 : offset + 10], "little")
            c1 = int.from_bytes(data[offset + 10 : offset + 12], "little")
            codes = int.from_bytes(data[offset + 12 : offset + 16], "little")
            offset += 16
            colors = dxt1_palette(c0, c1, allow_transparent=False)
            alphas = dxt5_alpha_palette(a0, a1)
            for py in range(4):
                y = by * 4 + py
                if y >= height:
                    continue
                for px in range(4):
                    x = bx * 4 + px
                    if x >= width:
                        continue
                    pixel_index = py * 4 + px
                    color_index = (codes >> (2 * pixel_index)) & 0x03
                    alpha_index = (alpha_bits >> (3 * pixel_index)) & 0x07
                    r, g, b, a = colors[color_index]
                    dst = (y * width + x) * 4
                    out[dst : dst + 4] = bytes((r, g, b, alphas[alpha_index]))
    return bytes(out)

def decode_bc4(data: bytes, width: int, height: int, *, signed: bool) -> bytes:
    block_w = (width + 3) // 4
    block_h = (height + 3) // 4
    need(data, block_w * block_h * 8, "BC4")
    out = bytearray(width * height * 4)
    offset = 0
    for by in range(block_h):
        for bx in range(block_w):
            values = bc_alpha_palette(data[offset], data[offset + 1], signed=signed)
            bits = int.from_bytes(data[offset + 2 : offset + 8], "little")
            offset += 8
            for py in range(4):
                y = by * 4 + py
                if y >= height:
                    continue
                for px in range(4):
                    x = bx * 4 + px
                    if x >= width:
                        continue
                    idx = (bits >> (3 * (py * 4 + px))) & 0x07
                    r = values[idx]
                    dst = (y * width + x) * 4
                    out[dst : dst + 4] = bytes((r, r, r, 255))
    return bytes(out)

def decode_bc5(data: bytes, width: int, height: int, *, signed: bool) -> bytes:
    block_w = (width + 3) // 4
    block_h = (height + 3) // 4
    need(data, block_w * block_h * 16, "BC5")
    out = bytearray(width * height * 4)
    offset = 0
    for by in range(block_h):
        for bx in range(block_w):
            r_values = bc_alpha_palette(data[offset], data[offset + 1], signed=signed)
            r_bits = int.from_bytes(data[offset + 2 : offset + 8], "little")
            g_values = bc_alpha_palette(data[offset + 8], data[offset + 9], signed=signed)
            g_bits = int.from_bytes(data[offset + 10 : offset + 16], "little")
            offset += 16
            for py in range(4):
                y = by * 4 + py
                if y >= height:
                    continue
                for px in range(4):
                    x = bx * 4 + px
                    if x >= width:
                        continue
                    pixel = py * 4 + px
                    r = r_values[(r_bits >> (3 * pixel)) & 0x07]
                    g = g_values[(g_bits >> (3 * pixel)) & 0x07]
                    dst = (y * width + x) * 4
                    out[dst : dst + 4] = bytes((r, g, 0, 255))
    return bytes(out)

def decode_dxt1(data: bytes, width: int, height: int) -> bytes:
    need(data, block_level_size(width, height, 8), "DXT1")
    native = decode_block_compressed_native(data, width, height, b"DXT1", 8)
    return native if native is not None else decode_dxt1_python(data, width, height)

def decode_dxt3(data: bytes, width: int, height: int) -> bytes:
    need(data, block_level_size(width, height, 16), "DXT3")
    native = decode_block_compressed_native(data, width, height, b"DXT3", 16)
    return native if native is not None else decode_dxt3_python(data, width, height)

def decode_dxt5(data: bytes, width: int, height: int) -> bytes:
    need(data, block_level_size(width, height, 16), "DXT5")
    native = decode_block_compressed_native(data, width, height, b"DXT5", 16)
    return native if native is not None else decode_dxt5_python(data, width, height)

def block_level_size(width: int, height: int, block_bytes: int) -> int:
    return ((width + 3) // 4) * ((height + 3) // 4) * block_bytes

def decode_block_compressed_native(
    data: bytes, width: int, height: int, fourcc: bytes, block_bytes: int
) -> bytes | None:
    if width % 4 or height % 4:
        return None
    from PIL import Image

    level = block_level_size(width, height, block_bytes)
    try:
        image = Image.open(io.BytesIO(dds_fourcc_header(width, height, level, fourcc) + data[:level]))
        rgba = image.convert("RGBA").tobytes()
    except Exception:
        return None
    if len(rgba) != width * height * 4:
        return None
    return rgba

def decode_dds_via_pillow(data: bytes, width: int, height: int, *, dxgi_format: int) -> bytes:
    from PIL import Image

    dds = dds_dx10_header(width, height, len(data), dxgi_format) + data
    try:
        image = Image.open(io.BytesIO(dds)).convert("RGBA")
    except Exception as exc:
        raise TextureDecodeError(f"Pillow DDS decode failed for DXGI {dxgi_format}: {exc}") from exc
    return image.tobytes()

def decode_crunch(data: bytes, width: int, height: int, texture_format: int) -> bytes:
    if texture_format in (28, 29):
        from .crunch import CrunchDecodeError, decode_crn_to_dxt

        try:
            block_data, header = decode_crn_to_dxt(data, expected_unity_format=texture_format)
        except CrunchDecodeError as exc:
            raise TextureDecodeError(f"{format_name(texture_format)} decode failed: {exc}") from exc
        if header.width != width or header.height != height:
            raise TextureDecodeError(
                f"{format_name(texture_format)} CRN header is {header.width}x{header.height}, "
                f"Texture2D object is {width}x{height}"
            )
        if texture_format == 28:
            return decode_dxt1(block_data, width, height)
        return decode_dxt5(block_data, width, height)

    raise TextureDecodeError(
        f"{format_name(texture_format)} is a mobile Crunch format that RWBY GE doesnt use"
    )

def write_dxt_color_block(out: bytearray, width: int, height: int, bx: int, by: int, colors, codes: int) -> None:
    for py in range(4):
        y = by * 4 + py
        if y >= height:
            continue
        for px in range(4):
            x = bx * 4 + px
            if x >= width:
                continue
            pixel_index = py * 4 + px
            color_index = (codes >> (2 * pixel_index)) & 0x03
            dst = (y * width + x) * 4
            out[dst : dst + 4] = bytes(colors[color_index])

def dxt1_palette(c0: int, c1: int, *, allow_transparent: bool):
    r0, g0, b0 = rgb565_tuple(c0)
    r1, g1, b1 = rgb565_tuple(c1)
    colors = [
        (r0, g0, b0, 255),
        (r1, g1, b1, 255),
    ]
    if c0 > c1 or not allow_transparent:
        colors.append(((2 * r0 + r1) // 3, (2 * g0 + g1) // 3, (2 * b0 + b1) // 3, 255))
        colors.append(((r0 + 2 * r1) // 3, (g0 + 2 * g1) // 3, (b0 + 2 * b1) // 3, 255))
    else:
        colors.append(((r0 + r1) // 2, (g0 + g1) // 2, (b0 + b1) // 2, 255))
        colors.append((0, 0, 0, 0))
    return colors

def dxt5_alpha_palette(a0: int, a1: int) -> list[int]:
    values = [a0, a1]
    if a0 > a1:
        values.extend(
            [
                (6 * a0 + a1) // 7,
                (5 * a0 + 2 * a1) // 7,
                (4 * a0 + 3 * a1) // 7,
                (3 * a0 + 4 * a1) // 7,
                (2 * a0 + 5 * a1) // 7,
                (a0 + 6 * a1) // 7,
            ]
        )
    else:
        values.extend(
            [
                (4 * a0 + a1) // 5,
                (3 * a0 + 2 * a1) // 5,
                (2 * a0 + 3 * a1) // 5,
                (a0 + 4 * a1) // 5,
                0,
                255,
            ]
        )
    return values

def bc_alpha_palette(a0: int, a1: int, *, signed: bool) -> list[int]:
    if signed:
        a0 = signed_to_unorm(a0)
        a1 = signed_to_unorm(a1)
    return dxt5_alpha_palette(a0, a1)

def signed_to_unorm(value: int) -> int:
    signed = value - 256 if value > 127 else value
    return clamp_byte(round((max(signed / 127.0, -1.0) * 0.5 + 0.5) * 255))

def rgb565_tuple(value: int) -> tuple[int, int, int]:
    return (expand5((value >> 11) & 0x1F), expand6((value >> 5) & 0x3F), expand5(value & 0x1F))

def yuv_to_rgb(y: int, u: int, v: int) -> tuple[int, int, int]:
    c = y - 16
    d = u - 128
    e = v - 128
    r = clamp_byte((298 * c + 409 * e + 128) >> 8)
    g = clamp_byte((298 * c - 100 * d - 208 * e + 128) >> 8)
    b = clamp_byte((298 * c + 516 * d + 128) >> 8)
    return r, g, b

def dds_fourcc_header(width: int, height: int, data_size: int, fourcc: bytes) -> bytes:
    return dds_header(width, height, data_size, fourcc)

def dds_dx10_header(width: int, height: int, data_size: int, dxgi_format: int) -> bytes:
    return dds_header(width, height, data_size, b"DX10", dxgi_format)

def dds_header(width: int, height: int, data_size: int, fourcc: bytes, dxgi_format: int | None = None) -> bytes:
    magic = b"DDS "
    header_size = 124
    flags = 0x00021007
    pitch_or_linear = data_size
    depth = 0
    mipmap_count = 1
    reserved1 = b"\x00" * 44
    pf_size = 32
    pf_flags = 0x00000004
    pf_fourcc = int.from_bytes(fourcc, "little")
    pf_rgb_bit_count = 0
    masks = (0, 0, 0, 0)
    caps = 0x1000
    caps2 = caps3 = caps4 = reserved2 = 0
    header = struct.pack(
        "<I I I I I I I 44s I I I I I I I I I I I I I",
        header_size,
        flags,
        height,
        width,
        pitch_or_linear,
        depth,
        mipmap_count,
        reserved1,
        pf_size,
        pf_flags,
        pf_fourcc,
        pf_rgb_bit_count,
        *masks,
        caps,
        caps2,
        caps3,
        caps4,
        reserved2,
    )
    if dxgi_format is None:
        return magic + header
    return magic + header + struct.pack("<I I I I I", dxgi_format, 3, 0, 1, 0)

def downscale16(component: int) -> int:
    return ((component * 255) + 32895) >> 16

def float_to_byte(value: float) -> int:
    if not math.isfinite(value):
        return 0
    return clamp_byte(round(value * 255))

def clamp_byte(value: int) -> int:
    if value < 0:
        return 0
    if value > 255:
        return 255
    return int(value)

def expand4(value: int) -> int:
    return (value << 4) | value

def expand5(value: int) -> int:
    return (value << 3) | (value >> 2)

def expand6(value: int) -> int:
    return (value << 2) | (value >> 4)

