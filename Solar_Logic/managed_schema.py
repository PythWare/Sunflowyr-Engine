from __future__ import annotations
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from .binary import EndianReader
from .serialized import SerializedAsset

class ManagedSchemaError(ValueError):
    pass

TABLE_IDS = {
    "Module": 0,
    "TypeRef": 1,
    "TypeDef": 2,
    "FieldPtr": 3,
    "Field": 4,
    "MethodPtr": 5,
    "MethodDef": 6,
    "ParamPtr": 7,
    "Param": 8,
    "InterfaceImpl": 9,
    "MemberRef": 10,
    "Constant": 11,
    "CustomAttribute": 12,
    "FieldMarshal": 13,
    "DeclSecurity": 14,
    "ClassLayout": 15,
    "FieldLayout": 16,
    "StandAloneSig": 17,
    "EventMap": 18,
    "EventPtr": 19,
    "Event": 20,
    "PropertyMap": 21,
    "PropertyPtr": 22,
    "Property": 23,
    "MethodSemantics": 24,
    "MethodImpl": 25,
    "ModuleRef": 26,
    "TypeSpec": 27,
    "ImplMap": 28,
    "FieldRVA": 29,
    "ENCLog": 30,
    "ENCMap": 31,
    "Assembly": 32,
    "AssemblyProcessor": 33,
    "AssemblyOS": 34,
    "AssemblyRef": 35,
    "AssemblyRefProcessor": 36,
    "AssemblyRefOS": 37,
    "File": 38,
    "ExportedType": 39,
    "ManifestResource": 40,
    "NestedClass": 41,
    "GenericParam": 42,
    "MethodSpec": 43,
    "GenericParamConstraint": 44,
}

TABLE_NAMES = [None] * 64
for table_name, table_index in TABLE_IDS.items():
    TABLE_NAMES[table_index] = table_name

TYPE_DEF_OR_REF = ("TypeDef", "TypeRef", "TypeSpec")
HAS_CONSTANT = ("Field", "Param", "Property")
HAS_CUSTOM_ATTRIBUTE = (
    "MethodDef",
    "Field",
    "TypeRef",
    "TypeDef",
    "Param",
    "InterfaceImpl",
    "MemberRef",
    "Module",
    "DeclSecurity",
    "Property",
    "Event",
    "StandAloneSig",
    "ModuleRef",
    "TypeSpec",
    "Assembly",
    "AssemblyRef",
    "File",
    "ExportedType",
    "ManifestResource",
    "GenericParam",
    "GenericParamConstraint",
    "MethodSpec",
)
MEMBER_REF_PARENT = ("TypeDef", "TypeRef", "ModuleRef", "MethodDef", "TypeSpec")
CUSTOM_ATTRIBUTE_TYPE = (None, None, "MethodDef", "MemberRef", None)
RESOLUTION_SCOPE = ("Module", "ModuleRef", "AssemblyRef", "TypeRef")
TYPE_OR_METHOD_DEF = ("TypeDef", "MethodDef")

FIELD_ACCESS_MASK = 0x0007
FIELD_PUBLIC = 0x0006
FIELD_STATIC = 0x0010
FIELD_INIT_ONLY = 0x0020
FIELD_LITERAL = 0x0040
FIELD_NOT_SERIALIZED = 0x0080

ELEMENT_NAMES = {
    0x02: "bool",
    0x03: "char",
    0x04: "SInt8",
    0x05: "UInt8",
    0x06: "SInt16",
    0x07: "UInt16",
    0x08: "SInt32",
    0x09: "UInt32",
    0x0A: "SInt64",
    0x0B: "UInt64",
    0x0C: "float",
    0x0D: "double",
    0x0E: "string",
    0x18: "IntPtr",
    0x19: "UIntPtr",
    0x1C: "object",
}

UNITY_VALUE_TYPES = {
    "UnityEngine.Vector2",
    "UnityEngine.Vector3",
    "UnityEngine.Vector4",
    "UnityEngine.Rect",
    "UnityEngine.Quaternion",
    "UnityEngine.Color",
    "UnityEngine.Color32",
    "UnityEngine.LayerMask",
    "UnityEngine.Bounds",
    "UnityEngine.Matrix4x4",
}

UNITY_SERIALIZABLE_TYPES = {
    "UnityEngine.AnimationCurve",
    "UnityEngine.Gradient",
    "UnityEngine.RectOffset",
    "UnityEngine.GUIStyle",
    "UnityEngine.UI.FontData",
    "Solar.GameManagerPrefabs",
    "UnityEngine.Color32",
    "UnityEngine.Matrix4x4",
    "UnityEngine.PropertyName",
}

UNITY_OBJECT_TYPE_HINTS = {
    "UnityEngine.Object",
    "UnityEngine.GameObject",
    "UnityEngine.Component",
    "UnityEngine.Behaviour",
    "UnityEngine.MonoBehaviour",
    "UnityEngine.ScriptableObject",
    "UnityEngine.Transform",
    "UnityEngine.Material",
    "UnityEngine.Texture",
    "UnityEngine.Texture2D",
    "UnityEngine.Sprite",
    "UnityEngine.AudioClip",
    "UnityEngine.AnimationClip",
    "UnityEngine.Animator",
    "UnityEngine.RuntimeAnimatorController",
    "UnityEngine.Font",
    "UnityEngine.Shader",
    "UnityEngine.Mesh",
    "UnityEngine.TextAsset",
}

@dataclass(slots=True)
class TypeSig:
    kind: str
    name: str = ""
    token: int | None = None
    args: list["TypeSig"] = field(default_factory=list)
    generic_index: int | None = None

    @property
    def full_name(self) -> str:
        return self.name

@dataclass(slots=True)
class FieldDef:
    token: int
    name: str
    flags: int
    signature: TypeSig
    attributes: set[str] = field(default_factory=set)

@dataclass(slots=True)
class TypeDef:
    token: int
    name: str
    namespace: str
    flags: int
    extends: TypeSig | None
    fields: list[FieldDef] = field(default_factory=list)
    generic_params: list[str] = field(default_factory=list)

    @property
    def full_name(self) -> str:
        return f"{self.namespace}.{self.name}" if self.namespace else self.name

@dataclass(slots=True)
class TypeRef:
    token: int
    name: str
    namespace: str

    @property
    def full_name(self) -> str:
        return f"{self.namespace}.{self.name}" if self.namespace else self.name

@dataclass(slots=True)
class MemberRef:
    token: int
    parent: int
    name: str

