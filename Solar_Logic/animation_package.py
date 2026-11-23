from __future__ import annotations
import base64, hashlib, json, tempfile, zipfile
from pathlib import Path
from typing import Any
from .animation_export import AnimationClipExportError, read_animation_clip_document
from .animation_glb_export import AnimationGlbExportError, export_animation_clip_glb
from .animation_repack import (
    AnimationClipRepackError,
    AnimationClipWriter,
    load_animation_asset,
    normalize_container,
)
from .mono_repack import taildata_value
from .model_context import ModelContextIndex
from .serialized import SerializedAsset, SerializedObject

ANIMATION_PACKAGE_FORMAT = "sunflowyr-animation-package"
ANIMATION_PACKAGE_VERSION = 1
CLIP_MEMBER = "clip.json"
MANIFEST_MEMBER = "manifest.json"
PREVIEW_MEMBER = "preview.glb"

class AnimationPackageError(ValueError):
    pass

def export_animation_package(
    asset: SerializedAsset,
    obj: SerializedObject,
    context: ModelContextIndex,
    out_path: Path,
    metadata: dict[str, Any],
    pointers: Any | None = None,
) -> dict[str, Any]:
    try:
        document, source_conversion = read_animation_clip_document(asset, obj)
    except AnimationClipExportError as exc:
        raise AnimationPackageError(str(exc)) from exc
    if pointers is not None:
        document = rewrite_clip_pointers(document, pointers)

    packed_document, binary_members = externalize_animation_blobs(document)
    model_candidates = [
        {
            "container": key[0],
            "pathId": key[1],
            "classId": 43,
            "name": context.object_names.get(key, ""),
        }
        for key in context.animation_model_candidates(asset, obj)
    ]
    clip_bytes = encode_json(packed_document)
    preview_bytes: bytes | None = None
    preview_conversion: dict[str, Any] | None = None
    preview_error: str | None = None
    with tempfile.TemporaryDirectory(prefix="sunflowyr_animation_") as folder:
        preview_path = Path(folder) / PREVIEW_MEMBER
        try:
            preview_conversion = export_animation_clip_glb(
                asset,
                obj,
                context,
                preview_path,
                {},
            )
            preview_bytes = preview_path.read_bytes()
        except AnimationGlbExportError as exc:
            preview_error = str(exc)

    member_records = [member_record(CLIP_MEMBER, clip_bytes, "animation-document")]
    for member_name, member_data in binary_members.items():
        member_records.append(member_record(member_name, member_data, "unity-binary-array"))
    if preview_bytes is not None:
        member_records.append(member_record(PREVIEW_MEMBER, preview_bytes, "visual-preview"))

    manifest = {
        "format": ANIMATION_PACKAGE_FORMAT,
        "version": ANIMATION_PACKAGE_VERSION,
        "game": "rwby-ge",
        "unityVersion": asset.unity_version,
        "name": str(document.get("m_Name") or f"AnimationClip_{obj.path_id}"),
        "source": {
            "container": asset.relative_path,
            "pathId": obj.path_id,
            "classId": obj.class_id,
            "byteSize": obj.byte_size,
        },
        "clipDocument": CLIP_MEMBER,
        "preview": PREVIEW_MEMBER if preview_bytes is not None else None,
        "previewError": preview_error,
        "modelCandidates": model_candidates,
        "members": member_records,
        "editing": {
            "authoritative": CLIP_MEMBER,
            "binaryReferences": "sanim-member",
            "previewIsAuthoritative": False,
            "glbWriteBack": True,
            "note": (
                "clip.json and its referenced binary members contain the complete Unity AnimationClip. "
                "To animate in Blender, edit preview.glb or drop an edited copy beside this package "
                "named <package stem>.glb, the AssetBundle generator rebuilds the transform curves from "
                "it. clip.json is used unchanged when no edited GLB is present."
            ),
        },
    }
    manifest_bytes = encode_json(manifest)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        write_zip_member(archive, MANIFEST_MEMBER, manifest_bytes)
        write_zip_member(archive, CLIP_MEMBER, clip_bytes)
        for member_name, member_data in binary_members.items():
            write_zip_member(archive, member_name, member_data)
        if preview_bytes is not None:
            write_zip_member(archive, PREVIEW_MEMBER, preview_bytes)

    conversion = {
        "format": "animation_package_unity54",
        "packageFormat": ANIMATION_PACKAGE_FORMAT,
        "name": manifest["name"],
        "lossless": True,
        "writeBack": True,
        "assetBundleInput": True,
        "binaryMemberCount": len(binary_members),
        "clipJsonByteLength": len(clip_bytes),
        "binaryByteLength": sum(len(value) for value in binary_members.values()),
        "previewIncluded": preview_bytes is not None,
        "modelCandidateCount": len(model_candidates),
        "preview": preview_conversion,
        "source": source_conversion,
    }
    metadata["conversion"] = conversion
    metadata["writeback_note"] = (
        "The .sanim package is the complete AnimationClip editing and writeback source. Animate in "
        "Blender by editing its preview.glb or place an edited copy beside the package named "
        "<package stem>.glb. The AssetBundle generator rebuilds position, rotation and scale curves "
        "from the GLB. Everything else in the clip is preserved from clip.json."
    )
    return conversion

