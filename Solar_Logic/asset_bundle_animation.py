from __future__ import annotations
import json, struct
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from .animation_repack import AnimationClipRepackError, AnimationClipWriter
from .animation_glb_import import AnimationGlbImportError, apply_glb_curves, summarize_curves
from .animation_package import AnimationPackageError, read_animation_package
from .asset_bundle_material import BundlePointerRef

REFERENCE_SENTINEL = 0x4E55_4C46_7A00_0000
NULL_POINTER = b"\0" * 12

class NewAnimationClipAssetError(ValueError):
    pass

def try_build_animation_clip_asset(
    json_path: Path,
    fallback_name: str,
) -> tuple[bytes, str, dict[str, object]] | None:
    explicit = json_path.name.lower().endswith(".animationclip.json")
    try:
        document = json.loads(Path(json_path).read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        if explicit:
            raise NewAnimationClipAssetError(f"couldnt read AnimationClip JSON: {exc}") from exc
        return None
    if not isinstance(document, dict):
        if explicit:
            raise NewAnimationClipAssetError("AnimationClip JSON root must be an object")
        return None
    if not is_animation_clip_document(json_path, document):
        return None
    return build_animation_clip_document(document, fallback_name, "animationclip-json")

def recorded_member_hash(manifest: dict[str, Any], member: str) -> str:
    for record in manifest.get("members") or []:
        if isinstance(record, dict) and str(record.get("path", "")) == member:
            return str(record.get("sha256", ""))
    return ""

def find_animation_glb(package_path: Path, manifest: dict[str, Any]) -> tuple[bytes, str] | None:
    import hashlib, zipfile

    package_path = Path(package_path)
    sibling = package_path.with_suffix(".glb")
    if sibling.is_file():
        try:
            return sibling.read_bytes(), sibling.name
        except OSError as exc:
            raise NewAnimationClipAssetError(f"couldnt read {sibling.name}: {exc}") from exc
    preview_member = str(manifest.get("preview") or "")
    if not preview_member:
        return None
    try:
        with zipfile.ZipFile(package_path, "r") as archive:
            if preview_member not in set(archive.namelist()):
                return None
            data = archive.read(preview_member)
    except (OSError, zipfile.BadZipFile, KeyError):
        return None
    recorded = recorded_member_hash(manifest, preview_member)
    if not recorded or hashlib.sha256(data).hexdigest() == recorded:
        return None
    return data, preview_member

def build_animation_package_asset(
    package_path: Path,
    fallback_name: str,
) -> tuple[bytes, str, dict[str, object]]:
    try:
        document, manifest = read_animation_package(package_path)
    except AnimationPackageError as exc:
        raise NewAnimationClipAssetError(str(exc)) from exc
    edited = find_animation_glb(Path(package_path), manifest)
    rebuilt_from: str | None = None
    if edited is not None:
        data, origin = edited
        try:
            document = apply_glb_curves(document, data)
        except AnimationGlbImportError as exc:
            raise NewAnimationClipAssetError(
                f"couldnt rebuild the AnimationClip from {origin}: {exc}"
            ) from exc
        rebuilt_from = origin
    result = build_animation_clip_document(document, fallback_name, "sunflowyr-sanim")
    result[2]["packageVersion"] = int(manifest.get("version", 0))
    result[2]["visualPreviewIncluded"] = bool(manifest.get("preview"))
    if rebuilt_from is not None:
        result[2]["curvesRebuiltFrom"] = rebuilt_from
        result[2].update(summarize_curves(document))
    return result

def build_animation_clip_document(
    document: dict[str, Any],
    fallback_name: str,
    source_format: str,
) -> tuple[bytes, str, dict[str, object]]:
    document = dict(document)
    tail = document.get("solarUnparsedTail")
    if isinstance(tail, dict):
        try:
            tail_size = int(tail.get("size", 0) or 0)
        except (TypeError, ValueError) as exc:
            raise NewAnimationClipAssetError("solarUnparsedTail.size must be an integer") from exc
        if tail_size != 0:
            raise NewAnimationClipAssetError(
                "new AnimationClips cant use solarUnparsedTail, fill in every field instead"
            )
    document.pop("solarUnparsedTail", None)
    references: dict[int, tuple[str, str]] = {}
    document = swap_references(document, references, "root")
    pointer = find_non_null_pointer(document, references=set(references))
    if pointer is not None:
        raise NewAnimationClipAssetError(
            f'the new AnimationClip points at a game object at {pointer}, point it at a staged file like {{"asset": "Sound.wav"}}'
        )
    name = clean_animation_name(document.get("m_Name"), fallback_name)
    document["m_Name"] = name
    context = SimpleNamespace(
        endian="<",
        unity_version="5.4.1p4",
        format_version=15,
    )
    try:
        payload = AnimationClipWriter(context).write(document)
    except (AnimationClipRepackError, TypeError, ValueError, KeyError) as exc:
        raise NewAnimationClipAssetError(str(exc)) from exc
    pointer_refs = []
    for sentinel, (source_name, location) in references.items():
        marker = struct.pack("<iq", 0, sentinel)
        offset = payload.find(marker)
        if offset < 0 or payload.find(marker, offset + 1) >= 0:
            raise NewAnimationClipAssetError(f"couldnt place the reference at {location} in the written clip")
        payload = payload[:offset] + NULL_POINTER + payload[offset + len(marker):]
        pointer_refs.append(BundlePointerRef(payload_offset=offset, source_name=source_name, slot=f"clip {location}"))
    conversion = document.get("conversion") if isinstance(document.get("conversion"), dict) else {}
    muscle = document.get("m_MuscleClip") if isinstance(document.get("m_MuscleClip"), dict) else {}
    details = {
        "sourceFormat": source_format,
        "legacy": bool(document.get("m_Legacy", False)),
        "compressed": bool(document.get("m_Compressed", False)),
        "sampleRate": float(document.get("m_SampleRate", 0.0)),
        "duration": animation_duration(muscle, conversion),
        "rotationCurves": len(document.get("m_RotationCurves") or []),
        "eulerCurves": len(document.get("m_EulerCurves") or []),
        "positionCurves": len(document.get("m_PositionCurves") or []),
        "scaleCurves": len(document.get("m_ScaleCurves") or []),
        "floatCurves": len(document.get("m_FloatCurves") or []),
        "pointerCurves": len(document.get("m_PPtrCurves") or []),
        "events": len(document.get("m_Events") or []),
        "selfContained": not pointer_refs,
    }
    return payload, name, details, tuple(pointer_refs)

def trail_text(trail: list[str]) -> str:
    return "".join(trail)

def swap_references(value: Any, references: dict[int, tuple[str, str]], location: str, trail: list[str] | None = None) -> Any:
    trail = [location] if trail is None else trail
    if isinstance(value, dict):
        target = value.get("asset") or value.get("file")
        if isinstance(target, str) and target and set(value) <= {"asset", "file"}:
            sentinel = REFERENCE_SENTINEL + len(references)
            references[sentinel] = (target, trail_text(trail))
            return {"m_FileID": 0, "m_PathID": sentinel}
        output = {}
        for key, item in value.items():
            if isinstance(item, (dict, list)):
                trail.append(f".{key}")
                output[key] = swap_references(item, references, location, trail)
                trail.pop()
            else:
                output[key] = item
        return output
    if isinstance(value, list):
        if value and not isinstance(value[0], (dict, list)) and not any(isinstance(item, (dict, list)) for item in value):
            return list(value)
        output_list = []
        for index, item in enumerate(value):
            trail.append(f"[{index}]")
            output_list.append(swap_references(item, references, location, trail))
            trail.pop()
        return output_list
    return value

def is_animation_clip_document(json_path: Path, document: dict[str, Any]) -> bool:
    if json_path.name.lower().endswith(".animationclip.json"):
        return True
    if str(document.get("format", "")).casefold() in {
        "sunflowyr-animation-clip",
        "sunflowyr-new-animation-clip",
    }:
        return True
    conversion = document.get("conversion")
    return isinstance(conversion, dict) and conversion.get("format") == "animation_clip_json_unity54"

POINTER_KEYS = frozenset({"m_FileID", "file_id", "m_PathID", "path_id"})

def has_non_null_pointer(value: Any, references: set[int]) -> bool:
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            if not POINTER_KEYS.isdisjoint(item):
                try:
                    file_id = int(item.get("m_FileID", item.get("file_id", 0)) or 0)
                    path_id = int(item.get("m_PathID", item.get("path_id", 0)) or 0)
                except (TypeError, ValueError):
                    return True
                if (file_id != 0 or path_id != 0) and not (file_id == 0 and path_id in references):
                    return True
            stack.extend(child for child in item.values() if isinstance(child, (dict, list)))
        elif isinstance(item, list):
            stack.extend(child for child in item if isinstance(child, (dict, list)))
    return False

def find_non_null_pointer(value: Any, location: str = "root", references: set[int] | None = None) -> str | None:
    if not has_non_null_pointer(value, references or set()):
        return None
    return locate_non_null_pointer(value, location, references)

def locate_non_null_pointer(value: Any, location: str = "root", references: set[int] | None = None) -> str | None:
    if isinstance(value, dict):
        pointer_keys = {"m_FileID", "file_id", "m_PathID", "path_id"}
        if pointer_keys.intersection(value):
            try:
                file_id = int(value.get("m_FileID", value.get("file_id", 0)) or 0)
                path_id = int(value.get("m_PathID", value.get("path_id", 0)) or 0)
            except (TypeError, ValueError) as exc:
                raise NewAnimationClipAssetError(f"invalid object pointer at {location}") from exc
            if (file_id != 0 or path_id != 0) and not (file_id == 0 and path_id in (references or set())):
                return location
        for key, item in value.items():
            found = locate_non_null_pointer(item, f"{location}.{key}", references)
            if found is not None:
                return found
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found = locate_non_null_pointer(item, f"{location}[{index}]", references)
            if found is not None:
                return found
    return None

def animation_duration(muscle: dict[str, Any], conversion: dict[str, Any]) -> float | None:
    converted = conversion.get("duration")
    if isinstance(converted, (int, float)):
        return float(converted)
    start = muscle.get("m_StartTime")
    stop = muscle.get("m_StopTime")
    if isinstance(start, (int, float)) and isinstance(stop, (int, float)):
        return float(stop) - float(start)
    return None

def clean_animation_name(value: Any, fallback: str) -> str:
    name = str(value or fallback).strip()
    if not name:
        raise NewAnimationClipAssetError("AnimationClip name cant be empty")
    if len(name.encode("utf-8")) > 4096:
        raise NewAnimationClipAssetError("AnimationClip name is too long")
    return name
