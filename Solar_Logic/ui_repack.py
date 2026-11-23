from __future__ import annotations
import json
from pathlib import Path
from typing import Any
from .mono_repack import EndianWriter, taildata_value, write_pptr
from .serialized import load_serialized_asset

class UiRepackError(ValueError):
    pass

def repack_ui_component_json(json_path: Path, taildata: dict[str, Any], data_root: Path) -> bytes:
    document = json.loads(Path(json_path).read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise UiRepackError("UI component JSON root must be an object")

    container = str(taildata_value(taildata, "container") or "")
    if not container:
        raise UiRepackError("taildata record doesnt name a Unity container")
    asset_path = (Path(data_root) / container).resolve()
    asset = load_serialized_asset(asset_path, Path(data_root))
    class_id = int(taildata_value(taildata, "classId", 0) or 0)
    writer = EndianWriter(asset.endian)
    write_pptr(writer, asset, object_dict(document.get("m_GameObject")))

    if class_id == 222:
        pass
    elif class_id == 223:
        write_canvas(writer, asset, document)
    elif class_id == 224:
        write_rect_transform(writer, asset, document)
    elif class_id == 225:
        write_canvas_group(writer, document)
    else:
        raise UiRepackError(f"unsupported UI class {class_id}")
    return writer.bytes()

def write_canvas(writer: EndianWriter, asset: Any, document: dict[str, Any]) -> None:
    writer.bool(document.get("m_Enabled", True))
    writer.align()
    writer.i32(document.get("m_RenderMode", 0))
    write_pptr(writer, asset, object_dict(document.get("m_Camera")))
    writer.f32(document.get("m_PlaneDistance", 100.0))
    writer.bool(document.get("m_PixelPerfect", False))
    writer.bool(document.get("m_ReceivesEvents", True))
    writer.bool(document.get("m_OverrideSorting", False))
    writer.bool(document.get("m_OverridePixelPerfect", False))
    writer.f32(document.get("m_SortingBucketNormalizedSize", 0.0))
    writer.i32(document.get("m_SortingLayerID", document.get("m_AdditionalShaderChannelsFlag", 0)))
    writer.i16(document.get("m_SortingOrder", 0))
    writer.u8(document.get("m_TargetDisplay", 0))

def write_rect_transform(writer: EndianWriter, asset: Any, document: dict[str, Any]) -> None:
    write_vector(writer, document.get("m_LocalRotation"), 4, (0.0, 0.0, 0.0, 1.0))
    write_vector(writer, document.get("m_LocalPosition"), 3, (0.0, 0.0, 0.0))
    write_vector(writer, document.get("m_LocalScale"), 3, (1.0, 1.0, 1.0))
    children = document.get("m_Children")
    if not isinstance(children, list):
        children = []
    writer.i32(len(children))
    for child in children:
        write_pptr(writer, asset, object_dict(child))
    write_pptr(writer, asset, object_dict(document.get("m_Father")))
    write_vector(writer, document.get("m_AnchorMin"), 2, (0.0, 0.0))
    write_vector(writer, document.get("m_AnchorMax"), 2, (1.0, 1.0))
    write_vector(writer, document.get("m_AnchoredPosition"), 2, (0.0, 0.0))
    write_vector(writer, document.get("m_SizeDelta"), 2, (0.0, 0.0))
    write_vector(writer, document.get("m_Pivot"), 2, (0.5, 0.5))

def write_canvas_group(writer: EndianWriter, document: dict[str, Any]) -> None:
    writer.bool(document.get("m_Enabled", True))
    writer.align()
    writer.f32(document.get("m_Alpha", 1.0))
    writer.bool(document.get("m_Interactable", True))
    writer.bool(document.get("m_BlocksRaycasts", True))
    writer.bool(document.get("m_IgnoreParentGroups", False))
    writer.align()

def write_vector(
    writer: EndianWriter,
    value: Any,
    length: int,
    default: tuple[float, ...],
) -> None:
    values = value if isinstance(value, list) else list(default)
    for index in range(length):
        writer.f32(values[index] if index < len(values) else default[index])


def object_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}