shared_registries: dict[str, tuple[tuple, "ManagedTypeRegistry"]] = {}

def shared_registry(managed_dir: Path) -> "ManagedTypeRegistry":
    folder = Path(managed_dir).resolve()
    try:
        stamp = tuple(sorted((path.name.lower(), path.stat().st_size, path.stat().st_mtime_ns) for path in folder.glob("*.dll")))
    except OSError:
        stamp = ()
    cached = shared_registries.get(str(folder))
    if cached is not None and cached[0] == stamp:
        return cached[1]
    registry = ManagedTypeRegistry(folder)
    registry.load()
    shared_registries[str(folder)] = (stamp, registry)
    return registry

class ManagedTypeRegistry:
    def __init__(self, managed_dir: Path):
        self.managed_dir = managed_dir
        self.assemblies: list[DotNetAssembly] = []
        self.types_by_full_name: dict[str, TypeDef] = {}
        self.loaded = False

    def load(self) -> None:
        if self.loaded:
            return
        if not self.managed_dir.exists():
            self.loaded = True
            return
        for path in sorted(self.managed_dir.glob("*.dll"), key=lambda p: p.name.lower()):
            try:
                assembly = DotNetAssembly(path)
                assembly.parse()
            except Exception:
                continue
            self.assemblies.append(assembly)
            for type_def in assembly.type_defs.values():
                self.types_by_full_name.setdefault(type_def.full_name, type_def)
        self.loaded = True

    def get_type(self, full_name: str) -> TypeDef | None:
        self.load()
        return self.types_by_full_name.get(full_name)

    def resolve_type(self, sig: TypeSig) -> TypeDef | None:
        self.load()
        if sig.kind in ("class", "valuetype"):
            return self.types_by_full_name.get(sig.name)
        if sig.kind == "genericinst" and sig.args:
            return self.resolve_type(sig.args[0])
        return None

    def resolve_generic_vars(self, sig: TypeSig, context: list[TypeSig]) -> TypeSig:
        if sig.kind == "var":
            if sig.generic_index is not None and sig.generic_index < len(context):
                return context[sig.generic_index]
            return sig
        if not sig.args:
            return sig
        return TypeSig(
            kind=sig.kind,
            name=sig.name,
            token=sig.token,
            args=[self.resolve_generic_vars(arg, context) for arg in sig.args],
            generic_index=sig.generic_index,
        )

    def knows_type(self, type_name: str) -> bool:
        self.load()
        return self.types_by_full_name.get(type_name) is not None

    def serializable_fields(self, type_name: str) -> list[FieldDef]:
        self.load()
        type_def = self.types_by_full_name.get(type_name)
        if type_def is None:
            return []
        fields = self.serializable_fields_for_type(type_def, [])
        return self.apply_unity_field_overrides(type_def, fields)

    def serializable_fields_for_signature(self, sig: TypeSig) -> list[FieldDef]:
        self.load()
        type_def = self.resolve_type(sig)
        if type_def is None:
            return []
        context = sig.args[1:] if sig.kind == "genericinst" and len(sig.args) > 1 else []
        fields = self.serializable_fields_for_type(type_def, context)
        return self.apply_unity_field_overrides(type_def, fields)

    def apply_unity_field_overrides(self, type_def: TypeDef, fields: list[FieldDef]) -> list[FieldDef]:
        if type_def.full_name == "GameManager":
            return [
                FieldDef(
                    token=field_def.token,
                    name=field_def.name,
                    flags=field_def.flags,
                    signature=TypeSig("class", "Solar.GameManagerPrefabs")
                    if field_def.name == "m_prefabs"
                    else field_def.signature,
                    attributes=field_def.attributes,
                )
                for field_def in fields
            ]
        if self.inherits_from(type_def, "UnityEngine.UI.LayoutGroup") and not any(
            field_def.name == "m_ChildAlignment" for field_def in fields
        ):
            out: list[FieldDef] = []
            inserted = False
            for field_def in fields:
                out.append(field_def)
                if field_def.name == "m_Padding":
                    out.append(
                        FieldDef(
                            token=0,
                            name="m_ChildAlignment",
                            flags=FIELD_PUBLIC,
                            signature=TypeSig("primitive", "SInt32"),
                        )
                    )
                    inserted = True
            if inserted:
                return out
        if type_def.full_name == "UnityEngine.UI.InputField" and not any(
            field_def.name == "m_KeyboardType" for field_def in fields
        ):
            out = []
            for field_def in fields:
                out.append(field_def)
                if field_def.name == "m_AsteriskChar":
                    out.append(
                        FieldDef(
                            token=0,
                            name="m_KeyboardType",
                            flags=FIELD_PUBLIC,
                            signature=TypeSig("primitive", "SInt32"),
                        )
                    )
            return out
        return fields

    def inherits_from(self, type_def: TypeDef, base_name: str) -> bool:
        seen: set[str] = set()
        current: TypeDef | None = type_def
        while current is not None and current.full_name not in seen:
            seen.add(current.full_name)
            if current.full_name == base_name:
                return True
            if current.extends is None:
                return False
            if current.extends.name == base_name:
                return True
            current = self.resolve_type(current.extends)
        return False

    def serializable_fields_for_type(self, type_def: TypeDef, context: list[TypeSig]) -> list[FieldDef]:
        fields: list[FieldDef] = []
        if type_def.extends is not None:
            base_sig = self.resolve_generic_vars(type_def.extends, context)
            base_context = base_sig.args[1:] if base_sig.kind == "genericinst" and len(base_sig.args) > 1 else []
            base_def = self.resolve_type(base_sig)
            if base_def is not None and base_def.full_name not in {
                "System.Object",
                "UnityEngine.Object",
                "UnityEngine.ScriptableObject",
                "UnityEngine.MonoBehaviour",
                "UnityEngine.Behaviour",
                "UnityEngine.Component",
            }:
                fields.extend(self.serializable_fields_for_type(base_def, base_context))
        for field_def in type_def.fields:
            resolved = self.resolve_generic_vars(field_def.signature, context)
            if self.will_unity_serialize(field_def, resolved):
                fields.append(
                    FieldDef(
                        token=field_def.token,
                        name=field_def.name,
                        flags=field_def.flags,
                        signature=resolved,
                        attributes=field_def.attributes,
                    )
                )
        return fields

    def will_unity_serialize(self, field_def: FieldDef, signature: TypeSig | None = None) -> bool:
        if field_def.flags & (FIELD_STATIC | FIELD_INIT_ONLY | FIELD_LITERAL | FIELD_NOT_SERIALIZED):
            return False
        if "System.NonSerializedAttribute" in field_def.attributes:
            return False
        is_public = (field_def.flags & FIELD_ACCESS_MASK) == FIELD_PUBLIC
        if not is_public and "UnityEngine.SerializeField" not in field_def.attributes:
            return False
        return self.is_supported_type(signature or field_def.signature)

    def is_supported_type(self, sig: TypeSig) -> bool:
        sig = self.resolve_generic_vars(sig, [])
        if sig.kind in {"primitive", "string", "enum"}:
            return True
        if sig.kind in {"szarray", "array", "list"}:
            return bool(sig.args) and sig.args[0].kind not in {"szarray", "array", "list"} and self.is_supported_type(sig.args[0])
        if self.is_delegate(sig):
            return False
        if self.is_unity_event(sig) or sig.name in UNITY_SERIALIZABLE_TYPES:
            return True
        if self.is_unity_object(sig):
            return True
        if sig.kind in {"class", "valuetype", "genericinst"}:
            return self.resolve_type(sig) is not None or sig.name in UNITY_VALUE_TYPES
        return False

    def is_unity_object(self, sig: TypeSig) -> bool:
        if sig.kind == "genericinst" and sig.args:
            sig = sig.args[0]
        if sig.name in UNITY_OBJECT_TYPE_HINTS:
            return True
        type_def = self.resolve_type(sig)
        resolved_locally = type_def is not None
        seen: set[str] = set()
        while type_def is not None and type_def.full_name not in seen:
            seen.add(type_def.full_name)
            if type_def.full_name in {"UnityEngine.Object", "UnityEngine.MonoBehaviour", "UnityEngine.ScriptableObject"}:
                return True
            if type_def.extends is None:
                return False
            if type_def.extends.name in UNITY_OBJECT_TYPE_HINTS:
                return True
            type_def = self.resolve_type(type_def.extends)
        if resolved_locally:
            return False
        if sig.kind == "class" and sig.name.startswith("UnityEngine.") and sig.name not in UNITY_VALUE_TYPES | UNITY_SERIALIZABLE_TYPES:
            return True
        return False

    def is_unity_event(self, sig: TypeSig) -> bool:
        if sig.name.startswith("UnityEngine.Events.UnityEvent"):
            return True
        type_def = self.resolve_type(sig)
        seen: set[str] = set()
        while type_def is not None and type_def.full_name not in seen:
            seen.add(type_def.full_name)
            if type_def.extends is None:
                return False
            if type_def.extends.name.startswith("UnityEngine.Events.UnityEvent"):
                return True
            type_def = self.resolve_type(type_def.extends)
        return False

    def is_delegate(self, sig: TypeSig) -> bool:
        type_def = self.resolve_type(sig)
        seen: set[str] = set()
        while type_def is not None and type_def.full_name not in seen:
            seen.add(type_def.full_name)
            if type_def.extends is None:
                return False
            if type_def.extends.name in {"System.Delegate", "System.MulticastDelegate"}:
                return True
            type_def = self.resolve_type(type_def.extends)
        return False

    def is_enum(self, sig: TypeSig) -> bool:
        type_def = self.resolve_type(sig)
        return type_def is not None and type_def.extends is not None and type_def.extends.name == "System.Enum"

    def read_value(self, reader: EndianReader, asset: SerializedAsset, sig: TypeSig, *, is_element: bool = False) -> Any:
        if sig.kind == "primitive":
            value = read_primitive(reader, sig.name)
            if not is_element and self.requires_alignment(sig):
                reader.align()
            return value
        if sig.kind == "string":
            return reader.aligned_string(max_length=16 * 1024 * 1024)
        if sig.kind in {"szarray", "array", "list"}:
            element = sig.args[0]
            size = reader.i32()
            if size < 0 or size > 2_000_000:
                raise ManagedSchemaError(f"invalid managed array size {size}")
            values = [self.read_value(reader, asset, element, is_element=True) for _ in range(size)]
            if self.requires_alignment(element):
                reader.align()
            return values
        if self.is_unity_event(sig):
            return self.read_unity_event(reader, asset)
        if sig.name in UNITY_SERIALIZABLE_TYPES:
            return self.read_unity_serializable_type(reader, asset, sig)
        if self.is_unity_object(sig):
            return read_pptr_value(reader, asset)
        if self.is_enum(sig):
            return reader.i32()
        if sig.name in UNITY_VALUE_TYPES:
            value = self.read_unity_value_type(reader, asset, sig)
            if not is_element:
                reader.align()
            return value
        type_def = self.resolve_type(sig)
        if type_def is None:
            raise ManagedSchemaError(f"unsupported managed type {sig.name or sig.kind}")
        obj: dict[str, Any] = {}
        context = sig.args[1:] if sig.kind == "genericinst" and len(sig.args) > 1 else []
        fields = self.apply_unity_field_overrides(type_def, self.serializable_fields_for_type(type_def, context))
        for field_def in fields:
            obj[field_def.name] = self.read_value(reader, asset, field_def.signature)
        if not is_element and self.requires_alignment(sig):
            reader.align()
        return obj

    def requires_alignment(self, sig: TypeSig) -> bool:
        if sig.kind == "primitive":
            return sig.name in {"bool", "SInt8", "UInt8", "SInt16", "UInt16", "char"}
        if sig.kind in {"szarray", "array", "list"}:
            return bool(sig.args) and self.requires_alignment(sig.args[0])
        if self.is_unity_event(sig) or sig.name in UNITY_SERIALIZABLE_TYPES:
            return False
        if sig.kind in {"valuetype", "class", "genericinst"} and not self.is_unity_object(sig) and not self.is_enum(sig):
            return True
        return False

    def read_unity_event(self, reader: EndianReader, asset: SerializedAsset) -> Any:
        call_count = reader.i32()
        if call_count < 0 or call_count > 100_000:
            raise ManagedSchemaError(f"invalid UnityEvent persistent call count {call_count}")
        calls = []
        for _ in range(call_count):
            target = read_pptr_value(reader, asset)
            method_name = reader.aligned_string()
            mode = reader.i32()
            argument_cache = {
                "m_ObjectArgument": read_pptr_value(reader, asset),
                "m_ObjectArgumentAssemblyTypeName": reader.aligned_string(),
                "m_IntArgument": reader.i32(),
                "m_FloatArgument": read_float32(reader),
                "m_StringArgument": reader.aligned_string(),
                "m_BoolArgument": reader.bool(),
            }
            reader.align()
            calls.append(
                {
                    "m_Target": target,
                    "m_MethodName": method_name,
                    "m_Mode": mode,
                    "m_Arguments": argument_cache,
                    "m_CallState": reader.i32(),
                }
            )
        return {
            "m_PersistentCalls": {"m_Calls": calls},
            "m_TypeName": reader.aligned_string(),
        }

    def read_unity_serializable_type(self, reader: EndianReader, asset: SerializedAsset, sig: TypeSig) -> Any:
        name = sig.name
        if name == "UnityEngine.AnimationCurve":
            keys = []
            size = reader.i32()
            if size < 0 or size > 1_000_000:
                raise ManagedSchemaError(f"invalid AnimationCurve key count {size}")
            version = unity_version_tuple(asset.unity_version)
            for _ in range(size):
                key = {
                    "time": read_float32(reader),
                    "value": read_float32(reader),
                    "inSlope": read_float32(reader),
                    "outSlope": read_float32(reader),
                }
                if version[0] >= 2018:
                    key["weightedMode"] = reader.i32()
                    key["inWeight"] = read_float32(reader)
                    key["outWeight"] = read_float32(reader)
                keys.append(key)
            value = {
                "m_Curve": keys,
                "m_PreInfinity": reader.i32(),
                "m_PostInfinity": reader.i32(),
            }
            if version[0] > 5 or (version[0] == 5 and version[1] >= 3):
                value["m_RotationOrder"] = reader.i32()
            return value
        if name == "UnityEngine.RectOffset":
            return {
                "m_Left": reader.i32(),
                "m_Right": reader.i32(),
                "m_Top": reader.i32(),
                "m_Bottom": reader.i32(),
            }
        if name == "UnityEngine.GUIStyle":
            return read_gui_style(reader, asset)
        if name == "UnityEngine.UI.FontData":
            value = {
                "m_Font": read_pptr_value(reader, asset),
                "m_FontSize": reader.i32(),
                "m_FontStyle": reader.i32(),
                "m_BestFit": reader.bool(),
            }
            reader.align()
            value["m_MinSize"] = reader.i32()
            value["m_MaxSize"] = reader.i32()
            value["m_Alignment"] = reader.i32()
            value["m_AlignByGeometry"] = reader.bool()
            reader.align()
            value["m_RichText"] = reader.bool()
            reader.align()
            value["m_HorizontalOverflow"] = reader.i32()
            value["m_VerticalOverflow"] = reader.i32()
            value["m_LineSpacing"] = read_float32(reader)
            return value
        if name == "Solar.GameManagerPrefabs":
            return {"FXManager": read_pptr_value(reader, asset)}
        if name == "UnityEngine.Gradient":
            return self.read_gradient(reader, asset)
        if name == "UnityEngine.Color32":
            return {"rgba": reader.u32()}
        if name == "UnityEngine.Matrix4x4":
            return {f"e{row}{col}": read_float32(reader) for row in range(4) for col in range(4)}
        if name == "UnityEngine.PropertyName":
            return {"id": reader.aligned_string()}
        raise ManagedSchemaError(f"unsupported Unity serializable type {name}")

    def read_gradient(self, reader: EndianReader, asset: SerializedAsset) -> Any:
        version = unity_version_tuple(asset.unity_version)
        value: dict[str, Any] = {}
        for index in range(8):
            if version[0] > 5 or (version[0] == 5 and version[1] >= 6):
                value[f"key{index}"] = {
                    "r": read_float32(reader),
                    "g": read_float32(reader),
                    "b": read_float32(reader),
                    "a": read_float32(reader),
                }
            else:
                value[f"key{index}"] = {"rgba": reader.u32()}
        for prefix in ("ctime", "atime"):
            for index in range(8):
                value[f"{prefix}{index}"] = reader.u16()
        if version[0] > 5 or (version[0] == 5 and version[1] >= 5):
            value["m_Mode"] = reader.i32()
        value["m_NumColorKeys"] = reader.u8()
        value["m_NumAlphaKeys"] = reader.u8()
        reader.align()
        return value

    def read_unity_value_type(self, reader: EndianReader, asset: SerializedAsset, sig: TypeSig) -> Any:
        name = sig.name
        if name == "UnityEngine.Vector2":
            return {"x": read_float32(reader), "y": read_float32(reader)}
        if name == "UnityEngine.Vector3":
            return {"x": read_float32(reader), "y": read_float32(reader), "z": read_float32(reader)}
        if name == "UnityEngine.Vector4" or name == "UnityEngine.Quaternion":
            return {"x": read_float32(reader), "y": read_float32(reader), "z": read_float32(reader), "w": read_float32(reader)}
        if name == "UnityEngine.Color":
            return {"r": read_float32(reader), "g": read_float32(reader), "b": read_float32(reader), "a": read_float32(reader)}
        if name == "UnityEngine.Color32":
            return {"rgba": reader.u32()}
        if name == "UnityEngine.Rect":
            return {"x": read_float32(reader), "y": read_float32(reader), "width": read_float32(reader), "height": read_float32(reader)}
        if name == "UnityEngine.LayerMask":
            return {"m_Mask": reader.i32()}
        if name == "UnityEngine.Bounds":
            return {
                "m_Center": {"x": read_float32(reader), "y": read_float32(reader), "z": read_float32(reader)},
                "m_Extent": {"x": read_float32(reader), "y": read_float32(reader), "z": read_float32(reader)},
            }
        if name == "UnityEngine.Matrix4x4":
            return {f"e{row}{col}": read_float32(reader) for row in range(4) for col in range(4)}
        raise ManagedSchemaError(f"unsupported Unity value type {name}")

