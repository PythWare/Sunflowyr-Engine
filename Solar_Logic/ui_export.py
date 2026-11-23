from __future__ import annotations
import json
from pathlib import Path
from typing import Any
from .binary import EndianReader
from .material_export import read_pptr
from .serialized import SerializedAsset, SerializedObject
from .paths import ensure_parent

class UiExportError(ValueError):
    pass

UI_CLASS_KINDS = {
    222: "canvas_renderer_json",
    223: "canvas_json",
    224: "rect_transform_json",
    225: "canvas_group_json",
}

def export_ui_component_json(
    asset: SerializedAsset,
    obj: SerializedObject,
    out_path: Path,
    metadata: dict[str, Any],
    game_object_name: str = "",
) -> dict[str, Any]:
    with asset.path.open("rb") as handle:
        reader = EndianReader(handle, asset.endian)
        document, conversion = read_ui_component_json(asset, reader, obj, game_object_name)

    ensure_parent(out_path)
    with out_path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(document, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    conversion["gameObjectName"] = game_object_name
    metadata["conversion"] = conversion
    metadata["writeback_note"] = (
        f"{obj.type_name} JSON is editable and can be rebuilt into Unity {obj.type_name} object data."
    )
    return conversion

def read_ui_component_json(
    asset: SerializedAsset,
    reader: EndianReader,
    obj: SerializedObject,
    game_object_name: str = "",
) -> tuple[dict[str, Any], dict[str, Any]]:
    reader.seek(obj.byte_start)
    game_object = read_pptr(reader, asset)
    document: dict[str, Any] = {
        "m_GameObject": game_object,
        "solarGameObjectName": game_object_name,
    }

    if obj.class_id == 222:
        conversion_format = "canvas_renderer_json_unity5"
    elif obj.class_id == 223:
        document.update(read_canvas(reader, asset))
        conversion_format = "canvas_json_unity5"
    elif obj.class_id == 224:
        document.update(read_rect_transform(reader, asset))
        conversion_format = "rect_transform_json_unity5"
    elif obj.class_id == 225:
        document.update(read_canvas_group(reader))
        conversion_format = "canvas_group_json_unity5"
    else:
        raise UiExportError(f"unsupported UI class {obj.class_id}")

    object_end = obj.byte_start + obj.byte_size
    if reader.tell() != object_end:
        raise UiExportError(
            f"{obj.type_name} reader ended at 0x{reader.tell():x}, expected 0x{object_end:x}"
        )
    return document, {
        "format": conversion_format,
        "classId": obj.class_id,
        "className": obj.type_name,
    }

def read_canvas(reader: EndianReader, asset: SerializedAsset) -> dict[str, Any]:
    enabled = reader.bool()
    reader.align()
    return {
        "m_Enabled": enabled,
        "m_RenderMode": reader.i32(),
        "m_Camera": read_pptr(reader, asset),
        "m_PlaneDistance": reader.f32(),
        "m_PixelPerfect": reader.bool(),
        "m_ReceivesEvents": reader.bool(),
        "m_OverrideSorting": reader.bool(),
        "m_OverridePixelPerfect": reader.bool(),
        "m_SortingBucketNormalizedSize": reader.f32(),
        "m_SortingLayerID": reader.i32(),
        "m_SortingOrder": reader.i16(),
        "m_TargetDisplay": reader.u8(),
    }

def read_rect_transform(reader: EndianReader, asset: SerializedAsset) -> dict[str, Any]:
    local_rotation = read_vector(reader, 4)
    local_position = read_vector(reader, 3)
    local_scale = read_vector(reader, 3)
    child_count = reader.i32()
    if child_count < 0 or child_count > 1_000_000:
        raise UiExportError(f"invalid RectTransform child count {child_count}")
    children = [read_pptr(reader, asset) for _ in range(child_count)]
    return {
        "m_LocalRotation": local_rotation,
        "m_LocalPosition": local_position,
        "m_LocalScale": local_scale,
        "m_Children": children,
        "m_Father": read_pptr(reader, asset),
        "m_AnchorMin": read_vector(reader, 2),
        "m_AnchorMax": read_vector(reader, 2),
        "m_AnchoredPosition": read_vector(reader, 2),
        "m_SizeDelta": read_vector(reader, 2),
        "m_Pivot": read_vector(reader, 2),
    }

def read_canvas_group(reader: EndianReader) -> dict[str, Any]:
    enabled = reader.bool()
    reader.align()
    result = {
        "m_Enabled": enabled,
        "m_Alpha": reader.f32(),
        "m_Interactable": reader.bool(),
        "m_BlocksRaycasts": reader.bool(),
        "m_IgnoreParentGroups": reader.bool(),
    }
    reader.align()
    return result

def read_vector(reader: EndianReader, length: int) -> list[float]:
    return [reader.f32() for _ in range(length)]
