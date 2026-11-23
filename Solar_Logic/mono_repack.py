from __future__ import annotations
import base64, json, struct
from pathlib import Path
from typing import Any
from .managed_schema import (
    GUI_STYLE_OFFSETS,
    GUI_STYLE_STATES,
    RECT_OFFSET_FIELDS,
    ManagedSchemaError,
    ManagedTypeRegistry,
    shared_registry,
    TypeSig,
    UNITY_SERIALIZABLE_TYPES,
    UNITY_VALUE_TYPES,
)
from .serialized import SerializedAsset, load_serialized_asset

class MonoRepackError(ValueError):
    pass

class EndianWriter:
    def __init__(self, endian: str = "<") -> None:
        self.endian = endian
        self.data = bytearray()

    def tell(self) -> int:
        return len(self.data)

    def write(self, data: bytes) -> None:
        self.data.extend(data)

    def align(self, alignment: int = 4) -> None:
        pad = (-len(self.data)) % alignment
        if pad:
            self.data.extend(b"\0" * pad)

    def u8(self, value: int) -> None:
        self.data.append(int(value) & 0xFF)

    def bool(self, value: Any) -> None:
        self.u8(1 if bool(value) else 0)

    def i8(self, value: int) -> None:
        self.write(struct.pack("b", int(value)))

    def i16(self, value: int) -> None:
        self.write(struct.pack(self.endian + "h", int(value)))

    def u16(self, value: int) -> None:
        self.write(struct.pack(self.endian + "H", int(value) & 0xFFFF))

    def i32(self, value: int) -> None:
        self.write(struct.pack(self.endian + "i", int(value)))

    def u32(self, value: int) -> None:
        self.write(struct.pack(self.endian + "I", int(value) & 0xFFFFFFFF))

    def i64(self, value: int) -> None:
        self.write(struct.pack(self.endian + "q", int(value)))

    def u64(self, value: int) -> None:
        self.write(struct.pack(self.endian + "Q", int(value) & 0xFFFFFFFFFFFFFFFF))

    def f32(self, value: Any) -> None:
        self.write(struct.pack(self.endian + "f", float(value)))

    def f64(self, value: Any) -> None:
        self.write(struct.pack(self.endian + "d", float(value)))

    def aligned_string(self, value: Any) -> None:
        if value is None:
            encoded = b""
        else:
            encoded = str(value).encode("utf-8")
        self.i32(len(encoded))
        self.write(encoded)
        self.align()

    def bytes(self) -> bytes:
        return bytes(self.data)

def mono_script_pointer_offset(asset: SerializedAsset) -> int:
    pointer_size = 4 + (8 if asset.format_version >= 14 else 4)
    return (pointer_size + 1 + 3) & ~3

