from __future__ import annotations
import hashlib, json, struct, zipfile
from pathlib import Path
from typing import Any
from .editable_object_repack import load_editable_asset
from .mono_repack import EndianWriter, taildata_value
from .serialized import SerializedAsset, SerializedObject
from .shader_export import (
    ShaderExportError,
    ShaderSubProgram,
    convert_shader_object,
    lz4_compress_block,
    lz4_decompress_block,
    pack_shader_program,
    read_shader_object,
    read_shader_program,
    unity_version_tuple,
)

SHADER_PACKAGE_FORMAT = "sunflowyr-shader-package"
SHADER_PACKAGE_VERSION = 2
MANIFEST_MEMBER = "manifest.json"
SCRIPT_MEMBER = "script.shader"
PREVIEW_MEMBER = "preview.shader"
SUBPROGRAM_MEMBER = "subprograms.bin"
TRAILING_MEMBER = "trailing.bin"
PROGRAM_INDEX_MEMBER = "programs.json"
PROGRAM_CODE_FOLDER = "code"

class ShaderPackageError(ValueError):
    pass

def export_shader_package(
    asset: SerializedAsset,
    obj: SerializedObject,
    out_path: Path,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    try:
        shader = read_shader_object(asset, obj)
        preview, preview_conversion = convert_shader_object(asset, shader)
    except ShaderExportError as exc:
        raise ShaderPackageError(str(exc)) from exc
    members = {
        SCRIPT_MEMBER: shader.script,
        PREVIEW_MEMBER: preview.encode("utf-8"),
        SUBPROGRAM_MEMBER: shader.subprogram_blob,
        TRAILING_MEMBER: shader.trailing,
    }
    decoded = decode_programs(shader, asset.unity_version)
    program_records: list[dict] = []
    if decoded is not None:
        program_records, code_members = decoded
        members[PROGRAM_INDEX_MEMBER] = encode_json(
            {"format": "sunflowyr-shader-programs", "version": 1, "programs": program_records}
        )
        members.update(code_members)
    manifest = {
        "format": SHADER_PACKAGE_FORMAT,
        "version": SHADER_PACKAGE_VERSION,
        "game": "rwby-ge",
        "unityVersion": asset.unity_version,
        "name": shader.name,
        "pathName": shader.path_name,
        "decompressedSize": shader.decompressed_size,
        "source": {
            "container": asset.relative_path,
            "pathId": obj.path_id,
            "classId": obj.class_id,
            "byteSize": obj.byte_size,
        },
        "authoritativeScript": SCRIPT_MEMBER,
        "expandedPreview": PREVIEW_MEMBER,
        "compiledSubPrograms": SUBPROGRAM_MEMBER,
        "trailingData": TRAILING_MEMBER,
        "programIndex": PROGRAM_INDEX_MEMBER if program_records else None,
        "programCount": len(program_records),
        "members": [member_record(name, data) for name, data in members.items()],
        "editing": {
            "note": (
                "Edit script.shader while retaining valid GpuProgramIndex references. preview.shader is expanded "
                "for reading only. programs.json lists every compiled program with its keywords and its parameter "
                "table. the bytecode for each lives under code/ and can be replaced with output from the Shader "
                "Compiler. When programs.json is present the blob is rebuilt from it."
            )
        },
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        write_zip_member(archive, MANIFEST_MEMBER, encode_json(manifest))
        for name, data in members.items():
            write_zip_member(archive, name, data)
    conversion = {
        "format": "shader_package_unity54",
        "name": shader.name,
        "pathName": shader.path_name,
        "scriptByteLength": len(shader.script),
        "subProgramByteLength": len(shader.subprogram_blob),
        "trailingByteLength": len(shader.trailing),
        "lossless": True,
        "writeBack": True,
        "compiledCodeEditable": bool(program_records),
        "programCount": len(program_records),
        "preview": preview_conversion,
    }
    metadata["conversion"] = conversion
    metadata["writeback_note"] = (
        "The .sshader package holds the complete Unity Shader object. Edit script.shader for ShaderLab changes, "
        "and replace a program's bytecode under code/ to change compiled output. the blob is rebuilt from those "
        "on writeback and a replaced program's parameter table is regenerated from its own reflection."
    )
    return conversion

def decode_programs(shader, unity_version: str) -> tuple[list[dict], dict[str, bytes]] | None:
    if not shader.subprogram_blob:
        return None
    try:
        decompressed = lz4_decompress_block(shader.subprogram_blob, shader.decompressed_size)
        programs = read_shader_program(decompressed, unity_version_tuple(unity_version))
    except Exception:
        return None
    records: list[dict] = []
    members: dict[str, bytes] = {}
    for index, program in enumerate(programs):
        if program is None:
            records.append({"index": index, "present": False})
            continue
        code_name = f"{PROGRAM_CODE_FOLDER}/{index:04d}.bin"
        members[code_name] = program.program_code
        records.append(
            {
                "index": index,
                "present": True,
                "version": program.version,
                "programType": program.program_type,
                "stats": list(program.stats),
                "keywords": list(program.keywords),
                "localKeywords": list(program.local_keywords),
                "code": code_name,
                "codeByteLength": len(program.program_code),
                "codePrefix": program_code_prefix(program.program_code).hex(),
                "sourceOffset": program.source_offset,
                "parameterTable": program.parameter_table,
            }
        )
    return records, members

def program_code_prefix(code: bytes) -> bytes:
    start = code.find(b"DXBC")
    return code[:start] if start > 0 else b""

def rebuild_subprogram_blob(records: list[dict], members: dict[str, bytes]) -> tuple[bytes, int]:
    programs: list[ShaderSubProgram | None] = []
    for record in records:
        if not record.get("present", True):
            programs.append(None)
            continue
        code_name = str(record.get("code") or "")
        if code_name not in members:
            raise ShaderPackageError(f"shader package is missing program code {code_name}")
        programs.append(
            ShaderSubProgram(
                version=int(record["version"]),
                program_type=int(record["programType"]),
                keywords=list(record.get("keywords") or []),
                local_keywords=list(record.get("localKeywords") or []),
                program_code=members[code_name],
                stats=tuple(record.get("stats") or (0, 0, 0)),
                extra_stat=b"",
                parameter_table=record.get("parameterTable"),
                source_offset=record.get("sourceOffset"),
            )
        )
    raw = pack_shader_program(programs)
    return lz4_compress_block(raw), len(raw)

def extract_shader_package(package_path: Path, out_folder: Path) -> dict[str, Any]:
    package_path = Path(package_path)
    out_folder = Path(out_folder)
    try:
        with zipfile.ZipFile(package_path) as archive:
            names = archive.namelist()
            if MANIFEST_MEMBER not in names:
                raise ShaderPackageError("that file isnt a Sunflowyr shader package")
            manifest = json.loads(archive.read(MANIFEST_MEMBER).decode("utf-8-sig"))
            if manifest.get("format") != SHADER_PACKAGE_FORMAT:
                raise ShaderPackageError("that file is not a Sunflowyr shader package")
            out_folder.mkdir(parents=True, exist_ok=True)
            written = 0
            for name in names:
                target = (out_folder / name).resolve()
                try:
                    target.relative_to(out_folder.resolve())
                except ValueError:
                    raise ShaderPackageError(f"package member escapes the output folder: {name}")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(name))
                written += 1
    except (OSError, zipfile.BadZipFile, UnicodeError, json.JSONDecodeError) as exc:
        raise ShaderPackageError(f"could not open shader package: {exc}") from exc
    return {
        "folder": str(out_folder),
        "name": manifest.get("name", ""),
        "version": manifest.get("version"),
        "members": written,
        "programCount": manifest.get("programCount") or 0,
        "hasPrograms": PROGRAM_INDEX_MEMBER in names,
    }

def program_code_reflection(code: bytes) -> dict | None:
    start = code.find(b"DXBC")
    if start < 0 or len(code) - start < 0x20:
        return None
    dxbc = code[start:]
    try:
        chunk_count = struct.unpack_from("<I", dxbc, 0x1C)[0]
        if not 0 < chunk_count < 64:
            return None
        offsets = struct.unpack_from("<%dI" % chunk_count, dxbc, 0x20)
        if not any(dxbc[offset : offset + 4] == b"RDEF" for offset in offsets):
            return None
    except struct.error:
        return None
    from .shader_compiler import ShaderCompilerError, reflect_bytecode

    try:
        return reflect_bytecode(dxbc)
    except (ShaderCompilerError, OSError):
        return None

def restore_program_prefix(record: dict, code: bytes) -> bytes:
    prefix = str(record.get("codePrefix") or "")
    if not prefix or not code.startswith(b"DXBC"):
        return code
    try:
        return bytes.fromhex(prefix) + code
    except ValueError as exc:
        raise ShaderPackageError(
            f"program {record.get('index')} has an unreadable codePrefix in programs.json"
        ) from exc

def check_replacement_resources(record: dict, reflection: dict) -> None:
    from .shader_params import reflected_resource_counts

    prefix = str(record.get("codePrefix") or "")
    if len(prefix) != 12:
        return
    header = bytes.fromhex(prefix)
    counts = reflected_resource_counts(reflection)
    if header[1] == counts["textures"] and header[2] == counts["cbuffers"]:
        return
    raise ShaderPackageError(
        f"program {record.get('index')} binds {counts['textures']} textures and {counts['cbuffers']} cbuffers, "
        f"the original binds {header[1]} and {header[2]}, they must match"
    )

def refresh_program_tables(records: list[dict], members: dict[str, bytes]) -> int:
    from .shader_params import build_parameter_table_from_reflection

    refreshed = 0
    for record in records:
        if not record.get("present", True):
            continue
        code_name = str(record.get("code") or "")
        code = members.get(code_name)
        if not code:
            continue
        code = restore_program_prefix(record, code)
        members[code_name] = code
        record["codeByteLength"] = len(code)
        reflection = program_code_reflection(code)
        if reflection is None:
            continue
        check_replacement_resources(record, reflection)
        record["parameterTable"] = build_parameter_table_from_reflection(
            reflection, int(record["programType"]), record.get("parameterTable")
        )
        refreshed += 1
    return refreshed

def build_shader_package(folder: Path, out_path: Path) -> dict[str, Any]:
    folder = Path(folder)
    out_path = Path(out_path)
    manifest_path = folder / MANIFEST_MEMBER
    if not manifest_path.is_file():
        raise ShaderPackageError(f"{folder} holds no {MANIFEST_MEMBER}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ShaderPackageError(f"{MANIFEST_MEMBER} couldnt be read: {exc}") from exc
    if manifest.get("format") != SHADER_PACKAGE_FORMAT:
        raise ShaderPackageError(f"{MANIFEST_MEMBER} isnt a Sunflowyr shader package manifest")

    members: dict[str, bytes] = {}
    for item in sorted(folder.rglob("*")):
        if not item.is_file():
            continue
        name = item.relative_to(folder).as_posix()
        if name == MANIFEST_MEMBER:
            continue
        members[name] = item.read_bytes()

    refreshed = 0
    index_member = members.get(PROGRAM_INDEX_MEMBER)
    if index_member:
        try:
            index = json.loads(index_member.decode("utf-8-sig"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ShaderPackageError(f"{PROGRAM_INDEX_MEMBER} couldnt be read: {exc}") from exc
        records = index.get("programs") or []
        refreshed = refresh_program_tables(records, members)
        index["programs"] = records
        members[PROGRAM_INDEX_MEMBER] = encode_json(index)

    manifest["members"] = [member_record(name, data) for name, data in members.items()]
    manifest["members"].append(member_record(MANIFEST_MEMBER, encode_json(manifest)))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        write_zip_member(archive, MANIFEST_MEMBER, encode_json(manifest))
        for name, data in members.items():
            write_zip_member(archive, name, data)
    return {
        "package": str(out_path),
        "name": manifest.get("name", ""),
        "members": len(members) + 1,
        "tablesRefreshed": refreshed,
        "byteLength": out_path.stat().st_size,
    }

def read_shader_package(package_path: Path) -> tuple[dict[str, Any], dict[str, bytes]]:
    try:
        with zipfile.ZipFile(package_path, "r") as archive:
            names = set(archive.namelist())
            required = {MANIFEST_MEMBER, SCRIPT_MEMBER, SUBPROGRAM_MEMBER, TRAILING_MEMBER}
            missing = sorted(required - names)
            if missing:
                raise ShaderPackageError(f"shader package is missing: {', '.join(missing)}")
            manifest = json.loads(archive.read(MANIFEST_MEMBER).decode("utf-8-sig"))
            if not isinstance(manifest, dict) or manifest.get("format") != SHADER_PACKAGE_FORMAT:
                raise ShaderPackageError("file isnt a Sunflowyr shader package")
            if int(manifest.get("version", 0)) > SHADER_PACKAGE_VERSION:
                raise ShaderPackageError(f"unsupported shader package version {manifest.get('version')}")
            members = {
                SCRIPT_MEMBER: archive.read(SCRIPT_MEMBER),
                SUBPROGRAM_MEMBER: archive.read(SUBPROGRAM_MEMBER),
                TRAILING_MEMBER: archive.read(TRAILING_MEMBER),
            }
            for name in names:
                if name == PROGRAM_INDEX_MEMBER or name.startswith(PROGRAM_CODE_FOLDER + "/"):
                    members[name] = archive.read(name)
    except (OSError, zipfile.BadZipFile, KeyError, UnicodeError, json.JSONDecodeError) as exc:
        raise ShaderPackageError(f"couldnt read shader package: {exc}") from exc
    return manifest, members

def repack_shader_package(
    package_path: Path,
    taildata: dict[str, Any],
    data_root: Path,
) -> bytes:
    manifest, members = read_shader_package(package_path)
    class_id = int(taildata_value(taildata, "classId", 0) or 0)
    if class_id != 48:
        raise ShaderPackageError(f"Shader taildata expected class 48, found {class_id}")
    container = str(taildata_value(taildata, "container") or "")
    if not container:
        raise ShaderPackageError("taildata record doesnt name a Unity container")
    asset = load_editable_asset(Path(data_root), container)

    blob = members[SUBPROGRAM_MEMBER]
    decompressed_size = int(manifest.get("decompressedSize", 0))
    index_member = members.get(PROGRAM_INDEX_MEMBER)
    if index_member:
        try:
            index = json.loads(index_member.decode("utf-8-sig"))
            records = index.get("programs") or []
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ShaderPackageError(f"programs.json couldnt be read: {exc}") from exc
        if records:
            rebuilt_blob, rebuilt_size = rebuild_subprogram_blob(records, members)
            unchanged = False
            stored_size = int(manifest.get("decompressedSize", 0))
            if blob and stored_size:
                try:
                    unchanged = lz4_decompress_block(blob, stored_size) == lz4_decompress_block(
                        rebuilt_blob, rebuilt_size
                    )
                except Exception:
                    unchanged = False
            if not unchanged:
                blob, decompressed_size = rebuilt_blob, rebuilt_size

    writer = EndianWriter(asset.endian)
    writer.aligned_string(manifest.get("name", ""))
    write_byte_array(writer, members[SCRIPT_MEMBER])
    writer.aligned_string(manifest.get("pathName", ""))
    writer.u32(decompressed_size)
    write_byte_array(writer, blob)
    writer.write(members[TRAILING_MEMBER])
    return writer.bytes()

def write_byte_array(writer: EndianWriter, data: bytes) -> None:
    writer.i32(len(data))
    writer.write(data)
    writer.align()

def member_record(name: str, data: bytes) -> dict[str, Any]:
    return {"path": name, "byteLength": len(data), "sha256": hashlib.sha256(data).hexdigest()}

def encode_json(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")

def write_zip_member(archive: zipfile.ZipFile, name: str, data: bytes) -> None:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o600 << 16
    archive.writestr(info, data, compresslevel=9)