class DotNetAssembly:
    def __init__(self, path: Path):
        self.path = path
        self.data = path.read_bytes()
        self.streams: dict[str, tuple[int, int]] = {}
        self.rows: dict[str, int] = {}
        self.table_offsets: dict[str, int] = {}
        self.index_sizes: dict[str, int] = {}
        self.type_refs: dict[int, TypeRef] = {}
        self.type_defs: dict[int, TypeDef] = {}
        self.type_specs: dict[int, TypeSig] = {}
        self.member_refs: dict[int, MemberRef] = {}
        self.method_owner: dict[int, int] = {}
        self.type_field_ranges: dict[int, tuple[int, int]] = {}

    def parse(self) -> None:
        metadata_offset = self.find_metadata_offset()
        self.read_streams(metadata_offset)
        self.read_tables_header()
        self.read_tables()

    def find_metadata_offset(self) -> int:
        data = self.data
        if data[:2] != b"MZ" or len(data) < 0x40:
            raise ManagedSchemaError(f"{self.path} isnt a Windows DLL")
        header = struct.unpack_from("<I", data, 0x3C)[0]
        if data[header: header + 4] != b"PE\0\0":
            raise ManagedSchemaError(f"{self.path} isnt a Windows DLL")
        section_count, optional_size = struct.unpack_from("<H12xH", data, header + 6)
        optional = header + 24
        magic = struct.unpack_from("<H", data, optional)[0]
        directories = optional + (96 if magic == 0x10B else 112)
        directory_count = struct.unpack_from("<I", data, directories - 4)[0]
        clr_rva = struct.unpack_from("<I", data, directories + 14 * 8)[0] if directory_count > 14 else 0
        if clr_rva == 0:
            raise ManagedSchemaError(f"{self.path} isnt a managed assembly")
        table = optional + optional_size
        sections = [struct.unpack_from("<8xIIII", data, table + index * 40) for index in range(section_count)]

        def file_offset(rva: int) -> int:
            for virtual_size, address, raw_size, raw_pointer in sections:
                if address <= rva < address + max(virtual_size, raw_size):
                    return rva - address + raw_pointer
            if sections and rva < min(section[1] for section in sections):
                return rva
            raise ManagedSchemaError(f"{self.path} points outside its own file")

        clr_offset = file_offset(clr_rva)
        metadata_rva = struct.unpack_from("<IHHII", data, clr_offset)[3]
        return file_offset(metadata_rva)

    def read_streams(self, metadata_offset: int) -> None:
        if self.data[metadata_offset : metadata_offset + 4] != b"BSJB":
            raise ManagedSchemaError("missing CLI metadata signature")
        version_len = struct.unpack_from("<I", self.data, metadata_offset + 12)[0]
        pos = align4(metadata_offset + 16 + version_len)
        flags, stream_count = struct.unpack_from("<HH", self.data, pos)
        pos += 4
        for _ in range(stream_count):
            offset, size = struct.unpack_from("<II", self.data, pos)
            pos += 8
            end = self.data.index(b"\0", pos)
            name = self.data[pos:end].decode("ascii", "replace")
            pos = align4(end + 1)
            self.streams[name] = (metadata_offset + offset, size)

    def read_tables_header(self) -> None:
        tables_offset = self.streams.get("#~", self.streams.get("#-"))[0]
        heap_sizes = self.data[tables_offset + 6]
        self.index_sizes["string"] = 4 if heap_sizes & 0x01 else 2
        self.index_sizes["guid"] = 4 if heap_sizes & 0x02 else 2
        self.index_sizes["blob"] = 4 if heap_sizes & 0x04 else 2
        pos = tables_offset + 8
        valid, sorted_mask = struct.unpack_from("<QQ", self.data, pos)
        pos += 16
        for table_id, name in enumerate(TABLE_NAMES):
            if name and ((valid >> table_id) & 1):
                self.rows[name] = struct.unpack_from("<I", self.data, pos)[0]
                pos += 4
            elif name:
                self.rows[name] = 0
        for table_id, name in enumerate(TABLE_NAMES):
            if name and self.rows.get(name, 0):
                self.table_offsets[name] = pos
                pos += self.compute_row_size(name) * self.rows[name]

    def read_tables(self) -> None:
        self.read_type_refs()
        self.read_type_defs()
        self.read_fields()
        self.read_member_refs()
        self.read_type_specs()
        self.resolve_type_def_extends()
        self.attach_generic_params()
        self.attach_custom_attributes()

    def read_type_refs(self) -> None:
        pos = self.table_offsets.get("TypeRef")
        if pos is None:
            return
        row_size = self.compute_row_size("TypeRef")
        for rid in range(1, self.rows["TypeRef"] + 1):
            cur = pos + (rid - 1) * row_size
            scope, cur = self.read_coded(cur, RESOLUTION_SCOPE)
            name_idx, cur = self.read_index(cur, "string")
            ns_idx, cur = self.read_index(cur, "string")
            token = make_token("TypeRef", rid)
            self.type_refs[token] = TypeRef(token, self.string(name_idx), self.string(ns_idx))

    def read_type_defs(self) -> None:
        pos = self.table_offsets.get("TypeDef")
        if pos is None:
            return
        row_size = self.compute_row_size("TypeDef")
        raw: list[tuple[TypeDef, int, int]] = []
        for rid in range(1, self.rows["TypeDef"] + 1):
            cur = pos + (rid - 1) * row_size
            flags = struct.unpack_from("<I", self.data, cur)[0]
            cur += 4
            name_idx, cur = self.read_index(cur, "string")
            ns_idx, cur = self.read_index(cur, "string")
            extends_coded, cur = self.read_coded(cur, TYPE_DEF_OR_REF)
            field_list, cur = self.read_table_index(cur, "Field")
            method_list, cur = self.read_table_index(cur, "MethodDef")
            token = make_token("TypeDef", rid)
            type_def = TypeDef(token, self.string(name_idx), self.string(ns_idx), flags, None)
            type_def.extends = TypeSig("pending", token=extends_coded) if extends_coded else None
            self.type_defs[token] = type_def
            raw.append((type_def, field_list, method_list))
        for index, (type_def, field_start, method_start) in enumerate(raw):
            field_end = raw[index + 1][1] if index + 1 < len(raw) else self.rows.get("Field", 0) + 1
            method_end = raw[index + 1][2] if index + 1 < len(raw) else self.rows.get("MethodDef", 0) + 1
            self.type_field_ranges[type_def.token] = (field_start, field_end)
            for rid in range(method_start, method_end):
                self.method_owner[make_token("MethodDef", rid)] = type_def.token

    def read_fields(self) -> None:
        pos = self.table_offsets.get("Field")
        if pos is None:
            return
        row_size = self.compute_row_size("Field")
        fields: dict[int, FieldDef] = {}
        for rid in range(1, self.rows["Field"] + 1):
            cur = pos + (rid - 1) * row_size
            flags = struct.unpack_from("<H", self.data, cur)[0]
            cur += 2
            name_idx, cur = self.read_index(cur, "string")
            sig_idx, cur = self.read_index(cur, "blob")
            token = make_token("Field", rid)
            fields[token] = FieldDef(token, self.string(name_idx), flags, self.field_signature(sig_idx))
        for type_def in self.type_defs.values():
            field_start, field_end = self.type_field_ranges.get(type_def.token, (0, 0))
            type_def.fields = [fields[make_token("Field", rid)] for rid in range(field_start, field_end) if make_token("Field", rid) in fields]

    def read_member_refs(self) -> None:
        pos = self.table_offsets.get("MemberRef")
        if pos is None:
            return
        row_size = self.compute_row_size("MemberRef")
        for rid in range(1, self.rows["MemberRef"] + 1):
            cur = pos + (rid - 1) * row_size
            parent, cur = self.read_coded(cur, MEMBER_REF_PARENT)
            name_idx, cur = self.read_index(cur, "string")
            sig_idx, cur = self.read_index(cur, "blob")
            token = make_token("MemberRef", rid)
            self.member_refs[token] = MemberRef(token, parent, self.string(name_idx))

    def read_type_specs(self) -> None:
        pos = self.table_offsets.get("TypeSpec")
        if pos is None:
            return
        row_size = self.compute_row_size("TypeSpec")
        for rid in range(1, self.rows["TypeSpec"] + 1):
            cur = pos + (rid - 1) * row_size
            sig_idx, cur = self.read_index(cur, "blob")
            token = make_token("TypeSpec", rid)
            self.type_specs[token] = self.parse_type_blob(sig_idx)

    def resolve_type_def_extends(self) -> None:
        for type_def in self.type_defs.values():
            if type_def.extends is not None and type_def.extends.kind == "pending":
                token = type_def.extends.token or 0
                type_def.extends = self.type_from_coded(token) if token else None

    def attach_generic_params(self) -> None:
        pos = self.table_offsets.get("GenericParam")
        if pos is None:
            return
        row_size = self.compute_row_size("GenericParam")
        buckets: dict[int, list[tuple[int, str]]] = {}
        for rid in range(1, self.rows["GenericParam"] + 1):
            cur = pos + (rid - 1) * row_size
            number = struct.unpack_from("<H", self.data, cur)[0]
            cur += 2
            cur += 2
            owner, cur = self.read_coded(cur, TYPE_OR_METHOD_DEF)
            name_idx, cur = self.read_index(cur, "string")
            buckets.setdefault(owner, []).append((number, self.string(name_idx)))
        for owner, names in buckets.items():
            if (owner >> 24) == TABLE_IDS["TypeDef"] and owner in self.type_defs:
                self.type_defs[owner].generic_params = [name for num, name in sorted(names)]

    def attach_custom_attributes(self) -> None:
        pos = self.table_offsets.get("CustomAttribute")
        if pos is None:
            return
        row_size = self.compute_row_size("CustomAttribute")
        fields = {field_def.token: field_def for type_def in self.type_defs.values() for field_def in type_def.fields}
        for rid in range(1, self.rows["CustomAttribute"] + 1):
            cur = pos + (rid - 1) * row_size
            parent, cur = self.read_coded(cur, HAS_CUSTOM_ATTRIBUTE)
            attr_type, cur = self.read_coded(cur, CUSTOM_ATTRIBUTE_TYPE)
            value_idx, cur = self.read_index(cur, "blob")
            if (parent >> 24) != TABLE_IDS["Field"] or parent not in fields:
                continue
            attr_name = self.attribute_type_name(attr_type)
            if attr_name:
                fields[parent].attributes.add(attr_name)

    def attribute_type_name(self, token: int) -> str:
        table_id = token >> 24
        if table_id == TABLE_IDS["MemberRef"]:
            member = self.member_refs.get(token)
            return self.type_name_from_token(member.parent) if member else ""
        if table_id == TABLE_IDS["MethodDef"]:
            owner = self.method_owner.get(token)
            return self.type_name_from_token(owner) if owner else ""
        return ""

    def type_name_from_token(self, token: int | None) -> str:
        if token is None:
            return ""
        table_id = token >> 24
        if table_id == TABLE_IDS["TypeDef"]:
            type_def = self.type_defs.get(token)
            return type_def.full_name if type_def else ""
        if table_id == TABLE_IDS["TypeRef"]:
            type_ref = self.type_refs.get(token)
            return type_ref.full_name if type_ref else ""
        if table_id == TABLE_IDS["TypeSpec"]:
            sig = self.type_specs.get(token)
            return sig.name if sig else ""
        return ""

    def compute_row_size(self, name: str) -> int:
        s = 0
        if name == "Module":
            return 2 + self.index_sizes["string"] + self.index_sizes["guid"] * 3
        if name == "TypeRef":
            return self.coded_size(RESOLUTION_SCOPE) + self.index_sizes["string"] * 2
        if name == "TypeDef":
            return 4 + self.index_sizes["string"] * 2 + self.coded_size(TYPE_DEF_OR_REF) + self.table_index_size("Field") + self.table_index_size("MethodDef")
        if name == "FieldPtr":
            return self.table_index_size("Field")
        if name == "Field":
            return 2 + self.index_sizes["string"] + self.index_sizes["blob"]
        if name == "MethodPtr":
            return self.table_index_size("MethodDef")
        if name == "MethodDef":
            return 8 + self.index_sizes["string"] + self.index_sizes["blob"] + self.table_index_size("Param")
        if name == "ParamPtr":
            return self.table_index_size("Param")
        if name == "Param":
            return 4 + self.index_sizes["string"]
        if name == "InterfaceImpl":
            return self.table_index_size("TypeDef") + self.coded_size(TYPE_DEF_OR_REF)
        if name == "MemberRef":
            return self.coded_size(MEMBER_REF_PARENT) + self.index_sizes["string"] + self.index_sizes["blob"]
        if name == "Constant":
            return 2 + self.coded_size(HAS_CONSTANT) + self.index_sizes["blob"]
        if name == "CustomAttribute":
            return self.coded_size(HAS_CUSTOM_ATTRIBUTE) + self.coded_size(CUSTOM_ATTRIBUTE_TYPE) + self.index_sizes["blob"]
        if name == "FieldMarshal":
            return self.coded_size(("Field", "Param")) + self.index_sizes["blob"]
        if name == "DeclSecurity":
            return 2 + self.coded_size(("TypeDef", "MethodDef", "Assembly")) + self.index_sizes["blob"]
        if name == "ClassLayout":
            return 6 + self.table_index_size("TypeDef")
        if name == "FieldLayout":
            return 4 + self.table_index_size("Field")
        if name == "StandAloneSig":
            return self.index_sizes["blob"]
        if name == "EventMap":
            return self.table_index_size("TypeDef") + self.table_index_size("Event")
        if name == "EventPtr":
            return self.table_index_size("Event")
        if name == "Event":
            return 2 + self.index_sizes["string"] + self.coded_size(TYPE_DEF_OR_REF)
        if name == "PropertyMap":
            return self.table_index_size("TypeDef") + self.table_index_size("Property")
        if name == "PropertyPtr":
            return self.table_index_size("Property")
        if name == "Property":
            return 2 + self.index_sizes["string"] + self.index_sizes["blob"]
        if name == "MethodSemantics":
            return 2 + self.table_index_size("MethodDef") + self.coded_size(("Event", "Property"))
        if name == "MethodImpl":
            return self.table_index_size("TypeDef") + self.coded_size(("MethodDef", "MemberRef")) * 2
        if name == "ModuleRef":
            return self.index_sizes["string"]
        if name == "TypeSpec":
            return self.index_sizes["blob"]
        if name == "ImplMap":
            return 2 + self.coded_size(("Field", "MethodDef")) + self.index_sizes["string"] + self.table_index_size("ModuleRef")
        if name == "FieldRVA":
            return 4 + self.table_index_size("Field")
        if name == "ENCLog":
            return 8
        if name == "ENCMap":
            return 4
        if name == "Assembly":
            return 16 + self.index_sizes["blob"] + self.index_sizes["string"] * 2
        if name == "AssemblyProcessor":
            return 4
        if name == "AssemblyOS":
            return 12
        if name == "AssemblyRef":
            return 12 + self.index_sizes["blob"] * 2 + self.index_sizes["string"] * 2
        if name == "AssemblyRefProcessor":
            return 4 + self.table_index_size("AssemblyRef")
        if name == "AssemblyRefOS":
            return 12 + self.table_index_size("AssemblyRef")
        if name == "File":
            return 4 + self.index_sizes["string"] + self.index_sizes["blob"]
        if name == "ExportedType":
            return 8 + self.index_sizes["string"] * 2 + self.coded_size(("File", "AssemblyRef", "ExportedType"))
        if name == "ManifestResource":
            return 8 + self.index_sizes["string"] + self.coded_size(("File", "AssemblyRef", "ExportedType"))
        if name == "NestedClass":
            return self.table_index_size("TypeDef") * 2
        if name == "GenericParam":
            return 4 + self.coded_size(TYPE_OR_METHOD_DEF) + self.index_sizes["string"]
        if name == "MethodSpec":
            return self.coded_size(("MethodDef", "MemberRef")) + self.index_sizes["blob"]
        if name == "GenericParamConstraint":
            return self.table_index_size("GenericParam") + self.coded_size(TYPE_DEF_OR_REF)
        return s

    def table_index_size(self, table: str) -> int:
        return 2 if self.rows.get(table, 0) < 0x10000 else 4

    def coded_size(self, tables: tuple[str | None, ...]) -> int:
        tag_bits = max(1, (len(tables) - 1).bit_length())
        max_rows = max((self.rows.get(table, 0) for table in tables if table), default=0)
        return 2 if max_rows < (1 << (16 - tag_bits)) else 4

    def read_index(self, pos: int, heap: str) -> tuple[int, int]:
        size = self.index_sizes[heap]
        if size == 2:
            return struct.unpack_from("<H", self.data, pos)[0], pos + 2
        return struct.unpack_from("<I", self.data, pos)[0], pos + 4

    def read_table_index(self, pos: int, table: str) -> tuple[int, int]:
        size = self.table_index_size(table)
        if size == 2:
            return struct.unpack_from("<H", self.data, pos)[0], pos + 2
        return struct.unpack_from("<I", self.data, pos)[0], pos + 4

    def read_coded(self, pos: int, tables: tuple[str | None, ...]) -> tuple[int, int]:
        size = self.coded_size(tables)
        raw = struct.unpack_from("<H" if size == 2 else "<I", self.data, pos)[0]
        tag_bits = max(1, (len(tables) - 1).bit_length())
        tag = raw & ((1 << tag_bits) - 1)
        rid = raw >> tag_bits
        table = tables[tag] if tag < len(tables) else None
        token = make_token(table, rid) if table and rid else 0
        return token, pos + size

    def string(self, index: int) -> str:
        if index == 0:
            return ""
        base, size = self.streams["#Strings"]
        end = self.data.index(b"\0", base + index)
        return self.data[base + index : end].decode("utf-8", "replace")

    def read_blob(self, index: int) -> bytes:
        if index == 0:
            return b""
        base, size = self.streams["#Blob"]
        length, pos = read_compressed_uint(self.data, base + index)
        return self.data[pos : pos + length]

    def field_signature(self, blob_index: int) -> TypeSig:
        blob = self.read_blob(blob_index)
        if not blob or blob[0] != 0x06:
            raise ManagedSchemaError("invalid field signature")
        sig, pos = self.parse_type(blob, 1)
        return sig

    def parse_type_blob(self, blob_index: int) -> TypeSig:
        blob = self.read_blob(blob_index)
        sig, pos = self.parse_type(blob, 0)
        return sig

    def parse_type(self, blob: bytes, pos: int) -> tuple[TypeSig, int]:
        while blob[pos] in (0x1F, 0x20, 0x41, 0x45):
            marker = blob[pos]
            pos += 1
            if marker in (0x1F, 0x20):
                token_value, pos = read_compressed_uint(blob, pos)
        etype = blob[pos]
        pos += 1
        if etype == 0x01:
            return TypeSig("void", "void"), pos
        if etype in (0x0F, 0x10):
            element, pos = self.parse_type(blob, pos)
            kind = "pointer" if etype == 0x0F else "byref"
            return TypeSig(kind, name=f"{element.name}{'*' if etype == 0x0F else '&'}", args=[element]), pos
        if etype == 0x1B:
            pos += 1
            count, pos = read_compressed_uint(blob, pos)
            result, pos = self.parse_type(blob, pos)
            for unused_index in range(count):
                parameter, pos = self.parse_type(blob, pos)
            return TypeSig("fnptr", name="fnptr", args=[result]), pos
        if etype in ELEMENT_NAMES:
            name = ELEMENT_NAMES[etype]
            return TypeSig("string" if etype == 0x0E else "primitive", name), pos
        if etype == 0x11 or etype == 0x12:
            token, pos = self.read_sig_coded_type(blob, pos)
            name = self.type_name_from_token(token)
            return TypeSig("valuetype" if etype == 0x11 else "class", name=name, token=token), pos
        if etype == 0x13:
            index, pos = read_compressed_uint(blob, pos)
            return TypeSig("var", generic_index=index), pos
        if etype == 0x1E:
            index, pos = read_compressed_uint(blob, pos)
            return TypeSig("mvar", generic_index=index), pos
        if etype == 0x1D:
            element, pos = self.parse_type(blob, pos)
            return TypeSig("szarray", args=[element]), pos
        if etype == 0x14:
            element, pos = self.parse_type(blob, pos)
            rank, pos = read_compressed_uint(blob, pos)
            sizes, pos = read_compressed_uint(blob, pos)
            for _ in range(sizes):
                size, pos = read_compressed_uint(blob, pos)
            lows, pos = read_compressed_uint(blob, pos)
            for _ in range(lows):
                low, pos = read_compressed_uint(blob, pos)
            return TypeSig("array", args=[element], name=f"{element.name}[{rank}]"), pos
        if etype == 0x15:
            class_or_value = blob[pos]
            pos += 1
            if class_or_value not in (0x11, 0x12):
                raise ManagedSchemaError("generic instance has unsupported generic type marker")
            token, pos = self.read_sig_coded_type(blob, pos)
            generic_type = TypeSig("valuetype" if class_or_value == 0x11 else "class", self.type_name_from_token(token), token=token)
            count, pos = read_compressed_uint(blob, pos)
            args = [generic_type]
            for _ in range(count):
                arg, pos = self.parse_type(blob, pos)
                args.append(arg)
            if generic_type.name in {"System.Collections.Generic.List`1", "System.Collections.Generic.IList`1"}:
                return TypeSig("list", name=generic_type.name, token=generic_type.token, args=args[1:]), pos
            return TypeSig("genericinst", name=generic_type.name, token=generic_type.token, args=args), pos
        raise ManagedSchemaError(f"unsupported element type 0x{etype:02x}")

    def read_sig_coded_type(self, blob: bytes, pos: int) -> tuple[int, int]:
        raw, pos = read_compressed_uint(blob, pos)
        tag = raw & 0x03
        rid = raw >> 2
        table = TYPE_DEF_OR_REF[tag]
        return make_token(table, rid), pos

    def type_from_coded(self, token: int) -> TypeSig:
        table_id = token >> 24
        if table_id == TABLE_IDS["TypeSpec"] and token in self.type_specs:
            return self.type_specs[token]
        return TypeSig("class", name=self.type_name_from_token(token), token=token)

