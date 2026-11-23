from __future__ import annotations
import base64, json
from functools import lru_cache
from pathlib import Path
from typing import Any
from .class_ids import class_name
from .unity_typetree_codec import load_schema, read_typetree, write_typetree

SETTINGS_FORMAT = "sunflowyr-media-settings"
SETTINGS_SUFFIX = ".settings.json"
BAKED_SUFFIX = "_collision.bin"
SCHEMAS = {28: "texture2d_schema.json", 89: "cubemap_schema.json", 43: "mesh_schema.json", 83: "audio_clip_schema.json"}
TEXTURE_CONTENT = {"m_Width", "m_Height", "m_CompleteImageSize", "m_TextureFormat", "m_MipCount", "m_ImageCount", "image data", "m_StreamData"}
CONTENT_FIELDS = {
    28: TEXTURE_CONTENT,
    89: TEXTURE_CONTENT,
    43: {"m_SubMeshes", "m_Shapes", "m_BindPose", "m_BoneNameHashes", "m_RootBoneNameHash", "m_IndexBuffer", "m_Skin", "m_VertexData", "m_CompressedMesh", "m_LocalAABB"},
    83: {"m_Channels", "m_Frequency", "m_BitsPerSample", "m_Length", "m_Resource", "m_CompressionFormat"},
}
BAKED_FIELDS = {"m_BakedConvexCollisionMesh": "convex", "m_BakedTriangleCollisionMesh": "triangle"}

class MediaSettingsError(ValueError):
    pass

@lru_cache(maxsize=8)
def schema_tree(class_id: int):
    return load_schema(SCHEMAS[class_id])

def settings_path(media_path: Path) -> Path:
    media_path = Path(media_path)
    return media_path.with_name(media_path.stem + SETTINGS_SUFFIX)

def baked_path(media_path: Path, kind: str) -> Path:
    media_path = Path(media_path)
    return media_path.with_name(f"{media_path.stem}.{kind}{BAKED_SUFFIX}")

def is_media_sidecar(path: Path) -> bool:
    name = Path(path).name.lower()
    return name.endswith(SETTINGS_SUFFIX) or name.endswith(BAKED_SUFFIX)

def blob_bytes(value: Any) -> bytes:
    if isinstance(value, dict) and value.get("encoding") == "base64":
        return base64.b64decode(str(value.get("data") or ""))
    return b""

def blob_value(data: bytes) -> dict[str, Any]:
    return {"encoding": "base64", "byteLength": len(data), "data": base64.b64encode(data).decode("ascii")}

def write_media_settings(data: bytes, class_id: int, endian: str, media_path: Path) -> list[Path]:
    if class_id not in SCHEMAS:
        return []
    value = read_typetree(data, schema_tree(class_id), endian)
    fields: dict[str, Any] = {}
    written: list[Path] = []
    for key, item in value.items():
        if key in CONTENT_FIELDS[class_id]:
            continue
        if key in BAKED_FIELDS:
            blob = blob_bytes(item)
            if blob:
                target = baked_path(media_path, BAKED_FIELDS[key])
                target.write_bytes(blob)
                written.append(target)
                fields[key] = {"file": target.name, "size": len(blob)}
            else:
                fields[key] = {"file": None, "size": 0}
            continue
        fields[key] = item
    document = {
        "format": SETTINGS_FORMAT,
        "version": 1,
        "classId": class_id,
        "className": class_name(class_id),
        "note": "Every field of this object that the media file beside it doesnt carry. Edit values here and writeback applies them.",
        "fields": fields,
    }
    target = settings_path(media_path)
    target.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    written.insert(0, target)
    return written

def apply_media_settings(produced: bytes, class_id: int, endian: str, media_path: Path, keep_baked: bool = False) -> bytes:
    source = settings_path(media_path)
    if class_id not in SCHEMAS or not source.is_file():
        return produced
    try:
        document = json.loads(source.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MediaSettingsError(f"{source.name} couldnt be read: {exc}") from exc
    fields = document.get("fields") if isinstance(document, dict) else None
    if not isinstance(fields, dict):
        raise MediaSettingsError(f"{source.name} has no fields object")
    tree = schema_tree(class_id)
    value = read_typetree(produced, tree, endian)
    for key, item in fields.items():
        if key not in value or key in CONTENT_FIELDS[class_id]:
            continue
        if key in BAKED_FIELDS:
            name = item.get("file") if isinstance(item, dict) else None
            if keep_baked and name:
                blob_path = source.with_name(str(name))
                if not blob_path.is_file():
                    raise MediaSettingsError(f"{source.name} names {name}, which is missing")
                value[key] = blob_value(blob_path.read_bytes())
            else:
                value[key] = blob_value(b"")
            continue
        value[key] = item
    return write_typetree(value, tree, endian)
