from __future__ import annotations
import base64, json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from .binary import EndianReader
from .serialized import SerializedAsset, SerializedObject
from .paths import ensure_parent

class MonoExportError(ValueError):
    pass

@dataclass(slots=True)
class MonoScriptInfo:
    name: str
    class_name: str
    namespace: str
    assembly_name: str

    @property
    def full_name(self) -> str:
        return f"{self.namespace}.{self.class_name}" if self.namespace else self.class_name

def build_monoscript_index(assets: Iterable[SerializedAsset]) -> dict[tuple[str, int], MonoScriptInfo]:
    scripts: dict[tuple[str, int], MonoScriptInfo] = {}
    for asset in assets:
        script_objects = [obj for obj in asset.objects if obj.class_id == 115]
        if not script_objects:
            continue
        with asset.path.open("rb") as handle:
            reader = EndianReader(handle, asset.endian)
            for obj in script_objects:
                try:
                    scripts[(norm(asset.relative_path), obj.path_id)] = read_monoscript(asset, reader, obj)
                except Exception:
                    continue
    return scripts

def read_monoscript(asset: SerializedAsset, reader: EndianReader, obj: SerializedObject) -> MonoScriptInfo:
    version = unity_version_tuple(asset.unity_version)
    reader.seek(obj.byte_start)
    name = reader.aligned_string(max_length=4096)
    if version[0] > 3 or (version[0] == 3 and version[1] >= 4):
        reader.i32()
    if version[0] < 5:
        reader.u32()
    else:
        reader.read(16)
    if version[0] < 3:
        reader.aligned_string(max_length=4096)
    class_name = reader.aligned_string(max_length=4096)
    namespace = reader.aligned_string(max_length=4096) if version[0] >= 3 else ""
    assembly_name = reader.aligned_string(max_length=4096)
    return MonoScriptInfo(name=name, class_name=class_name, namespace=namespace, assembly_name=assembly_name)

