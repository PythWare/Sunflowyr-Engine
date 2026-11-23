from __future__ import annotations
import json
from pathlib import Path
from typing import Any
from .binary import EndianReader
from .serialized import SerializedAsset, SerializedObject

class CgProgramExportError(ValueError):
    pass

def export_cgprogram_json(
    asset: SerializedAsset,
    obj: SerializedObject,
    out_path: Path,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    with asset.path.open("rb") as handle:
        reader = EndianReader(handle, asset.endian)
        reader.seek(obj.byte_start)
        name = reader.aligned_string(max_length=4096)
        remaining = obj.byte_start + obj.byte_size - reader.tell()
        if remaining < 0:
            raise CgProgramExportError("CGProgram name crosses the object boundary")
        trailing = reader.read(remaining)
    document = {
        "format": "sunflowyr-cgprogram",
        "version": 1,
        "game": "rwby-ge",
        "unityVersion": asset.unity_version,
        "m_Name": name,
        "trailingData": {
            "encoding": "hex",
            "byteLength": len(trailing),
            "data": trailing.hex(),
        },
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    conversion = {
        "format": "cgprogram_json_unity54",
        "name": name,
        "trailingByteLength": len(trailing),
        "writeBack": True,
    }
    metadata["conversion"] = conversion
    metadata["writeback_note"] = (
        "CGProgram JSON exposes the name and preserves its undocumented Unity 5.4 runtime bytes."
    )
    return conversion
