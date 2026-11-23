from __future__ import annotations
import json
from pathlib import Path
from typing import Any
from .editable_object_repack import load_editable_asset
from .mono_repack import EndianWriter, taildata_value

class CgProgramRepackError(ValueError):
    pass

def repack_cgprogram_json(
    json_path: Path,
    taildata: dict[str, Any],
    data_root: Path,
) -> bytes:
    document = json.loads(Path(json_path).read_text(encoding="utf-8-sig"))
    if not isinstance(document, dict) or document.get("format") != "sunflowyr-cgprogram":
        raise CgProgramRepackError("file isnt a Sunflowyr CGProgram JSON document")
    class_id = int(taildata_value(taildata, "classId", 0) or 0)
    if class_id != 109:
        raise CgProgramRepackError(f"CGProgram taildata expected class 109, found {class_id}")
    container = str(taildata_value(taildata, "container") or "")
    if not container:
        raise CgProgramRepackError("taildata record doesnt name a Unity container")
    trailing = document.get("trailingData")
    if not isinstance(trailing, dict) or trailing.get("encoding") != "hex":
        raise CgProgramRepackError("CGProgram JSON is missing trailingData hex")
    try:
        trailing_bytes = bytes.fromhex(str(trailing.get("data") or ""))
    except ValueError as exc:
        raise CgProgramRepackError("CGProgram trailingData is invalid hexadecimal text") from exc
    expected_size = int(trailing.get("byteLength", len(trailing_bytes)))
    if len(trailing_bytes) != expected_size:
        raise CgProgramRepackError(
            f"CGProgram trailingData expected {expected_size} bytes, found {len(trailing_bytes)}"
        )
    asset = load_editable_asset(Path(data_root), container)
    writer = EndianWriter(asset.endian)
    writer.aligned_string(document.get("m_Name", ""))
    writer.write(trailing_bytes)
    return writer.bytes()