GUI_STYLE_STATES = ("m_Normal", "m_Hover", "m_Active", "m_Focused", "m_OnNormal", "m_OnHover", "m_OnActive", "m_OnFocused")
GUI_STYLE_OFFSETS = ("m_Border", "m_Margin", "m_Padding", "m_Overflow")
RECT_OFFSET_FIELDS = ("m_Left", "m_Right", "m_Top", "m_Bottom")

def read_gui_style(reader: EndianReader, asset: SerializedAsset) -> dict[str, Any]:
    value: dict[str, Any] = {"m_Name": reader.aligned_string()}
    for state in GUI_STYLE_STATES:
        value[state] = {
            "m_Background": read_pptr_value(reader, asset),
            "m_TextColor": {channel: read_float32(reader) for channel in "rgba"},
        }
    for offset in GUI_STYLE_OFFSETS:
        value[offset] = {field: reader.i32() for field in RECT_OFFSET_FIELDS}
    value["m_Font"] = read_pptr_value(reader, asset)
    value["m_FontSize"] = reader.i32()
    value["m_FontStyle"] = reader.i32()
    value["m_Alignment"] = reader.i32()
    value["m_WordWrap"] = reader.bool()
    value["m_RichText"] = reader.bool()
    reader.align()
    value["m_TextClipping"] = reader.i32()
    value["m_ImagePosition"] = reader.i32()
    value["m_ContentOffset"] = {"x": read_float32(reader), "y": read_float32(reader)}
    value["m_FixedWidth"] = read_float32(reader)
    value["m_FixedHeight"] = read_float32(reader)
    value["m_StretchWidth"] = reader.bool()
    value["m_StretchHeight"] = reader.bool()
    reader.align()
    return value