def read_animation_package(package_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    path = Path(package_path)
    try:
        with zipfile.ZipFile(path, "r") as archive:
            names = set(archive.namelist())
            if MANIFEST_MEMBER not in names or CLIP_MEMBER not in names:
                raise AnimationPackageError("Animation package is missing manifest.json or clip.json")
            manifest = decode_json(archive.read(MANIFEST_MEMBER), MANIFEST_MEMBER)
            if manifest.get("format") != ANIMATION_PACKAGE_FORMAT:
                raise AnimationPackageError("file is not a Sunflowyr AnimationClip package")
            if int(manifest.get("version", 0)) != ANIMATION_PACKAGE_VERSION:
                raise AnimationPackageError(
                    f"unsupported AnimationClip package version {manifest.get('version')}"
                )
            packed_document = decode_json(archive.read(CLIP_MEMBER), CLIP_MEMBER)
            document = hydrate_animation_blobs(packed_document, archive, names)
    except (OSError, zipfile.BadZipFile, KeyError) as exc:
        raise AnimationPackageError(f"couldnt read AnimationClip package: {exc}") from exc
    if not isinstance(document, dict):
        raise AnimationPackageError("AnimationClip package clip.json root must be an object")
    return document, manifest

def repack_animation_package(
    package_path: Path,
    taildata: dict[str, Any],
    data_root: Path,
) -> bytes:
    document, manifest = read_animation_package(package_path)
    class_id = int(taildata_value(taildata, "classId", 0) or 0)
    if class_id != 74:
        raise AnimationPackageError(f"AnimationClip taildata expected class 74, found {class_id}")
    container = str(taildata_value(taildata, "container") or "")
    if not container:
        raise AnimationPackageError("taildata record doesnt name a Unity container")
    source = manifest.get("source")
    if isinstance(source, dict):
        source_class = int(source.get("classId", class_id))
        source_container = str(source.get("container") or container)
        if source_class != class_id:
            raise AnimationPackageError(
                f"AnimationClip package class mismatch: package has {source_class}, taildata has {class_id}"
            )
        if normalize_container(source_container) != normalize_container(container):
            raise AnimationPackageError(
                "AnimationClip package and taildata name different source containers"
            )
    asset = load_animation_asset(Path(data_root), container)

    from .animation_glb_import import AnimationGlbImportError, apply_glb_curves
    from .asset_bundle_animation import find_animation_glb

    edited = find_animation_glb(Path(package_path), manifest)
    if edited is not None:
        data, origin = edited
        try:
            document = apply_glb_curves(document, data)
        except AnimationGlbImportError as exc:
            raise AnimationPackageError(
                f"couldnt rebuild the AnimationClip from {origin}: {exc}"
            ) from exc

    try:
        return AnimationClipWriter(asset).write(document)
    except AnimationClipRepackError as exc:
        raise AnimationPackageError(str(exc)) from exc

def repack_animation_clip_glb(
    glb_path: Path,
    taildata: dict[str, Any],
    data_root: Path,
) -> bytes:
    class_id = int(taildata_value(taildata, "classId", 0) or 0)
    if class_id != 74:
        raise AnimationPackageError(f"AnimationClip taildata expected class 74, found {class_id}")
    container = str(taildata_value(taildata, "container") or "")
    path_id = int(taildata_value(taildata, "pathId", 0) or 0)
    if not container or not path_id:
        raise AnimationPackageError("animation GLB taildata is missing container or pathId")
    asset = load_animation_asset(Path(data_root), container)
    obj = next(
        (item for item in asset.objects if item.path_id == path_id and item.class_id == class_id),
        None,
    )
    if obj is None:
        raise AnimationPackageError(f"AnimationClip path {path_id} wasnt found in {container}")
    try:
        document, unused_conversion = read_animation_clip_document(asset, obj)
    except AnimationClipExportError as exc:
        raise AnimationPackageError(str(exc)) from exc

    from .animation_glb_import import AnimationGlbImportError, apply_glb_curves

    try:
        document = apply_glb_curves(document, Path(glb_path).read_bytes())
    except (AnimationGlbImportError, OSError) as exc:
        raise AnimationPackageError(
            f"couldnt rebuild the AnimationClip from {Path(glb_path).name}: {exc}"
        ) from exc
    try:
        return AnimationClipWriter(asset).write(document)
    except AnimationClipRepackError as exc:
        raise AnimationPackageError(str(exc)) from exc

def rewrite_clip_pointers(value: Any, pointers: Any) -> Any:
    if isinstance(value, dict):
        if "m_FileID" in value and "m_PathID" in value:
            if int(value.get("m_FileID") or 0) == 0 and int(value.get("m_PathID") or 0) == 0:
                return value
            replaced = pointers(int(value["m_FileID"]), int(value["m_PathID"]))
            return replaced if isinstance(replaced, dict) else {"m_FileID": 0, "m_PathID": 0}
        return {key: rewrite_clip_pointers(item, pointers) for key, item in value.items()}
    if isinstance(value, list):
        return [rewrite_clip_pointers(item, pointers) for item in value]
    return value

def externalize_animation_blobs(document: Any) -> tuple[Any, dict[str, bytes]]:
    members: dict[str, bytes] = {}
    member_by_identity: dict[tuple[int, str], str] = {}

    def visit(value: Any) -> Any:
        if isinstance(value, dict):
            if value.get("encoding") == "base64" and isinstance(value.get("data"), str):
                try:
                    data = base64.b64decode(value["data"], validate=True)
                except Exception as exc:
                    raise AnimationPackageError("AnimationClip contains invalid base64 binary data") from exc
                digest = hashlib.sha256(data).hexdigest()
                identity = (len(data), digest)
                member_name = member_by_identity.get(identity)
                if member_name is None:
                    member_name = f"blobs/{len(members) + 1:04d}-{digest[:12]}.bin"
                    members[member_name] = data
                    member_by_identity[identity] = member_name
                result = {key: visit(item) for key, item in value.items() if key != "data"}
                result["encoding"] = "sanim-member"
                result["member"] = member_name
                result["byteLength"] = len(data)
                result["sha256"] = digest
                return result
            return {key: visit(item) for key, item in value.items()}
        if isinstance(value, list):
            return [visit(item) for item in value]
        return value

    return visit(document), members

def hydrate_blob(value: dict[str, Any], archive: zipfile.ZipFile, names: set[str]) -> dict[str, Any]:
    member_name = str(value.get("member") or "")
    if not member_name.startswith("blobs/") or member_name not in names:
        raise AnimationPackageError(f"AnimationClip binary member is missing: {member_name}")
    data = archive.read(member_name)
    expected_size = int(value.get("byteLength", len(data)))
    if len(data) != expected_size:
        raise AnimationPackageError(
            f"AnimationClip binary member {member_name} expected {expected_size} bytes, found {len(data)}"
        )
    result = {key: item for key, item in value.items() if key not in {"member", "sha256"}}
    result["encoding"] = "base64"
    result["data"] = base64.b64encode(data).decode("ascii")
    return result

def hydrate_animation_blobs(value: Any, archive: zipfile.ZipFile, names: set[str]) -> Any:
    if isinstance(value, dict) and value.get("encoding") == "sanim-member":
        return hydrate_blob(value, archive, names)
    stack = [value]
    while stack:
        item = stack.pop()
        children = item.items() if isinstance(item, dict) else enumerate(item) if isinstance(item, list) else ()
        for key, child in children:
            if isinstance(child, dict):
                if child.get("encoding") == "sanim-member":
                    item[key] = hydrate_blob(child, archive, names)
                else:
                    stack.append(child)
            elif isinstance(child, list):
                stack.append(child)
    return value

def member_record(name: str, data: bytes, role: str) -> dict[str, Any]:
    return {
        "path": name,
        "role": role,
        "byteLength": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }

def encode_json(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")

def decode_json(data: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(data.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise AnimationPackageError(f"{label} isnt valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise AnimationPackageError(f"{label} root must be an object")
    return value

def write_zip_member(archive: zipfile.ZipFile, name: str, data: bytes) -> None:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o600 << 16
    archive.writestr(info, data, compresslevel=9)
