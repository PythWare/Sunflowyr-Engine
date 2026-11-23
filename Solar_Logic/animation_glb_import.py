from __future__ import annotations
import json, math, struct
from typing import Any

GLB_MAGIC = 0x46546C67
JSON_CHUNK = 0x4E4F534A
BIN_CHUNK = 0x004E4942

COMPONENT_FORMATS = {5120: ("b", 1), 5121: ("B", 1), 5122: ("h", 2), 5123: ("H", 2), 5125: ("I", 4), 5126: ("f", 4)}
TYPE_COUNTS = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT4": 16}
NORMALIZE_DIVISORS = {5120: 127.0, 5121: 255.0, 5122: 32767.0, 5123: 65535.0}

CURVE_FIELDS = {
    "translation": "m_PositionCurves",
    "rotation": "m_RotationCurves",
    "scale": "m_ScaleCurves",
}
VECTOR_AXES = ("x", "y", "z")
QUATERNION_AXES = ("x", "y", "z", "w")
CONSTANT_TOLERANCE = 1e-6

class AnimationGlbImportError(ValueError):
    pass

def read_glb(data: bytes) -> tuple[dict[str, Any], bytes]:
    if len(data) < 12:
        raise AnimationGlbImportError("file is too small to be a binary glTF")
    magic, unused_version, total = struct.unpack_from("<III", data, 0)
    if magic != GLB_MAGIC:
        raise AnimationGlbImportError("file is not a binary glTF (.glb)")
    limit = min(int(total), len(data))
    offset = 12
    document: dict[str, Any] | None = None
    binary = b""
    while offset + 8 <= limit:
        length, kind = struct.unpack_from("<II", data, offset)
        payload = data[offset + 8 : offset + 8 + length]
        if kind == JSON_CHUNK:
            document = json.loads(payload.decode("utf-8"))
        elif kind == BIN_CHUNK:
            binary = bytes(payload)
        offset += 8 + length
    if document is None:
        raise AnimationGlbImportError("binary glTF has no JSON chunk")
    return document, binary

def accessor_values(document: dict[str, Any], binary: bytes, index: int) -> list[tuple[float, ...]]:
    accessors = document.get("accessors") or []
    if not 0 <= index < len(accessors):
        raise AnimationGlbImportError(f"accessor {index} is out of range")
    accessor = accessors[index]
    count = int(accessor.get("count", 0))
    component = int(accessor.get("componentType", 5126))
    kind = str(accessor.get("type", "SCALAR"))
    if component not in COMPONENT_FORMATS or kind not in TYPE_COUNTS:
        raise AnimationGlbImportError(f"unsupported accessor format {kind}/{component}")
    code, size = COMPONENT_FORMATS[component]
    parts = TYPE_COUNTS[kind]
    view_index = accessor.get("bufferView")
    if view_index is None:
        return [tuple(0.0 for _ in range(parts)) for _ in range(count)]
    views = document.get("bufferViews") or []
    if not 0 <= int(view_index) < len(views):
        raise AnimationGlbImportError(f"bufferView {view_index} is out of range")
    view = views[int(view_index)]
    base = int(view.get("byteOffset", 0)) + int(accessor.get("byteOffset", 0))
    stride = int(view.get("byteStride") or (size * parts))
    divisor = NORMALIZE_DIVISORS.get(component) if accessor.get("normalized") else None
    layout = "<" + code * parts
    out: list[tuple[float, ...]] = []
    for item in range(count):
        at = base + item * stride
        if at + size * parts > len(binary):
            raise AnimationGlbImportError("accessor reads past the end of the glTF buffer")
        values = struct.unpack_from(layout, binary, at)
        if divisor is not None:
            values = tuple(max(float(value) / divisor, -1.0) for value in values)
        out.append(tuple(float(value) for value in values))
    return out