def read_primitive(reader: EndianReader, name: str) -> Any:
    if name == "bool":
        return reader.bool()
    if name == "SInt8":
        value = reader.u8()
        return value - 256 if value > 127 else value
    if name == "UInt8":
        return reader.u8()
    if name == "char":
        return chr(reader.u16())
    if name == "SInt16":
        return reader.i16()
    if name == "UInt16":
        return reader.u16()
    if name == "SInt32":
        return reader.i32()
    if name == "UInt32":
        return reader.u32()
    if name == "SInt64":
        return reader.i64()
    if name == "UInt64":
        return reader.u64()
    if name == "float":
        return read_float32(reader)
    if name == "double":
        return struct.unpack(reader.endian + "d", reader.read(8))[0]
    raise ManagedSchemaError(f"unsupported primitive {name}")

def read_float32(reader: EndianReader) -> float:
    value = reader.f32()
    rounded = float(f"{value:.7g}")
    if struct.pack(reader.endian + "f", rounded) == struct.pack(reader.endian + "f", value):
        return rounded
    return value

def read_pptr_value(reader: EndianReader, asset: SerializedAsset) -> dict[str, int]:
    file_id = reader.i32()
    path_id = reader.i64() if asset.format_version >= 14 else reader.i32()
    return {"m_FileID": file_id, "m_PathID": path_id}

def make_token(table: str | None, rid: int) -> int:
    if not table or rid == 0:
        return 0
    return (TABLE_IDS[table] << 24) | rid

def read_compressed_uint(data: bytes, pos: int) -> tuple[int, int]:
    first = data[pos]
    if (first & 0x80) == 0:
        return first, pos + 1
    if (first & 0xC0) == 0x80:
        return ((first & 0x3F) << 8) | data[pos + 1], pos + 2
    if (first & 0xE0) == 0xC0:
        return ((first & 0x1F) << 24) | (data[pos + 1] << 16) | (data[pos + 2] << 8) | data[pos + 3], pos + 4
    raise ManagedSchemaError("invalid compressed metadata integer")

def align4(value: int) -> int:
    return (value + 3) & ~3

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