def repack_monobehaviour_json(json_path: Path, taildata: dict[str, Any], data_root: Path) -> bytes:
    document = json.loads(Path(json_path).read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise MonoRepackError("MonoBehaviour JSON root must be an object")

    container = str(taildata_value(taildata, "container") or "")
    if not container:
        raise MonoRepackError("taildata record doesnt name a Unity container")
    asset_path = (Path(data_root) / container).resolve()
    asset = load_serialized_asset(asset_path, Path(data_root))
    writer = EndianWriter(asset.endian)

    write_pptr(writer, asset, document.get("m_GameObject") or {})
    writer.u8(int(document.get("m_Enabled", 1)))
    writer.align()
    write_pptr(writer, asset, document.get("m_Script") or {})
    writer.aligned_string(document.get("m_Name", ""))

    raw_fields = document.get("solarRawSerializedFields")
    if isinstance(raw_fields, dict) and raw_fields.get("encoding") == "base64":
        data_text = raw_fields.get("data")
        if not isinstance(data_text, str):
            raise MonoRepackError("solarRawSerializedFields.data must be base64 text")
        writer.write(base64.b64decode(data_text))
        return writer.bytes()

    script_name = managed_type_name(document, taildata)
    if not script_name:
        raise MonoRepackError("typed MonoBehaviour JSON doesnt include solarScript.full_name")
    registry = shared_registry(Path(data_root) / "Managed")
    fields = registry.serializable_fields(script_name)
    if not fields and not registry.knows_type(script_name):
        raise MonoRepackError(
            f"the script {script_name!r} wasnt found in {Path(data_root) / 'Managed'}, check that the folder is there"
        )
    if not fields:
        return writer.bytes()
    for field in fields:
        if field.name not in document:
            raise MonoRepackError(f"typed MonoBehaviour JSON is missing field '{field.name}'")
        write_managed_value(writer, asset, registry, field.signature, document[field.name])
    return writer.bytes()

def repack_monoscript_json(json_path: Path, taildata: dict[str, Any], data_root: Path) -> bytes:
    document = json.loads(Path(json_path).read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise MonoRepackError("MonoScript JSON root must be an object")
    container = str(taildata_value(taildata, "container") or "")
    if not container:
        raise MonoRepackError("taildata record doesnt name a Unity container")
    asset_path = (Path(data_root) / container).resolve()
    asset = load_serialized_asset(asset_path, Path(data_root))
    hash_text = document.get("m_PropertiesHash")
    if not isinstance(hash_text, str):
        raise MonoRepackError("MonoScript JSON is missing m_PropertiesHash")
    try:
        properties_hash = bytes.fromhex(hash_text)
    except ValueError as exc:
        raise MonoRepackError("m_PropertiesHash must be hexadecimal text") from exc
    if len(properties_hash) != 16:
        raise MonoRepackError("m_PropertiesHash must contain exactly 16 bytes")

    writer = EndianWriter(asset.endian)
    writer.aligned_string(document.get("m_Name", ""))
    writer.i32(int(document.get("m_ExecutionOrder", 0)))
    writer.write(properties_hash)
    writer.aligned_string(document.get("m_ClassName", ""))
    writer.aligned_string(document.get("m_Namespace", ""))
    writer.aligned_string(document.get("m_AssemblyName", ""))
    writer.bool(document.get("m_IsEditorScript", False))
    return writer.bytes()

def managed_type_name(document: dict[str, Any], taildata: dict[str, Any]) -> str:
    script = document.get("solarScript")
    if isinstance(script, dict):
        value = script.get("full_name") or script.get("fullName")
        if value:
            return str(value)
    conversion = taildata.get("conversion")
    if isinstance(conversion, dict):
        value = conversion.get("managedType") or conversion.get("managed_type")
        if value:
            return str(value)
        script = conversion.get("script")
        if isinstance(script, dict):
            value = script.get("fullName") or script.get("full_name")
            if value:
                return str(value)
    return ""

def write_pptr(writer: EndianWriter, asset: SerializedAsset, value: dict[str, Any]) -> None:
    writer.i32(int(value.get("m_FileID", value.get("file_id", value.get("fileId", 0)))))
    path_id = int(value.get("m_PathID", value.get("path_id", value.get("pathId", 0))))
    if asset.format_version >= 14:
        writer.i64(path_id)
    else:
        writer.i32(path_id)

def write_managed_value(
    writer: EndianWriter,
    asset: SerializedAsset,
    registry: ManagedTypeRegistry,
    sig: TypeSig,
    value: Any,
    *,
    is_element: bool = False,
) -> None:
    if sig.kind == "primitive":
        write_primitive(writer, sig.name, value)
        if not is_element and registry.requires_alignment(sig):
            writer.align()
        return
    if sig.kind == "string":
        writer.aligned_string(value)
        return
    if sig.kind in {"szarray", "array", "list"}:
        if not isinstance(value, list):
            raise MonoRepackError(f"expected list for managed array {sig.name or sig.kind}")
        element = sig.args[0]
        writer.i32(len(value))
        for item in value:
            write_managed_value(writer, asset, registry, element, item, is_element=True)
        if registry.requires_alignment(element):
            writer.align()
        return
    if registry.is_unity_event(sig):
        write_unity_event(writer, asset, value)
        return
    if sig.name in UNITY_SERIALIZABLE_TYPES:
        write_unity_serializable_type(writer, asset, sig, value)
        return
    if registry.is_unity_object(sig):
        write_pptr(writer, asset, value if isinstance(value, dict) else {})
        return
    if registry.is_enum(sig):
        writer.i32(int(value))
        return
    if sig.name in UNITY_VALUE_TYPES:
        write_unity_value_type(writer, asset, sig, value)
        if not is_element:
            writer.align()
        return

    fields = registry.serializable_fields_for_signature(sig)
    if not fields:
        raise MonoRepackError(f"unsupported managed type {sig.name or sig.kind}")
    if not isinstance(value, dict):
        raise MonoRepackError(f"expected object for managed type {sig.name or sig.kind}")
    for field in fields:
        if field.name not in value:
            raise MonoRepackError(f"managed value for {sig.name} is missing field '{field.name}'")
        write_managed_value(writer, asset, registry, field.signature, value[field.name])
    if not is_element and registry.requires_alignment(sig):
        writer.align()

def write_primitive(writer: EndianWriter, name: str, value: Any) -> None:
    if name == "bool":
        writer.bool(value)
    elif name == "SInt8":
        writer.i8(int(value))
    elif name == "UInt8":
        writer.u8(int(value))
    elif name == "char":
        writer.u16(ord(value[0]) if isinstance(value, str) and value else int(value))
    elif name == "SInt16":
        writer.i16(int(value))
    elif name == "UInt16":
        writer.u16(int(value))
    elif name == "SInt32":
        writer.i32(int(value))
    elif name == "UInt32":
        writer.u32(int(value))
    elif name == "SInt64":
        writer.i64(int(value))
    elif name == "UInt64":
        writer.u64(int(value))
    elif name == "float":
        writer.f32(value)
    elif name == "double":
        writer.f64(value)
    else:
        raise ManagedSchemaError(f"unsupported primitive {name}")

def write_unity_event(writer: EndianWriter, asset: SerializedAsset, value: Any) -> None:
    value = value if isinstance(value, dict) else {}
    persistent = value.get("m_PersistentCalls")
    calls = persistent.get("m_Calls", []) if isinstance(persistent, dict) else []
    if not isinstance(calls, list):
        calls = []
    writer.i32(len(calls))
    for call in calls:
        call = call if isinstance(call, dict) else {}
        write_pptr(writer, asset, call.get("m_Target") if isinstance(call.get("m_Target"), dict) else {})
        writer.aligned_string(call.get("m_MethodName", ""))
        writer.i32(int(call.get("m_Mode", 0)))
        args = call.get("m_Arguments")
        args = args if isinstance(args, dict) else {}
        write_pptr(writer, asset, args.get("m_ObjectArgument") if isinstance(args.get("m_ObjectArgument"), dict) else {})
        writer.aligned_string(args.get("m_ObjectArgumentAssemblyTypeName", ""))
        writer.i32(int(args.get("m_IntArgument", 0)))
        writer.f32(args.get("m_FloatArgument", 0.0))
        writer.aligned_string(args.get("m_StringArgument", ""))
        writer.bool(args.get("m_BoolArgument", False))
        writer.align()
        writer.i32(int(call.get("m_CallState", 0)))
    writer.aligned_string(value.get("m_TypeName", ""))

def write_gui_style(writer: EndianWriter, asset: SerializedAsset, value: dict[str, Any]) -> None:
    writer.aligned_string(value.get("m_Name", ""))
    for state_name in GUI_STYLE_STATES:
        state = value.get(state_name) if isinstance(value.get(state_name), dict) else {}
        write_pptr(writer, asset, state.get("m_Background") if isinstance(state.get("m_Background"), dict) else {})
        color = state.get("m_TextColor") if isinstance(state.get("m_TextColor"), dict) else {}
        for channel in "rgba":
            writer.f32(color.get(channel, 0.0))
    for offset_name in GUI_STYLE_OFFSETS:
        offset = value.get(offset_name) if isinstance(value.get(offset_name), dict) else {}
        for field in RECT_OFFSET_FIELDS:
            writer.i32(int(offset.get(field, 0)))
    write_pptr(writer, asset, value.get("m_Font") if isinstance(value.get("m_Font"), dict) else {})
    writer.i32(int(value.get("m_FontSize", 0)))
    writer.i32(int(value.get("m_FontStyle", 0)))
    writer.i32(int(value.get("m_Alignment", 0)))
    writer.bool(value.get("m_WordWrap", False))
    writer.bool(value.get("m_RichText", False))
    writer.align()
    writer.i32(int(value.get("m_TextClipping", 0)))
    writer.i32(int(value.get("m_ImagePosition", 0)))
    offset = value.get("m_ContentOffset") if isinstance(value.get("m_ContentOffset"), dict) else {}
    writer.f32(offset.get("x", 0.0))
    writer.f32(offset.get("y", 0.0))
    writer.f32(value.get("m_FixedWidth", 0.0))
    writer.f32(value.get("m_FixedHeight", 0.0))
    writer.bool(value.get("m_StretchWidth", False))
    writer.bool(value.get("m_StretchHeight", False))
    writer.align()

def write_unity_serializable_type(writer: EndianWriter, asset: SerializedAsset, sig: TypeSig, value: Any) -> None:
    value = value if isinstance(value, dict) else {}
    name = sig.name
    version = unity_version_tuple(asset.unity_version)
    if name == "UnityEngine.AnimationCurve":
        keys = value.get("m_Curve", [])
        keys = keys if isinstance(keys, list) else []
        writer.i32(len(keys))
        for key in keys:
            key = key if isinstance(key, dict) else {}
            writer.f32(key.get("time", 0.0))
            writer.f32(key.get("value", 0.0))
            writer.f32(key.get("inSlope", 0.0))
            writer.f32(key.get("outSlope", 0.0))
            if version[0] >= 2018:
                writer.i32(int(key.get("weightedMode", 0)))
                writer.f32(key.get("inWeight", 0.0))
                writer.f32(key.get("outWeight", 0.0))
        writer.i32(int(value.get("m_PreInfinity", 0)))
        writer.i32(int(value.get("m_PostInfinity", 0)))
        if version[0] > 5 or (version[0] == 5 and version[1] >= 3):
            writer.i32(int(value.get("m_RotationOrder", 0)))
        return
    if name == "UnityEngine.RectOffset":
        for field in ("m_Left", "m_Right", "m_Top", "m_Bottom"):
            writer.i32(int(value.get(field, 0)))
        return
    if name == "UnityEngine.GUIStyle":
        write_gui_style(writer, asset, value)
        return
    if name == "UnityEngine.UI.FontData":
        write_pptr(writer, asset, value.get("m_Font") if isinstance(value.get("m_Font"), dict) else {})
        writer.i32(int(value.get("m_FontSize", 0)))
        writer.i32(int(value.get("m_FontStyle", 0)))
        writer.bool(value.get("m_BestFit", False))
        writer.align()
        writer.i32(int(value.get("m_MinSize", 0)))
        writer.i32(int(value.get("m_MaxSize", 0)))
        writer.i32(int(value.get("m_Alignment", 0)))
        writer.bool(value.get("m_AlignByGeometry", False))
        writer.align()
        writer.bool(value.get("m_RichText", False))
        writer.align()
        writer.i32(int(value.get("m_HorizontalOverflow", 0)))
        writer.i32(int(value.get("m_VerticalOverflow", 0)))
        writer.f32(value.get("m_LineSpacing", 0.0))
        return
    if name == "Solar.GameManagerPrefabs":
        write_pptr(writer, asset, value.get("FXManager") if isinstance(value.get("FXManager"), dict) else {})
        return
    if name == "UnityEngine.Gradient":
        write_gradient(writer, asset, value)
        return
    if name == "UnityEngine.Color32":
        writer.u32(int(value.get("rgba", 0)))
        return
    if name == "UnityEngine.Matrix4x4":
        write_matrix4x4(writer, value)
        return
    if name == "UnityEngine.PropertyName":
        writer.aligned_string(value.get("id", ""))
        return
    raise MonoRepackError(f"unsupported Unity serializable type {name}")

def write_gradient(writer: EndianWriter, asset: SerializedAsset, value: dict[str, Any]) -> None:
    version = unity_version_tuple(asset.unity_version)
    for index in range(8):
        item = value.get(f"key{index}", {})
        item = item if isinstance(item, dict) else {}
        if version[0] > 5 or (version[0] == 5 and version[1] >= 6):
            writer.f32(item.get("r", 0.0))
            writer.f32(item.get("g", 0.0))
            writer.f32(item.get("b", 0.0))
            writer.f32(item.get("a", 0.0))
        else:
            writer.u32(int(item.get("rgba", 0)))
    for prefix in ("ctime", "atime"):
        for index in range(8):
            writer.u16(int(value.get(f"{prefix}{index}", 0)))
    if version[0] > 5 or (version[0] == 5 and version[1] >= 5):
        writer.i32(int(value.get("m_Mode", 0)))
    writer.u8(int(value.get("m_NumColorKeys", 0)))
    writer.u8(int(value.get("m_NumAlphaKeys", 0)))
    writer.align()

def write_unity_value_type(writer: EndianWriter, asset: SerializedAsset, sig: TypeSig, value: Any) -> None:
    value = value if isinstance(value, dict) else {}
    name = sig.name
    if name == "UnityEngine.Vector2":
        writer.f32(value.get("x", 0.0))
        writer.f32(value.get("y", 0.0))
        return
    if name == "UnityEngine.Vector3":
        writer.f32(value.get("x", 0.0))
        writer.f32(value.get("y", 0.0))
        writer.f32(value.get("z", 0.0))
        return
    if name == "UnityEngine.Vector4" or name == "UnityEngine.Quaternion":
        writer.f32(value.get("x", 0.0))
        writer.f32(value.get("y", 0.0))
        writer.f32(value.get("z", 0.0))
        writer.f32(value.get("w", 0.0))
        return
    if name == "UnityEngine.Color":
        writer.f32(value.get("r", 0.0))
        writer.f32(value.get("g", 0.0))
        writer.f32(value.get("b", 0.0))
        writer.f32(value.get("a", 0.0))
        return
    if name == "UnityEngine.Color32":
        writer.u32(int(value.get("rgba", 0)))
        return
    if name == "UnityEngine.Rect":
        writer.f32(value.get("x", 0.0))
        writer.f32(value.get("y", 0.0))
        writer.f32(value.get("width", 0.0))
        writer.f32(value.get("height", 0.0))
        return
    if name == "UnityEngine.LayerMask":
        writer.i32(int(value.get("m_Mask", 0)))
        return
    if name == "UnityEngine.Bounds":
        center = value.get("m_Center", {})
        extent = value.get("m_Extent", {})
        center = center if isinstance(center, dict) else {}
        extent = extent if isinstance(extent, dict) else {}
        for field in ("x", "y", "z"):
            writer.f32(center.get(field, 0.0))
        for field in ("x", "y", "z"):
            writer.f32(extent.get(field, 0.0))
        return
    if name == "UnityEngine.Matrix4x4":
        write_matrix4x4(writer, value)
        return
    raise MonoRepackError(f"unsupported Unity value type {name}")

def write_matrix4x4(writer: EndianWriter, value: dict[str, Any]) -> None:
    for row in range(4):
        for col in range(4):
            writer.f32(value.get(f"e{row}{col}", 0.0))

def taildata_value(taildata: dict[str, Any], key: str, default: Any = None) -> Any:
    if key in taildata:
        return taildata[key]
    snake = "".join(["_" + char.lower() if char.isupper() else char for char in key]).lstrip("_")
    return taildata.get(snake, default)

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
