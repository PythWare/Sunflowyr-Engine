from __future__ import annotations
import json, struct
from pathlib import Path
from typing import Any
from .asset_bundle_writer import align_buffer, pack_aligned_string

RGBA32_FORMAT = 4
DXT1_FORMAT = 10
DXT5_FORMAT = 12
TEXTURE2D_DIMENSION = 2
FORMAT_NAMES = {RGBA32_FORMAT: "RGBA32", DXT1_FORMAT: "DXT1", DXT5_FORMAT: "DXT5"}
SIDECAR_SUFFIX = ".suntex"
MAX_TEXTURE_SIZE = 0x4000
WRAP_MODES = {"repeat": 0, "clamp": 1}

class NewTextureAssetError(ValueError):
    pass

def build_texture_asset(image_path: Path, name: str) -> tuple[bytes, dict[str, object]]:
    try:
        from PIL import Image
    except ImportError as exc:
        raise NewTextureAssetError("Pillow is required for new Texture2D assets") from exc
    try:
        with Image.open(image_path) as source:
            image = source.convert("RGBA")
    except Exception as exc:
        raise NewTextureAssetError(f"couldnt read image: {exc}") from exc
    if image.width <= 0 or image.height <= 0:
        raise NewTextureAssetError("image dimensions must be positive")
    if image.width > MAX_TEXTURE_SIZE or image.height > MAX_TEXTURE_SIZE:
        raise NewTextureAssetError(
            f"image is {image.width}x{image.height}, Unity 5.4 accepts up to {MAX_TEXTURE_SIZE} pixels per side"
        )
    image = image.transpose(Image.Transpose.FLIP_TOP_BOTTOM)

    settings = read_texture_sidecar(image_path)
    texture_format = resolve_texture_format(settings["format"], image)
    mipmaps = resolve_mipmaps(settings["mipmaps"], image.width, image.height)

    levels = build_mip_levels(image, mipmaps)
    encoded_levels = [encode_level(level, texture_format) for level in levels]
    payload = build_texture2d_object(
        name,
        image.width,
        image.height,
        texture_format,
        encoded_levels,
        WRAP_MODES[settings["wrap"]],
    )
    return payload, {
        "sourceFormat": image_path.suffix.lower().lstrip("."),
        "textureFormat": FORMAT_NAMES[texture_format],
        "width": image.width,
        "height": image.height,
        "mipCount": len(encoded_levels),
        "isReadable": True,
        "settingsSidecar": image_path.name + SIDECAR_SUFFIX if settings["from_sidecar"] else None,
    }

def read_texture_sidecar(image_path: Path) -> dict[str, Any]:
    sidecar_path = image_path.with_name(image_path.name + SIDECAR_SUFFIX)
    if not sidecar_path.is_file():
        return {"format": "auto", "mipmaps": "auto", "wrap": "clamp", "from_sidecar": False}
    try:
        document = json.loads(sidecar_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise NewTextureAssetError(f"couldnt read {sidecar_path.name}: {exc}") from exc
    if not isinstance(document, dict):
        raise NewTextureAssetError(f"{sidecar_path.name} root must be an object")
    format_value = str(document.get("format", "auto")).strip().upper()
    if format_value in {"AUTO", ""}:
        format_value = "auto"
    elif format_value not in {"RGBA32", "DXT1", "DXT5"}:
        raise NewTextureAssetError(
            f'{sidecar_path.name} format must be "auto", "RGBA32", "DXT1", or "DXT5"'
        )
    mip_value = document.get("mipmaps", "auto")
    if not isinstance(mip_value, bool):
        if str(mip_value).strip().lower() not in {"auto", ""}:
            raise NewTextureAssetError(f'{sidecar_path.name} mipmaps must be true, false, or "auto"')
        mip_value = "auto"
    wrap_value = str(document.get("wrap", "clamp")).strip().lower() or "clamp"
    if wrap_value not in WRAP_MODES:
        raise NewTextureAssetError(f'{sidecar_path.name} wrap must be "clamp" or "repeat"')
    return {"format": format_value, "mipmaps": mip_value, "wrap": wrap_value, "from_sidecar": True}

def resolve_texture_format(requested: str, image: Any) -> int:
    dxt_capable = (
        image.width % 4 == 0
        and image.height % 4 == 0
        and image.width >= 4
        and image.height >= 4
    )
    if requested == "RGBA32":
        return RGBA32_FORMAT
    if requested in {"DXT1", "DXT5"}:
        if not dxt_capable:
            raise NewTextureAssetError(
                f"{requested} needs dimensions that are multiples of four, "
                f"found {image.width}x{image.height}"
            )
        return DXT1_FORMAT if requested == "DXT1" else DXT5_FORMAT
    if not dxt_capable:
        return RGBA32_FORMAT
    alpha_extremes = image.getextrema()[3]
    return DXT5_FORMAT if alpha_extremes[0] < 255 else DXT1_FORMAT

def resolve_mipmaps(requested: Any, width: int, height: int) -> bool:
    if isinstance(requested, bool):
        return requested
    return max(width, height) > 4

def build_mip_levels(image: Any, mipmaps: bool) -> list[Any]:
    from PIL import Image

    levels = [image]
    if not mipmaps:
        return levels
    width, height = image.width, image.height
    while width > 1 or height > 1:
        width = max(1, width // 2)
        height = max(1, height // 2)
        levels.append(image.resize((width, height), Image.Resampling.LANCZOS))
    return levels

def encode_level(level: Any, texture_format: int) -> bytes:
    rgba = level.tobytes()
    if texture_format == RGBA32_FORMAT:
        return rgba
    from .texture_repack import encode_dxt1, encode_dxt5

    if texture_format == DXT1_FORMAT:
        return encode_dxt1(rgba, level.width, level.height)
    return encode_dxt5(rgba, level.width, level.height)

def build_texture2d_object(
    name: str,
    width: int,
    height: int,
    texture_format: int,
    mip_levels: list[bytes],
    wrap_mode: int = 1,
) -> bytes:
    if not mip_levels:
        raise NewTextureAssetError("texture needs at least one mip level")
    image_data = b"".join(mip_levels)
    output = bytearray(pack_aligned_string(name))
    output.extend(
        struct.pack(
            "<iiiii",
            width,
            height,
            len(image_data),
            texture_format,
            len(mip_levels),
        )
    )
    output.extend(b"\1\1")
    align_buffer(output, 4)
    output.extend(struct.pack("<iiii", 1, TEXTURE2D_DIMENSION, 1, 1))
    output.extend(struct.pack("<f", 0.0))
    output.extend(struct.pack("<iiii", wrap_mode, 6, 1, len(image_data)))
    output.extend(image_data)
    align_buffer(output, 4)
    output.extend(struct.pack("<II", 0, 0))
    output.extend(pack_aligned_string(""))
    return bytes(output)
