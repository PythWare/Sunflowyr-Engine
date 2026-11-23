from __future__ import annotations
import base64, math, struct
from pathlib import Path
from typing import Any
from .animation_export import AnimationClipExportError, read_animation_clip_document
from .gltf_writer import GlbBuilder
from .model_context import ModelContextIndex, ObjectKey
from .serialized import SerializedAsset, SerializedObject

class AnimationGlbExportError(ValueError):
    pass

def export_animation_clip_glb(
    asset: SerializedAsset,
    obj: SerializedObject,
    context: ModelContextIndex,
    out_path: Path,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    try:
        document, source_conversion = read_animation_clip_document(asset, obj)
    except AnimationClipExportError as exc:
        raise AnimationGlbExportError(str(exc)) from exc

    curve_paths = animation_curve_paths(document)
    root_key, hierarchy_paths = choose_animation_root(context, asset, obj, curve_paths)
    builder = GlbBuilder(str(document.get("m_Name") or f"AnimationClip_{obj.path_id}"))
    node_indices, resolved_paths = build_animation_nodes(builder, context, root_key, hierarchy_paths, curve_paths)
    rig_joint_count = add_animation_rig(builder, node_indices, resolved_paths)
    tracks, skipped_curves, decoded_compressed_curves = build_animation_tracks(
        document,
        node_indices,
    )
    float_curve_count = len(document.get("m_FloatCurves") or [])
    pointer_curve_count = len(document.get("m_PPtrCurves") or [])
    event_count = len(document.get("m_Events") or [])
    animation_index = builder.add_animation(
        str(document.get("m_Name") or f"AnimationClip_{obj.path_id}"),
        tracks,
        extras={
            "unity_container": asset.relative_path,
            "unity_animation_clip_path_id": obj.path_id,
        },
    )
    if animation_index is None:
        raise AnimationGlbExportError("AnimationClip has no supported transform animation channels")
    metrics = builder.write(out_path)
    metrics.update(
        {
            "format": "animation_glb_2",
            "sourceFormat": "unity_5_4_animation_clip",
            "clipName": document.get("m_Name", ""),
            "clipPathId": obj.path_id,
            "sampleRate": document.get("m_SampleRate"),
            "trackCount": len(tracks),
            "animatedPathCount": len({str(track.get("unity_path") or "") for track in tracks}),
            "curvePathCount": len(curve_paths),
            "resolvedCurvePathCount": len(curve_paths & resolved_paths),
            "skippedCurveCount": skipped_curves,
            "floatCurveCount": float_curve_count,
            "pointerCurveCount": pointer_curve_count,
            "eventCount": event_count,
            "visualOnly": False,
            "writeBack": True,
            "lossless": False,
            "linkedHierarchy": root_key is not None,
            "rootTransformPathId": root_key[1] if root_key is not None else 0,
            "rigJointCount": rig_joint_count,
            "streamedCurveCount": source_conversion.get("streamed_curve_count") or 0,
            "denseCurveCount": source_conversion.get("dense_curve_count") or 0,
            "compressedRotationCurveCount": source_conversion.get("compressed_rotation_curve_count") or 0,
            "decodedCompressedRotationCurveCount": decoded_compressed_curves,
        }
    )
    metadata["conversion"] = metrics
    metadata["writeback_note"] = (
        "Animation GLB is editable in Blender. The AssetBundle generator rebuilds the clip's position, "
        "rotation and scale curves from it and keeps float curves, object curves and events from the "
        "game's clip. Key tangents are resampled from the GLB keys, so curve shapes round trip as "
        "linear or stepped. Mecanim clips are refused because they play m_MuscleClip, not these curves."
    )
    return metrics

def add_animation_rig(
    builder: GlbBuilder,
    node_indices: dict[str, int],
    resolved_paths: set[str],
) -> int:
    if "" not in resolved_paths:
        return 0
    joint_paths = sorted(resolved_paths, key=lambda path: (path.count("/"), path))
    joint_nodes = [node_indices[path] for path in joint_paths if path in node_indices]
    marker = builder.add_animation_rig_marker(
        "Sunflowyr Animation",
        joint_nodes,
        node_indices[""],
    )
    roots = [node_indices[""]]
    if marker is not None:
        roots.append(marker)
    builder.set_scene_roots(roots)
    return len(joint_nodes)

def animation_curve_paths(document: dict[str, Any]) -> set[str]:
    output: set[str] = set()
    for key in ("m_RotationCurves", "m_EulerCurves", "m_PositionCurves", "m_ScaleCurves"):
        for curve in document.get(key) or []:
            if isinstance(curve, dict):
                output.add(normalize_path(curve.get("path")))
    for curve in document.get("m_CompressedRotationCurves") or []:
        if isinstance(curve, dict):
            output.add(normalize_path(curve.get("m_Path")))
    return output

def choose_animation_root(
    context: ModelContextIndex,
    asset: SerializedAsset,
    obj: SerializedObject,
    curve_paths: set[str],
) -> tuple[ObjectKey | None, dict[str, ObjectKey]]:
    best_root = None
    best_paths: dict[str, ObjectKey] = {}
    best_score = -1
    for root_key in context.animation_roots(asset, obj):
        paths = hierarchy_path_map(context, root_key)
        score = sum(path in paths for path in curve_paths)
        if score > best_score or (score == best_score and len(paths) < len(best_paths)):
            best_root = root_key
            best_paths = paths
            best_score = score
    return best_root, best_paths

def hierarchy_path_map(context: ModelContextIndex, root_key: ObjectKey) -> dict[str, ObjectKey]:
    output = {"": root_key}
    pending = [(root_key, "")]
    seen: set[ObjectKey] = set()
    while pending:
        key, path = pending.pop()
        if key in seen:
            continue
        seen.add(key)
        transform = context.transforms.get(key)
        if transform is None:
            continue
        for child_pointer in transform.children:
            child_key = context.resolve(transform.asset_key, child_pointer)
            child = context.transforms.get(child_key) if child_key is not None else None
            if child_key is None or child is None:
                continue
            child_name = child.name or f"Transform_{child_key[1]}"
            child_path = f"{path}/{child_name}".strip("/")
            output.setdefault(child_path, child_key)
            pending.append((child_key, child_path))
    return output

def build_animation_nodes(
    builder: GlbBuilder,
    context: ModelContextIndex,
    root_key: ObjectKey | None,
    hierarchy_paths: dict[str, ObjectKey],
    curve_paths: set[str],
) -> tuple[dict[str, int], set[str]]:
    required_paths = {""}
    for path in curve_paths:
        parts = [part for part in path.split("/") if part]
        for index in range(1, len(parts) + 1):
            required_paths.add("/".join(parts[:index]))

    node_indices: dict[str, int] = {}
    resolved_paths: set[str] = set()
    for path in sorted(required_paths, key=lambda item: (item.count("/"), item)):
        transform_key = hierarchy_paths.get(path)
        transform = context.transforms.get(transform_key) if transform_key is not None else None
        if transform is not None:
            resolved_paths.add(path)
            translation = convert_position(transform.local_position)
            rotation = convert_rotation(transform.local_rotation)
            scale = transform.local_scale
            extras = {
                "unity_container": transform_key[0],
                "unity_transform_path_id": transform_key[1],
                "unity_animation_path": path,
            }
            name = transform.name or (path.rsplit("/", 1)[-1] if path else "AnimationRoot")
        else:
            translation = (0.0, 0.0, 0.0)
            rotation = (0.0, 0.0, 0.0, 1.0)
            scale = (1.0, 1.0, 1.0)
            extras = {"unity_animation_path": path, "synthetic_animation_node": True}
            name = path.rsplit("/", 1)[-1] if path else "AnimationRoot"
        node_indices[path] = builder.add_node(
            name,
            translation=translation,
            rotation=rotation,
            scale=scale,
            extras=extras,
        )

    children: dict[str, list[int]] = {path: [] for path in node_indices}
    for path, node_index in node_indices.items():
        if not path:
            continue
        parent_path = path.rsplit("/", 1)[0] if "/" in path else ""
        if parent_path in children:
            children[parent_path].append(node_index)
    for path, child_indices in children.items():
        builder.set_node_children(node_indices[path], child_indices)
    builder.set_scene_roots([node_indices[""]])
    return node_indices, resolved_paths

def build_animation_tracks(
    document: dict[str, Any],
    node_indices: dict[str, int],
) -> tuple[list[dict[str, Any]], int, int]:
    tracks: list[dict[str, Any]] = []
    skipped = 0
    curve_groups = (
        ("m_PositionCurves", "translation", "vector3"),
        ("m_RotationCurves", "rotation", "quaternion"),
        ("m_ScaleCurves", "scale", "vector3"),
    )
    rotation_paths: set[str] = set()
    for source_key, target_path, value_kind in curve_groups:
        for curve in document.get(source_key) or []:
            if not isinstance(curve, dict):
                skipped += 1
                continue
            path = normalize_path(curve.get("path"))
            keyframes = expand_keyframes((curve.get("curve") or {}).get("m_Curve"), value_kind)
            track = keyframes_to_track(path, target_path, value_kind, keyframes, node_indices)
            if track is None:
                skipped += 1
                continue
            tracks.append(track)
            if target_path == "rotation":
                rotation_paths.add(path)

    decoded_compressed = 0
    for curve in document.get("m_CompressedRotationCurves") or []:
        if not isinstance(curve, dict):
            skipped += 1
            continue
        path = normalize_path(curve.get("m_Path"))
        if path in rotation_paths:
            continue
        times = unpack_packed_int_values(curve.get("m_Times"))
        rotations = unpack_packed_quaternions(curve.get("m_Values"))
        if not times or len(times) != len(rotations):
            skipped += 1
            continue
        keyframes = [
            {"time": value * 0.01, "value": vector_document(rotation)}
            for value, rotation in zip(times, rotations)
        ]
        track = keyframes_to_track(path, "rotation", "quaternion", keyframes, node_indices)
        if track is None:
            skipped += 1
            continue
        tracks.append(track)
        rotation_paths.add(path)
        decoded_compressed += 1

    for curve in document.get("m_EulerCurves") or []:
        if not isinstance(curve, dict):
            skipped += 1
            continue
        path = normalize_path(curve.get("path"))
        if path in rotation_paths:
            continue
        keyframes = expand_keyframes((curve.get("curve") or {}).get("m_Curve"), "euler")
        track = keyframes_to_track(path, "rotation", "euler", keyframes, node_indices)
        if track is None:
            skipped += 1
            continue
        tracks.append(track)
        rotation_paths.add(path)
    return tracks, skipped, decoded_compressed

def unpack_packed_int_values(value: Any) -> list[int]:
    if not isinstance(value, dict):
        return []
    count = int(value.get("m_NumItems") or 0)
    bit_size = int(value.get("m_BitSize") or 0)
    data = decode_blob(value.get("m_Data"))
    if count <= 0 or bit_size <= 0 or not data:
        return []
    values: list[int] = []
    byte_index = 0
    bit_index = 0
    for item_index in range(count):
        packed, byte_index, bit_index = read_packed_bits(
            data,
            bit_size,
            byte_index,
            bit_index,
        )
        values.append(packed)
    return values

def unpack_packed_quaternions(value: Any) -> list[tuple[float, float, float, float]]:
    if not isinstance(value, dict):
        return []
    count = int(value.get("m_NumItems") or 0)
    data = decode_blob(value.get("m_Data"))
    if count <= 0 or not data:
        return []
    output: list[tuple[float, float, float, float]] = []
    byte_index = 0
    bit_index = 0
    for item_index in range(count):
        flags, byte_index, bit_index = read_packed_bits(data, 3, byte_index, bit_index)
        omitted = flags & 3
        components = [0.0, 0.0, 0.0, 0.0]
        square_sum = 0.0
        for component_index in range(4):
            if component_index == omitted:
                continue
            bit_size = 9 if (omitted + 1) % 4 == component_index else 10
            packed, byte_index, bit_index = read_packed_bits(
                data,
                bit_size,
                byte_index,
                bit_index,
            )
            maximum = (1 << bit_size) - 1
            component = packed / (0.5 * maximum) - 1.0
            components[component_index] = component
            square_sum += component * component
        components[omitted] = math.sqrt(max(0.0, 1.0 - square_sum))
        if flags & 4:
            components[omitted] = -components[omitted]
        output.append(tuple(components))
    return output

def read_packed_bits(
    data: bytes,
    bit_count: int,
    byte_index: int,
    bit_index: int,
) -> tuple[int, int, int]:
    value = 0
    written = 0
    while written < bit_count:
        if byte_index >= len(data):
            raise AnimationGlbExportError("packed animation curve is truncated")
        take = min(bit_count - written, 8 - bit_index)
        mask = (1 << take) - 1
        value |= ((data[byte_index] >> bit_index) & mask) << written
        written += take
        bit_index += take
        if bit_index == 8:
            byte_index += 1
            bit_index = 0
    return value, byte_index, bit_index

def decode_blob(value: Any) -> bytes:
    if not isinstance(value, dict) or value.get("encoding") != "base64":
        return b""
    data = value.get("data")
    if not isinstance(data, str):
        return b""
    return base64.b64decode(data)

def expand_keyframes(value: Any, value_kind: str) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if not isinstance(value, dict) or value.get("encoding") != "base64":
        return []
    raw_text = value.get("data")
    count = int(value.get("count") or 0)
    stride = int(value.get("stride") or 0)
    if not isinstance(raw_text, str) or count <= 0 or stride <= 0:
        return []
    raw = base64.b64decode(raw_text)
    endian = "<" if value.get("byte_order") == "little" else ">"
    output = []
    value_count = {"vector3": 3, "quaternion": 4, "euler": 3}.get(value_kind)
    if value_count is None:
        return []
    for index in range(count):
        offset = index * stride
        if offset + 4 + value_count * 4 > len(raw):
            break
        time = struct.unpack_from(endian + "f", raw, offset)[0]
        values = struct.unpack_from(endian + f"{value_count}f", raw, offset + 4)
        output.append({"time": time, "value": vector_document(values)})
    return output

def keyframes_to_track(
    path: str,
    target_path: str,
    value_kind: str,
    keyframes: list[dict[str, Any]],
    node_indices: dict[str, int],
) -> dict[str, Any] | None:
    node_index = node_indices.get(path)
    if node_index is None:
        return None
    pairs: list[tuple[float, tuple[float, ...]]] = []
    previous_rotation = None
    for keyframe in keyframes:
        time = finite_number(keyframe.get("time"))
        value = keyframe.get("value")
        if time is None or not isinstance(value, dict):
            continue
        if value_kind == "vector3":
            vector = vector_values(value, 3)
            if vector is None:
                continue
            converted = convert_position(vector) if target_path == "translation" else vector
        elif value_kind == "quaternion":
            vector = vector_values(value, 4)
            if vector is None:
                continue
            converted = normalize_quaternion(convert_rotation(vector))
            converted = continuous_quaternion(previous_rotation, converted)
            previous_rotation = converted
        else:
            vector = vector_values(value, 3)
            if vector is None:
                continue
            converted = continuous_quaternion(previous_rotation, unity_euler_to_gltf_quaternion(vector))
            previous_rotation = converted
        pairs.append((time, converted))
    if not pairs:
        return None
    pairs.sort(key=lambda item: item[0])
    return {
        "node": node_index,
        "path": target_path,
        "value_type": "VEC4" if target_path == "rotation" else "VEC3",
        "times": [item[0] for item in pairs],
        "values": [item[1] for item in pairs],
        "interpolation": "LINEAR",
        "unity_path": path,
    }

def unity_euler_to_gltf_quaternion(euler: tuple[float, float, float]) -> tuple[float, float, float, float]:
    x = math.radians(euler[0]) * 0.5
    y = math.radians(-euler[1]) * 0.5
    z = math.radians(-euler[2]) * 0.5
    qx = (math.sin(x), 0.0, 0.0, math.cos(x))
    qy = (0.0, math.sin(y), 0.0, math.cos(y))
    qz = (0.0, 0.0, math.sin(z), math.cos(z))
    return normalize_quaternion(quaternion_multiply(quaternion_multiply(qy, qx), qz))

def quaternion_multiply(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    lx, ly, lz, lw = left
    rx, ry, rz, rw = right
    return (
        lw * rx + lx * rw + ly * rz - lz * ry,
        lw * ry - lx * rz + ly * rw + lz * rx,
        lw * rz + lx * ry - ly * rx + lz * rw,
        lw * rw - lx * rx - ly * ry - lz * rz,
    )

def continuous_quaternion(
    previous: tuple[float, float, float, float] | None,
    current: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    if previous is not None and sum(a * b for a, b in zip(previous, current)) < 0.0:
        return tuple(-value for value in current)
    return current

def normalize_quaternion(values: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    length = math.sqrt(sum(value * value for value in values))
    if length <= 1e-12:
        return (0.0, 0.0, 0.0, 1.0)
    return tuple(value / length for value in values)

def convert_position(values: tuple[float, float, float]) -> tuple[float, float, float]:
    return (-values[0], values[1], values[2])

def convert_rotation(values: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    return (values[0], -values[1], -values[2], values[3])

def vector_values(value: dict[str, Any], length: int) -> tuple[float, ...] | None:
    names = ("x", "y", "z", "w")
    output = []
    for name in names[:length]:
        number = finite_number(value.get(name))
        if number is None:
            return None
        output.append(number)
    return tuple(output)

def vector_document(values: tuple[float, ...]) -> dict[str, float]:
    return {name: float(value) for name, value in zip(("x", "y", "z", "w"), values)}

def finite_number(value: Any) -> float | None:
    if not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None

def normalize_path(value: Any) -> str:
    return str(value or "").replace("\\", "/").strip("/")