def node_paths(document: dict[str, Any]) -> dict[int, str]:
    nodes = document.get("nodes") or []
    scenes = document.get("scenes") or []
    scene_index = int(document.get("scene", 0))
    roots: list[int] = []
    if 0 <= scene_index < len(scenes):
        roots = [int(item) for item in scenes[scene_index].get("nodes", [])]
    if not roots:
        claimed = {int(child) for node in nodes for child in node.get("children", [])}
        roots = [index for index in range(len(nodes)) if index not in claimed]
    paths: dict[int, str] = {}
    stack: list[tuple[int, str | None]] = [(root, None) for root in roots]
    while stack:
        index, parent = stack.pop()
        if index in paths or not 0 <= index < len(nodes):
            continue
        name = str(nodes[index].get("name") or f"node{index}")
        if parent is None:
            path = ""
        elif parent == "":
            path = name
        else:
            path = f"{parent}/{name}"
        paths[index] = path
        for child in nodes[index].get("children", []):
            stack.append((int(child), path))
    return paths

def node_parents(nodes: list[Any]) -> dict[int, int]:
    parents: dict[int, int] = {}
    for index, node in enumerate(nodes):
        if isinstance(node, dict):
            for child in node.get("children", []):
                parents.setdefault(int(child), index)
    return parents

def path_below(index: int, root: int, nodes: list[Any], parents: dict[int, int]) -> str | None:
    parts: list[str] = []
    current: int | None = index
    seen: set[int] = set()
    while current != root:
        if current is None or current in seen or not 0 <= current < len(nodes):
            return None
        seen.add(current)
        parts.append(str(nodes[current].get("name") or f"node{current}"))
        current = parents.get(current)
    return "/".join(reversed(parts))

def path_hint(node: Any) -> str | None:
    extras = node.get("extras") if isinstance(node, dict) else None
    if isinstance(extras, dict) and isinstance(extras.get("unity_animation_path"), str):
        return extras["unity_animation_path"].replace("\\", "/").strip("/")
    return None

def animated_node_paths(
    document: dict[str, Any],
    animated: set[int],
    known_paths: set[str],
) -> dict[int, str]:
    nodes = document.get("nodes") or []
    hints = {
        index: hint
        for index in animated
        if 0 <= index < len(nodes) and (hint := path_hint(nodes[index])) is not None
    }
    scene_paths = node_paths(document)
    best = {index: scene_paths[index] for index in animated if index in scene_paths}
    if len(hints) == len(animated) or not known_paths:
        best.update(hints)
        return best
    parents = node_parents(nodes)
    best_score = sum(path in known_paths for path in best.values())
    candidates: set[int] = set()
    for index in animated:
        current: int | None = index
        while current is not None and current not in candidates:
            candidates.add(current)
            current = parents.get(current)
    for root in sorted(candidates):
        paths = {
            index: path
            for index in animated
            if (path := path_below(index, root, nodes, parents)) is not None
        }
        score = sum(path in known_paths for path in paths.values())
        if score > best_score:
            best = paths
            best_score = score
    best.update(hints)
    return best

def convert_position(values: tuple[float, ...]) -> tuple[float, ...]:
    return (-values[0], values[1], values[2])

def convert_rotation(values: tuple[float, ...]) -> tuple[float, ...]:
    return (values[0], -values[1], -values[2], values[3])

def convert_sample(target_path: str, values: tuple[float, ...]) -> tuple[float, ...]:
    if target_path == "translation":
        return convert_position(values)
    if target_path == "rotation":
        return convert_rotation(values)
    return tuple(values[:3])

def convert_slope(target_path: str, values: tuple[float, ...]) -> tuple[float, ...]:
    return convert_sample(target_path, values)

def axes_for(target_path: str) -> tuple[str, ...]:
    return QUATERNION_AXES if target_path == "rotation" else VECTOR_AXES

def as_vector(target_path: str, values: tuple[float, ...]) -> dict[str, float]:
    axes = axes_for(target_path)
    return {axis: float(values[position]) for position, axis in enumerate(axes)}