def export_monoscript_json(
    asset: SerializedAsset,
    obj: SerializedObject,
    out_path: Path,
    metadata: dict,
) -> dict:
    with asset.path.open("rb") as handle:
        reader = EndianReader(handle, asset.endian)
        reader.seek(obj.byte_start)
        name = reader.aligned_string(max_length=4096)
        execution_order = reader.i32()
        properties_hash = reader.read(16)
        class_name = reader.aligned_string(max_length=4096)
        namespace = reader.aligned_string(max_length=4096)
        assembly_name = reader.aligned_string(max_length=4096)
        is_editor_script = reader.bool()
        object_end = obj.byte_start + obj.byte_size
        if reader.tell() != object_end:
            raise MonoExportError(
                f"MonoScript reader stopped at 0x{reader.tell():x}; expected 0x{object_end:x}"
            )

    full_name = f"{namespace}.{class_name}" if namespace else class_name
    document = {
        "format": "sunflowyr-monoscript",
        "version": 1,
        "game": "rwby-ge",
        "unityVersion": asset.unity_version,
        "source": {
            "container": asset.relative_path,
            "pathId": obj.path_id,
            "classId": obj.class_id,
            "className": obj.type_name,
            "byteOffset": obj.byte_start,
            "byteSize": obj.byte_size,
        },
        "m_Name": name,
        "m_ExecutionOrder": execution_order,
        "m_PropertiesHash": properties_hash.hex(),
        "m_ClassName": class_name,
        "m_Namespace": namespace,
        "m_AssemblyName": assembly_name,
        "m_IsEditorScript": is_editor_script,
        "fullName": full_name,
    }
    ensure_parent(out_path)
    with out_path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(document, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    conversion = {
        "format": "monoscript_json_unity54",
        "name": name,
        "fullName": full_name,
        "assemblyName": assembly_name,
        "propertiesHash": properties_hash.hex(),
        "sourceByteLength": obj.byte_size,
    }
    metadata["conversion"] = conversion
    metadata["writeback_note"] = (
        "MonoScript JSON exposes the complete Unity 5.4 script identity record and rebuilds directly "
        "to serialized MonoScript bytes."
    )
    return conversion

def export_monobehaviour_json(
    asset: SerializedAsset,
    obj: SerializedObject,
    out_path: Path,
    metadata: dict,
    scripts: dict[tuple[str, int], MonoScriptInfo],
    managed_registry: Any | None = None,
) -> dict:
    with asset.path.open("rb") as handle:
        reader = EndianReader(handle, asset.endian)
        document, conversion = read_monobehaviour_json(asset, reader, obj, scripts, managed_registry)

    ensure_parent(out_path)
    with out_path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(document, handle, ensure_ascii=False, indent=2)
        handle.write("\n")

    script = document.get("solarScript")
    conversion.update(
        {
            "name": document.get("m_Name", ""),
            "script": script,
        }
    )
    return conversion

def read_monobehaviour_json(
    asset: SerializedAsset,
    reader: EndianReader,
    obj: SerializedObject,
    scripts: dict[tuple[str, int], MonoScriptInfo],
    managed_registry: Any | None = None,
) -> tuple[dict, dict]:
    reader.seek(obj.byte_start)
    game_object = read_pptr(reader, asset)
    enabled = reader.u8()
    reader.align()
    script = read_pptr(reader, asset)
    name = reader.aligned_string(max_length=4096)
    custom_offset = reader.tell()
    object_end = obj.byte_start + obj.byte_size
    if custom_offset > object_end:
        raise MonoExportError("MonoBehaviour base fields ran past the object")
    script_info = resolve_script(asset, script, scripts)

    document = {
        "m_GameObject": pptr_json(game_object),
        "m_Enabled": enabled,
        "m_Script": pptr_json(script),
        "m_Name": name,
        "solarScript": None,
    }
    if script_info is not None:
        document["solarScript"] = {
            "name": script_info.name,
            "class_name": script_info.class_name,
            "namespace": script_info.namespace,
            "full_name": script_info.full_name,
            "assembly_name": script_info.assembly_name,
        }
    base_document = dict(document)

    if managed_registry is not None and script_info is not None:
        try:
            fields = managed_registry.serializable_fields(script_info.full_name)
            if fields or custom_offset == object_end:
                typed_document = dict(base_document)
                reader.seek(custom_offset)
                for field in fields:
                    typed_document[field.name] = managed_registry.read_value(reader, asset, field.signature)
                if reader.tell() != object_end:
                    raise MonoExportError(
                        f"typed MonoBehaviour reader stopped at 0x{reader.tell():x}; expected 0x{object_end:x}"
                    )
                return typed_document, {
                    "format": "monobehaviour_json_typed_managed",
                    "managed_type": script_info.full_name,
                    "managed_field_count": len(fields),
                    "custom_data_size": object_end - custom_offset,
                }
        except Exception as exc:
            reader.seek(custom_offset)
            document = dict(base_document)
            add_raw_serialized_fields(document, reader, obj, custom_offset, object_end)
            document["solarTypedError"] = str(exc)
            return document, {
                "format": "monobehaviour_json_base_plus_raw",
                "managed_type": script_info.full_name,
                "typed_error": str(exc),
                "custom_data_size": object_end - custom_offset,
            }

    reader.seek(custom_offset)
    add_raw_serialized_fields(document, reader, obj, custom_offset, object_end)
    return document, {
        "format": "monobehaviour_json_base_plus_raw",
        "managed_type": script_info.full_name if script_info is not None else None,
        "custom_data_size": object_end - custom_offset,
    }

def add_raw_serialized_fields(
    document: dict,
    reader: EndianReader,
    obj: SerializedObject,
    custom_offset: int,
    object_end: int,
) -> None:
    custom_data = reader.read(object_end - custom_offset)
    document["solarRawSerializedFields"] = {
        "offset_absolute": custom_offset,
        "offset_relative_to_object": custom_offset - obj.byte_start,
        "size": len(custom_data),
        "encoding": "base64",
        "data": base64.b64encode(custom_data).decode("ascii"),
    }

def read_pptr(reader: EndianReader, asset: SerializedAsset) -> dict:
    file_id_offset = reader.tell()
    file_id = reader.i32()
    path_id_offset = reader.tell()
    if asset.format_version < 14:
        path_id = reader.i32()
    else:
        path_id = reader.i64()
    return {
        "file_id": file_id,
        "path_id": path_id,
        "file_id_offset": file_id_offset,
        "path_id_offset": path_id_offset,
    }

def pptr_json(pptr: dict) -> dict:
    return {
        "m_FileID": pptr["file_id"],
        "m_PathID": pptr["path_id"],
    }

def resolve_script(
    asset: SerializedAsset,
    script: dict,
    scripts: dict[tuple[str, int], MonoScriptInfo],
) -> MonoScriptInfo | None:
    file_id = int(script["file_id"])
    path_id = int(script["path_id"])
    if path_id == 0:
        return None
    if file_id == 0:
        return scripts.get((norm(asset.relative_path), path_id))
    external_index = file_id - 1
    if 0 <= external_index < len(asset.externals):
        external = asset.externals[external_index]
        return scripts.get((norm(Path(external).name), path_id)) or scripts.get((norm(external), path_id))
    return None

def norm(path: str) -> str:
    return path.replace("\\", "/").lower()

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
