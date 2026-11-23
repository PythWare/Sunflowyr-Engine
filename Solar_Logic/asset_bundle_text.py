from __future__ import annotations
import struct
from pathlib import Path
from .asset_bundle_writer import align_buffer, pack_aligned_string

def build_text_asset(file_path: Path, name: str) -> tuple[bytes, dict[str, object]]:
    data = Path(file_path).read_bytes()
    output = bytearray(pack_aligned_string(name))
    output.extend(struct.pack("<I", len(data)))
    output.extend(data)
    align_buffer(output, 4)
    output.extend(pack_aligned_string(""))
    return bytes(output), {
        "sourceFormat": file_path.suffix.lower().lstrip(".") or "bytes",
        "byteLength": len(data),
    }