def segment_slope(
    first: tuple[float, ...], second: tuple[float, ...], span: float, width: int
) -> tuple[float, ...]:
    if span <= 0.0:
        return tuple(0.0 for _ in range(width))
    return tuple((second[index] - first[index]) / span for index in range(width))

def build_keyframes(
    times: list[float],
    samples: list[tuple[float, ...]],
    interpolation: str,
    target_path: str,
) -> list[dict[str, Any]]:
    width = len(axes_for(target_path))
    if interpolation == "CUBICSPLINE":
        if len(samples) != len(times) * 3:
            raise AnimationGlbImportError("cubic spline sampler output doesnt hold three values per key")
        keys: list[dict[str, Any]] = []
        for index, time in enumerate(times):
            in_tangent = convert_slope(target_path, samples[index * 3])
            value = convert_sample(target_path, samples[index * 3 + 1])
            out_tangent = convert_slope(target_path, samples[index * 3 + 2])
            keys.append(
                {
                    "time": float(time),
                    "value": as_vector(target_path, value),
                    "inSlope": as_vector(target_path, in_tangent),
                    "outSlope": as_vector(target_path, out_tangent),
                }
            )
        return keys

    if len(samples) != len(times):
        raise AnimationGlbImportError("sampler output count doesnt match its input count")
    values = [convert_sample(target_path, sample) for sample in samples]
    if target_path == "rotation":
        for index in range(1, len(values)):
            if sum(a * b for a, b in zip(values[index - 1], values[index])) < 0.0:
                values[index] = tuple(-value for value in values[index])
    if interpolation == "STEP":
        stepped = tuple(math.inf for _ in range(width))
        segments = [stepped] * max(0, len(times) - 1)
    else:
        segments = [
            segment_slope(values[index], values[index + 1], times[index + 1] - times[index], width)
            for index in range(len(times) - 1)
        ]
    flat = tuple(0.0 for _ in range(width))
    keys = []
    for index, time in enumerate(times):
        incoming = segments[index - 1] if index > 0 else (segments[0] if segments else flat)
        outgoing = segments[index] if index < len(segments) else incoming
        keys.append(
            {
                "time": float(time),
                "value": as_vector(target_path, values[index]),
                "inSlope": as_vector(target_path, incoming),
                "outSlope": as_vector(target_path, outgoing),
            }
        )
    return keys

def is_constant(keys: list[dict[str, Any]]) -> bool:
    first = keys[0]["value"]
    return all(
        abs(float(key["value"][axis]) - float(first[axis])) <= CONSTANT_TOLERANCE
        for key in keys[1:]
        for axis in first
    )

def curves_from_glb(
    data: bytes,
    animation_index: int = 0,
    known_curves: set[tuple[str, str]] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    document, binary = read_glb(data)
    animations = document.get("animations") or []
    if not animations:
        raise AnimationGlbImportError("the glTF file holds no animation")
    if not 0 <= animation_index < len(animations):
        animation_index = 0
    animation = animations[animation_index]
    samplers = animation.get("samplers") or []
    channels = [
        channel
        for channel in animation.get("channels") or []
        if (channel.get("target") or {}).get("node") is not None
        and str((channel.get("target") or {}).get("path", "")) in CURVE_FIELDS
    ]
    known_curves = known_curves or set()
    paths = animated_node_paths(
        document,
        {int(channel["target"]["node"]) for channel in channels},
        {path for unused_field, path in known_curves},
    )
    result: dict[str, list[dict[str, Any]]] = {field: [] for field in CURVE_FIELDS.values()}
    for channel in channels:
        node = int(channel["target"]["node"])
        target_path = str(channel["target"]["path"])
        field = CURVE_FIELDS[target_path]
        path = paths.get(node)
        if path is None:
            continue
        sampler_index = int(channel.get("sampler", -1))
        if not 0 <= sampler_index < len(samplers):
            continue
        sampler = samplers[sampler_index]
        times = [row[0] for row in accessor_values(document, binary, int(sampler["input"]))]
        samples = accessor_values(document, binary, int(sampler["output"]))
        if not times:
            continue
        keys = build_keyframes(
            times, samples, str(sampler.get("interpolation", "LINEAR")).upper(), target_path
        )
        if known_curves and (field, path) not in known_curves and is_constant(keys):
            continue
        result[field].append({"curve": {"m_Curve": keys}, "path": path})
    return result

def original_curve_keys(document: dict[str, Any]) -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    for field in CURVE_FIELDS.values():
        for entry in document.get(field) or []:
            if isinstance(entry, dict):
                keys.add((field, str(entry.get("path", ""))))
    for entry in document.get("m_EulerCurves") or []:
        if isinstance(entry, dict):
            keys.add(("m_RotationCurves", str(entry.get("path", ""))))
    for entry in document.get("m_CompressedRotationCurves") or []:
        if isinstance(entry, dict):
            keys.add(("m_RotationCurves", str(entry.get("m_Path", ""))))
    return keys

def apply_glb_curves(document: dict[str, Any], data: bytes, animation_index: int = 0) -> dict[str, Any]:
    check_legacy_target(document)
    known_curves = original_curve_keys(document)
    rebuilt = curves_from_glb(data, animation_index, known_curves)
    if not any(rebuilt.values()):
        raise AnimationGlbImportError("the glTF animation has no transform channels")
    known_paths = {path for unused_field, path in known_curves}
    rebuilt_paths = {entry["path"] for curves in rebuilt.values() for entry in curves}
    if known_paths and not known_paths & rebuilt_paths:
        raise AnimationGlbImportError(
            "none of the GLB bones match the bones this clip animates "
            f"(for example {sorted(known_paths)[0]!r}), keep the exported bone names"
        )
    updated = dict(document)
    for field, curves in rebuilt.items():
        originals = {}
        for entry in document.get(field) or []:
            if isinstance(entry, dict):
                originals[str(entry.get("path", ""))] = entry.get("curve") or {}
        merged = []
        for entry in curves:
            source = originals.get(entry["path"], {})
            curve = dict(entry["curve"])
            curve["m_PreInfinity"] = int(source.get("m_PreInfinity", 2))
            curve["m_PostInfinity"] = int(source.get("m_PostInfinity", 2))
            curve["m_RotationOrder"] = int(source.get("m_RotationOrder", 4))
            merged.append({"curve": curve, "path": entry["path"]})
        updated[field] = merged
    updated["m_CompressedRotationCurves"] = []
    updated["m_EulerCurves"] = []
    updated["m_Compressed"] = False
    return updated

def check_legacy_target(document: dict[str, Any]) -> None:
    if document.get("m_Legacy") in (None, True, 1):
        return
    muscle = document.get("m_MuscleClip") or {}
    clip = muscle.get("m_Clip") or {}
    dense = clip.get("m_DenseClip") or {}
    streamed = clip.get("m_StreamedClip") or {}
    constant = clip.get("m_ConstantClip") or {}
    has_muscle_data = bool(
        int(dense.get("m_FrameCount", 0) or 0)
        or (streamed.get("data") or streamed.get("m_Data"))
        or (constant.get("data") or constant.get("m_Data"))
        or int(document.get("m_MuscleClipSize", 0) or 0)
    )
    if not has_muscle_data:
        return
    raise AnimationGlbImportError(
        f"{document.get('m_Name') or 'this clip'} is a Mecanim clip, so GLB curve edits cant change it"
    )

def summarize_curves(document: dict[str, Any]) -> dict[str, int]:
    return {
        "positionCurves": len(document.get("m_PositionCurves") or []),
        "rotationCurves": len(document.get("m_RotationCurves") or []),
        "scaleCurves": len(document.get("m_ScaleCurves") or []),
    }
